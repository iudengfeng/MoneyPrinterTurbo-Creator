"""Validated local video-processing options shared by UI and durable works."""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path

POSITIONS = {"top-left", "top-center", "top-right", "middle-left", "center", "middle-right",
             "bottom-left", "bottom-center", "bottom-right"}
DEFAULTS = {
    "pip_items": [], "highlight_keywords": {}, "output_resolution": "720P",
    "silence_trim": False, "silence_threshold": -40.0, "silence_min_duration": 0.7,
    "green_screen": False, "green_background_path": "", "green_color": "#00ff00",
    "green_similarity": 0.12, "green_blend": 0.03, "beauty_strength": 0.0,
    "cover_title": "", "cover_aspect": "", "cover_frame_fraction": 0.0,
}
RENDER_FIELDS = set(DEFAULTS) - {"cover_title", "cover_aspect", "cover_frame_fraction"}
RELEASE_FIELDS = {"cover_title", "cover_aspect", "cover_frame_fraction"}


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}必须在 {low}～{high} 之间。")
    return float(value)


def normalize(values):
    result = copy.deepcopy(DEFAULTS)
    result.update({key: copy.deepcopy(values[key]) for key in DEFAULTS if key in values})
    if result["output_resolution"] not in {"576P", "720P", "1080P"}:
        raise ValueError("请选择 576P、720P 或 1080P 导出分辨率。")
    for key in ("silence_trim", "green_screen"):
        if not isinstance(result[key], bool):
            raise ValueError("处理开关必须为布尔值。")
    for key, low, high in (("silence_threshold", -70, -15), ("silence_min_duration", .2, 3),
                           ("green_similarity", .01, 1), ("green_blend", 0, 1),
                           ("beauty_strength", 0, .5), ("cover_frame_fraction", 0, 1)):
        result[key] = number(result[key], key, low, high)
    color = result["green_color"]
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("绿幕颜色必须是六位颜色值。")
    background = result["green_background_path"]
    if not isinstance(background, str):
        raise ValueError("背景必须是本机文件路径。")
    result["green_background_path"] = str(Path(background).expanduser().resolve()) if background else ""
    if not isinstance(result["cover_title"], str) or len(result["cover_title"]) > 120:
        raise ValueError("封面标题最多 120 字。")
    if result["cover_aspect"] not in {"", "9:16", "16:9", "1:1"}:
        raise ValueError("封面比例无效。")
    groups = result["highlight_keywords"]
    if not isinstance(groups, dict) or set(groups) - {"main", "description", "action", "emotion"}:
        raise ValueError("关键词分组无效。")
    for terms in groups.values():
        if not isinstance(terms, list) or len(terms) > 12 or any(not isinstance(term, str) or not 1 <= len(term.strip()) <= 40 for term in terms):
            raise ValueError("每组最多 12 个关键词，每个词最多 40 字。")
    result["highlight_keywords"] = {group: list(dict.fromkeys(term.strip() for term in terms)) for group, terms in groups.items() if terms}
    items = result["pip_items"]
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("画中画最多支持 32 段素材。")
    normalized = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
            raise ValueError("画中画缺少素材路径。")
        start = number(item.get("start", 0), "素材开始时间", 0, 1800)
        end = number(item.get("end", 0), "素材结束时间", 0, 1800)
        if end <= start or item.get("mode", "window") not in {"window", "full"} or item.get("position", "top-right") not in POSITIONS:
            raise ValueError("画中画时间、模式或位置无效。")
        normalized.append({"path": str(Path(item["path"]).expanduser().resolve()), "start": start, "end": end,
                           "size": number(item.get("size", .34), "窗口大小", .15, .6),
                           "padding": number(item.get("padding", .02), "边距", 0, .2),
                           "position": item.get("position", "top-right"), "mode": item.get("mode", "window")})
    result["pip_items"] = normalized
    return result


def dimensions(aspect, resolution="720P"):
    if aspect not in {"9:16", "16:9", "1:1"} or resolution not in {"576P", "720P", "1080P"}:
        raise ValueError("画幅或导出分辨率无效。")
    edge = {"576P": 576, "720P": 720, "1080P": 1080}[resolution]
    long_edge = {576: 1024, 720: 1280, 1080: 1920}[edge]
    return (edge, long_edge) if aspect == "9:16" else (long_edge, edge) if aspect == "16:9" else (edge, edge)


def remap_pip(items, timeline):
    """Split overlay windows through the same edits used for voice and frames."""
    if not timeline:
        return copy.deepcopy(items)
    result = []
    for item in items:
        matches = []
        for interval in timeline:
            start = max(item["start"], interval["source_start"])
            end = min(item["end"], interval["source_end"])
            if end - start > .03:
                target = interval["target_start"] - interval["source_start"]
                mapped = dict(item, start=start+target, end=end+target)
                if matches and abs(matches[-1]["end"]-mapped["start"]) < .04:
                    matches[-1]["end"] = mapped["end"]
                else:
                    matches.append(mapped)
        result.extend(matches)
    return result
