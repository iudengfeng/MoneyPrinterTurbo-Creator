from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.services.creator import avatar, avatar_mixed_generation, composition, duix, extract, rendering, store


class CreatorAvatarMixedGenerationTest(unittest.TestCase):
    """Real PCM/card composition; only local neural lip inference is stubbed."""

    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory()
        cls.asset_root = Path(cls.assets.name)
        cls.ffmpeg = extract.ffmpeg_binary()
        cls.audio = cls.asset_root / "full.wav"
        cls.reference = cls.asset_root / "reference.mp4"
        for arguments in (
            ["-f", "lavfi", "-i", "sine=frequency=440:duration=100:sample_rate=44100", "-c:a", "pcm_s16le", str(cls.audio)],
            ["-f", "lavfi", "-i", "color=c=blue:s=160x240:r=25:d=24.4", "-an", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", str(cls.reference)],
        ):
            subprocess.run([cls.ffmpeg, "-v", "error", "-nostdin", "-y", *arguments],
                           check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "selected.wav"
        shutil.copy2(self.audio, self.source)
        self.env = patch.dict(os.environ, {"MPT_CREATOR_DATA": str(self.root / "creator"), "FFMPEG_BINARY": self.ffmpeg})
        self.env.start()
        self.options = patch.object(avatar, "list_options", return_value=[
            {"id": "duix:1", "name": "我的人物", "available": True, "video_path": str(self.reference)},
        ])
        self.options.start()
        self.segments = [{"start": index * 5, "end": (index + 1) * 5,
                          "text": f"第{index + 1}步，准备文案，选择声音，检查画面。"} for index in range(20)]
        self.real_compose = composition.compose

    def tearDown(self):
        self.options.stop()
        self.env.stop()
        self.temp.cleanup()

    def _compose_small(self, plan, audio_path, folder, **kwargs):
        plan["size"] = (180, 320)
        return self.real_compose(plan, audio_path, folder, **kwargs)

    def _native(self, audio_path, model_id, **kwargs):
        self.assertEqual(model_id, "duix:1")
        self.assertEqual(kwargs["source_narration_id"], "")
        self.assertNotIn("allow_reference_reuse", kwargs)
        self.assertNotEqual(Path(audio_path), self.source)
        duration = avatar._wav_duration(Path(audio_path))
        self.assertLessEqual(duration, 24)
        self.assertLess(duration, 100)
        ident = store.new_id()
        target = self.root / f"native-{ident}.mp4"
        subprocess.run([self.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                        f"testsrc2=s=160x240:r=25:d={duration:.8f}", "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(target)], check=True, capture_output=True, timeout=30)
        return store.save_record("avatar_jobs", ident, {"state": "done", "video_path": str(target),
                                  "duration": duration, "audio_path": str(audio_path), "reference_strategy": "continuous",
                                  "warnings": [], "reference_chunks": [{"reference_offset": 0}]})

    def test_full_narration_survives_real_composition_and_compact_job_is_internal(self):
        original = self.source.read_bytes()
        reference_before = self.reference.read_bytes()
        narration_id = "a" * 32
        store.save_record("narrations", narration_id, {"state": "done", "preview": False, "audio_path": str(self.source)})
        with patch.object(avatar, "generate", side_effect=self._native) as native, \
                patch.object(composition, "compose", side_effect=self._compose_small), \
                patch.object(extract, "extract_media", side_effect=AssertionError("segments must be reused")), \
                patch.object(duix, "generate_audio", side_effect=AssertionError("must not synthesize speech")):
            result = avatar_mixed_generation.generate(self.source, "duix:1", script="完整的一百秒口播。",
                                                     segments=self.segments, source_narration_id=narration_id,
                                                     output_dir=self.root / "workflow-visuals")
        native.assert_called_once()
        self.assertEqual(result["state"], "done")
        self.assertTrue(result["mixed"])
        self.assertEqual(result["source_kind"], "avatar_mixed")
        self.assertFalse(result["subtitles_burned"])
        self.assertEqual(result["source_narration_id"], narration_id)
        self.assertAlmostEqual(result["duration"], 100, places=5)
        self.assertAlmostEqual(rendering.probe_source(result["video_path"])["duration"], 100, delta=0.08)
        self.assertLessEqual(result["cameo_duration"], 24)
        self.assertGreater(result["cameo_duration"], 10)
        self.assertEqual(result["clean_video_path"], result["video_path"])
        self.assertNotEqual(result["video_path"], result["native_avatar_video_path"])
        self.assertTrue(Path(result["video_path"]).is_relative_to(self.root / "workflow-visuals"))
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.reference.read_bytes(), reference_before)
        with wave.open(str(self.source), "rb") as original_audio, wave.open(result["full_audio_path"], "rb") as full_audio:
            self.assertEqual(full_audio.readframes(full_audio.getnframes()), original_audio.readframes(original_audio.getnframes()))
        prepared = json.loads(Path(result["prepared_path"]).read_text("utf-8"))
        self.assertEqual(prepared["cameo_ranges"], result["cameo_ranges"])
        self.assertTrue(Path(result["plan_path"]).is_file())
        self.assertEqual(result["plan"]["segments"][0]["start"], 0)
        self.assertEqual(result["plan"]["segments"][-1]["end"], 100)
        child = store.get_record("avatar_jobs", result["native_avatar_job_id"])
        self.assertTrue(child["internal"])
        self.assertEqual(child["internal_parent_id"], result["id"])
        self.assertEqual([row["id"] for row in avatar.list_jobs()], [result["id"]])

    def test_missing_segments_run_asr_once_against_complete_source(self):
        with patch.object(extract, "extract_media", return_value={"id": "recognition", "segments": self.segments}) as recognition, \
                patch.object(avatar, "generate", side_effect=self._native), \
                patch.object(composition, "compose", side_effect=self._compose_small), \
                patch.object(duix, "generate_audio", side_effect=AssertionError("must not synthesize speech")):
            result = avatar_mixed_generation.generate(self.source, "duix:1", script="整条口播，识别一次。")
        recognition.assert_called_once()
        self.assertEqual(avatar._wav_duration(Path(recognition.call_args.args[0])), 100)
        self.assertEqual(result["source_transcription_id"], "recognition")
        self.assertAlmostEqual(rendering.probe_source(result["video_path"])["duration"], 100, delta=0.08)

    def test_failed_visual_composition_preserves_preparation_and_native_checkpoint(self):
        with patch.object(avatar, "generate", side_effect=self._native), \
                patch.object(composition, "compose", side_effect=RuntimeError("画面合成失败")), \
                patch.object(duix, "generate_audio", side_effect=AssertionError("must not synthesize speech")):
            with self.assertRaisesRegex(RuntimeError, "画面合成失败"):
                avatar_mixed_generation.generate(self.source, "duix:1", segments=self.segments)
        public = avatar.list_jobs()
        self.assertEqual(len(public), 1)
        result = public[0]
        self.assertEqual(result["state"], "failed")
        self.assertTrue(Path(result["original_audio_path"]).is_file())
        self.assertTrue(Path(result["prepared_path"]).is_file())
        self.assertTrue(Path(result["compact_audio_path"]).is_file())
        self.assertTrue(Path(result["native_avatar_video_path"]).is_file())
        self.assertNotIn("video_path", result)
        self.assertTrue(store.get_record("avatar_jobs", result["native_avatar_job_id"])["internal"])

    def test_wrong_or_preview_narration_is_rejected_before_asr_or_native(self):
        narration_id = "a" * 32
        for metadata in ({"state": "done", "preview": True, "audio_path": str(self.source)},
                         {"state": "done", "preview": False, "audio_path": str(self.root / "another.wav")}):
            store.save_record("narrations", narration_id, metadata)
            with patch.object(extract, "extract_media") as recognition, patch.object(avatar, "generate") as native:
                with self.assertRaisesRegex(ValueError, "正式配音"):
                    avatar_mixed_generation.generate(self.source, "duix:1", source_narration_id=narration_id)
            recognition.assert_not_called()
            native.assert_not_called()
        self.assertFalse(avatar.list_jobs())


if __name__ == "__main__":
    unittest.main()
