"""Validated media import and CPU Whisper transcription for Creator Studio."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from app.services.creator import store

MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
MAX_MEDIA_SECONDS = 2 * 60 * 60
MAX_DOWNLOAD_SECONDS = 15 * 60
_MODEL_LOCK = threading.Lock()
_TRANSCRIBE_LOCK = threading.Lock()
_model = None
_model_size = None
_MODEL_SIZES = {"tiny", "base", "small", "medium", "large-v3", "large-v3-turbo", "turbo"}


class MediaExtractError(RuntimeError):
    """A media import/transcription failure that can be shown to the user."""


def _progress(callback, message: str, percent: float | None = None):
    if callback:
        callback(message, percent)


def ffmpeg_binary() -> str:
    configured = os.environ.get("FFMPEG_BINARY", "").strip().strip('"')
    if configured and configured not in {"auto-detect", "ffmpeg-imageio"}:
        resolved = shutil.which(configured) or configured
        if Path(resolved).is_file():
            return str(resolved)
    detected = shutil.which("ffmpeg")
    if detected:
        return detected
    try:
        import imageio_ffmpeg

        detected = imageio_ffmpeg.get_ffmpeg_exe()
        if Path(detected).is_file():
            return detected
    except (ImportError, RuntimeError, OSError):
        pass
    raise MediaExtractError("未找到 FFmpeg，请检查本机环境中的 FFmpeg 路径。")


def _run(command: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaExtractError("音视频处理超时，请缩短视频后重试。") from exc
    except OSError as exc:
        raise MediaExtractError("无法启动音视频工具，请检查 FFmpeg 安装路径。") from exc


def probe_media(path: str | Path, *, require_audio: bool = True) -> dict:
    """Probe actual media, falling back to FFmpeg when ffprobe is not bundled."""
    media = Path(path).expanduser().resolve()
    if not media.is_file() or media.stat().st_size == 0:
        raise MediaExtractError("音视频文件不存在或为空，请重新上传。")
    if media.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise MediaExtractError("文件超过 1GB，请先剪短或压缩后再导入。")
    with media.open("rb") as handle:
        head = handle.read(2048).lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html", b"<?xml")) or b"<html" in head[:256]:
        raise MediaExtractError("下载内容是网页而非音视频，请更换链接或上传本地视频。")
    binary = ffmpeg_binary()
    sibling = Path(binary).parent / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if ffprobe:
        result = _run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(media)])
        if result.returncode:
            raise MediaExtractError("文件不是有效音视频或下载不完整，请重新导入；无需重复安装 FFmpeg。")
        try:
            metadata = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise MediaExtractError("无法读取音视频信息，请重新导入文件。") from exc
        streams = metadata.get("streams", [])
        if require_audio and not any(stream.get("codec_type") == "audio" for stream in streams):
            raise MediaExtractError("视频中没有音轨，无法提取口播文案。")
        if not any(stream.get("codec_type") in {"audio", "video"} for stream in streams):
            raise MediaExtractError("文件中没有可用音视频流。")
        try:
            duration = float(metadata.get("format", {}).get("duration", 0) or 0)
        except (TypeError, ValueError):
            duration = 0
    else:
        command = [binary, "-hide_banner", "-v", "info", "-i", str(media), "-t", "1"]
        if require_audio:
            command += ["-map", "0:a:0"]
        command += ["-f", "null", "-"]
        result = _run(command)
        if result.returncode:
            if "matches no streams" in result.stderr.lower():
                raise MediaExtractError("视频中没有音轨，无法提取口播文案。")
            raise MediaExtractError("文件不是有效音视频或下载不完整，请重新导入；无需重复安装 FFmpeg。")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        duration = (float(match[1]) * 3600 + float(match[2]) * 60 + float(match[3])) if match else 0
        metadata = {"format": {"duration": duration}}
    if not math.isfinite(duration) or duration < 0:
        raise MediaExtractError("音视频时长无效，请重新导入文件。")
    if duration > MAX_MEDIA_SECONDS:
        raise MediaExtractError("目前单次导入支持 2 小时以内的音视频，请先分段。")
    metadata["duration"] = duration
    return metadata


def _parse_url(value: str) -> str:
    match = re.search(r"https?://[^\s<>\"，。]+", str(value))
    if not match:
        raise MediaExtractError("请粘贴包含 http 或 https 地址的视频分享链接。")
    url = match[0].rstrip("）)]}；;！!、")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MediaExtractError("视频链接格式无效。")
    return url


def _download_shared(url: str, directory: Path, progress=None) -> Path:
    try:
        import yt_dlp
    except ImportError as exc:
        raise MediaExtractError("分享链接解析器 yt-dlp 未安装，请安装增强版依赖，或上传本地音视频。") from exc
    started = time.monotonic()

    def download_hook(status):
        downloaded = status.get("downloaded_bytes", 0) or 0
        if downloaded > MAX_DOWNLOAD_BYTES or time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
            raise MediaExtractError("链接下载超过大小或时间限制，请上传本地视频。")
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        percent = min(90, downloaded * 90 / total) if total else None
        _progress(progress, "正在下载分享视频", percent)

    class QuietLogger:
        def debug(self, _message):
            pass

        def warning(self, _message):
            pass

        def error(self, _message):
            pass

    options = {
        "outtmpl": str(directory / "source.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "socket_timeout": 30,
        "retries": 1,
        "fragment_retries": 1,
        "quiet": True,
        "no_warnings": True,
        "logger": QuietLogger(),
        "progress_hooks": [download_hook],
        "ffmpeg_location": str(Path(ffmpeg_binary()).parent),
        "match_filter": lambda info, **_kwargs: "视频超过 2 小时" if (info.get("duration") or 0) > MAX_MEDIA_SECONDS else None,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
            if not info or info.get("_type") == "playlist":
                raise MediaExtractError("请提供单条视频链接，暂不支持账号主页或播放列表。")
            candidates = [Path(item["filepath"]) for item in info.get("requested_downloads", []) if item.get("filepath")]
            candidates += [Path(downloader.prepare_filename(info))]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise MediaExtractError("链接没有返回可下载的视频，请换链接或上传本地视频。")
    except MediaExtractError:
        raise
    except Exception as exc:
        raise MediaExtractError("分享链接解析或下载失败；链接可能过期、平台需要登录或暂不受支持。请换链接或上传本地视频。") from exc


def download_media(url: str, progress=None) -> Path:
    """Import a direct media URL or supported public share URL, never an HTML MP4."""
    url = _parse_url(url)
    directory = store.data_root() / "downloads" / store.new_id()
    directory.mkdir(parents=True)
    output = directory / "source.media"
    _progress(progress, "检查视频链接", 0)
    try:
        started = time.monotonic()
        with requests.get(url, stream=True, timeout=(10, 30), headers={"User-Agent": "Mozilla/5.0"}) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].lower()
            declared_length = response.headers.get("content-length", "")
            if declared_length.isdigit() and int(declared_length) > MAX_DOWNLOAD_BYTES:
                raise MediaExtractError("下载文件超过 1GB，请上传剪短后的视频。")
            if content_type in {"text/html", "application/xhtml+xml", "application/vnd.apple.mpegurl", "application/x-mpegurl"}:
                shared = True
            else:
                shared = False
                total = 0
                with output.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        if total == 0 and (chunk.lstrip().lower().startswith((b"<!doctype html", b"<html", b"#extm3u"))):
                            shared = True
                            break
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES or time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                            raise MediaExtractError("链接下载超过大小或时间限制，请上传本地视频。")
                        handle.write(chunk)
                        percent = min(90, total * 90 / int(declared_length)) if declared_length.isdigit() and int(declared_length) else None
                        _progress(progress, "正在下载音视频", percent)
        if shared:
            output.unlink(missing_ok=True)
            output = _download_shared(url, directory, progress)
        _progress(progress, "校验下载的音视频", 95)
        probe_media(output)
        _progress(progress, "视频下载完成", 100)
        return output.resolve()
    except MediaExtractError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except requests.RequestException as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise MediaExtractError("无法下载该链接，请检查网络、链接是否过期，或改用本地上传。") from exc
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _get_model(size: str):
    global _model, _model_size
    if size not in _MODEL_SIZES:
        raise MediaExtractError("Whisper 模型名称无效，请选择 tiny、base、small、medium 或 large-v3。")
    with _MODEL_LOCK:
        if _model is not None and _model_size == size:
            return _model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise MediaExtractError("当前 Python 环境缺少 faster-whisper，请检查增强版启动环境。") from exc
        _model = None
        _model_size = None
        project = Path(__file__).resolve().parents[3]
        candidates = [project / "models" / f"whisper-{size}", store.data_root() / "models" / f"whisper-{size}"]
        local = next((candidate for candidate in candidates if (candidate / "model.bin").is_file()), None)
        model_source = str(local) if local else size
        try:
            loaded = WhisperModel(
                model_source,
                device="cpu",
                compute_type="int8",
                cpu_threads=min(4, os.cpu_count() or 1),
                num_workers=1,
                download_root=str(store.data_root() / "models" / "faster-whisper"),
            )
        except Exception as exc:
            raise MediaExtractError("Whisper 模型加载失败。首次使用需下载模型，请检查网络或将模型放入项目 models 目录。") from exc
        _model, _model_size = loaded, size
        return _model


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def extract_media(path, language="zh", model_size="small", progress=None) -> dict:
    media = Path(path).expanduser().resolve()
    _progress(progress, "校验音视频文件", 0)
    metadata = probe_media(media)
    ident = store.new_id()
    directory = store.data_root() / "extractions" / ident
    directory.mkdir(parents=True)
    audio = directory / "reference.wav"
    try:
        _progress(progress, "提取口播音频", 10)
        result = _run([
            ffmpeg_binary(), "-hide_banner", "-v", "error", "-nostdin", "-y", "-i", str(media),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
        ], timeout=30 * 60)
        if result.returncode or not audio.is_file() or audio.stat().st_size <= 44:
            raise MediaExtractError("无法读取完整音轨，视频可能下载不完整或已损坏，请重新导入文件。")
        _progress(progress, "等待 Whisper 识别（首次使用可能下载模型）", 20)
        with _TRANSCRIBE_LOCK:
            model = _get_model(model_size)
            _progress(progress, "正在识别口播文案", 25)
            try:
                recognized, info = model.transcribe(
                    str(audio), language=None if language in {None, "", "auto"} else language,
                    beam_size=5, word_timestamps=True, vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500},
                )
                segments = []
                duration = metadata["duration"] or getattr(info, "duration", 0) or 0
                for segment in recognized:
                    text = segment.text.strip()
                    if not text:
                        continue
                    start, end = float(segment.start), float(segment.end)
                    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                        raise MediaExtractError("识别结果时间轴无效，请重试或更换 Whisper 模型。")
                    item = {"start": round(start, 3), "end": round(end, 3), "text": text}
                    words = getattr(segment, "words", None)
                    if words:
                        item["words"] = [
                            {"start": round(float(word.start), 3), "end": round(float(word.end), 3), "text": word.word.strip()}
                            for word in words
                        ]
                    segments.append(item)
                    if duration:
                        _progress(progress, "正在识别口播文案", min(94, 25 + end * 69 / duration))
            except MediaExtractError:
                raise
            except Exception as exc:
                raise MediaExtractError("Whisper 转写失败，请检查音轨是否能正常播放，或更换模型重试。") from exc
        if not segments:
            raise MediaExtractError("未识别到可用口播。请确认文件有清晰人声，或更换识别语言后重试。")
        text = "\n".join(segment["text"] for segment in segments)
        txt = directory / "transcript.txt"
        srt = directory / "transcript.srt"
        txt.write_text(text + "\n", encoding="utf-8")
        srt.write_text("\n\n".join(
            f"{index}\n{_srt_time(segment['start'])} --> {_srt_time(segment['end'])}\n{segment['text']}"
            for index, segment in enumerate(segments, start=1)
        ) + "\n", encoding="utf-8")
        result = store.save_record("extracts", ident, {
            "text": text, "segments": segments, "txt_path": str(txt), "srt_path": str(srt),
            "media_path": str(media), "audio_path": str(audio),
            "language": getattr(info, "language", language), "duration": duration, "model_size": model_size,
        })
        _progress(progress, "文案提取完成", 100)
        return result
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
