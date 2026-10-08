import unittest

from app.services.creator import spoken_library


class SpokenLibraryTests(unittest.TestCase):
    def test_five_profiles_default_is_industry_unrestricted_and_idle(self):
        profiles = spoken_library.list_profiles()
        self.assertEqual(5, len(profiles))
        self.assertEqual("spoken_general", profiles[0]["industry_id"])
        for row in profiles:
            profile = spoken_library.get_profile(row["industry_id"])
            self.assertEqual([], profile["content_sources"])
            self.assertFalse(profile["update_policy"]["enabled"])
        self.assertEqual([], spoken_library.get_profile()["keyword_pool"]["must_include"])
        with self.assertRaises(ValueError):
            spoken_library.get_profile("../../config")

    def test_long_source_is_preserved_and_first_hook_is_a_literal_sentence(self):
        text = "别急着选工具。\n" + "先看本机配置和使用场景。" * 300 + "最后检查导出参数。"
        result = spoken_library.structure_text(text, "工具测评")
        self.assertEqual(text, result["full_content"])
        self.assertEqual(text, result["spoken_script"])
        self.assertEqual("别急着选工具。", result["hook_3s"])
        self.assertEqual("source_rules_v1", result["extraction_method"])
        self.assertLessEqual(len(result["bullet_points"]), 10)
        self.assertTrue(all(point in text for point in result["bullet_points"]))
        self.assertEqual(12000, len(spoken_library.structure_text("字" * 14000)["full_content"]))

    def test_short_actual_script_structures_without_library_length_gate(self):
        result = spoken_library.structure_text("先看菜单，再决定套餐。")
        self.assertEqual("先看菜单，再决定套餐。", result["spoken_script"])
        self.assertTrue(result["bullet_points"])
        self.assertFalse(spoken_library.filter_item(result)["eligible"])

    def test_material_clues_and_extra_facts_come_from_source_only(self):
        text = "先看菜单，再决定套餐。人均79元。地址：幸福路8号。服务时长：40分钟。#避坑 #真实体验"
        result = spoken_library.structure_text(text, "餐厅测评", "catering_spoken")
        self.assertTrue(result["material_clues"])
        for clue in result["material_clues"]:
            self.assertIn(clue["description"], text)
            self.assertIn(clue["source_text"], text)
        self.assertEqual("人均79元", result["industry_extra_fields"]["price"])
        self.assertEqual("地址：幸福路8号", result["industry_extra_fields"]["location"])
        self.assertIn("避坑", result["tags"])
        self.assertNotIn("套餐", result["industry_extra_fields"])
        no_fact = spoken_library.structure_text("这家店挺适合周末去，价格我还没有查到。", industry_id="catering_spoken")
        self.assertEqual({}, no_fact["industry_extra_fields"])

    def test_tags_are_preserved_without_inventing_topic_keywords(self):
        result = spoken_library.structure_text("介绍我今天的真实体验。#经验分享", tags=["#同城", "同城"])
        self.assertEqual(["经验分享", "同城"], result["tags"])
        self.assertEqual([], spoken_library.structure_text("完全没有标签。", "价格测评")["tags"])

    def test_unknown_format_and_non_local_industry_are_admitted(self):
        text = "关于开源软件，可以先检查系统要求，再根据实际需求做选择。" * 4
        item = {"title": "操作系统讨论", "full_content": text}
        result = spoken_library.filter_item(item)
        self.assertTrue(result["eligible"])
        self.assertEqual("未知", result["content_format"])

    def test_explicitly_unsuitable_formats_and_short_content_are_filtered(self):
        text = "这段内容只有展示画面，没有进一步的信息解释。" * 8
        for form in ("纯图片轮播", "带货挂车无口播", "BGM展示", "招商加盟", "纯特效展示"):
            with self.subTest(form=form):
                result = spoken_library.filter_item({"full_content": text, "content_format": form})
                self.assertFalse(result["eligible"])
                self.assertTrue(any(form in reason for reason in result["reasons"]))
        short = spoken_library.filter_item({"full_content": "一句介绍"})
        self.assertEqual(["正文不足 80 字"], short["reasons"])
        self.assertEqual("未知", spoken_library.content_format("这不是纯BGM，实际还有文字解释。"))

    def test_specialized_profile_uses_its_own_threshold_and_keywords(self):
        profile = spoken_library.get_profile("education_spoken")
        self.assertEqual(100, profile["scraping_rule"]["min_text_length"])
        item = {"industry_id": "education_spoken", "full_content": "家长挑选课程时应该先试听再比较。" * 10}
        self.assertTrue(spoken_library.filter_item(item)["eligible"])
        other = {"industry_id": "education_spoken", "full_content": "关于操作系统，我们今天谈谈磁盘管理问题。" * 10}
        self.assertFalse(spoken_library.filter_item(other)["eligible"])

    def test_ranking_prefers_real_comment_count_and_preserves_unknown(self):
        rows = [
            {"id": "unknown", "hot_metrics": {"like": 999999, "comment": None, "collect": None}, "comments": ["片段"] * 8},
            {"id": "zero", "hot_metrics": {"like": 2, "comment": 0, "collect": None}},
            {"id": "discussed", "hot_metrics": {"like": 1, "comment": 50, "collect": 4}},
        ]
        self.assertEqual(["discussed", "zero", "unknown"], [row["id"] for row in sorted(rows, key=spoken_library.rank_item, reverse=True)])
        self.assertEqual("unknown", sorted(rows, key=lambda row: spoken_library.rank_item(row, False), reverse=True)[0]["id"])
        self.assertIsNone(rows[0]["hot_metrics"]["comment"])
        self.assertEqual((-1, -1, -1), spoken_library.rank_item({"hot_metrics": {"comment": True, "like": float("nan")}}))


if __name__ == "__main__":
    unittest.main()
