"""Generate one full-length avatar/card video without repeating body footage.

The short native avatar is an internal intermediate. The public avatar job
always represents the full narration and keeps its original global timeline.
"""
from __future__ import annotations

import re
import shutil
from concurrent.futures import CancelledError
from pathlib import Path

from . import avatar, avatar_mixed, composition, duix, extract, store


def _phase(progress, start, end):
    if progress is None:
        return None

    def report(message, percent=None):
        return progress(message, None if percent is None else start + max(0, min(100, float(percent))) * (end - start) / 100)

    return report


def _write_checkpoint(path, data):
    avatar._atomic_json(path, data)


def generate(audio_path, model_id, script="", aspect="9:16", progress=None, source_narration_id="", *,
             segments=None, output_dir=None, materials=None, video_brief=None) -> dict:
    """Use selected original PCM for lips, retaining the complete final voice.

    Existing ASR segments can be passed by the workflow, avoiding a second
    recognition. Preparation and native output paths are retained on failure
    so a failed visual composition does not erase completed local work.
    """
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
    source = Path(str(audio_path or "")).expanduser().resolve()
    if source_narration_id:
        narration = store.get_record("narrations", source_narration_id)
        if (not narration or narration.get("state") != "done" or narration.get("preview")
                or Path(str(narration.get("audio_path") or "")).resolve() != source):
            raise ValueError("所选正式配音已变化或不存在，请重新选择完整配音版本。")
    if (source.suffix.lower() not in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
            or not source.is_file() or not 0 < source.stat().st_size <= avatar.MAX_AUDIO_BYTES):
        raise ValueError("请提供有效完整配音，支持 WAV、MP3、M4A、AAC、FLAC、OGG，大小不超过 128MB。")
    if source.suffix.lower() == ".wav":
        avatar._wav_duration(source)
    info = extract.probe_media(source)
    if float(info.get("duration") or 0) > avatar.MAX_AUDIO_SECONDS:
        raise ValueError(f"数字人配音最多 {avatar.MAX_AUDIO_SECONDS} 秒，请先分段。")
    option = next((row for row in avatar.list_options() if row["id"] == model_id), None)
    if not option or not option.get("available"):
        raise ValueError("所选人物不存在或参考视频已缺失，请刷新形象列表。")
    reference = Path(option["video_path"])
    avatar._video_info(reference)
    reference_duration = avatar._reference_frames(reference) / avatar._REFERENCE_FPS
    ident = store.new_id()
    folder = ((Path(output_dir).expanduser().resolve() / f"avatar-mixed-generation-{ident}") if output_dir is not None
              else store.data_root() / "avatars" / ident)
    folder.mkdir(parents=True, exist_ok=False)
    record = {"state": "preparing", "model_id": model_id, "model_name": option["name"], "script": script,
              "aspect": aspect, "mode": "mixed", "avatar_mode": "mixed", "mixed": True,
              "source_kind": "avatar_mixed", "source_audio_name": source.name,
              "source_narration_id": source_narration_id, "reference_duration": reference_duration,
              "source_reference_path": str(reference), "generation_dir": str(folder)}
    store.save_record("avatar_jobs", ident, record)
    try:
        original = folder / ("original" + source.suffix.lower())
        shutil.copy2(source, original)
        store.update_record("avatar_jobs", ident, {"original_audio_path": str(original)})
        if segments is None:
            duix._report(progress, "识别完整口播时间轴", 2)
            transcription = extract.extract_media(original, language="zh", model_size="small", progress=_phase(progress, 2, 16))
            segments = transcription.get("segments") or []
            store.update_record("avatar_jobs", ident, {"source_transcription_id": transcription.get("id", ""),
                                "source_subtitle_path": transcription.get("srt_path", "")})
        if not script and isinstance(segments, list):
            script = "".join(str(row.get("text", "")) for row in segments if isinstance(row, dict)).strip()
            store.update_record("avatar_jobs", ident, {"script": script})
        prepared = avatar_mixed.prepare(script, segments, original, reference_duration, folder,
                                        aspect=aspect, progress=_phase(progress, 16, 24),
                                        materials=materials, video_brief=video_brief)
        prepared_path = folder / "prepared.json"
        _write_checkpoint(prepared_path, prepared)
        prepared_metadata = {"prepared_path": str(prepared_path), "full_audio_path": prepared["full_audio_path"],
                             "audio_path": prepared["full_audio_path"], "compact_audio_path": prepared["compact_audio_path"],
                             "duration": prepared["duration"], "cameo_duration": prepared["cameo_duration"],
                             "cameo_ranges": prepared["cameo_ranges"], "state": "running"}
        store.update_record("avatar_jobs", ident, prepared_metadata)
        # The compact PCM is an internal derivative, not the selected formal
        # narration version. Native generation must validate its own path and
        # use a continuous reference without enabling action reuse.
        compact = avatar.generate(prepared["compact_audio_path"], model_id, script=script, aspect=aspect,
                                  progress=_phase(progress, 24, 72), source_narration_id="")
        compact_id = compact.get("id", "")
        if compact_id and store.get_record("avatar_jobs", compact_id):
            store.update_record("avatar_jobs", compact_id, {"internal": True, "internal_parent_id": ident,
                                "source_kind": "avatar_mixed_compact"})
        store.update_record("avatar_jobs", ident, {"native_avatar_job_id": compact_id,
                            "native_avatar_video_path": compact["video_path"],
                            "reference_strategy": compact.get("reference_strategy", "continuous"),
                            "reference_chunks": compact.get("reference_chunks", [])})
        plan = avatar_mixed.attach_avatar(prepared, compact["video_path"])
        plan_path = folder / "plan.json"
        _write_checkpoint(plan_path, plan)
        store.update_record("avatar_jobs", ident, {"plan_path": str(plan_path)})
        rendered = composition.compose(plan, prepared["full_audio_path"], folder, progress=_phase(progress, 72, 98))
        warnings = list(dict.fromkeys([*plan.get("warnings", []), *compact.get("warnings", []), *rendered.get("warnings", [])]))
        result = store.update_record("avatar_jobs", ident, {"state": "done", "video_path": rendered["video_path"],
                                     "clean_video_path": rendered["video_path"], "duration": prepared["duration"],
                                     "width": rendered["width"], "height": rendered["height"],
                                     "subtitles_burned": False, "composition_dir": rendered.get("composition_dir"),
                                     "warnings": warnings, "plan": plan})
        duix._report(progress, "人物与图文视频已完成，完整口播播放一次", 100)
        return result
    except BaseException as exc:
        store.update_record("avatar_jobs", ident, {"state": "cancelled" if isinstance(exc, (CancelledError, KeyboardInterrupt)) else "failed",
                            "error": str(exc)[:1200]})
        raise
