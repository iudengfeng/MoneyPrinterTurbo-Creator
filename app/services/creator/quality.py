"""Check real output media before offering a completed creator work.

These checks cover technical delivery, not factual accuracy, copyright,
lip-sync quality or platform approval. No model or paid service is invoked.
"""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

from . import extract, rendering


def inspect_video(video_path, subtitle_path=None, expected_duration=None, progress=None) -> dict:
    report = {"pass": False, "video_path": str(video_path or ""), "checks": [], "errors": [], "warnings": []}

    def check(name, passed, message, *, warning=False):
        report["checks"].append({"name": name, "state": "passed" if passed else ("warning" if warning else "failed"), "message": message})
        if not passed:
            report["warnings" if warning else "errors"].append(message)

    if progress:
        progress("检查成片文件与音视频轨道", 5)
    try:
        path = Path(str(video_path or "")).expanduser().resolve()
        info = rendering.probe_source(path)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, TypeError) as exc:
        check("file", False, "成片文件不能完整读取：" + str(exc)[:350])
        return report
    duration = info["duration"]
    report.update(duration=duration, width=info["width"], height=info["height"], video_path=str(path))
    check("file", True, "成片文件可以读取。")
    check("video", info["has_video"] and info["width"] > 0 and info["height"] > 0, "成片应包含有效视频画面。")
    check("audio", info["has_audio"], "成片应包含配音音轨。")
    if expected_duration is not None:
        try:
            expected = float(expected_duration)
            valid = math.isfinite(expected) and expected > 0
        except (TypeError, ValueError):
            expected, valid = 0, False
        tolerance = max(0.2, min(1.0, expected * 0.003))
        check("duration", valid and abs(duration - expected) <= tolerance,
              "成片时长应与实际配音一致，避免提前结束或拖长。")
    if subtitle_path:
        try:
            captions = rendering.read_srt(subtitle_path)
            valid = bool(captions) and all(0 <= item["start"] < item["end"] <= duration + 0.15 for item in captions)
            check("subtitles", valid, "字幕应有有效内容，且时间轴位于成片范围内。")
            if captions:
                overlap = any(right["start"] < left["end"] - 0.06 for left, right in zip(captions, captions[1:]))
                check("subtitle_overlap", not overlap, "部分字幕时间重叠，请检查预览。", warning=True)
        except (ValueError, OSError, UnicodeError, TypeError, KeyError) as exc:
            check("subtitles", False, "字幕文件无法检查：" + str(exc)[:250])
    if report["errors"]:
        return report
    if progress:
        progress("完整解码并检查声音、黑帧", 35)
    command = [extract.ffmpeg_binary(), "-nostdin", "-hide_banner", "-v", "info", "-xerror", "-i", str(path),
               "-map", "0:v:0", "-map", "0:a:0", "-vf", "blackdetect=d=0.1:pix_th=0.10:pic_th=0.98",
               "-af", "volumedetect", "-f", "null", "-"]
    try:
        decoded = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=max(60, min(600, duration * 3)),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        check("decode", decoded.returncode == 0, "成片音视频应可以完整播放。")
        if decoded.returncode:
            report["decode_detail"] = decoded.stderr[-500:]
        else:
            match = re.search(r"max_volume:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dB", decoded.stderr)
            if match:
                peak = float(match.group(1))
                report["peak_db"] = peak if math.isfinite(peak) else None
                check("sound", peak > -60, "配音接近静音，请检查声音后重新生成。")
                if peak > -60:
                    check("volume", peak >= -35, "配音音量偏小，建议试听后调整。", warning=True)
            else:
                check("sound", False, "未能完成声音检查，请重新检查音轨。")
            dark_sections = re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)", decoded.stderr)
            dark_seconds = sum(float(value[2]) for value in dark_sections)
            if dark_sections and duration - 0.12 <= float(dark_sections[-1][1]) <= duration:
                # blackdetect closes a trailing interval at the last frame's
                # timestamp. Include that frame's display time as well.
                dark_seconds += duration - float(dark_sections[-1][1])
            report["black_seconds"] = min(duration, dark_seconds)
            check("black_frames", dark_seconds < duration * 0.95, "成片几乎全是黑屏，请检查画面素材。")
            if duration * 0.95 > dark_seconds > max(1, duration * 0.25):
                check("dark_sections", False, "成片包含较多黑屏或暗场，请检查画面预览。", warning=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        check("decode", False, "成片播放检查未完成：" + str(exc)[:250])
    report["pass"] = not report["errors"]
    if progress:
        progress("成片检查通过" if report["pass"] else "成片检查需要处理", 100)
    return report
