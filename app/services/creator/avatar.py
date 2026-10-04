"""Audio-driven local Duix avatars without a second speech synthesis pass.

The native engine consumes files inside its mounted face2face temp directory.
Fusion's process lock and service lease protect the same GPU and containers.
Only video references are supported; this adapter never writes Duix's database.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import wave
from concurrent.futures import CancelledError
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import duix, extract, store

MAX_VIDEO_BYTES = 500 * 1024 * 1024
MAX_VIDEO_SECONDS = 120
MAX_AUDIO_BYTES = 128 * 1024 * 1024
MAX_AUDIO_SECONDS = 600
_CHUNK_SECONDS = 8
_CONTAINERS = ("duix-avatar-asr", "duix-avatar-tts", "duix-avatar-gen-video")
_PROCESS_LOCK = threading.Lock()
_STATUS_LOCK = threading.Lock()
_STATUS_CACHE = {}


class _EngineMissingError(RuntimeError):
    """Docker is running, but the separate native avatar engine is absent."""


def _configuration():
    cfg = duix._settings()
    cfg.update(avatar_root=Path(r"D:\duix_avatar_data\face2face\temp"),
               avatar_url="http://127.0.0.1:8383/easy")
    settings_file = cfg["root"] / "settings.json"
    if settings_file.is_file():
        try:
            custom = json.loads(settings_file.read_text("utf-8"))
            cfg.update({key: custom[key] for key in ("avatar_root", "avatar_url") if key in custom})
        except (OSError, ValueError, TypeError):
            raise RuntimeError("无法读取本机数字人接入配置，请检查融影工作台 settings.json。") from None
    cfg["root"] = Path(cfg["root"]).resolve()
    cfg["avatar_root"] = Path(cfg["avatar_root"]).expanduser().resolve()
    parsed = urlsplit(str(cfg["avatar_url"]))
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") != "/easy"):
        raise RuntimeError("数字人口型接口必须使用本机 http 地址和 /easy 路径。")
    cfg["avatar_url"] = str(cfg["avatar_url"]).rstrip("/")
    return cfg


def _model_path(cfg, value):
    root = cfg["avatar_root"]
    path = (root / str(value or "").replace("\\", "/")).resolve()
    if not str(value or "") or not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def list_options() -> list[dict]:
    """Expose reference videos, never voice samples, text or writable DB handles."""
    cfg = _configuration()
    options = []
    database = Path(cfg["hey_db"]).resolve()
    if database.is_file():
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                rows = list(db.execute("SELECT id,name,video_path FROM f2f_model ORDER BY id"))
            for ident, name, reference in rows:
                path = _model_path(cfg, reference)
                options.append({"id": f"duix:{ident}", "name": str(name or f"数字人 {ident}")[:80],
                                "source": "duix", "video_path": str(path) if path else "",
                                "available": path is not None,
                                "reason": "" if path else "参考视频已缺失，请重新导入人物视频。"})
        except sqlite3.Error:
            raise RuntimeError("无法读取 Duix 人物列表，请确认本机数据库可读。") from None
    for row in store.list_records("avatar_profiles"):
        path = _model_path(cfg, row.get("video_path"))
        options.append({**row, "id": f"local:{row['id']}", "source": "local",
                        "video_path": str(path) if path else "", "available": path is not None,
                        "reason": "" if path else "参考视频已缺失，请重新导入人物视频。"})
    return options


def _run_media(arguments, *, timeout=600):
    try:
        result = subprocess.run([extract.ffmpeg_binary(), "-nostdin", "-hide_banner", "-xerror", "-y", *map(str, arguments)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError("数字人音视频处理超时，请缩短素材后重试。") from None
    except OSError:
        raise RuntimeError("无法执行音视频工具，请检查 FFmpeg 路径。") from None
    if result.returncode:
        raise RuntimeError("音视频文件损坏、不完整或格式无法处理；无需重复安装 FFmpeg。\n" +
                           (result.stderr or "音视频工具没有返回有效结果").strip()[-350:])
    return result


def _video_info(path):
    metadata = extract.probe_media(path, require_audio=False)
    result = _run_media(["-loglevel", "info", "-i", path, "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"], timeout=60)
    # Mapping and decoding one frame proves this is video even without ffprobe.
    if "Video:" not in result.stderr:
        raise ValueError("人物素材没有有效视频画面，请上传 MP4 或 MOV 视频。")
    duration = float(metadata.get("duration") or 0)
    if not math.isfinite(duration) or not 0.1 <= duration <= MAX_VIDEO_SECONDS:
        raise ValueError(f"人物参考视频支持 0.1～{MAX_VIDEO_SECONDS} 秒，请先剪短视频。")
    return duration


def import_profile(name, video_path) -> dict:
    name = str(name or "").strip()
    if not name or len(name) > 80 or re.search(r"[\x00-\x1f]", name):
        raise ValueError("请输入 1～80 字人物名称。")
    source = Path(str(video_path or "")).expanduser().resolve()
    if source.suffix.lower() not in {".mp4", ".mov"}:
        raise ValueError("人物形象目前支持 MP4、MOV 视频，图片数字人尚未接入。")
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_VIDEO_BYTES:
        raise ValueError("请上传有效人物视频，大小不超过 500MB。")
    cfg = _configuration()
    if not cfg["avatar_root"].is_dir():
        raise RuntimeError("未找到 Duix 视频共享目录，请先完成本机 Duix 安装。")
    _video_info(source)
    ident = store.new_id()
    target = cfg["avatar_root"] / f"mpt-profile-{ident}.mp4"
    try:
        _run_media(["-loglevel", "error", "-i", source, "-map", "0:v:0", "-an",
                    "-vf", "scale=720:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=25",
                    "-t", str(MAX_VIDEO_SECONDS + 0.1), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-threads", "2", "-pix_fmt", "yuv420p", "-movflags", "+faststart", target])
        duration = _video_info(target)
        record = store.save_record("avatar_profiles", ident, {"name": name, "video_path": str(target),
                                    "duration": duration, "source_name": source.name, "state": "ready"})
        return {**record, "id": f"local:{ident}", "source": "local", "available": True, "reason": ""}
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _docker_binary():
    executable = shutil.which("docker")
    fallback = Path.home() / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe"
    if not executable and fallback.is_file():
        executable = str(fallback)
    if not executable:
        raise RuntimeError("未找到 Docker Desktop，请先完成本机 Duix 安装。")
    return executable


def _docker(arguments, *, timeout=12):
    try:
        result = subprocess.run([_docker_binary(), *arguments], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError("Docker Desktop 响应超时，请确认引擎已启动后再试。") from None
    except OSError:
        raise RuntimeError("无法连接 Docker Desktop，请确认引擎已启动。") from None
    if result.returncode:
        raise RuntimeError("Docker Desktop 引擎未就绪，或所需 Duix 容器不存在；请打开 Docker 并检查本机 Duix 安装。")
    return result.stdout


def _service_states():
    installed = set(_docker(["ps", "-a", "--format", "{{.Names}}"]).splitlines())
    names = [name for name in _CONTAINERS if name in installed]
    if "duix-avatar-gen-video" not in names:
        raise _EngineMissingError("Docker 已启动，但尚未安装数字人口型容器 duix-avatar-gen-video，请先安装本机数字人引擎。")
    try:
        rows = json.loads(_docker(["inspect", *names]))
        states = {row["Name"].lstrip("/"): bool(row["State"]["Running"]) for row in rows}
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("无法读取 Duix 容器状态，请检查 Docker Desktop。") from None
    if set(states) != set(names):
        raise RuntimeError("Duix 容器状态无法完整读取，请检查 Docker Desktop。")
    return states


def _duix_busy(cfg):
    database = Path(cfg["hey_db"]).resolve()
    if not database.is_file():
        return False
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            return db.execute("SELECT 1 FROM video WHERE status IN ('pending','waiting') LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        raise RuntimeError("无法确认原 Duix 任务状态，请关闭其未完成任务后重试。") from None


def status() -> dict:
    """Read-only health probe; cached briefly and never starts engines or services."""
    try:
        cfg = _configuration()
    except (RuntimeError, ValueError, OSError) as exc:
        return {"available": False, "docker_ready": False, "models_count": 0, "reason": str(exc)}
    cache_key = (str(cfg["root"]), str(cfg["avatar_root"]), str(cfg["hey_db"]))
    with _STATUS_LOCK:
        cached = _STATUS_CACHE.get(cache_key)
        if cached and time.monotonic() - cached[0] < 10:
            return dict(cached[1])
        result = {"available": False, "docker_ready": False, "models_count": 0, "reason": ""}
        try:
            result["models_count"] = sum(bool(row["available"]) for row in list_options())
            if not cfg["root"].is_dir() or not cfg["avatar_root"].is_dir():
                raise RuntimeError("未找到 Duix 共享目录或融影接入目录，请检查本机安装路径。")
            _service_states()
            result["docker_ready"] = True
            if duix._engine_busy(cfg["root"]) or _PROCESS_LOCK.locked() or _duix_busy(cfg):
                raise RuntimeError("已有 Duix 或融影任务正在生成，请等它完成后再提交。")
            if _recovery_path(cfg):
                _owned_recovery(cfg)
                result.update(recovery_needed=True, reason="上次数字人任务已中断，请先点击恢复数字人引擎。")
            if not result["models_count"]:
                raise RuntimeError("请先导入人物参考视频，或恢复 Duix 中已有人物的参考视频。")
            result["available"] = not result.get("recovery_needed", False)
        except (RuntimeError, ValueError, OSError) as exc:
            result["reason"] = str(exc)
            if isinstance(exc, _EngineMissingError):
                result["docker_ready"] = True
        _STATUS_CACHE[cache_key] = (time.monotonic(), dict(result))
        return result


def list_jobs() -> list[dict]:
    return store.list_records("avatar_jobs")


def _atomic_json(path, value):
    temporary = path.with_name(path.name + "." + store.new_id() + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _recovery_path(cfg):
    paths = [cfg["root"] / name for name in ("service-lease.json", "avatar-service-lease.json")
             if (cfg["root"] / name).is_file()]
    if len(paths) > 1:
        raise RuntimeError("发现多个未恢复的数字人服务记录，请先检查原融影工作台；没有修改任何服务。")
    return paths[0] if paths else None


def _owned_recovery(cfg):
    path = _recovery_path(cfg)
    try:
        if path is None:
            raise ValueError("missing recovery state")
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid recovery object")
        if data.get("owner") != "mpt_avatar":
            raise RuntimeError("其他融影任务的服务恢复未完成，请先在原融影工作台恢复；没有修改其他任务记录。")
        initial = data["initial"]
        if (not re.fullmatch(r"[a-f0-9]{32}", str(data.get("job") or ""))
                or not isinstance(initial, dict) or "duix-avatar-gen-video" not in initial
                or not set(initial).issubset(_CONTAINERS) or any(not isinstance(value, bool) for value in initial.values())):
            raise ValueError("invalid recovery state")
        data["_path"] = path
        return data
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("数字人引擎恢复记录异常，请检查本机 avatar-service-lease.json；没有修改原服务。") from None


def _restore_owned_recovery(cfg):
    data = _owned_recovery(cfg)
    # An interrupted native task may still own the listener. Reset only our
    # leased avatar container before restoring the captured original states.
    _set_running("duix-avatar-gen-video", False)
    for name, running in data["initial"].items():
        _set_running(name, running)
    data["_path"].unlink()
    previous = store.get_record("avatar_jobs", data["job"])
    if previous and previous.get("state") in {"preparing", "running"}:
        store.update_record("avatar_jobs", data["job"], {"state": "failed", "error": "上次任务已中断；引擎已恢复，原配音和已生成片段仍保留。"})


@contextmanager
def _gpu_lease(cfg, ident):
    """Use Fusion's existing byte lock and recovery-compatible service lease."""
    if not _PROCESS_LOCK.acquire(blocking=False):
        raise RuntimeError("已有数字人任务占用本机引擎，请完成后再提交。")
    handle = None
    locked = False
    initial = None
    warnings = []
    lease_file = cfg["root"] / "service-lease.json"
    try:
        if not cfg["root"].is_dir():
            raise RuntimeError("未找到融影接入目录，无法安全取得数字人引擎。")
        handle = (cfg["root"] / "engine.lock").open("a+b")
        if not handle.seek(0, os.SEEK_END):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise RuntimeError("已有融影任务占用本机引擎，请完成后再提交。") from None
        if _recovery_path(cfg):
            _owned_recovery(cfg)
        if _duix_busy(cfg):
            raise RuntimeError("原 Duix 仍有生成任务，请完成后再提交。")
        if _recovery_path(cfg):
            _restore_owned_recovery(cfg)
        initial = _service_states()
        if set(initial) != set(_CONTAINERS):
            # Old Fusion recovery expects all three containers. Avoid giving
            # it a partial lease that it cannot restore; the byte lock remains
            # shared and both outstanding recovery files block new jobs.
            lease_file = cfg["root"] / "avatar-service-lease.json"
        _atomic_json(lease_file, {"job": ident, "initial": initial, "owner": "mpt_avatar"})
        yield warnings
    finally:
        try:
            if initial is not None:
                for name in initial:
                    try:
                        _set_running(name, initial[name])
                    except (RuntimeError, OSError) as exc:
                        warnings.append(f"{name} 恢复失败：{str(exc)[:200]}")
                if not warnings:
                    try:
                        lease_file.unlink(missing_ok=True)
                    except OSError:
                        warnings.append("引擎状态已恢复，但恢复记录无法清理，请检查本机目录权限。")
        finally:
            try:
                if locked:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                if handle:
                    handle.close()
                _PROCESS_LOCK.release()
                _STATUS_CACHE.clear()


