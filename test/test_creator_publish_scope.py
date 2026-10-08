"""Publishing stops after bounded submission verification, without analytics."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.creator import publishing, store


class PublishingScopeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.folder.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.video = Path(self.folder.name) / "browser-fixture.mp4"
        self.video.write_bytes(b"isolated-browser-fixture")
        account = publishing.save_account("douyin", "scope-fixture")
        self.task = store.save_record("publisher_tasks", "scope-test", {
            "status": "prepared", "account_id": account["id"], "platform": "douyin",
            "platform_name": "抖音", "title": "本地测试", "description": "",
            "video_path": str(self.video), "video_hash": hashlib.sha256(self.video.read_bytes()).hexdigest(),
            "cover_path": "", "submit_started": False,
        })

    def test_submission_verification_stops_after_review_result_and_closes_browser(self):
        observations = [
            {"status": "submission_unknown", "snapshot": "提交处理中", "flags": {"submitted": True}},
            {"status": "reviewing", "snapshot": "审核中", "flags": {"submitted": True}},
        ]
        with patch.object(publishing, "inspect_media"), patch.object(publishing, "BrowserWorker") as worker, patch.object(
            publishing, "_decide", return_value={"action": "submit", "ref": "e3"}
        ) as decide:
            worker.return_value.start.return_value = {"snapshot": '- button "发布" [ref=e3]', "flags": {}}
            worker.return_value.action.side_effect = observations
            result = publishing.execute_publish(self.task["id"])
        self.assertEqual(result["status"], "reviewing")
        self.assertEqual(result["result_evidence"], "审核中")
        self.assertEqual(decide.call_count, 1)
        self.assertEqual([call.args[0]["action"] for call in worker.return_value.action.call_args_list], ["submit", "wait"])
        worker.return_value.close.assert_called_once_with()

    def test_unconfirmed_result_is_bounded_and_never_reopens_or_republishes(self):
        with patch.object(publishing, "inspect_media"), patch.object(publishing, "BrowserWorker") as worker, patch.object(
            publishing, "_decide", return_value={"action": "submit", "ref": "e3"}
        ) as decide:
            worker.return_value.start.return_value = {"snapshot": '- button "发布" [ref=e3]', "flags": {}}
            worker.return_value.action.return_value = {"status": "submission_unknown", "snapshot": "结果待核对", "flags": {"submitted": True}}
            result = publishing.execute_publish(self.task["id"])
            repeated = publishing.execute_publish(self.task["id"])
        self.assertEqual(result["status"], "submission_unknown")
        self.assertEqual(repeated["status"], "submission_unknown")
        self.assertTrue(result["submit_started"])
        self.assertEqual(decide.call_count, 1)
        self.assertEqual(worker.call_count, 1)
        self.assertEqual([call.args[0]["action"] for call in worker.return_value.action.call_args_list], ["submit"] + ["wait"] * 6)
        worker.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
