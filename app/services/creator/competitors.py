"""Public competitor research, separate from publishing accounts and analytics.

Scrapling runs in an isolated subprocess with its own dependency directory. Only
public HTML/JSON is read: no publishing profiles, cookies, media downloads,
authentication, browser impersonation, or challenge solving are involved.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from . import spoken_library, store

_SETTINGS = "competitor_settings"
_RUNS = "competitor_runs"
_ITEMS = "competitor_items"
_OWNER = store.new_id()
_HANDLES = {}
_SCHEDULERS = {}
_LOCK = threading.RLock()
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="competitor-research")
_DEFAULTS = {"sources": [], "keywords": [], "city": "", "industry": "通用",
             "auto_update": False, "interval_hours": 24, "max_items": 30,
             "request_delay": 2.0, "auto_paused": False, "pause_reason": "",
             "spoken_profile": "spoken_general", "min_text_length": 80,
             "prefer_high_comment": True}
_MAX_BODY = 2 * 1024 * 1024
_MAX_REQUESTS = 30
_SELECTORS = {"item", "url", "title", "caption", "comments", "likes", "comment_count",
              "collect_count", "published_at", "author", "tags", "content_format"}


class CollectionError(RuntimeError):
    """A public-source failure; its message contains no response secrets."""


class NeedsUser(CollectionError):
    """Access requires a user action; unattended updates must pause."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _short(value, limit=600):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return re.sub(r"\s+", " ", unescape(str(value))).strip()[:limit]


