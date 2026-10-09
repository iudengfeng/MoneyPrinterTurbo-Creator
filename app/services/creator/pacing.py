"""Cut measured long pauses while keeping frames, voice and subtitles aligned."""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path

from . import extract, processing, rendering


def keep_intervals(silences, duration, padding=.08):
    cuts = []
    for start, end in sorted(silences):
        start, end = max(0., start), min(duration, end)
        left = 0. if start < .01 else start + padding
        right = duration if end >= duration-.01 else end-padding
        left, right = round(left*30)/30, round(right*30)/30
        if right > left:
            if cuts and left <= cuts[-1][1]:
                cuts[-1][1] = max(right, cuts[-1][1])
            else:
                cuts.append([left, right])
    keep, cursor = [], 0.
    for start, end in cuts:
        if start-cursor >= 1/30:
            keep.append((cursor, start))
        cursor = end
    if duration-cursor >= 1/30:
        keep.append((cursor, duration))
    if not keep:
        raise ValueError("这段音轨只有静音，无法生成有效口播，请检查配音。")
    return keep


def detect_silences(audio, duration, threshold, minimum):
    response = extract._run([extract.ffmpeg_binary(), "-hide_banner", "-nostdin", "-i", str(audio),
                             "-map", "0:a:0", "-af", f"silencedetect=noise={threshold:.3f}dB:d={minimum:.3f}",
                             "-f", "null", "-"], timeout=max(60, duration*2))
    if response.returncode:
        raise ValueError("无法分析配音停顿，请检查音轨文件。")
    silences, start = [], None
    for marker, value in re.findall(r"silence_(start|end):\s*(-?[0-9.]+)", response.stderr):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("静音分析结果无效。")
        if marker == "start":
            start = max(0, value)
        elif start is not None:
            silences.append((start, min(duration, value)))
            start = None
    if start is not None:
        silences.append((start, duration))
    return silences


def remap_segments(rows, timeline):
    result = []
    for row in rows or []:
        matches = []
        for interval in timeline:
            start = max(float(row["start"]), interval["source_start"])
            end = min(float(row["end"]), interval["source_end"])
            if end > start:
                offset = interval["target_start"]-interval["source_start"]
                matches.append((start+offset, end+offset))
        if matches:
            mapped = copy.deepcopy(row)
            mapped.update(start=matches[0][0], end=matches[-1][1])
            if row.get("words"):
                mapped["words"] = remap_segments(row["words"], timeline)
            result.append(mapped)
    return result


def prepare_media(video_path, audio_path, srt_path, segments, output_dir, *, enabled,
                  threshold_db=-40, min_silence=.7, progress=None):
    threshold = processing.number(threshold_db, "静音阈值", -70, -15)
    minimum = processing.number(min_silence, "最短静音", .2, 3)
    if not isinstance(enabled, bool):
        raise ValueError("剪气口开关无效。")
    video = rendering.probe_source(video_path)
    audio = rendering.probe_source(audio_path or video_path)
    if not video["has_video"] or not audio["has_audio"]:
        raise ValueError("剪气口需要有效视频画面与口播音轨。")
    duration = audio["duration"]
    unchanged = {"video_path": video["path"], "audio_path": audio["path"], "srt_path": str(srt_path or ""),
                 "segments": copy.deepcopy(segments), "duration": duration, "timeline": [], "removed_seconds": 0.}
    if not enabled:
        return unchanged
    if progress:
        progress("分析口播停顿，保留短呼吸与停连", 5)
    silences = detect_silences(audio["path"], duration, threshold, minimum)
    if not silences:
        return unchanged
    intervals = keep_intervals(silences, duration)
    if len(intervals) > 1000:
        raise ValueError("剪辑片段过多，请缩短视频或提高最短静音时长。")
    timeline, cursor = [], 0.
    for start, end in intervals:
        timeline.append({"source_start": start, "source_end": end, "target_start": cursor,
                         "target_end": cursor+end-start})
        cursor += end-start
    if duration-cursor < 1/30:
        return unchanged
    folder = Path(output_dir).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    target_video, target_audio, target_srt = folder / "paced.mp4", folder / "paced.wav", folder / "paced.srt"
    originals = {Path(video["path"]).resolve(), Path(audio["path"]).resolve(), Path(srt_path).resolve() if srt_path else None}
    if target_video in originals or target_audio in originals or target_srt in originals:
        raise ValueError("输出不能覆盖原视频、音频或字幕。")
    count = len(intervals)
    filters = [f"[0:v]fps=30,split={count}" + "".join(f"[vr{i}]" for i in range(count)),
               f"[1:a:0]asplit={count}" + "".join(f"[ar{i}]" for i in range(count))]
    for i, (start, end) in enumerate(intervals):
        filters += [f"[vr{i}]trim=start={start:.9f}:end={end:.9f},setpts=PTS-STARTPTS[v{i}]",
                    f"[ar{i}]atrim=start={start:.9f}:end={end:.9f},asetpts=PTS-STARTPTS[a{i}]"]
    filters.append("".join(f"[v{i}][a{i}]" for i in range(count)) + f"concat=n={count}:v=1:a=1[vout][aout]")
    command = [extract.ffmpeg_binary(), "-hide_banner", "-v", "error", "-nostdin", "-y", "-i", video["path"],
               "-i", audio["path"], "-filter_complex_threads", "2", "-filter_complex", ";".join(filters),
               "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
               "-c:a", "aac", "-r", "30", "-movflags", "+faststart", str(target_video)]
    response = extract._run(command, timeout=max(120, duration*5))
    if response.returncode or not target_video.is_file():
        raise RuntimeError("视频剪气口失败：" + response.stderr[-500:])
    response = extract._run([extract.ffmpeg_binary(), "-v", "error", "-nostdin", "-y", "-i", str(target_video),
                             "-vn", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(target_audio)], timeout=max(60, duration*2))
    if response.returncode:
        raise RuntimeError("剪气口音轨提取失败。")
    info = rendering.probe_source(target_video)
    if abs(info["duration"]-cursor) > .12 or not info["has_audio"]:
        raise RuntimeError("剪气口后的音画时长校验未通过。")
    rows = remap_segments(segments or (rendering.read_srt(srt_path) if srt_path else []), timeline)
    if rows:
        rendering._write_captions(target_srt, rows)
    return {"video_path": str(target_video), "audio_path": str(target_audio),
            "srt_path": str(target_srt) if rows else "", "segments": rows, "duration": cursor,
            "timeline": timeline, "removed_seconds": duration-cursor}
