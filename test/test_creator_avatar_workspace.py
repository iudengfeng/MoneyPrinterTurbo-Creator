"""Verify audio provenance and navigation with real queue/store, fake avatar engine."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services.creator import jobs, narration, store


class AvatarWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.directory.name})
        self.environment.start()
        video = store.data_root() / "reference.mp4"
        video.write_bytes(b"test-video-placeholder")
        self.options = [{"id": "local:person", "name": "我的测试形象", "video_path": str(video), "source": "local", "available": True}]
        self.patchers = [
            patch("app.services.creator.avatar.list_options", side_effect=lambda: self.options),
            patch("app.services.creator.avatar.status", return_value={"available": True, "reason": ""}),
            patch("app.services.creator.avatar.generate", side_effect=AssertionError("Unexpected avatar request")),
            patch("app.services.creator.avatar.list_jobs", side_effect=lambda: store.list_records("avatar_jobs")),
        ]
        self.options_mock, self.status_mock, self.provider, self.history_mock = [item.start() for item in self.patchers]

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.environment.stop()
        self.directory.cleanup()

    def wav(self):
        path = store.data_root() / "narrations" / store.new_id() / "narration.wav"
        path.parent.mkdir(parents=True)
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\0\0" * 24000)
        return path

    def narration(self, text="完整配音内容", *, preview=False):
        return store.save_record("narrations", store.new_id(), {
            "text": text, "voice_name": "测试女声", "audio_path": str(self.wav()),
            "preview": preview, "state": "done", "duration": 1.0,
        })

    def app(self, current=None, draft=None):
        app = AppTest.from_string("from webui.creator_avatar_workspace import render\nrender()", default_timeout=30)
        if current:
            app.session_state["creator_narration_result"] = current
        if draft is not None:
            app.session_state["creator_voice_draft"] = draft
        app.run()
        self.assertFalse(app.exception)
        return app

    def button(self, app, label):
        return next(item for item in app.button if item.label == label)

    def original(self, app):
        return next(item for item in app.text_area if item.label == "这份配音的原文")

    def collect_pending(self, app):
        deadline = time.monotonic() + 10
        while "creator_avatar_pending" in app.session_state:
            row = jobs.get_job(app.session_state["creator_avatar_pending"]["id"])
            if row and row["state"] not in {"running", "queued"}:
                app.run()
                break
            self.assertLess(time.monotonic(), deadline, "Avatar task did not finish")
            time.sleep(0.02)
        self.assertFalse(app.exception)

    def generation(self, audio_path, model_id, *, script="", aspect="9:16", source_narration_id="", progress=None):
        # The fake provider creates an opaque file only: this checks UI/job
        # provenance and navigation, not the digital-human rendering model.
        ident = store.new_id()
        path = store.data_root() / "avatar_jobs" / ident / "video.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"test-generated-video-placeholder")
        if progress:
            progress("测试口播已完成", 100)
        return store.save_record("avatar_jobs", ident, {
            "state": "done", "video_path": str(path), "clean_video_path": str(path),
            "audio_path": audio_path, "model_id": model_id, "model_name": self.options[0]["name"],
            "script": script, "aspect": aspect, "source_narration_id": source_narration_id, "duration": 1.0,
        })

    def test_empty_page_still_allows_preparing_people_without_provider_calls(self):
        self.status_mock.return_value = {"available": False, "reason": "本机数字人服务尚未启动"}
        app = self.app()
        self.assertEqual(app.radio(key="creator_avatar_model").value, "local:person")
        self.assertTrue(self.button(app, "生成口播视频").disabled)
        self.assertFalse(self.button(app, "上传形象").disabled)
        self.assertTrue(any("尚未启动" in item.value for item in app.warning))
        self.assertTrue(any("照片生成" in item.value for item in app.caption))
        self.provider.assert_not_called()

    def test_current_complete_audio_requires_confirmation_and_reaches_job_unchanged(self):
        row = self.narration("完整文案、声音和语速由第二步的这份配音决定。")
        app = self.app(row, row["text"])
        self.assertEqual(self.original(app).value, row["text"])
        self.assertTrue(self.original(app).disabled)
        self.assertTrue(self.button(app, "生成口播视频").disabled)
        app.radio(key="creator_avatar_aspect").set_value("16:9").run()
        app.checkbox(key="creator_avatar_audio_confirmed").check().run()
        self.assertFalse(self.button(app, "生成口播视频").disabled)
        with patch("app.services.creator.avatar.generate", side_effect=self.generation) as generate:
            self.button(app, "生成口播视频").click().run()
            self.collect_pending(app)
            generate.assert_called_once()
            self.assertEqual(generate.call_args.args, (row["audio_path"], "local:person"))
            self.assertEqual(generate.call_args.kwargs["script"], row["text"])
            self.assertEqual(generate.call_args.kwargs["aspect"], "16:9")
            self.assertEqual(generate.call_args.kwargs["source_narration_id"], row["id"])
        result = app.session_state["creator_avatar_result"]
        self.assertEqual(result["audio_path"], row["audio_path"])
        self.assertTrue(any("口播视频已生成" in item.value for item in app.success))
        self.assertTrue(any(item.proto.label == "下载口播视频" for item in app.get("download_button")))
        self.assertEqual(jobs.list_jobs()[0]["state"], "done")
        self.assertEqual(narration.list_narrations()[0]["id"], row["id"])

    def test_changed_script_warns_and_resets_audio_confirmation(self):
        row = self.narration("旧稿的完整配音")
        app = self.app(row, row["text"])
        app.checkbox(key="creator_avatar_audio_confirmed").check().run()
        self.assertFalse(self.button(app, "生成口播视频").disabled)
        app.session_state["creator_voice_draft"] = "第二步尚未配音的新文案"
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(self.original(app).value, "旧稿的完整配音")
        self.assertFalse(app.checkbox(key="creator_avatar_audio_confirmed").value)
        self.assertTrue(self.button(app, "生成口播视频").disabled)
        self.assertTrue(any("当前编辑的文案不同" in item.value for item in app.warning))
        self.provider.assert_not_called()

    def test_history_never_preselects_and_excludes_short_previews(self):
        formal = self.narration("正式完整文案")
        preview = self.narration("短试听", preview=True)
        app = self.app()
        self.assertEqual(app.radio(key="creator_avatar_audio_source").value, "历史完整配音")
        choice = app.selectbox(key="creator_avatar_history_id")
        self.assertIsNone(choice.value)
        self.assertEqual(len(choice.options), 1)
        self.assertIn(formal["text"], choice.options[0])
        self.assertNotIn(preview["text"], choice.options[0])
        self.assertTrue(self.button(app, "生成口播视频").disabled)
        choice.set_value(formal["id"]).run()
        self.assertEqual(self.original(app).value, formal["text"])
        self.assertFalse(app.checkbox(key="creator_avatar_audio_confirmed").value)
        self.provider.assert_not_called()

    def test_current_preview_cannot_be_used_as_full_audio(self):
        app = self.app(self.narration("这只是前80字试听", preview=True))
        app.radio(key="creator_avatar_audio_source").set_value("本次完整配音").run()
        self.assertFalse(app.exception)
        self.assertTrue(self.button(app, "生成口播视频").disabled)
        self.assertTrue(any("还没有完整配音" in item.value for item in app.info))
        self.provider.assert_not_called()

    def test_switching_complete_audio_resets_confirmation_and_shows_selected_script(self):
        current = self.narration("本次正式配音原文")
        previous = self.narration("之前完成的另一篇文案")
        app = self.app(current)
        app.checkbox(key="creator_avatar_audio_confirmed").check().run()
        app.radio(key="creator_avatar_audio_source").set_value("历史完整配音").run()
        self.assertTrue(self.button(app, "生成口播视频").disabled)
        app.selectbox(key="creator_avatar_history_id").set_value(previous["id"]).run()
        self.assertFalse(app.checkbox(key="creator_avatar_audio_confirmed").value)
        self.assertEqual(self.original(app).value, previous["text"])
        self.provider.assert_not_called()

    def test_missing_reference_or_audio_prevents_generation(self):
        row = self.narration()
        self.options[0]["available"] = False
        self.options[0]["reason"] = "所选人物视频已移动"
        app = self.app(row)
        app.checkbox(key="creator_avatar_audio_confirmed").check().run()
        self.assertTrue(self.button(app, "生成口播视频").disabled)
        self.assertTrue(any("人物视频已移动" in item.value for item in app.warning))
        self.options[0]["available"] = True
        Path(row["audio_path"]).unlink()
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(self.button(app, "生成口播视频").disabled)
        self.assertTrue(any("配音文件已移动" in item.value for item in app.warning))
        self.provider.assert_not_called()

    def test_failed_job_preserves_previous_success_and_selected_audio(self):
        row = self.narration()
        previous = self.generation(row["audio_path"], "local:person", script="先前成功成品")
        app = self.app(row)
        app.session_state["creator_avatar_result"] = previous
        app.run()
        app.checkbox(key="creator_avatar_audio_confirmed").check().run()
        with patch("app.services.creator.avatar.generate", side_effect=RuntimeError("本机口播服务暂时不可用")):
            self.button(app, "生成口播视频").click().run()
            self.collect_pending(app)
        self.assertEqual(app.session_state["creator_avatar_result"]["id"], previous["id"])
        self.assertTrue(Path(previous["video_path"]).is_file())
        self.assertTrue(any("暂时不可用" in item.value for item in app.error))
        self.assertEqual(self.original(app).value, row["text"])

    def test_editing_handoff_keeps_synced_video_and_clears_stale_audio_and_subtitle(self):
        row = self.narration()
        result = self.generation(row["audio_path"], "local:person", aspect="16:9")
        app = self.app()
        app.session_state["creator_avatar_result"] = result
        app.session_state["creator_render_audio"] = "old-audio.wav"
        app.session_state["creator_srt_path"] = "old-subtitle.srt"
        app.session_state["creator_video_has_subtitles"] = True
        app.session_state["creator_render_audio_upload"] = "old-upload"
        app.run()
        app.button(key="creator_avatar_current_render").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["creator_video_path"], result["clean_video_path"])
        self.assertEqual(app.session_state["creator_render_audio"], "")
        self.assertNotIn("creator_render_audio_upload", app.session_state)
        self.assertEqual(app.session_state["creator_srt_path"], "")
        self.assertFalse(app.session_state["creator_video_has_subtitles"])
        self.assertEqual(app.session_state["creator_render_aspect"], "16:9")
        self.assertEqual(app.session_state["creator_selected_tab"], "模板剪辑")
        self.assertTrue(app.session_state["creator_navigation_pending"])

    def test_upload_dialog_allows_local_preparation_when_engine_is_off(self):
        self.status_mock.return_value = {"available": False, "reason": "本机数字人服务尚未启动"}
        app = self.app()
        self.button(app, "上传形象").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.text_input(key="creator_avatar_upload_name"))
        self.assertTrue(self.button(app, "保存形象"))
        self.assertTrue(any("视频最长 2 分钟" in item.value for item in app.caption))
        self.provider.assert_not_called()

    def test_explicit_recovery_job_does_not_replace_existing_video(self):
        self.status_mock.return_value = {"available": False, "recovery_needed": True, "reason": "上次口播任务没有收尾，请恢复"}
        narration_row = self.narration()
        previous = self.generation(narration_row["audio_path"], "local:person", script="已经完成的成品")
        app = self.app(narration_row)
        app.session_state["creator_avatar_result"] = previous
        app.run()
        self.assertFalse(self.button(app, "恢复数字人引擎").disabled)
        self.assertTrue(self.button(app, "生成口播视频").disabled)
        with patch("app.services.creator.avatar.recover_engine", return_value={"state": "done", "message": "数字人引擎已恢复"}, create=True) as recovery:
            self.button(app, "恢复数字人引擎").click().run()
            self.collect_pending(app)
            recovery.assert_called_once()
            self.assertIn("progress", recovery.call_args.kwargs)
        self.assertEqual(app.session_state["creator_avatar_result"]["id"], previous["id"])
        self.assertTrue(any("数字人引擎已恢复" in item.value for item in app.success))
        self.assertEqual(jobs.list_jobs()[0]["label"], "恢复数字人引擎")
        self.provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
