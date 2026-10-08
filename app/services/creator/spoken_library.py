"""Source-only spoken reference structure; no model, account, or media access.

The hook is the first source sentence, not a measured three-second recording.
Bullet points and visual clues are excerpts, so downstream rewriting can keep
facts separate from inspiration. Public descriptions are not ASR transcripts.
"""
from __future__ import annotations

import copy
import json
import math
import re
from html import unescape
from pathlib import Path

_PROFILE_IDS = ("spoken_general", "catering_spoken", "beauty_spoken",
                "education_spoken", "local_service_spoken")
_PROFILE_ROOT = Path(__file__).resolve().parents[3] / "resource" / "creator"
_EXCLUDED_FORMATS = ("纯图片轮播", "带货挂车无口播", "无口播展示", "BGM展示", "纯BGM", "BGM种草",
                     "招商加盟", "纯特效展示", "纯菜品展示", "BGM刷锅", "纯效果对比图", "纯展示视频",
                     "招生硬广", "纯机构宣传", "纯商家宣传")
_SPOKEN_FORMATS = ("口播", "探店", "测评", "教程", "避坑", "攻略", "经验分享", "本地生活讲解")
_CLUES = (
    ("商品/项目", r"菜品|招牌菜|套餐|单品|产品|商品|课程|体验课|项目|美甲|美睫|护肤|服务项目|软件|工具"),
    ("环境", r"门头|门店|店内|大厅|吧台|包间|人流|排队|环境|教室|校区|地址|上门范围"),
    ("信息卡", r"价格|人均|\d+(?:\.\d+)?\s*元|价格表|菜单|团购|评价截图|有效期|注意事项|年龄段|步骤|参数|配置"),
    ("过程", r"制作过程|服务过程|操作步骤|操作界面|项目过程|到店体验|课程画面|演示|点击|制作|先.{0,40}再|第一步|第二步"),
    ("对比", r"对比|区别|前后|门市价|团购价|避坑|性价比|相比|优缺点"),
)
_EXTRA_PATTERNS = {
    "price": r"(?:人均|价格|套餐价|单次|费用|学费|售价|门市价|团购价)?\s*\d+(?:\.\d+)?\s*(?:元|块钱)|[¥￥]\s*\d+(?:\.\d+)?",
    "location": r"(?:地址|校区地址|门店地址|地点|上门范围)\s*[:：]\s*[^。！？!?\n]{1,100}",
    "duration": r"(?:活动时间|有效期|服务时长|项目时长|时长)\s*[:：]\s*[^。！？!?\n]{1,100}|\d+(?:\.\d+)?\s*(?:分钟|小时)",
    "package": r"(?:套餐|招牌菜)\s*[:：]\s*[^。！？!?\n]{1,100}",
    "project": r"(?:项目|服务项目)\s*[:：]\s*[^。！？!?\n]{1,100}",
    "course": r"(?:课程|体验课|试听课)\s*[:：]\s*[^。！？!?\n]{1,100}",
    "age_group": r"\d+\s*[-—～至到]\s*\d+\s*岁|\d+\s*岁|(?:年龄段|适合年龄)\s*[:：]\s*[^。！？!?\n]{1,100}",
    "effect": r"(?:效果|前后对比)\s*[:：]\s*[^。！？!?\n]{1,100}",
    "service_scope": r"(?:服务范围|上门范围)\s*[:：]\s*[^。！？!?\n]{1,100}",
}


def get_profile(id="spoken_general") -> dict:
    """Read one bundled profile; never accept a filesystem path as its ID."""
    if id not in _PROFILE_IDS:
        raise ValueError("请选择通用、餐饮、美业、教培或本地服务口播模板。")
    return copy.deepcopy(json.loads((_PROFILE_ROOT / f"industry_{id}.json").read_text(encoding="utf-8")))


def list_profiles() -> list[dict]:
    return [{"industry_id": name, "industry_name": get_profile(name)["industry_name"]} for name in _PROFILE_IDS]


def _text(value, limit=12000):
    if not isinstance(value, str):
        return ""
    value = unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    value = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines())
    return re.sub(r"\n{3,}", "\n\n", value).strip()[:limit]


def _sentences(text):
    return [match.group(0).strip() for match in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text) if match.group(0).strip()]