def _url(value, *, resolve=False):
    if not isinstance(value, str) or len(value) > 2000 or re.search(r"[\x00-\x20]", value.strip()):
        raise ValueError("来源链接必须是长度不超过 2000 字的公开网页链接。")
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("来源仅支持无账户密码的 HTTP/HTTPS 公开网页链接。")
    host = parsed.hostname.lower()
    if re.search(r"\.(?:mp4|mov|mkv|webm|avi|mp3|wav|m4a|aac|flac|jpg|jpeg|png|zip|exe|pdf)$", parsed.path, re.I):
        raise ValueError("请填写公开网页分享链接，竞品采集不下载音视频或其他文件。")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost")):
        raise ValueError("竞品来源不能是本机或内网地址。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("竞品来源不能是本机或内网地址。")
    if resolve:
        try:
            addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except (OSError, ValueError) as exc:
            raise CollectionError("来源域名无法解析，请检查链接和网络。") from exc
        if any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise CollectionError("来源指向本机或内网，已停止采集。")
    if host.startswith(("creator.", "creatorcenter.", "ad.")) or host in {"creator.douyin.com", "creator.xiaohongshu.com"}:
        raise ValueError("这里只接收公开竞品来源，请勿填写自己的发布后台。")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def get_settings():
    saved = store.get_record(_SETTINGS, "default") or {}
    return {**_DEFAULTS, **saved}


def save_settings(changes):
    if not isinstance(changes, dict) or set(changes) - set(_DEFAULTS):
        raise ValueError("竞品采集设置包含未知字段。")
    current = get_settings()
    config = {**current, **changes}
    spoken_library.get_profile(config["spoken_profile"])
    if (isinstance(config["min_text_length"], bool) or not isinstance(config["min_text_length"], int)
            or not 0 <= config["min_text_length"] <= 12000):
        raise ValueError("口播正文长度门槛必须为 0～12000 字的整数。")
    if not isinstance(config["prefer_high_comment"], bool):
        raise ValueError("评论优先开关必须是布尔值。")
    for field in ("city", "industry"):
        if not isinstance(config[field], str) or len(config[field]) > 100:
            raise ValueError("城市与行业最多支持 100 字。")
        config[field] = config[field].strip()
    if config["city"] in {"不限", "城市不限"}:
        config["city"] = ""
    if not isinstance(config["keywords"], list) or len(config["keywords"]) > 20:
        raise ValueError("关键词最多支持 20 项。")
    if any(not isinstance(word, str) or not word.strip() or len(word) > 100 for word in config["keywords"]):
        raise ValueError("每个关键词应为 1～100 字的文本。")
    config["keywords"] = list(dict.fromkeys(word.strip() for word in config["keywords"]))
    if not isinstance(config["auto_update"], bool) or not isinstance(config["auto_paused"], bool):
        raise ValueError("自动更新开关必须是布尔值。")
    for field, low, high in (("interval_hours", 1, 168), ("max_items", 1, 100)):
        if isinstance(config[field], bool) or not isinstance(config[field], int) or not low <= config[field] <= high:
            raise ValueError(f"{field} 必须为 {low}～{high} 的整数。")
    delay = config["request_delay"]
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 2 <= delay <= 30:
        raise ValueError("公开页面请求间隔必须在 2～30 秒之间。")
    if not isinstance(config["sources"], list) or len(config["sources"]) > 30:
        raise ValueError("公开来源最多支持 30 项。")
    sources = []
    for row in config["sources"]:
        row = {"url": row} if isinstance(row, str) else row
        if not isinstance(row, dict) or set(row) - {"url", "keyword", "city", "industry", "selectors"}:
            raise ValueError("每个来源需要公开链接，可选关键词、城市、行业与选择器。")
        url = row.get("url", "")
        if not isinstance(url, str):
            raise ValueError("来源链接必须是文本。")
        if re.search(r"\{(?!keyword\}|city\}|industry\})", url):
            raise ValueError("来源模板只支持 {keyword}、{city}、{industry}。")
        _url(url.format(keyword="示例", city="示例", industry="示例"))
        source = {"url": url}
        for field in ("keyword", "city", "industry"):
            value = row.get(field, "")
            if not isinstance(value, str) or len(value) > 100:
                raise ValueError("来源标签最多支持 100 字。")
            source[field] = value.strip()
        selectors = row.get("selectors") or {}
        if not isinstance(selectors, dict) or set(selectors) - _SELECTORS or any(not isinstance(value, str) or len(value) > 300 for value in selectors.values()):
            raise ValueError("公开网页选择器格式不正确。")
        source["selectors"] = selectors
        if source not in sources:
            sources.append(source)
    config["sources"] = sources
    # Changing sources explicitly allows their new public addresses to be tried.
    if config["sources"] != current["sources"] or (changes.get("auto_update") and not current["auto_update"]):
        config.update(auto_paused=False, pause_reason="", next_run_at="")
    if not sources:
        config["next_run_at"] = ""
    saved = store.save_record(_SETTINGS, "default", config)
    if any(config[field] != current[field] for field in ("spoken_profile", "min_text_length")):
        _restructure_saved_items(config)
    return saved


def dependency_root():
    return Path(os.environ.get("MPT_SCRAPLING_SITE") or Path(__file__).resolve().parents[3] / "storage" / "creator" / "vendor" / "scrapling").resolve()


def dependency_ready():
    return (dependency_root() / "scrapling" / "__init__.py").is_file()


def _fetch_worker():
    """Called only by an isolated child Python, never the Streamlit interpreter."""
    from scrapling import Selector
    from scrapling.fetchers import Fetcher

    request = json.load(sys.stdin)
    chunks = []
    received = 0
    def retain(chunk):
        nonlocal received
        received += len(chunk)
        if received > _MAX_BODY:
            raise ValueError("Public page exceeds 2MB")
        chunks.append(chunk)
        return len(chunk)
    page = Fetcher.get(request["url"], timeout=18, retries=1, follow_redirects=False,
                       stealthy_headers=False, impersonate=None,
                       headers={"User-Agent": "MoneyPrinterTurbo-Creator/PublicResearch", "Accept": "text/html,application/json"},
                       content_callback=retain, discard_cookies=True)
    headers = {str(key).lower(): str(value) for key, value in page.headers.items()}
    body = b"".join(chunks)
    if len(body) > _MAX_BODY:
        print(json.dumps({"status": 413, "error": "公开页面超过 2MB，已停止处理。"}, ensure_ascii=False))
        return
    selected = []
    selectors = request.get("selectors", {})
    if selectors and page.status == 200:
        document = Selector(body, url=page.url)
        scopes = document.css(selectors["item"]) if selectors.get("item") else [document]
        for scope in list(scopes)[:100]:
            row = {}
            for field, selector in selectors.items():
                if field == "item" or not selector:
                    continue
                values = []
                for node in scope.css(selector):
                    value = node.get_all_text() if hasattr(node, "get_all_text") else str(node)
                    value = spoken_library._text(value) if field == "caption" else _short(value, 600)
                    if value:
                        values.append(value)
                row[field] = values[:8] if field in {"comments", "tags"} else (values[0] if values else "")
            selected.append(row)
    charset = re.search(r"charset=[\"']?([\w-]+)", headers.get("content-type", ""), re.I)
    try:
        html = body.decode(charset[1] if charset else "utf-8", errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")
    print(json.dumps({"status": page.status, "url": page.url, "headers": {key: headers.get(key, "") for key in ("content-type", "location")},
                      "html": html, "selected_rows": selected}, ensure_ascii=False))


def _request_public(url, selectors=None):
    target = dependency_root()
    if not dependency_ready():
        raise NeedsUser("公开采集引擎未安装，请安装独立 Scrapling 依赖后重试。")
    script = "import sys;sys.path[:0]=sys.argv[1:3];from app.services.creator.competitors import _fetch_worker;_fetch_worker()"
    command = [sys.executable, "-X", "utf8", "-I", "-c", script, str(target), str(Path(__file__).resolve().parents[3])]
    # Do not propagate API credentials, proxy configuration or a publisher profile.
    env = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC"}}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        process = subprocess.run(command, input=json.dumps({"url": url, "selectors": selectors or {}}), text=True,
                                 encoding="utf-8", errors="replace", capture_output=True, timeout=25, env=env,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectionError("公开页面请求超时或采集进程无法启动，请检查网络。") from exc
    if process.returncode:
        # Upstream tracebacks can contain URLs and environment details; keep them private.
        raise CollectionError("Scrapling 无法读取公开页面，请检查来源是否可公开访问与依赖是否完整。")
    try:
        result = json.loads(process.stdout)
    except (ValueError, TypeError) as exc:
        raise CollectionError("公开页面未返回有效采集结果。") from exc
    return result


def _fetch_public(url, selectors=None):
    for _ in range(6):
        url = _url(url, resolve=True)
        result = _request_public(url, selectors)
        status = result.get("status", 0)
        if status in {301, 302, 303, 307, 308}:
            location = result.get("headers", {}).get("location")
            if not location:
                raise CollectionError("公开页面跳转缺少目标地址。")
            url = urljoin(url, location)
            continue
        if status in {401, 403, 407, 429}:
            raise NeedsUser(f"来源返回 HTTP {status}，可能需要登录、验证或已限流。自动采集已暂停，请打开公开页面核查。")
        if status != 200:
            raise CollectionError(f"公开页面返回 HTTP {status}，未导入参考内容。")
        content_type = result.get("headers", {}).get("content-type", "").lower()
        if content_type and not any(kind in content_type for kind in ("html", "json", "text/plain")):
            raise CollectionError("链接返回音视频或其他文件；竞品采集只读取公开网页，不下载素材。")
        result["url"] = url
        _check_barrier(result.get("html", ""), url)
        return result
    raise CollectionError("公开来源跳转过多，已停止采集。")


def _check_barrier(html, url):
    lower = html[:_MAX_BODY].lower()
    page_path = urlsplit(url).path.lower()
    if re.search(r"/(?:login|signin|passport|captcha)(?:/|$)", page_path):
        raise NeedsUser("来源跳转到了登录或验证页面。请更换无需登录的公开来源。")
    # Recognize access gates, not an ordinary article mentioning a captcha.
    gate_title = re.search(r"<title[^>]*>(.*?)</title>", lower, re.S)
    title = re.sub(r"<[^>]+>", "", gate_title[1]).strip() if gate_title else ""
    if any(word in title for word in ("访问验证", "安全验证", "人机验证", "captcha", "just a moment", "access denied", "sign in", "登录")):
        raise NeedsUser("公开来源需要登录或安全验证，自动采集已暂停；不会自动绕过验证。")
    if any(marker in lower for marker in ("cf-chl-widget", "cf-chl-platform", 'id="captcha_container"', 'id="verify-bar-code"', "请完成验证后继续访问", "请登录后查看完整内容")):
        raise NeedsUser("公开来源需要登录或安全验证，自动采集已暂停；不会自动绕过验证。")


class _Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.scripts = []
        self.title = ""
        self.h1 = ""
        self.article = ""
        self._stack = []
        self._script = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "article" in self._stack and tag in {"p", "div", "li", "br", "h2", "h3"} and len(self.article) < 12000:
            self.article += "\n"
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
            if key and attrs.get("content"):
                self.meta.setdefault(key.lower(), attrs["content"])
        if tag == "link" and attrs.get("rel") == "canonical":
            self.meta["canonical"] = attrs.get("href", "")
        if tag == "script":
            self._script = [attrs, ""]
        if tag not in {"meta", "link", "br", "hr", "img", "input", "source", "wbr", "area", "embed", "base", "param", "track"}:
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if "article" in self._stack and tag in {"p", "div", "li", "h2", "h3"} and len(self.article) < 12000:
            self.article += "\n"
        if tag == "script" and self._script is not None:
            self.scripts.append(self._script)
            self._script = None
        if tag in self._stack:
            index = len(self._stack) - 1 - self._stack[::-1].index(tag)
            self._stack = self._stack[:index]

    def handle_data(self, text):
        if self._script is not None:
            self._script[1] += text
        elif "style" not in self._stack:
            if "title" in self._stack:
                self.title += text
            if "h1" in self._stack:
                self.h1 += text
            if "article" in self._stack and len(self.article) < 12000:
                self.article += text


def _json_values(document, raw):
    values = []
    if raw.lstrip().startswith(("{", "[")):
        try:
            values.append(json.loads(raw))
        except ValueError:
            pass
    for attrs, text in document.scripts:
        if attrs.get("type", "").lower() in {"application/ld+json", "application/json"} or attrs.get("id") in {"RENDER_DATA", "__NEXT_DATA__", "__NUXT_DATA__"}:
            candidate = unquote(text.strip()) if attrs.get("id") == "RENDER_DATA" else text.strip()
        else:
            match = re.search(r"(?:window\.)?__(?:INITIAL_STATE|NEXT_DATA)__\s*=\s*([\s\S]+?)(?:;\s*$|$)", text.strip())
            candidate = match[1] if match else ""
        if candidate:
            try:
                # XHS embeds JS's undefined in otherwise valid JSON. No eval is used.
                candidate = re.sub(r"(?<=:)\s*undefined(?=\s*[,}])", "null", candidate)
                values.append(json.loads(candidate))
            except ValueError:
                continue
    return values


def _walk(value, depth=0):
    if depth > 25:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:1000]:
            yield from _walk(child, depth + 1)


def _platform(url):
    host = (urlsplit(url).hostname or "").lower()
    if host == "douyin.com" or host.endswith(".douyin.com") or host.endswith(".iesdouyin.com"):
        return "douyin"
    if host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com") or host == "xhslink.com":
        return "xiaohongshu"
    return "web"


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return int(value)
    value = str(value).strip().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([千万亿kKwW]?)\+?", value)
    if not match:
        return None
    multiplier = {"千": 1000, "万": 10000, "w": 10000, "k": 1000, "亿": 100000000}.get(match[2].lower(), 1)
    return int(float(match[1]) * multiplier)


def _date(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    text = _short(value, 100)
    return text if re.match(r"^\d{4}-\d{2}-\d{2}(?:[T ]|$)", text) else None


def _comments(value):
    rows = value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])
    result = []
    for row in rows[:50]:
        if isinstance(row, str):
            text, author = _short(row, 240), ""
        elif isinstance(row, dict):
            text = _short(row.get("text") or row.get("content") or row.get("comment"), 240)
            user = row.get("user") or row.get("author") or {}
            author = _short(user.get("nickname") or user.get("name"), 80) if isinstance(user, dict) else _short(user, 80)
        else:
            continue
        if text and text not in {item["text"] for item in result}:
            result.append({"text": text, "author": author})
        if len(result) >= 8:
            break
    return result


