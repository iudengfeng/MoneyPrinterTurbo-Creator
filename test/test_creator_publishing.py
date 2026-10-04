import os
import hashlib
import json
import shutil
import subprocess
import threading
import time
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.services.creator import extract, publishing, store


class PublishingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.assets.name) / "actual-video.mp4"
        subprocess.run([extract.ffmpeg_binary(), "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                        "color=c=blue:s=160x240:r=25:d=1", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(cls.fixture)],
                       check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"MPT_CREATOR_DATA": str(self.root / "creator")})
        self.env.start()
        self.video = self.root / "test.mp4"
        shutil.copy2(self.fixture, self.video)
        self.cover = self.root / "cover.png"
        Image.new("RGB", (720, 1280), "#ff8811").save(self.cover)
        self.account = publishing.save_account("抖音", "测试账号")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def draft(self):
        return publishing.prepare_publish(str(self.video), "测试标题", "测试正文", [self.account["id"]])[0]

    def test_account_profiles_are_distinct_and_persist(self):
        second = publishing.save_account("xiaohongshu", "测试账号")
        self.assertNotEqual(self.account["profile_path"], second["profile_path"])
        self.assertTrue(Path(second["profile_path"]).is_dir())
        self.assertEqual(self.account["id"], publishing.save_account("douyin", "测试账号")["id"])
        self.assertEqual(len(publishing.list_accounts()), 2)

    def test_prepare_does_not_start_browser_and_duplicate_keeps_status(self):
        with patch.object(publishing, "BrowserWorker") as worker, patch.object(publishing, "open_login") as login:
            task = self.draft()
            store.update_record("publisher_tasks", task["id"], {"status": "submitted", "submit_started": True})
            duplicate = self.draft()
            worker.assert_not_called()
            login.assert_not_called()
        self.assertEqual(task["id"], duplicate["id"])
        self.assertEqual(duplicate["status"], "submitted")
        self.assertEqual(Path(task["video_path"]).read_bytes(), self.video.read_bytes())

    def test_additional_selected_account_never_duplicates_first_account(self):
        first = self.draft()
        second_account = publishing.save_account("小红书", "另一个账号")
        results = publishing.prepare_publish(str(self.video), "测试标题", "测试正文", [self.account["id"], second_account["id"]])
        self.assertEqual(first["id"], results[0]["id"])
        self.assertNotEqual(results[0]["id"], results[1]["id"])

    def test_uncertain_submit_never_retries_browser(self):
        task = self.draft()
        store.update_record("publisher_tasks", task["id"], {"status": "running", "submit_started": True})
        with patch.object(publishing, "BrowserWorker") as worker:
            result = publishing.execute_publish(task["id"])
            worker.assert_not_called()
        self.assertTrue(result["submit_started"])

    def test_same_account_serializes_across_tasks(self):
        with publishing._account_lease(self.account["id"]):
            with self.assertRaisesRegex(RuntimeError, "正在登录或发布"):
                publishing._acquire(self.account["id"])
        token = publishing._acquire(self.account["id"])
        publishing._release(self.account["id"], token)

    def test_waiting_login_does_not_submit(self):
        task = self.draft()
        with patch.object(publishing, "BrowserWorker") as worker, patch.object(publishing, "_decide") as decide:
            worker.return_value.start.return_value = {"status": "waiting_login", "snapshot": "请先登录"}
            result = publishing.execute_publish(task["id"])
            decide.assert_not_called()
            worker.return_value.action.assert_not_called()
        self.assertEqual(result["status"], "waiting_login")
        self.assertFalse(result["submit_started"])

    def test_submit_marker_is_durable_before_click_and_result_is_not_invented(self):
        task = self.draft()
        def action(value):
            if value["action"] == "submit":
                self.assertTrue(store.get_record("publisher_tasks", task["id"])["submit_started"])
            return {"status": "submission_unknown", "snapshot": "仍在等待平台返回", "flags": {"submitted": True}}
        with patch.object(publishing, "BrowserWorker") as worker, patch.object(publishing, "_decide", return_value={"action": "submit", "ref": "e3"}):
            worker.return_value.start.return_value = {"snapshot": 'button "发布" [ref=e3]', "flags": {}}
            worker.return_value.action.side_effect = action
            result = publishing.execute_publish(task["id"])
        self.assertEqual(result["status"], "submission_unknown")
        self.assertNotIn("work_url", result)

    def test_pre_submit_failure_remains_retryable(self):
        task = self.draft()
        with patch.object(publishing, "BrowserWorker", side_effect=RuntimeError("浏览器未就绪")):
            result = publishing.execute_publish(task["id"])
        self.assertEqual(result["status"], "failed_before_submit")
        self.assertFalse(result["submit_started"])

    def test_successful_submit_is_returned_once(self):
        task = self.draft()
        with patch.object(publishing, "BrowserWorker") as worker, patch.object(publishing, "_decide", return_value={"action": "submit", "ref": "e3"}):
            worker.return_value.start.return_value = {"snapshot": 'button "发布" [ref=e3]', "flags": {}}
            worker.return_value.action.return_value = {"status": "submitted", "snapshot": "发布成功", "flags": {"submitted": True}}
            result = publishing.execute_publish(task["id"])
            again = publishing.execute_publish(task["id"])
            self.assertEqual(worker.call_count, 1)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(again["id"], result["id"])

    def test_real_media_inspection_and_cover_details_without_staging(self):
        result = publishing.inspect_media(self.video, self.cover)
        self.assertEqual((result["width"], result["height"]), (160, 240))
        self.assertAlmostEqual(result["duration"], 1, places=1)
        self.assertFalse(result["has_audio"])
        self.assertEqual((result["cover_width"], result["cover_height"]), (720, 1280))
        self.assertEqual(result["video_bytes"], self.video.stat().st_size)
        self.assertFalse((store.data_root() / "publishing_assets").exists())

    def test_broken_video_cover_and_audio_only_never_create_tasks(self):
        broken = self.root / "broken.mp4"
        broken.write_bytes(b"<html>not actual media</html>")
        for video in (broken, self.root / "missing.mp4"):
            with self.subTest(video=video), self.assertRaises(ValueError):
                publishing.prepare_publish(video, "标题", "正文", [self.account["id"]])
        self.cover.write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "有效图片"):
            publishing.prepare_publish(self.video, "标题", "正文", [self.account["id"]], self.cover)
        audio = self.root / "no-video.mp4"
        subprocess.run([extract.ffmpeg_binary(), "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-c:a", "aac", str(audio)], check=True, capture_output=True, timeout=30)
        with self.assertRaisesRegex(ValueError, "视频画面"):
            publishing.prepare_publish(audio, "标题", "正文", [self.account["id"]])
        self.assertEqual(publishing.list_tasks(), [])

    def test_overrides_saved_per_account_and_original_tasks_not_overwritten(self):
        second = publishing.save_account("小红书", "第二账号")
        originals = publishing.prepare_publish(self.video, "共同标题", "共同正文", [self.account["id"], second["id"]], self.cover)
        overrides = {self.account["id"]: {"title": "抖音独立标题", "description": "抖音独立正文"},
                     second["id"]: {"title": "小红书独立标题", "description": "小红书独立正文"}}
        results = publishing.prepare_publish(self.video, "共同标题", "共同正文", [self.account["id"], second["id"]], self.cover,
                                             per_account_overrides=overrides)
        self.assertEqual([(row["title"], row["description"]) for row in results], [("抖音独立标题", "抖音独立正文"), ("小红书独立标题", "小红书独立正文")])
        self.assertTrue(all(row["id"] != originals[index]["id"] for index, row in enumerate(results)))
        for original in originals:
            self.assertEqual(publishing.get_task(original["id"])["title"], "共同标题")
        overrides[self.account["id"]]["title"] = "后改的输入对象"
        self.assertEqual(publishing.get_task(results[0]["id"])["title"], "抖音独立标题")
        self.video.write_bytes(b"later changed source")
        self.assertEqual(Path(results[0]["video_path"]).read_bytes(), self.fixture.read_bytes())

    def test_add_account_with_different_override_does_not_duplicate_first(self):
        first = publishing.prepare_publish(self.video, "共同标题", "正文", [self.account["id"]], per_account_overrides={self.account["id"]: {"title": "独立标题"}})[0]
        second = publishing.save_account("小红书", "第二账号")
        rows = publishing.prepare_publish(self.video, "共同标题", "正文", [self.account["id"], second["id"]],
                                          per_account_overrides={self.account["id"]: {"title": "独立标题"}, second["id"]: {"title": "第二标题"}})
        self.assertEqual(first["id"], rows[0]["id"])
        self.assertNotEqual(first["id"], rows[1]["id"])

    def test_text_limit_reports_instead_of_truncating_and_validates_overrides(self):
        for kwargs in ({"title": "x" * 121}, {"description": "x" * 10001},
                       {"per_account_overrides": {"unselected": {"title": "标题"}}},
                       {"per_account_overrides": {self.account["id"]: {"title": ""}}},
                       {"per_account_overrides": {self.account["id"]: {"cover": "unexpected"}}}):
            args = dict(video_path=self.video, title="标题", description="正文", accounts=[self.account["id"]])
            args.update(kwargs)
            with self.subTest(kwargs=str(kwargs)[:80]), self.assertRaises(ValueError):
                publishing.prepare_publish(**args)
        title = "中" * 120
        body = "正文" * 3000
        row = publishing.prepare_publish(self.video, title, body, [self.account["id"]])[0]
        self.assertEqual(row["title"], title)
        self.assertEqual(row["description"], body)

    def test_no_cover_materials_do_not_inherit_previous_cover(self):
        first = publishing.prepare_publish(self.video, "标题", "正文", [self.account["id"]], self.cover)[0]
        second = publishing.prepare_publish(self.video, "标题", "正文", [self.account["id"]], None)[0]
        self.assertNotEqual(first["id"], second["id"])
        self.assertIsNone(second["cover_path"])
        self.assertEqual(second["cover_hash"], "")

    def test_login_state_requires_live_verified_worker_and_matching_account(self):
        status_file = publishing._status_file(self.account)
        state = {"account_id": self.account["id"], "platform": "douyin", "pid": os.getpid(), "status": "logged_in",
                 "verified": True, "updated_at": "2026-10-04T01:00:00Z", "verified_at": "2026-10-04T01:00:00Z"}
        status_file.write_text(json.dumps(state), "utf-8")
        current = publishing.account_status(self.account["id"])
        self.assertTrue(current["login_verified"])
        self.assertEqual(current["status"], "logged_in")
        with patch.object(publishing, "_alive", return_value=False):
            closed = publishing.account_status(self.account["id"])
        self.assertFalse(closed["login_verified"])
        self.assertEqual(closed["status"], "previously_verified")
        state.update(account_id="different", verified=True)
        status_file.write_text(json.dumps(state), "utf-8")
        self.assertFalse(publishing.account_status(self.account["id"])["login_verified"])
        state.update(account_id=self.account["id"], status="login_ready")
        state.pop("verified")
        status_file.write_text(json.dumps(state), "utf-8")
        self.assertFalse(publishing.account_status(self.account["id"])["login_verified"])
        status_file.write_text(json.dumps({"status": {"corrupt": True}, "pid": True, "verified": True}), "utf-8")
        self.assertFalse(publishing.account_status(self.account["id"])["login_verified"])
        status_file.write_text(json.dumps({"status": "logged_in", "pid": os.getpid(), "verified": True}), "utf-8")
        self.assertFalse(publishing.account_status(self.account["id"])["login_verified"])

    def test_check_and_close_login_only_signal_existing_worker(self):
        status_file = publishing._status_file(self.account)
        status_file.write_text(json.dumps({"pid": os.getpid(), "status": "waiting_login", "verified": False}), "utf-8")
        with patch.object(publishing, "BrowserWorker") as worker, patch.object(publishing.subprocess, "Popen") as popen, patch.object(publishing, "_LOGIN_REFRESH_TIMEOUT", 0.1):
            result = publishing.check_login(self.account["id"])
            self.assertTrue(result["login_open"])
            self.assertTrue(result["login_check_pending"])
            self.assertTrue(status_file.with_suffix(".check").is_file())
            closing = publishing.close_login(self.account["id"])
            self.assertEqual(closing["status"], "closing")
            self.assertFalse(closing["login_verified"])
            self.assertTrue(status_file.with_suffix(".close").is_file())
            worker.assert_not_called()
            popen.assert_not_called()
        status_file.write_text(json.dumps({"pid": os.getpid(), "status": "closed", "verified": False, "was_verified": True}), "utf-8")
        self.assertEqual(publishing.close_login(self.account["id"])["status"], "previously_verified")
        self.assertFalse(status_file.with_suffix(".close").exists())

    def test_login_refresh_waits_for_new_worker_evidence_without_opening_browser(self):
        status_file = publishing._status_file(self.account)
        status = {"account_id": self.account["id"], "platform": "douyin", "pid": os.getpid(),
                  "status": "waiting_login", "verified": False, "updated_at": "before-refresh"}
        status_file.write_text(json.dumps(status), "utf-8")
        def update_after_request():
            deadline = time.monotonic() + 3
            while not status_file.with_suffix(".check").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            status_file.write_text(json.dumps(dict(status, status="logged_in", verified=True, updated_at="after-refresh")), "utf-8")
            status_file.with_suffix(".check").unlink(missing_ok=True)
        thread = threading.Thread(target=update_after_request)
        thread.start()
        try:
            with patch.object(publishing.subprocess, "Popen") as popen:
                result = publishing.check_login(self.account["id"])
                popen.assert_not_called()
        finally:
            thread.join(timeout=3)
        self.assertEqual(result["login_checked_at"], "after-refresh")
        self.assertTrue(result["login_verified"])
        self.assertFalse(result["login_check_pending"])

    def test_local_zip_with_no_account_contains_matching_file_hashes_and_copy(self):
        store.delete_record("publisher_accounts", self.account["id"])
        events = []
        with patch.object(publishing, "BrowserWorker") as worker:
            result = publishing.export_materials(self.video, "本地导出标题", "完整发布正文\n#话题", self.cover,
                                                progress=lambda *args: events.append(args))
            worker.assert_not_called()
        self.assertEqual(result["state"], "done")
        with zipfile.ZipFile(result["zip_path"]) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            video_bytes = archive.read(metadata["video"]["file"])
            cover_bytes = archive.read(metadata["cover"]["file"])
            self.assertEqual(hashlib.sha256(video_bytes).hexdigest(), metadata["video"]["sha256"])
            self.assertEqual(hashlib.sha256(cover_bytes).hexdigest(), metadata["cover"]["sha256"])
            self.assertEqual(video_bytes, self.video.read_bytes())
            self.assertEqual(cover_bytes, self.cover.read_bytes())
            self.assertIn("完整发布正文\n#话题", archive.read("发布文案.txt").decode("utf-8"))
            self.assertNotIn("profile_path", archive.read("metadata.json").decode("utf-8"))
        self.assertEqual(events[-1][1], 100)
        self.assertEqual(publishing.list_tasks(), [])

    def test_zip_without_cover_and_per_account_copy_remains_exact(self):
        overrides = {self.account["id"]: {"title": "专属标题", "description": "专属正文"}}
        result = publishing.export_materials(self.video, "默认标题", "默认正文", per_account_overrides=overrides)
        with zipfile.ZipFile(result["zip_path"]) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            self.assertIsNone(metadata["cover"])
            self.assertFalse(any(name.startswith("cover") for name in archive.namelist()))
            self.assertEqual(metadata["per_account_overrides"], overrides)
            self.assertIn("专属正文", archive.read("账号文案-01.txt").decode("utf-8"))

    def test_export_failure_preserves_old_package_and_safe_error(self):
        first = publishing.export_materials(self.video, "已有素材包")
        old_bytes = Path(first["zip_path"]).read_bytes()
        with patch.object(publishing.zipfile.ZipFile, "write", side_effect=RuntimeError("secret-key-example")):
            with self.assertRaises(RuntimeError) as caught:
                publishing.export_materials(self.video, "失败素材包")
        self.assertNotIn("secret-key-example", str(caught.exception))
        self.assertEqual(Path(first["zip_path"]).read_bytes(), old_bytes)
        self.assertEqual(len(publishing.list_exports()), 1)
        self.assertTrue(any(row["state"] == "failed" for row in store.list_records("publisher_exports")))

    def test_reservation_blocks_same_task_same_account_and_login_lease(self):
        first = self.draft()
        second = publishing.prepare_publish(self.video, "不同标题", "正文", [self.account["id"]])[0]
        token = publishing.reserve_publish(first["id"])
        self.assertEqual(publishing.get_task(first["id"])["status"], "queued")
        with self.assertRaises(RuntimeError):
            publishing.reserve_publish(first["id"])
        with self.assertRaises(RuntimeError):
            publishing.reserve_publish(second["id"])
        with self.assertRaises(RuntimeError):
            publishing._acquire(self.account["id"])
        self.assertFalse(publishing.cancel_reservation(first["id"], "wrong-token"))
        self.assertTrue(publishing.cancel_reservation(first["id"], token))
        self.assertEqual(publishing.get_task(first["id"])["status"], "prepared")

    def test_enqueue_failure_cancels_queue_and_reserved_account(self):
        from app.services.creator import jobs
        task = self.draft()
        with patch.object(jobs, "submit", side_effect=ValueError("队列已满")):
            with self.assertRaisesRegex(ValueError, "队列已满"):
                publishing.enqueue_publish(task["id"])
        self.assertEqual(publishing.get_task(task["id"])["status"], "prepared")
        token = publishing._acquire(self.account["id"])
        publishing._release(self.account["id"], token)

    def test_deleted_account_cannot_create_a_reservation(self):
        task = self.draft()
        store.delete_record("publisher_accounts", self.account["id"])
        with self.assertRaisesRegex(ValueError, "发布账号不存在"):
            publishing.reserve_publish(task["id"])
        with store.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM publisher_leases").fetchone()[0], 0)
        self.assertEqual(publishing.get_task(task["id"])["status"], "prepared")

    def test_account_deleted_after_queue_releases_only_its_unstarted_reservation(self):
        task = self.draft()
        token = publishing.reserve_publish(task["id"])
        store.delete_record("publisher_accounts", self.account["id"])
        with patch.object(publishing, "BrowserWorker") as worker:
            with self.assertRaisesRegex(ValueError, "发布账号不存在"):
                publishing.execute_publish(task["id"], reservation_token=token)
            worker.assert_not_called()
        self.assertEqual(publishing.get_task(task["id"])["status"], "failed_before_submit")
        lease = publishing._acquire(self.account["id"])
        publishing._release(self.account["id"], lease)

    def test_deleted_queued_task_cleanup_requires_exact_token_and_current_pid(self):
        task = self.draft()
        token = publishing.reserve_publish(task["id"])
        store.delete_record("publisher_tasks", task["id"])
        with self.assertRaises(ValueError):
            publishing.execute_publish(task["id"], reservation_token="wrong-token")
        with store.connection() as conn:
            self.assertEqual(conn.execute("SELECT token FROM publisher_leases WHERE account_id=?", (self.account["id"],)).fetchone()[0], token)
            conn.execute("UPDATE publisher_leases SET pid=? WHERE account_id=?", (os.getpid() + 100000, self.account["id"]))
        with self.assertRaises(ValueError):
            publishing.execute_publish(task["id"], reservation_token=token)
        with store.connection() as conn:
            self.assertIsNotNone(conn.execute("SELECT token FROM publisher_leases WHERE account_id=?", (self.account["id"],)).fetchone())
            conn.execute("UPDATE publisher_leases SET pid=? WHERE account_id=?", (os.getpid(), self.account["id"]))
        with self.assertRaises(ValueError):
            publishing.execute_publish(task["id"], reservation_token=token)
        lease = publishing._acquire(self.account["id"])
        publishing._release(self.account["id"], lease)

    def test_missing_task_call_cannot_release_a_running_owner(self):
        task = self.draft()
        token = publishing.reserve_publish(task["id"])
        store.update_record("publisher_tasks", task["id"], {"status": "running", "execution_pid": os.getpid()})
        with self.assertRaises(ValueError):
            publishing.execute_publish("different-missing-task", reservation_token=token)
        with self.assertRaises(RuntimeError):
            publishing._acquire(self.account["id"])
        self.assertEqual(publishing.get_task(task["id"])["status"], "running")
        publishing._release(self.account["id"], token)

    def test_reserved_execute_uses_single_lease_and_does_not_submit_when_logged_out(self):
        from app.services.creator import jobs
        task = self.draft()
        with patch.object(jobs, "submit", return_value="queued-job") as submit:
            self.assertEqual(publishing.enqueue_publish(task["id"], app_config={"snapshot": True}), "queued-job")
        token = submit.call_args.kwargs["reservation_token"]
        with patch.object(publishing, "BrowserWorker") as worker:
            worker.return_value.start.return_value = {"status": "waiting_login"}
            result = publishing.execute_publish(task["id"], reservation_token=token)
            worker.assert_called_once()
            worker.return_value.action.assert_not_called()
        self.assertEqual(result["status"], "waiting_login")
        lease = publishing._acquire(self.account["id"])
        publishing._release(self.account["id"], lease)

    def test_dead_claim_recovers_without_repeating_uncertain_submission(self):
        task = self.draft()
        publishing.reserve_publish(task["id"])
        with patch.object(publishing, "_alive", return_value=False):
            self.assertEqual(publishing.get_task(task["id"])["status"], "failed_before_submit")
        store.update_record("publisher_tasks", task["id"], {"status": "running", "execution_pid": 999999, "submit_started": True})
        with patch.object(publishing, "_alive", return_value=False):
            unknown = publishing.get_task(task["id"])
        self.assertEqual(unknown["status"], "submission_unknown")
        with patch.object(publishing, "BrowserWorker") as worker:
            again = publishing.execute_publish(task["id"])
            worker.assert_not_called()
        self.assertEqual(again["status"], "submission_unknown")

    def test_execute_rejects_modified_staged_media_without_browser(self):
        task = self.draft()
        Path(task["video_path"]).write_bytes(b"changed file")
        with patch.object(publishing, "BrowserWorker") as worker:
            result = publishing.execute_publish(task["id"])
            worker.assert_not_called()
        self.assertEqual(result["status"], "failed_before_submit")
        self.assertFalse(result["submit_started"])


if __name__ == "__main__":
    unittest.main()
