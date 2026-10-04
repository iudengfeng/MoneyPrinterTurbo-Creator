"""Persistent voice assets, with cloud cloning and existing local Duix voices."""
from __future__ import annotations

import re
import shutil
import wave
from pathlib import Path

from . import duix, store

_MAX_SAMPLE_BYTES = 20 * 1024 * 1024


def _voice_id(ident):
    if not re.fullmatch(r"[a-f0-9]{32}", str(ident)):
        raise ValueError("声音编号无效。")
    return str(ident)


def _wav_valid(path):
    try:
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
            expected = frames * audio.getnchannels() * audio.getsampwidth()
            if frames <= 0 or expected > 128 * 1024 * 1024 or len(audio.readframes(frames)) != expected:
                raise wave.Error("invalid PCM")
    except (OSError, wave.Error):
        raise ValueError("声音文件无效或未生成，请换一段能正常播放的音频。") from None


def save_voice(name, audio_path, transcript="", provider="voxcpm", duix_voice_id=None) -> dict:
    name = str(name or "").strip()
    if not name or len(name) > 80 or re.search(r"[\x00-\x1f]", name):
        raise ValueError("请输入 1～80 字的声音名称。")
    if provider not in {"voxcpm", "duix"}:
        raise ValueError("声音支持 VoxCPM 克隆和 Duix 本机已有音色。")
    transcript = str(transcript or "").strip()
    if len(transcript) > 5000:
        raise ValueError("参考音频文案过长，请使用简短样音。")
    if provider == "duix":
        duix_voice_id = duix._positive_id(duix_voice_id)
        if not any(row["id"] == duix_voice_id for row in duix.list_profiles()["voices"]):
            raise ValueError("所选 Duix 本机音色不存在。")
        if audio_path:
            raise ValueError("Duix 接入使用其已有音色；新增本机音色请先在 Duix 中创建。上传样音请选择 VoxCPM。")
    ident = store.new_id()
    folder = store.data_root() / "voices" / ident
    sample = None
    try:
        if provider == "voxcpm":
            source = Path(str(audio_path or "")).expanduser().resolve()
            if not source.is_file() or source.stat().st_size > _MAX_SAMPLE_BYTES:
                raise ValueError("请选择有效样音文件，大小不超过 20MB。")
            from app.services import voice

            normalized = voice.prepare_voxcpm_reference_audio(source.read_bytes(), source.suffix)
            folder.mkdir(parents=True, exist_ok=True)
            sample = folder / "reference.wav"
            sample.write_bytes(normalized)
            _wav_valid(sample)
        return store.save_record("voices", ident, {
            "name": name, "provider": provider, "sample_path": str(sample) if sample else "",
            "transcript": transcript, "duix_voice_id": duix_voice_id if provider == "duix" else None,
        })
    except Exception:
        if folder.exists():
            shutil.rmtree(folder)
        raise


def list_voices() -> list:
    return store.list_records("voices")


def preview_voice(ident, text, progress=None) -> str:
    ident = _voice_id(ident)
    profile = store.get_record("voices", ident)
    if not profile:
        raise ValueError("声音档案不存在，请刷新声音列表。")
    text = str(text or "").strip()
    if not text or len(text) > 600:
        raise ValueError("请输入 1～600 字试听文案。")
    if profile["provider"] == "duix":
        return duix.generate_audio(text, profile["duix_voice_id"], progress)["audio_path"]
    from app.config import config
    from app.services import voice

    if not config.voxcpm.get("api_key") or not config.voxcpm.get("model_id"):
        raise ValueError("VoxCPM 声音克隆需要先在配音设置中填写 API Key 和模型 ID；也可选择已有 Duix 本机音色。")
    sample_root = (store.data_root() / "voices" / ident).resolve()
    sample = Path(profile["sample_path"]).resolve()
    if not sample.is_relative_to(sample_root) or not sample.is_file():
        raise ValueError("该声音的样音文件已丢失，请重新保存。")
    _wav_valid(sample)
    folder = store.data_root() / "voice_previews" / store.new_id()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "preview.wav"
    duix._report(progress, "生成声音克隆试听", 10)
    reference = sample.read_bytes()
    result = voice.voxcpm_tts(
        text=text, voice_id=config.voxcpm.get("voice_id") or voice.VOXCPM_DEFAULT_VOICE,
        voice_file=str(target), reference_audio=reference,
        prompt_audio=reference if profile.get("transcript") else None,
        prompt_text=profile.get("transcript", ""),
    )
    if result is None or not target.is_file():
        target.unlink(missing_ok=True)
        raise RuntimeError("VoxCPM 声音克隆未生成音频，请检查配音配置、网络和账户额度。")
    _wav_valid(target)
    duix._report(progress, "声音试听已完成", 100)
    return str(target)