def _tags(value):
    rows = value if isinstance(value, list) else re.split(r"[,，]", value) if isinstance(value, str) else []
    result = []
    for row in rows[:50]:
        if isinstance(row, dict):
            row = row.get("hashtag_name") or row.get("name") or row.get("title") or ""
        tag = _short(row, 40).lstrip("#＃")
        if tag and tag not in result:
            result.append(tag)
    return result[:20]


def _item(row, url, source, *, platform=None):
    platform = platform or _platform(url)
    title = _short(row.get("title"), 200)
    caption = _short(row.get("caption"), 600)
    settings = get_settings()
    profile_id = source.get("spoken_profile") or settings["spoken_profile"]
    structured = spoken_library.structure_text(row.get("caption", ""), title, profile_id, _tags(row.get("tags")))
    if row.get("content_format"):
        structured["content_format"] = spoken_library.content_format(structured["full_content"], title, row["content_format"])
    if not title and not caption:
        return None
    source_url = row.get("url") if isinstance(row.get("url"), str) and row.get("url") else url
    try:
        source_url = _url(urljoin(url, source_url))
    except ValueError:
        source_url = url
    source_id = _short(row.get("source_id"), 200)
    if not source_id:
        match = re.search(r"/(?:video|note|explore|discovery/item)/(\w+)", urlsplit(source_url).path)
        source_id = match[1] if match else hashlib.sha256(source_url.encode()).hexdigest()[:32]
    comments = _comments(row.get("comments"))
    likes = _number(row.get("likes"))
    comment_count = _number(row.get("comment_count"))
    collect_count = _number(row.get("collect_count"))
    published = _date(row.get("published_at"))
    missing = [key for key, value in (("title", title), ("public_caption", caption), ("comments", comments),
                                     ("likes", likes), ("comment_count", comment_count), ("collect_count", collect_count),
                                     ("published_at", published)) if value is None or value == "" or value == []]
    item = {"id": hashlib.sha256(f"{platform}:{source_id}".encode()).hexdigest()[:32],
            "title": title, "public_caption": caption, "comments": comments, "source_url": source_url,
            "platform": platform, "source_id": source_id, "published_at": published,
            "fetched_at": _now(), "likes": likes, "author": _short(row.get("author"), 100),
            "comment_count": comment_count, "collect_count": collect_count,
            "hot_metrics": {"like": likes, "comment": comment_count, "collect": collect_count},
            "keyword": source.get("keyword", ""), "city": source.get("city", ""),
            "industry": source.get("industry", "通用"), "missing_fields": missing,
            "caption_is_excerpt": True, "source_label": "公开口播参考（原文规则拆解，非视频转写）",
            "spoken_profile": profile_id, "content_origin": row.get("content_origin", "public_description"),
            **structured}
    eligibility = spoken_library.filter_item(item, {**settings, "spoken_profile": profile_id})
    item.update(eligible=eligibility["eligible"], filter_reasons=eligibility["reasons"], content_format=eligibility["content_format"])
    return item


