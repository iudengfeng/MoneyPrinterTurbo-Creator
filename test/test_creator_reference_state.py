"""Regress project isolation and asynchronous results on the reference home."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from webui import creator_reference_release as release
from webui import creator_reference_state as controller


SHELL = """
import streamlit as st
from webui.creator_reference_state import ReferenceContext
if 'test_requested_project' in st.session_state:
    st.query_params['project'] = st.session_state['test_requested_project']
ctx = ReferenceContext()
st.text_area('原文', key='ref_original_text', persist_state='session')
st.text_area('文案', key='ref_script_text', persist_state='session')
st.text_input('发布标题', key='ref_publish_title', persist_state='session')
st.text_input('平台标签', key='ref_publish_tags', persist_state='session')
"""


def _project(ident, text):
    return {
        "id": ident, "state": "draft",
        "config": {
            "input_mode": "script", "input_text": text,
            "voice_id": "edge:zh-CN-XiaoxiaoNeural", "speed": 1.0,
            "audio_path": "", "source_video_path": "", "materials": [],
            "release_title": ident + " 的标题", "hashtags": ["知识", "方法"],
        },
        "stages": {name: {"state": "pending", "result": {}} for name in ("script", "voice", "visuals", "render", "release")},
    }


class ReferenceStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.projects = {
            "work-a": _project("work-a", "作品 A 的已保存真实稿件。"),
            "work-b": _project("work-b", "作品 B 的独立稿件。"),
        }
        self.job_rows = {}
        self.video_info = {}
        self.patches = [
            patch.dict(os.environ, {"MPT_CREATOR_DATA": self.directory.name}),
            patch.object(controller.workflow, "get_project", side_effect=self.get_project),
            patch.object(controller.workflow, "create_project", side_effect=self.create_project),
            patch.object(controller.workflow, "update_project", side_effect=self.update_project),
            patch.object(controller.workflow, "submit_project", return_value="pipeline-job"),
            patch.object(controller.jobs, "get_job", side_effect=lambda ident: deepcopy(self.job_rows.get(ident))),
            patch.object(controller.jobs, "submit", side_effect=self.submit_job),
            patch.object(controller.store, "data_root", return_value=Path(self.directory.name)),
            patch.object(controller.store, "save_record", side_effect=AssertionError("No real record store writes")),
            patch.object(controller.st, "toast"),
            patch.object(controller.rendering, "probe_source", side_effect=self.probe_video),
        ]
        self.mocks = [item.start() for item in self.patches]
        for item in reversed(self.patches):
            self.addCleanup(item.stop)
        self.get_mock, self.create_mock, self.update_mock, self.pipeline_mock = self.mocks[1:5]
        self.submit_mock = self.mocks[6]
        self.probe_mock = self.mocks[-1]

    def probe_video(self, path):
        return deepcopy(self.video_info.get(path, {"path": path, "has_video": True, "has_audio": True, "duration": 2.5}))

    def get_project(self, ident):
        if ident not in self.projects:
            raise KeyError(ident)
        return deepcopy(self.projects[ident])

    def create_project(self, config):
        self.assertTrue(config["input_text"].strip(), "Production projects require a script")
        row = _project("created-work", config["input_text"])
        row["config"].update(deepcopy(config))
        self.projects[row["id"]] = row
        return deepcopy(row)

    def update_project(self, ident, changes):
        row = self.projects[ident]
        row["config"].update(deepcopy(changes))
        return deepcopy(row)

    def submit_job(self, label, operation, *args, **kwargs):
        ident = "queued-job-" + str(self.submit_mock.call_count)
        self.job_rows[ident] = {"state": "queued", "message": label}
        return ident

    @contextmanager
    def state(self, session=None, query=None):
        session = session if session is not None else {}
        query = query if query is not None else {}
        with patch.object(controller.st, "session_state", session), patch.object(controller.st, "query_params", query):
            yield session, query

    def completed(self, session, kind, result, *, project_id="work-a", ident="finished-job"):
        session["ref_pending_actions"] = {ident: {"kind": kind, "project_id": project_id, "label": "测试任务"}}
        self.job_rows[ident] = {"state": "done", "message": "已完成", "result": deepcopy(result)}

    def media(self, name):
        path = Path(self.directory.name) / name
        path.write_bytes(b"isolated-media-fixture")
        return str(path)

    def finish_video_import(self, ident, video, text="视频中的真实口播内容，识别结果属于这份原片。"):
        self.job_rows[ident] = {
            "state": "done", "message": "文案提取完成",
            "result": {
                "text": text, "media_path": video,
                "audio_path": self.media("extracted-reference.wav"), "srt_path": self.media("extracted-transcript.srt"),
                "txt_path": self.media("extracted-transcript.txt"),
                "segments": [{"start": 0.0, "end": 2.5, "text": text}],
                "language": "zh", "duration": 2.5, "model_size": "small",
            },
        }

    def app(self, ident="work-a"):
        app = AppTest.from_string(SHELL, default_timeout=30)
        app.session_state["test_requested_project"] = ident
        app.run()
        self.assertFalse(app.exception)
        return app

    def test_existing_project_primes_native_widgets_without_overwriting_same_project_edits(self):
        accepted = "从文案阶段返回的已确认内容。"
        self.projects["work-a"]["stages"]["script"]["result"] = {"text": accepted}
        app = self.app()
        self.assertEqual(app.text_area(key="ref_original_text").value, accepted)
        self.assertEqual(app.text_area(key="ref_script_text").value, accepted)
        self.assertEqual(app.text_input(key="ref_publish_title").value, "work-a 的标题")
        self.assertEqual(app.text_input(key="ref_publish_tags").value, "知识 方法")
        edited = "这是仍在编辑器中的未提交修改，普通重绘时必须完整保留。"
        app.text_area(key="ref_script_text").set_value(edited).run()
        app.text_input(key="ref_publish_title").set_value("我的新标题").run()
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area(key="ref_script_text").value, edited)
        self.assertEqual(app.text_input(key="ref_publish_title").value, "我的新标题")
        self.update_mock.assert_not_called()

    def test_unknown_query_project_falls_back_to_empty_draft(self):
        with self.state({"ref_current_project": "work-a", "ref_loaded_project": "work-a", "ref_script_text": "旧作品"}, {"project": "unknown-work"}) as (session, _):
            ctx = controller.ReferenceContext()
            self.assertEqual(ctx.project, {})
            self.assertEqual(session["ref_current_project"], "")
            self.assertEqual(session["ref_script_text"], "")
            self.assertEqual(session["ref_original_text"], "")
        self.get_mock.assert_called_once_with("unknown-work")
        self.create_mock.assert_not_called()

    def test_extract_completion_is_applied_before_widgets_and_saved_in_current_project(self):
        app = self.app()
        text = "真实视频识别后的原文，完成任务时由主页面统一同步。"
        app.session_state["ref_pending_actions"] = {"extract-job": {"kind": "extract", "project_id": "work-a", "label": "识别"}}
        self.job_rows["extract-job"] = {"state": "done", "message": "成功", "result": {"text": text}}
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area(key="ref_original_text").value, text)
        self.assertEqual(app.text_area(key="ref_script_text").value, text)
        self.assertEqual(self.projects["work-a"]["config"]["input_text"], text)
        self.assertEqual(app.session_state["ref_pending_actions"], {})

    def test_script_completion_keeps_original_reference_and_updates_editor_before_widgets(self):
        app = self.app()
        reference = app.text_area(key="ref_original_text").value
        generated = "模型已经返回并完成校验的本次生成稿件。"
        app.session_state["ref_pending_actions"] = {"script-job": {"kind": "script", "project_id": "work-a", "label": "创作"}}
        self.job_rows["script-job"] = {"state": "done", "result": {"text": generated}}
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area(key="ref_original_text").value, reference)
        self.assertEqual(app.text_area(key="ref_script_text").value, generated)
        self.assertEqual(self.projects["work-a"]["config"]["input_text"], generated)

    def test_result_for_previous_project_cannot_overwrite_new_project(self):
        for kind in ("extract", "script", "voice_audio", "publish_copy", "pipeline"):
            with self.subTest(kind=kind), self.state({}, {"project": "work-b"}) as (session, _):
                self.completed(session, kind, {"text": "旧作品结果", "audio_path": self.media("old.wav"), "title": "旧发布标题", "tags": ["旧标签"]})
                ctx = controller.ReferenceContext()
                self.assertEqual(ctx.project["id"], "work-b")
                self.assertEqual(session["ref_script_text"], self.projects["work-b"]["config"]["input_text"])
                self.assertEqual(session["ref_publish_title"], "work-b 的标题")
                self.assertNotIn("ref_voice_audio_result", session)
                self.assertEqual(session["ref_pending_actions"], {})
        self.update_mock.assert_not_called()

    def test_unbound_extract_result_does_not_attach_to_a_newly_selected_project(self):
        with self.state({}, {"project": "work-b"}) as (session, _):
            self.completed(session, "extract", {"text": "在未建作品时启动的旧识别任务。"}, project_id="")
            controller.ReferenceContext()
            self.assertEqual(session["ref_script_text"], "作品 B 的独立稿件。")
            self.assertEqual(self.projects["work-b"]["config"]["input_text"], "作品 B 的独立稿件。")
        self.update_mock.assert_not_called()

    def test_unbound_extract_creates_project_when_user_still_has_empty_draft(self):
        with self.state() as (session, query):
            text = "尚未建作品时完成的真实识别原文。"
            self.completed(session, "extract", {"text": text}, project_id="")
            ctx = controller.ReferenceContext()
            self.assertEqual(ctx.project["id"], "created-work")
            self.assertEqual(session["ref_original_text"], text)
            self.assertEqual(session["ref_script_text"], text)
            self.assertEqual(query["project"], "created-work")
            self.assertEqual(self.projects["created-work"]["config"]["input_text"], text)
        self.create_mock.assert_called_once()

    def test_voice_completion_binds_actual_media_to_the_same_project(self):
        audio = self.media("generated.wav")
        with self.state({}, {"project": "work-a"}) as (session, _):
            self.completed(session, "voice_audio", {"audio_path": audio})
            ctx = controller.ReferenceContext()
            self.assertEqual(ctx.current_audio(), audio)
            self.assertEqual(session["ref_voice_audio_result"], {"audio_path": audio})
            self.assertEqual(self.projects["work-a"]["config"]["audio_path"], audio)
            self.assertEqual(self.projects["work-a"]["config"]["source_video_path"], "")
            self.assertEqual(session["ref_pending_actions"], {})
        self.update_mock.assert_called_once()
        self.create_mock.assert_not_called()

    def test_switching_project_clears_previous_audio_and_video_previews(self):
        old_audio = self.media("previous.wav")
        old_video = self.media("previous.mp4")
        new_audio = self.media("selected.wav")
        new_video = self.media("selected.mp4")
        self.projects["work-b"]["config"].update(audio_path=new_audio, source_video_path=new_video)
        session = {
            "ref_loaded_project": "work-a", "ref_current_project": "work-a",
            "ref_voice_audio_result": {"audio_path": old_audio}, "ref_imported_video": old_video,
        }
        with self.state(session, {"project": "work-b"}):
            ctx = controller.ReferenceContext()
            self.assertEqual(ctx.current_audio(), new_audio)
            self.assertEqual(ctx.current_video(), new_video)
            self.assertEqual(ctx.project["config"]["input_text"], "作品 B 的独立稿件。")

    def test_publish_copy_accepts_real_adapter_string_tags_and_metadata_list_tags(self):
        metadata = {"titles": ["实际返回的标题", "备选标题", "另一个标题"], "hashtags": ["知识分享", "口播"], "description": "本次视频的真实发布正文。", "cover_title": "封面标题"}
        with patch.object(release.release_assets, "generate_metadata", return_value=metadata):
            actual = release._generate_copy("本次口播稿件")
        self.assertIsInstance(actual["tags"], str)
        for result, tags in ((actual, "#知识分享 #口播"), (metadata, "知识分享 口播")):
            with self.subTest(tags=tags), self.state({}, {"project": "work-a"}) as (session, _):
                self.completed(session, "publish_copy", result)
                controller.ReferenceContext()
                self.assertEqual(session["ref_publish_title"], "实际返回的标题")
                self.assertEqual(session["ref_publish_tags"], tags)
                self.assertEqual(session["ref_publish_description"], metadata["description"])
        self.update_mock.assert_not_called()

    def test_clearing_draft_remains_empty_on_rerun_without_creating_invalid_project(self):
        with self.state({}, {"project": "work-a"}) as (session, _):
            ctx = controller.ReferenceContext()
            session["ref_script_text"] = ""
            ctx.save_script("")
            controller.ReferenceContext()
            self.assertEqual(session["ref_script_text"], "")
            self.assertEqual(self.projects["work-a"]["config"]["input_text"], "作品 A 的已保存真实稿件。")
        self.update_mock.assert_not_called()
        self.create_mock.assert_not_called()

    def test_busy_project_or_job_rejects_duplicate_queue_and_pipeline_submit(self):
        for source in ("project", "job"):
            with self.subTest(source=source), self.state({}, {"project": "work-a"}) as (session, _):
                if source == "project":
                    self.projects["work-a"]["state"] = "running"
                else:
                    self.projects["work-a"]["state"] = "draft"
                    session["ref_pending_actions"] = {"running-job": {"kind": "extract", "project_id": "work-a", "label": "已有任务"}}
                    self.job_rows["running-job"] = {"state": "queued"}
                ctx = controller.ReferenceContext()
                self.assertTrue(ctx.busy)
                with self.assertRaisesRegex(ValueError, "正在处理"):
                    ctx.queue("script", "重复请求", lambda: None)
                with self.assertRaisesRegex(ValueError, "正在处理"):
                    ctx.submit_stage("voice")
        self.submit_mock.assert_not_called()
        self.pipeline_mock.assert_not_called()
        self.update_mock.assert_not_called()

    def test_queue_label_and_operation_title_are_independent_and_second_request_is_blocked(self):
        def operation(title):
            return {"text": title}
        with self.state({}, {"project": "work-a"}) as (session, _):
            ctx = controller.ReferenceContext()
            ident = ctx.queue("script", "撰写文案任务", operation, title="业务文案标题")
            self.submit_mock.assert_called_once_with("撰写文案任务", operation, title="业务文案标题")
            self.assertEqual(session["ref_pending_actions"][ident]["project_id"], "work-a")
            self.assertEqual(session["ref_pending_actions"][ident]["kind"], "script")
            with self.assertRaises(ValueError):
                ctx.queue("script", "重复任务", operation, title="不能再次发送")
        self.assertEqual(self.submit_mock.call_count, 1)

    def test_changed_script_clears_bound_old_audio_video_and_local_preview_caches(self):
        audio, video = self.media("old-script.wav"), self.media("old-script.mp4")
        self.projects["work-a"]["config"].update(audio_path=audio, source_video_path=video)
        with self.state({}, {"project": "work-a"}) as (session, _):
            ctx = controller.ReferenceContext()
            session["ref_voice_audio_result"] = {"audio_path": audio}
            session["ref_imported_video"] = video
            text = "已经修改的新稿，旧音轨和旧视频不能继续用于当前作品。"
            session["ref_script_text"] = text
            ctx.save_script(text)
            submitted = self.update_mock.call_args.args[1]
            self.assertEqual(submitted["input_text"], text)
            self.assertEqual(submitted["audio_path"], "")
            self.assertEqual(submitted["source_video_path"], "")
            self.assertNotIn("ref_voice_audio_result", session)
            self.assertNotIn("ref_imported_video", session)
            self.assertEqual(ctx.project["config"]["audio_path"], "")

    def test_failed_job_preserves_current_edits_and_does_not_store_error_as_script(self):
        with self.state({}, {"project": "work-a"}) as (session, _):
            controller.ReferenceContext()
            session["ref_script_text"] = "失败之前仍在编辑器里的手工修改。"
            self.completed(session, "script", {"text": "不应该采用的内容"})
            self.job_rows["finished-job"].update(state="failed", message="文案模型连接失败")
            controller.ReferenceContext()
            self.assertEqual(session["ref_script_text"], "失败之前仍在编辑器里的手工修改。")
            self.assertEqual(session["ref_last_error"], "文案模型连接失败")
            self.assertEqual(session["ref_pending_actions"], {})
        self.update_mock.assert_not_called()

    def test_one_click_default_is_a_ui_preference_not_an_unknown_workflow_field(self):
        with self.state({}, {"project": "work-a"}) as (session, _):
            ctx = controller.ReferenceContext()
            self.assertEqual(session["ref_creation_mode"], "one_click")
            self.assertNotIn("creation_mode", ctx._form_config())
            session["ref_creation_mode"] = "step_by_step"
            controller.ReferenceContext()
            self.assertEqual(session["ref_creation_mode"], "step_by_step")

    def test_import_after_native_mode_widget_defers_full_mode_without_state_error(self):
        video = self.media("native-import.mp4")
        self.projects["work-a"]["config"].update(kind="knowledge", avatar_mode="mixed")
        app = AppTest.from_string(SHELL + """
