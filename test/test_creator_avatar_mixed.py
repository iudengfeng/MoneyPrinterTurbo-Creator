from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from app.services.creator import avatar_mixed, extract


class AvatarMixedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory()
        cls.root = Path(cls.assets.name)
        cls.audio = cls.root / "完整口播.wav"
        cls.duration = 99.78
        length = round(cls.duration * 44100)
        clock = np.arange(length) / 44100
        # A changing tone makes offsets observable instead of silently accepting
        # repeated identical audio or a placeholder signal from another range.
        values = np.round(np.sin(2 * math.pi * (170 * clock + 0.7 * clock**2)) * 12000).astype("<i2")
        with wave.open(str(cls.audio), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(44100)
            writer.writeframes(values.tobytes())
        cls.entries = [{"start": index * 4.0, "end": min(cls.duration - 0.04, (index + 1) * 4.0 - 0.1),
                        "text": f"第 {index + 1} 段口播说明。"} for index in range(25)]
        cls.ffmpeg = extract.ffmpeg_binary()
        cls.avatar = cls.root / "短人物.mp4"
        subprocess.run([cls.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                        "testsrc2=s=90x160:r=30:d=24.4", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", str(cls.avatar)], check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _prepare(self, **changes):
        return avatar_mixed.prepare("完整的知识讲解文案。", changes.get("segments", self.entries),
                                    changes.get("audio_path", self.audio), changes.get("reference_duration", 24.4),
                                    self.output, aspect=changes.get("aspect", "9:16"),
                                    materials=changes.get("materials"), video_brief=changes.get("video_brief"))

    @staticmethod
    def _pcm(path):
        with wave.open(str(path), "rb") as reader:
            return reader.getparams(), reader.readframes(reader.getnframes())

    def test_three_disjoint_cameos_fit_reference_and_keep_full_global_coverage(self):
        result = self._prepare()
        ranges = result["cameo_ranges"]
        self.assertEqual(len(ranges), 3)
        self.assertEqual(ranges[0]["start"], 0)
        self.assertGreater(ranges[1]["start"], self.duration * 0.3)
        self.assertLess(ranges[1]["end"], self.duration * 0.7)
        self.assertGreater(ranges[-1]["start"], self.duration * 0.7)
        self.assertGreater(result["cameo_duration"], 20)
        self.assertLessEqual(result["cameo_duration"], min(24, self.duration * 0.3, 24.4 - 1 / 30))
        for previous, current in zip(ranges, ranges[1:]):
            self.assertLess(previous["end"], current["start"])
            self.assertAlmostEqual(previous["media_end"], current["media_start"], places=7)
        self.assertAlmostEqual(ranges[-1]["media_end"], result["cameo_duration"])
        plan = result["plan"]
        self.assertEqual(plan["kind"], "knowledge")
        self.assertEqual(plan["source_kind"], "avatar_mixed")
        self.assertEqual(plan["segments"][0]["start"], 0)
        self.assertAlmostEqual(plan["segments"][-1]["end"], self.duration)
        for previous, current in zip(plan["segments"], plan["segments"][1:]):
            self.assertAlmostEqual(previous["end"], current["start"])
        self.assertTrue(any(row["media_type"] == "card" for row in plan["segments"]))
        self.assertTrue(all(row["source_cue_text"] in {cue["text"] for cue in self.entries} for row in plan["segments"]))
        json.dumps(result, ensure_ascii=False)

    def test_compact_pcm_is_exact_selected_original_audio_without_added_silence(self):
        original_bytes = hashlib.sha256(self.audio.read_bytes()).digest()
        result = self._prepare()
        full_params, full = self._pcm(result["full_audio_path"])
        compact_params, compact = self._pcm(result["compact_audio_path"])
        self.assertEqual((full_params.nchannels, full_params.sampwidth, full_params.framerate), (1, 2, 44100))
        self.assertEqual(compact_params[:3], full_params[:3])
        expected = b"".join(full[row["start_sample"] * 2:row["end_sample"] * 2] for row in result["cameo_ranges"])
        self.assertEqual(compact, expected)
        self.assertEqual(compact_params.nframes, sum(row["end_sample"] - row["start_sample"] for row in result["cameo_ranges"]))
        self.assertGreater(np.std(np.frombuffer(compact, dtype="<i2")), 5000)
        self.assertEqual(self._pcm(self.audio)[1], full)
        self.assertEqual(hashlib.sha256(self.audio.read_bytes()).digest(), original_bytes)
        self.assertTrue(Path(result["directory"]).is_relative_to(self.output))

    def test_attachment_uses_sequential_offsets_and_never_restarts_avatar(self):
        result = self._prepare()
        original = json.dumps(result, sort_keys=True)
        attached = avatar_mixed.attach_avatar(result, self.avatar)
        shots = [row for row in attached["segments"] if row["media_type"] == "video"]
        self.assertGreater(len(shots), 2)
        self.assertEqual(shots[0]["media_start"], 0)
        for previous, current in zip(shots, shots[1:]):
            self.assertAlmostEqual(previous["media_start"] + previous["end"] - previous["start"], current["media_start"], places=7)
        self.assertTrue(all(row["no_loop"] for row in shots))
        self.assertTrue(all(abs(row["media_start"] * 30 - round(row["media_start"] * 30)) < 1e-6 for row in shots))
        self.assertTrue(all(row["media_path"] == str(self.avatar.resolve()) for row in shots))
        self.assertAlmostEqual(shots[-1]["media_start"] + shots[-1]["end"] - shots[-1]["start"], result["cameo_duration"])
        self.assertEqual(json.dumps(result, sort_keys=True), original)
        self.assertEqual(attached["duration"], result["duration"])

    def test_real_material_between_cameos_is_not_replaced_by_avatar_attachment(self):
        entries = [dict(row) for row in self.entries]
        entries[12]["text"] = "先看价格表，再决定套餐是否合适。"
        material = self.output / "价格表.mp4"
        shutil.copy2(self.avatar, material)
        result = self._prepare(segments=entries, materials=[str(material)])
        inserted = [row for row in result["plan"]["segments"]
                    if row.get("media_path") == str(material.resolve()) and not row.get("avatar_cameo")]
        self.assertTrue(inserted)
        attached = avatar_mixed.attach_avatar(result, self.avatar)
        surviving = [row for row in attached["segments"] if row.get("media_path") == str(material.resolve())]
        self.assertEqual(len(surviving), len(inserted))
        self.assertTrue(all(row["no_loop"] for row in surviving))
        self.assertTrue(all(row.get("media_start", 0) >= 0 for row in surviving))
        cameos = [row for row in attached["segments"] if row.get("avatar_cameo")]
        self.assertTrue(cameos)
        self.assertTrue(all(row["media_path"] == str(self.avatar.resolve()) for row in cameos))

    def test_video_audio_input_is_decoded_without_overwriting_source(self):
        source = self.output / "带音轨的原视频.mp4"
        subprocess.run([self.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(self.avatar),
                        "-i", str(self.audio), "-map", "0:v:0", "-map", "1:a:0", "-t", "8", "-c:v", "copy",
                        "-c:a", "aac", str(source)], check=True, capture_output=True, timeout=30)
        before = hashlib.sha256(source.read_bytes()).digest()
        result = self._prepare(audio_path=source, segments=self.entries[:2])
        self.assertAlmostEqual(result["duration"], 8, delta=0.04)
        params, _ = self._pcm(result["full_audio_path"])
        self.assertEqual((params.nchannels, params.sampwidth, params.framerate), (1, 2, 44100))
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)
        persisted = json.loads(Path(result["checkpoint_path"]).read_text("utf-8"))
        self.assertEqual(persisted, result)

    def test_short_reference_fits_safety_budget_even_with_long_asr_phrases(self):
        entries = [{"start": 0, "end": 30, "text": "这是一段很长的口播。"},
                   {"start": 32, "end": 70, "text": "中间的完整内容。"},
                   {"start": 72, "end": 99.7, "text": "最后的内容。"}]
        result = self._prepare(reference_duration=3.02, segments=entries)
        self.assertLessEqual(result["cameo_duration"], 3.02 - 1 / 30)
        self.assertEqual(len(result["cameo_ranges"]), 2)
        self.assertTrue(any("分段过长" in warning for warning in result["plan"]["warnings"]))
        for previous, current in zip(result["cameo_ranges"], result["cameo_ranges"][1:]):
            self.assertLess(previous["end"], current["start"])

    def test_too_short_reference_is_rejected_with_an_actionable_message(self):
        for value in (0, 1.9, -10):
            with self.subTest(value=value), self.assertRaisesRegex(avatar_mixed.AvatarMixedError, "不足 2 秒.*上传更长"):
                self._prepare(reference_duration=value)
        for value in (None, "invalid", float("nan")):
            with self.subTest(value=value), self.assertRaisesRegex(avatar_mixed.AvatarMixedError, "有效秒数"):
                self._prepare(reference_duration=value)

    def test_invalid_audio_and_short_avatar_are_rejected_without_looping(self):
        broken = self.output / "not-audio.mp4"
        broken.write_text("not a media file", encoding="utf-8")
        with self.assertRaisesRegex(avatar_mixed.AvatarMixedError, "有效文件"):
            self._prepare(audio_path=broken)
        result = self._prepare()
        short = self.output / "too-short.mp4"
        subprocess.run([self.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(self.avatar),
                        "-t", "0.5", "-an", "-c:v", "copy", str(short)], check=True, capture_output=True, timeout=30)
        with self.assertRaisesRegex(avatar_mixed.AvatarMixedError, "短于.*不能循环"):
            avatar_mixed.attach_avatar(result, short)

    def test_each_preparation_is_unique_and_source_timestamps_remain_unchanged(self):
        before = json.dumps(self.entries, sort_keys=True)
        first = self._prepare()
        second = self._prepare()
        self.assertNotEqual(first["directory"], second["directory"])
        self.assertTrue(Path(first["compact_audio_path"]).is_file())
        self.assertEqual(json.dumps(self.entries, sort_keys=True), before)
        original_by_text = {row["text"]: row for row in self.entries}
        for row in first["plan"]["segments"]:
            source = original_by_text[row["text"]]
            self.assertEqual(row["source_cue_start"], source["start"])
            self.assertEqual(row["source_cue_end"], source["end"])


if __name__ == "__main__":
    unittest.main()
