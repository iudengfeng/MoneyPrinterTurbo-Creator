"""Bounded adapter for Douyin's official, permission-gated video search API.

No website crawling or undocumented endpoints are used. Tokens stay in memory;
results contain only fields actually supplied by the official response.
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

import requests

from app.services.creator import store


TOKEN_URL = "https://open.douyin.com/oauth/client_token/"
SEARCH_URL = "https://open.douyin.com/dy_open_api/v1/search/video/"
REQUEST_TIMEOUT = (10, 35)
PAGE_SIZE = 20
MAX_PAGES = 5
_SORT_TYPES = {"relevance": 0, "likes": 1, "latest": 2}
_token_cache = {}
_token_lock = threading.Lock()


class DouyinSearchError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _settings(app_config=None):
    if app_config is None:
        from app.config import config

        app_config = config.app
    settings = app_config.get("creator_douyin_search", {}) if isinstance(app_config, dict) else {}
    return settings if isinstance(settings, dict) else {}


def _credentials(app_config=None):
    settings = _settings(app_config)
    client_key = settings.get("client_key", "")
    client_secret = settings.get("client_secret", "")
    device_id = settings.get("device_id", "")
    if not isinstance(client_key, str) or not client_key.strip() or not isinstance(client_secret, str) or not client_secret.strip():
        raise ValueError("请配置抖音开放平台的 client_key、client_secret 和 device_id，并开通 aweme.dy.video_search 能力。")
    if isinstance(device_id, bool) or not str(device_id).strip().isdigit():
        raise ValueError("请填写有效的抖音开放平台 device_id（正整数）。")
    device_id = int(str(device_id).strip())
    if not 1 <= device_id <= 9223372036854775807:
        raise ValueError("抖音开放平台 device_id 必须为有效的 Int64 正整数。")
    return client_key.strip(), client_secret.strip(), device_id


def is_configured(app_config=None) -> bool:
    try:
        _credentials(app_config)
    except ValueError:
        return False
    return True


def _error(payload):
    layers = [payload]
    for _ in range(2):
        nested = layers[-1].get("data")
        if not isinstance(nested, dict):
            break
        layers.append(nested)
    for layer in layers:
        for field in ("err_no", "error_code"):
            if field not in layer:
                continue
            try:
                code = int(layer[field])
            except (ValueError, TypeError):
                raise DouyinSearchError("抖音官方接口返回了无效的错误码，请稍后重试。") from None
            if code == 0:
                continue
            if code in {28001014, 28001018, 28001019}:
                message = f"抖音官方搜索权限未开通或不可用（错误码 {code}）。请在开发者平台申请 aweme.dy.video_search 能力。"
            elif code in {10003, 10013}:
                message = f"抖音应用凭证无效（错误码 {code}）。请检查 client_key 和 client_secret。"
            elif code in {28001003, 28001008}:
                message = f"抖音接口授权凭证无效或已过期（错误码 {code}），请重试获取凭证。"
            elif code == 28001016:
                message = "抖音开放平台应用已被禁用，请到开发者平台检查应用状态。"
            elif code in {28003017, 10020}:
                message = f"抖音官方接口额度或调用频率受限（错误码 {code}），请稍后重试或检查平台额度。"
            else:
                message = f"抖音官方接口请求失败（错误码 {code}），请检查应用权限、参数或稍后重试。"
            # Provider descriptions may echo secrets, so they are never displayed.
            raise DouyinSearchError(message, code=code)


def _request_json(method, url, **kwargs):
    response = None
    try:
        response = method(url, timeout=REQUEST_TIMEOUT, allow_redirects=False, **kwargs)
        status = response.status_code
        if status in {401, 403}:
            raise DouyinSearchError("抖音官方接口拒绝访问，请检查应用凭证和 aweme.dy.video_search 权限。")
        if status == 429:
            raise DouyinSearchError("抖音官方接口调用过于频繁，请稍后重试。")
        if not 200 <= status < 300:
            raise DouyinSearchError(f"抖音官方接口连接失败（HTTP {status}），请稍后重试。")
        payload = response.json()
    except requests.RequestException:
        raise DouyinSearchError("抖音官方接口连接失败或超时，请检查网络后重试。") from None
    except (ValueError, TypeError):
        raise DouyinSearchError("抖音官方接口没有返回有效 JSON，请稍后重试。") from None
    finally:
        if response is not None:
            response.close()
    if not isinstance(payload, dict):
        raise DouyinSearchError("抖音官方接口响应格式无效，请稍后重试。")
    _error(payload)
    return payload


def _token(credentials):
    client_key, client_secret, _device_id = credentials
    fingerprint = hashlib.sha256((client_key + "\0" + client_secret).encode()).hexdigest()
    with _token_lock:
        cached = _token_cache.get(fingerprint)
        if cached and cached[1] > time.monotonic():
            return cached[0], fingerprint
        payload = _request_json(requests.post, TOKEN_URL, json={
            "client_key": client_key, "client_secret": client_secret, "grant_type": "client_credential",
        })
        data = payload.get("data", {})
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise DouyinSearchError("抖音开放平台未返回调用凭证，请检查应用配置。")
        try:
            lifetime = max(1, min(7200, int(data.get("expires_in", 7200))))
        except (ValueError, TypeError):
            lifetime = 7200
        _token_cache[fingerprint] = (token, time.monotonic() + max(0, lifetime - 60))
        return token, fingerprint


def _integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _source_url(value, item_id):
    # Numeric IDs have a canonical public URL, without tracking/authentication data.
    if item_id.isdigit():
        return "https://www.douyin.com/video/" + item_id
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (hostname == "douyin.com" or hostname.endswith(".douyin.com") or hostname == "iesdouyin.com" or hostname.endswith(".iesdouyin.com")):
            return ""
        return urlunsplit(("https", hostname, parsed.path, "", ""))
    except ValueError:
        return ""


def _page(payload):
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict) or not isinstance(data.get("video_list"), list):
        raise DouyinSearchError("抖音官方搜索没有返回有效的视频列表，请检查能力是否开通。")
    return data


def _persist_reference(ident, result):
    existing = store.get_record("references", ident)
    if existing:
        # A refreshed search must preserve text/title edited by the user.
        return store.update_record("references", ident, {
            "digg_count": result["digg_count"], "metrics": result["metrics"],
            "fetched_at": result["fetched_at"], "search_window_days": result["search_window_days"],
        })
    return store.save_record("references", ident, result)


def search(keyword, count=9, days=30, sort="likes", app_config=None, progress=None) -> list[dict]:
    if not isinstance(keyword, str) or not keyword.strip() or len(keyword.strip()) > 200:
        raise ValueError("请填写 1～200 字的抖音搜索关键词。")
    keyword = keyword.strip()
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
        raise ValueError("搜索数量必须为 1～20 的整数。")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 180:
        raise ValueError("时间范围必须为 1～180 天的整数。")
    if sort not in _SORT_TYPES:
        raise ValueError("搜索排序必须为 likes、latest 或 relevance。")
    credentials = _credentials(app_config)
    if progress:
        progress("正在连接抖音官方搜索", 5)
    token, fingerprint = _token(credentials)
    now = datetime.now(timezone.utc)
    minimum_time = int((now - timedelta(days=days)).timestamp())
    maximum_time = int(now.timestamp())
    fetched_at = now.isoformat()
    results, seen = [], set()
    cursor, search_id = 0, None
    scanned_count = skipped_missing_date = 0
    more_available = False
    for page_index in range(MAX_PAGES):
        params = {"keyword": keyword, "count": PAGE_SIZE, "device_id": credentials[2],
                  "cursor": cursor, "publish_time": 180, "sort_type": _SORT_TYPES[sort]}
        if search_id:
            params["search_id"] = search_id
        if progress:
            progress(f"正在读取抖音官方搜索第 {page_index + 1} 页", 10 + page_index * 15)
        try:
            payload = _request_json(requests.get, SEARCH_URL,
                                    headers={"access-token": token, "content-type": "application/json"}, params=params)
        except DouyinSearchError as exc:
            if exc.code in {28001003, 28001008}:
                with _token_lock:
                    _token_cache.pop(fingerprint, None)
            raise
        page = _page(payload)
        raw_items = page["video_list"][:PAGE_SIZE]
        scanned_count += len(raw_items)
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            published = _integer(item.get("create_time"))
            if published is None:
                skipped_missing_date += 1
                continue
            if not minimum_time <= published <= maximum_time:
                continue
            item_id = item.get("item_id")
            item_id = str(item_id).strip() if isinstance(item_id, (str, int)) and not isinstance(item_id, bool) else ""
            title = item.get("title")
            if not item_id or len(item_id) > 200 or not isinstance(title, str) or not title.strip():
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            text = item.get("high_quality_text")
            text = text.strip() if isinstance(text, str) else ""
            if len(text) > 30000:
                raise DouyinSearchError("抖音接口返回的正文超过导入限制，请改用链接或本地视频提取。")
            statistics = item.get("statistics")
            likes = _integer(statistics.get("digg_count")) if isinstance(statistics, dict) else None
            if likes is not None and likes < 0:
                likes = None
            identifier = "douyin_" + hashlib.sha256(item_id.encode()).hexdigest()
            results.append({
                "id": identifier, "item_id": item_id, "title": title.strip()[:300],
                "text": text, "keyword": keyword, "source": "douyin_official", "source_label": "抖音官方搜索",
                "source_url": _source_url(item.get("link"), item_id),
                "author": item.get("nickname", "").strip()[:200] if isinstance(item.get("nickname"), str) else "",
                "digg_count": likes, "metrics": f"点赞 {likes}" if likes is not None else "",
                "create_time": published, "published_at": datetime.fromtimestamp(published, timezone.utc).isoformat(),
                "fetched_at": fetched_at, "has_text": bool(text), "reference_id": identifier if text else None,
                "search_window_days": days, "search_sort": sort,
            })
        more_available = page.get("has_more") in (True, 1, "1", "true")
        if not more_available or page_index == MAX_PAGES - 1:
            break
        next_cursor = _integer(page.get("cursor"))
        if next_cursor is None or next_cursor == cursor:
            raise DouyinSearchError("抖音官方搜索分页游标无效，请稍后重新搜索。")
        if search_id is None:
            search_id = page.get("search_id")
            if not isinstance(search_id, str) or not search_id.strip():
                raise DouyinSearchError("抖音官方搜索未返回分页标识，请稍后重新搜索。")
        cursor = next_cursor
    if sort == "likes":
        results.sort(key=lambda row: (row["digg_count"] is not None, row["digg_count"] or 0, row["create_time"]), reverse=True)
    elif sort == "latest":
        results.sort(key=lambda row: row["create_time"], reverse=True)
    results = results[:count]
    for index, result in enumerate(results):
        result.update({"scanned_count": scanned_count, "skipped_missing_date": skipped_missing_date,
                       "scan_limit": PAGE_SIZE * MAX_PAGES, "more_available": more_available,
                       "coverage_label": "仅筛选官方搜索返回的前 100 条，不代表全平台排名"})
        if result["has_text"]:
            _persist_reference(result["reference_id"], result)
        existing = store.get_record("douyin_results", result["id"])
        if existing:
            result["created_at"] = existing.get("created_at", fetched_at)
        results[index] = store.save_record("douyin_results", result["id"], result)
    if progress:
        progress(f"已保存 {len(results)} 条抖音官方搜索结果", 100)
    return results