def _set_running(name, needed):
    states = _service_states()
    if name not in states:
        if needed:
            raise RuntimeError(f"数字人所需容器 {name} 不存在，请检查本机引擎安装。")
        return
    if states[name] != needed:
        _docker(["start" if needed else "stop", name], timeout=80)


def recover_engine(progress=None) -> dict:
    """Explicit queued recovery of this adapter's interrupted service lease."""
    cfg = _configuration()
    duix._report(progress, "检查数字人引擎恢复记录", 5)
    # The shared lock validates and restores ownership before taking a fresh
    # lease. Foreign Fusion recovery records are always refused unchanged.
    with _gpu_lease(cfg, store.new_id()) as warnings:
        duix._report(progress, "恢复原有数字人引擎状态", 90)
    if warnings:
        raise RuntimeError("数字人引擎尚未完整恢复：" + "；".join(warnings))
    duix._report(progress, "数字人引擎已恢复", 100)
    return {"state": "done", "message": "数字人引擎已恢复，可以重新提交口播任务。", "warnings": []}


def _wav_duration(path):
    try:
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
            frame_bytes = audio.getnchannels() * audio.getsampwidth()
            if frames <= 0 or frames * frame_bytes > MAX_AUDIO_BYTES:
                raise wave.Error("empty or too large")
            actual = 0
            while block := audio.readframes(8192):
                if len(block) % frame_bytes:
                    raise wave.Error("truncated sample")
                actual += len(block) // frame_bytes
            if actual != frames:
                raise wave.Error("truncated audio")
            duration = frames / audio.getframerate()
        if not 0.1 <= duration <= MAX_AUDIO_SECONDS:
            raise ValueError(f"数字人配音支持 0.1～{MAX_AUDIO_SECONDS} 秒，请先分段。")
        return duration
    except (OSError, wave.Error, EOFError, ZeroDivisionError):
        raise ValueError("配音 WAV 损坏、不完整或没有声音，请重新上传；无需重复安装 FFmpeg。") from None


