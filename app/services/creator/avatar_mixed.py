"""Reuse a short avatar once, interleaved with cards on the full voice timeline.

The avatar is driven by an exact concatenation of selected source-audio ranges.
Only its video is placed back onto those ranges; the final audio and subtitles
remain those of the complete narration. No speech, image or data is invented.
"""

from __future__ import annotations

import copy
import itertools
import json
import math
import subprocess
import uuid
import wave
from pathlib import Path

from . import extract, planning, rendering

_FPS = 30
_SAMPLE_RATE = 44100
_SAMPLES_PER_FRAME = _SAMPLE_RATE // _FPS


class AvatarMixedError(ValueError):
    """An actionable input or decoding problem in mixed-avatar preparation."""


def _finite(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AvatarMixedError(f"{label}必须是有效秒数。") from exc
    if not math.isfinite(result):
        raise AvatarMixedError(f"{label}必须是有效秒数。")
    return result


def _probe(path, *, video=False):
    try:
        result = rendering.probe_source(path)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        raise AvatarMixedError("无法读取人物视频或口播音轨，请重新上传有效文件。") from exc
    if video and not result["has_video"]:
        raise AvatarMixedError("数字人输出没有视频画面，请重新生成人物镜头。")
    if not video and not result["has_audio"]:
        raise AvatarMixedError("口播文件没有可用音轨，请重新生成配音。")
    return result


def _select_ranges(cues, duration, budget_frames):
    """Prefer complete ASR phrases and three widely separated cameos.

    All cuts lie on both the 30 fps grid and the 44100 Hz sample grid. A phrase
    boundary moves by at most half a frame. Exceptionally long ASR phrases are
    split only when no complete phrase can fit an individual cameo budget.
    """
    total_frames = math.floor(duration * _FPS + 1e-8)
    count = 3 if duration >= 12 and budget_frames >= 3 * _FPS else 2 if duration >= 6 and budget_frames >= 2 * _FPS else 1
    minimum = min(2 * _FPS, max(1, budget_frames // count))
    maximum = budget_frames if count == 1 else min(budget_frames - minimum * (count - 1), math.ceil(budget_frames * 0.55))
    starts = sorted({0, *(round(row["start"] * _FPS) for row in cues)})
    ends = sorted({total_frames, *(min(total_frames, round(row["end"] * _FPS)) for row in cues)})
    anchors = (0.0, 0.5, 1.0) if count == 3 else (0.0, 1.0) if count == 2 else (0.0,)
    groups, fallback = [], False
    for anchor in anchors:
        candidates = []
        for start in starts:
            for end in ends:
                length = end - start
                if not minimum <= length <= maximum or start < 0 or end > total_frames:
                    continue
                if anchor == 0 and start != 0:
                    continue
                if anchor == 1 and end != total_frames:
                    continue
                if anchor == 0.5 and not 0.3 <= ((start + end) / 2) / total_frames <= 0.7:
                    continue
                distance = abs((start + end) / 2 - anchor * total_frames)
                candidates.append((start, end, distance))
        if not candidates:
            # A single very long ASR phrase still needs a safe short cameo.
            # The exact original phrase remains on its source cue metadata.
            length = max(1, budget_frames // count)
            start = 0 if anchor == 0 else total_frames - length if anchor == 1 else round(total_frames * 0.5 - length / 2)
            candidates = [(start, start + length, 0.0)]
            fallback = True
        # Keep diverse lengths to let the combined budget be filled efficiently.
        by_length = {}
        for candidate in candidates:
            length = candidate[1] - candidate[0]
            if length not in by_length or candidate[2] < by_length[length][2]:
                by_length[length] = candidate
        candidates = sorted(by_length.values(), key=lambda row: (row[2], -(row[1] - row[0])))
        if len(candidates) > 48:
            # Spread representatives across the length range, instead of losing
            # every short option to long but mutually incompatible candidates.
            lengths = sorted(by_length)
            chosen_lengths = {lengths[round(index * (len(lengths) - 1) / 47)] for index in range(48)}
            candidates = [by_length[length] for length in chosen_lengths]
        groups.append(candidates)
    best, best_score = None, None
    gap = min(_FPS, max(1, total_frames // 20))
    for combination in itertools.product(*groups):
        if any(previous[1] + gap > current[0] for previous, current in zip(combination, combination[1:])):
            continue
        frames = sum(end - start for start, end, _ in combination)
        if frames > budget_frames:
            continue
        # Cover as much of the allowed avatar budget as possible, then select
        # the candidates closest to opening / middle / closing positions.
        imbalance = sum(abs(end - start - budget_frames / count) for start, end, _ in combination)
        score = (frames, -sum(row[2] for row in combination) - imbalance * 2)
        if best_score is None or score > best_score:
            best, best_score = combination, score
    if best is None:
        # Rare timestamps can make individually valid phrase candidates unable
        # to share a total budget; use bounded, disjoint frame-grid windows.
        length = max(1, budget_frames // count)
        best = [(0 if anchor == 0 else total_frames - length if anchor == 1 else round(total_frames * 0.5 - length / 2), 0, 0.0) for anchor in anchors]
        best = [(start, start + length, distance) for start, _, distance in best]
        fallback = True
    result, offset_samples = [], 0
    for start, end, _ in best:
        samples = (end - start) * _SAMPLES_PER_FRAME
        result.append({
            "start": start / _FPS,
            "end": end / _FPS,
            "start_sample": start * _SAMPLES_PER_FRAME,
            "end_sample": end * _SAMPLES_PER_FRAME,
            "media_start": offset_samples / _SAMPLE_RATE,
            "media_end": (offset_samples + samples) / _SAMPLE_RATE,
        })
        offset_samples += samples
    return result, fallback


def _overlay(plan, ranges):
    original = plan["segments"]
    boundaries = {0.0, plan["duration"]}
    # A subtitle timestamp is never changed. Visual cuts alone use the render
    # grid, so rounding a later video node does not repeat or skip a source frame.
    boundaries.update(min(plan["duration"], round(row["visual_start"] * _FPS) / _FPS) for row in original)
    boundaries.update(value for row in ranges for value in (row["start"], row["end"]))
    boundaries = sorted(boundaries)
    segments = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end - start < 1e-7:
            continue
        middle = (start + end) / 2
        source = next((row for row in original if row["visual_start"] <= middle < row["visual_end"]), original[-1])
        cameo = next((row for row in ranges if row["start"] <= middle < row["end"]), None)
        row = {**source, "id": f"shot-{len(segments) + 1:03d}", "start": start, "end": end,
               "visual_start": start, "visual_end": end, "source_cue_start": source["start"],
               "source_cue_end": source["end"], "source_cue_text": source["text"], "reuse_count": 0}
        if cameo:
            row.update(media_type="video", media_path=None, no_loop=True,
                       avatar_cameo=True,
                       media_start=cameo["media_start"] + start - cameo["start"],
                       match_reason="同一人物的开头、中段或结尾镜头；每段只播放一次，使用原配音片段驱动。")
        else:
            row["avatar_cameo"] = False
            if source.get("media_type") == "video":
                row["media_start"] = float(source.get("media_start") or 0) + start - source["visual_start"]
                row["no_loop"] = True
            elif source.get("media_type") == "card":
                row.update(media_type="card", media_path=None,
                           match_reason="使用这一段真实口播的信息卡，人物模板不循环。")
                row.pop("media_start", None)
        segments.append(row)
    plan.update(segments=segments, shots=segments, source_kind="avatar_mixed", cameo_ranges=ranges,
                matching_method="asr_timed_avatar_and_cards")
    return plan


def prepare(script, segments, audio_path, reference_duration, output_dir, *, aspect="9:16", progress=None,
            materials=None, video_brief=None):
    """Decode and select source audio, without TTS or avatar generation.

    The JSON-compatible result is a resumable checkpoint. ``compact_audio_path``
    drives the avatar once; ``full_audio_path`` is the sole final narration.
    Selected ranges total at most 24 seconds, 30% of the full voice, and the
    reference duration minus one safety frame. The input file is never changed.
    """
    reference = _finite(reference_duration, "人物模板时长")
    if reference < 2:
        raise AvatarMixedError("人物模板不足 2 秒，无法安排自然的人物镜头。请上传更长的同人物视频。")
    source = _probe(audio_path)
    if progress:
        progress("准备人物与图文穿插，保留整条配音及字幕时间轴", 0)
    root = Path(output_dir).expanduser().resolve()
    directory = root / f"avatar-mixed-{uuid.uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=False)
    full_audio = directory / "full-narration.wav"
    compact_audio = directory / "avatar-narration.wav"
    command = [extract.ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-i", source["path"], "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(_SAMPLE_RATE),
               "-c:a", "pcm_s16le", str(full_audio)]
    try:
        decoded = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=max(60, min(600, source["duration"] * 4)),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AvatarMixedError("配音解码失败或超时，请检查 FFmpeg 或更换有效口播音轨。") from exc
    if decoded.returncode:
        raise AvatarMixedError("配音无法完整解码，请重新生成或上传有效音轨。" + decoded.stderr[-350:])
    try:
        with wave.open(str(full_audio), "rb") as reader:
            frame_count = reader.getnframes()
            duration = frame_count / reader.getframerate()
            pcm = reader.readframes(frame_count)
    except (OSError, wave.Error) as exc:
        raise AvatarMixedError("没有得到完整的 PCM 配音，请重新生成音轨。") from exc
    if duration < 2:
        raise AvatarMixedError("口播不足 2 秒，请先准备一段完整的文案和配音。")
    base = planning.build_plan(script, segments, materials or [], kind="knowledge", aspect=aspect,
                               duration=duration, video_brief=video_brief)
    budget = math.floor(min(24.0, duration * 0.3, reference - 1 / _FPS) * _FPS + 1e-8)
    ranges, split_phrase = _select_ranges(base["segments"], duration, budget)
    selected = b"".join(pcm[row["start_sample"] * 2:row["end_sample"] * 2] for row in ranges)
    if not selected:
        raise AvatarMixedError("没有可用的人物口播片段，请重新识别配音时间轴。")
    with wave.open(str(compact_audio), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(_SAMPLE_RATE)
        writer.writeframes(selected)
    plan = _overlay(base, ranges)
    cameo_duration = len(selected) / (2 * _SAMPLE_RATE)
    plan["warnings"] = list(dict.fromkeys([*base.get("warnings", []),
        "人物只用于部分口播，其余匹配上传素材或使用文案信息卡；没有下载竞品画面或补造事实数据。"]))
    if split_phrase:
        plan["warnings"].append("部分语音分段过长，人物切换采用安全的短镜头；完整原句和字幕时间轴保持不变。")
    plan["cameo_duration"] = cameo_duration
    plan["reference_duration"] = reference
    checkpoint = directory / "prepared.json"
    result = {"plan": plan, "full_audio_path": str(full_audio), "compact_audio_path": str(compact_audio),
              "cameo_ranges": ranges, "cameo_duration": cameo_duration, "reference_duration": reference,
              "duration": duration, "directory": str(directory), "sample_rate": _SAMPLE_RATE,
              "source_audio_path": source["path"], "source_segments": copy.deepcopy(segments),
              "checkpoint_path": str(checkpoint), "state": "prepared"}
    checkpoint.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        progress(f"已安排 {len(ranges)} 处人物镜头，共 {cameo_duration:.2f} 秒，其余使用素材或信息卡", 100)
    return result


def attach_avatar(prepared, avatar_video_path):
    """Attach a newly generated compact avatar video without resetting offsets."""
    if not isinstance(prepared, dict) or not isinstance(prepared.get("plan"), dict):
        raise AvatarMixedError("缺少人物与图文计划，请重新准备分镜。")
    source = _probe(avatar_video_path, video=True)
    needed = _finite(prepared.get("cameo_duration"), "人物镜头时长")
    if source["duration"] + 0.02 < needed:
        raise AvatarMixedError("人物输出短于安排的镜头，无法完整播放且不能循环。请用选定的短配音重新生成人物。")
    plan = copy.deepcopy(prepared["plan"])
    for row in plan["segments"]:
        if row.get("avatar_cameo", row.get("media_type") == "video" and not row.get("media_path")):
            row["media_path"] = source["path"]
            row["no_loop"] = True
    plan["shots"] = plan["segments"]
    plan["avatar_video_path"] = source["path"]
    plan["avatar_duration"] = source["duration"]
    return plan
