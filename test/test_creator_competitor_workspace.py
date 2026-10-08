"""Customer reference-library behavior, using local records and mocked collection."""

import os
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services.creator import competitors, store


class SpokenReferenceWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.folder.name})
        environment.start()
        self.addCleanup(environment.stop)
        scheduler = patch.object(competitors, "ensure_scheduler", return_value=False)
        self.scheduler = scheduler.start()
        self.addCleanup(scheduler.stop)
        fetch = patch.object(competitors, "_fetch_public", side_effect=AssertionError("Unexpected public fetch"))
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)
        dependency = patch.object(competitors, "dependency_ready", return_value=True)
        dependency.start()
        self.addCleanup(dependency.stop)

    def app(self):
        app = AppTest.from_string("from webui.creator_competitor_workspace import render\nrender()", default_timeout=20).run()
        self.assertFalse(app.exception)
        return app

    @staticmethod
    def save(app):
        return next(button for button in app.button if button.label == "保存采集设置").click().run()

    @staticmethod
    def displayed(app):
        return "\n".join(str(node.value) for kind in (app.markdown, app.caption, app.info, app.warning) for node in kind)

    def test_default_is_general_with_empty_sources_and_no_collection(self):
        app = self.app()
        self.assertEqual(app.header[0].value, "口播参考库")
        self.assertEqual(app.selectbox(key="competitor_spoken_profile").value, "spoken_general")
        self.assertEqual(len(app.selectbox(key="competitor_spoken_profile").options), 5)
        self.assertEqual(app.number_input(key="competitor_min_text_length").value, 80)
        self.assertTrue(app.checkbox(key="competitor_prefer_high_comment").value)
        self.assertFalse(app.checkbox(key="competitor_auto_update").value)
        self.assertTrue(app.checkbox(key="competitor_auto_update").disabled)
        self.assertTrue(app.button(key="competitor_collect_now").disabled)
        self.assertEqual(app.text_area(key="competitor_sources").value, "")
        self.assertFalse(any(node.key == "competitor_selectors" for node in app.text_area))
        self.fetch.assert_not_called()

    def test_saved_template_and_filters_restore_without_losing_existing_source_selectors(self):
        source = {"url": "https://example.org/beauty", "keyword": "皮肤护理", "selectors": {"caption": "article::text"}}
        competitors.save_settings({"sources": [source]})
        app = self.app()
        app.selectbox(key="competitor_spoken_profile").set_value("beauty_spoken")
        app.number_input(key="competitor_min_text_length").set_value(120)
        app.checkbox(key="competitor_prefer_high_comment").set_value(False)
        app.text_input(key="competitor_city").set_value("成都")
        app = self.save(app)
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        saved = competitors.get_settings()
        self.assertEqual(saved["spoken_profile"], "beauty_spoken")
        self.assertEqual(saved["min_text_length"], 120)
        self.assertFalse(saved["prefer_high_comment"])
        self.assertEqual(saved["city"], "成都")
        self.assertEqual(saved["sources"][0]["selectors"], source["selectors"])
        self.assertEqual(saved["sources"][0]["keyword"], source["keyword"])
        restored = self.app()
        self.assertEqual(restored.selectbox(key="competitor_spoken_profile").value, "beauty_spoken")
        self.assertEqual(restored.number_input(key="competitor_min_text_length").value, 120)
        self.fetch.assert_not_called()

    def test_removing_sources_disables_automatic_updates_and_keeps_existing_records(self):
        competitors.save_settings({"sources": ["https://example.org/reference"], "auto_update": True})
        store.save_record("competitor_items", "kept", {"title": "既有口播", "public_caption": "此前的参考正文"})
        app = self.app()
        app.text_area(key="competitor_sources").set_value("")
        app = self.save(app)
        self.assertFalse(app.exception)
        self.assertEqual(competitors.get_settings()["sources"], [])
        self.assertFalse(competitors.get_settings()["auto_update"])
        self.assertIsNotNone(store.get_record("competitor_items", "kept"))
        self.assertTrue(app.button(key="competitor_collect_now").disabled)
        self.fetch.assert_not_called()

    def test_structured_cards_and_legacy_rows_show_original_content_without_invented_counts(self):
        rows = [{
            "title": "怎样准备一条口播", "hook_3s": "你是不是总卡在第一句话？",
            "bullet_points": ["先列出观众的问题", "再解释两条建议"],
            "material_clues": [{"type": "演示", "description": "写稿过程的近景", "source_text": "先列问题"}],
            "tags": ["问题清单", "写稿演示"], "spoken_script": "这是实际取得的原文，不是生成的转写。",
            "hot_metrics": {"like": None, "comment": 0, "collect": 7},
            "source_url": "https://example.org/spoken", "eligible": True,
        }, {"title": "旧参考", "public_caption": "此前保存的摘要", "likes": None}]
        with patch.object(competitors, "list_items", return_value=rows):
            app = self.app()
        displayed = self.displayed(app)
        for text in ("你是不是总卡在第一句话", "先列出观众的问题", "写稿过程的近景", "写稿演示", "这是实际取得的原文", "此前保存的摘要"):
            self.assertIn(text, displayed)
        self.assertIn("公开点赞：未取得", displayed)
        self.assertIn("公开评论数：0", displayed)
        self.assertIn("公开收藏数：7", displayed)
        self.assertIn("尚无口播拆解", displayed)
        self.assertTrue(any(node.proto.url == "https://example.org/spoken" for node in app.get("link_button")))

    def test_filtered_summary_and_record_explain_exclusion_instead_of_disappearing(self):
        run = {"state": "done", "message": "参考已更新", "filtered": 3,
               "filter_reasons": {"正文不足80字": 2, "非口播内容": 1}}
        item = {"title": "过短片段", "public_caption": "短摘要", "eligible": False, "filter_reasons": ["正文不足80字"]}
        with patch.object(competitors, "list_runs", return_value=[run]), patch.object(competitors, "list_items", return_value=[item]):
            app = self.app()
        displayed = self.displayed(app)
        self.assertIn("本次过滤 3 条", displayed)
        self.assertIn("正文不足80字：2 条", displayed)
        self.assertIn("非口播内容：1 条", displayed)
        self.assertIn("已过滤，仅保留来源记录：正文不足80字", displayed)
        self.fetch.assert_not_called()

    def test_manual_update_only_requests_background_collection(self):
        competitors.save_settings({"sources": ["https://example.org/spoken"]})
        app = self.app()
        with patch.object(competitors, "submit_collection", return_value="local-task") as submit:
            app.button(key="competitor_collect_now").click().run()
        self.assertFalse(app.exception)
        submit.assert_called_once_with()
        self.fetch.assert_not_called()

    def test_background_completion_refreshes_library_once_and_keeps_unsaved_form_edits(self):
        run = {"id": "background-run", "state": "running", "message": "正在读取参考", "progress": 10}
        items = []
        with patch.object(competitors, "list_runs", side_effect=lambda: [dict(run)]), patch.object(
            competitors, "list_items", side_effect=lambda _keyword="": list(items)
        ), patch.object(competitors, "submit_collection") as submit:
            app = self.app()
            app.text_input(key="competitor_city").set_value("未保存的城市")
            run.update(state="done", message="已更新口播参考", progress=100)
            items.append({"title": "新取得的口播", "spoken_script": "新取得的正文", "eligible": True})
            app.run()
            self.assertFalse(app.exception)
            self.assertIn("新取得的正文", self.displayed(app))
            self.assertEqual(app.text_input(key="competitor_city").value, "未保存的城市")
            self.assertNotIn("competitor_pending_run", app.session_state.filtered_state)
            app.run()
            self.assertFalse(app.exception)
            submit.assert_not_called()
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
