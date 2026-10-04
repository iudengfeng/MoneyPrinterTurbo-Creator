"""Exercise the manual's first step without using real user data or model calls."""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services.creator import jobs, store, topics


class ScriptWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.directory.name})
        self.environment.start()
        self.model_guard = patch("app.services.creator.topics._generate", side_effect=AssertionError("Unexpected model request"))
        self.model = self.model_guard.start()

    def tearDown(self):
        self.model_guard.stop()
        self.environment.stop()
        self.directory.cleanup()

    def app(self):
        app = AppTest.from_string(
            "from webui.creator_script_workspace import render\nrender()",
            default_timeout=30,
        ).run()
        self.assertFalse(app.exception)
        return app

    def collect_pending(self, app):
        deadline = time.monotonic() + 10
        while "creator_script_pending" in app.session_state:
            pending = app.session_state["creator_script_pending"]
            job = jobs.get_job(pending["id"])
            if job and job["state"] not in ("queued", "running"):
                app.run()
                break
            self.assertLess(time.monotonic(), deadline, "Background script task did not finish")
            time.sleep(0.02)
        self.assertFalse(app.exception)

    def field(self, app, group, label):
        return next(item for item in getattr(app, group) if item.label == label)

    def button(self, app, label):
        return self.field(app, "button", label)

    def view(self, app, name):
        app.radio(key="creator_script_view").set_value(name).run()
        self.assertFalse(app.exception, name)

    def reference(self):
        return topics.save_reference(
            "孩子不愿意开口，先改变提问方式",
            "不要只问今天学了什么。试试请孩子讲一个最有意思的小发现，再耐心听他说完。",
            keyword="教育", source_url="https://example.com/reference", author="测试参考作者",
        )

    def test_default_first_step_shows_keyword_search(self):
        app = self.app()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="creator_script_view").value, "关键词选题")
        self.assertTrue(self.field(app, "text_input", "行业或赛道关键词"))
        self.assertTrue(self.button(app, "搜索参考库"))
        self.assertTrue(self.button(app, "AI 拓展选题"))
        self.assertEqual(topics.list_drafts(), [])
        self.model.assert_not_called()

    def test_reference_import_persists_source_and_opens_full_text(self):
        app = self.app()
        self.view(app, "参考文案")
        original = "讲解一个知识点之前，先用孩子熟悉的生活场景引出问题，再让孩子尝试解释。"
        self.field(app, "text_input", "参考标题").set_value("让孩子听得懂的解释")
        self.field(app, "text_input", "行业关键词").set_value("教育")
        self.field(app, "text_area", "参考原文").set_value(original)
        self.field(app, "text_input", "来源链接（可选）").set_value("https://example.com/source")
        self.field(app, "text_input", "作者（可选）").set_value("参考作者")
        self.button(app, "保存到参考库").click().run()
        self.assertFalse(app.exception)
        rows = topics.list_references("教育")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], original)
        self.assertEqual(rows[0]["source_url"], "https://example.com/source")
        self.assertEqual(rows[0]["author"], "参考作者")
        self.assertEqual(rows[0]["source"], "local")
        app.button(key="script_view_" + rows[0]["id"]).click().run()
        self.assertFalse(app.exception)
        full_text = app.text_area(key="creator_reference_full_" + rows[0]["id"])
        self.assertEqual(full_text.value, original)
        self.assertTrue(full_text.disabled)
        self.model.assert_not_called()

    def test_original_and_imitation_preserve_reference_and_allow_style_selection(self):
        reference = self.reference()
        app = self.app()
        self.view(app, "参考文案")
        app.button(key="script_original_" + reference["id"]).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="creator_script_view").value, "文案编辑")
        self.assertEqual(app.text_input(key="creator_script_title").value, reference["title"])
        self.assertEqual(app.session_state["creator_script_context"]["mode"], "original")
        self.assertEqual(app.session_state["creator_script_context"]["reference_text"], reference["text"])
        styles = app.selectbox(key="creator_script_style")
        self.assertEqual(styles.options, list(topics.STYLE_PRESETS))
        styles.set_value("故事共鸣").run()
        self.assertEqual(app.selectbox(key="creator_script_style").value, "故事共鸣")
        self.view(app, "参考文案")
        app.button(key="script_imitate_" + reference["id"]).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["creator_script_context"]["mode"], "imitate")
        self.assertEqual(app.session_state["creator_script_context"]["reference_text"], reference["text"])
        self.model.assert_not_called()

    def test_direct_script_save_and_continue_edit_restore_saved_text(self):
        app = self.app()
        self.button(app, "直接写文案").click().run()
        self.assertFalse(app.exception)
        title = "新手整理书桌的三个步骤"
        text = "先把桌面上的物品分成常用和不常用两组。常用的放在伸手就能拿到的位置，其他物品收进抽屉。"
        app.text_input(key="creator_script_title").set_value(title)
        app.text_area(key="creator_script_editor").set_value(text)
        app.selectbox(key="creator_script_style").set_value("清单教程")
        self.button(app, "保存文案").click().run()
        self.assertFalse(app.exception)
        drafts = topics.list_drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["title"], title)
        self.assertEqual(drafts[0]["text"], text)
        self.assertEqual(drafts[0]["style"], "清单教程")
        app.text_area(key="creator_script_editor").set_value("尚未保存的修改").run()
        self.view(app, "我的文案")
        app.button(key="creator_draft_open_" + drafts[0]["id"]).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area(key="creator_script_editor").value, text)
        self.assertEqual(app.text_input(key="creator_script_title").value, title)
        self.assertEqual(app.selectbox(key="creator_script_style").value, "清单教程")
        self.model.assert_not_called()

    def test_confirm_script_saves_and_hands_exact_text_to_voice_and_avatar(self):
        app = self.app()
        self.button(app, "直接写文案").click().run()
        title = "把今天的一个小发现讲清楚"
        text = "今天我发现，把一个大目标拆成很小的行动，开始做就没有那么难了。先完成第一步，再决定下一步。"
        app.text_input(key="creator_script_title").set_value(title)
        app.text_area(key="creator_script_editor").set_value(text)
        self.button(app, "确认文案，下一步配音 →").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["creator_selected_tab"], "配音")
        self.assertTrue(app.session_state["creator_navigation_pending"])
        self.assertEqual(app.session_state["creator_voice_text"], text)
        self.assertEqual(app.session_state["creator_duix_script"], text)
        self.assertEqual(topics.list_drafts()[0]["text"], text)
        self.model.assert_not_called()

    def test_completed_background_draft_is_loaded_into_editor(self):
        app = self.app()
        text = "与其一次讲很多知识点，不如先讲清一个具体问题，再用一个贴近日常生活的小例子帮助对方理解。"
        draft = topics.save_draft("一次讲清一个问题", text, style="科普干货")
        job_id = store.new_id()
        store.save_record("jobs", job_id, {
            "label": "生成文案", "state": "done", "message": "已完成", "progress": 100, "result": draft,
        })
        app.session_state["creator_script_pending"] = {"id": job_id, "action": "draft"}
        app.run()
        self.assertFalse(app.exception)
        self.assertNotIn("creator_script_pending", app.session_state)
        self.assertEqual(app.radio(key="creator_script_view").value, "文案编辑")
        self.assertEqual(app.session_state["creator_script_draft_id"], draft["id"])
        self.assertEqual(app.text_area(key="creator_script_editor").value, text)
        self.assertEqual(app.text_input(key="creator_script_title").value, draft["title"])
        self.assertTrue(any("文案已生成并保存" in notice.value for notice in app.success))

    def test_unsaved_editor_text_and_controls_survive_navigation(self):
        app = self.app()
        self.button(app, "直接写文案").click().run()
        text = "这是尚未保存的完整稿件。去参考库查看资料，再返回编辑时不能丢失。"
        title = "保留当前未完成的创作"
        app.text_input(key="creator_script_title").set_value(title).run()
        app.text_area(key="creator_script_editor").set_value(text).run()
        app.selectbox(key="creator_script_style").set_value("故事共鸣").run()
        self.assertEqual(app.session_state["creator_script_editor_buffer"], text)
        self.view(app, "参考文案")
        self.view(app, "文案编辑")
        self.assertEqual(app.text_area(key="creator_script_editor").value, text)
        self.assertEqual(app.text_input(key="creator_script_title").value, title)
        self.assertEqual(app.selectbox(key="creator_script_style").value, "故事共鸣")
        app.text_area(key="creator_script_editor").set_value("").run()
        self.view(app, "关键词选题")
        self.view(app, "文案编辑")
        self.assertEqual(app.text_area(key="creator_script_editor").value, "")
        self.assertEqual(app.session_state["creator_script_editor_buffer"], "")
        self.assertEqual(topics.list_drafts(), [])
        self.model.assert_not_called()

    def test_generate_click_uses_real_jobs_and_failed_regeneration_keeps_saved_edits(self):
        app = self.app()
        self.button(app, "直接写文案").click().run()
        title = "先观察问题，再给孩子建议"
        generated = "孩子遇到困难时，先别急着给答案。请他描述刚才发生了什么，再一起把问题拆成一个个小步骤，让他选择最想先试的一步。"
        app.text_input(key="creator_script_title").set_value(title)
        with patch("app.services.creator.topics._generate", return_value=generated) as model:
            app.button(key="creator_script_generate").click().run()
            self.collect_pending(app)
            model.assert_called_once()
        self.assertEqual(app.text_area(key="creator_script_editor").value, generated)
        draft_id = app.session_state["creator_script_draft_id"]
        self.assertEqual(store.get_record("drafts", draft_id)["text"], generated)
        self.assertEqual(jobs.list_jobs()[0]["state"], "done")

        edited = generated + "把最后一步改得更具体，便于马上开始。"
        # Commit the textarea change before clicking a button, matching browser
        # blur/change. AppTest can lose an uncommitted local widget value when
        # a button's script requests a second rerun in the same simulated event.
        app.text_area(key="creator_script_editor").set_value(edited).run()
        with patch("app.services.creator.topics._generate", side_effect=topics.TopicGenerationError("测试模型连接失败，已有文案保留")) as model:
            app.button(key="creator_script_generate").click().run()
            self.collect_pending(app)
            model.assert_called_once()
        self.assertEqual(app.text_area(key="creator_script_editor").value, edited)
        self.assertEqual(store.get_record("drafts", draft_id)["text"], edited)
        self.assertEqual(jobs.list_jobs()[0]["state"], "failed")
        self.assertTrue(any("测试模型连接失败" in notice.value for notice in app.error))

    def test_failed_background_generation_preserves_current_and_saved_draft(self):
        saved = "这是已经保存的稿件，需要在重新生成失败时完整保留。"
        edited = saved + "这一句是当前编辑器中的未保存修改。"
        draft = topics.save_draft("应该保留的稿件", saved)
        app = self.app()
        self.view(app, "我的文案")
        app.button(key="creator_draft_open_" + draft["id"]).click().run()
        app.text_area(key="creator_script_editor").set_value(edited).run()
        job_id = store.new_id()
        store.save_record("jobs", job_id, {
            "label": "重新生成文案", "state": "failed", "message": "模型暂时无法连接，请稍后重试。", "progress": 10,
        })
        app.session_state["creator_script_pending"] = {"id": job_id, "action": "draft"}
        app.run()
        self.assertFalse(app.exception)
        self.assertNotIn("creator_script_pending", app.session_state)
        self.assertEqual(app.text_area(key="creator_script_editor").value, edited)
        self.assertEqual(store.get_record("drafts", draft["id"])["text"], saved)
        self.assertTrue(any("模型暂时无法连接" in notice.value for notice in app.error))
        self.assertFalse(app.button(key="creator_script_generate").disabled)
        self.model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
