from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from app.services.creator import extract, release_assets as assets, store, topics


class CreatorReleaseAssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tempfile.TemporaryDirectory()
        cls.media_root = Path(cls.files.name)
        cls.video = cls.media_root / "source.mp4"
        cls.audio = cls.media_root / "voice.wav"
        ffmpeg = extract.ffmpeg_binary()
        subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                        "color=red:s=240x320:r=25:d=1", "-f", "lavfi", "-i",
                        "color=blue:s=240x320:r=25:d=1", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                        "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(cls.video)],
                       check=True, capture_output=True, timeout=30)
        subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", str(cls.audio)], check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.files.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": str(self.root / "creator")})
        self.environment.start()
        self.text = "这是一段用于验证的视频文案，完成选题、文案和配音后，我们准备标题和封面。"
        self.payload = {"titles": ["从选题到成片", "如何准备视频封面", "口播视频的制作步骤"],
                        "hashtags": ["视频制作", "#口播", "视频制作"], "description": "先确认文案和配音，再准备标题与封面。",
                        "cover_title": "从选题到成片"}

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_provider_uses_config_snapshot_and_only_actual_copy_context(self):
        config = {"llm_provider": "existing"}
        events = []
        with patch.object(topics, "_generate", return_value=json.dumps(self.payload, ensure_ascii=False)) as provider:
            result = assets.generate_metadata(self.text, app_config=config, progress=lambda *args: events.append(args))
        self.assertIs(provider.call_args.kwargs["app_config"], config)
        prompt = provider.call_args.args[0]
        self.assertIn(json.dumps({"text": self.text}, ensure_ascii=False), prompt)
        self.assertIn("不是实时热榜", prompt)
        self.assertEqual(result["hashtags"], ["视频制作", "口播"])
        self.assertEqual(events[-1][1], 100)
        self.assertEqual(store.list_records("release_assets"), [])

    def test_valid_json_fence_and_unicode_duplicate_tags(self):
        self.payload["hashtags"] = ["#AI", "ai", "ＡＩ", "知识分享"]
        result = assets._validated_metadata("```json\n" + json.dumps(self.payload) + "\n```", 3)
        self.assertEqual(result["hashtags"], ["AI", "知识分享"])

    def test_invalid_metadata_rejected_without_partial_persistence(self):
        invalid = [[], dict(self.payload, titles=["重复", "重复", "第三个"]),
                   dict(self.payload, titles=["x"]), dict(self.payload, titles=["x" * 121, "第二", "第三"]),
                   dict(self.payload, hashtags=[]), dict(self.payload, hashtags=["话题 空格"]),
                   dict(self.payload, description=""), dict(self.payload, cover_title=""),
                   dict(self.payload, ranking=1)]
        for payload in invalid:
            with self.subTest(payload=payload):
                with patch.object(topics, "_generate", return_value=json.dumps(payload)):
                    with self.assertRaises(assets.ReleaseAssetsError):
                        assets.generate_metadata(self.text)
        with self.assertRaises(assets.ReleaseAssetsError):
            assets._validated_metadata('{"titles":[],"titles":[]}', 3)
        self.assertEqual(store.list_records("release_assets"), [])

    def test_provider_error_never_echoes_credentials(self):
        with patch.object(topics, "_generate", side_effect=topics.TopicGenerationError("secret-key-example")):
            with self.assertRaises(assets.ReleaseAssetsError) as caught:
                assets.generate_metadata(self.text)
        self.assertNotIn("secret-key-example", str(caught.exception))
        self.assertIn("检查模型", str(caught.exception))
        with patch.object(topics, "_generate", side_effect=RuntimeError("unexpected-secret-key")):
            with self.assertRaises(assets.ReleaseAssetsError) as caught:
                assets.generate_metadata(self.text)
        self.assertNotIn("unexpected-secret-key", str(caught.exception))

    def test_input_limits_fail_before_provider_call(self):
        with patch.object(topics, "_generate") as provider:
            for text in ("你好", "", "x" * 6001, None):
                with self.subTest(text=str(text)[:20]), self.assertRaises(ValueError):
                    assets.generate_metadata(text)
            for count in (0, 6, 1.5, True):
                with self.subTest(count=count), self.assertRaises(ValueError):
                    assets.generate_metadata(self.text, count=count)
            provider.assert_not_called()

    def test_frame_time_matches_actual_red_then_blue_video(self):
        early = assets.generate_cover(self.video, "真实前半段画面", frame_time=0.2)
        late = assets.generate_cover(self.video, "真实后半段画面", frame_time=1.5)
        with Image.open(early["cover_path"]) as red, Image.open(late["cover_path"]) as blue:
            r1, _, b1 = red.getpixel((360, 100))
            r2, _, b2 = blue.getpixel((360, 100))
        self.assertGreater(r1, b1 + 100)
        self.assertGreater(b2, r2 + 100)
        self.assertEqual(late["frame_time"], 1.5)
        self.assertEqual(len(assets.list_covers()), 2)

    def test_four_styles_are_distinct_real_images_with_requested_dimensions(self):
        image_hashes = set()
        for style in assets.list_styles():
            row = assets.generate_cover(self.video, "标题和封面准备完成", style=style["id"], aspect="1:1")
            with Image.open(row["cover_path"]) as image:
                self.assertEqual(image.size, (720, 720))
                image_hashes.add(hashlib.sha256(image.tobytes()).hexdigest())
            self.assertEqual(row["state"], "done")
            self.assertEqual((row["width"], row["height"]), (720, 720))
        self.assertEqual(len(image_hashes), 4)
        landscape = assets.generate_cover(self.video, "横屏封面", aspect="16:9")
        with Image.open(landscape["cover_path"]) as image:
            self.assertEqual(image.size, (1280, 720))

    def test_aspect_conversion_preserves_both_edges_of_source_picture(self):
        source = Image.new("RGB", (1280, 720), "#506070")
        draw = ImageDraw.Draw(source)
        draw.rectangle((0, 0, 80, 719), fill="red")
        draw.rectangle((1200, 0, 1279, 719), fill="blue")
        cover = assets._compose_cover(source, "保持完整视频画面", "clean", (720, 1280))
        red = blue = 0
        for pixel in cover.crop((0, 0, 720, 500)).getdata():
            red += int(pixel[0] > 200 and pixel[2] < 60)
            blue += int(pixel[2] > 200 and pixel[0] < 60)
        self.assertGreater(red, 1000)
        self.assertGreater(blue, 1000)

    def test_long_chinese_and_english_layout_retains_every_visible_character(self):
        cases = ["选题文案配音数字人成片标题封面发布" * 6 + "最后四字完整显示", "A" * 120,
                 "Creator video production and publishing starts with accurate copy and clear voices. " + "完整保留最后文字"]
        for title in cases:
            self.assertLessEqual(len(title), 120)
            for width, height in ((400, 270), (1000, 210)):
                font, lines, line_height = assets._layout_title(title, width, height, 90)
                self.assertEqual("".join("".join(lines).split()), "".join(title.split()))
                self.assertLessEqual(len(lines) * line_height, height)
                measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
                self.assertTrue(all(measure.textlength(line, font=font) <= width for line in lines))
        title = "选题文案配音数字人成片标题封面发布" * 6 + "最后四字完整显示"
        row = assets.generate_cover(self.video, title, style="business", aspect="1:1")
        self.assertEqual(row["title"], title)

    def test_natural_title_does_not_wrap_comma_onto_next_line(self):
        title = "从选题到成片，一步步完成"
        _, lines, _ = assets._layout_title(title, 578, 331, 83)
        self.assertFalse(any(line.startswith("，") for line in lines))
        self.assertEqual("".join(lines), title)

    def test_source_is_preserved_and_regeneration_keeps_previous_cover(self):
        original = hashlib.sha256(self.video.read_bytes()).hexdigest()
        first = assets.generate_cover(self.video, "第一版封面")
        first_bytes = Path(first["cover_path"]).read_bytes()
        second = assets.generate_cover(self.video, "第二版封面")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(Path(first["cover_path"]).read_bytes(), first_bytes)
        self.assertEqual(hashlib.sha256(self.video.read_bytes()).hexdigest(), original)

    def test_invalid_time_style_title_and_audio_are_rejected_before_records(self):
        for frame_time in (-1, 2, 8, float("nan"), float("inf"), True, "1"):
            with self.subTest(frame_time=frame_time), self.assertRaises(ValueError):
                assets.generate_cover(self.video, "标题", frame_time=frame_time)
        for kwargs in ({"style": "unknown"}, {"aspect": "3:4"}, {"title": "x" * 121}, {"title": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                assets.generate_cover(self.video, **dict({"title": "标题"}, **kwargs))
        with self.assertRaises(ValueError):
            assets.generate_cover(self.audio, "音频不是视频")
        self.assertEqual(store.list_records("covers"), [])

    def test_failed_cover_has_safe_state_and_preserves_successful_history(self):
        first = assets.generate_cover(self.video, "已有封面")
        original = Path(first["cover_path"]).read_bytes()
        with patch.object(assets, "_compose_cover", side_effect=RuntimeError("secret-key-example")):
            with self.assertRaises(assets.ReleaseAssetsError) as caught:
                assets.generate_cover(self.video, "失败封面")
        self.assertNotIn("secret-key-example", str(caught.exception))
        rows = store.list_records("covers")
        failed = next(row for row in rows if row["state"] == "failed")
        self.assertNotIn("secret-key-example", failed["error"])
        self.assertFalse((store.data_root() / "covers" / failed["id"] / "cover.png").exists())
        self.assertEqual([row["id"] for row in assets.list_covers()], [first["id"]])
        self.assertEqual(Path(first["cover_path"]).read_bytes(), original)

    def test_missing_font_has_clear_error_before_record_creation(self):
        with patch.object(assets, "_font_path", side_effect=assets.ReleaseAssetsError("请安装中文字体")):
            with self.assertRaisesRegex(assets.ReleaseAssetsError, "中文字体"):
                assets.generate_cover(self.video, "标题")
        self.assertEqual(store.list_records("covers"), [])

    def test_saved_materials_are_complete_distinct_and_do_not_publish(self):
        cover = assets.generate_cover(self.video, "封面标题")
        row = assets.save_materials(self.video, "发布标题", description="发布正文", hashtags=["#AI", "ai", "视频制作"],
                                   cover_path=cover["cover_path"], source_text=self.text)
        self.assertEqual(row["publish_description"], "发布正文\n\n#AI #视频制作")
        self.assertEqual(row["hashtags"], ["AI", "视频制作"])
        self.assertEqual(row["source_text"], self.text)
        self.assertEqual(row["cover_path"], cover["cover_path"])
        next_row = assets.save_materials(self.video, "另一个标题")
        self.assertNotEqual(row["id"], next_row["id"])
        self.assertEqual(next_row["publish_description"], "")
        self.assertEqual(len(assets.list_materials()), 2)
        self.assertEqual(store.list_records("publish_tasks"), [])

    def test_material_validation_rejects_broken_images_and_bad_limits(self):
        broken = self.root / "broken.png"
        broken.write_text("not an image", "utf-8")
        with self.assertRaises(ValueError):
            assets.save_materials(self.video, "标题", cover_path=broken)
        for kwargs in ({"title": "x" * 121}, {"description": "x" * 2001}, {"source_text": "x" * 6001},
                       {"hashtags": ["x"] * 11}, {"hashtags": ["#"]}, {"hashtags": "#错误列表"}):
            with self.subTest(kwargs=str(kwargs)[:100]), self.assertRaises(ValueError):
                assets.save_materials(self.video, **dict({"title": "标题"}, **kwargs))
        self.assertEqual(store.list_records("release_assets"), [])

    def test_history_hides_materials_whose_cover_has_disappeared(self):
        cover = assets.generate_cover(self.video, "封面")
        assets.save_materials(self.video, "发布标题", cover_path=cover["cover_path"])
        Path(cover["cover_path"]).unlink()
        self.assertEqual(assets.list_covers(), [])
        self.assertEqual(assets.list_materials(), [])


if __name__ == "__main__":
    unittest.main()
