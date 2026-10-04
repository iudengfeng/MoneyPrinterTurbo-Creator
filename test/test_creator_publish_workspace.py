"""Verify local publishing review, account sessions and immutable handoffs."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from streamlit.testing.v1 import AppTest

from app.services.creator import jobs, store


class PublishWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.directory.name})
        self.environment.start()
        self.video = self.file("finished.mp4")
        self.other = self.file("another.mp4")
        self.cover = self.file("cover.png")
        self.accounts = []
        self.materials = []
        self.patchers = [
            patch("app.services.creator.publishing.inspect_media", side_effect=self.inspect, create=True),
            patch("app.services.creator.publishing.list_accounts", side_effect=lambda: self.accounts),
            patch("app.services.creator.publishing.list_tasks", side_effect=lambda: store.list_records("publisher_tasks")),
            patch("app.services.creator.publishing.get_task", side_effect=lambda ident: store.get_record("publisher_tasks", ident), create=True),
            patch("app.services.creator.release_assets.list_materials", side_effect=lambda: self.materials),
            patch("app.services.creator.publishing.prepare_publish", side_effect=self.prepare),
            patch("app.services.creator.publishing.export_materials", side_effect=self.export, create=True),
            patch("app.services.creator.publishing.enqueue_publish", side_effect=AssertionError("Publishing requires explicit user confirmation"), create=True),
            patch("app.services.creator.publishing.open_login", side_effect=AssertionError("Unexpected login window")),
            patch("app.services.creator.publishing.close_login", side_effect=AssertionError("Unexpected login close")),
            patch("app.services.creator.publishing.check_login", side_effect=AssertionError("Unexpected login check"), create=True),
            patch("webui.creator_publish_workspace._config_snapshot", return_value={"llm_provider": "test"}),
        ]
        (self.inspect_mock, self.accounts_mock, self.tasks_mock, self.task_mock, self.materials_mock, self.prepare_mock,
         self.export_mock, self.enqueue_mock, self.open_mock, self.close_mock, self.check_mock, self.config_mock) = [item.start() for item in self.patchers]

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
            path.write_bytes(b"opaque-media-for-ui-tests")
        return str(path)

    def inspect(self, video, cover=None):
        return {"video_path": video, "cover_path": cover, "duration": 7.32, "width": 1280, "height": 720, "has_audio": True,
                "video_bytes": Path(video).stat().st_size}

    def account(self, ident="douyin-one", platform="douyin", **extra):
        row = {"id": ident, "platform": platform, "platform_name": "抖音" if platform == "douyin" else "小红书", "name": ident,
               "status": "not_logged_in", "login_open": False, **extra}
        self.accounts.append(row)
        return row

    def prepare(self, video_path, title, description, accounts, cover_path=None, per_account_overrides=None):
        results = []
        by_id = {row["id"]: row for row in self.accounts}
        for ident in accounts:
            account = by_id[ident]
            override = (per_account_overrides or {}).get(ident, {})
            task_id = store.new_id()
            row = {"video_path": video_path, "cover_path": cover_path, "title": override.get("title", title),
                   "description": override.get("description", description), "account_id": ident, "account_name": account["name"],
                   "platform": account["platform"], "platform_name": account["platform_name"], "status": "prepared", "submit_started": False,
                   "message": "待预览确认发布"}
            results.append(store.save_record("publisher_tasks", task_id, row))
        return results

    def export(self, video_path, title, description="", cover_path=None, per_account_overrides=None, progress=None):
        path = store.data_root() / "local-package.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.write(video_path, "video.mp4")
            if cover_path:
                archive.write(cover_path, "cover.png")
            archive.writestr("copy.txt", title + "\n" + description)
        return {"id": "package", "state": "done", "zip_path": str(path), "title": title}

    def app(self, current=True, **state):
        app = AppTest.from_string("""import streamlit as st
from webui.creator_publish_workspace import render, load_materials
page = st.sidebar.radio('工作步骤', ['发布中心', '标题和封面'], key='creator_selected_tab')
if st.session_state.pop('load_this_material', False):
    load_materials(st.session_state['test_material_row'])
if page == '发布中心':
    render()
else:
    st.write('标题和封面测试页')