def _prepare_audio(source, folder):
    if source.suffix.lower() not in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}:
        raise ValueError("请选择 WAV、MP3、M4A、AAC、FLAC 或 OGG 配音音频。")
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_AUDIO_BYTES:
        raise ValueError("请提供有效配音音频，大小不超过 128MB。")
    if source.suffix.lower() == ".wav":
        _wav_duration(source)
    metadata = extract.probe_media(source)
    if float(metadata.get("duration") or 0) > MAX_AUDIO_SECONDS:
        raise ValueError(f"数字人配音最多 {MAX_AUDIO_SECONDS} 秒，请先分段。")
    original = folder / ("original" + source.suffix.lower())
    shutil.copy2(source, original)
    normalized = folder / "narration.wav"
    _run_media(["-loglevel", "error", "-i", original, "-map", "0:a:0", "-vn",
                "-t", str(MAX_AUDIO_SECONDS + 0.1), "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", normalized])
    duration = _wav_duration(normalized)
    return normalized, original, duration


def _audio_chunks(audio, folder):
    """Split PCM on exact sample boundaries; never pad or synthesize speech."""
    chunks = []
    with wave.open(str(audio), "rb") as source:
        rate = source.getframerate()
        cursor = 0
        while data := source.readframes(rate * _CHUNK_SECONDS):
            frames = len(data) // (source.getnchannels() * source.getsampwidth())
            path = folder / f"audio-{len(chunks):02d}.wav"
            with wave.open(str(path), "wb") as target:
                target.setparams(source.getparams())
                target.writeframes(data)
            chunks.append({"path": path, "start": cursor / rate, "duration": frames / rate})
            cursor += frames
    return chunks


