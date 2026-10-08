"""Seekable local HTML overlays rendered by HyperFrames and composited by FFmpeg."""

from __future__ import annotations

import html
import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import extract

_STYLES = {"clean", "bold", "knowledge", "business"}
_ACCENTS = {"clean": "#ffffff", "bold": "#ffe34d", "knowledge": "#8ad7ff", "business": "#ede4ca"}
_FONT_SUFFIXES = {".ttf", ".ttc", ".otf"}


def component_root() -> Path:
    return Path(__file__).resolve().parents[3] / "creator-hyperframes"


def is_ready() -> bool:
    root = component_root()
    return bool(shutil.which("node") and (root / "node_modules/hyperframes/bin/hyperframes.mjs").is_file()
                and (root / "node_modules/gsap/dist/gsap.min.js").is_file())


def _number(value, name, lower, upper):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}无效。") from exc
    if not math.isfinite(number) or not lower <= number <= upper:
        raise ValueError(f"{name}超出允许范围。")
    return number


def _settings(payload):
    width = int(_number(payload.get("width", 720), "画面宽度", 64, 3840))
    height = int(_number(payload.get("height", 1280), "画面高度", 64, 3840))
    fps = int(_number(payload.get("fps", 30), "帧率", 1, 60))
    frames = int(_number(payload.get("durationInFrames", 90), "画面帧数", 1, fps * 1800))
    if width % 2 or height % 2:
        raise ValueError("画面尺寸需要为偶数。")
    if payload.get("style", "clean") not in _STYLES:
        raise ValueError("不支持的画面风格。")
    if payload.get("template", "talking") not in {"talking", "pip", "cards"}:
        raise ValueError("不支持的画面模板。")
    if payload.get("subtitleStyle", "clean") not in {"clean", "bold", "yellow", "none"}:
        raise ValueError("不支持的字幕样式。")
    if payload.get("colorGrade", "none") not in {"none", "warm", "cool", "vivid"}:
        raise ValueError("不支持的画面颜色。")
    if payload.get("videoFit", "contain") not in {"contain", "cover"}:
        raise ValueError("不支持的画面适配方式。")
    return width, height, fps, frames, frames / fps


def _captions(payload, duration):
    rows = payload.get("captions") or []
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("字幕数据无效或过多。")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("字幕数据格式无效。")
        start = _number(row.get("start"), "字幕开始时间", 0, duration)
        end = _number(row.get("end"), "字幕结束时间", 0, duration + 0.05)
        text = re.sub(r"[^\S\n]+", " ", str(row.get("text", "")).replace("\r\n", "\n").replace("\r", "\n")).strip()[:1000]
        if end <= start or not text:
            continue
        result.append({"start": start, "end": min(end, duration), "text": text})
    return sorted(result, key=lambda row: (row["start"], row["end"]))


def _point_cards(captions, duration, style):
    """Extract original phrases only; never invent product claims or prices."""
    if style == "clean":
        return []
    cards, last_end = [], -6.0
    for row in captions:
        text = row["text"]
        if len(text) < 8 or len(text) > 45 or row["start"] < 4 or row["start"] < last_end + 5:
            continue
        start, end = row["start"], min(duration, max(row["end"], row["start"] + 2.4))
        if end - start < 1:
            continue
        cards.append({"start": start, "end": end, "text": text})
        last_end = end
        if len(cards) >= 80:
            break
    return cards


def _prepare_font_asset(assets):
    """Keep a selected font inside this private render project, under a fixed name."""
    configured = os.environ.get("MPT_CREATOR_FONT", "").strip()
    if not configured:
        return None
    source = Path(configured).expanduser()
    if source.suffix.lower() not in _FONT_SUFFIXES or not source.is_file():
        return None
    filename = "creator-font" + source.suffix.lower()
    target = Path(assets) / filename
    try:
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
    except OSError as exc:
        raise ValueError("选定的本机字体无法用于字幕，请检查字体文件是否可读取。") from exc
    return filename