""", default_timeout=30)
        if current:
            app.session_state["creator_publish_video"] = self.video
            app.session_state["creator_publish_title"] = "本次精确标题"
            app.session_state["creator_publish_description"] = "本次正文\n#创作记录"
            app.session_state["creator_publish_cover"] = self.cover
        for key, value in state.items():
            app.session_state[key] = value
        app.run()
        self.assertFalse(app.exception)
        return app

    def collect(self, app):
        deadline = time.monotonic() + 10
        while "creator_publish_pending" in app.session_state and app.session_state["creator_publish_pending"]:
            pending = app.session_state["creator_publish_pending"]
            if all((jobs.get_job(ident) or {}).get("state") not in {"queued", "running"} for ident in pending):
                app.run()
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        notices = {"error": [row.value for row in app.error], "success": [row.value for row in app.success]}
        app.run()
        self.assertFalse(app.exception)
        self.assertFalse(app.session_state["creator_publish_pending"] if "creator_publish_pending" in app.session_state else {})
        ids = [row.id for row in app.text_input]
        self.assertEqual(len(ids), len(set(ids)), "The settled page must have unique widget ids")
        return notices

    def test_without_accounts_shows_real_material_and_guides_but_never_prepares(self):
        app = self.app()
        self.assertEqual(app.text_input(key="creator_publish_title").value, "本次精确标题")
        self.assertTrue(any("请先在上方添加发布账号" in row.value for row in app.info))
        self.assertTrue(app.button(key="creator_publish_prepare").disabled)
        self.assertFalse(app.button(key="creator_publish_export").disabled)
        self.assertEqual(app.selectbox(key="creator_publish_account_platform").value, "douyin")
        self.prepare_mock.assert_not_called()
        self.enqueue_mock.assert_not_called()
        self.open_mock.assert_not_called()

    def test_missing_or_invalid_video_cannot_prepare_or_export(self):
        self.account()
        self.inspect_mock.side_effect = ValueError("视频缺少有效画面，可能下载不完整。")
        app = self.app()
        self.assertTrue(app.button(key="creator_publish_prepare").disabled)
        self.assertTrue(app.button(key="creator_publish_export").disabled)
        self.assertTrue(any("下载不完整" in row.value for row in app.warning))
        self.prepare_mock.assert_not_called()

    def test_shared_router_handoff_replaces_previous_uploads_and_overrides(self):
        app = self.app(creator_publish_source="历史发布素材", creator_publish_override_title_old_buffer="旧账号标题",
                       creator_publish_package={"zip_path": "older.zip"}, creator_publish_video_import={"path": self.other, "name": "old"})
        app.session_state["test_material_row"] = {"video_path": self.other, "title": "新作品标题", "publish_description": "", "cover_path": ""}
        app.session_state["load_this_material"] = True
        app.run()
        self.assertEqual(app.radio(key="creator_publish_source").value, "本次素材")
        self.assertEqual(app.text_input(key="creator_publish_title").value, "新作品标题")
        self.assertEqual(app.text_area(key="creator_publish_description").value, "")
        self.assertEqual(app.text_input(key="creator_publish_cover").value, "")
        self.assertFalse(app.checkbox(key="creator_publish_cover_enabled").value)
        self.assertNotIn("creator_publish_video_import", app.session_state)
        self.assertNotIn("creator_publish_package", app.session_state)
        self.assertNotIn("creator_publish_override_title_old_buffer", app.session_state)

    def test_drafts_and_explicit_empty_values_survive_navigation(self):
        app = self.app()
        app.text_input(key="creator_publish_title").set_value("").run()
        app.text_area(key="creator_publish_description").set_value("").run()
        app.text_input(key="creator_publish_cover").set_value("").run()
        app.checkbox(key="creator_publish_cover_enabled").uncheck().run()
        app.radio(key="creator_selected_tab").set_value("标题和封面").run()
        for key in ("creator_publish_title", "creator_publish_description", "creator_publish_cover", "creator_publish_cover_enabled"):
            del app.session_state[key]
        app.radio(key="creator_selected_tab").set_value("发布中心").run()
        self.assertEqual(app.text_input(key="creator_publish_title").value, "")
        self.assertEqual(app.text_area(key="creator_publish_description").value, "")
        self.assertEqual(app.text_input(key="creator_publish_cover").value, "")
        self.assertFalse(app.checkbox(key="creator_publish_cover_enabled").value)

    def test_changing_actual_video_clears_unrelated_metadata_cover_and_package(self):
        app = self.app(creator_publish_package={"zip_path": "previous.zip"})
        app.text_input(key="creator_publish_video").set_value(self.other).run()
        self.assertEqual(app.text_input(key="creator_publish_title").value, "")
        self.assertEqual(app.text_area(key="creator_publish_description").value, "")
        self.assertEqual(app.text_input(key="creator_publish_cover").value, "")
        self.assertNotIn("creator_publish_package", app.session_state)
        self.assertTrue(app.button(key="creator_publish_export").disabled)

    def test_history_selection_restores_its_actual_title_cover_and_description(self):
        self.materials = [{"id": "stored", "video_path": self.other, "title": "历史素材标题", "publish_description": "历史正文\n#历史话题", "cover_path": self.cover}]
        app = self.app()
        app.radio(key="creator_publish_source").set_value("历史发布素材").run()
        app.selectbox(key="creator_publish_history_id").select("stored").run()
        self.assertEqual(app.text_input(key="creator_publish_title").value, "历史素材标题")
        self.assertEqual(app.text_area(key="creator_publish_description").value, "历史正文\n#历史话题")
        self.assertTrue(app.checkbox(key="creator_publish_cover_enabled").value)
        self.assertFalse(app.button(key="creator_publish_export").disabled)
        app.radio(key="creator_selected_tab").set_value("标题和封面").run()
        app.radio(key="creator_selected_tab").set_value("发布中心").run()
        self.assertEqual(app.selectbox(key="creator_publish_history_id").value, "stored")

    def test_two_saved_packages_for_same_video_keep_their_own_copy_and_cover(self):
        self.materials = [{"id": "first", "video_path": self.video, "title": "同片第一版标题", "publish_description": "第一版正文", "cover_path": self.cover},
                          {"id": "second", "video_path": self.video, "title": "同片第二版标题", "publish_description": "", "cover_path": ""}]
        app = self.app()
        app.radio(key="creator_publish_source").set_value("历史发布素材").run()
        app.selectbox(key="creator_publish_history_id").select("first").run()
        self.assertEqual(app.text_area(key="creator_publish_description").value, "第一版正文")
        app.selectbox(key="creator_publish_history_id").select("second").run()
        self.assertEqual(app.text_input(key="creator_publish_title").value, "同片第二版标题")
        self.assertEqual(app.text_area(key="creator_publish_description").value, "")
        self.assertEqual(app.text_input(key="creator_publish_cover").value, "")
        self.assertFalse(app.checkbox(key="creator_publish_cover_enabled").value)

    def test_prepare_saves_exact_per_account_copy_and_does_not_execute(self):
        first = self.account()
        second = self.account("xhs-one", "xiaohongshu")
        app = self.app()
        app.multiselect(key="creator_publish_selected_accounts").set_value([first["id"], second["id"]]).run()
        app.checkbox(key="creator_publish_override_enabled").check().run()
        app.text_input(key="creator_publish_override_title_" + first["id"]).set_value("抖音专属标题").run()
        app.text_area(key="creator_publish_override_description_" + second["id"]).set_value("").run()
        app.button(key="creator_publish_prepare").click().run()
        self.collect(app)
        self.prepare_mock.assert_called_once()
        request = self.prepare_mock.call_args.kwargs
        self.assertEqual(request["accounts"], [first["id"], second["id"]])
        self.assertEqual(request["video_path"], self.video)
        self.assertEqual(request["cover_path"], self.cover)
        self.assertEqual(request["per_account_overrides"][first["id"]]["title"], "抖音专属标题")
        self.assertEqual(request["per_account_overrides"][second["id"]]["description"], "")
        self.assertEqual(len(store.list_records("publisher_tasks")), 2)
        self.assertTrue(all(task["status"] == "prepared" for task in store.list_records("publisher_tasks")))
        self.enqueue_mock.assert_not_called()
        self.config_mock.assert_not_called()

    def test_prepared_history_is_visible_without_current_material_or_accounts(self):
        account = self.account()
        task = self.prepare(self.video, "独立存档标题", "存档正文", [account["id"]], cover_path=self.cover)[0]
        self.accounts = []
        app = self.app(current=False)
        self.assertTrue(any("独立存档标题" in row.label for row in app.expander))
        self.assertTrue(app.button(key="creator_confirm_" + task["id"]).disabled)
        self.assertTrue(any("存档正文" == row.value for row in app.text))
        self.enqueue_mock.assert_not_called()

    def test_running_or_uncertain_tasks_have_no_retry_button(self):
        account = self.account()
        for status, submit_started in (("running", False), ("submission_unknown", True), ("submitted", True)):
            task = self.prepare(self.video, "状态保护" + status, "", [account["id"]])[0]
            store.update_record("publisher_tasks", task["id"], {"status": status, "submit_started": submit_started})
        app = self.app(current=False)
        self.assertFalse(any(row.key.startswith("creator_confirm_") for row in app.button))
        self.assertTrue(any("不会直接重发" in row.value for row in app.warning))
        self.enqueue_mock.assert_not_called()

    def test_active_same_account_locks_other_prepared_task_and_login(self):
        account = self.account()
        running = self.prepare(self.video, "正在发布", "", [account["id"]])[0]
        prepared = self.prepare(self.video, "另一个预览", "", [account["id"]])[0]
        store.update_record("publisher_tasks", running["id"], {"status": "running"})
        app = self.app()
        self.assertTrue(app.button(key="creator_confirm_" + prepared["id"]).disabled)
        self.assertTrue(app.button(key="creator_login_" + account["id"]).disabled)
        self.enqueue_mock.assert_not_called()

    def test_closed_previously_verified_account_is_not_labeled_logged_in(self):
        account = self.account(login_status="previously_verified", login_verified=True, login_open=False)
        app = self.app()
        self.assertTrue(any("此前已核对，请再次检查" in row.value for row in app.caption))
        self.assertFalse(any("账号状态：本次已核对登录" in row.value for row in app.caption))
        self.assertFalse(app.button(key="creator_login_" + account["id"]).disabled)

    def test_account_add_is_explicit_and_does_not_open_login(self):
        app = self.app(current=False)
        app.text_input(key="creator_publish_account_name").set_value("我的小红书账号").run()
        app.selectbox(key="creator_publish_account_platform").select("xiaohongshu").run()
        with patch("app.services.creator.publishing.save_account", return_value={"id": "saved"}) as save:
            app.button(key="creator_publish_add_account").click().run()
            save.assert_called_once_with("xiaohongshu", "我的小红书账号")
        self.assertFalse(app.exception)
        self.open_mock.assert_not_called()
        self.enqueue_mock.assert_not_called()

    def test_login_open_check_and_close_are_only_explicit_account_actions(self):
        account = self.account()
        app = self.app()
        self.open_mock.side_effect = None
        self.open_mock.return_value = {"status": "waiting_login", "message": "请在专用窗口扫码登录。"}
        app.button(key="creator_login_" + account["id"]).click().run()
        self.collect(app)
        self.open_mock.assert_called_once_with(account["id"])
        self.check_mock.assert_not_called()
        self.close_mock.assert_not_called()
        self.enqueue_mock.assert_not_called()
        account.update(login_open=True, login_status="waiting_login")
        app.run()
        self.assertTrue(app.button(key="creator_login_" + account["id"]).disabled)
        self.check_mock.side_effect = None
        self.check_mock.return_value = {"status": "waiting_login", "message": "尚未确认登录，请核对专用窗口。"}
        app.button(key="creator_check_login_" + account["id"]).click().run()
        self.collect(app)
        self.check_mock.assert_called_once()
        self.assertEqual(self.check_mock.call_args.args, (account["id"],))
        self.assertTrue(any("账号状态：等待扫码登录" in item.value for item in app.caption))
        self.close_mock.side_effect = None
        self.close_mock.return_value = {"status": "closing", "message": "正在关闭这个账号的登录窗口。"}
        app.button(key="creator_close_login_" + account["id"]).click().run()
        self.collect(app)
        self.close_mock.assert_called_once_with(account["id"])
        self.open_mock.assert_called_once()
        self.config_mock.assert_not_called()

    def test_explicit_confirm_uses_atomic_enqueue_and_locks_account_while_queued(self):
        account = self.account()
        first = self.prepare(self.video, "明确确认的作品", "正文", [account["id"]])[0]
        second = self.prepare(self.video, "等待同一账号", "", [account["id"]])[0]
        app = self.app()
        def enqueue(ident, app_config=None):
            # Real jobs.submit first recovers the root, then writes its new
            # queued record. Preserve that order in this bounded UI fixture.
            jobs.list_jobs()
            return store.save_record("jobs", "mock-confirmed-job", {"state": "queued", "label": "发布确认", "message": "排队中", "progress": 0})["id"]
        self.enqueue_mock.side_effect = enqueue
        app.button(key="creator_confirm_" + first["id"]).click().run()
        self.enqueue_mock.assert_called_once_with(first["id"], app_config={"llm_provider": "test"})
        self.assertEqual(app.session_state["creator_publish_pending"]["mock-confirmed-job"]["task_id"], first["id"])
        self.assertTrue(app.button(key="creator_confirm_" + second["id"]).disabled)
        self.assertTrue(app.button(key="creator_login_" + account["id"]).disabled)
        self.open_mock.assert_not_called()

    def test_previous_upload_cannot_restore_an_intentionally_cleared_cover_path(self):
        app = self.app(creator_publish_cover_import={"path": self.cover, "name": "之前上传的封面.png"},
                       creator_publish_cover_import_applied=self.cover)
        app.text_input(key="creator_publish_cover").set_value("").run()
        app.checkbox(key="creator_publish_cover_enabled").uncheck().run()
        app.radio(key="creator_selected_tab").set_value("标题和封面").run()
        app.radio(key="creator_selected_tab").set_value("发布中心").run()
        self.assertEqual(app.text_input(key="creator_publish_cover").value, "")
        self.assertFalse(app.checkbox(key="creator_publish_cover_enabled").value)
        self.assertFalse(app.button(key="creator_publish_export").disabled)

    def test_local_export_without_account_contains_selected_files_and_never_model(self):
        app = self.app()
        app.button(key="creator_publish_export").click().run()
        self.collect(app)
        self.export_mock.assert_called_once()
        path = app.session_state["creator_publish_package"]["zip_path"]
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read("video.mp4"), Path(self.video).read_bytes())
            self.assertEqual(archive.read("cover.png"), Path(self.cover).read_bytes())
            self.assertIn("本次精确标题", archive.read("copy.txt").decode())
        self.assertTrue(any(row.proto.label == "下载本地素材包" for row in app.get("download_button")))
        self.prepare_mock.assert_not_called()
        self.enqueue_mock.assert_not_called()
        self.config_mock.assert_not_called()

    def test_changing_copy_marks_existing_zip_stale_instead_of_downloadable(self):
        app = self.app()
        app.button(key="creator_publish_export").click().run()
        self.collect(app)
        app.text_input(key="creator_publish_title").set_value("素材包的新标题").run()
        self.assertTrue(any("修改后请重新生成" in row.value for row in app.info))
        self.assertFalse(any(row.proto.label == "下载本地素材包" for row in app.get("download_button")))

    def test_overwriting_same_video_path_marks_previous_zip_stale(self):
        app = self.app()
        app.button(key="creator_publish_export").click().run()
        self.collect(app)
        self.assertTrue(any(row.proto.label == "下载本地素材包" for row in app.get("download_button")))
        Path(self.video).write_bytes(b"changed-valid-video-fixture-for-mocked-inspector")
        app.run()
        self.assertTrue(any("修改后请重新生成" in row.value for row in app.info))
        self.assertFalse(any(row.proto.label == "下载本地素材包" for row in app.get("download_button")))

    def test_prepare_failure_retains_existing_prepared_history_and_editable_copy(self):
        account = self.account()
        previous = self.prepare(self.video, "以前保存的预览", "", [account["id"]])[0]
        app = self.app()
        app.multiselect(key="creator_publish_selected_accounts").set_value([account["id"]]).run()
        self.prepare_mock.side_effect = ValueError("视频不完整，请重新选择。")
        app.button(key="creator_publish_prepare").click().run()
        notices = self.collect(app)
        self.assertTrue(any("视频不完整" in value for value in notices["error"]))
        self.assertEqual(app.text_input(key="creator_publish_title").value, "本次精确标题")
        self.assertEqual(store.get_record("publisher_tasks", previous["id"])["status"], "prepared")
        self.enqueue_mock.assert_not_called()

    def test_navigation_and_text_edits_do_not_repeat_media_probe(self):
        app = self.app()
        app.text_input(key="creator_publish_title").set_value("更新标题").run()
        app.radio(key="creator_selected_tab").set_value("标题和封面").run()
        app.radio(key="creator_selected_tab").set_value("发布中心").run()
        self.assertEqual(self.inspect_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
