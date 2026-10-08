"""Local Duix adapter through the existing Fusion engine and its GPU lease."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import wave
from concurrent.futures import CancelledError
from contextlib import closing
from pathlib import Path

import requests

from . import extract, store

_start_lock = threading.Lock()
_JOB_TIMEOUT = 3600
_POLL_SECONDS = 2
_ARTIFACT_NAMES = ("final.mp4", "narration.wav", "subtitles.srt", "timeline.json")
_LEGACY_ROOT = Path(r"D:\HeyGemFusion")


def _read_settings(path):
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (OSError, ValueError, TypeError):
        raise RuntimeError("本机 Duix 接入配置无法读取，请检查接入 settings.json。") from None


def _configured_path(value, base, label):
    if not isinstance(value, str) or not value.strip() or re.search(r"[\x00-\x1f]", value):
        raise RuntimeError(f"本机 Duix 的{label}配置无效。")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _settings():
    integration = store.data_root() / "integrations" / "duix"
    local_file = integration / "settings.json"
    root_override = os.environ.get("MPT_FUSION_ROOT", "").strip()
    custom = {}
    if root_override:
        root = _configured_path(root_override, Path.cwd(), "接入目录")
        settings_file = root / "settings.json"
        custom = _read_settings(settings_file)
        origin = "environment"
    elif local_file.is_file():
        custom = _read_settings(local_file)
        root_value = custom.get("root") or custom.get("fusion_root")
        root = _configured_path(root_value, integration, "接入目录") if root_value else integration.resolve()
        # Existing service settings supply defaults; the local integration wins.
        if root / "settings.json" != local_file:
            custom = {**_read_settings(root / "settings.json"), **custom}
        settings_file, origin = local_file, "integration"
    elif (_LEGACY_ROOT / "settings.json").is_file():
        root = _LEGACY_ROOT.resolve()
        settings_file = root / "settings.json"
        custom = _read_settings(settings_file)
        origin = "legacy"
    else:
        root, settings_file, origin = integration.resolve(), local_file, "unconfigured"
    try:
        ffmpeg = extract.ffmpeg_binary() if not custom.get("ffmpeg") else custom["ffmpeg"]
    except (RuntimeError, OSError):
        ffmpeg = ""
    cfg = {
        "root": root,
        "python": sys.executable,
        "hey_db": str(Path.home() / "AppData/Roaming/Duix.Avatar/biz.db"),
        "ffmpeg": ffmpeg,
        "port": 18600,
        "configured": origin != "unconfigured" and root.is_dir(),
        "configuration_source": origin,
        "settings_file": settings_file,
        "legacy_deployment": root == _LEGACY_ROOT.resolve() and (root / "settings.json").is_file(),
    }
    cfg.update({key: custom[key] for key in ("python", "hey_db", "port", "ffmpeg", "avatar_root", "avatar_url") if key in custom})
    for key, label in (("python", "Python"), ("hey_db", "人物数据库")):
        cfg[key] = str(_configured_path(cfg[key], root, label))
    if cfg["ffmpeg"]:
        if not isinstance(cfg["ffmpeg"], str) or re.search(r"[\x00-\x1f]", cfg["ffmpeg"]):
            raise RuntimeError("本机 Duix 的音视频工具配置无效。")
        detected = shutil.which(cfg["ffmpeg"])
        cfg["ffmpeg"] = detected or str(_configured_path(cfg["ffmpeg"], root, "音视频工具"))
    database_override = os.environ.get("MPT_DUIX_DB", "").strip()
    if database_override:
        cfg["hey_db"] = str(_configured_path(database_override, root, "人物数据库"))
    cfg["port"] = _positive_id(cfg["port"])
    if cfg["port"] > 65535:
        raise ValueError("本机 Duix 服务端口无效。")
    return cfg


def _positive_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value)):
        raise ValueError("请选择有效的数字人或音色。")
    return int(value)


def list_profiles() -> dict:
    """Read only public profile columns; never expose reference audio or text."""
    cfg = _settings()
    if cfg.get("configured") is False:
        return {"models": [], "voices": [], "message": "本机 Duix 尚未配置，基础配音与图文成片可继续使用。"}
    db_path = Path(cfg["hey_db"]).resolve()
    if not db_path.is_file():
        return {"models": [], "voices": [], "message": "未找到本机 Duix 音色数据库。"}
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            models = [
                {"id": ident, "name": str(name or f"数字人 {ident}")[:120], "voice_id": voice_id}
                for ident, name, voice_id in db.execute("SELECT id,name,voice_id FROM f2f_model ORDER BY id")
            ]
            voices = [
                {"id": ident, "name": f"本机音色 {ident}", "lang": lang}
                for ident, lang in db.execute("SELECT id,lang FROM voice ORDER BY id")
            ]
    except sqlite3.Error:
        raise RuntimeError("无法读取本机 Duix 人物和音色，请确认 Duix 已完成初始化。") from None
    return {"models": models, "voices": voices}


def _session():
    session = requests.Session()
    session.trust_env = False
    return session


def _service_health(session, base):
    try:
        response = session.get(base + "/health", timeout=2)
        return response.ok and response.json().get("app") == "HeyGemFusion"
    except (requests.RequestException, ValueError):
        return False


def _engine_busy(root):
    lock = root / "engine.lock"
    if not lock.is_file() or not lock.stat().st_size:
        return False
    with lock.open("r+b") as handle:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def _connect(progress=None):
    cfg = _settings()
    if cfg.get("configured") is False:
        raise RuntimeError("本机 Duix 尚未配置，请先配置数字人接入目录；基础配音与图文成片无需 Duix。")
    base = f"http://127.0.0.1:{cfg['port']}"
    session = _session()
    with _start_lock:
        if not _service_health(session, base):
            if not (cfg["root"] / "server.py").is_file() or not Path(cfg["python"]).is_file():
                raise RuntimeError("未找到现有 Duix 融合服务，请检查本机安装路径。")
            if _engine_busy(cfg["root"]):
                raise RuntimeError("已有 Duix 任务正在生成，请完成后再提交。")
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", cfg["port"]))
                except OSError:
                    raise RuntimeError("Duix 接入端口被其他程序占用，请调整融影工作台端口。") from None
            _report(progress, "启动本机 Duix 接入服务", 2)
            log_dir = store.data_root() / "duix"
            log_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", FUSION_ROOT=str(cfg["root"]))
            with (log_dir / "service.log").open("a", encoding="utf-8") as log:
                proc = subprocess.Popen(
                    [cfg["python"], "-X", "utf8", "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(cfg["port"])],
                    cwd=cfg["root"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            store.save_record("duix_service", "local", {"pid": proc.pid, "port": cfg["port"], "root": str(cfg["root"])})
            deadline = time.monotonic() + 60
            while not _service_health(session, base):
                if proc.poll() is not None:
                    raise RuntimeError(f"本机 Duix 接入服务启动失败，日志：{log_dir / 'service.log'}")
                if time.monotonic() >= deadline:
                    proc.terminate()
                    raise RuntimeError("本机 Duix 接入服务启动超时，请稍后重试。")
                time.sleep(0.5)
    # Fusion supplies an HttpOnly session cookie through its homepage.
    response = session.get(base + "/", timeout=10)
    if not response.ok or not session.cookies.get("fusion_session"):
        raise RuntimeError("本机 Duix 接入会话建立失败。")
    return session, base, cfg


def _report(progress, message, percent=None):
    if progress is not None and progress(message, percent) is False:
        raise CancelledError("任务已取消，已生成的结果仍保留在本机。")


def _api(session, base, path, method="GET", **kwargs):
    try:
        response = session.request(method, base + path, timeout=(5, 20), **kwargs)
        if not response.ok:
            try:
                detail = str(response.json().get("detail", ""))[:300]
            except ValueError:
                detail = ""
            raise RuntimeError(detail or f"本机 Duix 接口失败（HTTP {response.status_code}）。")
        return response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("无法连接本机 Duix 接入服务，请检查 Docker 与本机服务后重试。") from None


def _split_script(script: str, audio_only=False):
    script = str(script or "").strip()
    if not script or len(script) > 600:
        raise ValueError("请输入 1～600 字文案；长文案请分成多个视频。")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", script):
        raise ValueError("文案包含无法处理的控制字符。")
    # Conservative phrase lengths keep 6 GB GPU clips below the engine's 10 s limit.
    size = 100 if audio_only else 24
    parts = [part.strip() for part in re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", script) if part.strip()]
    chunks = []
    for part in parts:
        while len(part) > size:
            split = max(part.rfind("，", 1, size + 1), part.rfind(",", 1, size + 1), part.rfind(" ", 1, size + 1))
            cut = split + 1 if split >= max(4, size // 2) else size
            chunks.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            chunks.append(part)
    if len(chunks) > 20:
        raise ValueError("此文案拆分后超过 20 个片段，请分成多个视频。")
    return [{"text": part, "kind": "avatar"} for part in chunks]


def _ensure_docker(progress=None):
    _report(progress, "检查本机 Duix 引擎", 1)
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Duix 需要 Docker Desktop 引擎，请启动 Docker Desktop，待引擎就绪后重新提交。") from None
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("Docker Desktop 引擎尚未就绪，请等它启动完成后重新提交 Duix 任务。")


def _create_clean_video(source, target):
    """Mux the uncaptioned picture with narration; keep the finished preview."""
    picture = (source / "picture.mp4").resolve()
    if not picture.is_relative_to(source) or not picture.is_file() or picture.stat().st_size == 0:
        return "", "原数字人任务没有可用于重新剪辑的无字幕画面，已保留带字幕成片。"
    copied_picture = target / "picture.mp4"
    shutil.copy2(picture, copied_picture)
    clean = target / "clean.mp4"
    executable = os.environ.get("FFMPEG_BINARY") or _settings()["ffmpeg"]
    if not Path(executable).is_file():
        executable = shutil.which(str(executable)) or shutil.which("ffmpeg")
    if not executable:
        return "", "未找到 FFmpeg，无字幕编辑源尚未生成；现有成片可以直接预览和发布。"
    try:
        result = subprocess.run(
            [str(executable), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(copied_picture), "-i", str(target / "narration.wav"),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
             "-shortest", "-movflags", "+faststart", str(clean)],
            capture_output=True, timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode or not clean.is_file() or clean.stat().st_size == 0:
            raise RuntimeError("no clean video")
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        clean.unlink(missing_ok=True)
        return "", "无字幕编辑源混流失败，已保留带字幕成片；可以直接预览和发布。"
    return str(clean), ""


def _copy_results(root, remote_id, target, audio_only):
    if not re.fullmatch(r"[a-f0-9]{32}", remote_id):
        raise RuntimeError("Duix 返回了无效任务编号。")
    jobs_root = (root / "jobs").resolve()
    source = (jobs_root / remote_id).resolve()
    if not source.is_relative_to(jobs_root):
        raise RuntimeError("Duix 结果路径无效。")
    required = {"narration.wav", "subtitles.srt", "timeline.json"}
    if not audio_only:
        required.add("final.mp4")
    target.mkdir(parents=True, exist_ok=True)
    for name in _ARTIFACT_NAMES:
        path = (source / name).resolve()
        if not path.is_relative_to(source) or (name in required and (not path.is_file() or path.stat().st_size == 0)):
            raise RuntimeError(f"Duix 尚未生成有效结果：{name}")
        if path.is_file():
            shutil.copy2(path, target / name)
    try:
        with wave.open(str(target / "narration.wav"), "rb") as wav:
            frames = wav.getnframes()
            expected = frames * wav.getnchannels() * wav.getsampwidth()
            if frames <= 0 or expected > 128 * 1024 * 1024 or len(wav.readframes(frames)) != expected:
                raise wave.Error("empty")
    except (OSError, wave.Error):
        raise RuntimeError("Duix 配音结果不是有效 WAV 音频。") from None
    result = {
        "audio_path": str(target / "narration.wav"), "srt_path": str(target / "subtitles.srt"),
        "timeline_path": str(target / "timeline.json"),
        "video_path": str(target / "final.mp4") if not audio_only else "",
    }
    if not audio_only:
        clean_path, warning = _create_clean_video(source, target)
        result.update(clean_video_path=clean_path, subtitles_burned=True)
        if warning:
            result["edit_warning"] = warning
    return result


def _run(script, model_id, voice_id, aspect, progress, audio_only=False):
    voice_id = _positive_id(voice_id)
    model_id = _positive_id(model_id) if not audio_only else 1
    if aspect not in {"9:16", "16:9"}:
        raise ValueError("数字人视频支持 9:16 和 16:9。")
    segments = _split_script(script, audio_only)
    profiles = list_profiles()
    if not any(row["id"] == voice_id for row in profiles["voices"]):
        raise ValueError("所选本机音色不存在，请刷新声音列表。")
    if not audio_only and not any(row["id"] == model_id for row in profiles["models"]):
        raise ValueError("所选数字人不存在，请刷新人物列表。")
    _ensure_docker(progress)
    session, base, cfg = _connect(progress)
    remote_id = None
    ident = store.new_id()
    store.save_record("duix_jobs", ident, {"state": "preparing", "audio_only": audio_only, "model_id": model_id, "voice_id": voice_id})
    try:
        jobs = _api(session, base, "/api/jobs")
        if any(job.get("state") in {"queued", "running"} for job in jobs) or _engine_busy(cfg["root"]):
            raise RuntimeError("已有 Duix 任务正在生成，请完成后再提交。")
        # The unchanged Fusion worker serializes inference and manages GPU leases.
        result = _api(session, base, "/api/jobs", "POST", json={
            "title": "MoneyPrinterTurbo 配音" if audio_only else "MoneyPrinterTurbo 数字人口播",
            "model_id": model_id, "voice_id": voice_id, "aspect": aspect,
            "segments": segments, "audio_only": audio_only,
        })
        remote_id = str(result.get("id", ""))
        if not re.fullmatch(r"[a-f0-9]{32}", remote_id):
            remote_id = None
            raise RuntimeError("Duix 返回了无效任务编号。")
        store.update_record("duix_jobs", ident, {"state": "running", "fusion_id": remote_id})
        deadline = time.monotonic() + _JOB_TIMEOUT
        while time.monotonic() < deadline:
            jobs = _api(session, base, "/api/jobs")
            job = next((job for job in jobs if job.get("id") == remote_id), None)
            if job is None:
                raise RuntimeError("本机 Duix 任务记录丢失，请检查融合服务。")
            _report(progress, str(job.get("stage") or "Duix 等待处理"), job.get("progress", 0))
            if job.get("state") in {"failed", "cancelled"}:
                if job["state"] == "cancelled":
                    raise CancelledError("Duix 任务已取消，已生成片段仍保留。")
                raise RuntimeError("Duix 生成失败：" + str(job.get("error") or job.get("stage") or "请检查本机服务")[:500])
            if job.get("state") == "done":
                paths = _copy_results(cfg["root"], remote_id, store.data_root() / "duix" / ident, audio_only)
                result = {"id": ident, "fusion_id": remote_id, **paths, "warnings": job.get("warnings", [])}
                store.update_record("duix_jobs", ident, {"state": "done", **result})
                _report(progress, "本机配音已完成" if audio_only else "数字人口播已完成", 100)
                return result
            time.sleep(_POLL_SECONDS)
        raise TimeoutError("Duix 生成超过一小时，已请求停止；任务编号已保留，可查看原融合工作台记录。")
    except BaseException as exc:
        # Do not leave a remote GPU job running after cancellation or lost polling.
        if remote_id:
            try:
                _api(session, base, f"/api/jobs/{remote_id}/cancel", "POST")
            except RuntimeError:
                pass
        store.update_record("duix_jobs", ident, {"state": "cancelled" if isinstance(exc, (CancelledError, KeyboardInterrupt)) else "failed", "error": str(exc)[:500]})
        raise
    finally:
        session.close()


def generate(script, model_id, voice_id, aspect="9:16", progress=None) -> dict:
    return _run(script, model_id, voice_id, aspect, progress)


def generate_audio(script, voice_id, progress=None) -> dict:
    """Use an existing local Duix voice; this does not train a new voice."""
    return _run(script, 1, voice_id, "9:16", progress, audio_only=True)
