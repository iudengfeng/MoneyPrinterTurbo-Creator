"""Official API contract tests using mock HTTP responses, not live Douyin data."""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from app.services.creator import douyin_search, jobs, store


CONFIG = {"creator_douyin_search": {"client_key": "test-app", "client_secret": "private-secret-marker", "device_id": "123456"}}
TOKEN = "private-token-marker"


def response(payload, status=200):
    return SimpleNamespace(status_code=status, json=Mock(return_value=payload), close=Mock())


def token_response():
    return response({"data": {"error_code": 0, "access_token": TOKEN, "expires_in": 7200}, "message": "success"})


def item(ident, days_old=1, likes=20, text="这是一篇官方接口返回的口播正文。", **changes):
    record = {
        "item_id": str(ident), "title": f"参考标题 {ident}",
        "create_time": int((datetime.now(timezone.utc) - timedelta(days=days_old)).timestamp()),
        "statistics": {"digg_count": likes}, "high_quality_text": text,
        "nickname": "示例作者", "link": f"https://www.douyin.com/video/{ident}",
    }
    record.update(changes)
    return record


def page(items, cursor=20, has_more=False, search_id="official-search-id"):
    return response({"err_no": 0, "err_msg": "success", "data": {"data": {
        "video_list": items, "cursor": cursor, "has_more": has_more, "search_id": search_id,
    }}})


class DouyinSearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.directory.name})
        self.environment.start()
        with douyin_search._token_lock:
            douyin_search._token_cache.clear()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_configuration_requires_nested_credentials_and_int64_device_id(self):
        self.assertTrue(douyin_search.is_configured(CONFIG))
        for settings in [
            {}, {"client_key": "app", "client_secret": "secret"},
            {"client_key": "app", "client_secret": "secret", "device_id": False},
            {"client_key": "app", "client_secret": "secret", "device_id": "bad"},
            {"client_key": "app", "client_secret": "secret", "device_id": 9223372036854775808},
        ]:
            with self.subTest(settings=settings):
                self.assertFalse(douyin_search.is_configured({"creator_douyin_search": settings}))

    def test_invalid_parameters_and_missing_credentials_make_no_network_request(self):
        with patch.object(douyin_search.requests, "post") as post, patch.object(douyin_search.requests, "get") as get:
            for arguments in [
                {"keyword": ""}, {"keyword": "家具", "count": 0}, {"keyword": "家具", "count": True},
                {"keyword": "家具", "count": 21}, {"keyword": "家具", "days": 0},
                {"keyword": "家具", "days": 181}, {"keyword": "家具", "sort": "wrong"},
            ]:
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    douyin_search.search(**arguments, app_config=CONFIG)
            with self.assertRaises(ValueError):
                douyin_search.search("家具", app_config={})
            post.assert_not_called()
            get.assert_not_called()

    def test_request_contract_uses_fixed_official_hosts_token_header_and_timeouts(self):
        with patch.object(douyin_search.requests, "post", return_value=token_response()) as post, patch.object(douyin_search.requests, "get", return_value=page([item(101)])) as get:
            result = douyin_search.search("家具", app_config=CONFIG)
        self.assertEqual(douyin_search.TOKEN_URL, post.call_args.args[0])
        self.assertEqual("client_credential", post.call_args.kwargs["json"]["grant_type"])
        self.assertEqual(douyin_search.SEARCH_URL, get.call_args.args[0])
        self.assertEqual(TOKEN, get.call_args.kwargs["headers"]["access-token"])
        for call in [post.call_args, get.call_args]:
            self.assertEqual((10, 35), call.kwargs["timeout"])
            self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(180, get.call_args.kwargs["params"]["publish_time"])
        self.assertEqual(1, get.call_args.kwargs["params"]["sort_type"])
        self.assertNotIn(TOKEN, json.dumps(get.call_args.kwargs["params"]))
        self.assertEqual("抖音官方搜索", result[0]["source_label"])
        self.assertIn("created_at", result[0])
        persisted = json.dumps(store.list_records("references") + store.list_records("douyin_results"))
        self.assertNotIn(TOKEN, persisted)
        self.assertNotIn("private-secret-marker", persisted)

    def test_token_is_reused_in_memory_for_same_credentials(self):
        with patch.object(douyin_search.requests, "post", return_value=token_response()) as post, patch.object(douyin_search.requests, "get", return_value=page([item(101)])):
            douyin_search.search("家具", app_config=CONFIG)
            douyin_search.search("装修", app_config=CONFIG)
        self.assertEqual(1, post.call_count)

    def test_multiple_pages_use_cursor_and_search_id_then_filter_and_sort_real_likes(self):
        pages = [
            page([item(101, likes=10), item(102, days_old=31, likes=9999)], cursor=20, has_more=True),
            page([item(103, likes=50), item(104, likes=30, text="")], cursor=40),
        ]
        with patch.object(douyin_search.requests, "post", return_value=token_response()), patch.object(douyin_search.requests, "get", side_effect=pages) as get:
            results = douyin_search.search("家具", count=3, days=30, app_config=CONFIG)
        self.assertEqual(["103", "104", "101"], [row["item_id"] for row in results])
        self.assertEqual(20, get.call_args_list[1].kwargs["params"]["cursor"])
        self.assertEqual("official-search-id", get.call_args_list[1].kwargs["params"]["search_id"])
        self.assertEqual(4, results[0]["scanned_count"])
        self.assertEqual(2, len(store.list_records("references")))
        self.assertEqual(3, len(store.list_records("douyin_results")))

    def test_unknown_statistics_stay_none_instead_of_fabricated_zero(self):
        items = [item(101, statistics={}), item(102, likes=0), item(103, likes=-4)]
        with patch.object(douyin_search.requests, "post", return_value=token_response()), patch.object(douyin_search.requests, "get", return_value=page(items)):
            results = douyin_search.search("家具", count=3, app_config=CONFIG)
        self.assertEqual("102", results[0]["item_id"])
        self.assertEqual(0, results[0]["digg_count"])
        unknown = [row for row in results if row["item_id"] == "101"][0]
        self.assertIsNone(unknown["digg_count"])
        self.assertEqual("", unknown["metrics"])

    def test_missing_or_future_publication_date_is_not_claimed_inside_time_window(self):
        items = [item(101, create_time=None), item(102, days_old=-1), item(103, days_old=29), item(104, days_old=31)]
        with patch.object(douyin_search.requests, "post", return_value=token_response()), patch.object(douyin_search.requests, "get", return_value=page(items)):
            results = douyin_search.search("家具", days=30, app_config=CONFIG)
        self.assertEqual(["103"], [row["item_id"] for row in results])
        self.assertEqual(1, results[0]["skipped_missing_date"])
        self.assertEqual(30, results[0]["search_window_days"])

    def test_missing_body_is_saved_as_result_without_turning_title_into_reference_text(self):
        with patch.object(douyin_search.requests, "post", return_value=token_response()), patch.object(douyin_search.requests, "get", return_value=page([item(101, text=None)])):
            result = douyin_search.search("家具", app_config=CONFIG)[0]
        self.assertFalse(result["has_text"])
        self.assertIsNone(result["reference_id"])
        self.assertEqual("", result["text"])
        self.assertEqual([], store.list_records("references"))
        self.assertEqual(1, len(store.list_records("douyin_results")))

    def test_repeated_search_deduplicates_and_preserves_user_edited_reference(self):
        with patch.object(douyin_search.requests, "post", return_value=token_response()), patch.object(douyin_search.requests, "get", side_effect=[page([item(101)]), page([item(101, likes=45, text="接口返回了后来变化的正文")])]):
            first = douyin_search.search("家具", app_config=CONFIG)[0]
            store.update_record("references", first["reference_id"], {"title": "用户改过的标题", "text": "用户保存的正文"})
            second = douyin_search.search("家具", app_config=CONFIG)[0]
        self.assertEqual(first["id"], second["id"])
        reference = store.get_record("references", first["reference_id"])
        self.assertEqual("用户改过的标题", reference["title"])
        self.assertEqual("用户保存的正文", reference["text"])
        self.assertEqual(45, reference["digg_count"])
        self.assertEqual(1, len(store.list_records("references")))
        self.assertEqual(1, len(store.list_records("douyin_results")))

    def test_search_is_bounded_to_five_pages_and_one_hundred_items(self):
        pages = [page([item(page_index * 20 + index, likes=page_index * 20 + index, text="") for index in range(20)], cursor=(page_index + 1) * 20, has_more=True) for page_index in range(5)]
        with patch.object(douyin_search.requests, "post", return_value=token_response()), patch.object(douyin_search.requests, "get", side_effect=pages) as get:
            results = douyin_search.search("家具", count=20, app_config=CONFIG)
        self.assertEqual(5, get.call_count)
        self.assertEqual(20, len(results))
        self.assertEqual(100, results[0]["scanned_count"])
        self.assertTrue(results[0]["more_available"])
        self.assertIn("不代表全平台排名", results[0]["coverage_label"])

    def test_duplicate_video_in_later_page_is_returned_once(self):
        pages = [page([item(101)], has_more=True), page([item(101), item(102)], cursor=40)]
        with patch.object(douyin_search.requests, "post", return_value=token_response()), patch.object(douyin_search.requests, "get", side_effect=pages):
            results = douyin_search.search("家具", count=3, app_config=CONFIG)
        self.assertEqual(2, len(results))

    def test_permission_error_has_clear_scope_message_and_no_echoed_token(self):
        failure = response({"err_no": 28001018, "err_msg": TOKEN + " " + CONFIG["creator_douyin_search"]["client_secret"]})
        with patch.object(douyin_search.requests, "post", return_value=token_response()), patch.object(douyin_search.requests, "get", return_value=failure), self.assertRaisesRegex(douyin_search.DouyinSearchError, "aweme.dy.video_search") as raised:
            douyin_search.search("家具", app_config=CONFIG)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertNotIn("private-secret-marker", str(raised.exception))
        self.assertEqual([], store.list_records("douyin_results"))

    def test_token_error_code_is_recognized_without_exposing_secret(self):
        failure = response({"data": {"error_code": 10013, "description": "bad " + CONFIG["creator_douyin_search"]["client_secret"]}})
        with patch.object(douyin_search.requests, "post", return_value=failure), patch.object(douyin_search.requests, "get") as get, self.assertRaisesRegex(douyin_search.DouyinSearchError, "应用凭证无效") as raised:
            douyin_search.search("家具", app_config=CONFIG)
        self.assertNotIn("private-secret-marker", str(raised.exception))
        get.assert_not_called()

    def test_network_error_does_not_expose_secret_or_request_url(self):
        error = requests.Timeout("https://example.invalid?access_token=" + TOKEN)
        with patch.object(douyin_search.requests, "post", side_effect=error), self.assertRaisesRegex(douyin_search.DouyinSearchError, "超时") as raised:
            douyin_search.search("家具", app_config=CONFIG)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertNotIn("example.invalid", str(raised.exception))

    def test_http_errors_and_redirects_are_not_followed_or_parsed_as_results(self):
        for status in [302, 401, 403, 429, 500]:
            failed = response({"err_msg": TOKEN}, status=status)
            with self.subTest(status=status), patch.object(douyin_search.requests, "post", return_value=token_response()), patch.object(douyin_search.requests, "get", return_value=failed), self.assertRaises(douyin_search.DouyinSearchError) as raised:
                douyin_search.search("家具", app_config=CONFIG)
            self.assertNotIn(TOKEN, str(raised.exception))
            failed.json.assert_not_called()
            failed.close.assert_called_once()

    def test_failure_on_later_page_does_not_publish_partial_results(self):
        pages = [page([item(101)], has_more=True), response({"err_no": 28001018, "err_msg": "no permission"})]
        with patch.object(douyin_search.requests, "post", return_value=token_response()), patch.object(douyin_search.requests, "get", side_effect=pages), self.assertRaises(douyin_search.DouyinSearchError):
            douyin_search.search("家具", app_config=CONFIG)
        self.assertEqual([], store.list_records("douyin_results"))
        self.assertEqual([], store.list_records("references"))

    def test_official_search_accepts_background_jobs_progress_callback(self):
        with patch.object(douyin_search.requests, "post", return_value=token_response()), patch.object(douyin_search.requests, "get", return_value=page([item(101)])):
            ident = jobs.submit("抖音官方搜索", douyin_search.search, "家具", app_config=CONFIG)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = jobs.get_job(ident)
                if job["state"] in ("done", "failed"):
                    break
                time.sleep(0.02)
        self.assertEqual("done", job["state"], job["message"])
        self.assertEqual(100, job["progress"])
        self.assertEqual("101", job["result"][0]["item_id"])


if __name__ == "__main__":
    unittest.main()
