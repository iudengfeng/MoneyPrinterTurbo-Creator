"""Production controls must preserve edits and dispatch honest media operations."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services.creator import narration


class ReferenceContext:
    def __init__(self):
        self.project = {"state": "draft", "stages": {}}
        self.submissions = []
        self.queued = []
        self.media = []
        self.tools = []
        self.audio = ""
        self.source = ""
        self.finished = ""

    def stage(self, name):
        return self.project["stages"].get(name, {}).get("result", {})

    def queue(self, kind, title, operation, *args, **kwargs):
        self.queued.append((kind, title, operation, args, kwargs))

    def submit_stage(self, stage, changes=None):
        self.submissions.append((stage, changes))

    def current_audio(self):
        return self.audio

    def current_video(self, rendered=False):
        return self.finished if rendered else self.source

    def use_media(self, kind, path):
        self.media.append((kind, path))

    def stage_upload(self, upload):
        return str(upload)

    def open_tool(self, name):
        self.tools.append(name)

    def report_job_state(self):
        return None


class ReferenceProductionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.context = ReferenceContext()
        self.voices = [
            {"id": "edge:first", "name": "标准女声", "provider": "edge"},
            {"id": "duix:12", "name": "我的声音", "provider": "duix"},
        ]
        self.profiles = [{"id": "duix:4", "name": "我的形象", "available": True}]
        self.patchers = [
            patch("app.services.creator.narration.list_options", return_value=self.voices),
            patch("app.services.creator.narration.list_narrations", return_value=[]),
            patch("app.services.creator.avatar.list_options", side_effect=lambda: self.profiles),
            patch("app.services.creator.avatar.status", return_value={"available": True}),
            patch("app.services.creator.narration.generate", side_effect=AssertionError("Unexpected provider request")),
        ]
        self.voice_options, self.history, self.profile_options, self.engine, self.provider = [item.start() for item in self.patchers]

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.directory.cleanup()

    def file(self, name):
        path = Path(self.directory.name) / name
        path.write_bytes(b"real-file-ui-fixture")
        return str(path)

    def app(self, **state):
        app = AppTest.from_string("""import streamlit as st
from webui.creator_reference_production import render_voice_column, render_processing_column
ctx = st.session_state['test_ctx']
left, right = st.columns(2)
with left:
    render_voice_column(ctx)
with right:
    render_processing_column(ctx)
