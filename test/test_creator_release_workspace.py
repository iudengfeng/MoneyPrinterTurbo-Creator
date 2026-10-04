"""Verify fifth-step provenance, asynchronous outcomes and publishing handoff."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services.creator import jobs, store


_STYLES = [{"id": ident, "name": name, "description": "实际可生成的样式"} for ident, name in
           (("clean", "清爽留白"), ("bold", "醒目大字"), ("knowledge", "知识讲解"), ("business", "商务简洁"))]
_SCRIPT = "这是本次视频的真实口播内容，完成选题和剪辑后，我们继续准备标题和封面。"


class ReleaseWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.directory.name})
        self.environment.start()
        self.video = self.file("finished.mp4")
        self.other = self.file("another.mp4")
        self.cover = self.file("cover.png")
        self.renders = []
        self.materials = []
        self.patchers = [
            patch("app.services.creator.release_assets.list_styles", return_value=_STYLES),
            patch("app.services.creator.rendering.list_renders", side_effect=lambda: self.renders),
            patch("app.services.creator.rendering.probe_source", side_effect=self.probe),
            patch("app.services.creator.release_assets.list_materials", side_effect=lambda: self.materials),
            patch("app.services.creator.release_assets.generate_metadata", side_effect=AssertionError("Unexpected title generation")),
            patch("app.services.creator.release_assets.generate_cover", side_effect=AssertionError("Unexpected cover generation")),
            patch("app.services.creator.extract.extract_media", side_effect=AssertionError("Unexpected extraction")),
            patch("webui.creator_release_workspace._config_snapshot", return_value={"llm_provider": "test-provider", "custom_config": {"setting": 7}}),
        ]
        (self.styles_mock, self.renders_mock, self.probe_mock, self.materials_mock, self.metadata_mock,
         self.cover_mock, self.extract_mock, self.config_mock) = [item.start() for item in self.patchers]

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.environment.stop()
        self.directory.cleanup()

    def file(self, name):
        path = store.data_root() / name
        if path.suffix == ".png":
            from PIL import Image
            Image.new("RGB", (80, 120), "navy").save(path)
        else:
            path.write_bytes(b"opaque-media-for-ui-only")
        return str(path)

    def probe(self, path):
        return {"path": path, "duration": 7.32, "width": 1280, "height": 720, "has_audio": True, "has_video": True}

    def app(self, current=True, **state):
        app = AppTest.from_string("""import streamlit as st
from webui.creator_release_workspace import render, load_video
page = st.radio('测试导航', ['标题和封面', '配音'], key='test_page')
if st.session_state.pop('load_this_video', False):
    load_video(st.session_state['test_video_row'], text=st.session_state['test_video_text'])
if page == '标题和封面':
    render()
else:
    st.write('配音测试页')