def parse_public_page(page, source):
    """Extract actual public fields; unknown transcript/count/comments stay absent."""
    raw, url = page.get("html", ""), page["url"]
    _check_barrier(raw, url)
    document = _Document(raw)
    platform = _platform(url)
    rows = list(page.get("selected_rows") or [])
    for payload in _json_values(document, raw):
        for node in _walk(payload):
            if platform == "douyin" and node.get("aweme_id") and ("desc" in node or "video" in node):
                author = node.get("author") if isinstance(node.get("author"), dict) else {}
                stats = node.get("statistics") if isinstance(node.get("statistics"), dict) else {}
                rows.append({"source_id": str(node["aweme_id"]), "title": node.get("title") or node.get("desc", ""),
                             "caption": node.get("desc", ""), "url": f"https://www.douyin.com/video/{node['aweme_id']}",
                             "comments": node.get("comments") or node.get("comment_list"), "likes": stats.get("digg_count"),
                             "comment_count": stats.get("comment_count"), "collect_count": stats.get("collect_count"),
                             "tags": node.get("text_extra"),
                             "author": author.get("nickname", ""), "published_at": node.get("create_time")})
            elif platform == "xiaohongshu" and (node.get("noteId") or node.get("note_id")) and ("desc" in node or "title" in node):
                ident = node.get("noteId") or node.get("note_id")
                user = node.get("user") if isinstance(node.get("user"), dict) else {}
                stats = node.get("interactInfo") or node.get("interact_info") or {}
                stats = stats if isinstance(stats, dict) else {}
                rows.append({"source_id": str(ident), "title": node.get("title", ""), "caption": node.get("desc", ""),
                             "url": f"https://www.xiaohongshu.com/explore/{ident}", "likes": stats.get("likedCount", stats.get("liked_count")),
                             "comment_count": stats.get("commentCount", stats.get("comment_count")),
                             "collect_count": stats.get("collectedCount", stats.get("collected_count")),
                             "tags": node.get("tagList") or node.get("tag_list"),
                             "comments": node.get("comments") or node.get("comment_list"), "author": user.get("nickname", ""),
                             "published_at": node.get("time") or node.get("publishTime")})
            elif any(kind in {"VideoObject", "Article", "NewsArticle", "BlogPosting", "SocialMediaPosting"}
                     for kind in (node.get("@type") if isinstance(node.get("@type"), list) else [node.get("@type")]) if isinstance(kind, str)):
                author = node.get("author") or {}
                likes, comment_count, collect_count = None, node.get("commentCount"), None
                stats = node.get("interactionStatistic") or []
                for statistic in stats if isinstance(stats, list) else [stats]:
                    if isinstance(statistic, dict):
                        action = str(statistic.get("interactionType", ""))
                        if "LikeAction" in action:
                            likes = statistic.get("userInteractionCount")
                        elif "CommentAction" in action:
                            comment_count = statistic.get("userInteractionCount")
                        elif "BookmarkAction" in action or "SaveAction" in action:
                            collect_count = statistic.get("userInteractionCount")
                body = node.get("transcript") or node.get("articleBody") or node.get("description", "")
                rows.append({"title": node.get("headline") or node.get("name", ""), "caption": body,
                             "url": node.get("url") or node.get("mainEntityOfPage") if isinstance(node.get("mainEntityOfPage"), str) else node.get("url", ""),
                             "published_at": node.get("datePublished"), "comments": node.get("comment"), "likes": likes,
                             "comment_count": comment_count, "collect_count": collect_count,
                             "tags": node.get("keywords"),
                             "content_origin": "public_transcript" if node.get("transcript") else "public_article" if node.get("articleBody") else "public_description",
                             "author": author.get("name", "") if isinstance(author, dict) else author})
    if not rows:
        meta = document.meta
        rows.append({"title": meta.get("og:title") or meta.get("twitter:title") or document.h1 or document.title,
                     "caption": document.article or meta.get("og:description") or meta.get("description") or meta.get("twitter:description"),
                     "content_origin": "public_article" if document.article else "public_description",
                     "tags": meta.get("keywords", ""),
                     "url": meta.get("canonical") or meta.get("og:url") or url,
                     "author": meta.get("author", ""), "published_at": meta.get("article:published_time")})
    result = {}
    for row in rows[:200]:
        item = _item(row, url, source)
        if item:
            previous = result.get(item["id"])
            if not previous or len(item["full_content"]) > len(previous["full_content"]):
                result[item["id"]] = item
    if platform != "web" and (not result or all(not row["public_caption"] and not row["comments"] for row in result.values())):
        raise NeedsUser("该平台公开页仅返回页面外壳，未能读取竞品正文或评论。请更换公开分享链接或手工导入参考资料。")
    if platform != "web" and result and all(row["title"] in {"抖音", "抖音 - 记录美好生活", "小红书", "小红书 - 你的生活指南"}
                                            or ("亿人分享美好生活" in row["public_caption"] and not row["comments"]) for row in result.values()):
        raise NeedsUser("该平台只返回公共首页内容，未能读取具体竞品作品。请填写可公开查看的作品分享链接。")
    return sorted(result.values(), key=lambda item: spoken_library.rank_item(item, get_settings()["prefer_high_comment"]), reverse=True)