""", default_timeout=30)
        defaults = {
            "test_ctx": self.context, "ref_script_text": "这份完整口播文案应该用于本次制作。",
            "ref_voice_id": "edge:first", "ref_avatar_id": "duix:4", "ref_speed": 1.0,
            "ref_resolution": "720P", "ref_subtitles": True,
            "ref_bgm_enabled": False, "ref_bgm_path": "",
        }
        defaults.update(state)
        for key, value in defaults.items():
            app.session_state[key] = value
        app.run()
        self.assertFalse(app.exception)
        return app

    def test_narration_dispatches_complete_script_selected_voice_and_real_speed(self):
        app = self.app()
        app.selectbox(key="ref_voice_id").set_value("duix:12").run()
        app.selectbox(key="ref_speed").set_value(0.8).run()
        app.button(key="ref_generate_voice").click().run()
        self.assertFalse(app.exception)
        kind, title, operation, args, kwargs = self.context.queued[0]
        self.assertEqual((kind, title), ("voice_audio", "生成语音"))
        self.assertIs(operation, narration.generate)
        self.assertEqual(args, ("这份完整口播文案应该用于本次制作。", "duix:12"))
        self.assertEqual(kwargs, {"speed": 0.8, "emotion": "自然"})
        self.provider.assert_not_called()
        app.run()
        self.assertEqual(app.selectbox(key="ref_voice_id").value, "duix:12")
        self.assertEqual(app.selectbox(key="ref_speed").value, 0.8)

    def test_avatar_modes_dispatch_without_reference_loop_or_old_source(self):
        app = self.app()
        app.radio(key="ref_avatar_mode").set_value("full").run()
        app.button(key="ref_generate_avatar").click().run()
        self.assertEqual(self.context.submissions, [("visuals", {
            "kind": "avatar", "avatar_id": "duix:4", "avatar_mode": "full",
            "allow_reference_reuse": False, "aspect": "9:16", "source_video_path": "",
        })])
        self.engine.assert_called_once()

    def test_unavailable_resolution_and_service_are_explicit_errors(self):
        app = self.app()
        self.assertEqual(app.radio(key="ref_resolution").value, "720P")
        app.radio(key="ref_resolution").set_value("1080P").run()
        app.button(key="ref_generate_avatar").click().run()
        self.assertFalse(self.context.submissions)
        self.assertTrue(any("仅支持 720P" in item.value for item in app.error))
        app.radio(key="ref_resolution").set_value("720P").run()
        self.engine.return_value = {"available": False, "reason": "本机 Duix 尚未配置"}
        app.button(key="ref_generate_avatar").click().run()
        self.assertFalse(self.context.submissions)
        self.assertTrue(any("尚未配置" in item.value for item in app.error))

    def test_missing_profile_remains_empty_and_opens_existing_identity_tool(self):
        self.profiles = []
        app = self.app()
        self.assertIsNone(app.selectbox(key="ref_avatar_id").value)
        self.assertEqual(app.selectbox(key="ref_avatar_id").proto.placeholder, "请选择形象")
        app.button(key="ref_generate_avatar").click().run()
        self.assertFalse(self.context.submissions)
        self.assertTrue(any("请选择形象" in item.value for item in app.error))
        app.button(key="ref_manage_avatar").click().run()
        self.assertEqual(self.context.tools, ["数字人"])

    def test_render_subtitles_music_and_styles_reach_pipeline_and_edits_survive(self):
        music = self.file("chosen-track.wav")
        app = self.app(ref_bgm_path=music)
        self.assertTrue(app.toggle(key="ref_subtitles").value)
        self.assertFalse(app.toggle(key="ref_bgm_enabled").value)
        app.toggle(key="ref_subtitles").set_value(False).run()
        app.toggle(key="ref_bgm_enabled").set_value(True).run()
        app.slider(key="ref_bgm_volume").set_value(0.23).run()
        app.selectbox(key="ref_color_grade").set_value("warm").run()
        app.selectbox(key="ref_video_fit").set_value("cover").run()
        app.button(key="ref_generate_render").click().run()
        self.assertEqual(self.context.submissions, [("render", {
            "subtitle_style": "none", "bgm_path": music, "bgm_volume": 0.23,
            "color_grade": "warm", "video_fit": "cover",
        })])
        app.run()
        self.assertFalse(app.toggle(key="ref_subtitles").value)
        self.assertEqual(app.slider(key="ref_bgm_volume").value, 0.23)

    def test_music_missing_and_duplicate_burned_subtitles_block_submission(self):
        app = self.app(ref_bgm_enabled=True, ref_bgm_path=str(Path(self.directory.name) / "moved.mp3"))
        app.button(key="ref_generate_render").click().run()
        self.assertFalse(self.context.submissions)
        self.assertTrue(any("上传可用音乐" in item.value for item in app.error))
        app.toggle(key="ref_bgm_enabled").set_value(False).run()
        self.context.project["stages"]["visuals"] = {"result": {"subtitles_burned": True}}
        app.button(key="ref_generate_render").click().run()
        self.assertFalse(self.context.submissions)
        self.assertTrue(any("重复叠加" in item.value for item in app.error))

    def test_highlight_drafts_are_preserved_and_never_claimed_or_dispatched(self):
        app = self.app()
        app.text_area(key="ref_highlight_main").set_value("核心观点").run()
        app.radio(key="ref_highlight_group").set_value("行动词").run()
        app.text_area(key="ref_highlight_action").set_value("立即开始").run()
        app.radio(key="ref_highlight_group").set_value("主词").run()
        self.assertEqual(app.text_area(key="ref_highlight_main").value, "核心观点")
        self.assertTrue(any("未应用高亮" in item.value for item in app.caption))
        app.button(key="ref_generate_render").click().run()
        self.assertFalse(any("highlight" in key for key in self.context.submissions[0][1]))

    def test_previews_and_download_require_current_existing_files(self):
        self.context.audio = self.file("current.wav")
        self.context.source = self.file("source.mp4")
        self.context.finished = self.file("final.mp4")
        app = self.app()
        self.assertEqual(len(app.get("audio")), 1)
        self.assertEqual(len(app.get("video")), 2)
        self.assertEqual([item.proto.label for item in app.get("download_button")], ["下载成片"])
        Path(self.context.finished).unlink()
        app.run()
        self.assertFalse(app.get("download_button"))
        self.assertEqual(len(app.get("video")), 1)

    def test_empty_script_does_not_dispatch_voice_and_history_requires_explicit_use(self):
        audio = self.file("history.wav")
        self.history.return_value = [{"id": "voice-history", "voice_name": "上一份语音", "text": "历史文案", "audio_path": audio}]
        app = self.app(ref_script_text="")
        app.button(key="ref_generate_voice").click().run()
        self.assertFalse(self.context.queued)
        self.assertTrue(any("完整口播文案" in item.value for item in app.error))
        self.assertIsNone(app.selectbox(key="ref_audio_history").value)
        self.assertFalse(self.context.media)
        app.selectbox(key="ref_audio_history").set_value("voice-history").run()
        app.button(key="ref_use_audio_history").click().run()
        self.assertEqual(self.context.media, [("audio", audio)])

    def test_independent_audio_job_blocks_overlapping_generation_and_settings(self):
        self.context.busy = True
        app = self.app()
        for key in ("ref_generate_voice", "ref_generate_avatar", "ref_generate_render"):
            self.assertTrue(app.button(key=key).disabled)
        self.assertTrue(app.selectbox(key="ref_voice_id").disabled)
        self.assertTrue(app.toggle(key="ref_subtitles").disabled)
        self.assertFalse(self.context.submissions)
        self.assertFalse(self.context.queued)


if __name__ == "__main__":
    unittest.main()