""", default_timeout=30)
        if current:
            app.session_state["creator_release_video"] = self.video
            app.session_state["creator_release_current"] = {"video_path": self.video, "title": "本次成片", "aspect": "16:9"}
            app.session_state["creator_release_text"] = _SCRIPT
        for key, value in state.items():
            app.session_state[key] = value
        app.run()
        self.assertFalse(app.exception)
        return app

    def collect(self, app):
        deadline = time.monotonic() + 10
        while "creator_release_pending" in app.session_state:
            row = jobs.get_job(app.session_state["creator_release_pending"]["id"])
            if row and row["state"] not in {"running", "queued"}:
                app.run()
                break
            self.assertLess(time.monotonic(), deadline, "Background work did not finish")
            time.sleep(0.02)
        notices = {"error": [item.value for item in app.error], "success": [item.value for item in app.success]}
        # AppTest accumulates deltas from an internal st.rerun in one tree.
        # When the completion notice moves the page layout, that tree can hold
        # two copies of the same widget id; its old copy then overwrites the
        # next simulated edit. Refresh once after collection to represent the
        # browser's settled page, and require a unique, editable widget tree.
        app.run()
        self.assertFalse(app.exception)
        self.assertNotIn("creator_release_pending", app.session_state)
        ids = [item.id for item in app.text_input]
        self.assertEqual(len(ids), len(set(ids)), "Completed UI contains duplicate widget ids")
        self.assertFalse(app.text_input(key="creator_release_cover_title").disabled)
        return notices

    def make_cover(self, video_path, title, **kwargs):
        return {"id": "new-cover", "state": "done", "cover_path": self.cover, "title": title, "source_video_path": video_path,
                "style": kwargs["style"], "aspect": kwargs["aspect"], "frame_time": kwargs["frame_time"], "width": 1280, "height": 720}

    def save_materials(self, video_path, title, **kwargs):
        ident = store.new_id()
        description = kwargs.get("description", "")
        tags = kwargs.get("hashtags") or []
        return {"id": ident, "state": "done", "video_path": video_path, "title": title, **kwargs,
                "publish_description": description + ("\n" if description and tags else "") + " ".join("#" + tag for tag in tags)}

    def test_empty_workbench_offers_manual_controls_without_creating_tasks(self):
        app = self.app(current=False, creator_voice_draft="旧配音稿绝不能借来")
        self.assertEqual(app.text_area(key="creator_release_text").value, "")
        self.assertTrue(app.button(key="creator_release_generate_metadata").disabled)
        self.assertTrue(app.button(key="creator_release_generate_cover").disabled)
        self.assertTrue(app.button(key="creator_release_publish").disabled)
        self.assertFalse(app.button(key="creator_release_style_bold").disabled)
        self.metadata_mock.assert_not_called()
        self.cover_mock.assert_not_called()
        self.extract_mock.assert_not_called()

    def test_explicit_handoff_replaces_earlier_source_drafts_and_uploads(self):
        app = self.app(creator_release_cover_result={"cover_path": self.cover},
                       creator_release_cover_import={"path": self.cover, "name": "old"},
                       creator_release_title_buffer="旧标题", creator_release_text_buffer="旧文案")
        app.session_state["test_video_row"] = {"video_path": self.other, "title": "新成片", "aspect": "9:16"}
        app.session_state["test_video_text"] = "这是新成片的真实文案。"
        app.session_state["load_this_video"] = True
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area(key="creator_release_text").value, "这是新成片的真实文案。")
        self.assertEqual(app.text_input(key="creator_release_title").value, "新成片")
        self.assertNotIn("creator_release_cover_result", app.session_state)
        self.assertNotIn("creator_release_cover_import", app.session_state)
        self.assertEqual(app.selectbox(key="creator_release_aspect").value, "9:16")

    def test_ai_generation_uses_confirmed_text_and_frozen_configuration(self):
        app = self.app()
        self.metadata_mock.side_effect = None
        self.metadata_mock.return_value = {"titles": ["选题到发布的下一步", "成片完成，素材这样准备", "标题封面准备好了"],
                                           "description": "根据真实视频生成的正文。", "hashtags": ["创作记录", "知识分享"], "cover_title": "成片的下一步"}
        app.button(key="creator_release_generate_metadata").click().run()
        self.collect(app)
        self.metadata_mock.assert_called_once()
        self.assertEqual(self.metadata_mock.call_args.args, (_SCRIPT,))
        self.assertEqual(self.metadata_mock.call_args.kwargs["app_config"], {"llm_provider": "test-provider", "custom_config": {"setting": 7}})
        self.assertEqual(app.text_input(key="creator_release_title").value, "选题到发布的下一步")
        self.assertEqual(app.text_input(key="creator_release_hashtags").value, "#创作记录 #知识分享")
        app.button(key="creator_release_candidate_1").click().run()
        self.assertEqual(app.text_input(key="creator_release_title").value, "成片完成，素材这样准备")
        self.assertEqual(app.text_area(key="creator_release_text").value, _SCRIPT)

    def test_generation_failure_preserves_previous_cover_and_manual_edits(self):
        app = self.app(creator_release_title="手工标题", creator_release_cover_result=self.make_cover(self.video, "本次成片", style="clean", aspect="16:9", frame_time=0))
        self.metadata_mock.side_effect = RuntimeError("模型连接失败，请检查设置。")
        app.button(key="creator_release_regenerate_metadata").click().run()
        notices = self.collect(app)
        self.assertEqual(app.text_input(key="creator_release_title").value, "手工标题")
        self.assertEqual(app.session_state["creator_release_cover_result"]["cover_path"], self.cover)
        self.assertTrue(any("模型连接失败" in value for value in notices["error"]))

    def test_switching_to_another_video_clears_unrelated_copy_and_cover(self):
        self.renders = [{"id": "other", "video_path": self.other, "title": "另一份成片", "aspect": "9:16"}]
        app = self.app(creator_release_cover_result=self.make_cover(self.video, "本次成片", style="clean", aspect="16:9", frame_time=0),
                       creator_release_description="本次正文", creator_release_cover_import={"path": self.cover, "name": "old"})
        app.radio(key="creator_release_source").set_value("历史成片").run()
        self.assertEqual(app.text_area(key="creator_release_text").value, "")
        self.assertNotIn("creator_release_cover_result", app.session_state)
        app.selectbox(key="creator_release_history_id").select("other").run()
        self.assertEqual(app.text_area(key="creator_release_text").value, "")
        self.assertEqual(app.text_area(key="creator_release_description").value, "")
        self.assertEqual(app.text_input(key="creator_release_title").value, "另一份成片")
        self.assertNotIn("creator_release_cover_result", app.session_state)
        self.assertNotIn("creator_release_cover_import", app.session_state)
        self.assertTrue(app.button(key="creator_release_publish").disabled)

    def test_generated_cover_parameters_and_stale_preview_prevent_wrong_save(self):
        app = self.app()
        app.text_input(key="creator_release_cover_title").set_value("封面重点").run()
        app.slider(key="creator_release_frame_time").set_value(2.1).run()
        app.button(key="creator_release_style_bold").click().run()
        self.cover_mock.side_effect = self.make_cover
        app.button(key="creator_release_generate_cover").click().run()
        self.collect(app)
        self.cover_mock.assert_called_once()
        self.assertEqual(self.cover_mock.call_args.args, (self.video, "封面重点"))
        self.assertEqual(self.cover_mock.call_args.kwargs["frame_time"], 2.1)
        self.assertEqual(self.cover_mock.call_args.kwargs["style"], "bold")
        self.assertFalse(app.text_input(key="creator_release_cover_title").disabled)
        self.assertFalse(app.button(key="creator_release_save").disabled)
        app.text_input(key="creator_release_cover_title").set_value("修改后的封面重点").run()
        self.assertEqual(app.text_input(key="creator_release_cover_title").value, "修改后的封面重点")
        self.assertTrue(app.button(key="creator_release_save").disabled)
        self.assertTrue(any("之前生成的封面" in row.value for row in app.warning))
        app.text_input(key="creator_release_cover_title").set_value("封面重点").run()
        self.assertFalse(app.button(key="creator_release_save").disabled)

    def test_cover_failure_keeps_previous_image(self):
        app = self.app(creator_release_cover_result=self.make_cover(self.video, "本次成片", style="clean", aspect="16:9", frame_time=0))
        self.cover_mock.side_effect = RuntimeError("截取封面没有完成。")
        app.button(key="creator_release_generate_cover").click().run()
        notices = self.collect(app)
        self.assertEqual(app.session_state["creator_release_cover_result"]["cover_path"], self.cover)
        self.assertTrue(any("截取封面没有完成" in value for value in notices["error"]))
        self.assertFalse(app.button(key="creator_release_save").disabled)

    def test_manual_no_cover_handoff_clears_old_publishing_fields_without_accounts(self):
        app = self.app(creator_publish_title="过期标题", creator_publish_description="旧话题", creator_publish_cover=self.cover,
                       creator_publish_cover_path=self.cover, creator_publish_video_upload="old-upload", creator_publish_cover_import={"path": self.cover})
        app.radio(key="creator_release_cover_mode").set_value("暂不使用封面").run()
        app.text_input(key="creator_release_title").set_value("本次精确标题").run()
        with patch("app.services.creator.release_assets.save_materials", side_effect=self.save_materials) as save:
            app.button(key="creator_release_publish").click().run()
            save.assert_called_once()
            self.assertEqual(save.call_args.args, (self.video, "本次精确标题"))
            self.assertIsNone(save.call_args.kwargs["cover_path"])
        self.assertEqual(app.session_state["creator_publish_video"], self.video)
        self.assertEqual(app.session_state["creator_publish_title"], "本次精确标题")
        self.assertEqual(app.session_state["creator_publish_description"], "")
        self.assertEqual(app.session_state["creator_publish_cover"], "")
        self.assertEqual(app.session_state["creator_publish_cover_path"], "")
        self.assertEqual(app.session_state["creator_selected_tab"], "发布中心")
        self.assertNotIn("creator_publish_video_upload", app.session_state)
        self.assertNotIn("creator_publish_cover_import", app.session_state)

    def test_uploaded_cover_and_topics_are_handed_off_exactly(self):
        app = self.app(creator_release_cover_mode="上传自己的封面", creator_release_cover_import={"path": self.cover, "name": "我的封面.png"})
        app.text_area(key="creator_release_description").set_value("本次发布正文").run()
        app.text_input(key="creator_release_hashtags").set_value("#创作记录 #知识分享").run()
        self.assertFalse(app.button(key="creator_release_publish").disabled)
        with patch("app.services.creator.release_assets.save_materials", side_effect=self.save_materials) as save:
            app.button(key="creator_release_publish").click().run()
            self.assertEqual(save.call_args.kwargs["cover_path"], self.cover)
            self.assertEqual(save.call_args.kwargs["source_text"], _SCRIPT)
        self.assertEqual(app.session_state["creator_publish_cover_path"], self.cover)
        self.assertEqual(app.session_state["creator_publish_description"], "本次发布正文\n#创作记录 #知识分享")

    def test_navigation_and_widget_cleanup_preserve_intentional_empty_drafts(self):
        app = self.app()
        app.text_input(key="creator_release_title").set_value("保存后的标题").run()
        app.text_area(key="creator_release_text").set_value("").run()
        app.text_input(key="creator_release_cover_title").set_value("").run()
        app.radio(key="creator_release_cover_mode").set_value("暂不使用封面").run()
        app.radio(key="test_page").set_value("配音").run()
        for key in ("creator_release_title", "creator_release_text", "creator_release_cover_title", "creator_release_cover_mode"):
            del app.session_state[key]
        app.radio(key="test_page").set_value("标题和封面").run()
        self.assertEqual(app.text_input(key="creator_release_title").value, "保存后的标题")
        self.assertEqual(app.text_area(key="creator_release_text").value, "")
        self.assertEqual(app.text_input(key="creator_release_cover_title").value, "")
        self.assertEqual(app.radio(key="creator_release_cover_mode").value, "暂不使用封面")

    def test_extraction_reads_selected_finished_video_and_never_old_narration(self):
        app = self.app(creator_release_text="", creator_voice_draft="此前配音旧文案")
        self.extract_mock.side_effect = None
        self.extract_mock.return_value = {"text": "这是从当前成片音轨识别出的口播文案。", "media_path": self.video}
        app.button(key="creator_release_extract").click().run()
        self.collect(app)
        self.assertEqual(self.extract_mock.call_args.args, (self.video,))
        self.assertEqual(app.text_area(key="creator_release_text").value, "这是从当前成片音轨识别出的口播文案。")

    def test_silent_video_accepts_manual_copy_but_cannot_extract_audio(self):
        self.probe_mock.side_effect = lambda path: dict(self.probe(path), has_audio=False)
        app = self.app()
        self.assertTrue(app.button(key="creator_release_extract").disabled)
        self.assertFalse(app.button(key="creator_release_generate_metadata").disabled)
        self.assertFalse(app.button(key="creator_release_generate_cover").disabled)
        self.extract_mock.assert_not_called()

    def test_restoring_saved_materials_uses_saved_source_text_and_actual_cover(self):
        self.materials = [{"id": "saved", "video_path": self.other, "title": "存档标题", "description": "存档正文", "hashtags": ["存档话题"],
                           "source_text": "存档成片的真实文案", "cover_path": self.cover, "publish_description": "存档正文\n#存档话题"}]
        app = self.app()
        app.button(key="creator_release_restore_saved").click().run()
        self.assertEqual(app.session_state["creator_release_video"], self.other)
        self.assertEqual(app.text_area(key="creator_release_text").value, "存档成片的真实文案")
        self.assertEqual(app.text_input(key="creator_release_title").value, "存档标题")
        self.assertEqual(app.radio(key="creator_release_cover_mode").value, "上传自己的封面")
        self.assertEqual(app.session_state["creator_release_cover_import"]["path"], self.cover)
        self.assertFalse(app.button(key="creator_release_publish").disabled)

    def test_navigation_and_control_edits_reuse_unchanged_media_probe(self):
        app = self.app()
        app.text_input(key="creator_release_title").set_value("新标题").run()
        app.radio(key="test_page").set_value("配音").run()
        app.radio(key="test_page").set_value("标题和封面").run()
        self.assertEqual(self.probe_mock.call_count, 1)

    def test_historical_source_survives_navigation_and_widget_cleanup(self):
        self.renders = [{"id": "other", "video_path": self.other, "title": "历史作品", "aspect": "9:16"}]
        app = self.app()
        app.radio(key="creator_release_source").set_value("历史成片").run()
        app.selectbox(key="creator_release_history_id").select("other").run()
        app.text_area(key="creator_release_text").set_value("当前历史作品的手工确认文案。").run()
        app.radio(key="test_page").set_value("配音").run()
        del app.session_state["creator_release_history_id"]
        app.radio(key="test_page").set_value("标题和封面").run()
        self.assertEqual(app.selectbox(key="creator_release_history_id").value, "other")
        self.assertEqual(app.text_area(key="creator_release_text").value, "当前历史作品的手工确认文案。")

    def test_running_job_disables_source_cover_and_draft_changes(self):
        pending = {"id": "busy-task", "kind": "cover", "source_video_path": self.video}
        with patch("app.services.creator.jobs.get_job", return_value={"state": "running", "progress": 40, "message": "正在生成封面"}):
            app = self.app(creator_release_pending=pending)
            self.assertTrue(app.radio(key="creator_release_source").disabled)
            self.assertTrue(app.text_input(key="creator_release_cover_title").disabled)
            self.assertTrue(app.text_area(key="creator_release_text").disabled)
            self.assertTrue(app.slider(key="creator_release_frame_time").disabled)
            self.assertTrue(app.button(key="creator_release_style_bold").disabled)
            self.assertTrue(app.button(key="creator_release_publish").disabled)

    def test_finished_job_from_another_source_cannot_replace_current_draft(self):
        app = self.app()
        app.session_state["creator_release_pending"] = {"id": "old-task", "kind": "metadata", "source_video_path": self.other}
        old = {"state": "done", "result": {"titles": ["不相关旧标题"], "description": "不相关旧正文", "cover_title": "旧封面文字", "hashtags": ["旧话题"]}}
        with patch("app.services.creator.jobs.get_job", return_value=old):
            app.run()
        self.assertEqual(app.text_input(key="creator_release_title").value, "本次成片")
        self.assertEqual(app.text_area(key="creator_release_description").value, "")
        self.assertNotIn("creator_release_metadata_result", app.session_state)

    def test_job_finishing_between_collection_and_progress_preserves_next_edit(self):
        app = self.app()
        app.text_input(key="creator_release_cover_title").set_value("封面重点").run()
        app.session_state["creator_release_pending"] = {"id": "race-task", "kind": "cover", "source_video_path": self.video}
        complete = {"state": "done", "result": self.make_cover(self.video, "封面重点", style="clean", aspect="16:9", frame_time=0)}
        responses = [{"state": "running", "progress": 99, "message": "即将完成"}, complete]
        with patch("app.services.creator.jobs.get_job", side_effect=lambda ident: responses.pop(0) if responses else complete):
            app.run()
        self.collect(app)
        self.assertNotIn("creator_release_pending", app.session_state)
        self.assertFalse(app.text_input(key="creator_release_cover_title").disabled)
        app.text_input(key="creator_release_cover_title").set_value("完成后修改的封面重点").run()
        self.assertEqual(app.text_input(key="creator_release_cover_title").value, "完成后修改的封面重点")
        self.assertTrue(app.button(key="creator_release_save").disabled)

    def test_save_failure_keeps_editable_material_and_does_not_navigate(self):
        app = self.app(creator_release_cover_mode="上传自己的封面", creator_release_cover_import={"path": self.cover, "name": "封面.png"})
        with patch("app.services.creator.release_assets.save_materials", side_effect=ValueError("封面校验未通过，请重新上传。")):
            app.button(key="creator_release_publish").click().run()
        self.assertEqual(app.text_input(key="creator_release_title").value, "本次成片")
        self.assertEqual(app.session_state["creator_release_cover_import"]["path"], self.cover)
        self.assertNotIn("creator_selected_tab", app.session_state)
        self.assertTrue(any("封面校验未通过" in row.value for row in app.error))

    def test_publish_callback_routes_before_shared_sidebar_and_reads_latest_edit(self):
        app = AppTest.from_string("""import streamlit as st
