"""Reference publishing column keeps platform uploads behind explicit review."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services.creator import jobs, store
from webui import creator_reference_release as release


class ReferenceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.folder.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.video = str(Path(self.folder.name) / "finished.mp4")
        Path(self.video).write_bytes(b"isolated-ui-media-fixture")
        self.accounts = [
            {"id": "douyin-one", "name": "我的抖音", "platform": "douyin", "platform_name": "抖音", "login_open": False},
            {"id": "xhs-one", "name": "我的小红书", "platform": "xiaohongshu", "platform_name": "小红书", "login_open": False},
        ]
        patches = [
            patch("app.services.creator.publishing.list_accounts", side_effect=lambda: self.accounts),
            patch("app.services.creator.publishing.list_tasks", side_effect=lambda: store.list_records("publisher_tasks")),
            patch("app.services.creator.publishing.prepare_publish", side_effect=self.prepare),
            patch("app.services.creator.publishing.enqueue_publish", side_effect=AssertionError("No upload without explicit confirmation")),
            patch("webui.creator_reference_release._config_snapshot", return_value={"llm_provider": "test"}),
            patch("webui.creator_publish_workspace._config_snapshot", return_value={"llm_provider": "confirmed-test"}),
        ]
        self.mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.prepare_mock = self.mocks[2]
        self.enqueue_mock = self.mocks[3]

    def prepare(self, video_path, title, description, accounts, cover_path=None):
        by_id = {row["id"]: row for row in self.accounts}
        rows = []
        for ident in accounts:
            account = by_id[ident]
            rows.append(store.save_record("publisher_tasks", store.new_id(), {
                "video_path": video_path, "cover_path": cover_path, "title": title, "description": description,
                "account_id": ident, "account_name": account["name"], "platform": account["platform"],
                "platform_name": account["platform_name"], "status": "prepared", "submit_started": False,
            }))
        return rows

    def app(self, current=True, **state):
        app = AppTest.from_string('''import streamlit as st
from webui.creator_reference_release import render
class Context:
    project = {}
    @property
    def busy(self):
        return st.session_state.get("test_busy", False)
    def stage(self, name):
        return {"text": st.session_state.get("test_script", "这是本次视频的真实口播文案，用于生成准确的标题和标签。")}
    def current_video(self, rendered=True):
        st.session_state["test_requested_rendered"] = rendered
        return st.session_state.get("test_video", "")
    def current_cover(self):
        return st.session_state.get("test_cover", "")
    def queue(self, kind, title, operation, *args, **kwargs):
        st.session_state["test_queued"] = {"kind": kind, "title": title, "args": args, "kwargs": kwargs}
    def submit_stage(self, name, changes):
        st.session_state["test_submission"] = {"stage": name, "changes": changes}
    def _ensure_project(self, changes=None):
        st.session_state["test_settings_saved"] = changes
    def open_tool(self, name):
        st.session_state["test_open_tool"] = name
render(Context())
''', default_timeout=30)
        if current:
            app.session_state["test_video"] = self.video
            app.session_state["ref_publish_title"] = "准确的本次标题"
            app.session_state["ref_publish_description"] = "本次真实正文"
            app.session_state["ref_publish_tags"] = "#数字人，短视频"
        for key, value in state.items():
            app.session_state[key] = value
        app.run()
        self.assertFalse(app.exception)
        return app

    def select_accounts(self, app, both=False):
        app.checkbox(key="ref_publish_douyin").check().run()
        app.selectbox(key="ref_publish_account_douyin").select("douyin-one").run()
        if both:
            app.checkbox(key="ref_publish_xiaohongshu").check().run()
            app.selectbox(key="ref_publish_account_xiaohongshu").select("xhs-one").run()
        return app

    def test_cover_settings_save_does_not_start_a_pipeline_or_paid_call(self):
        app = self.app(ref_script_text="本次需要保存封面设置的真实口播正文。")
        app.button(key="ref_cover_settings").click().run()
        app.text_input(key="ref_cover_title").set_value("单独封面标题").run()
        app.button(key="ref_cover_settings_save").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["test_settings_saved"]["cover_title"], "单独封面标题")
        self.assertNotIn("test_submission", app.session_state)
        self.assertNotIn("test_queued", app.session_state)

    def test_active_generation_disables_new_copy_cover_and_publish_requests(self):
        app = self.app(test_busy=True)
        for key in ("ref_cover_generate", "ref_cover_settings", "ref_publish_generate_copy", "ref_publish_open"):
            self.assertTrue(app.button(key=key).disabled)
        self.prepare_mock.assert_not_called()

    def test_empty_column_has_four_truthful_platforms_without_preparing_or_uploading(self):
        app = self.app(current=False)
        self.assertEqual([row.label for row in app.checkbox], ["抖音", "快手", "视频号", "小红书"])
        self.assertTrue(app.checkbox(key="ref_publish_kuaishou").disabled)
        self.assertTrue(app.checkbox(key="ref_publish_wechat").disabled)
        self.assertTrue(app.selectbox(key="ref_publish_account_kuaishou").disabled)
        self.assertTrue(app.button(key="ref_publish_open").disabled)
        self.assertTrue(app.button(key="ref_cover_generate").disabled)
        self.prepare_mock.assert_not_called()
        self.enqueue_mock.assert_not_called()

    def test_metadata_queues_actual_script_with_the_current_configuration(self):
        app = self.app(ref_script_text="这一份正在编辑的真实文案有足够字数，可以生成当前视频的标题和标签。")
        app.button(key="ref_publish_generate_copy").click().run()
        request = app.session_state["test_queued"]
        self.assertEqual(request["kind"], "publish_copy")
        self.assertEqual(request["args"], ("这一份正在编辑的真实文案有足够字数，可以生成当前视频的标题和标签。",))
        self.assertEqual(request["kwargs"]["app_config"], {"llm_provider": "test"})
        self.prepare_mock.assert_not_called()
        self.enqueue_mock.assert_not_called()

    def test_metadata_wrapper_preserves_real_service_output(self):
        result = {"titles": ["第一标题", "第二标题", "第三标题"], "hashtags": ["数字人", "口播"],
                  "description": "真实说明", "cover_title": "封面文字"}
        with patch.object(release.release_assets, "generate_metadata", return_value=result) as generate:
            actual = release._generate_copy("测试实际文案", app_config={"provider": "test"})
        generate.assert_called_once_with("测试实际文案", count=3, progress=None, app_config={"provider": "test"})
        self.assertEqual(actual["title"], "第一标题")
        self.assertEqual(actual["tags"], "#数字人 #口播")
        self.assertEqual(actual["description"], "真实说明")

    def test_cover_queues_selected_finished_video_and_saved_cover_settings(self):
        app = self.app(ref_cover_style="bold", ref_cover_aspect="1:1", ref_cover_frame_time=1.5)
        app.button(key="ref_cover_generate").click().run()
        request = app.session_state["test_queued"]
        self.assertEqual(request["kind"], "cover")
        self.assertEqual(request["args"], (self.video, "准确的本次标题"))
        self.assertEqual(request["kwargs"], {"style": "bold", "aspect": "1:1", "frame_time": 1.5})
        self.assertTrue(app.session_state["test_requested_rendered"])
        self.enqueue_mock.assert_not_called()

    def test_publish_click_prepares_exact_snapshot_and_only_opens_review(self):
        app = self.select_accounts(self.app(), both=True)
        app.button(key="ref_publish_open").click().run()
        self.assertFalse(app.exception)
        self.prepare_mock.assert_called_once_with(video_path=self.video, cover_path=None,
                                                  title="准确的本次标题", description="本次真实正文\n#数字人 #短视频",
                                                  accounts=["douyin-one", "xhs-one"])
        tasks = store.list_records("publisher_tasks")
        self.assertEqual(len(tasks), 2)
        self.assertTrue(any("确认发布到抖音（我的抖音）" == row.label for row in app.button))
        self.assertTrue(any("确认发布到小红书（我的小红书）" == row.label for row in app.button))
        self.assertTrue(any("标题：准确的本次标题" == row.value for row in app.text))
        self.assertTrue(any("本次真实正文\n#数字人 #短视频" == row.value for row in app.text))
        self.enqueue_mock.assert_not_called()

    def test_exact_account_confirmation_reuses_existing_atomic_enqueue(self):
        app = self.select_accounts(self.app())
        app.button(key="ref_publish_open").click().run()
        ident = app.session_state["ref_publish_preview_ids"][0]
        def enqueue(task_id, app_config=None):
            jobs.list_jobs()
            return store.save_record("jobs", "confirmed-test-job", {
                "state": "queued", "label": "本地模拟确认", "message": "排队中", "progress": 0})["id"]
        self.enqueue_mock.side_effect = enqueue
        app.button(key="ref_publish_confirm_" + ident).click().run()
        self.assertFalse(app.exception)
        self.enqueue_mock.assert_called_once_with(ident, app_config={"llm_provider": "confirmed-test"})
        self.assertEqual(app.session_state["creator_publish_pending"]["confirmed-test-job"]["task_id"], ident)

    def test_existing_submit_started_guard_excludes_confirmation(self):
        app = self.select_accounts(self.app())
        prepare = self.prepare_mock.side_effect
        def uncertain(**kwargs):
            rows = prepare(**kwargs)
            for row in rows:
                store.update_record("publisher_tasks", row["id"], {"status": "submission_unknown", "submit_started": True})
            return rows
        self.prepare_mock.side_effect = uncertain
        app.button(key="ref_publish_open").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(any(row.key.startswith("ref_publish_confirm_") for row in app.button))
        self.assertTrue(any("系统不会重发" in row.value for row in app.warning))
        self.enqueue_mock.assert_not_called()

    def test_login_window_blocks_review_confirmation(self):
        self.accounts[0]["login_open"] = True
        app = self.select_accounts(self.app())
        app.button(key="ref_publish_open").click().run()
        ident = app.session_state["ref_publish_preview_ids"][0]
        self.assertTrue(app.button(key="ref_publish_confirm_" + ident).disabled)
        self.enqueue_mock.assert_not_called()

    def test_scheduling_exports_a_manual_list_without_creating_a_publisher_task(self):
        app = self.app()
        app.button(key="ref_publish_schedule").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("暂未接入自动定时发布" in row.value for row in app.info))
        self.assertTrue(any(row.proto.label == "下载发布清单" for row in app.get("download_button")))
        self.prepare_mock.assert_not_called()
        self.enqueue_mock.assert_not_called()

    def test_account_management_opens_existing_tool_without_login_or_upload(self):
        app = self.app()
        app.button(key="ref_publish_accounts").click().run()
        self.assertEqual(app.session_state["test_open_tool"], "发布中心")
        self.prepare_mock.assert_not_called()
        self.enqueue_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