def _restructure_saved_items(settings):
    """Apply a new local filter/profile to captured text without fetching pages."""
    for row in store.list_records(_ITEMS):
        metrics = row.get("hot_metrics") if isinstance(row.get("hot_metrics"), dict) else {}
        # Older installations may have saved only a caption and source link.
        normalized = {"title": "", "public_caption": "", "comments": [], "author": "",
                      "keyword": "", "industry": "通用", "city": "", "platform": "web",
                      "source_url": row.get("url", ""), "published_at": None,
                      "fetched_at": "", "source_label": "公开口播参考（原文规则拆解，非视频转写）",
                      "likes": row.get("likes", metrics.get("like")),
                      "comment_count": row.get("comment_count", metrics.get("comment")),
                      "collect_count": row.get("collect_count", metrics.get("collect")), **row}
        normalized["full_content"] = row.get("full_content") or row.get("spoken_script") or row.get("public_caption") or ""
        normalized["spoken_profile"] = settings["spoken_profile"]
        normalized.setdefault("missing_fields", [field for field in ("title", "public_caption", "comments", "likes",
                             "comment_count", "collect_count", "published_at") if normalized.get(field) in (None, "", [])])
        _import_item(normalized, restructure_only=True)


def _import_item(item, *, restructure_only=False):
    old = store.get_record(_ITEMS, item["id"])
    merged = {**(old or {}), **item}
    field_times = dict((old or {}).get("field_fetched_at", {}))
    # A later access-limited page must not erase public fields captured earlier.
    for field in ("title", "public_caption", "full_content", "comments", "likes", "comment_count", "collect_count", "published_at", "author"):
        if old and item.get(field) in (None, "", []) and old.get(field) not in (None, "", []):
            merged[field] = old[field]
        elif not restructure_only and item.get(field) not in (None, "", []):
            field_times[field] = item["fetched_at"]
    merged["field_fetched_at"] = field_times
    merged["latest_missing_fields"] = ((old or {}).get("latest_missing_fields", item["missing_fields"])
                                     if restructure_only else item["missing_fields"])
    merged["missing_fields"] = [field for field in ("title", "public_caption", "comments", "likes", "comment_count", "collect_count", "published_at") if merged.get(field) in (None, "", [])]
    merged["first_fetched_at"] = (old or {}).get("first_fetched_at", item["fetched_at"])
    profile_id = merged.get("spoken_profile") or get_settings()["spoken_profile"]
    merged.update(spoken_library.structure_text(merged.get("full_content") or merged.get("public_caption", ""), merged.get("title", ""), profile_id, merged.get("tags")))
    # Re-evaluate against merged source text, retaining prior explicit format labels.
    format_label = item.get("content_format")
    if not item.get("full_content") and (not format_label or format_label == "未知"):
        format_label = (old or {}).get("content_format")
    merged["content_format"] = format_label or merged["content_format"]
    merged["hot_metrics"] = {"like": merged.get("likes"), "comment": merged.get("comment_count"), "collect": merged.get("collect_count")}
    eligibility = spoken_library.filter_item(merged, {**get_settings(), "spoken_profile": profile_id})
    merged.update(eligible=eligibility["eligible"], filter_reasons=eligibility["reasons"], content_format=eligibility["content_format"])
    saved = store.save_record(_ITEMS, item["id"], merged)
    reference_id = "competitor-" + item["id"]
    prior_reference = store.get_record("references", reference_id)
    if prior_reference and prior_reference.get("source") != "public_competitor":
        # Do not overwrite or remove a user-owned record, even if its ID collides.
        return False
    text = saved["full_content"]
    if text and saved["eligible"]:
        reference = {"title": saved["title"] or "公开口播参考", "text": text, "keyword": saved["keyword"],
                     "industry": saved["industry"], "city": saved["city"], "source_url": saved["source_url"], "author": saved["author"],
                     "source": "public_competitor", "source_label": saved["source_label"], "platform": saved["platform"],
                     "fetched_at": saved["fetched_at"], "missing_fields": saved["missing_fields"], "competitor_id": saved["id"],
                     "metrics": "；".join(f"{label}：{saved.get(field) if saved.get(field) is not None else '未取得'}"
                                        for label, field in (("点赞", "likes"), ("评论数", "comment_count"), ("收藏", "collect_count"))),
                     "comments": saved["comments"], "hot_metrics": saved["hot_metrics"], "eligible": True,
                     "spoken_profile": profile_id,
                     **{field: saved[field] for field in ("industry_id", "industry_name", "hook_3s", "full_content", "spoken_script",
                        "bullet_points", "material_clues", "tags", "industry_extra_fields", "content_format", "extraction_method")}}
        store.save_record("references", reference_id, reference)
        return prior_reference is None
    # Only this automatically collected reference can be removed by its filter;
    # original raw items and user-imported references remain available.
    if prior_reference:
        store.delete_record("references", reference_id)
    return False


