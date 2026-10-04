"""Exercise editing provenance, navigation and queue outcomes with a fake renderer."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services.creator import jobs, store


_PRESETS = [
    {"id": "clean", "name": "清爽口播", "description": "白字、原色", "color_grade": "none", "subtitle_style": "clean"},
    {"id": "bold", "name": "网感大字", "description": "鲜明画面", "color_grade": "vivid", "subtitle_style": "bold"},
    {"id": "knowledge", "name": "知识分享", "description": "重点黄字", "color_grade": "none", "subtitle_style": "yellow"},
    {"id": "business", "name": "简约商务", "description": "清爽冷色", "color_grade": "cool", "subtitle_style": "clean"},
]


class RenderWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.directory.name})
        self.environment.start()
        self.video = self.file("reference.mp4")
        self.audio = self.file("complete.wav")
        self.music = self.file("music.mp3")
        self.subtitle = self.file("corresponding.srt")
        self.picture = self.file("picture.png")
        self.library = []
        self.history = []
        self.patchers = [
            patch("app.services.creator.rendering.list_presets", return_value=_PRESETS, create=True),
            patch("app.services.creator.rendering.list_bgm", side_effect=lambda: self.library, create=True),
            patch("app.services.creator.rendering.list_renders", side_effect=lambda: store.list_records("renders"), create=True),
            patch("app.services.creator.rendering.probe_source", side_effect=self.probe, create=True),
            patch("app.services.creator.rendering.render_video", side_effect=AssertionError("Unexpected render request")),
            patch("app.services.creator.avatar.list_jobs", side_effect=lambda: self.history),
        ]
        self.presets_mock, self.music_mock, self.renders_mock, self.probe_mock, self.provider, self.history_mock = [item.start() for item in self.patchers]

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.environment.stop()
        self.directory.cleanup()

    def file(self, name):
        path = store.data_root() / name
        path.write_bytes(b"opaque-media-for-ui-test")
        return str(path)

    def probe(self, path):
        return {"path": path, "duration": 7.32, "width": 720, "height": 1280,
            "has_audio": True, "has_video": Path(path).suffix.lower() == ".mp4"}

    def app(self, current=True, **state):
        # Switching this small test router exercises Streamlit widget cleanup,
        # rather than calling the editing view repeatedly in isolation.
        app = AppTest.from_string("""import streamlit as st
from webui.creator_render_workspace import render
page = st.radio('测试导航', ['模板剪辑', '配音'], key='test_page')
if page == '模板剪辑':
    render()
else:
    st.write('配音测试页')