from webui.creator_release_workspace import render
page = st.sidebar.radio('工作流程', ['标题和封面', '发布中心'], key='creator_selected_tab')
if page == '标题和封面':
    render()
else:
    st.write(st.session_state['creator_publish_title'])
    st.write(st.session_state['creator_publish_description'])
""", default_timeout=30)
        app.session_state["creator_release_video"] = self.video
        app.session_state["creator_release_text"] = _SCRIPT
        app.session_state["creator_release_title"] = "之前的标题"
        app.session_state["creator_release_description"] = "之前的正文"
        app.session_state["creator_release_cover_mode"] = "暂不使用封面"
        app.run()
        self.assertFalse(app.exception)
        # Deliver edits together with the button event. A callback using the
        # previous render's kwargs would silently hand off the old text here.
        app.text_input(key="creator_release_title").set_value("点击时的新标题")
        app.text_area(key="creator_release_description").set_value("")
        with patch("app.services.creator.release_assets.save_materials", side_effect=self.save_materials) as save:
            app.button(key="creator_release_publish").click().run()
            save.assert_called_once()
            self.assertEqual(save.call_args.args, (self.video, "点击时的新标题"))
            self.assertEqual(save.call_args.kwargs["description"], "")
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="creator_selected_tab").value, "发布中心")
        self.assertEqual(app.session_state["creator_publish_title"], "点击时的新标题")
        self.assertEqual(app.session_state["creator_publish_description"], "")
        self.assertEqual(app.session_state["creator_publish_cover_path"], "")


if __name__ == "__main__":
    unittest.main()