def list_items(keyword=""):
    rows = store.list_records(_ITEMS)
    keyword = _short(keyword, 100).casefold()
    if keyword:
        rows = [row for row in rows if keyword in "\n".join(str(row.get(field, "")) for field in ("title", "public_caption", "keyword", "industry", "city")).casefold()]
    return sorted(rows, key=lambda item: spoken_library.rank_item(item, get_settings()["prefer_high_comment"]), reverse=True)


def _lock_file(handle, unlock=False):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _open_lock(name):
    folder = store.data_root() / "competitor_locks"
    folder.mkdir(exist_ok=True)
    handle = (folder / name).open("a+b")
    if handle.tell() == 0:
        handle.write(b"1")
        handle.flush()
    try:
        _lock_file(handle)
    except OSError:
        handle.close()
        return None
    return handle


def _owner_lock():
    with _LOCK:
        key = str(store.data_root())
        if key not in _HANDLES:
            handle = _open_lock(_OWNER + ".lock")
            if handle is None:
                raise CollectionError("采集进程锁无法建立。")
            _HANDLES[key] = handle
    return _OWNER


def _owner_alive(owner):
    if owner == _OWNER and str(store.data_root()) in _HANDLES:
        return True
    if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner):
        return False
    path = store.data_root() / "competitor_locks" / (owner + ".lock")
    if not path.is_file():
        return False
    try:
        with path.open("r+b") as handle:
            try:
                _lock_file(handle)
            except OSError:
                return True
            _lock_file(handle, unlock=True)
    except OSError:
        return True
    return False


