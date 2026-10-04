"""Check narration UI handoffs and results using real jobs but fake providers."""

import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services.creator import jobs, narration, store, topics


class VoiceWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.directory.name})
        self.environment.start()
        self.options = [
            {"id": "edge:zh-CN-XiaoxiaoNeural", "name": "测试在线女声", "provider": "edge", "description": "在线标准音色", "supports_emotion": False},
            {"id": "duix:12", "name": "测试本机音色", "provider": "duix", "description": "本机已有音色", "supports_emotion": False},
        ]
        self.option_patch = patch("app.services.creator.narration.list_options", return_value=self.options)
        self.option_patch.start()
        self.provider_guard = patch("app.services.creator.narration.generate", side_effect=AssertionError("Unexpected provider request"))
        self.provider = self.provider_guard.start()

    def tearDown(self):
        self.provider_guard.stop()
        self.option_patch.stop()
        self.environment.stop()
        self.directory.cleanup()

    def app(self, *, integrated=False):
        module = "creator_workspace" if integrated else "creator_voice_workspace"
        app = AppTest.from_string(f"from webui.{module} import render\nrender()", default_timeout=30).run()
        self.assertFalse(app.exception)
        return app

    def button(self, app, label):
        return next(item for item in app.button if item.label == label)

    def collect_pending(self, app):
        deadline = time.monotonic() + 10
        while "creator_narration_pending" in app.session_state:
            pending = app.session_state["creator_narration_pending"]
            job = jobs.get_job(pending["id"])
            if job and job["state"] not in ("queued", "running"):
                app.run()
                break
            self.assertLess(time.monotonic(), deadline, "Narration task did not finish")
            time.sleep(0.02)
        self.assertFalse(app.exception)

    def wav(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\x00\x00" * 24000)
        return path

    def generation(self, text, voice_id, speed=1.0, emotion="自然", preview=False, progress=None):
        # A local stand-in for a provider preserves the actual jobs/store/UI path.
        ident = store.new_id()
        path = self.wav(store.data_root() / "narrations" / ident / "narration.wav")
        option = next(row for row in self.options if row["id"] == voice_id)
        if progress:
            progress("测试音频已生成", 100)
        return store.save_record("narrations", ident, {
            "text": text, "voice_id": voice_id, "voice_name": option["name"], "provider": option["provider"],
            "speed": speed, "emotion": emotion, "preview": preview, "state": "done", "audio_path": str(path), "duration": 1.0,
        })

    def downloads(self, app):
        return [item.proto.label for item in app.get("download_button")]

    def test_voice_options_and_natural_delivery_render_without_provider_calls(self):
        app = self.app()
        self.assertEqual(app.radio(key="creator_narration_voice").options, [option["name"] for option in self.options])
        self.assertEqual(app.radio(key="creator_narration_voice").value, self.options[0]["id"])
        self.assertEqual(app.slider(key="creator_narration_speed").value, 1.0)
        self.assertTrue(self.button(app, "开始配音").disabled)
        self.assertTrue(any("自然语气" in item.value for item in app.caption))
        self.provider.assert_not_called()

    def test_selected_voice_speed_and_complete_text_reach_real_job_and_result(self):
        app = self.app()
        text = "第一步确认的文案应该完整进入配音。声音、语速以及最终可下载的文件应与这次选择一致。"
        app.radio(key="creator_narration_voice").set_value("duix:12").run()
        app.slider(key="creator_narration_speed").set_value(0.8).run()
        app.text_area(key="creator_voice_text").set_value(text).run()
        with patch("app.services.creator.narration.generate", side_effect=self.generation) as generate:
            self.button(app, "开始配音").click().run()
            self.collect_pending(app)
            generate.assert_called_once()
            self.assertEqual(generate.call_args.args, (text, "duix:12"))
            self.assertEqual(generate.call_args.kwargs["speed"], 0.8)
            self.assertEqual(generate.call_args.kwargs["emotion"], "自然")
            self.assertFalse(generate.call_args.kwargs["preview"])
        result = app.session_state["creator_narration_result"]
        self.assertEqual(result["text"], text)
        self.assertEqual(result["voice_id"], "duix:12")
        self.assertEqual(result["speed"], 0.8)
        self.assertFalse(result["preview"])
        self.assertTrue(Path(result["audio_path"]).is_file())
        self.assertEqual(jobs.list_jobs()[0]["state"], "done")
        self.assertTrue(app.get("audio"))
        self.assertIn("下载配音", self.downloads(app))
        self.assertEqual(narration.list_narrations()[0]["id"], result["id"])
        self.assertTrue(any(item.label == "我的配音 · 历史记录" for item in app.expander))
        app.button(key="creator_narration_current_render").click().run()
        self.assertEqual(app.session_state["creator_selected_tab"], "模板剪辑")
        self.assertEqual(app.session_state["creator_render_audio"], result["audio_path"])
        self.assertEqual(app.session_state["creator_srt_path"], "")
        self.assertFalse(app.session_state["creator_video_has_subtitles"])

    def test_preview_limits_text_to_80_characters_and_shows_playback(self):
        app = self.app()
        text = "这是完整稿件中的一句测试文字。" * 12
        app.text_area(key="creator_voice_text").set_value(text).run()
        with patch("app.services.creator.narration.generate", side_effect=self.generation) as generate:
            self.button(app, "试听前 80 字").click().run()
            self.collect_pending(app)
            self.assertEqual(generate.call_args.args, (text[:80], self.options[0]["id"]))
            self.assertTrue(generate.call_args.kwargs["preview"])
        preview = app.session_state["creator_narration_preview_result"]
        self.assertTrue(preview["preview"])
        self.assertEqual(preview["text"], text[:80])
        self.assertNotIn("creator_narration_result", app.session_state)
        self.assertEqual(app.text_area(key="creator_voice_text").value, text)
        self.assertTrue(any("试听已生成" in item.value for item in app.success))
        self.assertTrue(app.get("audio"))
        self.assertTrue(any("下载" in label and "试听" in label for label in self.downloads(app)))
        self.assertFalse(any(item.label == "用于模板剪辑 →" for item in app.button))
        self.assertEqual(narration.list_narrations(), [])

    def test_failed_new_generation_keeps_previous_audio_and_current_text(self):
        previous = self.generation("这是已经完成的配音。", self.options[0]["id"])
        app = self.app()
        app.session_state["creator_narration_result"] = previous
        app.run()
        text = "新的配音请求失败时，我仍应可以试听和下载上一份成功的结果。"
        app.text_area(key="creator_voice_text").set_value(text).run()
        with patch("app.services.creator.narration.generate", side_effect=RuntimeError("测试配音服务暂时不可用")):
            self.button(app, "开始配音").click().run()
            self.collect_pending(app)
        self.assertEqual(app.session_state["creator_narration_result"]["id"], previous["id"])
        self.assertEqual(app.text_area(key="creator_voice_text").value, text)
        self.assertTrue(Path(previous["audio_path"]).is_file())
        self.assertEqual(narration.list_narrations()[0]["id"], previous["id"])
        self.assertEqual(jobs.list_jobs()[0]["state"], "failed")
        self.assertTrue(any("测试配音服务暂时不可用" in item.value for item in app.error))
        self.assertTrue(app.get("audio"))
        self.assertIn("下载配音", self.downloads(app))

    def test_archived_sample_is_pending_and_upload_dialog_explains_creation(self):
        sample_path = self.wav(store.data_root() / "voice_samples" / "test" / "sample.wav")
        store.save_record("voice_samples", store.new_id(), {
            "name": "尚未创建的我的音色", "sample_path": str(sample_path),
            "provider": "pending_duix", "state": "sample_saved",
        })
        app = self.app()
        self.assertTrue(any(item.label == "待创建的本机音色 · 1" for item in app.expander))
        self.assertNotIn("尚未创建的我的音色", app.radio(key="creator_narration_voice").options)
        self.button(app, "上传音色").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="creator_narration_sample_mode").value, "在 Duix 中创建本机音色")
        self.assertTrue(any("样音档案" in item.value and "创建音色" in item.value for item in app.info))
        self.assertTrue(self.button(app, "保存音色"))
        self.assertEqual(store.list_records("voices"), [])
        self.provider.assert_not_called()

    def test_first_step_confirmation_navigates_to_voice_with_exact_script(self):
        app = self.app(integrated=True)
        self.assertEqual(app.radio(key="creator_selected_tab").value, "选题和文案")
        self.button(app, "直接写文案").click().run()
        text = "这一段是第一步确认的完整文案。下一步配音页面应该接收到完全相同的文字，稿件也应保存到我的文案。"
        app.text_input(key="creator_script_title").set_value("测试两步衔接")
        app.text_area(key="creator_script_editor").set_value(text)
        self.button(app, "确认文案，下一步配音 →").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key="creator_selected_tab").value, "配音")
        self.assertEqual(app.text_area(key="creator_voice_text").value, text)
        self.assertEqual(app.session_state["creator_duix_script"], text)
        self.assertEqual(topics.list_drafts()[0]["text"], text)
        self.provider.assert_not_called()

    def test_navigation_away_and_back_retains_edited_voice_script(self):
        app = self.app(integrated=True)
        app.radio(key="creator_selected_tab").set_value("配音").run()
        original = "这一篇配音稿即使切换到选题页，再返回配音页，也必须完整保留。"
        edited = original + "这句是在配音页新增的修改，尚未生成音频。"
        app.text_area(key="creator_voice_text").set_value(edited).run()
        self.assertEqual(app.session_state["creator_voice_draft"], edited)
        app.radio(key="creator_selected_tab").set_value("选题和文案").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["creator_voice_draft"], edited)
        app.radio(key="creator_selected_tab").set_value("配音").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area(key="creator_voice_text").value, edited)
        self.assertEqual(app.session_state["creator_voice_draft"], edited)
        self.provider.assert_not_called()

    def test_complete_narration_handoff_keeps_audio_and_requires_avatar_confirmation(self):
        text = "这一条完整配音应原样交给数字人，不应重复合成或自动跳过试听确认。"
        formal = self.generation(text, self.options[0]["id"])
        app = self.app()
        app.session_state["creator_narration_result"] = formal
        app.run()
        self.button(app, "确认配音，下一步数字人 →").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["creator_selected_tab"], "数字人")
        self.assertEqual(app.session_state["creator_avatar_audio_source"], "本次完整配音")
        self.assertEqual(app.session_state["creator_narration_result"]["audio_path"], formal["audio_path"])
        self.assertEqual(app.session_state["creator_narration_result"]["text"], text)
        self.assertNotIn("creator_avatar_audio_confirmed", app.session_state)
        self.provider.assert_not_called()

    def test_preview_keeps_complete_narration_and_has_no_editing_handoff(self):
        complete_text = "完整的配音包含所有内容，试听只用于比较声音，不应替换正式成品。" * 5
        formal = self.generation(complete_text, self.options[0]["id"])
        app = self.app(integrated=True)
        app.radio(key="creator_selected_tab").set_value("配音").run()
        app.session_state["creator_narration_result"] = formal
        app.run()
        app.text_area(key="creator_voice_text").set_value(complete_text).run()
        with patch("app.services.creator.narration.generate", side_effect=self.generation) as generate:
            self.button(app, "试听前 80 字").click().run()
            self.collect_pending(app)
            self.assertTrue(generate.call_args.kwargs["preview"])
        preview = app.session_state["creator_narration_preview_result"]
        self.assertNotEqual(preview["id"], formal["id"])
        self.assertTrue(preview["preview"])
        self.assertEqual(app.session_state["creator_narration_result"]["id"], formal["id"])
        self.assertEqual(app.session_state["creator_narration_result"]["text"], complete_text)
        self.assertEqual([row["id"] for row in narration.list_narrations()], [formal["id"]])
        button_keys = {button.key for button in app.button}
        self.assertIn("creator_narration_current_render", button_keys)
        self.assertIn("creator_narration_history_" + formal["id"] + "_render", button_keys)
        self.assertNotIn("creator_narration_preview_render", button_keys)
        self.assertNotIn("creator_narration_history_" + preview["id"] + "_render", button_keys)
        # The generic task centre must not provide a separate path to send the
        # truncated preview into editing, even when the formal audio is visible.
        preview_jobs = [row for row in jobs.list_jobs() if row.get("result", {}).get("id") == preview["id"]]
        self.assertEqual(len(preview_jobs), 1)
        self.assertNotIn("creator_job_audio_use_" + preview_jobs[0]["id"], button_keys)
        app.button(key="creator_narration_current_render").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["creator_render_audio"], formal["audio_path"])


if __name__ == "__main__":
    unittest.main()
