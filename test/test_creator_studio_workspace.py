"""Customer flow tests: persistent projects, stage reuse and safe publishing."""
import copy
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from webui import creator_studio_workspace as studio


class WorkflowFixture:
    def __init__(self):
        self.projects = {}
        self.submissions = []
        self.edits = []

    def create_project(self, config):
        ident = f"{len(self.projects) + 1:032x}"
        row = {"id": ident, "title": config["input_text"][:20], "config": copy.deepcopy(config), "state": "draft",
               "current_stage": "script", "stages": {key: {"state": "pending", "result": {}} for key in studio.STEPS}, "result": {}}
        self.projects[ident] = row
        return copy.deepcopy(row)

    def update_project(self, ident, changes):
        self.edits.append((ident, copy.deepcopy(changes)))
        if changes.get("acknowledge_paid_retry"):
            self.projects[ident]["remote_pending"] = None
            self.projects[ident]["state"] = "paused"
            return self.get_project(ident)
        if "config" in changes:
            self.projects[ident]["config"].update(copy.deepcopy(changes["config"]))
        self.projects[ident].update(copy.deepcopy({key: value for key, value in changes.items() if key != "config"}))
        return self.get_project(ident)

    def list_projects(self):
        return copy.deepcopy(list(self.projects.values()))

    def get_project(self, ident):
        return copy.deepcopy(self.projects.get(ident))

    def submit_project(self, ident, until_stage=None):
        self.submissions.append((ident, until_stage))
        row = self.projects[ident]
        row["stages"]["script"] = {"state": "done", "result": {"text": row["config"]["input_text"]}}
        if row["config"]["kind"] in {"product", "montage"} and not row["config"]["materials"]:
            row["state"] = "needs_user"
            row["current_stage"] = "visuals"
            row["stages"]["visuals"] = {"state": "needs_user", "error": "请上传商品图片或视频，再继续生成。", "result": {}}
        else:
            row["state"] = "paused" if until_stage else "done"
            row["current_stage"] = until_stage or "release"
        return "job-" + ident

    def cancel_project(self, ident):
        self.projects[ident]["state"] = "cancelled"


class StudioWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.folder.name})
        self.environment.start()
        self.workflow = WorkflowFixture()
        self.workflow_patch = patch.object(studio, "_workflow", return_value=self.workflow)
        self.workflow_patch.start()
        self.voices_patch = patch("app.services.creator.narration.list_options", return_value=[
            {"id": studio.DEFAULT_VOICE, "name": "标准女声", "provider": "edge"},
            {"id": "duix:2", "name": "我的声音", "provider": "duix"},
        ])
        self.voices_patch.start()
        self.people_patch = patch("app.services.creator.avatar.list_options", return_value=[{"id": "local:1", "name": "我的人物"}])
        self.people_patch.start()
        self.publish_guard = patch("app.services.creator.publishing.execute_publish", side_effect=AssertionError("Unexpected publication"))
        self.execute = self.publish_guard.start()

    def tearDown(self):
        self.publish_guard.stop()
        self.people_patch.stop()
        self.voices_patch.stop()
        self.workflow_patch.stop()
        self.environment.stop()
        self.folder.cleanup()

    def app(self, integrated=False):
        module = "creator_workspace" if integrated else "creator_studio_workspace"
        app = AppTest.from_string(f"from webui.{module} import render\nrender()", default_timeout=30).run()
        self.assertFalse(app.exception)
        return app

    def button(self, app, key):
        return next(item for item in app.button if item.key == key)

    def test_modes_submit_the_same_persisted_project_and_real_settings(self):
        app = self.app()
        app.text_area(key="studio_input_text").set_value("这是同一条作品的口播文案。").run()
        app.selectbox(key="studio_voice_id").set_value("duix:2").run()
        app.selectbox(key="studio_style").set_value("business").run()
        app.selectbox(key="studio_template").set_value("cards").run()
        self.button(app, "studio_generate").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(self.workflow.projects), 1)
        ident = next(iter(self.workflow.projects))
        config = self.workflow.projects[ident]["config"]
        self.assertEqual(config["voice_id"], "duix:2")
        self.assertEqual(config["style"], "business")
        self.assertEqual(config["template"], "cards")
        self.assertFalse(config["allow_paid"])
        self.assertEqual(self.workflow.submissions, [(ident, None)])
        app.radio(key="studio_mode").set_value("分步制作").run()
        app.radio(key="studio_step").set_value("voice").run()
        self.button(app, "studio_generate").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(self.workflow.projects), 1)
        self.assertEqual(self.workflow.submissions[-1], (ident, "voice"))
        self.assertEqual(self.workflow.edits[-1][0], ident)
        self.execute.assert_not_called()

    def test_page_reload_restores_project_and_completed_script_without_generation(self):
        project = self.workflow.create_project({**studio.DEFAULTS, "input_text": "已经保存的稿件"})
        ident = project["id"]
        self.workflow.projects[ident]["stages"]["script"] = {"state": "done", "result": {"text": "已经生成的完整口播"}}
        app = AppTest.from_string("from webui.creator_studio_workspace import render\nrender()", default_timeout=30)
        app.query_params["project"] = ident
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key="studio_project_choice").value, ident)
        self.assertEqual(app.text_area(key="studio_input_text").value, "已经保存的稿件")
        self.assertEqual(next(item for item in app.text_area if item.label == "编辑口播稿").value, "已经生成的完整口播")
        self.assertEqual(self.workflow.submissions, [])
        self.button(app, "studio_refresh").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.workflow.submissions, [])

    def test_missing_materials_stays_saved_and_explains_next_action(self):
        app = self.app()
        app.text_area(key="studio_input_text").set_value("介绍这件商品的真实卖点。").run()
        app.selectbox(key="studio_kind").set_value("product").run()
        self.button(app, "studio_generate").click().run()
        self.assertFalse(app.exception)
        ident = next(iter(self.workflow.projects))
        self.assertEqual(self.workflow.projects[ident]["state"], "needs_user")
        self.assertTrue(any("请上传商品图片或视频" in item.value for item in app.warning))
        app.radio(key="studio_mode").set_value("分步制作").run()
        app.radio(key="studio_step").set_value("visuals").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key="studio_project_choice").value, ident)
        self.assertFalse(self.button(app, "studio_generate").disabled)

    def test_publish_handoff_only_loads_metadata_and_navigates(self):
        project = self.workflow.create_project({**studio.DEFAULTS, "input_text": "发布前仍然需要选择账号"})
        ident = project["id"]
        video = Path(self.folder.name) / "finished.mp4"
        video.write_bytes(b"preview-fixture")
        self.workflow.projects[ident].update({"state": "done", "result": {"video_path": str(video), "title": "成片标题"}})
        self.workflow.projects[ident]["stages"]["render"] = {"state": "done", "result": {"video_path": str(video), "quality": {"pass": True}}}
        self.workflow.projects[ident]["stages"]["release"] = {"state": "done", "result": {"description": "发布正文"}}
        app = self.app()
        app.selectbox(key="studio_project_choice").set_value(ident).run()
        with patch("webui.creator_publish_workspace.load_materials") as load:
            self.button(app, "studio_publish_handoff").click().run()
        self.assertFalse(app.exception)
        load.assert_called_once()
        self.assertEqual(load.call_args.args[0]["video_path"], str(video))
        self.assertEqual(app.session_state["creator_selected_tab"], "发布中心")
        self.assertTrue(app.session_state["creator_navigation_pending"])
        self.execute.assert_not_called()

    def test_fragment_handoff_rerenders_integrated_publisher_without_submitting(self):
        from app.services.creator import extract, publishing
        project = self.workflow.create_project({**studio.DEFAULTS, "input_text": "成片与发布页面的完整交接"})
        ident = project["id"]
        video = Path(self.folder.name) / "handoff.mp4"
        subprocess.run([
            extract.ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=96x160:r=10:d=0.4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        ], check=True, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.workflow.projects[ident].update({"state": "done", "result": {"video_path": str(video), "title": "本次成片标题"}})
        self.workflow.projects[ident]["stages"]["render"] = {"state": "done", "result": {"video_path": str(video), "quality": {"pass": True}}}
        self.workflow.projects[ident]["stages"]["release"] = {"state": "done", "result": {"description": "本次发布正文"}}
        app = self.app(integrated=True)
        app.selectbox(key="studio_project_choice").set_value(ident).run()
        with patch.object(studio.st, "rerun", wraps=studio.st.rerun) as rerun, patch.object(publishing, "prepare_publish") as prepare:
            self.button(app, "studio_publish_handoff").click().run()
        self.assertFalse(app.exception)
        rerun.assert_any_call(scope="app")
        self.assertEqual(app.radio(key="creator_selected_tab").value, "发布中心")
        self.assertEqual(app.text_input(key="creator_publish_video").value, str(video))
        self.assertEqual(app.text_input(key="creator_publish_title").value, "本次成片标题")
        self.assertEqual(app.text_area(key="creator_publish_description").value, "本次发布正文")
        self.assertTrue(any("核对素材，准备发布。" in item.value for item in app.markdown))
        self.assertTrue(any("成片已带入发布中心" in item.value for item in app.caption))
        prepare.assert_not_called()
        self.execute.assert_not_called()
        self.assertEqual(publishing.list_tasks(), [])

    def test_library_opens_saved_work_without_submitting(self):
        project = self.workflow.create_project({**studio.DEFAULTS, "input_text": "恢复中的作品"})
        self.workflow.projects[project["id"]]["state"] = "interrupted"
        app = AppTest.from_string("from webui.creator_studio_workspace import render_library\nrender_library()", default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("已中断" in item.value for item in app.caption))
        self.button(app, "studio_open_" + project["id"]).click().run()
        self.assertEqual(app.session_state["studio_pending_project"], project["id"])
        self.assertEqual(app.session_state["creator_selected_tab"], "创作中心")
        self.assertEqual(self.workflow.submissions, [])

    def test_visual_track_and_failed_quality_are_never_offered_as_finished_video(self):
        video = Path(self.folder.name) / "base.mp4"
        video.write_bytes(b"local-preview-fixture")
        project = self.workflow.create_project({**studio.DEFAULTS, "input_text": "基础画面不是最终成片"})
        ident = project["id"]
        row = self.workflow.projects[ident]
        row.update(state="failed", current_stage="render", result={"video_path": str(video)})
        row["stages"]["visuals"] = {"state": "done", "result": {"video_path": str(video)}}
        row["stages"]["release"] = {"state": "done", "result": {"video_path": str(video)}}
        app = self.app()
        app.selectbox(key="studio_project_choice").set_value(ident).run()
        for render_state, passed in (("pending", True), ("failed", True), ("done", False)):
            row["stages"]["render"] = {"state": render_state, "result": {"video_path": str(video), "quality": {"pass": passed}}}
            app.run()
            self.assertFalse(app.exception)
            self.assertNotIn("成片", app.radio(key="studio_preview_" + ident).options)
            self.assertTrue(any("基础画面，尚未生成最终视频" in item.value for item in app.caption))
            self.assertFalse(any(item.key == "studio_download_video" for item in app.get("download_button")))
            self.assertFalse(any(item.key == "studio_publish_handoff" for item in app.button))
        row["stages"]["render"] = {"state": "done", "result": {"video_path": str(video), "quality": {"pass": True}}}
        app.run()
        app.radio(key="studio_preview_" + ident).set_value("成片").run()
        self.assertFalse(app.exception)
        self.assertTrue(any(item.key == "studio_download_video" for item in app.get("download_button")))
        self.assertTrue(any(item.key == "studio_publish_handoff" for item in app.button))
        self.execute.assert_not_called()

    def test_main_navigation_keeps_legacy_routes_and_defaults_to_studio(self):
        from webui.creator_workspace import CATEGORIES
        app = self.app(integrated=True)
        navigation = app.radio(key="creator_selected_tab")
        self.assertEqual(navigation.value, "创作中心")
        self.assertIn("作品库", navigation.options)
        self.assertEqual(app.sidebar.radio[0].options, list(CATEGORIES.values()))
        app.radio(key="creator_selected_tab").set_value("作品库").run()
        self.assertFalse(app.exception)
        app.radio(key="creator_category").set_value("render").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["creator_selected_tab"], "模板剪辑")

    def test_uploads_are_content_addressed_and_reused(self):
        upload = io.BytesIO(b"example-media")
        upload.name = "my-picture.png"
        upload.getvalue = lambda: b"example-media"
        first = studio._stage_upload(upload, media=True)
        self.assertEqual(first, studio._stage_upload(upload, media=True))
        self.assertEqual(Path(first).read_bytes(), b"example-media")
        upload.name = "unsafe.exe"
        with self.assertRaises(ValueError):
            studio._stage_upload(upload, media=True)

    def test_cloud_permission_is_available_for_saved_voice_and_not_duplicated(self):
        options = [{"id": studio.DEFAULT_VOICE, "name": "标准女声", "provider": "edge"},
                   {"id": "saved:clone", "name": "我的克隆声音", "provider": "voxcpm"}]
        with patch("app.services.creator.narration.list_options", return_value=options):
            app = self.app()
            app.selectbox(key="studio_voice_id").set_value("saved:clone").run()
            self.assertFalse(app.exception)
            self.assertFalse(app.checkbox(key="studio_allow_paid").value)
            app.radio(key="studio_input_mode").set_value("topic").run()
            self.assertFalse(app.exception)
            self.assertEqual(len([item for item in app.checkbox if item.key == "studio_allow_paid"]), 1)

    def test_unknown_remote_request_requires_specific_authorization(self):
        project = self.workflow.create_project({**studio.DEFAULTS, "input_text": "需核查的请求"})
        ident = project["id"]
        self.workflow.projects[ident].update(state="needs_user", remote_pending={"provider": "voxcpm", "state": "sending"})
        app = self.app()
        app.selectbox(key="studio_project_choice").set_value(ident).run()
        self.assertTrue(self.button(app, "studio_retry_authorize_" + ident).disabled)
        self.assertEqual(self.workflow.edits, [])
        app.checkbox(key="studio_retry_confirm_" + ident).check().run()
        self.button(app, "studio_retry_authorize_" + ident).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.workflow.edits[-1], (ident, {"acknowledge_paid_retry": True}))
        self.assertEqual(self.workflow.submissions, [])

    def test_saved_material_metadata_and_real_timing_are_visible(self):
        from PIL import Image
        image = Path(self.folder.name) / "kitchen.png"
        Image.new("RGB", (24, 24), "blue").save(image)
        project = self.workflow.create_project({**studio.DEFAULTS, "input_text": "厨房收纳", "materials": [{"path": str(image), "name": "厨房收纳.png", "tags": ["厨房"]}]})
        ident = project["id"]
        self.workflow.projects[ident]["stages"]["visuals"] = {"state": "done", "result": {"plan": {"shots": [{
            "text": "厨房收纳", "start": 1.2, "end": 2.5, "visual_start": 0, "visual_end": 2.5,
            "media_path": str(image), "media_type": "image", "match_reason": "文件名/素材标签匹配：厨房",
        }]}}}
        app = self.app()
        app.selectbox(key="studio_project_choice").set_value(ident).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("0.0–2.5 秒" in item.value for item in app.markdown))
        self.assertTrue(any("素材标签匹配：厨房" in item.value for item in app.caption))
        self.assertTrue(any("厨房收纳.png" in item.value for item in app.caption))
        self.assertTrue(app.get("image"))
        self.assertEqual(app.session_state["studio_saved_materials"][0]["name"], "厨房收纳.png")

    def test_real_workflow_draft_restores_without_starting_worker(self):
        from app.services.creator import workflow
        project = workflow.create_project({**studio.DEFAULTS, "input_text": "真实作品存储"})
        with patch.object(studio, "_workflow", return_value=workflow), patch.object(workflow, "submit_project") as submit:
            app = AppTest.from_string("from webui.creator_studio_workspace import render\nrender()", default_timeout=30)
            app.query_params["project"] = project["id"]
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.selectbox(key="studio_project_choice").value, project["id"])
            self.assertEqual(app.text_area(key="studio_input_text").value, "真实作品存储")
            submit.assert_not_called()

    def test_generated_topic_script_can_be_edited_and_saved_without_regenerating(self):
        project = self.workflow.create_project({**studio.DEFAULTS, "input_mode": "topic", "input_text": "原始主题"})
        ident = project["id"]
        self.workflow.projects[ident]["stages"]["script"] = {"state": "done", "result": {"text": "生成后的文案"}}
        app = self.app()
        app.selectbox(key="studio_project_choice").set_value(ident).run()
        next(item for item in app.text_area if item.label == "编辑口播稿").set_value("我修改后的口播稿。").run()
        self.assertEqual(self.workflow.projects[ident]["config"]["input_text"], "原始主题")
        self.assertEqual(self.workflow.projects[ident]["config"]["input_mode"], "topic")
        self.assertEqual(self.workflow.projects[ident]["stages"]["script"]["result"]["text"], "生成后的文案")
        self.button(app, "studio_apply_script").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.workflow.projects[ident]["config"]["input_mode"], "script")
        self.assertEqual(self.workflow.projects[ident]["config"]["input_text"], "我修改后的口播稿。")
        self.assertEqual(app.text_area(key="studio_input_text").value, "我修改后的口播稿。")
        self.assertEqual(self.workflow.submissions, [])

    def test_terminal_progress_refreshes_page_once_and_preserves_edits(self):
        state = {"studio_page_state": ("project-one", "running"), "studio_input_text": "未保存的编辑"}
        with patch.object(studio.st, "session_state", state), patch.object(studio.st, "rerun") as rerun:
            studio._refresh_after_completion({"id": "project-one", "state": "running"})
            rerun.assert_not_called()
            studio._refresh_after_completion({"id": "project-one", "state": "needs_user"})
            rerun.assert_called_once_with(scope="app")
            self.assertEqual(state["studio_preview_pending"], "project-one")
            self.assertEqual(state["studio_input_text"], "未保存的编辑")
            studio._refresh_after_completion({"id": "project-one", "state": "needs_user"})
            studio._refresh_after_completion({"id": "another-project", "state": "done"})
            self.assertEqual(rerun.call_count, 1)

    def test_completion_selects_latest_valid_preview_once(self):
        video = Path(self.folder.name) / "latest.mp4"
        video.write_bytes(b"preview-fixture")
        project = self.workflow.create_project({**studio.DEFAULTS, "input_text": "完成后直接显示成片"})
        ident = project["id"]
        row = self.workflow.projects[ident]
        row["stages"]["script"] = {"state": "done", "result": {"text": "已生成的文案"}}
        row["stages"]["visuals"] = {"state": "done", "result": {"video_path": str(video)}}
        app = self.app()
        app.selectbox(key="studio_project_choice").set_value(ident).run()
        key = "studio_preview_" + ident
        self.assertEqual(app.radio(key=key).value, "分镜")
        row["state"] = "done"
        row["stages"]["render"] = {"state": "done", "result": {"video_path": str(video), "quality": {"pass": True}}}
        row["stages"]["release"] = {"state": "done", "result": {}}
        app.session_state["studio_preview_pending"] = ident
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key=key).value, "成片")
        self.assertNotIn("studio_preview_pending", app.session_state)
        self.assertTrue(any(item.key == "studio_publish_handoff" for item in app.button))
        app.radio(key=key).set_value("文案").run()
        self.assertEqual(app.radio(key=key).value, "文案")
        self.assertEqual(self.workflow.submissions, [])
        row["state"] = "failed"
        row["stages"]["render"] = {"state": "done", "result": {"video_path": str(video), "quality": {"pass": False}}}
        app.session_state["studio_preview_pending"] = ident
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key=key).value, "分镜")

    def test_only_active_work_polls_and_idle_controls_remain_available(self):
        project = self.workflow.create_project({**studio.DEFAULTS, "input_text": "完成后的作品预览应保持稳定"})
        ident = project["id"]
        with patch.object(studio, "_poll_progress", side_effect=studio._progress) as poll:
            app = self.app()
            app.selectbox(key="studio_project_choice").set_value(ident).run()
            poll.assert_not_called()
            for state in ("queued", "running"):
                self.workflow.projects[ident]["state"] = state
                app.run()
                self.assertFalse(app.exception)
                poll.assert_called_once_with(ident)
                self.assertTrue(self.button(app, "studio_generate").disabled)
                poll.reset_mock()
            for state in ("done", "needs_user", "interrupted", "failed"):
                self.workflow.projects[ident]["state"] = state
                app.run()
                self.assertFalse(app.exception)
                poll.assert_not_called()
                self.assertFalse(self.button(app, "studio_generate").disabled)

    def test_six_goals_keep_examples_as_reference_and_do_not_request_cloud_services(self):
        from app.services.creator import narration, topics
        with patch.object(topics, "generate_draft") as draft, patch.object(narration, "generate") as voice:
            app = self.app()
            self.assertEqual(set(app.radio(key="studio_purpose_choice").options), set(studio.PURPOSE_LABELS.values()))
            self.assertEqual("product_service", app.radio(key="studio_purpose_choice").value)
            for purpose in studio.PURPOSE_LABELS:
                app.radio(key="studio_purpose_choice").set_value(purpose).run()
                self.assertFalse(app.exception)
                self.assertEqual("", app.text_area(key="studio_input_text").value)
                self.assertEqual("", app.selectbox(key="studio_brand_profile_id").value)
                self.assertTrue(any("示例，需替换为你确认的真实资料" in item.value for item in app.markdown))
            app.radio(key="studio_input_mode").set_value("topic").run()
            self.assertFalse(app.checkbox(key="studio_allow_paid").value)
            self.assertTrue(any("已有文案仍按实际配音时长" in item.value for item in app.caption))
            draft.assert_not_called()
            voice.assert_not_called()
        self.assertEqual([], self.workflow.submissions)

    def test_saved_brand_can_be_reused_for_second_work_without_reentering_details(self):
        from app.services.creator import brand_profiles
        profile = brand_profiles.save_profile({"name": "同一客户", "business_type": "service",
                                               "offering": "上门整理", "audience": "需要整理空间的家庭"})
        app = self.app()
        for text in ("第一条真实服务说明。", "第二条整理建议。"):
            app.selectbox(key="studio_brand_profile_id").set_value(profile["id"]).run()
            app.text_area(key="studio_input_text").set_value(text).run()
            self.button(app, "studio_save").click().run()
            self.assertFalse(app.exception)
            app.selectbox(key="studio_project_choice").set_value("").run()
        self.assertEqual(2, len(self.workflow.projects))
        self.assertTrue(all(row["config"]["brand_snapshot"]["offering"] == "上门整理"
                            for row in self.workflow.projects.values()))
        self.assertEqual([], self.workflow.submissions)

    def test_existing_work_keeps_saved_customer_snapshot_until_explicit_refresh(self):
        from app.services.creator import brand_profiles
        profile = brand_profiles.save_profile({"name": "原名称", "offering": "已确认的服务", "audience": "目标客户"})
        snapshot = brand_profiles.snapshot(profile["id"])
        project = self.workflow.create_project({**studio.DEFAULTS, "input_text": "现有作品正文。",
                                               "brand_profile_id": profile["id"], "brand_snapshot": snapshot})
        brand_profiles.save_profile({"name": "新名称", "offering": "后来更新的服务"}, profile["id"], expected_version=1)
        app = self.app()
        app.selectbox(key="studio_project_choice").set_value(project["id"]).run()
        self.assertEqual(snapshot, app.session_state["studio_brand_snapshot"])
        self.button(app, "studio_save").click().run()
        self.assertEqual(snapshot, self.workflow.projects[project["id"]]["config"]["brand_snapshot"])
        self.button(app, "studio_refresh_brand").click().run()
        self.button(app, "studio_save").click().run()
        self.assertFalse(app.exception)
        refreshed = self.workflow.projects[project["id"]]["config"]["brand_snapshot"]
        self.assertEqual(2, refreshed["version"])
        self.assertEqual("后来更新的服务", refreshed["offering"])
        self.assertEqual([], self.workflow.submissions)

    def test_legacy_work_keeps_duration_and_route_without_adopting_inferred_goal(self):
        config = {"input_text": "较长的已有口播文案。", "input_mode": "script", "kind": "knowledge", "materials": []}
        project = self.workflow.create_project(config)
        app = self.app()
        app.selectbox(key="studio_project_choice").set_value(project["id"]).run()
        self.assertEqual("knowledge", app.radio(key="studio_purpose_choice").value)
        self.assertEqual(0, app.selectbox(key="studio_target_duration").value)
        self.assertEqual("", app.selectbox(key="studio_content_template").value)
        self.button(app, "studio_save").click().run()
        saved = self.workflow.projects[project["id"]]["config"]
        self.assertEqual("", saved["video_purpose"])
        self.assertEqual(0, saved["target_duration"])
        self.assertEqual("knowledge", saved["kind"])
        self.assertEqual([], self.workflow.submissions)

    def test_new_customer_dialog_attaches_real_saved_profile_to_current_creation(self):
        from app.services.creator import brand_profiles
        app = self.app()
        self.button(app, "studio_new_brand").click().run()
        self.assertFalse(app.exception)
        app.text_area(key="brand_editor_offering").set_value("客户实际提供的商品。").run()
        app.text_area(key="brand_editor_audience").set_value("希望了解商品的人。").run()
        self.button(app, "brand_editor_save").click().run()
        self.assertFalse(app.exception)
        profiles = brand_profiles.list_profiles()
        self.assertEqual(1, len(profiles))
        self.assertEqual(profiles[0]["id"], app.selectbox(key="studio_brand_profile_id").value)
        self.assertEqual("客户实际提供的商品。", app.session_state["studio_brand_snapshot"]["offering"])
        self.assertEqual("", app.text_area(key="studio_input_text").value)
        self.assertEqual([], self.workflow.submissions)


if __name__ == "__main__":
    unittest.main()
