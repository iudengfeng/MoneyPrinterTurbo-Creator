"""Reusable customer facts, kept separate from publishing/legacy accounts."""
from __future__ import annotations

import copy
import json
import re
import unicodedata
from datetime import datetime, timezone

from . import store

_KIND = "brand_profiles"
BUSINESS_TYPES = {"general": "通用", "local_store": "本地门店", "ecommerce": "电商商品",
                  "service": "专业服务", "knowledge": "知识内容"}
EXTRA_FIELDS = ("address", "opening_hours", "activity", "product_features", "price", "purchase_url",
                "service_area", "case_notes", "contact", "expertise", "content_style")
MAX_EXTRA_JSON = 12000
_TEXT_FIELDS = {"name": ("档案名称", 120), "industry": ("行业", 120),
                "offering": ("产品或服务", 4000), "audience": ("目标客户", 2000),
                "differentiators": ("特点与优势", 4000), "desired_action": ("期望客户行动", 1000)}
_INPUT_FIELDS = set(_TEXT_FIELDS) | {"business_type", "extras", "expected_version"}


class ProfileConflict(ValueError):
    """The editor is based on an older version of this same customer profile."""


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("客户档案标识无效。")
    return value


def _text(value, label, limit):
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是文本。")
    # Paragraphs are valid customer input; terminal/NUL/DEL controls are not.
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if any(unicodedata.category(char) == "Cc" and char != "\n" for char in value):
        raise ValueError(f"{label}包含不支持的控制字符。")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{label}最多支持 {limit} 字。")
    return value


def _validated(data, existing=None):
    existing = existing or {}
    result = {}
    for key, (label, limit) in _TEXT_FIELDS.items():
        result[key] = _text(data.get(key, existing.get(key, "")), label, limit)
    for key in ("offering", "audience"):
        if not result[key]:
            raise ValueError(f"请填写{_TEXT_FIELDS[key][0]}。")
    if not result["name"]:
        result["name"] = result["offering"].replace("\n", " ")[:20]
    business_type = data.get("business_type", existing.get("business_type", "general"))
    if not isinstance(business_type, str) or business_type not in BUSINESS_TYPES:
        raise ValueError("请选择通用、本地门店、电商商品、专业服务或知识内容。")
    result["business_type"] = business_type
    extras = data.get("extras", existing.get("extras", {}))
    if not isinstance(extras, dict) or set(extras) - set(EXTRA_FIELDS):
        raise ValueError("客户档案的补充资料包含未知字段。")
    result["extras"] = {key: _text(value, "补充资料", 6000 if key == "case_notes" else 3000)
                        for key, value in extras.items()}
    if len(json.dumps(result["extras"], ensure_ascii=False)) > MAX_EXTRA_JSON:
        raise ValueError("补充资料合计过长，请保留关键事实或缩短案例描述后保存。")
    return result


def get_profile(id):
    return copy.deepcopy(store.get_record(_KIND, _identifier(id)))


def list_profiles():
    return copy.deepcopy(store.list_records(_KIND))


def save_profile(data, id=None, *, expected_version=None):
    """Create or update one profile atomically; IDs and prior snapshots stay fixed.

Partial updates retain omitted fields; providing ``extras`` replaces that map.
Use the current version to detect concurrent edits. No sample facts are filled.
"""
    if not isinstance(data, dict) or set(data) - _INPUT_FIELDS:
        raise ValueError("客户档案包含未知字段。")
    supplied_version = data.get("expected_version")
    if supplied_version is not None:
        if expected_version is not None and expected_version != supplied_version:
            raise ValueError("客户档案版本号不一致。")
        expected_version = supplied_version
    if expected_version is not None and (isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1):
        raise ValueError("客户档案版本号必须为正整数。")
    if id is None and expected_version is not None:
        raise ValueError("新档案无需填写已有版本号。")
    ident = store.new_id() if id is None else _identifier(id)
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT data FROM records WHERE kind=? AND id=?", (_KIND, ident)).fetchone()
        existing = json.loads(row[0]) if row else None
        if id is not None and existing is None:
            raise ValueError("客户档案不存在，请重新选择。")
        version = (existing or {}).get("version", 0)
        if expected_version is not None and version != expected_version:
            raise ProfileConflict("这份客户档案已被更新，请重新打开后保存。")
        result = _validated(data, existing)
        now = datetime.now(timezone.utc).isoformat()
        result.update(id=ident, version=version + 1, created_at=(existing or {}).get("created_at", now), updated_at=now)
        connection.execute("INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated",
                           (_KIND, ident, json.dumps(result, ensure_ascii=False), now))
    return copy.deepcopy(result)


def snapshot(id):
    """Return a detached, validated copy for one work's explicit selection."""
    profile = get_profile(id)
    if profile is None:
        raise ValueError("客户档案不存在，请重新选择。")
    facts = _validated({key: profile[key] for key in _INPUT_FIELDS - {"expected_version"} if key in profile})
    facts.update({key: profile[key] for key in ("id", "version", "created_at", "updated_at") if key in profile})
    return copy.deepcopy(facts)