def content_format(text, title="", explicit=""):
    if isinstance(explicit, str) and explicit.strip() and explicit not in {"unknown", "未知", "未识别"}:
        return explicit.strip()[:40]
    data = f"{title}\n{text}"
    for label in _EXCLUDED_FORMATS:
        if label in data and not re.search(r"(?:不是|并非|避免|不要|拒绝|不做|非)\s*" + re.escape(label), data):
            return label
    for label in _SPOKEN_FORMATS:
        if label in data:
            return label
    return "未知"


def structure_text(text, title="", industry_id="spoken_general", tags=None) -> dict:
    """Extract literal source excerpts. Safe for short, user-provided scripts."""
    profile = get_profile(industry_id)
    full = _text(text)
    sentences = _sentences(full)
    # Long lines can contain multiple semicolon-separated factual points.
    points = []
    for sentence in sentences:
        clauses = re.split(r"[；;]", sentence) if len(sentence) > 180 else [sentence]
        for clause in clauses:
            clause = clause.strip()
            if clause and clause not in points:
                points.append(clause[:300])
        if len(points) >= 10:
            break
    clues = []
    for category, pattern in _CLUES:
        for sentence in sentences:
            if re.search(pattern, sentence):
                # The description itself is a source substring, not a generated scene.
                source = sentence[:220]
                clue_type = "菜品/商品" if category == "商品/项目" and industry_id == "catering_spoken" else category
                clue = {"type": clue_type, "description": source, "source_text": source}
                if clue not in clues:
                    clues.append(clue)
                if sum(row["type"] == clue_type for row in clues) >= 3:
                    break
    literal_tags = re.findall(r"[#＃]([^\s#＃，。！？!?；;：:]{1,40})", full)
    supplied = tags if isinstance(tags, list) else []
    literal_tags += [str(tag).strip().lstrip("#＃") for tag in supplied if isinstance(tag, str) and tag.strip()]
    extras = {}
    for key in profile["extract_fields"]["industry_extra_fields"]:
        pattern = _EXTRA_PATTERNS.get(key)
        if pattern:
            matches = list(dict.fromkeys(match.group(0).strip() for match in re.finditer(pattern, full)))
            if matches:
                extras[key] = "；".join(matches[:4])
    return {"industry_id": industry_id, "industry_name": profile["industry_name"],
            "hook_3s": (sentences[0] if sentences else "")[:160],
            "full_content": full, "spoken_script": full, "bullet_points": points[:10],
            "material_clues": clues[:15], "tags": list(dict.fromkeys(literal_tags))[:20],
            "industry_extra_fields": extras, "content_format": content_format(full, title),
            "extraction_method": "source_rules_v1"}


def filter_item(item, settings=None) -> dict:
    """Admit unknown formats but reject explicitly unsuitable formats/short text."""
    settings = settings or {}
    profile_id = settings.get("spoken_profile") or item.get("industry_id") or "spoken_general"
    profile = get_profile(profile_id)
    rule = profile["scraping_rule"]
    text = _text(item.get("full_content") or item.get("spoken_script") or item.get("public_caption") or "")
    form = content_format(text, item.get("title", ""), item.get("content_format", ""))
    reasons = []
    threshold = settings.get("min_text_length", rule["min_text_length"])
    if len(re.sub(r"\s+", "", text)) < threshold:
        reasons.append(f"正文不足 {threshold} 字")
    if form in set(_EXCLUDED_FORMATS) | set(rule.get("exclude_formats", [])):
        reasons.append(f"内容形式不适合口播：{form}")
    data = f"{item.get('title', '')}\n{text}"
    keywords = profile["keyword_pool"]
    for word in keywords.get("exclude", []):
        if word and word in data:
            reasons.append(f"匹配排除词：{word}")
    required = keywords.get("must_include", [])
    if required and not any(word in data for word in required):
        reasons.append("未匹配所选行业的口播关键词")
    return {"eligible": not reasons, "reasons": reasons, "content_format": form}


def rank_item(item, prefer_high_comment=True):
    """Known counts precede unknowns; visible excerpts never become a count."""
    metrics = item.get("hot_metrics") if isinstance(item.get("hot_metrics"), dict) else {}
    def value(key, fallback):
        number = metrics.get(key, item.get(fallback))
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
            return -1
        return number
    comments = value("comment", "comment_count")
    likes = value("like", "likes")
    collected = value("collect", "collect_count")
    return (comments, likes, collected) if prefer_high_comment else (likes, collected, comments)