st.radio('画面组织', ['mixed', 'full'], key='ref_avatar_mode', persist_state='session')
if st.button('导入原片', key='test_import_video'):
    ctx.use_media('video', st.session_state['test_video'])
    st.session_state['test_import_config'] = ctx._form_config()
""", default_timeout=30)
        app.session_state["test_requested_project"] = "work-a"
        app.session_state["test_video"] = video
        app.run()
        app.button(key="test_import_video").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="ref_avatar_mode").value, "mixed")
        self.assertEqual(app.session_state["test_import_config"]["avatar_mode"], "full")
        self.assertEqual(app.session_state["test_import_config"]["kind"], "avatar")
        self.assertEqual(self.projects["work-a"]["config"]["source_video_path"], video)
        self.assertEqual(self.projects["work-a"]["config"]["avatar_mode"], "full")
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="ref_avatar_mode").value, "full")
        self.assertNotIn("ref_media_preferences_pending", app.session_state)
        self.submit_mock.assert_not_called()

    def test_same_event_pipeline_cannot_replace_imported_full_video_with_old_mixed_radio(self):
        video, audio = self.media("owned-video.mp4"), self.media("previous-voice.wav")
        self.projects["work-a"]["config"].update(kind="knowledge", avatar_mode="mixed", audio_path=audio, allow_reference_reuse=True)
        with self.state({}, {"project": "work-a"}) as (session, _):
            ctx = controller.ReferenceContext()
            session["ref_voice_audio_result"] = {"audio_path": audio}
            ctx.use_media("video", video)
            self.assertEqual(session["ref_avatar_mode"], "mixed")
            self.assertNotIn("ref_voice_audio_result", session)
            ctx.submit_stage("release")
            config = self.projects["work-a"]["config"]
            self.assertEqual(config["source_video_path"], video)
            self.assertEqual(config["kind"], "avatar")
            self.assertEqual(config["avatar_mode"], "full")
            self.assertFalse(config["allow_reference_reuse"])
            self.assertEqual(config["audio_path"], "")
            self.assertEqual(ctx.current_video(), video)
            self.assertEqual(ctx.current_video(rendered=True), "")
        self.pipeline_mock.assert_called_once_with("work-a", until_stage="release")
        self.submit_mock.assert_not_called()

    def test_empty_draft_with_voiced_video_queues_real_asr_then_creates_bound_project(self):
        video = self.media("voiced-original.mp4")
        with self.state() as (session, query):
            ctx = controller.ReferenceContext()
            ident = ctx.use_media("video", video)
            self.submit_mock.assert_called_once_with("识别导入口播视频", controller.extract.extract_media,
                                                      video, language="zh", model_size="small")
            self.assertEqual(ctx.project, {})
            self.create_mock.assert_not_called()
            meta = session["ref_pending_actions"][ident]
            self.assertEqual(meta["kind"], "video_import_extract")
            self.assertEqual(meta["project_id"], "")
            self.assertEqual(meta["source_video_path"], video)
            self.assertEqual(meta["import_scope"], session["ref_video_import_scope"])
            initial_scope = session["ref_video_import_scope"]
            self.assertTrue(ctx.busy)
            self.finish_video_import(ident, video)
            result = self.job_rows[ident]["result"]
            loaded = controller.ReferenceContext()
            self.assertEqual(loaded.project["id"], "created-work")
            self.assertEqual(query["project"], "created-work")
            self.assertEqual(session["ref_video_import_scope"], initial_scope)
            self.assertEqual(session["ref_original_text"], result["text"])
            self.assertEqual(session["ref_script_text"], result["text"])
            self.assertEqual(session["ref_avatar_mode"], "full")
            self.assertEqual(loaded.project["config"]["source_video_path"], video)
            self.assertEqual(loaded.project["config"]["kind"], "avatar")
            self.assertEqual(loaded.project["config"]["audio_path"], "")
            self.assertEqual(loaded.stage("voice"), {}, "No ASR checkpoint or temporary audio can be smuggled into the voice stage")
            self.assertEqual(loaded.current_video(), video)
            self.assertEqual(loaded.current_video(rendered=True), "")
            self.assertEqual(session["ref_pending_actions"], {})
            controller.ReferenceContext()
            self.assertEqual(session["ref_video_import_scope"], initial_scope)
        self.create_mock.assert_called_once()
        self.update_mock.assert_not_called()

    def test_existing_project_with_cleared_editor_binds_recognition_to_that_project(self):
        video = self.media("existing-project-import.mp4")
        self.projects["work-a"]["config"].update(language="auto", model_size="base")
        with self.state({}, {"project": "work-a"}) as (session, _):
            ctx = controller.ReferenceContext()
            session["ref_script_text"] = ""
            ident = ctx.use_media("video", video)
            self.assertEqual(session["ref_pending_actions"][ident]["project_id"], "work-a")
            self.assertEqual(self.submit_mock.call_args.kwargs, {"language": "auto", "model_size": "base"})
            self.finish_video_import(ident, video)
            loaded = controller.ReferenceContext()
            self.assertEqual(loaded.project["id"], "work-a")
            self.assertEqual(loaded.project["config"]["input_text"], self.job_rows[ident]["result"]["text"])
            self.assertEqual(loaded.project["config"]["source_video_path"], video)
            self.assertEqual(session["ref_avatar_mode"], "full")
            self.assertEqual(self.projects["work-b"]["config"]["source_video_path"], "")
        self.create_mock.assert_not_called()
        self.update_mock.assert_called_once()

    def test_silent_video_without_script_requires_real_narration_input(self):
        video = self.media("silent-original.mp4")
        self.video_info[video] = {"has_video": True, "has_audio": False}
        with self.state():
            ctx = controller.ReferenceContext()
            with self.assertRaisesRegex(ValueError, "没有声音.*文案.*配音"):
                ctx.use_media("video", video)
        self.submit_mock.assert_not_called()
        self.create_mock.assert_not_called()
        self.update_mock.assert_not_called()

    def test_invalid_video_is_rejected_before_project_or_recognition_is_started(self):
        video = self.media("invalid-original.mp4")
        self.video_info[video] = {"has_video": False, "has_audio": True}
        with self.state({}, {"project": "work-a"}):
            ctx = controller.ReferenceContext()
            with self.assertRaisesRegex(ValueError, "有效画面"):
                ctx.use_media("video", video)
        self.submit_mock.assert_not_called()
        self.update_mock.assert_not_called()

    def test_pending_import_blocks_duplicate_import_without_probing_or_submitting_again(self):
        video = self.media("once-original.mp4")
        with self.state():
            ctx = controller.ReferenceContext()
            ctx.use_media("video", video)
            with self.assertRaisesRegex(ValueError, "正在处理"):
                ctx.use_media("video", video)
        self.submit_mock.assert_called_once()
        self.probe_mock.assert_called_once()
        self.create_mock.assert_not_called()

    def test_old_project_and_unbound_video_recognition_cannot_attach_to_new_project(self):
        video = self.media("previous-project-import.mp4")
        for project_id in ("", "work-a"):
            with self.subTest(project=project_id), self.state({}, {"project": project_id} if project_id else {}) as (session, query):
                ctx = controller.ReferenceContext()
                session["ref_script_text"] = ""
                ident = ctx.use_media("video", video)
                self.finish_video_import(ident, video)
                query["project"] = "work-b"
                loaded = controller.ReferenceContext()
                self.assertEqual(loaded.project["id"], "work-b")
                self.assertEqual(session["ref_script_text"], "作品 B 的独立稿件。")
                self.assertEqual(session["ref_avatar_mode"], "mixed")
                self.assertEqual(loaded.project["config"]["source_video_path"], "")
                self.assertNotIn("ref_imported_video", session)
                self.assertEqual(session["ref_pending_actions"], {})
        self.create_mock.assert_not_called()
        self.update_mock.assert_not_called()

    def test_new_empty_draft_has_a_new_scope_and_rejects_the_previous_empty_draft_result(self):
        video = self.media("abandoned-empty-draft.mp4")
        with self.state() as (session, query):
            ctx = controller.ReferenceContext()
            ident = ctx.use_media("video", video)
            old_scope = session["ref_video_import_scope"]
            self.finish_video_import(ident, video)
            session["studio_pending_project"] = ""
            loaded = controller.ReferenceContext()
            self.assertNotEqual(session["ref_video_import_scope"], old_scope)
            self.assertEqual(loaded.project, {})
            self.assertEqual(session["ref_script_text"], "")
            self.assertEqual(query, {})
            self.assertEqual(session["ref_pending_actions"], {})
        self.create_mock.assert_not_called()

    def test_old_import_completion_preserves_new_project_attempted_upload_and_failure_state(self):
        old_video, new_video = self.media("old-project-video.mp4"), self.media("new-project-video.mp4")
        with self.state({}, {"project": "work-a"}) as (session, query):
            ctx = controller.ReferenceContext()
            session["ref_script_text"] = ""
            ident = ctx.use_media("video", old_video)
            query["project"] = "work-b"
            controller.ReferenceContext()
            failure = {"project_id": "work-b", "source_video_path": new_video, "message": "新作品自己的待处理提示"}
            session["ref_imported_voice_video"] = new_video
            session["ref_video_import_failure"] = deepcopy(failure)
            self.finish_video_import(ident, old_video)
            controller.ReferenceContext()
            self.assertEqual(session["ref_imported_voice_video"], new_video)
            self.assertEqual(session["ref_video_import_failure"], failure)
            self.assertEqual(session["ref_script_text"], "作品 B 的独立稿件。")
            self.assertEqual(session["ref_pending_actions"], {})
        self.create_mock.assert_not_called()
        self.update_mock.assert_not_called()

    def test_user_creating_a_project_during_empty_import_keeps_the_new_project_text(self):
        video = self.media("pending-empty-import.mp4")
        with self.state() as (session, _):
            ctx = controller.ReferenceContext()
            ident = ctx.use_media("video", video)
            text = "用户随后明确保存的独立稿件，旧导入结果不能替换。"
            session["ref_script_text"] = text
            ctx.save_script(text)
            self.finish_video_import(ident, video)
            loaded = controller.ReferenceContext()
            self.assertEqual(loaded.project["config"]["input_text"], text)
            self.assertEqual(session["ref_script_text"], text)
            self.assertEqual(loaded.project["config"]["source_video_path"], "")
        self.create_mock.assert_called_once()
        self.update_mock.assert_not_called()

    def test_mutated_video_or_wrong_asr_source_is_not_bound(self):
        for condition in ("changed", "different", "empty_text"):
            with self.subTest(condition=condition), self.state() as (session, _):
                video = self.media("untrusted-" + condition + ".mp4")
                ctx = controller.ReferenceContext()
                ident = ctx.use_media("video", video)
                self.finish_video_import(ident, video)
                session["ref_imported_voice_video"] = video
                if condition == "changed":
                    Path(video).write_bytes(b"the-user-replaced-this-video-after-the-job-was-queued")
                elif condition == "different":
                    self.job_rows[ident]["result"]["media_path"] = self.media("other-asr-source.mp4")
                else:
                    self.job_rows[ident]["result"]["text"] = " "
                loaded = controller.ReferenceContext()
                self.assertEqual(loaded.project, {})
                self.assertEqual(session["ref_script_text"], "")
                self.assertEqual(session["ref_pending_actions"], {})
                self.assertTrue(session.get("ref_last_error"))
                self.assertEqual(session["ref_imported_voice_video"], video)
                self.assertEqual(session["ref_video_import_failure"]["source_video_path"], video)
        self.create_mock.assert_not_called()
        self.update_mock.assert_not_called()

    def test_failed_asr_keeps_attempted_marker_and_only_explicit_retry_queues_again(self):
        video = self.media("failed-asr-video.mp4")
        with self.state() as (session, _):
            ctx = controller.ReferenceContext()
            ident = ctx.use_media("video", video)
            session["ref_imported_voice_video"] = video
            self.job_rows[ident] = {"state": "failed", "message": "未识别到清晰人声，请换一个视频。"}
            controller.ReferenceContext()
            self.assertEqual(session["ref_last_error"], "未识别到清晰人声，请换一个视频。")
            self.assertEqual(session["ref_script_text"], "")
            self.assertEqual(session["ref_imported_voice_video"], video)
            self.assertEqual(session["ref_video_import_failure"]["project_id"], "")
            self.assertEqual(session["ref_video_import_failure"]["source_video_path"], video)
            self.assertEqual(session["ref_pending_actions"], {})
            controller.ReferenceContext()
            controller.ReferenceContext()
            self.submit_mock.assert_called_once()
            controller.ReferenceContext().use_media("video", video)
            self.assertEqual(self.submit_mock.call_count, 2)
            self.assertNotIn("ref_video_import_failure", session)
        self.create_mock.assert_not_called()

    def test_deferred_preferences_for_previous_project_cannot_modify_new_project_mode(self):
        video = self.media("old-deferred-video.mp4")
        with self.state({}, {"project": "work-a"}) as (session, query):
            ctx = controller.ReferenceContext()
            ctx.use_media("video", video)
            self.assertIn("ref_media_preferences_pending", session)
            query["project"] = "work-b"
            loaded = controller.ReferenceContext()
            self.assertEqual(loaded.project["id"], "work-b")
            self.assertEqual(session["ref_avatar_mode"], "mixed")
            self.assertEqual(loaded._form_config()["avatar_mode"], "mixed")
            self.assertNotIn("ref_media_preferences_pending", session)

    def test_one_click_pipeline_hydrates_blank_publish_fields_before_native_widgets(self):
        app = self.app()
        app.text_input(key="ref_publish_title").set_value("").run()
        app.text_input(key="ref_publish_tags").set_value("").run()
        cover = self.media("one-click-cover.png")
        released = {"title": "本次口播首句生成的发布标题", "description": "真实成片发布说明", "hashtags": ["口播", "原创"],
                    "cover_path": cover}
        self.projects["work-a"]["stages"]["release"]["result"] = released
        app.session_state["ref_pending_actions"] = {"one-click-job": {"kind": "pipeline", "project_id": "work-a", "label": "一键成片"}}
        self.job_rows["one-click-job"] = {"state": "done", "result": deepcopy(self.projects["work-a"])}
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_input(key="ref_publish_title").value, released["title"])
        self.assertEqual(app.text_input(key="ref_publish_tags").value, "#口播 #原创")
        self.assertEqual(app.session_state["ref_publish_description"], released["description"])
        self.assertEqual(app.session_state["ref_cover_path"], cover)
        self.update_mock.assert_not_called()

    def test_risk_report_requires_explicit_adoption_and_rejects_stale_text(self):
        with self.state({}, {"project": "work-a"}) as (session, _):
            controller.ReferenceContext()
            source = session["ref_script_text"]
            self.completed(session, "script_review", {"source_text": source, "optimized_text": "审阅后的新稿", "risks": []})
            controller.ReferenceContext()
            self.assertEqual(session["ref_script_text"], source)
            self.assertTrue(session["ref_script_review_open"])
            session["ref_script_text"] = "用户已经重新修改"
            session["ref_script_review_adopt_pending"] = {"source_text": source, "optimized_text": "审阅后的新稿", "project_id": "work-a"}
            controller.ReferenceContext()
            self.assertEqual(session["ref_script_text"], "用户已经重新修改")
        self.update_mock.assert_not_called()

    def test_scrapling_collection_shows_result_without_overwriting_the_manuscript(self):
        with self.state({}, {"project": "work-a"}) as (session, _):
            controller.ReferenceContext()
            source = session["ref_script_text"]
            result = {"engine": "scrapling", "status": "needs_user", "message": "需要核查来源", "imported": 0}
            self.completed(session, "viral_collect", result)
            controller.ReferenceContext()
            self.assertEqual(session["ref_viral_result"], result)
            self.assertEqual(session["ref_script_text"], source)
            self.assertEqual(session["ref_script_dialog"], "library")
        self.update_mock.assert_not_called()

    def test_explicit_risk_adoption_is_applied_before_native_widget_initialization(self):
        app = self.app()
        source = app.text_area(key="ref_script_text").value
        app.session_state["ref_script_review_adopt_pending"] = {"source_text": source, "optimized_text": "确定采用的口播正文", "project_id": "work-a"}
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area(key="ref_script_text").value, "确定采用的口播正文")
        self.assertEqual(self.projects["work-a"]["config"]["input_text"], "确定采用的口播正文")

    def test_one_click_pipeline_preserves_each_nonblank_publish_edit(self):
        with self.state({}, {"project": "work-a"}) as (session, _):
            controller.ReferenceContext()
            session.update(ref_publish_title="手工定稿标题", ref_publish_description="手工定稿正文", ref_publish_tags="#手工标签")
            self.projects["work-a"]["stages"]["release"]["result"] = {
                "title": "后台默认标题", "description": "后台默认正文", "hashtags": ["后台标签"],
            }
            self.completed(session, "pipeline", deepcopy(self.projects["work-a"]))
            controller.ReferenceContext()
            self.assertEqual(session["ref_publish_title"], "手工定稿标题")
            self.assertEqual(session["ref_publish_description"], "手工定稿正文")
            self.assertEqual(session["ref_publish_tags"], "#手工标签")
        self.update_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
