from __future__ import annotations

import io
import math
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from app.services.creator import composition, extract, planning, rendering


class CreatorCompositionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory()
        cls.asset_root = Path(cls.assets.name)
        cls.ffmpeg = extract.ffmpeg_binary()
        cls.audio = cls.asset_root / "voice.wav"
        cls.video = cls.asset_root / "实拍绿景.mp4"
        cls.short_video = cls.asset_root / "短素材.mp4"
        cls.changing_video = cls.asset_root / "前红后蓝.mp4"
        cls.unequal_video = cls.asset_root / "画面短于原声.mp4"
        cls.picture = cls.asset_root / "咖啡.png"
        Image.new("RGB", (120, 80), (230, 80, 20)).save(cls.picture)
        for args in (
            ["-f", "lavfi", "-i", "sine=frequency=660:duration=1.234:sample_rate=48000", str(cls.audio)],
            ["-f", "lavfi", "-i", "color=c=green:s=160x240:r=30:d=1.1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.video)],
            ["-f", "lavfi", "-i", "color=c=green:s=160x240:r=30:d=0.3", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.short_video)],
            ["-f", "lavfi", "-i", "color=c=red:s=160x240:r=30:d=1", "-f", "lavfi", "-i", "color=c=blue:s=160x240:r=30:d=1",
             "-f", "lavfi", "-i", "sine=frequency=1000:duration=2:sample_rate=48000",
             "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a:0",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.changing_video)],
            ["-f", "lavfi", "-i", "color=c=green:s=160x240:r=30:d=0.3", "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.unequal_video)],
        ):
            subprocess.run([cls.ffmpeg, "-v", "error", "-nostdin", "-y", *args], check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.entries = [{"start": 0.08, "end": 0.44, "text": "咖啡休息。"},
                        {"start": 0.57, "end": 1.13, "text": "看看窗外的绿景。"}]

    def tearDown(self):
        self.temp.cleanup()

    def _plan(self, *, materials=None, kind="knowledge", duration=1.234):
        result = planning.build_plan("咖啡和绿景", self.entries, materials or [], kind=kind, duration=duration)
        result["size"] = (180, 320)
        return result

    def _frame(self, path, seconds):
        result = subprocess.run([self.ffmpeg, "-v", "error", "-ss", str(seconds), "-i", str(path),
                                 "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                                capture_output=True, check=True, timeout=30)
        return Image.open(io.BytesIO(result.stdout)).convert("RGB")

    def test_foreign_price_cannot_replace_card_but_real_cue_excerpt_can(self):
        plan = self._plan()
        plan["segments"][0].update(card_text="竞品双人餐99元", card_title="竞品北京门店")
        plan["segments"][1].update(card_text="窗外的绿景", card_title="绿景")
        with patch.object(composition, "_information_card", wraps=composition._information_card) as cards:
            result = composition.compose(plan, self.audio, self.root)
        self.assertEqual(cards.call_args_list[0].args[0], "咖啡休息。")
        self.assertEqual(cards.call_args_list[0].kwargs["title"], "")
        self.assertEqual(cards.call_args_list[1].args[0], "窗外的绿景")
        self.assertEqual(cards.call_args_list[1].kwargs["title"], "绿景")
        self.assertTrue(rendering.probe_source(result["video_path"])["has_video"])

    def test_price_excerpt_keeps_decimal_and_number_boundaries(self):
        cue = "我们的套餐199元，价格以店内公示为准。"
        for value in ("99元", "1.99元", "19", "1990元"):
            with self.subTest(value=value):
                body, title = composition._card_content({"text": cue, "card_text": value, "card_title": value}, cue)
                self.assertEqual(body, cue)
                self.assertEqual(title, "")
        self.assertEqual(composition._card_content({"text": cue, "card_text": "199元"}, cue)[0], "199元")

    def test_chinese_information_cards_create_real_video_with_original_audio(self):
        result = composition.compose(self._plan(), self.audio, self.root)
        source = rendering.probe_source(result["video_path"])
        self.assertTrue(source["has_video"] and source["has_audio"])
        self.assertEqual((source["width"], source["height"]), (180, 320))
        self.assertAlmostEqual(source["duration"], 1.234, delta=0.08)
        frame = np.array(self._frame(result["video_path"], 0.2))
        # Card text produces actual bright glyphs, rather than a blank color video.
        self.assertGreater(np.sum(np.min(frame[80:230], axis=2) > 160), 25)
        pcm = subprocess.run([self.ffmpeg, "-v", "error", "-i", result["video_path"], "-map", "0:a:0",
                              "-ac", "1", "-ar", "48000", "-f", "f32le", "-"], capture_output=True, check=True, timeout=30)
        values = np.frombuffer(pcm.stdout, dtype=np.float32)[4800:24000].astype(float)
        clock = np.arange(len(values)) / 48000
        tone = abs(np.sum(values * np.exp(-2j * math.pi * 660 * clock))) * 2 / len(values)
        self.assertGreater(tone, 0.1)
        self.assertTrue(Path(result["video_path"]).is_relative_to(self.root))
        self.assertTrue(list(Path(result["composition_dir"]).glob("card-*.png")))

    def test_images_and_real_video_are_used_in_correct_scenes_without_overwriting_inputs(self):
        image_bytes, video_bytes = self.picture.read_bytes(), self.video.read_bytes()
        result = composition.compose(self._plan(materials=[self.picture, self.video]), self.audio, self.root)
        first = self._frame(result["video_path"], 0.25).getpixel((90, 160))
        second = self._frame(result["video_path"], 0.9).getpixel((90, 160))
        self.assertGreater(first[0], first[1] + 70)
        self.assertGreater(second[1], second[0] + 50)
        self.assertEqual(self.picture.read_bytes(), image_bytes)
        self.assertEqual(self.video.read_bytes(), video_bytes)
        self.assertFalse(any("循环" in text for text in result["warnings"]))

    def test_video_offsets_play_later_source_frames_without_repeating_or_using_original_audio(self):
        audio_bytes, video_bytes = self.audio.read_bytes(), self.changing_video.read_bytes()
        plan = self._plan()
        plan["source_kind"] = "avatar_mixed"
        for segment, offset in zip(plan["segments"], (0.1, 1.0)):
            segment.update(media_type="video", media_path=str(self.changing_video), media_start=offset)
        result = composition.compose(plan, self.audio, self.root)
        first = self._frame(result["video_path"], 0.25).getpixel((90, 160))
        second = self._frame(result["video_path"], 0.9).getpixel((90, 160))
        self.assertGreater(first[0], first[2] + 150)
        self.assertGreater(second[2], second[0] + 150)
        source = rendering.probe_source(result["video_path"])
        self.assertAlmostEqual(source["duration"], 1.234, delta=0.04)
        self.assertTrue(source["has_audio"])
        pcm = subprocess.run([self.ffmpeg, "-v", "error", "-i", result["video_path"], "-map", "0:a:0",
                              "-ac", "1", "-ar", "48000", "-f", "f32le", "-"], capture_output=True, check=True, timeout=30)
        values = np.frombuffer(pcm.stdout, dtype=np.float32)[4800:48000].astype(float)
        clock = np.arange(len(values)) / 48000
        voice_tone = abs(np.sum(values * np.exp(-2j * math.pi * 660 * clock))) * 2 / len(values)
        source_tone = abs(np.sum(values * np.exp(-2j * math.pi * 1000 * clock))) * 2 / len(values)
        self.assertGreater(voice_tone, 0.1)
        self.assertLess(source_tone, voice_tone * 0.02)
        self.assertEqual(self.audio.read_bytes(), audio_bytes)
        self.assertEqual(self.changing_video.read_bytes(), video_bytes)

    def test_short_video_rejected_instead_of_looping_and_preserves_inputs(self):
        audio_bytes, video_bytes = self.audio.read_bytes(), self.short_video.read_bytes()
        plan = self._plan()
        plan["segments"][0].update(media_type="video", media_path=str(self.short_video))
        with self.assertRaisesRegex(composition.CompositionError, "不足分镜.*补充更长"):
            composition.compose(plan, self.audio, self.root)
        self.assertEqual(self.audio.read_bytes(), audio_bytes)
        self.assertEqual(self.short_video.read_bytes(), video_bytes)
        self.assertFalse(list(self.root.glob("composition-*")))

    def test_video_range_must_cover_offset_plus_quantized_shot(self):
        plan = self._plan()
        plan["segments"][1].update(media_type="video", media_path=str(self.changing_video), media_start=1.7)
        with self.assertRaisesRegex(composition.CompositionError, "1.70 秒.*不足分镜"):
            composition.compose(plan, self.audio, self.root)
        self.assertFalse(list(self.root.glob("composition-*")))

    def test_long_original_audio_does_not_disguise_short_video_stream(self):
        self.assertGreater(rendering.probe_source(self.unequal_video)["duration"], 1.9)
        plan = self._plan()
        plan["segments"][0].update(media_type="video", media_path=str(self.unequal_video))
        with self.assertRaisesRegex(composition.CompositionError, "不足分镜"):
            composition.compose(plan, self.audio, self.root)

    def test_invalid_video_offsets_are_rejected(self):
        for offset in (-0.1, math.nan, math.inf, "not seconds", None, True):
            with self.subTest(offset=offset):
                plan = self._plan()
                plan["segments"][0].update(media_type="video", media_path=str(self.changing_video), media_start=offset)
                with self.assertRaisesRegex(composition.CompositionError, "开始位置"):
                    composition.compose(plan, self.audio, self.root)

    def test_one_frame_rounding_tail_is_padded_without_looping(self):
        plan = self._plan()
        # First visual lasts 17 / 30 seconds; the source has 0.55 seconds left.
        plan["segments"][0].update(media_type="video", media_path=str(self.changing_video), media_start=1.45)
        result = composition.compose(plan, self.audio, self.root)
        self.assertAlmostEqual(rendering.probe_source(result["video_path"])["duration"], 1.234, delta=0.04)
        frame = self._frame(result["video_path"], 0.53).getpixel((90, 160))
        self.assertGreater(frame[2], frame[0] + 150)

    def test_high_frame_rate_tail_is_not_extended_by_a_whole_output_frame(self):
        source = self.root / "sixty-fps.mp4"
        subprocess.run([self.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                        "color=c=blue:s=160x240:r=60:d=1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                       check=True, capture_output=True, timeout=30)
        plan = self._plan()
        plan["segments"][0].update(media_type="video", media_path=str(source), media_start=0.46)
        with self.assertRaisesRegex(composition.CompositionError, "不足分镜"):
            composition.compose(plan, self.audio, self.root)

    def test_audio_tail_is_covered_without_inventing_subtitle_times(self):
        plan = self._plan(duration=1.13)
        result = composition.compose(plan, self.audio, self.root)
        # FFmpeg's fallback duration header has centisecond precision.
        self.assertAlmostEqual(result["duration"], 1.234, delta=0.01)
        self.assertEqual(plan["segments"][-1]["end"], 1.13)
        self.assertEqual(self._frame(result["video_path"], 1.18).size, (180, 320))

    def test_global_frame_rounding_does_not_accumulate_per_shot_drift(self):
        entries = [{"start": index * 0.113, "end": index * 0.113 + 0.08, "text": "咖啡。"} for index in range(10)]
        plan = planning.build_plan("咖啡", entries, [self.picture], duration=1.234)
        plan["size"] = (180, 320)
        result = composition.compose(plan, self.audio, self.root)
        self.assertAlmostEqual(rendering.probe_source(result["video_path"])["duration"], 1.234, delta=1 / 30 + 0.02)

    def test_each_call_writes_a_new_directory_inside_work_folder(self):
        first = composition.compose(self._plan(), self.audio, self.root)
        second = composition.compose(self._plan(), self.audio, self.root)
        self.assertNotEqual(first["video_path"], second["video_path"])
        self.assertTrue(Path(first["video_path"]).is_file())

    def test_bad_images_and_video_files_report_actionable_failure(self):
        for suffix, media_type in ((".png", "image"), (".mp4", "video")):
            with self.subTest(media_type=media_type):
                broken = self.root / f"broken{suffix}"
                broken.write_text("<html>not media</html>", "utf-8")
                plan = self._plan()
                plan["segments"][0].update(media_type=media_type, media_path=str(broken))
                with self.assertRaisesRegex(composition.CompositionError, "损坏|不完整|有效"):
                    composition.compose(plan, self.audio, self.root)

    def test_overrun_rejected_instead_of_extending_audio(self):
        plan = self._plan()
        plan["segments"][-1]["end"] = 2.5
        with self.assertRaisesRegex(composition.CompositionError, "超出当前配音时长"):
            composition.compose(plan, self.audio, self.root)

    def test_ffmpeg_timeout_has_clear_error(self):
        with patch.object(composition.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 60)):
            with self.assertRaisesRegex(composition.CompositionError, "超时"):
                composition._run(["ffmpeg"], timeout=60, label="画面合成")

    def test_missing_audio_is_not_replaced_with_silence(self):
        with self.assertRaisesRegex(composition.CompositionError, "配音文件"):
            composition.compose(self._plan(), self.root / "missing.wav", self.root)

    def test_product_cannot_sneak_information_card_into_final_composition(self):
        plan = self._plan(materials=[self.picture], kind="product")
        plan["segments"][0]["media_type"] = "card"
        with self.assertRaisesRegex(composition.CompositionError, "真实素材"):
            composition.compose(plan, self.audio, self.root)

    def test_avatar_requires_real_video_and_reuses_valid_base(self):
        plan = self._plan(kind="avatar")
        with self.assertRaisesRegex(composition.CompositionError, "缺少"):
            composition.compose(plan, self.audio, self.root)
        with self.assertRaisesRegex(composition.CompositionError, "短于当前配音"):
            composition.compose(plan, self.audio, self.root, base_video_path=self.short_video)
        base = self.root / "avatar.mp4"
        subprocess.run([self.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=green:s=160x240:r=30:d=1.3",
                        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base)], check=True, capture_output=True, timeout=30)
        result = composition.compose(plan, self.audio, self.root, base_video_path=base)
        self.assertEqual(result["video_path"], str(base))
        self.assertEqual(result["source"], "avatar")

    def test_invalid_dimensions_and_nan_times_are_rejected(self):
        plan = self._plan()
        plan["size"] = (180, 180)
        with self.assertRaisesRegex(composition.CompositionError, "画幅"):
            composition.compose(plan, self.audio, self.root)
        plan["size"] = (180, 320)
        plan["segments"][0]["start"] = math.nan
        with self.assertRaisesRegex(composition.CompositionError, "时间"):
            composition.compose(plan, self.audio, self.root)

    def test_progress_uses_existing_callback_shape(self):
        calls = []
        composition.compose(self._plan(), self.audio, self.root, progress=lambda message, percent: calls.append((message, percent)))
        self.assertEqual(calls[-1][1], 100)


if __name__ == "__main__":
    unittest.main()
