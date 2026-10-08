"""Build real local visual tracks from timed cards, images and video clips."""

from __future__ import annotations

import math
import os
import re
import subprocess
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import extract, rendering

_ASPECTS = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (720, 720)}
_FPS = 30


class CompositionError(ValueError):
    """An actionable local media/composition error."""


def _number(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CompositionError(f"{label}无效。") from exc
    if not math.isfinite(result):
        raise CompositionError(f"{label}无效。")
    return result


def _probe(path, *, label, audio=False, video=False):
    try:
        result = rendering.probe_source(path)
    except subprocess.TimeoutExpired as exc:
        raise CompositionError(f"检查{label}超时，请缩短或更换文件。") from exc
    except (ValueError, OSError, extract.MediaExtractError) as exc:
        raise CompositionError(f"{label}不是有效音视频或文件不完整，请重新上传。") from exc
    if audio and not result["has_audio"]:
        raise CompositionError(f"{label}没有可用音轨，请重新生成配音。")
    if video and not result["has_video"]:
        raise CompositionError(f"{label}没有可用视频画面，请重新上传。")
    return result


def _run(command, *, timeout, label):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise CompositionError(f"{label}超时，请缩短视频或更换素材后重试。") from exc
    except OSError as exc:
        raise CompositionError("无法启动 FFmpeg，请检查本机环境中的安装路径。") from exc
    if result.returncode:
        detail = (result.stderr or "")[-600:].strip()
        raise CompositionError(f"{label}失败，素材可能损坏或无法解码。{detail}")
    return result


def _video_timing(source, binary):
    """Measure the video stream, rather than a possibly longer audio track.

    FFmpeg is also the fallback when the portable installation has no ffprobe.
    Inspect the video packet timestamps once per source, including the last
    packet duration. Audio duration never extends the available visual track.
    """
    result = _run(
        [binary, "-hide_banner", "-nostats", "-nostdin", "-threads", "2", "-i", source["path"],
         "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "copy", "-f", "framehash", "-"],
        timeout=max(60, source["duration"] * 4), label="视频素材时长检查",
    )
    time_base = re.search(r"^#tb 0:\s*(\d+)/(\d+)$", result.stdout, flags=re.MULTILINE)
    packets = re.findall(r"^0,\s*-?\d+,\s*(-?\d+),\s*(\d+),", result.stdout, flags=re.MULTILINE)
    if not time_base or not packets:
        raise CompositionError("视频素材没有可用画面，请重新上传。")
    fps_match = re.search(r"Stream[^\n]*Video:[^\n]*?\b([\d.]+) fps\b", result.stderr)
    fps = float(fps_match.group(1)) if fps_match else _FPS
    if not math.isfinite(fps) or fps <= 0:
        fps = _FPS
    tick = int(time_base.group(1)) / int(time_base.group(2))
    timestamps = [(int(pts), int(length)) for pts, length in packets]
    start = min(pts for pts, _ in timestamps)
    # B-frame packet order differs from presentation order; inspect all PTSs.
    end = max(pts + (length or 1 / fps / tick) for pts, length in timestamps)
    duration = (end - start) * tick
    if not math.isfinite(duration) or duration <= 0:
        raise CompositionError("视频素材没有可用画面，请重新上传。")
    return {**source, "duration": duration, "fps": fps}


def _video_inputs(segments, cuts, total_frames, binary):
    """Validate every requested source window before writing any output."""
    sources, inputs = {}, {}
    for index, segment in enumerate(segments):
        if segment.get("media_type", "card") != "video":
            continue
        raw_start = segment.get("media_start", 0)
        if isinstance(raw_start, bool):
            raise CompositionError("视频素材开始位置无效，请填写非负秒数。")
        media_start = _number(raw_start, "视频素材开始位置")
        if media_start < 0:
            raise CompositionError("视频素材开始位置不能为负数，请填写非负秒数。")
        path = str(Path(segment.get("media_path") or "").expanduser().resolve())
        if path not in sources:
            sources[path] = _video_timing(_probe(path, label="视频素材", video=True), binary)
        source = sources[path]
        frames = min(total_frames, cuts[index + 1]) - min(total_frames, cuts[index])
        if frames <= 0:
            continue
        shot_duration = frames / _FPS
        # Only a rounding tail of at most one original/output frame is allowed.
        # This never turns a short source into a repeated or frozen long scene.
        tolerance = min(0.05, 1 / source["fps"], 1 / _FPS)
        if media_start >= source["duration"] or media_start + shot_duration > source["duration"] + tolerance + 1e-6:
            available = max(0.0, source["duration"] - media_start)
            raise CompositionError(
                f"视频素材 {Path(source['path']).name} 在 {media_start:.2f} 秒之后仅剩 {available:.2f} 秒，"
                f"不足分镜所需的 {shot_duration:.2f} 秒。请补充更长的素材、缩短该分镜或改用图文画面。"
            )
        inputs[index] = (source, media_start)
    return inputs


def _font_path():
    resource = Path(__file__).resolve().parents[3] / "resource" / "fonts"
    configured = os.environ.get("MPT_CREATOR_FONT", "").strip()
    candidates = ([Path(configured)] if configured else []) + [
        resource / "NotoSansSC.ttf",
        resource / "MicrosoftYaHeiNormal.ttc",
        resource / "STHeitiMedium.ttc",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise CompositionError("没有找到可显示中文的字体，请安装微软雅黑或 Noto Sans CJK。")


def load_font(path, size, *, weight=600):
    """Use a readable variable-font weight while preserving static fonts.

    A variable font's default axis may be Thin. Set only its Weight/wght axis,
    retain all other axis defaults, and never modify the font file itself.
    """
    font = ImageFont.truetype(str(path), size)
    try:
        axes = font.get_variation_axes()
    except OSError:
        return font
    coordinates = []
    has_weight = False
    for axis in axes:
        name = axis.get("name", "")
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        if str(name).casefold() in {"weight", "wght"}:
            coordinates.append(max(axis["minimum"], min(axis["maximum"], weight)))
            has_weight = True
        else:
            coordinates.append(axis["default"])
    if has_weight:
        try:
            font.set_variation_by_axes(coordinates)
        except OSError:
            # Older/static font engines may expose axes without supporting a
            # variation setter. Preserve their original usable font instance.
            pass
    return font


def _wrap(text, font, max_width):
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = []
    for paragraph in str(text).splitlines() or [""]:
        current = ""
        for char in paragraph:
            if current and draw.textlength(current + char, font=font) > max_width:
                lines.append(current)
                current = ""
            current += char
        if current:
            lines.append(current)
    return lines


def _information_card(text, output, size, index, count, *, title=""):
    """Render Chinese via Pillow; user text never becomes an FFmpeg expression."""
    width, height = size
    image = Image.new("RGB", size, "#101c30")
    draw = ImageDraw.Draw(image)
    margin = round(width * 0.09)
    top, available_height = round(height * 0.25), round(height * 0.46)
    font_path = _font_path()
    if title:
        title_font = load_font(font_path, max(16, round(width * 0.042)), weight=700)
        title_lines = _wrap(title, title_font, width - margin * 2)
        title_height = math.ceil(title_font.size * 1.4)
        title_y = round(height * 0.14)
        for line in title_lines[:2]:
            draw.text((margin, title_y), line, fill="#56b6ff", font=title_font, anchor="lt")
            title_y += title_height
        top = max(top, title_y + round(height * 0.035))
        available_height = round(height * 0.71) - top
    minimum = max(12, round(width * 0.022))
    maximum = max(minimum, round(width * 0.067))
    for font_size in range(maximum, minimum - 1, -1):
        font = load_font(font_path, font_size, weight=600)
        lines = _wrap(text, font, width - margin * 2)
        line_height = math.ceil(font_size * 1.5)
        if len(lines) * line_height <= available_height:
            break
    else:
        raise CompositionError("一段信息卡文字过长，请将该段文案分成更短的句子再生成。")
    draw.rounded_rectangle((margin, top - margin, width - margin, top - margin + max(4, width // 100)), radius=2, fill="#56b6ff")
    y = top + max(0, (available_height - len(lines) * line_height) / 2)
    for line in lines:
        draw.text((margin, round(y)), line, fill="#f3f7ff", font=font, anchor="lt")
        y += line_height
    small = load_font(font_path, max(12, round(width * 0.027)), weight=400)
    draw.text((margin, round(height * 0.77)), f"{index + 1:02d} / {count:02d}", fill="#86a2be", font=small)
    image.save(output, "PNG")


def _card_content(segment, script):
    """Only show excerpts of the customer's script or the real spoken cue."""
    cue = str(segment.get("source_cue_text") or segment.get("text") or "").strip()
    references = [str(script or ""), cue]
    def literal(value):
        value = value.strip() if isinstance(value, str) else ""
        compact = re.sub(r"\s+", "", value).casefold()
        pattern = re.escape(compact)
        if compact and compact[0].isdigit():
            pattern = r"(?<![\d.,\-+])" + pattern
        if compact and compact[-1].isdigit():
            pattern += r"(?![\d.,])"
        return value if compact and any(re.search(pattern, re.sub(r"\s+", "", source).casefold()) for source in references) else ""
    body = literal(segment.get("card_text")) or cue
    title = literal(segment.get("card_title"))
    if len(title) > 50 or title.rstrip("，。！？；,.!?;") == body.rstrip("，。！？；,.!?;"):
        title = ""
    return body, title


def _prepare_picture(source, output, size):
    path = Path(source).expanduser().resolve()
    if not path.is_file() or not path.stat().st_size:
        raise CompositionError("图片素材不存在或为空，请重新上传。")
    try:
        with Image.open(path) as opened:
            if opened.width * opened.height > 40_000_000:
                raise CompositionError("图片超过 4000 万像素，请先缩小后上传。")
            opened.verify()
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            # Leave a small margin so a gentle zoom does not crop product labels.
            maximum = (round(size[0] * 0.94), round(size[1] * 0.94))
            fitted = ImageOps.contain(image, maximum, method=Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", size, "#101826")
            canvas.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2), fitted)
            canvas.save(output, "PNG")
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, CompositionError):
            raise
        raise CompositionError(f"图片素材 {path.name} 已损坏或不完整，请重新上传。") from exc


def _size(plan):
    aspect = plan.get("aspect", "9:16")
    if aspect not in _ASPECTS:
        raise CompositionError("画幅仅支持 9:16、16:9 和 1:1。")
    size = plan.get("size", _ASPECTS[aspect])
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        raise CompositionError("输出画面尺寸无效。")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 64 or value > 1920 or value % 2 for value in size):
        raise CompositionError("输出宽高必须是 64～1920 之间的偶数。")
    width, height = size
    expected_width, expected_height = _ASPECTS[aspect]
    if abs(width / height - expected_width / expected_height) > 0.02:
        raise CompositionError("输出尺寸与所选画幅不一致。")
    return aspect, (width, height)


def compose(plan, audio_path, output_dir, *, base_video_path=None, progress=None):
    """Compose timed footage and the real voice into a fresh work directory.

    ASR cue boundaries remain unchanged. Visual cuts are quantized against the
    global 30 fps timeline to avoid accumulating rounding errors per shot.
    Avatar base video is validated and returned for downstream decoration.
    """
    if not isinstance(plan, dict):
        raise CompositionError("缺少有效分镜计划，请先生成分镜。")
    aspect, size = _size(plan)
    audio = _probe(audio_path, label="配音文件", audio=True)
    duration = audio["duration"]
    kind = plan.get("kind", "knowledge")
    if kind not in {"knowledge", "product", "montage", "avatar"}:
        raise CompositionError("视频类型无效，请重新选择。")
    segments = plan.get("segments") or plan.get("shots")
    if not isinstance(segments, list) or not segments or len(segments) > 600:
        raise CompositionError("分镜计划应包含 1～600 段真实语音分镜。")
    previous_end = 0.0
    for segment in segments:
        if not isinstance(segment, dict):
            raise CompositionError("分镜内容格式无效。")
        start = _number(segment.get("start"), "分镜开始时间")
        end = _number(segment.get("end"), "分镜结束时间")
        if start < 0 or end <= start or start < previous_end - 0.001:
            raise CompositionError("分镜时间轴无效或重叠，请重新识别当前配音。")
        if end > duration + 0.15 or start >= duration:
            raise CompositionError("分镜超出当前配音时长，请重新识别配音，不能拉长音轨补齐。")
        previous_end = end
    if base_video_path:
        base = _probe(base_video_path, label="数字人视频", video=True)
        if base["duration"] < duration - 0.15:
            raise CompositionError("数字人视频短于当前配音，请用同一份配音重新生成数字人。")
        if progress:
            progress("数字人口播视频已检查，可继续字幕与模板包装", 100)
        return {"video_path": base["path"], "duration": duration, "width": base["width"], "height": base["height"],
                "aspect": aspect, "composition_dir": None, "state": "done", "source": "avatar"}
    if kind == "avatar":
        raise CompositionError("数字人口播缺少已生成的人物视频，请先生成数字人。")
    binary = extract.ffmpeg_binary()
    width, height = size
    total_frames = max(1, math.ceil(duration * _FPS - 1e-6))
    cuts = [0] + [round(_number(row["start"], "分镜开始时间") * _FPS) for row in segments[1:]] + [total_frames]
    video_inputs = _video_inputs(segments, cuts, total_frames, binary)
    root = Path(output_dir).expanduser().resolve()
    directory = root / f"composition-{uuid.uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=False)
    clips, warnings = [], []
    for index, segment in enumerate(segments):
        frames = min(total_frames, cuts[index + 1]) - min(total_frames, cuts[index])
        if frames <= 0:
            warnings.append(f"分镜 {segment.get('id', index + 1)} 短于一帧，未单独切换画面；字幕时间不变。")
            continue
        media_type = segment.get("media_type", "card")
        clip = directory / f"segment-{index:04d}.mp4"
        command = [binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-filter_threads", "2"]
        if media_type == "card":
            if kind != "knowledge":
                raise CompositionError("商品介绍和素材混剪必须使用真实素材，请为缺失分镜补充图片或视频。")
            picture = directory / f"card-{index:04d}.png"
            text, card_title = _card_content(segment, plan.get("script", ""))
            if not text:
                raise CompositionError("信息卡缺少口播文字。")
            _information_card(text, picture, size, index, len(segments), title=card_title)
            command += ["-loop", "1", "-framerate", str(_FPS), "-i", str(picture), "-vf", "setsar=1"]
        elif media_type == "image":
            picture = directory / f"image-{index:04d}.png"
            _prepare_picture(segment.get("media_path") or "", picture, size)
            zoom = f"zoompan=z='min(1.035,1+0.035*on/{max(1, frames - 1)})':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d={frames}:s={width}x{height}:fps={_FPS},setsar=1"
            command += ["-i", str(picture), "-vf", zoom]
        elif media_type == "video":
            source, media_start = video_inputs[index]
            scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x101826,setsar=1,fps={_FPS},tpad=stop_mode=clone:stop=1"
            command += ["-ss", f"{media_start:.9f}", "-i", source["path"], "-vf", scale]
        else:
            raise CompositionError("分镜素材类型无效，请重新选择图片或视频。")
        command += ["-an", "-frames:v", str(frames), "-r", str(_FPS), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "2", "-progress", "pipe:1", "-nostats", str(clip)]
        result = _run(command, timeout=max(60, frames / _FPS * 8), label=f"第 {index + 1} 段画面合成")
        written_frames = re.findall(r"^frame=(\d+)$", result.stdout, flags=re.MULTILINE)
        if not written_frames or int(written_frames[-1]) != frames:
            raise CompositionError(f"第 {index + 1} 段画面不足所需时长，请补充更长的视频素材或改用图文画面。")
        clips.append(clip)
        if progress:
            progress(f"已合成 {index + 1}/{len(segments)} 段画面", (index + 1) * 85 / len(segments))
    if not clips:
        raise CompositionError("没有可合成的画面，请重新生成分镜。")
    # Generated ASCII basenames avoid concat quoting of arbitrary user paths.
    listing = directory / "concat.txt"
    listing.write_text("\n".join(f"file '{clip.name}'" for clip in clips) + "\n", encoding="utf-8")
    output = directory / "visual-track.mp4"
    _run([binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "1", "-i", str(listing),
          "-i", audio["path"], "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
          "-ar", "48000", "-t", f"{duration:.6f}", "-movflags", "+faststart", str(output)],
         timeout=max(60, duration * 4), label="画面与配音合成")
    actual = _probe(output, label="合成视频", audio=True, video=True)
    if abs(actual["duration"] - duration) > 0.2:
        raise CompositionError("合成视频时长与配音不一致，请重新生成分镜。")
    if progress:
        progress("基础视频已生成，可继续字幕与模板包装", 100)
    return {"video_path": str(output), "duration": duration, "width": width, "height": height, "aspect": aspect,
            "composition_dir": str(directory), "state": "done", "warnings": warnings, "source": "composition"}
