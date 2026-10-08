"""Bundled Chinese variable fonts produce readable cards and complete covers."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.services.creator import composition, release_assets


FONT = Path(__file__).resolve().parents[1] / "resource" / "fonts" / "NotoSansSC.ttf"


class VariableFontTests(unittest.TestCase):
    def test_weight_axis_is_set_without_changing_other_axis_defaults(self):
        for name in (b"Weight", "wght"):
            with self.subTest(name=name):
                font = Mock()
                font.get_variation_axes.return_value = [
                    {"name": b"Optical Size", "minimum": 8, "default": 14, "maximum": 72},
                    {"name": name, "minimum": 100, "default": 100, "maximum": 900},
                ]
                with patch.object(composition.ImageFont, "truetype", return_value=font):
                    self.assertIs(composition.load_font("font.ttf", 32, weight=600), font)
                font.set_variation_by_axes.assert_called_once_with([14, 600])

    def test_requested_weight_is_clamped_to_font_axis_range(self):
        for requested, expected in ((50, 100), (1200, 900)):
            with self.subTest(weight=requested):
                font = Mock()
                font.get_variation_axes.return_value = [{"name": b"Weight", "minimum": 100, "default": 100, "maximum": 900}]
                with patch.object(composition.ImageFont, "truetype", return_value=font):
                    composition.load_font("font.ttf", 20, weight=requested)
                font.set_variation_by_axes.assert_called_once_with([expected])

    def test_static_font_variation_error_retains_the_original_usable_font(self):
        font = Mock()
        font.get_variation_axes.side_effect = OSError("invalid argument")
        with patch.object(composition.ImageFont, "truetype", return_value=font):
            self.assertIs(composition.load_font("static.ttf", 32), font)
        font.set_variation_by_axes.assert_not_called()

    def test_font_without_weight_axis_keeps_its_original_settings(self):
        font = Mock()
        font.get_variation_axes.return_value = [{"name": b"Width", "minimum": 75, "default": 100, "maximum": 125}]
        with patch.object(composition.ImageFont, "truetype", return_value=font):
            self.assertIs(composition.load_font("width.ttf", 32), font)
        font.set_variation_by_axes.assert_not_called()

    def test_font_candidates_prefer_explicit_choice_then_bundled_noto(self):
        self.assertTrue(FONT.is_file(), "Release must bundle the open Chinese font")
        with patch.dict(os.environ, {"MPT_CREATOR_FONT": ""}):
            self.assertEqual(Path(composition._font_path()), FONT)
            self.assertEqual(Path(release_assets._font_path()), FONT)
        with tempfile.TemporaryDirectory() as directory:
            explicit = Path(directory) / "selected.ttf"
            explicit.write_bytes(b"font-path-selection-fixture")
            with patch.dict(os.environ, {"MPT_CREATOR_FONT": str(explicit)}):
                self.assertEqual(Path(composition._font_path()), explicit)
                self.assertEqual(Path(release_assets._font_path()), explicit)

    def test_real_noto_has_readable_weight_and_unchanged_font_bytes(self):
        self.assertTrue(FONT.is_file())
        original = hashlib.sha256(FONT.read_bytes()).digest()
        thin = ImageFont.truetype(str(FONT), 32)
        semibold = composition.load_font(FONT, 32, weight=600)
        text = "中文口播清晰可读"
        thin_ink = np.array(thin.getmask(text), dtype=np.uint8).sum()
        semibold_ink = np.array(semibold.getmask(text), dtype=np.uint8).sum()
        self.assertGreater(semibold_ink, thin_ink * 2)
        self.assertEqual(hashlib.sha256(FONT.read_bytes()).digest(), original)

    def test_real_noto_card_uses_title_body_and_counter_weights(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MPT_CREATOR_FONT": str(FONT)}):
            output = Path(directory) / "card.png"
            with patch.object(composition, "load_font", wraps=composition.load_font) as load:
                composition._information_card("咖啡休息。", output, (180, 320), 0, 2, title="咖啡")
            weights = [call.kwargs["weight"] for call in load.call_args_list]
            self.assertEqual(weights[0], 700)
            self.assertIn(600, weights)
            self.assertEqual(weights[-1], 400)
            with Image.open(output) as image:
                frame = np.array(image.convert("RGB"))
            self.assertGreater(np.sum(np.min(frame[80:230], axis=2) > 160), 25)

    def test_real_noto_cover_layout_preserves_all_characters_with_measured_wrap(self):
        titles = [
            "选题文案配音数字人成片标题封面发布" * 6 + "最后四字完整显示",
            "Creator video production starts with accurate copy and clear voices. 完整保留最后文字",
            "从选题到成片，一步步完成",
        ]
        with patch.dict(os.environ, {"MPT_CREATOR_FONT": str(FONT)}):
            for title in titles:
                for width, height in ((400, 270), (1000, 210)):
                    with self.subTest(title=title[:20], bounds=(width, height)):
                        with patch.object(composition, "load_font", wraps=composition.load_font) as load:
                            font, lines, line_height = release_assets._layout_title(title, width, height, 90)
                        self.assertTrue(all(call.kwargs["weight"] == 700 for call in load.call_args_list))
                        self.assertEqual("".join("".join(lines).split()), "".join(title.split()))
                        self.assertLessEqual(len(lines) * line_height, height)
                        measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
                        self.assertTrue(all(measure.textlength(line, font=font) <= width for line in lines))


if __name__ == "__main__":
    unittest.main()