def _wait_native(session, cfg, progress):
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        duix._report(progress, "等待本机数字人口型服务就绪", 8)
        try:
            response = session.get(cfg["avatar_url"] + "/query", params={"code": "mpt-avatar-health"}, timeout=3)
            if response.ok and isinstance(response.json(), dict):
                return
        except (requests.RequestException, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError("数字人口型服务启动超时，请检查 Docker 中 duix-avatar-gen-video 的日志。")


def _native_clip(session, cfg, chunk, reference, folder, ident, index, progress):
    shared_name = f"mpt-avatar-{ident}-{index:02d}.wav"
    shared_audio = cfg["avatar_root"] / shared_name
    shutil.copy2(chunk["path"], shared_audio)
    code = store.new_id()
    marker = folder / f"avatar-{index:02d}.json"
    payload = {"audio_url": shared_name, "video_url": reference.relative_to(cfg["avatar_root"]).as_posix(),
               "code": code, "chaofen": 0, "watermark_switch": 0, "pn": 1}
    _atomic_json(marker, {"code": code, "state": "submitting"})
    try:
        response = session.post(cfg["avatar_url"] + "/submit", json=payload, timeout=(5, 30))
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("invalid submit response")
        if data.get("code") != 10000:
            raise RuntimeError("数字人片段提交失败：" + str(data.get("msg") or "本机引擎拒绝任务")[:200])
        _atomic_json(marker, {"code": code, "state": "running"})
        deadline = time.monotonic() + 900
        cancel_requested = False
        while time.monotonic() < deadline:
            response = session.get(cfg["avatar_url"] + "/query", params={"code": code}, timeout=(5, 20))
            response.raise_for_status()
            answer = response.json()
            if not isinstance(answer, dict) or not isinstance(answer.get("data") or {}, dict):
                raise ValueError("invalid query response")
            state = answer.get("data") or {}
            if answer.get("code") == 10000 and state.get("status") == 2:
                relative = str(state.get("result") or "").replace("\\", "/").lstrip("/")
                remote = _model_path(cfg, relative)
                if remote is None:
                    raise RuntimeError("数字人引擎返回了无效或目录外的结果文件。")
                target = folder / f"avatar-{index:02d}.mp4"
                shutil.copy2(remote, target)
                _atomic_json(marker, {"code": code, "state": "done", "path": str(target)})
                if cancel_requested:
                    raise CancelledError("当前口型片段已完成并保留，数字人任务已取消。")
                return target
            if state.get("status") == 3 or answer.get("code") in {9999, 10002, 10003}:
                raise RuntimeError("数字人口型生成失败：" + str(state.get("msg") or answer.get("msg") or "请检查本机引擎")[:250])
            if progress is not None and progress("正在同步数字人口型" if not cancel_requested else "正在等待当前片段结束后取消", None) is False:
                cancel_requested = True
            time.sleep(2)
        raise RuntimeError("数字人片段生成超过 15 分钟，任务编号已保留，请检查本机引擎。")
    except (requests.RequestException, ValueError, TypeError):
        raise RuntimeError("数字人口型接口连接失败或返回无效数据，请检查本机数字人服务。") from None


def _assemble(clips, chunks, audio, folder, aspect):
    width, height = (720, 1280) if aspect == "9:16" else (1280, 720)
    normalized = []
    for index, (clip, chunk) in enumerate(zip(clips, chunks)):
        target = folder / f"clip-{index:02d}.mp4"
        # Eight-second boundaries coincide with 25fps frames. The final clip
        # may need <= one frame padding; the unchanged audio sets final length.
        video_filter = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x171b26,"
                        f"setsar=1,fps=25,tpad=stop_mode=clone:stop_duration={chunk['duration']:.6f}")
        _run_media(["-loglevel", "error", "-i", clip, "-map", "0:v:0", "-an", "-t", f"{chunk['duration']:.6f}",
                    "-vf", video_filter, "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-threads", "2",
                    "-pix_fmt", "yuv420p", target])
        normalized.append(target)
    manifest = folder / "concat.txt"
    manifest.write_text("".join(f"file '{path.name}'\n" for path in normalized), "utf-8")
    picture = folder / "picture.mp4"
    _run_media(["-loglevel", "error", "-f", "concat", "-safe", "0", "-i", manifest, "-an", "-c:v", "copy", picture])
    output = folder / "final.mp4"
    _run_media(["-loglevel", "error", "-i", picture, "-i", audio, "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", output])
    extract.probe_media(output)
    _run_media(["-loglevel", "error", "-i", output, "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], timeout=600)
    return output


def generate(audio_path, model_id, script="", aspect="9:16", progress=None, source_narration_id="") -> dict:
    """Drive native lips from an existing audio version, preserving its timing."""
    if aspect not in {"9:16", "16:9"}:
        raise ValueError("数字人视频支持 9:16 和 16:9。")
    if not re.fullmatch(r"(?:duix:[1-9]\d*|local:[a-f0-9]{32})", str(model_id or "")):
        raise ValueError("请选择有效的人物形象。")
    script = str(script or "").strip()
    if len(script) > 6000 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", script):
        raise ValueError("配音文案最多 6000 字，且不能包含控制字符。")
    source_narration_id = str(source_narration_id or "")
    if source_narration_id and not re.fullmatch(r"[a-f0-9]{32}", source_narration_id):
        raise ValueError("配音版本编号无效，请重新选择配音。")
    cfg = _configuration()
    option = next((row for row in list_options() if row["id"] == model_id), None)
    if not option or not option.get("available"):
        raise ValueError("所选人物不存在或参考视频已缺失，请刷新形象列表。")
    reference = Path(option["video_path"])
    _video_info(reference)
    source = Path(str(audio_path or "")).expanduser().resolve()
    if source_narration_id:
        narration = store.get_record("narrations", source_narration_id)
        if (not narration or narration.get("state") != "done" or narration.get("preview")
                or Path(str(narration.get("audio_path") or "")).resolve() != source):
            raise ValueError("所选正式配音已变化或不存在，请重新选择完整配音版本。")
    ident = store.new_id()
    folder = store.data_root() / "avatars" / ident
    folder.mkdir(parents=True, exist_ok=False)
    metadata = {"state": "preparing", "model_id": model_id, "model_name": option["name"], "script": script,
                "aspect": aspect, "source_audio_name": source.name, "source_narration_id": source_narration_id}
    store.save_record("avatar_jobs", ident, metadata)
    warnings = []
    session = requests.Session()
    session.trust_env = False
    try:
        duix._report(progress, "验证并准备已有配音", 2)
        audio, original, duration = _prepare_audio(source, folder)
        chunks = _audio_chunks(audio, folder)
        store.update_record("avatar_jobs", ident, {"audio_path": str(audio), "original_audio_path": str(original), "duration": duration})
        with _gpu_lease(cfg, ident) as warnings:
            duix._report(progress, "准备本机数字人口型引擎", 5)
            _set_running("duix-avatar-asr", False)
            _set_running("duix-avatar-tts", False)
            _set_running("duix-avatar-gen-video", True)
            _wait_native(session, cfg, progress)
            store.update_record("avatar_jobs", ident, {"state": "running", "chunk_count": len(chunks)})
            clips = []
            try:
                for index, chunk in enumerate(chunks):
                    duix._report(progress, f"生成口型片段 {index + 1}/{len(chunks)}", 12 + 65 * index / len(chunks))
                    clips.append(_native_clip(session, cfg, chunk, reference, folder, ident, index, progress))
            except BaseException:
                # The native API has no verified cancellation endpoint. Stop
                # our leased container before releasing the GPU on uncertainty.
                try:
                    _set_running("duix-avatar-gen-video", False)
                except RuntimeError as exc:
                    warnings.append("口型引擎停止失败：" + str(exc)[:200])
                raise
            _set_running("duix-avatar-gen-video", False)
            duix._report(progress, "拼接画面并保留完整原配音", 82)
            output = _assemble(clips, chunks, audio, folder, aspect)
        duix._report(progress, "数字人口播已完成", 100)
        return store.update_record("avatar_jobs", ident, {"state": "done", "video_path": str(output),
                                   "clean_video_path": str(output), "subtitles_burned": False, "warnings": warnings})
    except BaseException as exc:
        store.update_record("avatar_jobs", ident, {"state": "cancelled" if isinstance(exc, (CancelledError, KeyboardInterrupt)) else "failed",
                            "error": str(exc)[:1000], "warnings": warnings})
        raise
    finally:
        session.close()
        # All names belong to this job; original profile/videos are untouched.
        for path in cfg["avatar_root"].glob(f"mpt-avatar-{ident}-*.wav"):
            path.unlink(missing_ok=True)