""", default_timeout=30)
        if current:
            app.session_state["creator_video_path"] = self.video
            app.session_state["creator_render_aspect"] = "9:16"
        for key, value in state.items():
            app.session_state[key] = value
        app.run()
        self.assertFalse(app.exception)
        return app

    def button(self, app, label):
        return next(item for item in app.button if item.label == label)

    def render_result(self, video_path, **kwargs):
        ident = store.new_id()
        path = store.data_root() / (ident + ".mp4")
        path.write_bytes(b"opaque-finished-video")
        return store.save_record("renders", ident, {"video_path": str(path), "source_video_path": video_path,
            "title": kwargs.get("title", ""), "aspect": kwargs.get("aspect", "9:16"), "duration": 7.32,
            "style": kwargs.get("style", "clean"), "srt_path": kwargs.get("subtitle_path") or ""})

    def collect(self, app):
        deadline = time.monotonic() + 10
        while "creator_render_pending" in app.session_state:
            row = jobs.get_job(app.session_state["creator_render_pending"]["id"])
            if row and row["state"] not in {"running", "queued"}:
                app.run()
                break
            self.assertLess(time.monotonic(), deadline, "Render job did not finish")
            time.sleep(0.02)
        notices = [item.value for item in app.error] + [item.value for item in app.success]
        # A completed fragment rerun can leave two UI trees in AppTest. Refresh
        # the stable page before sending the next user edit to its widgets.
        app.run()
        self.assertFalse(app.exception)
        self.assertNotIn("creator_render_pending", app.session_state)
        for controls in (app.text_input, app.checkbox):
            ids = [item.id for item in controls]
            self.assertEqual(len(ids), len(set(ids)), "Completed page must have unique controls")
        return notices

    def test_empty_source_cannot_submit_but_options_remain_available(self):
        app = self.app(current=False)
        self.assertEqual(app.radio(key="creator_render_source").value, "上传本地视频")
        self.assertTrue(self.button(app, "开始剪辑").disabled)
        self.assertFalse(self.button(app, "✓ 清爽口播").disabled)
        self.provider.assert_not_called()

    def test_current_video_uses_original_audio_and_maps_auto_style(self):
        app = self.app()
        app.text_input(key="creator_render_title").set_value("本次作品").run()
        app.button(key="creator_render_preset_bold").click().run()
        self.assertFalse(self.button(app, "开始剪辑").disabled)
        with patch("app.services.creator.rendering.render_video", side_effect=self.render_result) as render:
            self.button(app, "开始剪辑").click().run()
            notices = self.collect(app)
            render.assert_called_once()
            self.assertEqual(render.call_args.args, (self.video,))
            request = render.call_args.kwargs
            self.assertIsNone(request["audio_path"])
            self.assertTrue(request["auto_subtitles"])
            self.assertFalse(request["source_subtitles_burned"])
            self.assertEqual(request["style"], "bold")
            self.assertEqual(request["subtitle_style"], "bold")
            self.assertEqual(request["color_grade"], "vivid")
            self.assertEqual(request["video_fit"], "contain")
            self.assertEqual(request["title"], "本次作品")
        self.assertEqual(app.session_state["creator_render_result"]["title"], "本次作品")
        self.assertTrue(any("成片已完成" in message for message in notices))
        self.assertTrue(any(item.proto.label == "下载成片" for item in app.get("download_button")))

    def test_existing_burned_subtitles_are_never_duplicated(self):
        app = self.app(creator_video_has_subtitles=True)
        self.assertEqual(app.selectbox(key="creator_render_subtitle_mode").value, "保留视频原有字幕")
        with patch("app.services.creator.rendering.render_video", side_effect=self.render_result) as render:
            self.button(app, "开始剪辑").click().run()
            self.collect(app)
            self.assertTrue(render.call_args.kwargs["source_subtitles_burned"])
            self.assertFalse(render.call_args.kwargs["auto_subtitles"])
            self.assertEqual(render.call_args.kwargs["subtitle_style"], "none")
            self.assertIsNone(render.call_args.kwargs["subtitle_path"])

    def test_formal_narration_direct_handoff_is_kept_on_first_editing_visit(self):
        app = self.app(creator_render_audio=self.audio, creator_render_replace_audio=True)
        self.assertTrue(app.checkbox(key="creator_render_replace_audio").value)
        self.assertTrue(any("不会重新生成数字人的嘴型" in item.value for item in app.caption))
        with patch("app.services.creator.rendering.render_video", side_effect=self.render_result) as render:
            self.button(app, "开始剪辑").click().run()
            self.collect(app)
            self.assertEqual(render.call_args.kwargs["audio_path"], self.audio)
            self.assertTrue(render.call_args.kwargs["auto_subtitles"])

    def test_known_burned_video_cannot_add_second_caption_layer(self):
        app = self.app(creator_video_has_subtitles=True)
        app.selectbox(key="creator_render_subtitle_mode").set_value("自动识别生成").run()
        self.assertTrue(self.button(app, "开始剪辑").disabled)
        self.assertTrue(any("叠成两层" in item.value for item in app.warning))
        app.selectbox(key="creator_render_subtitle_mode").set_value("不添加字幕").run()
        self.assertFalse(self.button(app, "开始剪辑").disabled)
        self.provider.assert_not_called()

    def test_custom_audio_srt_color_and_pip_reach_renderer_exactly(self):
        app = self.app(creator_render_audio=self.audio, creator_srt_path=self.subtitle)
        app.radio(key="creator_render_mode").set_value("自定义").run()
        app.selectbox(key="creator_render_color_grade").set_value("warm").run()
        app.selectbox(key="creator_render_subtitle_style").set_value("yellow").run()
        app.selectbox(key="creator_render_video_fit").set_value("cover").run()
        app.checkbox(key="creator_render_pip_enabled").check().run()
        app.session_state["creator_render_pip_0_import"] = {"path": self.picture, "name": "示意图.png"}
        app.run()
        app.number_input(key="creator_render_pip_0_start").set_value(1.2).run()
        app.number_input(key="creator_render_pip_0_end").set_value(5.5).run()
        app.selectbox(key="creator_render_pip_0_position").set_value("bottom-left").run()
        app.slider(key="creator_render_pip_0_size").set_value(0.4).run()
        with patch("app.services.creator.rendering.render_video", side_effect=self.render_result) as render:
            self.button(app, "开始剪辑").click().run()
            self.collect(app)
            request = render.call_args.kwargs
            self.assertEqual(request["audio_path"], self.audio)
            self.assertEqual(request["subtitle_path"], self.subtitle)
            self.assertFalse(request["auto_subtitles"])
            self.assertEqual(request["color_grade"], "warm")
            self.assertEqual(request["subtitle_style"], "yellow")
            self.assertEqual(request["video_fit"], "cover")
            self.assertEqual(request["pip_items"], [{"path": self.picture, "start": 1.2, "end": 5.5, "position": "bottom-left", "size": 0.4}])

    def test_navigation_preserves_title_aspect_custom_choices_and_saved_uploads(self):
        app = self.app()
        app.text_input(key="creator_render_title").set_value("导航后继续剪辑").run()
        app.selectbox(key="creator_render_aspect").set_value("16:9").run()
        app.selectbox(key="creator_render_video_fit").set_value("cover").run()
        app.radio(key="creator_render_mode").set_value("自定义").run()
        app.selectbox(key="creator_render_color_grade").set_value("cool").run()
        app.selectbox(key="creator_render_subtitle_mode").set_value("使用字幕文件").run()
        app.session_state["creator_render_subtitle_import"] = {"path": self.subtitle, "name": "新配音字幕.srt"}
        app.run()
        app.radio(key="test_page").set_value("配音").run()
        app.radio(key="test_page").set_value("模板剪辑").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_input(key="creator_render_title").value, "导航后继续剪辑")
        self.assertEqual(app.selectbox(key="creator_render_aspect").value, "16:9")
        self.assertEqual(app.selectbox(key="creator_render_video_fit").value, "cover")
        self.assertEqual(app.radio(key="creator_render_mode").value, "自定义")
        self.assertEqual(app.selectbox(key="creator_render_color_grade").value, "cool")
        self.assertEqual(app.selectbox(key="creator_render_subtitle_mode").value, "使用字幕文件")
        self.assertFalse(self.button(app, "开始剪辑").disabled)
        self.assertEqual(self.probe_mock.call_count, 1)

    def test_true_music_library_defaults_first_track_and_respects_user_switch_off(self):
        self.library = [{"id": "first", "name": "本机音乐 01", "path": self.music}]
        app = self.app()
        self.assertTrue(app.checkbox(key="creator_render_bgm_enabled").value)
        self.assertEqual(app.selectbox(key="creator_render_bgm_id").value, "first")
        app.slider(key="creator_render_bgm_volume").set_value(0.23).run()
        with patch("app.services.creator.rendering.render_video", side_effect=self.render_result) as render:
            self.button(app, "开始剪辑").click().run()
            self.collect(app)
            self.assertEqual(render.call_args.kwargs["bgm_path"], self.music)
            self.assertEqual(render.call_args.kwargs["bgm_volume"], 0.23)
        app.checkbox(key="creator_render_bgm_enabled").uncheck().run()
        app.radio(key="test_page").set_value("配音").run()
        app.radio(key="test_page").set_value("模板剪辑").run()
        self.assertFalse(app.checkbox(key="creator_render_bgm_enabled").value)

    def test_title_survives_widget_cleanup_and_respects_intentional_clear(self):
        app = self.app()
        app.text_input(key="creator_render_title").set_value("页面切换后保留这份标题").run()
        app.radio(key="test_page").set_value("配音").run()
        # Reproduce an intermittently discarded widget state observed in the
        # installed browser. The editing draft must outlive that widget key.
        del app.session_state["creator_render_title"]
        app.radio(key="test_page").set_value("模板剪辑").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_input(key="creator_render_title").value, "页面切换后保留这份标题")
        app.text_input(key="creator_render_title").set_value("").run()
        app.radio(key="test_page").set_value("配音").run()
        del app.session_state["creator_render_title"]
        app.radio(key="test_page").set_value("模板剪辑").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_input(key="creator_render_title").value, "")

    def test_history_never_preselects_or_reuses_previous_source_subtitle_and_audio(self):
        second = self.file("other.mp4")
        self.history = [{"id": "ready", "state": "done", "video_path": second, "model_name": "我的人物", "script": "另一份配音"},
            {"id": "failed", "state": "failed", "video_path": self.video, "model_name": "失败任务"}]
        app = self.app(creator_render_audio=self.audio, creator_srt_path=self.subtitle)
        app.session_state["creator_render_subtitle_import"] = {"path": self.subtitle, "name": "旧字幕.srt"}
        app.radio(key="creator_render_source").set_value("历史口播").run()
        choice = app.selectbox(key="creator_render_history_id")
        self.assertIsNone(choice.value)
        self.assertEqual(len(choice.options), 1)
        self.assertTrue(self.button(app, "开始剪辑").disabled)
        choice.set_value("ready").run()
        self.assertFalse(app.checkbox(key="creator_render_replace_audio").value)
        self.assertEqual(app.selectbox(key="creator_render_subtitle_mode").value, "自动识别生成")
        self.assertNotIn("creator_render_subtitle_import", app.session_state)
        app.selectbox(key="creator_render_subtitle_mode").set_value("使用字幕文件").run()
        self.assertTrue(self.button(app, "开始剪辑").disabled)
        self.provider.assert_not_called()

    def test_video_without_audio_requires_audio_or_disables_auto_subtitles(self):
        self.probe_mock.side_effect = lambda path: {"path": path, "duration": 7.32, "has_video": True, "has_audio": False}
        app = self.app()
        self.assertTrue(self.button(app, "开始剪辑").disabled)
        self.assertTrue(any("原视频没有音轨" in item.value for item in app.warning))
        app.selectbox(key="creator_render_subtitle_mode").set_value("不添加字幕").run()
        self.assertFalse(self.button(app, "开始剪辑").disabled)
        self.provider.assert_not_called()

    def test_pip_missing_file_or_out_of_range_time_cannot_submit(self):
        app = self.app()
        app.radio(key="creator_render_mode").set_value("自定义").run()
        app.checkbox(key="creator_render_pip_enabled").check().run()
        self.assertTrue(self.button(app, "开始剪辑").disabled)
        app.session_state["creator_render_pip_0_import"] = {"path": self.picture, "name": "示意图.png"}
        app.run()
        self.assertFalse(self.button(app, "开始剪辑").disabled)
        app.number_input(key="creator_render_pip_0_end").set_value(8.0).run()
        self.assertTrue(self.button(app, "开始剪辑").disabled)
        self.assertTrue(any("成片时长以内" in item.value for item in app.warning))
        self.provider.assert_not_called()

    def test_failed_render_preserves_completed_video_and_editing_inputs(self):
        previous = self.render_result(self.video, title="上一份成片", aspect="9:16")
        app = self.app(creator_render_result=previous)
        app.text_input(key="creator_render_title").set_value("新版本").run()
        with patch("app.services.creator.rendering.render_video", side_effect=RuntimeError("字幕识别暂时失败")):
            self.button(app, "开始剪辑").click().run()
            notices = self.collect(app)
        self.assertEqual(app.session_state["creator_render_result"]["id"], previous["id"])
        self.assertTrue(Path(previous["video_path"]).is_file())
        self.assertEqual(app.text_input(key="creator_render_title").value, "新版本")
        self.assertTrue(any("识别暂时失败" in message for message in notices))

    def test_finished_handoff_uses_matching_script_for_title_and_cover_step(self):
        Path(self.subtitle).write_text("1\n00:00:00,000 --> 00:00:02,000\n这条成片的开头。\n\n2\n00:00:02,000 --> 00:00:04,000\n这是对应的后半句。\n", "utf-8")
        result = self.render_result(self.video, title="待发布作品", aspect="16:9", subtitle_path=self.subtitle)
        app = self.app(creator_render_result=result, creator_publish_video_upload="old-file", creator_publish_title="旧标题")
        app.session_state["creator_release_text"] = "另一条视频的旧文案"
        app.button(key="creator_render_current_publish").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["creator_release_video"], result["video_path"])
        self.assertEqual(app.session_state["creator_release_title"], "待发布作品")
        self.assertEqual(app.session_state["creator_release_text"], "这条成片的开头。这是对应的后半句。")
        self.assertEqual(app.session_state["creator_selected_tab"], "标题和封面")
        self.assertTrue(app.session_state["creator_navigation_pending"])
        app.session_state["creator_render_result"] = dict(result, title="")
        app.run()
        app.button(key="creator_render_current_publish").click().run()
        self.assertEqual(app.session_state["creator_release_title"], "")

    def test_moved_source_prevents_new_render_without_discarding_previous_result(self):
        previous = self.render_result(self.video, title="成功作品")
        Path(self.video).unlink()
        app = self.app(creator_render_result=previous)
        self.assertTrue(self.button(app, "开始剪辑").disabled)
        self.assertEqual(app.session_state["creator_render_result"]["id"], previous["id"])
        self.assertTrue(any("已移动或删除" in item.value for item in app.warning))
        self.provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