def _recover():
    for row in store.list_records(_RUNS):
        if row.get("state") in {"queued", "running"} and not _owner_alive(row.get("owner")):
            store.update_record(_RUNS, row["id"], {"state": "interrupted", "message": "采集程序已重启；已保存参考保留，可重新更新。", "ended_at": _now()})


def list_runs():
    _recover()
    return store.list_records(_RUNS)


def get_run(ident):
    _recover()
    return store.get_record(_RUNS, ident)


def _claim(state):
    settings = get_settings()
    if not settings["sources"]:
        raise ValueError("请先添加公开竞品来源；只有关键词不会自动读取平台数据。")
    _recover()
    owner = _owner_lock()
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute("SELECT data FROM records WHERE kind=?", (_RUNS,)).fetchall()
        for saved, in active:
            row = json.loads(saved)
            if row.get("state") in {"running", "queued"}:
                return row, False
        ident = store.new_id()
        now = _now()
        row = {"id": ident, "state": state, "owner": owner, "created_at": now, "updated_at": now,
               "progress": 0, "message": "准备更新公开口播参考", "items": 0, "imported": 0,
               "filtered": 0, "filter_reasons": {}, "errors": []}
        conn.execute("INSERT INTO records VALUES(?,?,?,?)", (_RUNS, ident, json.dumps(row, ensure_ascii=False), now))
    store.update_record(_SETTINGS, "default", {"last_run_id": ident, "last_started_at": _now(),
                        "next_run_at": (datetime.now(timezone.utc) + timedelta(hours=settings["interval_hours"])).isoformat()})
    return row, True


def _expanded_sources(settings):
    seen = set()
    for source in settings["sources"]:
        keywords = [source["keyword"]] if source.get("keyword") else settings["keywords"] or [""]
        if "{keyword}" not in source["url"]:
            keywords = [source.get("keyword") or "、".join(settings["keywords"])]
        for keyword in keywords:
            labels = {"keyword": keyword, "city": source.get("city") or settings["city"], "industry": source.get("industry") or settings["industry"]}
            url = source["url"].format(**{key: quote(value, safe="") for key, value in labels.items()})
            if url not in seen:
                seen.add(url)
                yield {**source, **labels, "url": url, "spoken_profile": settings["spoken_profile"]}
            if len(seen) >= _MAX_REQUESTS:
                return


