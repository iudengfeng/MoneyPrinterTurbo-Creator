from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.services.creator import planning


class CreatorPlanningTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.coffee = self.root / "咖啡杯.png"
        self.computer = self.root / "computer-screen.png"
        self.other = self.root / "upload-001.png"
        for index, path in enumerate((self.coffee, self.computer, self.other)):
            Image.new("RGB", (80, 120), (index * 70, 90, 180)).save(path)
        self.entries = [
            {"start": 0.23, "end": 0.93, "text": "咖啡带来一段休息时间。"},
            {"start": 1.18, "end": 2.31, "text": "Computer tools help us work."},
        ]

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_cue_times_are_preserved_and_pauses_are_visual_only(self):
        result = planning.build_plan("咖啡与工作", self.entries, [self.coffee, self.computer], duration=2.8)
        self.assertEqual(result["duration"], 2.8)
        self.assertEqual([(row["start"], row["end"]) for row in result["segments"]], [(0.23, 0.93), (1.18, 2.31)])
        self.assertEqual([(row["visual_start"], row["visual_end"]) for row in result["segments"]], [(0, 1.18), (1.18, 2.8)])
        self.assertEqual([row["media_path"] for row in result["segments"]], [str(self.coffee), str(self.computer)])
        self.assertEqual(result["matching_method"], "filename_metadata")
        self.assertEqual(result["shots"], result["segments"])

    def test_metadata_matches_opaque_upload_filename_and_records_reason(self):
        row = planning.build_plan("咖啡", self.entries[:1], [{"file_path": self.other, "tags": ["咖啡", "杯子"]}])["segments"][0]
        self.assertEqual(row["media_path"], str(self.other))
        self.assertIn("文件名/素材标签", row["match_reason"])
        self.assertIn("咖啡", row["keywords"])

    def test_knowledge_uses_information_cards_when_unrelated_materials_exist(self):
        result = planning.build_plan("讲咖啡", self.entries[:1], [self.computer])
        self.assertEqual(result["segments"][0]["media_type"], "card")
        self.assertIsNone(result["segments"][0]["media_path"])
        self.assertIn("没有匹配", result["segments"][0]["match_reason"])
        self.assertTrue(result["warnings"])

    def test_repeated_paths_are_deduplicated_before_fallback_rotation(self):
        entries = [{"start": index, "end": index + 0.7, "text": "无关的内容。"} for index in range(4)]
        result = planning.build_plan("介绍", entries, [self.coffee, self.coffee, self.computer], kind="product")
        self.assertEqual([row["media_path"] for row in result["segments"]], [str(self.coffee), str(self.computer)] * 2)
        self.assertEqual([row["reuse_count"] for row in result["segments"]], [0, 0, 1, 1])
        self.assertIn("未进行画面语义识别", result["segments"][0]["match_reason"])

    def test_equally_matching_assets_are_used_before_repeating(self):
        entries = [{"start": index, "end": index + 0.8, "text": "咖啡"} for index in range(3)]
        material = {"path": self.other, "keywords": "咖啡杯"}
        rows = planning.build_plan("咖啡", entries, [self.coffee, material])["segments"]
        self.assertNotEqual(rows[0]["media_path"], rows[1]["media_path"])
        self.assertEqual(rows[2]["reuse_count"], 1)

    def test_product_and_montage_cannot_replace_missing_materials_with_cards(self):
        for kind in ("product", "montage"):
            with self.subTest(kind=kind), self.assertRaisesRegex(planning.PlanningError, "真实图片或视频"):
                planning.build_plan("内容", self.entries, [], kind=kind)

    def test_avatar_does_not_need_uploaded_footage(self):
        result = planning.build_plan("口播", self.entries, [], kind="avatar", aspect="16:9")
        self.assertTrue(all(row["media_type"] == "avatar" for row in result["segments"]))
        self.assertEqual(result["aspect"], "16:9")

    def test_invalid_missing_and_unsupported_assets_are_reported(self):
        unsupported = self.root / "notes.txt"
        unsupported.write_text("资料", "utf-8")
        result = planning.build_plan("口播", self.entries, [{}, self.root / "missing.png", unsupported])
        self.assertGreaterEqual(len(result["warnings"]), 3)
        self.assertTrue(all(row["media_type"] == "card" for row in result["segments"]))

    def test_unsorted_entries_are_ordered_without_changing_timestamps(self):
        result = planning.build_plan("口播", list(reversed(self.entries)), [])
        self.assertEqual([row["start"] for row in result["segments"]], [0.23, 1.18])

    def test_duration_must_be_real_audio_duration_without_out_of_range_cues(self):
        with self.assertRaisesRegex(planning.PlanningError, "实际配音时长"):
            planning.build_plan("口播", self.entries, [], duration=2)
        near = planning.build_plan("口播", self.entries, [], duration=2.25)
        self.assertEqual(near["duration"], 2.25)
        self.assertEqual(near["segments"][-1]["end"], 2.31)

    def test_missing_invalid_and_overlapping_entries_are_rejected(self):
        invalid = [[], [{"start": math.nan, "end": 1, "text": "内容"}],
                   [{"start": 1, "end": 0, "text": "内容"}],
                   [{"start": 0, "end": 1, "text": ""}],
                   [{"start": 0, "end": 1, "text": "甲"}, {"start": 0.5, "end": 2, "text": "乙"}]]
        for entries in invalid:
            with self.subTest(entries=entries), self.assertRaises(planning.PlanningError):
                planning.build_plan("口播", entries, [])

    def test_kind_aspect_and_script_are_validated(self):
        for kwargs in ({"kind": "unknown"}, {"aspect": "4:3"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(planning.PlanningError):
                planning.build_plan("口播", self.entries, [], **kwargs)
        with self.assertRaises(planning.PlanningError):
            planning.build_plan(" ", self.entries, [])

    def test_progress_uses_existing_callback_shape(self):
        calls = []
        planning.build_plan("口播", self.entries, [], progress=lambda message, percent: calls.append((message, percent)))
        self.assertEqual(calls[0][1], 0)
        self.assertEqual(calls[-1][1], 100)

    def test_grounded_visual_clue_matches_local_labels_for_its_actual_asr_cue(self):
        script = "先看店内大厅和吧台。看看环境是否安静。再对照菜单选择套餐。"
        entries = [
            {"start": 0.23, "end": 0.93, "text": "先看店内大厅和吧台。"},
            {"start": 1.18, "end": 2.31, "text": "看看环境是否安静。"},
            {"start": 2.7, "end": 3.5, "text": "再对照菜单选择套餐。"},
        ]
        materials = [{"path": self.other, "tags": ["吧台", "大厅"]}]
        brief = {
            "bullet_points": ["看看环境是否安静。", "再对照菜单选择套餐。"],
            "material_clues": [{"type": "环境", "description": "店内大厅和吧台",
                                "source_text": "先看店内大厅和吧台。看看环境是否安静。"}],
        }
        plain = planning.build_plan(script, entries, materials, duration=4)
        result = planning.build_plan(script, entries, materials, duration=4, video_brief=brief)
        self.assertEqual("card", plain["segments"][1]["media_type"])
        self.assertEqual(str(self.other), result["segments"][1]["media_path"])
        self.assertIn("口播画面线索", result["segments"][1]["match_reason"])
        self.assertEqual("店内大厅和吧台", result["segments"][1]["visual_clues"][0]["description"])
        self.assertEqual([(row["start"], row["end"]) for row in entries],
                         [(row["start"], row["end"]) for row in result["segments"]])
        self.assertEqual([(0, 1.18), (1.18, 2.7), (2.7, 4)],
                         [(row["visual_start"], row["visual_end"]) for row in result["segments"]])

    def test_reference_prices_and_addresses_cannot_enter_cards_even_with_real_source_anchor(self):
        script = "先看看店内环境是否安静。再根据自己的需求决定是否体验。"
        entries = [{"start": 0.1, "end": 1.3, "text": "先看看店内环境是否安静。"},
                   {"start": 1.7, "end": 3.1, "text": "再根据自己的需求决定是否体验。"}]
        brief = {
            "full_content": "竞品套餐99元，地址东街1号。",
            "bullet_points": ["竞品套餐99元，地址东街1号。"],
            "material_clues": [{"type": "信息", "description": "套餐99元，地址东街1号",
                                "source_text": entries[0]["text"]}],
            "industry_extra_fields": {"price": "99元", "location": "东街1号"},
        }
        result = planning.build_plan(script, entries, [], video_brief=brief)
        rendered_fields = str([row[field] for row in result["segments"]
                               for field in ("card_title", "card_text", "visual_clues", "keywords")])
        self.assertNotIn("99元", rendered_fields)
        self.assertNotIn("东街1号", rendered_fields)
        self.assertEqual(script, result["video_brief"]["spoken_script"])
        self.assertEqual([(row["start"], row["end"]) for row in entries],
                         [(row["start"], row["end"]) for row in result["segments"]])

    def test_script_information_point_becomes_card_without_inventing_content(self):
        point = "咖啡喝得太快，可能来不及休息。"
        script = point + "先给自己留一点安静的时间。"
        entries = [{"start": 0.2, "end": 1.2, "text": "咖啡喝得太快。"},
                   {"start": 1.6, "end": 2.8, "text": "先给自己留一点安静的时间。"}]
        result = planning.build_plan(script, entries, [], video_brief={"bullet_points": [point]})
        self.assertEqual(point, result["segments"][0]["card_text"])
        self.assertEqual("咖啡喝得太快", result["segments"][0]["card_title"])
        self.assertTrue(all(planning._contained(row["card_text"], script) for row in result["segments"]))

    def test_brief_never_changes_balanced_rotation_for_unrelated_product_assets(self):
        script = "现在说明如何安排时间。"
        entries = [{"start": index, "end": index + 0.7, "text": script} for index in range(4)]
        brief = {"bullet_points": [script], "material_clues": [{"type": "环境", "description": "山川远景", "source_text": script}]}
        result = planning.build_plan(script, entries, [self.coffee, self.computer], kind="product", video_brief=brief)
        self.assertEqual([str(self.coffee), str(self.computer)] * 2, [row["media_path"] for row in result["segments"]])
        self.assertTrue(all("轮换" in row["match_reason"] for row in result["segments"]))

    def test_fact_containment_keeps_decimal_values_and_numeric_boundaries(self):
        for excerpt, source in [("99元", "套餐199元。"), ("1.99元", "套餐199元。"),
                                ("99元", "套餐1.99元。"), ("99", "价格99.5元。"),
                                ("99", "价格999元。"), ("99元", "价格1,99元。")]:
            with self.subTest(excerpt=excerpt, source=source):
                self.assertFalse(planning._contained(excerpt, source))
        self.assertTrue(planning._contained("99 元", "套餐99元。"))
        self.assertTrue(planning._contained("1.99元", "套餐1.99元。"))
        self.assertTrue(planning._contained("ABC", "型号 abc 可选。"))

    def test_foreign_price_cannot_use_real_199_yuan_sentence_as_provenance(self):
        script = "套餐199元。先看服务包含的内容，再决定是否购买。"
        entries = [{"start": 0.1, "end": 1.2, "text": "套餐199元。"},
                   {"start": 1.5, "end": 3.1, "text": "先看服务包含的内容，再决定是否购买。"}]
        for foreign in ("99元", "1.99元", "套餐1.99元。"):
            with self.subTest(foreign=foreign):
                brief = {
                    "bullet_points": [foreign],
                    "material_clues": [{"type": "信息卡", "description": foreign,
                                        "source_text": entries[0]["text"]}],
                }
                result = planning.build_plan(script, entries, [], video_brief=brief)
                self.assertNotIn(foreign, result["video_brief"]["bullet_points"])
                self.assertTrue(all(clue["description"] != foreign for clue in result["video_brief"]["material_clues"]))
                self.assertEqual("套餐199元。", result["segments"][0]["card_text"])
                self.assertTrue(all(row["card_text"] != foreign for row in result["segments"]))
        # An invented source_text cannot make an otherwise actual description safe.
        bad_anchor = {"material_clues": [{"type": "信息卡", "description": "套餐199元。", "source_text": "99元"}]}
        result = planning.build_plan(script, entries, [], video_brief=bad_anchor)
        self.assertTrue(all(clue["source_text"] != "99元" for clue in result["video_brief"]["material_clues"]))

    def test_original_99_yuan_fact_can_drive_card_and_visual_clue(self):
        script = "套餐99元。先看服务包含的内容。"
        entries = [{"start": 0.1, "end": 1.2, "text": "套餐99元。"},
                   {"start": 1.5, "end": 2.8, "text": "先看服务包含的内容。"}]
        brief = {"bullet_points": ["99元"],
                 "material_clues": [{"type": "信息卡", "description": "99元", "source_text": "套餐99元。"}]}
        result = planning.build_plan(script, entries, [], video_brief=brief)
        self.assertEqual(["99元"], result["video_brief"]["bullet_points"])
        self.assertEqual("99元", result["video_brief"]["material_clues"][0]["description"])
        self.assertEqual("套餐99元。", result["segments"][0]["card_text"])


if __name__ == "__main__":
    unittest.main()
