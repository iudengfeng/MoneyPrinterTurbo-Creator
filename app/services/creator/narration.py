"""Second-step narration with real providers and independent audio versions.

The existing Duix and VoxCPM adapters do not expose emotion controls. Only
natural delivery is therefore offered; speed is applied to the resulting
audio with FFmpeg, rather than being stored as a cosmetic UI preference.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import wave
from concurrent.futures import CancelledError
from pathlib import Path

from . import duix, extract, store, voices

MAX_TEXT_LENGTH = 6000
_CHUNK_LENGTH = 600
_EDGE_VOICES = (
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 普通话女声"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 普通话女声"),
    ("zh-CN-YunxiNeural", "云希 · 普通话男声"),
    ("zh-CN-YunjianNeural", "云健 · 普通话男声"),
    ("zh-CN-YunyangNeural", "云扬 · 普通话男声"),
)


def list_options() -> list[dict]:
    """List real voice IDs without fetching paid catalogues or starting engines."""
    options = [
        {"id": f"edge:{ident}", "name": name, "provider": "edge",
         "description": "Edge 在线标准音色，需要联网，无需填写 API Key。",
         "supports_emotion": False}
        for ident, name in _EDGE_VOICES
    ]
    try:
        profiles = duix.list_profiles()
    except (ValueError, RuntimeError, OSError):
        profiles = {"voices": []}
    options.extend({
        "id": f"duix:{profile['id']}", "name": profile["name"], "provider": "duix",
        "description": "Duix 本机已有音色，需要本机 Duix 与 Docker 引擎就绪。",
        "supports_emotion": False,
    } for profile in profiles.get("voices", []))
    for profile in voices.list_voices():
        if profile.get("provider") not in {"duix", "voxcpm"}:
            continue
        options.append({
            "id": f"saved:{profile['id']}", "name": profile["name"],
            "provider": profile["provider"], "supports_emotion": False,
            "description": ("已保存的 VoxCPM 克隆样音，需要在原配音设置配置云端 API Key 和模型 ID。"
                            if profile["provider"] == "voxcpm" else
                            "已收藏的 Duix 本机音色，需要本机 Duix 与 Docker 引擎就绪。"),
        })
    return options


def list_narrations() -> list[dict]:
    """Return successful versions; errors remain in the task/record history."""
    return [row for row in store.list_records("narrations") if row.get("state") == "done" and not row.get("preview")]


def list_samples() -> list[dict]:
    return store.list_records("voice_samples")


def _text(value) -> str:
    text = str(value or "").strip()
    if not text or len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"请输入 1～{MAX_TEXT_LENGTH} 字配音文案。")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        raise ValueError("配音文案包含无法处理的控制字符。")
    return text


def _speed(value) -> float:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        speed = float("nan")
    if isinstance(value, bool) or not math.isfinite(speed) or not 0.8 <= speed <= 1.2:
        raise ValueError("语速支持 0.8～1.2 倍。")
    return speed


def _chunks(text: str) -> list[str]:
    """Keep every character, preferring punctuation before the 600-char bound."""
    chunks = []
    remaining = text
    while len(remaining) > _CHUNK_LENGTH:
        window = remaining[:_CHUNK_LENGTH]
        cut = max(window.rfind(mark) for mark in "。！？!?；;\n，, ") + 1
        if cut < _CHUNK_LENGTH // 2:
            cut = _CHUNK_LENGTH
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            frame_count = audio.getnframes()
            frame_size = audio.getnchannels() * audio.getsampwidth()
            if frame_count <= 0 or frame_count * frame_size > 128 * 1024 * 1024:
                raise wave.Error("empty or oversized PCM")
            actual_frames = 0
            while block := audio.readframes(8192):
                if len(block) % frame_size:
                    raise wave.Error("incomplete sample")
                actual_frames += len(block) // frame_size
            if actual_frames != frame_count:
                raise wave.Error("truncated PCM")
            return frame_count / audio.getframerate()
    except (OSError, wave.Error, EOFError, ZeroDivisionError):
        raise RuntimeError("生成的配音不是完整可播放音频，请检查所选配音服务后重试。") from None


def _combine(source_paths: list[Path], target: Path, speed: float) -> float:
    """Decode, concatenate and change tempo without changing source files."""
    target = Path(target).resolve()
    source_paths = [Path(path).resolve() for path in source_paths]
    if not source_paths or target in source_paths:
        raise ValueError("配音输出必须使用独立文件，不能覆盖原始音频。")
    for source in source_paths:
        if not source.is_file() or not 0 < source.stat().st_size <= 128 * 1024 * 1024:
            raise RuntimeError("配音服务没有返回有效音频，请检查配音配置与网络后重试。")
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(".narration.partial.wav")
    command = [extract.ffmpeg_binary(), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-y"]
    filters = []
    for index, source in enumerate(source_paths):
        command.extend(["-i", str(source)])
        filters.append(f"[{index}:a:0]aformat=sample_fmts=fltp:sample_rates=24000:channel_layouts=mono[a{index}]")
    inputs = "".join(f"[a{index}]" for index in range(len(source_paths)))
    filters.append(f"{inputs}concat=n={len(source_paths)}:v=0:a=1,atempo={speed:.6f}[out]")
    command.extend(["-filter_complex", ";".join(filters), "-map", "[out]", "-ar", "24000", "-ac", "1",
                    "-c:a", "pcm_s16le", str(staged)])
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("配音合成或语速处理失败：" + (result.stderr or "FFmpeg 未输出有效音频").strip()[-600:])
        duration = _duration(staged)
        os.replace(staged, target)
        return duration
    except subprocess.TimeoutExpired:
        raise RuntimeError("配音合成超过 10 分钟，请缩短文案后重试。") from None
    except OSError:
        raise RuntimeError("无法执行配音合成，请检查 FFmpeg 路径与输出目录。") from None
    finally:
        staged.unlink(missing_ok=True)


def _render_chunk(text: str, option: dict, folder: Path, index: int, progress) -> Path:
    ident = option["id"]
    if ident.startswith("duix:"):
        result = duix.generate_audio(text, int(ident.split(":", 1)[1]), progress=progress)
        source = Path(str(result.get("audio_path") or "")).resolve()
    elif ident.startswith("saved:"):
        source = Path(voices.preview_voice(ident.split(":", 1)[1], text, progress=progress)).resolve()
    else:
        from app.services import voice

        source = folder / f"source-{index:03d}.mp3"
        result = voice.tts(text=text, voice_name=ident.split(":", 1)[1], voice_rate=1.0, voice_file=str(source))
        if result is None:
            source.unlink(missing_ok=True)
            raise RuntimeError("Edge 配音未生成音频，请检查网络或更换音色；原配音结果已保留。")
        return source
    if not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError("所选声音没有生成音频，请检查其配音服务。")
    voices._wav_valid(source)
    destination = folder / f"source-{index:03d}.wav"
    shutil.copy2(source, destination)
    return destination


def generate(text, voice_id, speed=1.0, emotion="自然", progress=None, preview=False) -> dict:
    """Generate an entire <=6000-char script; failures never overwrite a version."""
    text, speed = _text(text), _speed(speed)
    if not isinstance(preview, bool):
        raise ValueError("试听标记无效。")
    if emotion != "自然":
        raise ValueError("当前 Duix、VoxCPM 和 Edge 接口仅支持自然表达，无法选择其他情绪。")
    option = next((row for row in list_options() if row["id"] == voice_id), None)
    if not option:
        raise ValueError("所选音色不存在，请刷新音色列表后重试。")
    # Resolve before a provider call, so a missing merger cannot waste synthesis.
    extract.ffmpeg_binary()
    ident = store.new_id()
    folder = store.data_root() / "narrations" / ident
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "script.txt").write_text(text, "utf-8")
    metadata = {"text": text, "voice_id": option["id"], "voice_name": option["name"],
                "provider": option["provider"], "speed": speed, "emotion": emotion, "preview": preview, "state": "running"}
    store.save_record("narrations", ident, metadata)
    try:
        chunks = _chunks(text)
        source_paths = []
        for index, chunk in enumerate(chunks):
            base = 5 + 70 * index / len(chunks)
            duix._report(progress, f"生成配音 {index + 1}/{len(chunks)}", base)

            def chunk_progress(message, percent=None):
                bounded = min(100, max(0, float(percent or 0)))
                duix._report(progress, str(message), base + bounded * 70 / (100 * len(chunks)))

            source_paths.append(_render_chunk(chunk, option, folder, index + 1, chunk_progress))
        duix._report(progress, "合并配音并处理语速", 82)
        target = folder / "narration.wav"
        duration = _combine(source_paths, target, speed)
        duix._report(progress, "配音完成", 100)
        return store.update_record("narrations", ident, {
            "state": "done", "audio_path": str(target), "duration": duration,
            "source_audio_paths": [str(path) for path in source_paths], "chunk_count": len(chunks),
            "txt_path": str(folder / "script.txt"),
        })
    except BaseException as exc:
        store.update_record("narrations", ident, {
            "state": "cancelled" if isinstance(exc, (CancelledError, KeyboardInterrupt)) else "failed",
            "error": str(exc)[:1000],
        })
        raise


def save_sample(name, audio_path, transcript="", mode="archive") -> dict:
    """Archive a clear sample or create an explicitly cloud-backed voice asset."""
    if mode == "voxcpm":
        return voices.save_voice(name, audio_path, transcript=transcript, provider="voxcpm")
    if mode != "archive":
        raise ValueError("声音样本支持保存待训练档案或 VoxCPM 云端克隆。")
    name = str(name or "").strip()
    if not name or len(name) > 80 or re.search(r"[\x00-\x1f]", name):
        raise ValueError("请输入 1～80 字声音名称。")
    transcript = str(transcript or "").strip()
    if len(transcript) > 5000:
        raise ValueError("参考文案过长，请使用简短样音。")
    source = Path(str(audio_path or "")).expanduser().resolve()
    if not source.is_file() or not 0 < source.stat().st_size <= 20 * 1024 * 1024:
        raise ValueError("请上传有效音频，大小不超过 20MB。")
    from app.services import voice

    normalized = voice.prepare_voxcpm_reference_audio(source.read_bytes(), source.suffix)
    ident = store.new_id()
    folder = store.data_root() / "voice_samples" / ident
    folder.mkdir(parents=True, exist_ok=True)
    sample = folder / "sample.wav"
    try:
        sample.write_bytes(normalized)
        duration = _duration(sample)
        return store.save_record("voice_samples", ident, {
            "name": name, "sample_path": str(sample), "transcript": transcript,
            "duration": duration, "provider": "pending_duix", "state": "sample_saved",
            "message": "已保存声音样本。请在 Duix 中训练为本机音色后再刷新音色列表；当前样本尚不能用于本机克隆配音。",
        })
    except Exception:
        shutil.rmtree(folder)
        raise
