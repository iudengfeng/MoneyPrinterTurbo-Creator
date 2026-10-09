"""Local HyperFrames alpha overlays and FFmpeg picture/audio composition."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

from . import extract, processing, store

_ASPECTS = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (720, 720)}
_POSITIONS = processing.POSITIONS
_PRESETS = (
    {"id": "clean", "name": "清爽口播", "description": "留白标题与清晰字幕，保留原画面颜色。", "color_grade": "none", "subtitle_style": "clean"},
    {"id": "bold", "name": "醒目观点", "description": "黄色强调与大字字幕，适合简短观点。", "color_grade": "vivid", "subtitle_style": "bold"},
    {"id": "knowledge", "name": "知识讲解", "description": "蓝色标题标记与清晰字幕，适合知识分享。", "color_grade": "cool", "subtitle_style": "clean"},
    {"id": "business", "name": "商务表达", "description": "简洁边框标题与暖色画面。", "color_grade": "warm", "subtitle_style": "clean"},
)
_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
_VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def list_presets() -> list[dict]:
    return [dict(row) for row in _PRESETS]


def list_bgm() -> list[dict]:
    folder = Path(__file__).resolve().parents[3] / "resource" / "songs"
    paths = sorted(path for path in folder.glob("*") if path.is_file() and path.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg"})
    return [{"id": path.stem, "name": f"本机音乐 {index:02d}", "path": str(path.resolve())} for index, path in enumerate(paths, 1)]


def list_renders() -> list[dict]:
    return [row for row in store.list_records("renders") if row.get("state", "done") == "done" and Path(row.get("video_path", "")).is_file()]


def _ffprobe(path):
    ffmpeg = extract.ffmpeg_binary()
    probe = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    executable = os.environ.get("MPT_FFPROBE") or (str(probe) if probe.is_file() else shutil.which("ffprobe"))
    if not executable:
        from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
        try:
            info = ffmpeg_parse_infos(str(path), decode_file=False)
        except Exception as exc:
            raise ValueError("素材无法读取，请重新导入有效音视频。") from exc
        streams = []
        if info.get("video_found"):
            width, height = info.get("video_size", (0, 0))
            streams.append({"codec_type": "video", "width": width, "height": height})
        if info.get("audio_found"):
            streams.append({"codec_type": "audio"})
        return {"format": {"duration": info.get("duration", 0)}, "streams": streams}
    result = subprocess.run([executable, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError("素材无法读取：" + result.stderr[-500:])
    return json.loads(result.stdout)


def probe_source(path) -> dict:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or not source.stat().st_size:
        raise ValueError("请选择可读取的音视频文件。")
    if source.stat().st_size > 1024 * 1024 * 1024:
        raise ValueError("单个音视频素材不能超过 1GB，请先剪短或压缩。")
    info = _ffprobe(source)
    duration = float(info.get("format", {}).get("duration", 0) or 0)
    if not math.isfinite(duration) or not 0 < duration <= 1800:
        raise ValueError("视频或音轨时长应在 0～30 分钟之间。")
    streams = info.get("streams", [])
    video = next((row for row in streams if row.get("codec_type") == "video"), {})
    return {"path": str(source), "duration": duration, "width": int(video.get("width", 0)), "height": int(video.get("height", 0)),
            "has_audio": any(row.get("codec_type") == "audio" for row in streams), "has_video": bool(video)}


def read_srt(path):
    if not path:
        return []
    source = Path(path)
    if not source.is_file() or source.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("字幕文件不存在或超过 2MB，请上传有效 SRT。")
    text = source.read_text("utf-8-sig")
    def seconds(raw):
        h, m, s, ms = map(int, re.split("[:,.]", raw))
        if m > 59 or s > 59:
            raise ValueError("字幕时间轴中的分钟或秒数无效。")
        return h * 3600 + m * 60 + s + ms / 1000
    entries = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        match = re.match(r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})", lines[timing])
        if match:
            start, end = map(seconds, match.groups())
            content = "\n".join(lines[timing + 1:]).strip()
            if end > start and content:
                entries.append({"start": start, "end": end, "text": content})
    if not entries and text.strip():
        raise ValueError("字幕文件中没有有效时间轴，请上传 SRT 字幕。")
    return sorted(entries, key=lambda row: (row["start"], row["end"]))


def _units(text):
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _text(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def _balanced_groups(tokens, max_units):
    """Partition complete tokens, balancing length instead of leaving a tiny tail."""
    if not tokens:
        return []
    lengths = [_units(token["text"]) for token in tokens]
    target = sum(lengths) / max(1, math.ceil(sum(lengths) / max_units))
    count = len(tokens)
    costs, previous = [float("inf")] * (count + 1), [None] * (count + 1)
    costs[0] = 0
    for stop in range(1, count + 1):
        length = 0
        for begin in range(stop - 1, -1, -1):
            length += lengths[begin]
            # A single timed word is never cut; the renderer can reduce its font.
            if length > max_units and stop - begin > 1:
                break
            short_penalty = max(0, 6 - length) ** 2 * 10 if count > 1 else 0
            candidate = costs[begin] + 100 + (length - target) ** 2 + short_penalty
            if candidate < costs[stop]:
                costs[stop], previous[stop] = candidate, begin
    groups, cursor = [], count
    while cursor:
        begin = previous[cursor]
        groups.append(tokens[begin:cursor])
        cursor = begin
    return list(reversed(groups))


def _timed_tokens(entry, text):
    words = entry.get("words")
    if not words:
        return None
    tokens, cursor, previous_start = [], 0, -1.0
    for word in words:
        try:
            start, end = float(word["start"]), float(word["end"])
            word_text = _text(word.get("text", ""))
        except (TypeError, ValueError, KeyError):
            return None
        if not word_text or not math.isfinite(start) or not math.isfinite(end) or start < previous_start or end < start:
            return None
        position = text.find(word_text, cursor)
        if position < 0:
            return None
        stop = position + len(word_text)
        tokens.append({"text": text[cursor:stop], "start": start, "end": end})
        cursor, previous_start = stop, start
    if not tokens:
        return None
    tokens[-1]["text"] += text[cursor:]
    return tokens


def _plain_tokens(text, max_units):
    tokens = []
    for token in re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*|.", text):
        parts = list(token) if _units(token) > max_units else [token]
        for part in parts:
            if re.fullmatch(r"[。！？!?，,；;：:]", part) and tokens:
                tokens[-1]["text"] += part
            else:
                tokens.append({"text": part})
    return tokens


def prepare_captions(entries, duration, width, subtitle_style="clean"):
    """Create readable short clauses, preserving real Whisper word timing."""
    font_size = width * (0.064 if subtitle_style == "bold" else 0.052)
    max_units = min(30, max(14, int(width * 0.78 / (font_size / 2))))
    result = []
    for entry in entries:
        raw_start, raw_end = float(entry["start"]), float(entry["end"])
        if not math.isfinite(raw_start) or not math.isfinite(raw_end):
            continue
        start, end = max(0.0, raw_start), min(duration, raw_end)
        text = _text(entry.get("text", ""))
        if end <= start or not text:
            continue
        timed = _timed_tokens(entry, text)
        if timed is not None:
            timed = [dict(token, start=max(start, token["start"]), end=min(end, token["end"]))
                     for token in timed if token["start"] < end and token["end"] >= start]
            if not timed:
                continue
        tokens = timed if timed is not None else _plain_tokens(text, max_units)
        clauses, clause = [], []
        for token in tokens:
            if timed and clause and token["start"] - clause[-1]["end"] > 0.45:
                clauses.append(clause)
                clause = []
            clause.append(token)
            if re.search(r"[。！？!?，,；;：:]\s*$", token["text"]):
                clauses.append(clause)
                clause = []
        if clause:
            clauses.append(clause)
        groups = [group for clause in clauses for group in _balanced_groups(clause, max_units)]
        total_units = sum(_units("".join(token["text"] for token in group)) for group in groups)
        cursor = start
        for index, group in enumerate(groups):
            content = "".join(token["text"] for token in group).strip()
            if timed:
                group_start, group_end = max(start, group[0]["start"]), min(end, group[-1]["end"])
                group_end = min(end, max(group_end, group_start + 0.001))
            else:
                group_start = cursor
                group_end = end if index == len(groups) - 1 else cursor + (end - start) * _units("".join(token["text"] for token in group)) / total_units
                cursor = group_end
            if content and group_end > group_start:
                result.append({"start": round(group_start, 3), "end": round(group_end, 3), "text": content})
    return result


def _write_captions(path, captions):
    path.write_text("\n\n".join(
        f"{index}\n{extract._srt_time(row['start'])} --> {extract._srt_time(row['end'])}\n{row['text']}"
        for index, row in enumerate(captions, 1)
    ) + "\n", "utf-8")


def _validate_pip(items, duration):
    if not isinstance(items, (list, tuple)) or len(items) > 32:
        raise ValueError("画中画最多支持 32 段素材。")
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("画中画素材参数无效。")
        path = Path(item.get("path", "")).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > 500 * 1024 * 1024:
            raise ValueError("画中画素材不存在或超过 500MB。")
        start, end, size = float(item.get("start", 0)), float(item.get("end", duration)), float(item.get("size", 0.3))
        if not all(math.isfinite(value) for value in (start, end, size)) or not 0 <= start < end <= duration + 0.05:
            raise ValueError("画中画时间必须位于成片内，结束时间应大于开始时间。")
        position = item.get("position", "top-right")
        mode, padding = item.get("mode", "window"), float(item.get("padding", .02))
        if mode not in {"window", "full"} or not math.isfinite(padding) or not 0 <= padding <= .2 or position not in _POSITIONS or not 0.15 <= size <= 0.6:
            raise ValueError("画中画位置或尺寸无效，尺寸应在 15%～60% 之间。")
        if path.suffix.lower() in _IMAGE_TYPES:
            from PIL import Image
            try:
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    width, height = image.size
            except Exception as exc:
                raise ValueError("画中画图片无法读取，请重新上传。") from exc
            if not 0 < width * height <= 40_000_000:
                raise ValueError("画中画图片尺寸过大，请缩小后上传。")
            kind, source_duration = "image", None
        elif path.suffix.lower() in _VIDEO_TYPES:
            metadata = probe_source(path)
            if not metadata["has_video"]:
                raise ValueError("画中画素材不包含视频。")
            width, height, source_duration = metadata["width"], metadata["height"], metadata["duration"]
            kind = "video"
        else:
            raise ValueError("画中画支持 PNG、JPG、WEBP 图片及 MP4、MOV、M4V、WEBM、MKV 视频。")
        result.append({"path": str(path), "start": start, "end": min(duration, end), "position": position, "size": size,
                       "kind": kind, "width": width, "height": height, "sourceDuration": source_duration, "mode": mode, "padding": padding})
    return result


def _cached_render_browser():
    configured_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured_cache == "0":
        return None
    if configured_cache:
        cache = Path(configured_cache).expanduser()
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        cache = Path(local) / "ms-playwright"
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright"
    try:
        candidates = [path for path in cache.glob("chromium_headless_shell-*/chrome-headless-shell-*/*")
                      if path.name in {"chrome-headless-shell", "chrome-headless-shell.exe"} and path.is_file()]
        return str(max(candidates, key=lambda path: path.stat().st_mtime).resolve()) if candidates else None
    except OSError:
        return None


def _run_hyperframes(root, request, duration, log, progress):
    from . import hyperframes
    payload = json.loads(Path(request).read_text("utf-8"))
    def report(message, percent=None):
        if progress:
            progress(message, 20 + max(0, min(100, float(percent or 0))) * 0.68)
    return hyperframes.render_visual(payload, log, progress=report)


def _mix_audio(visual, output, audio, bgm, duration, volume, log):
    binary = extract.ffmpeg_binary()
    command = [binary, "-hide_banner", "-v", "error", "-nostdin", "-y", "-i", str(visual)]
    audio_index = bgm_index = None
    if audio:
        audio_index = 1
        command += ["-i", str(audio)]
    if bgm and volume > 0:
        bgm_index = 2 if audio else 1
        command += ["-stream_loop", "-1", "-i", str(bgm)]
    duration_arg = f"{duration:.9f}"
    filters = []
    if audio_index is not None:
        filters.append(f"[{audio_index}:a:0]atrim=duration={duration_arg},asetpts=PTS-STARTPTS,apad,atrim=duration={duration_arg}[voice]")
    if bgm_index is not None:
        fade = min(0.6, duration / 2)
        filters.append(f"[{bgm_index}:a:0]atrim=duration={duration_arg},asetpts=PTS-STARTPTS,volume={volume:.6f},afade=t=in:st=0:d={fade:.6f},afade=t=out:st={max(0, duration-fade):.6f}:d={fade:.6f}[music]")
    if audio_index is not None and bgm_index is not None:
        filters.append("[voice][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.97:latency=1[mixed]")
        final_audio = "mixed"
    else:
        final_audio = "voice" if audio_index is not None else "music"
    if filters:
        command += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", f"[{final_audio}]", "-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-map", "0:v:0", "-an"]
    command += ["-c:v", "copy", "-t", duration_arg, "-movflags", "+faststart", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=max(60, duration * 2), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with log.open("a", encoding="utf-8") as handle:
        handle.write("\nFFmpeg audio mix:\n" + result.stderr)
    if result.returncode or not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("口播与背景音乐合成失败：" + result.stderr[-700:])


def render_video(video_path, audio_path=None, subtitle_path=None, title="", template="talking", bgm_path=None, aspect="9:16", image_path=None, progress=None,
                 *, style="clean", color_grade="none", subtitle_style="clean", bgm_volume=0.12, auto_subtitles=False, pip_items=None, source_subtitles_burned=False, video_fit="contain", processing_options=None):
    options = processing.normalize(processing_options or {})
    if template not in {"talking", "pip", "cards"} or aspect not in _ASPECTS:
        raise ValueError("不支持的模板或画幅。")
    if style not in {row["id"] for row in _PRESETS} or color_grade not in {"none", "warm", "cool", "vivid"} or subtitle_style not in {"clean", "bold", "yellow", "none"}:
        raise ValueError("模板风格、画面颜色或字幕样式无效。")
    if video_fit not in {"contain", "cover"}:
        raise ValueError("画面适配方式应为完整显示或铺满裁切。")
    volume = float(bgm_volume)
    if not math.isfinite(volume) or not 0 <= volume <= 0.4:
        raise ValueError("背景音乐音量应在 0～40% 之间。")
    if auto_subtitles and subtitle_path:
        raise ValueError("自动字幕和上传字幕只能选择一种。")
    if source_subtitles_burned and (auto_subtitles or subtitle_path):
        raise ValueError("视频已带字幕，请保留原字幕，或导入无字幕视频再生成字幕。")
    if subtitle_style == "none" and (auto_subtitles or subtitle_path):
        raise ValueError("请选一种字幕样式后再生成或上传字幕。")
    source = probe_source(video_path)
    if not source["has_video"]:
        raise ValueError("所选文件不包含视频画面。")
    audio = probe_source(audio_path) if audio_path else source
    if audio_path and not audio["has_audio"]:
        raise ValueError("所选配音文件不包含音轨。")
    duration = audio["duration"] if audio_path else source["duration"]
    from .composition import _video_timing
    video_timing = _video_timing(source, extract.ffmpeg_binary())
    tolerance = min(0.05, 1 / video_timing["fps"])
    if duration > video_timing["duration"] + tolerance + 1e-6:
        raise ValueError(f"人物视频画面只有 {video_timing['duration']:.1f} 秒，完整配音是 {duration:.1f} 秒。"
                         "不能循环画面补齐；请用这份配音重新生成数字人，或改用人物＋图文。")
    bgm = probe_source(bgm_path) if bgm_path else None
    if bgm and not bgm["has_audio"]:
        raise ValueError("所选背景音乐不包含音轨。")
    if auto_subtitles and not audio["has_audio"]:
        raise ValueError("视频没有口播音轨，请先添加配音或关闭自动字幕。")
    items = _validate_pip([] if pip_items is None else pip_items, duration)
    legacy_image = None
    if template == "pip":
        if not image_path:
            raise ValueError("画中画模板需要一张背景图片。")
        legacy_image = _validate_pip([{"path": image_path}], duration)[0]
        if legacy_image["kind"] != "image":
            raise ValueError("画中画模板背景需要一张图片。")
    root = Path(__file__).resolve().parents[3] / "creator-hyperframes"
    if not shutil.which("node") or not (root / "node_modules" / "hyperframes").is_dir():
        raise ValueError("HyperFrames 组件未就绪，请先完成工作台组件安装。")
    ident = store.new_id()
    folder = store.data_root() / "renders" / ident
    folder.mkdir(parents=True)
    output, visual, log = folder / "final.mp4", folder / "visual.mp4", folder / "render.log"
    base = {"title": str(title)[:120], "template": template, "style": style, "aspect": aspect, "duration": duration,
            "source_video_path": source["path"], "source_audio_path": audio["path"] if audio["has_audio"] else "",
            "source_video_duration": video_timing["duration"],
            "renderer": "hyperframes", "overlay_format": "alpha-auto",
            "audio_mode": "replacement" if audio_path else ("original" if audio["has_audio"] else "none"),
            "bgm_path": bgm["path"] if bgm else "", "bgm_volume": volume, "color_grade": color_grade, "subtitle_style": subtitle_style,
            "pip_items": items, "video_fit": video_fit, "log_path": str(log)}
    store.save_record("renders", ident, dict(base, state="running"))
    try:
        if progress:
            progress("准备剪辑素材", 2)
        srt = str(Path(subtitle_path).resolve()) if subtitle_path else ""
        entries = None
        if auto_subtitles:
            def recognition_progress(message, percent=None):
                if progress:
                    progress("生成口播字幕 · " + message, 3 + min(100, float(percent or 0)) * 0.15)
            transcription = extract.extract_media(audio["path"], language="zh", model_size="small", progress=recognition_progress)
            srt = transcription["srt_path"]
            entries = transcription.get("segments")
        captions = entries if entries else (read_srt(srt) if srt else [])
        if options["silence_trim"]:
            from . import pacing
            edited = pacing.prepare_media(source["path"], audio["path"] if audio["has_audio"] else None, srt, captions,
                                          folder / "pacing", enabled=True, threshold_db=options["silence_threshold"],
                                          min_silence=options["silence_min_duration"], progress=progress)
            if edited.get("removed_seconds", 0) > 0:
                source = probe_source(edited["video_path"])
                audio = probe_source(edited["audio_path"])
                duration = edited["duration"]
                captions = edited["segments"]
                srt = edited.get("srt_path") or ""
                items = processing.remap_pip(items, edited["timeline"])
                video_timing = _video_timing(source, extract.ffmpeg_binary())
            base.update(duration=duration, removed_seconds=edited.get("removed_seconds", 0), edit_timeline=edited.get("timeline", []))
        width, height = processing.dimensions(aspect, options["output_resolution"])
        prepared = prepare_captions(captions, duration, width, subtitle_style)
        if srt and not prepared:
            raise ValueError("字幕时间轴与所选音轨没有交集，请换成这段口播对应的字幕。")
        if srt:
            copied_srt = folder / "subtitles.srt"
            base["source_subtitle_path"] = srt
            _write_captions(copied_srt, prepared)
            srt = str(copied_srt)
        payload = {"video": source["path"], "videoFrames": max(1, math.ceil(video_timing["duration"] * 30)), "image": legacy_image["path"] if legacy_image else None,
                   "title": base["title"], "template": template, "style": style, "colorGrade": color_grade, "subtitleStyle": subtitle_style, "videoFit": video_fit,
                   "width": width, "height": height, "fps": 30, "durationInFrames": math.ceil(duration * 30), "captions": prepared,
                   "pipItems": items, "processing": options, "output": str(visual), "silent": True}
        base.update(output_resolution=options["output_resolution"], highlight_keywords=options["highlight_keywords"],
                    green_screen=options["green_screen"], beauty_strength=options["beauty_strength"], pip_items=items)
        request = folder / "request.json"
        request.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        if progress:
            progress("准备字幕与观点动画", 20)
        render_metadata = _run_hyperframes(root, request, duration, log, progress)
        if isinstance(render_metadata, dict):
            for key in ("overlay_format", "renderer_version", "html_path", "report_path"):
                if key in render_metadata:
                    base[key] = render_metadata[key]
        if not visual.is_file():
            raise RuntimeError("模板没有生成视频，请查看渲染记录后重试。")
        if progress:
            progress("保留口播音轨并混合背景音乐", 90)
        _mix_audio(visual, output, audio["path"] if audio["has_audio"] else None, bgm["path"] if bgm else None, duration, volume, log)
        result_info = probe_source(output)
        if not result_info["has_video"] or abs(result_info["duration"] - duration) > 0.12 or (audio["has_audio"] or bgm and volume > 0) and not result_info["has_audio"]:
            raise RuntimeError("成片音视频校验未通过，请查看渲染记录后重试。")
        result = store.save_record("renders", ident, dict(base, state="done", video_path=str(output), width=width, height=height,
                                   srt_path=srt, subtitles_burned=bool(prepared) or bool(source_subtitles_burned)))
        visual.unlink(missing_ok=True)
        if progress:
            progress("成片已完成", 100)
        return result
    except Exception as exc:
        output.unlink(missing_ok=True)
        store.save_record("renders", ident, dict(base, state="failed", error=str(exc)[:1600]))
        raise
