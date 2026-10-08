import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from app.services.creator import topics, publishing, extract
from webui import creator_workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{"MPT_CREATOR_DATA":self.directory.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def video(self, name="test.mp4"):
        path = Path(self.directory.name) / name
        subprocess.run([extract.ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "color=c=blue:s=96x160:r=10:d=0.4",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                       check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return path

    def go(self, app, page):
        category = creator_workspace.PAGE_CATEGORY[page]
        if app.radio(key="creator_category").value != category:
            app.radio(key="creator_category").set_value(category).run()
        if len(creator_workspace.CATEGORY_PAGES[category]) > 1:
            app.radio(key="creator_selected_tab").set_value(page).run()
        self.assertFalse(app.exception)

    def test_workspace_steps_render_without_exceptions(self):
        app=AppTest.from_string("from webui.creator_workspace import render\nrender()",default_timeout=30)
        with patch("app.services.creator.duix.list_profiles",return_value={"models":[{"id":1,"name":"测试人物"}],"voices":[{"id":1,"name":"测试音色"}]}):
            app.run()
            self.assertFalse(app.exception)
            for step in ["账号选题","声音与数字人","模板剪辑","标题和封面","发布中心","文案提取"]:
                self.go(app, step)
                self.assertFalse(app.exception,step)

    def test_account_form_saves_persistent_account(self):
        app=AppTest.from_string("from webui.creator_workspace import render\nrender()",default_timeout=30).run()
        self.go(app, "账号选题")
        for label,value in {"账号名称":"教程账号","行业或领域":"软件教程","目标受众":"新手","账号定位":"帮助新手学会软件"}.items():
            next(item for item in app.text_input if item.label==label).set_value(value) if label!="账号定位" else None
        next(item for item in app.text_area if item.label=="账号定位").set_value("帮助新手学会软件")
        next(item for item in app.button if item.label=="保存账号定位").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(topics.list_accounts()[0]["name"],"教程账号")

    def test_publishing_preview_is_visible_and_does_not_execute(self):
        account=publishing.save_account("douyin","测试发布账号")
        video=self.video()
        publishing.prepare_publish(str(video),"测试标题","测试正文",[account["id"]])
        app=AppTest.from_string("from webui.creator_workspace import render\nrender()",default_timeout=30).run()
        with patch("app.services.creator.publishing.execute_publish") as execute:
            self.go(app, "发布中心")
            self.assertFalse(app.exception)
            self.assertTrue(any(button.label=="确认发布到该账号" for button in app.button))
            execute.assert_not_called()
        self.assertEqual(publishing.list_tasks()[0]["status"],"prepared")

    def test_task_video_handoff_clears_previous_replacement_audio(self):
        app = AppTest.from_string(
            "import streamlit as st\n"
            "from webui.creator_workspace import _use_video\n"
            "st.button('使用成片', on_click=_use_video, args=({'video_path':'new-final.mp4',"
            "'clean_video_path':'new-clean.mp4','aspect':'16:9','subtitles_burned':False},))\n",
            default_timeout=30,
        ).run()
        app.session_state["creator_render_audio"] = "older-narration.wav"
        app.session_state["creator_srt_path"] = "older-subtitles.srt"
        app.session_state["creator_render_audio_upload"] = "older-upload"
        app.session_state["creator_render_replace_audio"] = True
        app.session_state["creator_render_audio_import"] = "older-staged-audio"
        app.session_state["creator_render_subtitle_import"] = "older-staged-subtitles"
        app.session_state["creator_render_material_fingerprint"] = "older-video"
        app.session_state["creator_render_source"] = "历史口播"
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["creator_video_path"], "new-clean.mp4")
        self.assertEqual(app.session_state["creator_publish_video"], "new-final.mp4")
        self.assertEqual(app.session_state["creator_render_audio"], "")
        self.assertEqual(app.session_state["creator_srt_path"], "")
        self.assertNotIn("creator_render_audio_upload", app.session_state)
        self.assertEqual(app.session_state["creator_render_aspect"], "16:9")
        self.assertFalse(app.session_state["creator_render_replace_audio"])
        self.assertEqual(app.session_state["creator_render_source"], "本次视频")
        self.assertNotIn("creator_render_material_fingerprint", app.session_state)
        self.assertNotIn("creator_render_audio_import", app.session_state)
        self.assertNotIn("creator_render_subtitle_import", app.session_state)
        self.assertEqual(app.session_state["creator_selected_tab"], "模板剪辑")

    def test_finished_video_is_visible_before_first_publish_account(self):
        video = self.video("finished.mp4")
        app = AppTest.from_string("from webui.creator_workspace import render\nrender()", default_timeout=30)
        app.session_state["creator_publish_video"] = str(video)
        app.run()
        with patch("app.services.creator.publishing.execute_publish") as execute:
            self.go(app, "发布中心")
            self.assertFalse(app.exception)
            self.assertTrue(any("成片已带入发布中心" in item.value for item in app.caption))
            self.assertTrue(any("请先在上方添加发布账号" in item.value for item in app.info))
            execute.assert_not_called()
        self.assertEqual(publishing.list_tasks(), [])

    def test_publish_metadata_and_cover_show_without_account_and_keep_empty_edits(self):
        from PIL import Image
        video = self.video("ready.mp4")
        cover = Path(self.directory.name) / "ready.png"
        Image.new("RGB", (40, 60), "blue").save(cover)
        app = AppTest.from_string("from webui.creator_workspace import render\nrender()", default_timeout=30)
        app.session_state["creator_publish_video"] = str(video)
        app.session_state["creator_publish_title"] = "这条成片的标题"
        app.session_state["creator_publish_description"] = "正文\n#创作教程"
        app.session_state["creator_publish_cover"] = str(cover)
        app.session_state["creator_selected_tab"] = "发布中心"
        with patch("app.services.creator.publishing.execute_publish") as execute, patch("app.services.creator.publishing.prepare_publish") as prepare:
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.text_input(key="creator_publish_title").value, "这条成片的标题")
            self.assertEqual(app.text_area(key="creator_publish_description").value, "正文\n#创作教程")
            self.assertTrue(any(item.caption == "发布封面" for item in app.get("image")[0].proto.imgs))
            app.text_input(key="creator_publish_title").set_value("").run()
            app.text_area(key="creator_publish_description").set_value("").run()
            self.go(app, "文案提取")
            self.go(app, "发布中心")
            self.assertFalse(app.exception)
            self.assertEqual(app.text_input(key="creator_publish_title").value, "")
            self.assertEqual(app.text_area(key="creator_publish_description").value, "")
            self.assertEqual(app.text_input(key="creator_publish_video").value, str(video))
            self.assertEqual(app.text_input(key="creator_publish_cover").value, str(cover))
            execute.assert_not_called()
            prepare.assert_not_called()

    def test_sidebar_has_only_five_categories_and_renders_only_selected_tool(self):
        with patch("webui.creator_avatar_workspace.render") as avatar, patch("webui.creator_render_workspace.render") as renderer:
            app = AppTest.from_string("from webui.creator_workspace import render\nrender()", default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertEqual([item.key for item in app.sidebar.radio], ["creator_category"])
            self.assertEqual(app.sidebar.radio[0].options, list(creator_workspace.CATEGORIES.values()))
            self.assertEqual(len(app.sidebar.button), 0)
            self.assertEqual(len(app.sidebar.expander), 0)
            avatar.assert_not_called()
            renderer.assert_not_called()
            self.go(app, "模板剪辑")
            renderer.assert_called()
            avatar.assert_not_called()
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.radio(key="creator_category").value, "render")
            self.assertEqual(app.session_state["creator_selected_tab"], "模板剪辑")
            self.go(app, "数字人")
            avatar.assert_called()

    def test_legacy_navigation_targets_select_correct_category_before_widget_creation(self):
        app = AppTest.from_string("from webui.creator_workspace import render\nrender()", default_timeout=30).run()
        app.session_state["creator_navigation_target"] = "发布中心"
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="creator_category").value, "publish")
        self.assertEqual(app.radio(key="creator_selected_tab").value, "发布中心")
        app.session_state["creator_navigation_target"] = "竞品素材库"
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="creator_category").value, "script")
        self.assertEqual(app.radio(key="creator_selected_tab").value, "口播参考库")

    def test_material_tool_uses_existing_studio_stage_without_creating_or_submitting_work(self):
        from app.services.creator import workflow
        app = AppTest.from_string("from webui.creator_workspace import render\nrender()", default_timeout=30).run()
        with patch.object(workflow, "create_project") as create, patch.object(workflow, "submit_project") as submit:
            self.go(app, "素材与分镜")
            self.assertEqual(app.radio(key="studio_mode").value, "分步制作")
            self.assertEqual(app.radio(key="studio_step").value, "visuals")
            self.assertTrue(app.get("file_uploader"))
            create.assert_not_called()
            submit.assert_not_called()

    def test_project_deep_link_opens_work_but_internal_material_picker_keeps_category(self):
        from app.services.creator import workflow
        first = workflow.create_project({"input_text": "深链接打开的实际作品", "input_mode": "script", "kind": "knowledge"})
        second = workflow.create_project({"input_text": "在素材页选择的另一条作品", "input_mode": "script", "kind": "knowledge"})
        app = AppTest.from_string("from webui.creator_workspace import render\nrender()", default_timeout=30).run()
        with patch.object(workflow, "submit_project") as submit:
            self.go(app, "发布中心")
            app.query_params["project"] = first["id"]
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.radio(key="creator_category").value, "script")
            self.assertEqual(app.radio(key="creator_selected_tab").value, "创作中心")
            self.assertEqual(app.selectbox(key="studio_project_choice").value, first["id"])
            self.assertEqual(app.text_area(key="studio_input_text").value, "深链接打开的实际作品")
            self.go(app, "素材与分镜")
            app.selectbox(key="studio_project_choice").set_value(second["id"]).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.radio(key="creator_category").value, "visuals")
            self.assertEqual(app.radio(key="creator_selected_tab").value, "素材与分镜")
            self.assertEqual(app.selectbox(key="studio_project_choice").value, second["id"])
            submit.assert_not_called()

    def test_category_change_without_callback_marker_survives_fragment_polling(self):
        state = {"creator_category": "audio", "creator_selected_tab": "创作中心",
                 "creator_last_rendered_category": "script", "creator_last_rendered_page": "创作中心"}
        with patch.object(creator_workspace.st, "session_state", state), patch.object(creator_workspace.st, "query_params", {}):
            self.assertEqual("配音", creator_workspace._sync_navigation())
            self.assertEqual("audio", state["creator_category"])
            # The old page label can arrive after the first category render.
            state["creator_selected_tab"] = "一键创作"
            self.assertEqual("配音", creator_workspace._sync_navigation())
            self.assertEqual("audio", state["creator_category"])
            # Explicit legacy handoffs still outrank the passive widget state.
            state["creator_navigation_target"] = "发布中心"
            self.assertEqual("发布中心", creator_workspace._sync_navigation())
            self.assertEqual("publish", state["creator_category"])


if __name__=="__main__":
    unittest.main()