def build_overlay_html(payload, *, font_asset=None) -> str:
    """Build offline composition; all user text remains escaped HTML text."""
    width, height, fps, frames, duration = _settings(payload)
    style, subtitle = payload.get("style", "clean"), payload.get("subtitleStyle", "clean")
    accent = _ACCENTS[style]
    captions = _captions(payload, duration)
    title = str(payload.get("title", ""))[:120]
    title_bg = "#ffe34def" if style == "bold" else "#101828cf"
    title_color = "#171717" if style == "bold" else "#ffffff"
    title_size = width * (0.042 if len(title) > 65 else 0.047 if len(title) > 35 else 0.054)
    caption_size = width * (0.064 if subtitle == "bold" else 0.052)
    caption_color = "#ffe34d" if subtitle in {"bold", "yellow"} else "#ffffff"
    point_size = min(width*.033, height*.035)
    if font_asset is not None and font_asset not in {"creator-font" + suffix for suffix in _FONT_SUFFIXES}:
        raise ValueError("本机字幕字体素材名称无效。")
    local_face = (f'@font-face{{font-family:"Creator Local";src:url("assets/{font_asset}");'
                  'font-weight:100 900;font-style:normal;font-display:block}') if font_asset else ""
    font_family = ('"Creator Local",' if font_asset else "") + '"Microsoft YaHei","Noto Sans CJK SC",sans-serif'
    load_local_font = f'await document.fonts.load(\'800 {caption_size:.9f}px "Creator Local"\').catch(()=>[]);' if font_asset else ""
    title_html = f'<div id="title" class="title"><span>{html.escape(title)}</span></div>' if title else ""
    caption_html, animations = [], []
    if title:
        animations.append('tl.fromTo("#title", {opacity:0,y:-18}, {opacity:1,y:0,duration:0.32,ease:"power2.out"}, 0);')
    if subtitle != "none":
        for index, row in enumerate(captions):
            content = html.escape(row["text"])
            caption_html.append(f'<div id="caption-{index}" class="clip caption" data-start="{row["start"]:.9f}" '
                                f'data-duration="{row["end"]-row["start"]:.9f}" data-track-index="10">'
                                f'<span>{content}</span></div>')
            entrance = min(0.12, (row["end"] - row["start"]) / 3)
            animations.append(f'tl.fromTo("#caption-{index} span", {{y:5}}, {{y:0,duration:{entrance:.9f},ease:"power2.out"}}, {row["start"]:.9f});')
    points = []
    for index, row in enumerate(_point_cards(captions, duration, style)):
        points.append(f'<div id="point-{index}" class="clip point" data-start="{row["start"]:.9f}" '
                      f'data-duration="{row["end"]-row["start"]:.9f}" data-track-index="9">'
                      f'<span>{html.escape(row["text"])}</span></div>')
        animations.append(f'tl.fromTo("#point-{index} span", {{y:12,scale:0.97}}, '
                          f'{{y:0,scale:1,duration:0.28,ease:"power2.out"}}, {row["start"]:.9f});')
    line = '<div class="footer-line"></div>' if payload.get("template") == "cards" else ""
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self';">
<title>Creator overlay</title><style>
{local_face}
@font-face{{font-family:"Microsoft YaHei";src:local("Microsoft YaHei");font-weight:100 900}}
@font-face{{font-family:"Noto Sans CJK SC";src:local("Noto Sans CJK SC");font-weight:100 900}}
*{{box-sizing:border-box}} html,body{{margin:0;width:{width}px;height:{height}px;background:transparent!important;overflow:hidden}}
#root{{position:relative;width:{width}px;height:{height}px;background:transparent;overflow:hidden;font-family:{font_family};font-weight:800;color:white}}
.title{{position:absolute;top:{height*.047:.3f}px;left:6%;right:6%;padding:{width*.019:.3f}px {width*.025:.3f}px;font-size:{title_size:.3f}px;line-height:1.38;background:{title_bg};color:{title_color};border-radius:14px;border-left:{width*.009:.3f}px solid {accent};overflow-wrap:anywhere}}
.title span{{display:block;white-space:pre-wrap}}
.caption{{position:absolute;bottom:{height*.105:.3f}px;left:7%;right:7%;display:flex;justify-content:center}}
.caption span{{display:block;max-width:100%;padding:{width*.015:.3f}px {width*.022:.3f}px;border-radius:12px;background:#000b;color:{caption_color};font-size:{caption_size:.3f}px;line-height:1.35;text-align:center;white-space:pre-wrap;overflow-wrap:anywhere;text-shadow:0 2px 5px #000,0 -1px 2px #000;border-bottom:3px solid {accent};}}
.point{{position:absolute;bottom:27%;left:6%;width:42%;max-height:22%;overflow:hidden;}}
.point span{{display:block;padding:{width*.016:.3f}px {width*.020:.3f}px;border-radius:12px;background:#101828d9;font-size:{point_size:.3f}px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;border-left:4px solid {accent};box-shadow:0 8px 24px #0004}}
.footer-line{{position:absolute;bottom:3.5%;left:6%;right:6%;height:3px;background:{accent};opacity:.7}}
</style></head><body>
<div id="root" data-composition-id="root" data-start="0" data-duration="{duration:.9f}" data-width="{width}" data-height="{height}" data-fps="{fps}">
{title_html}{''.join(points)}{''.join(caption_html)}{line}
</div><script src="assets/gsap.min.js"></script><script>
const tl = gsap.timeline({{paused:true}});
{''.join(animations)}
tl.to({{}},{{duration:0.001}},{max(0,duration-.001):.9f});
window.__timelines=window.__timelines||{{}};window.__timelines.root=tl;
// Fit real Chinese glyphs locally without a remote font request.
window.__renderReady=false;
window.__creatorFontReady=(async()=>{{
{load_local_font}
await document.fonts.ready;
const measure=document.createElement("canvas").getContext("2d");
for(const el of document.querySelectorAll(".caption span")){{
 let size={caption_size:.9f}; measure.font=`800 ${{size}}px {font_family}`;
 const measured=Math.max(...el.textContent.split("\\n").map(line=>measure.measureText(line).width));
 if(measured>{width*.80:.9f}) el.style.fontSize=`${{size*{width*.80:.9f}/measured}}px`;
}}
const title=document.querySelector(".title");
if(title){{let size={title_size:.9f};while(title.scrollHeight>size*1.38*4+{width*.038:.9f}&&size>{width*.023:.9f}){{size-=1;title.style.fontSize=`${{size}}px`;}}}}
window.__renderReady=true;
}})();
</script></body></html>'''


def _browser_path():
    configured = os.environ.get("HYPERFRAMES_BROWSER_PATH") or os.environ.get("MPT_RENDER_BROWSER")
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    cache_value = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if cache_value and cache_value != "0":
        cache = Path(cache_value)
    elif os.name == "nt":
        cache = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright"
    caches = [cache]
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if os.name == "nt" and local:
        caches.append(Path(local) / "ms-playwright")
    for folder in dict.fromkeys(caches):
        candidates = [path for path in folder.glob("chromium_headless_shell-*/chrome-headless-shell-*/*")
                      if path.name in {"chrome-headless-shell", "chrome-headless-shell.exe"} and path.is_file()]
        if candidates:
            return str(max(candidates, key=lambda path: path.stat().st_mtime).resolve())
    return None


def _run(command, log, duration, env=None, progress=None, phase="HyperFrames"):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n{phase}:\n")
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines = queue.Queue()
        def read_lines():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        deadline = time.monotonic() + max(300, min(7200, duration * 25))
        last = -1
        try:
            while True:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{phase}渲染超时，请缩短视频后重试。")
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                handle.write(line)
                handle.flush()
                match = re.search(r"(?:^|\s)(\d{1,3})(?:\.\d+)?%", line)
                if match and progress:
                    percent = min(100, int(match.group(1)))
                    if percent > last:
                        progress("HyperFrames 正在渲染字幕与观点卡", 20 + percent * .56)
                        last = percent
            code = proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                                   timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    proc.kill()
                proc.wait(timeout=15)
            reader.join(timeout=3)
            proc.stdout.close()
    if code:
        raise RuntimeError(f"{phase}渲染失败：" + log.read_text("utf-8", errors="replace")[-1500:])


def _fit_filter(width, height, fit):
    if fit == "cover":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    return f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x10131b"


def _even(value):
    return max(2, int(round(value / 2)) * 2)


def composite_command(payload, overlay_path):
    width, height, fps, frames, duration = _settings(payload)
    video = Path(payload.get("video", "")).resolve()
    if not video.is_file():
        raise ValueError("底层人物或素材视频不存在。")
    output = Path(payload.get("output", "")).resolve()
    if output == video:
        raise ValueError("输出路径不能覆盖原始视频。")
    binary = extract.ffmpeg_binary()
    command = [binary, "-hide_banner", "-v", "error", "-nostdin", "-y", "-i", str(video)]
    next_index, filters = 1, []
    overlay = Path(overlay_path).resolve()
    template, fit = payload.get("template", "talking"), payload.get("videoFit", "contain")
    grade = {"none": "", "warm": ",colorbalance=rs=.035:bs=-.025,eq=saturation=1.06:brightness=.008",
             "cool": ",colorbalance=rs=-.025:bs=.035,eq=saturation=.93:contrast=1.025",
             "vivid": ",eq=saturation=1.15:contrast=1.07:brightness=.015"}[payload.get("colorGrade", "none")]
    common = f"fps={fps},trim=end_frame={frames},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={1/fps:.9f},setsar=1"
    if template == "pip":
        background = Path(payload.get("image") or "").resolve()
        if not background.is_file():
            raise ValueError("画中画模板背景图片不存在。")
        command += ["-loop", "1", "-i", str(background)]
        filters.append(f"[{next_index}:v]{_fit_filter(width,height,'cover')},fps={fps},trim=duration={duration:.9f},setpts=PTS-STARTPTS[bg]")
        next_index += 1
        box_w, box_h = _even(width*.38), _even(height*.35)
        filters.append(f"[0:v]{common},{_fit_filter(box_w,box_h,fit)}{grade}[source]")
        filters.append(f"[bg][source]overlay=x={int(width*.57)}:y={int(height*.43)}:eof_action=pass[base]")
    elif template == "cards":
        box_w, box_h = _even(width*.90), _even(height*.64)
        filters.append(f"[0:v]{common},{_fit_filter(box_w,box_h,fit)}{grade},pad={width}:{height}:{int(width*.05)}:{int(height*.13)}:color=0x10131b[base]")
    else:
        filters.append(f"[0:v]{common},{_fit_filter(width,height,fit)}{grade}[base]")
    current = "base"
    for index, item in enumerate(payload.get("pipItems") or []):
        path = Path(item.get("path", "")).resolve()
        if not path.is_file():
            raise ValueError("画中画素材不存在。")
        start = _number(item.get("start", 0), "画中画开始时间", 0, duration)
        end = _number(item.get("end", duration), "画中画结束时间", 0, duration + .05)
        size = _number(item.get("size", .3), "画中画尺寸", .15, .6)
        if end <= start:
            raise ValueError("画中画时间无效。")
        if item.get("kind") == "image":
            command += ["-loop", "1", "-i", str(path)]
        else:
            command += ["-stream_loop", "-1", "-i", str(path)]
        box_w = _even(width*size)
        ratio = _number(item.get("height", 9), "素材高度", 1, 20000) / _number(item.get("width", 16), "素材宽度", 1, 20000)
        box_h = _even(min(height*.42, box_w*ratio))
        filters.append(f"[{next_index}:v]{_fit_filter(box_w,box_h,'contain')},fps={fps},trim=duration={end-start:.9f},setpts=PTS-STARTPTS+{start:.9f}/TB[pip{index}]")
        gap, position = int(width*.045), item.get("position", "top-right")
        if position == "center":
            x, y = int((width-box_w)/2), int((height-box_h)/2)
        elif position in {"top-left", "top-right", "bottom-left", "bottom-right"}:
            x = gap if "left" in position else width-gap-box_w
            y = int(height*.19) if position.startswith("top") else int(height*.77)-box_h
        else:
            raise ValueError("画中画位置无效。")
        label = f"base{index}"
        filters.append(f"[{current}][pip{index}]overlay={x}:{y}:enable='gte(t,{start:.9f})*lt(t,{end:.9f})':eof_action=pass[{label}]")
        next_index, current = next_index+1, label
    if overlay.suffix.lower() == ".webm":
        command += ["-c:v", "libvpx-vp9"]
    command += ["-i", str(overlay)]
    filters.append(f"[{next_index}:v]format=rgba,setpts=PTS-STARTPTS[alpha]")
    filters.append(f"[{current}][alpha]overlay=0:0:eof_action=pass:format=auto,format=yuv420p[visual]")
    command += ["-filter_complex_threads", "2", "-filter_complex", ";".join(filters), "-map", "[visual]", "-an",
                "-frames:v", str(frames), "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-movflags", "+faststart", str(output)]
    return command


def render_visual(payload, log, progress=None):
    """Render a genuine local HyperFrames alpha layer, then silent H.264 visual."""
    if not is_ready():
        raise ValueError("HyperFrames 组件未就绪，请完成创作中心组件安装。")
    width, height, fps, frames, duration = _settings(payload)
    from . import rendering
    from .composition import _video_timing
    source = rendering.probe_source(payload.get("video", ""))
    if not source["has_video"]:
        raise ValueError("底层素材不包含视频画面。")
    timing = _video_timing(source, extract.ffmpeg_binary())
    if duration > timing["duration"] + min(.05, 1/timing["fps"]) + 1e-6:
        raise ValueError("底层画面短于完整口播，不能循环补齐；请重新生成数字人或选择人物＋图文。")
    output, log = Path(payload["output"]).resolve(), Path(log)
    output.parent.mkdir(parents=True, exist_ok=True)
    project = output.parent / "hyperframes-project"
    assets = project / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    font_asset = _prepare_font_asset(assets)
    (project / "index.html").write_text(build_overlay_html(payload, font_asset=font_asset), "utf-8")
    shutil.copy2(component_root() / "node_modules/gsap/dist/gsap.min.js", assets / "gsap.min.js")
    (project / "hyperframes.json").write_text(json.dumps({"name": "creator-overlay"}), "utf-8")
    env = os.environ.copy()
    env.update({"HYPERFRAMES_NO_UPDATE_CHECK": "1", "HYPERFRAMES_NO_TELEMETRY": "1", "HYPERFRAMES_SKIP_SKILLS": "1",
                "DO_NOT_TRACK": "1", "HYPERFRAMES_FFMPEG_PATH": extract.ffmpeg_binary(), "PRODUCER_ENABLE_STREAMING_ENCODE": "true"})
    browser = _browser_path()
    if not browser:
        raise ValueError("未找到本机渲染浏览器，请完成创作中心浏览器组件安装。")
    env["HYPERFRAMES_BROWSER_PATH"] = browser
    probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    probe_candidates = [env.get("HYPERFRAMES_FFPROBE_PATH"), env.get("MPT_FFPROBE"),
                        str(Path(extract.ffmpeg_binary()).with_name(probe_name)),
                        str(component_root() / "bin" / probe_name), str(component_root() / "bin/ffmpeg" / probe_name),
                        shutil.which("ffprobe")]
    probe = next((str(Path(value).resolve()) for value in probe_candidates if value and Path(value).is_file()), None)
    if not probe:
        raise ValueError("HyperFrames 需要 FFprobe，请完成创作中心音视频组件安装。")
    env["HYPERFRAMES_FFPROBE_PATH"] = probe
    # ProRes alpha can stream frames on Windows, avoiding ~11 GB of raw scratch
    # for a 100-second 720x1280 composition. Short fixtures use VP9 WebM.
    format_name = "webm" if duration <= 12 else "mov"
    overlay = output.parent / f"hyperframes-overlay.{format_name}"
    cli = component_root() / "node_modules/hyperframes/bin/hyperframes.mjs"
    command = [shutil.which("node"), str(cli), "render", str(project), "--format", format_name,
               "--output", str(overlay), "--fps", str(fps), "--workers", "1", "--no-browser-gpu",
               "--no-best-effort", "--quality", "standard"]
    if format_name == "webm":
        command += ["--vp9-cpu-used", "6"]
    _run(command, log, duration, env=env, progress=progress)
    if not overlay.is_file() or overlay.stat().st_size == 0:
        raise RuntimeError("HyperFrames 没有生成透明字幕层，请查看渲染记录。")
    if progress:
        progress("FFmpeg 正在合成透明字幕与画面", 80)
    _run(composite_command(payload, overlay), log, duration, phase="FFmpeg")
    if not output.is_file() or not output.stat().st_size:
        raise RuntimeError("透明字幕与画面合成没有生成视频。")
    report = {"renderer": "hyperframes", "version": "0.8.137", "width": width, "height": height, "fps": fps,
              "frames": frames, "duration": duration, "alpha_format": format_name, "html_path": str(project / "index.html"),
              "original_text_point_cards": len(_point_cards(_captions(payload, duration), duration, payload.get("style", "clean")))}
    (output.parent / "hyperframes-render.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    if os.environ.get("MPT_HYPERFRAMES_KEEP_OVERLAY") != "1":
        overlay.unlink(missing_ok=True)
    return {"output": str(output), "overlay_format": "webm-vp9-alpha" if format_name == "webm" else "mov-prores4444-alpha",
            "overlay_path": str(overlay) if overlay.is_file() else "", "renderer_version": "0.8.137",
            "html_path": str(project / "index.html"), "report_path": str(output.parent / "hyperframes-render.json")}
