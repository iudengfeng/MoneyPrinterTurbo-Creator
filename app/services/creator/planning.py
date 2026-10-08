"""Deterministic shot planning from real ASR cues and local asset metadata.

Matching uses filenames and explicitly supplied labels, not image understanding.
No model call, network lookup or paid generation is performed by this module.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from pathlib import Path

_KINDS = {"knowledge", "montage", "product", "avatar"}
_ASPECTS = {"9:16", "16:9", "1:1"}
_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_VIDEOS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
_STOP = {
    "一个", "这个", "那个", "我们", "你们", "他们", "就是", "可以", "需要", "所以",
    "因为", "然后", "但是", "这样", "什么", "怎么", "很多", "时候", "已经", "一些",
    "一下", "来看", "今天", "大家", "这里", "其实", "视频", "the", "and", "for",
    "with", "this", "that", "from", "your", "have", "will", "about",
}


class PlanningError(ValueError):
    """A missing or invalid planning input that the user can correct."""


def _number(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanningError(f"{label}必须是有效秒数。") from exc
    if not math.isfinite(result):
        raise PlanningError(f"{label}必须是有效秒数。")
    return result


def _terms(text):
    """Extract literal English tokens and Chinese n-grams without a model."""
    result = []
    for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{1,}|[\u3400-\u9fff]+", str(text).lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            if len(token) <= 8 and token not in _STOP:
                result.append(token)
            for length in (4, 3, 2):
                for index in range(len(token) - length + 1):
                    part = token[index:index + length]
                    if part not in _STOP:
                        result.append(part)
        elif token not in _STOP:
            result.append(token)
    return list(dict.fromkeys(result))


def _materials(values):
    result, warnings, seen = [], [], set()
    for raw in values or []:
        row = dict(raw) if isinstance(raw, dict) else {"path": raw}
        value = row.get("path") or row.get("media_path") or row.get("file_path")
        if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
            warnings.append("忽略了没有本地文件路径的素材。")
            continue
        path = Path(value).expanduser().resolve()
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file() or not path.stat().st_size:
            warnings.append(f"忽略不存在或为空的素材：{path.name}")
            continue
        suffix = path.suffix.lower()
        if suffix not in _IMAGES | _VIDEOS:
            warnings.append(f"忽略不支持的素材格式：{path.name}")
            continue
        labels = []
        for field in ("title", "name", "description", "tags", "keywords"):
            label = row.get(field, "")
            if isinstance(label, (list, tuple, set)):
                label = " ".join(str(item) for item in label)
            labels.append(str(label))
        result.append({
            "path": str(path),
            "type": "image" if suffix in _IMAGES else "video",
            "labels": " ".join([path.stem, *labels]).lower(),
        })
    return result, warnings


def _literal(text):
    """Loose text comparison for cue association, never fact provenance."""
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", str(text).casefold())


def _provenance_text(text):
    """Ignore spacing/case only; decimal punctuation remains part of a fact."""
    return re.sub(r"\s+", "", str(text).casefold())


def _contained(text, source):
    value = _provenance_text(text)
    if not value:
        return False
    prefix = r"(?<![\d.])(?<!\d,)" if value[0].isdigit() or re.match(r"\.\d", value) else ""
    suffix = r"(?![\d.]|,\d)" if value[-1].isdigit() else ""
    return re.search(prefix + re.escape(value) + suffix, _provenance_text(source)) is not None


def _related(text, cue):
    left, right = _literal(text), _literal(cue)
    if min(len(left), len(right)) >= 4 and (left in right or right in left):
        return True
    overlap = set(_terms(text)) & set(_terms(cue))
    return sum(min(len(term), 6) for term in overlap) >= 4


def _safe_brief(script, supplied):
    """Keep only outline content grounded in the current spoken script.

    A competitor outline or stale draft may accidentally reach this boundary.
    Even when its source_text is real, a newly invented price/address in its
    description is rejected. Nonliteral visual hints must match the local
    rule-based extraction of this script, never an external model assertion.
    """
    if not isinstance(supplied, dict):
        return None
    from app.services.creator import spoken_library

    industry_id = str(supplied.get("industry_id") or "spoken_general")
    try:
        derived = spoken_library.structure_text(script, industry_id=industry_id)
    except ValueError:
        derived = spoken_library.structure_text(script)
    points = supplied.get("bullet_points", [])
    if not isinstance(points, list):
        points = []
    valid_points = [point.strip() for point in points if isinstance(point, str) and _contained(point, script)][:16]
    if not valid_points:
        valid_points = list(derived.get("bullet_points", []))[:16]
    canonical = [clue for clue in derived.get("material_clues", []) if isinstance(clue, dict)]
    clues = supplied.get("material_clues", [])
    if not isinstance(clues, list):
        clues = []
    valid_clues = []
    for raw in clues[:40]:
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description", "")).strip()
        source = str(raw.get("source_text", "")).strip()
        category = str(raw.get("type", "")).strip()
        if not description:
            continue
        matches = [clue for clue in canonical if _provenance_text(clue.get("description", "")) == _provenance_text(description)
                   and (not category or str(clue.get("type", "")) == category)]
        if source and not _contained(source, script):
            continue
        if not source:
            if matches:
                source = str(matches[0].get("source_text", ""))
            elif _contained(description, script):
                source = description
            else:
                continue
        if not _contained(source, script):
            continue
        if not _contained(description, source) and not any(
                _related(clue.get("source_text", ""), source) for clue in matches):
            continue
        clue = {"type": category, "description": description[:200], "source_text": source[:600]}
        if clue not in valid_clues:
            valid_clues.append(clue)
    # Include actual-script hints if an external/stale brief contained nothing
    # usable, so one incorrect reference cannot erase safe local extraction.
    if not valid_clues:
        valid_clues = canonical[:20]
    return {
        **derived,
        "full_content": script.strip(),
        "spoken_script": script.strip(),
        "bullet_points": valid_points,
        "material_clues": valid_clues,
    }


def _card_content(cue, points):
    """Use a real script information point, otherwise the real ASR utterance."""
    related = [point for point in points if _related(point, cue)]
    # Literal cue containment is strongest; prefer the shorter relevant point.
    related.sort(key=lambda point: (not (_contained(cue, point) or _contained(point, cue)), len(point)))
    text = related[0] if related else cue
    title = re.split(r"[，。！？；\n,.!?;]", text, maxsplit=1)[0].strip()[:24]
    return title or text[:24], text


def build_plan(script, entries, materials, *, kind="knowledge", aspect="9:16", duration=None, progress=None,
               video_brief=None):
    """Plan visuals while retaining the exact timestamps of real ASR entries.

    ``duration`` is optional but, when supplied, must be the actual audio duration.
    Visual ranges cover ASR pauses; subtitle/cue ``start`` and ``end`` are unchanged.
    Product/montage assets are used in a balanced rotation if labels do not match.
    Knowledge shots with no literal match use information cards instead.
    Optional ``video_brief`` supplies script-grounded information points and
    visual hints; it cannot add competitor facts or change the ASR timeline.
    """
    if kind not in _KINDS:
        raise PlanningError("请选择知识讲解、素材混剪、商品介绍或数字人口播。")
    if aspect not in _ASPECTS:
        raise PlanningError("画幅仅支持 9:16、16:9 和 1:1。")
    if not isinstance(script, str) or not script.strip():
        raise PlanningError("请先生成或填写口播文案。")
    if not isinstance(entries, (list, tuple)) or not entries:
        raise PlanningError("缺少真实语音时间轴，请先生成配音并识别字幕。")
    if len(entries) > 600:
        raise PlanningError("单次分镜最多支持 600 段字幕，请先缩短视频或合并字幕段落。")
    cues = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise PlanningError("字幕时间轴格式无效，请重新识别配音。")
        start = _number(raw.get("start"), "字幕开始时间")
        end = _number(raw.get("end"), "字幕结束时间")
        text = str(raw.get("text", "")).strip()
        if start < 0 or end <= start or not text:
            raise PlanningError("字幕必须包含文字，且结束时间应大于非负开始时间。")
        cues.append({"start": start, "end": end, "text": text})
    cues.sort(key=lambda row: (row["start"], row["end"]))
    for previous, current in zip(cues, cues[1:]):
        if current["start"] < previous["end"] - 0.001:
            raise PlanningError("口播字幕时间段重叠，请重新识别或修正字幕后生成。")
    cue_end = max(row["end"] for row in cues)
    total = _number(duration, "音轨时长") if duration is not None else cue_end
    if not 0 < total <= 1800:
        raise PlanningError("自动成片支持 30 分钟以内的有效音轨。")
    if cue_end > total + 0.15 or cues[-1]["start"] >= total:
        raise PlanningError("字幕超出实际配音时长，请重新识别当前配音，不能按文字拉长音轨。")
    assets, warnings = _materials(materials)
    brief = _safe_brief(script, video_brief)
    if kind in {"product", "montage"} and not assets:
        label = "商品介绍" if kind == "product" else "素材混剪"
        raise PlanningError(f"{label}需要上传至少一份真实图片或视频素材，不能用信息卡替代。")
    if progress:
        progress("根据真实语音时间轴规划画面", 0)
    usage, segments = Counter(), []
    for index, cue in enumerate(cues):
        terms = _terms(cue["text"])
        points = [point for point in (brief or {}).get("bullet_points", []) if _related(point, cue["text"])]
        clues = [clue for clue in (brief or {}).get("material_clues", [])
                 if _related(clue.get("source_text", ""), cue["text"])]
        extra_terms = _terms(" ".join([*points, *(clue["description"] for clue in clues)]))
        terms = list(dict.fromkeys([*terms, *extra_terms]))
        ranked = []
        for asset in assets:
            matches = [word for word in terms if word in asset["labels"]]
            score = sum(min(len(word), 8) for word in matches)
            ranked.append((score, -usage[asset["path"]], asset, matches))
        # Prefer not-yet-used assets at equal literal match scores.
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        chosen, matched = None, []
        if ranked and ranked[0][0] > 0:
            _, _, chosen, matched = ranked[0]
            reason = "文件名/素材标签匹配：" + "、".join(matched[:4])
            if clues:
                reason = "口播画面线索＋" + reason
        elif assets and kind in {"product", "montage"}:
            chosen = min(assets, key=lambda item: usage[item["path"]])
            reason = "文件名及标签没有相关词，按上传素材轮换；未进行画面语义识别。"
        elif kind == "avatar":
            reason = "使用已生成的数字人口播画面。"
        else:
            reason = "没有匹配的本地素材，使用本段口播的信息卡；未进行联网素材搜索。"
        count = usage[chosen["path"]] if chosen else 0
        if chosen:
            usage[chosen["path"]] += 1
            if count:
                reason += f" 此素材第 {count + 1} 次使用。"
        card_title, card_text = _card_content(cue["text"], points)
        segments.append({
            "id": f"shot-{index + 1:03d}",
            **cue,
            "visual_start": 0.0 if index == 0 else cue["start"],
            "visual_end": cues[index + 1]["start"] if index + 1 < len(cues) else total,
            "keywords": (matched + [term for term in terms if term not in matched])[:12],
            "media_path": chosen["path"] if chosen else None,
            "media_type": chosen["type"] if chosen else ("avatar" if kind == "avatar" else "card"),
            "match_reason": reason,
            "reuse_count": count,
            "card_title": card_title,
            "card_text": card_text,
            "material_clues": clues,
            "visual_clues": clues,
        })
        if progress:
            progress(f"已规划 {index + 1}/{len(cues)} 段画面", (index + 1) * 100 / len(cues))
    card_count = sum(row["media_type"] == "card" for row in segments)
    if card_count:
        warnings.append(f"{card_count} 段没有相关本地素材，使用信息卡。")
    if any("轮换" in row["match_reason"] for row in segments):
        warnings.append("部分画面按上传素材轮换，请在预览中检查相关性；这不是图像语义匹配。")
    return {
        "segments": segments,
        "shots": segments,
        "duration": total,
        "kind": kind,
        "aspect": aspect,
        "script": script.strip(),
        "warnings": warnings,
        "matching_method": "filename_metadata",
        "video_brief": brief,
    }
