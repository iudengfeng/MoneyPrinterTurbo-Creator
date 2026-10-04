from __future__ import annotations

import hashlib
import io
import math
import os
import struct
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.services.creator import duix, extract, narration, store, voices


def tone_bytes(seconds=2.0):
    result = io.BytesIO()
    with wave.open(result, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"".join(struct.pack("<h", round(6000 * math.sin(2 * math.pi * 330 * i / 24000)))
                                  for i in range(round(24000 * seconds))))
    return result.getvalue()


class CreatorNarrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"MPT_CREATOR_DATA": str(self.root / "creator")})
        self.env.start()
        self.profiles = patch.object(duix, "list_profiles", return_value={"voices": [{"id": 7, "name": "我的本机声音"}]})
        self.profiles.start()

    def tearDown(self):
        self.profiles.stop()
        self.env.stop()
        self.temp.cleanup()

    def ffmpeg_or_skip(self):
        try:
            return extract.ffmpeg_binary()
        except extract.MediaExtractError:
            self.skipTest("FFmpeg 未安装，真实音频变速验证需要 FFmpeg")

    def provider(self, fail=False):
        provider = types.ModuleType("app.services.voice")
        provider.prepare_voxcpm_reference_audio = lambda data, suffix: data
        calls = []

        def tts(**kwargs):
            calls.append(kwargs)
            if fail:
                return None
            Path(kwargs["voice_file"]).write_bytes(tone_bytes())
            return object()

        provider.tts = tts
        return provider, calls

    def test_voice_catalogue_keeps_real_sources_and_does_not_offer_pending_clone(self):
        store.save_record("voices", "a" * 32, {"name": "云端样音", "provider": "voxcpm", "sample_path": "private.wav"})
        store.save_record("voice_samples", "b" * 32, {"name": "待训练声音", "state": "sample_saved"})
        options = narration.list_options()
        self.assertTrue(any(row["id"] == "duix:7" for row in options))
        self.assertTrue(any(row["id"] == "saved:" + "a" * 32 for row in options))
        self.assertFalse(any(row["name"] == "待训练声音" for row in options))
        self.assertTrue(all(row["supports_emotion"] is False for row in options))
        self.assertNotIn("private.wav", str(options))
        with patch.object(duix, "list_profiles", side_effect=RuntimeError("数据库离线")):
            self.assertTrue(any(row["provider"] == "edge" for row in narration.list_options()))

    def test_invalid_parameters_fail_before_synthesis_or_persistence(self):
        with patch.object(narration, "_render_chunk") as render:
            for speed in (True, 0.79, 1.21, float("nan"), float("inf"), "bad"):
                with self.assertRaises(ValueError):
                    narration.generate("测试", "edge:zh-CN-XiaoxiaoNeural", speed=speed)
            for text in ("", "a" * 6001, "含有\x00控制字符"):
                with self.assertRaises(ValueError):
                    narration.generate(text, "edge:zh-CN-XiaoxiaoNeural")
            with self.assertRaisesRegex(ValueError, "仅支持自然"):
                narration.generate("测试", "edge:zh-CN-XiaoxiaoNeural", emotion="开心")
            with self.assertRaisesRegex(ValueError, "不存在"):
                narration.generate("测试", "../voice")
            render.assert_not_called()
        self.assertEqual(store.list_records("narrations"), [])

    def test_chunks_preserve_every_character_for_full_length_script(self):
        script = ("这是完整长文案，需要保留句号、换行和所有内容。\n" * 100)[:2400]
        chunks = narration._chunks(script)
        self.assertEqual("".join(chunks), script)
        self.assertTrue(all(0 < len(chunk) <= 600 for chunk in chunks))
        self.assertEqual("".join(narration._chunks("字" * 6000)), "字" * 6000)

    def test_actual_atempo_changes_duration_without_overwriting_source(self):
        self.ffmpeg_or_skip()
        source = self.root / "original.wav"
        source.write_bytes(tone_bytes())
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        for speed in (0.8, 1.0, 1.2):
            target = self.root / f"version-{speed}.wav"
            duration = narration._combine([source], target, speed)
            self.assertAlmostEqual(duration, 2 / speed, delta=0.08)
            self.assertEqual(narration._duration(target), duration)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
        with self.assertRaisesRegex(ValueError, "不能覆盖"):
            narration._combine([source], source, 1.0)

    def test_long_complete_script_synthesizes_all_chunks_then_persists_playable_audio(self):
        self.ffmpeg_or_skip()
        provider, calls = self.provider()
        script = ("这是长文案，用于完整配音。" * 120).strip()
        updates = []
        with patch.dict(sys.modules, {"app.services.voice": provider}), patch("app.services.voice", provider, create=True):
            result = narration.generate(script, "edge:zh-CN-XiaoxiaoNeural", speed=1.2,
                                        progress=lambda message, percent: updates.append((message, percent)))
        self.assertEqual(result["text"], script)
        self.assertEqual("".join(call["text"] for call in calls), script)
        self.assertTrue(all(call["voice_rate"] == 1.0 for call in calls))
        self.assertEqual(result["chunk_count"], len(calls))
        self.assertAlmostEqual(result["duration"], 2 * len(calls) / 1.2, delta=0.08)
        self.assertEqual(result["state"], "done")
        self.assertTrue(Path(result["audio_path"]).is_file())
        self.assertEqual(Path(result["txt_path"]).read_text("utf-8"), script)
        self.assertEqual(narration.list_narrations()[0]["id"], result["id"])
        self.assertEqual(updates[-1], ("配音完成", 100))

    def test_failed_provider_keeps_previous_successful_result_and_no_fake_audio(self):
        self.ffmpeg_or_skip()
        provider, _ = self.provider()
        with patch.dict(sys.modules, {"app.services.voice": provider}), patch("app.services.voice", provider, create=True):
            first = narration.generate("上一个成功结果。", "edge:zh-CN-XiaoxiaoNeural")
        original = Path(first["audio_path"]).read_bytes()
        failed_provider, _ = self.provider(fail=True)
        with patch.dict(sys.modules, {"app.services.voice": failed_provider}), patch("app.services.voice", failed_provider, create=True):
            with self.assertRaisesRegex(RuntimeError, "Edge 配音未生成"):
                narration.generate("这个调用失败。", "edge:zh-CN-XiaoxiaoNeural")
        self.assertEqual(Path(first["audio_path"]).read_bytes(), original)
        self.assertEqual(len(narration.list_narrations()), 1)
        failed = next(row for row in store.list_records("narrations") if row["state"] == "failed")
        self.assertNotIn("audio_path", failed)

    def test_saved_clone_and_duix_use_existing_service_for_complete_script(self):
        self.ffmpeg_or_skip()
        source = self.root / "generated-source.wav"
        source.write_bytes(tone_bytes())
        saved_id = "c" * 32
        store.save_record("voices", saved_id, {"name": "保存的声音", "provider": "duix", "duix_voice_id": 7})
        script = "完整文案。" * 150
        with patch.object(voices, "preview_voice", return_value=str(source)) as preview:
            result = narration.generate(script, f"saved:{saved_id}")
        self.assertGreater(preview.call_count, 1)
        self.assertEqual("".join(call.args[1] for call in preview.call_args_list), script)
        self.assertTrue(all(len(call.args[1]) <= 600 for call in preview.call_args_list))
        self.assertNotEqual(result["audio_path"], str(source))
        with patch.object(duix, "generate_audio", return_value={"audio_path": str(source)}) as generate:
            direct = narration.generate("本机已有音色。", "duix:7")
        self.assertEqual(generate.call_args.args[:2], ("本机已有音色。", 7))
        self.assertEqual(direct["provider"], "duix")

    def test_corrupt_provider_audio_and_truncated_wav_are_rejected(self):
        self.ffmpeg_or_skip()
        source = self.root / "corrupt.wav"
        source.write_bytes(b"<html>not audio</html>")
        with self.assertRaises(RuntimeError):
            narration._combine([source], self.root / "invalid-result.wav", 1)
        self.assertFalse((self.root / "invalid-result.wav").exists())
        self.assertFalse((self.root / ".narration.partial.wav").exists())
        source.write_bytes(tone_bytes()[:-20])
        with self.assertRaises(RuntimeError):
            narration._duration(source)

    def test_uploaded_sample_persists_but_is_not_a_trained_local_voice(self):
        provider, _ = self.provider()
        source = self.root / "sample.wav"
        source.write_bytes(tone_bytes(12))
        with patch.dict(sys.modules, {"app.services.voice": provider}), patch("app.services.voice", provider, create=True):
            result = narration.save_sample("自己的声音", source, "参考文案")
        source.unlink()
        self.assertEqual(result["state"], "sample_saved")
        self.assertEqual(result["duration"], 12)
        self.assertTrue(Path(result["sample_path"]).is_file())
        self.assertIn("尚不能", result["message"])
        self.assertEqual(narration.list_samples()[0]["name"], "自己的声音")
        self.assertFalse(any(row["name"] == "自己的声音" for row in narration.list_options()))
        with patch.object(voices, "save_voice", return_value={"provider": "voxcpm"}) as save:
            cloud = narration.save_sample("云端声音", "sample.wav", "参考文案", mode="voxcpm")
        self.assertEqual(cloud["provider"], "voxcpm")
        save.assert_called_once_with("云端声音", "sample.wav", transcript="参考文案", provider="voxcpm")


if __name__ == "__main__":
    unittest.main()