def _collect_claimed(ident, progress=None):
    handle = _open_lock("collection.lock")
    if handle is None:
        store.update_record(_RUNS, ident, {"state": "interrupted", "message": "另一进程正在更新参考库，请等待完成。", "ended_at": _now()})
        return {"status": "busy", "message": "另一进程正在采集。", "id": ident}
    total, imported, filtered, errors = 0, 0, 0, []
    filter_reasons = {}
    status = "done"
    def report(message, percent):
        store.update_record(_RUNS, ident, {"state": "running", "progress": percent, "message": message})
        if progress:
            progress(message, percent)
    try:
        settings = get_settings()
        sources = list(_expanded_sources(settings))
        report("正在读取已配置的公开竞品来源", 1)
        for index, source in enumerate(sources):
            if total >= settings["max_items"]:
                break
            if index:
                time.sleep(settings["request_delay"])
            report(f"正在读取公开来源 {index + 1}/{len(sources)}", int(index / len(sources) * 90))
            try:
                page = _fetch_public(source["url"], source.get("selectors"))
                items = parse_public_page(page, source)
                if not items:
                    errors.append({"source_url": source["url"], "message": "公开页未提供可用标题或摘要。"})
                for item in items[:settings["max_items"] - total]:
                    imported += int(_import_item(item))
                    saved = store.get_record(_ITEMS, item["id"])
                    if not saved["eligible"]:
                        filtered += 1
                        for reason in saved["filter_reasons"]:
                            filter_reasons[reason] = filter_reasons.get(reason, 0) + 1
                    total += 1
            except NeedsUser as exc:
                errors.append({"source_url": source["url"], "message": str(exc), "needs_user": True})
                status = "needs_user"
                store.update_record(_SETTINGS, "default", {"auto_paused": True, "pause_reason": str(exc)})
                break
            except (CollectionError, ValueError) as exc:
                errors.append({"source_url": source["url"], "message": str(exc)})
        if status != "needs_user" and total == 0:
            status = "failed"
        message = f"口播参考已更新：读取 {total} 条，新增入库 {imported} 条，过滤 {filtered} 条。"
        if status == "needs_user":
            message = "公开来源需要人工核查，自动更新已暂停；此前参考内容已保留。"
        elif status == "failed":
            message = "本次未获取公开竞品内容，请检查来源链接。"
        elif errors:
            message += f" {len(errors)} 个来源未能读取，见任务详情。"
        if status == "done":
            store.update_record(_SETTINGS, "default", {"auto_paused": False, "pause_reason": ""})
        result = {"id": ident, "status": status, "message": message, "items": total, "imported": imported,
                  "filtered": filtered, "filter_reasons": filter_reasons, "errors": errors}
        store.update_record(_RUNS, ident, {**result, "state": status, "progress": 100, "ended_at": _now()})
        store.update_record(_SETTINGS, "default", {"last_finished_at": _now()})
        if progress:
            progress(message, 100)
        return result
    except Exception:
        store.update_record(_RUNS, ident, {"state": "failed", "message": "采集未完成，已保存参考保留，请检查配置后重试。", "ended_at": _now()})
        raise
    finally:
        _lock_file(handle, unlock=True)
        handle.close()


def collect_once(progress=None):
    row, created = _claim("running")
    if not created:
        return {"status": "busy", "id": row["id"], "message": "公开参考库已有更新任务，请等待完成。"}
    return _collect_claimed(row["id"], progress)


def submit_collection():
    row, created = _claim("queued")
    if created:
        _EXECUTOR.submit(_collect_claimed, row["id"])
    return row["id"]


def _scheduler_due(settings):
    if not settings["auto_update"] or not settings["sources"] or settings.get("auto_paused"):
        return False
    if not settings.get("next_run_at"):
        return True
    try:
        return datetime.fromisoformat(settings["next_run_at"]) <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


def ensure_scheduler():
    """One scheduler per data directory; disabled/empty settings do no network work."""
    root = str(store.data_root())
    with _LOCK:
        if root in _SCHEDULERS:
            return True
        settings = get_settings()
        if not settings["auto_update"] or not settings["sources"]:
            return False
        handle = _open_lock("scheduler.lock")
        if handle is None:
            return False
        def loop():
            try:
                while str(store.data_root()) == root:
                    settings = get_settings()
                    if _scheduler_due(settings):
                        try:
                            submit_collection()
                        except Exception:
                            # A timer should not take down the UI. Retry at the next interval.
                            store.update_record(_SETTINGS, "default", {"next_run_at": (datetime.now(timezone.utc) + timedelta(hours=settings["interval_hours"])).isoformat()})
                    time.sleep(30)
            finally:
                _lock_file(handle, unlock=True)
                handle.close()
                with _LOCK:
                    _SCHEDULERS.pop(root, None)
        thread = threading.Thread(target=loop, name="competitor-auto-update", daemon=True)
        _SCHEDULERS[root] = thread
        thread.start()
    return True
