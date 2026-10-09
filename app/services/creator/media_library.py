"""Validated local images and videos explicitly uploaded by the user."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata

from PIL import Image, ImageOps

from . import extract, rendering, store

_KIND = "creator_materials"
_IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
_VIDEOS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def _folder():
    folder = store.data_root() / "material_library"
    folder.mkdir(parents=True, exist_ok=True)
    return folder.resolve()


def _text(value, label, limit):
    if not isinstance(value, str) or len(value) > limit or any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError(f"{label}请使用 {limit} 字以内的普通文字。")
    return value.strip()


def _tags(value):
    if isinstance(value, str):
        value = re.split(r"[\s,，;；]+", value.strip())
    if not isinstance(value, (list, tuple)) or len(value) > 20:
        raise ValueError("最多填写 20 个素材标签。")
    result = []
    for tag in value:
        tag = _text(tag, "素材标签", 40).lstrip("#")
        if tag and tag not in result:
            result.append(tag)
    return result


def _image_info(path, thumbnail):
    try:
        with Image.open(path) as image:
            width, height = image.size
            if image.format not in {"PNG", "JPEG", "WEBP"} or not 0 < width * height <= 40_000_000:
                raise ValueError("请上传 4000 万像素以内的 PNG、JPG 或 WebP 图片。")
            image.verify()
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((240, 160))
            image.save(thumbnail, "PNG")
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("图片无法完整读取，请重新上传。") from exc
    return {"kind": "image", "width": width, "height": height, "duration": 0.0}


def _video_thumbnail(path, thumbnail):
    command = [extract.ffmpeg_binary(), "-v", "error", "-nostdin", "-y", "-i", str(path), "-map", "0:v:0",
               "-frames:v", "1", "-vf", "scale=240:160:force_original_aspect_ratio=decrease", "-an", str(thumbnail)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=45,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("视频缩略图读取失败，请完成运行环境准备或重新上传。") from exc
    if result.returncode or not thumbnail.is_file():
        raise ValueError("视频画面无法完整读取，请重新上传。")
    try:
        with Image.open(thumbnail) as image:
            image.verify()
    except (OSError, SyntaxError) as exc:
        raise ValueError("视频缩略图无法读取，请重新上传。") from exc


def register_upload(upload, label="", tags=""):
    name = getattr(upload, "name", "")
    if not isinstance(name, str) or not name:
        raise ValueError("请选择自己的图片或视频文件。")
    name = Path(name).name
    suffix = Path(name).suffix.lower()
    if suffix not in _IMAGES | _VIDEOS:
        raise ValueError("支持 PNG、JPG、WebP 图片和 MP4、MOV、MKV、WebM、M4V 视频。")
    data = upload.getvalue()
    limit = 20 if suffix in _IMAGES else 500
    if not isinstance(data, bytes) or not 0 < len(data) <= limit * 1024 * 1024:
        raise ValueError(f"请选择不超过 {limit} MB 的有效素材。")
    label = _text(label or Path(name).stem, "素材名称", 160)
    tags = _tags(tags)
    digest = hashlib.sha256(data).hexdigest()
    ident = digest + suffix
    folder = _folder()
    destination = folder / ident
    thumbnail = folder / (ident + ".thumbnail.png")
    with tempfile.NamedTemporaryFile(dir=folder, prefix=".incoming-", suffix=suffix, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    temporary_thumbnail = temporary.with_name(temporary.name + ".thumbnail.png")
    try:
        if suffix in _IMAGES:
            info = _image_info(temporary, temporary_thumbnail)
        else:
            probe = rendering.probe_source(str(temporary))
            width, height, duration = probe.get("width", 0), probe.get("height", 0), probe.get("duration", 0)
            if not probe.get("has_video") or not 0 < width * height <= 40_000_000 or not math.isfinite(duration) or duration <= 0:
                raise ValueError("素材没有可用的视频画面，请重新上传。")
            _video_thumbnail(temporary, temporary_thumbnail)
            info = {"kind": "video", "width": width, "height": height, "duration": duration}
        os.replace(temporary, destination)
        os.replace(temporary_thumbnail, thumbnail)
        stat = destination.stat()
        return store.save_record(_KIND, ident, {
            "source": "upload", "path": str(destination), "thumbnail_path": str(thumbnail),
            "original_name": name, "label": label, "tags": tags, "sha256": digest,
            "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, **info,
        })
    finally:
        temporary.unlink(missing_ok=True)
        temporary_thumbnail.unlink(missing_ok=True)


def _owned(row):
    if not isinstance(row, dict) or row.get("source") != "upload" or row.get("kind") not in {"image", "video"}:
        return False
    folder = _folder()
    try:
        path = Path(row.get("path", "")).resolve()
        thumbnail = Path(row.get("thumbnail_path", "")).resolve()
        if not path.is_relative_to(folder) or not thumbnail.is_relative_to(folder) or not path.is_file() or not thumbnail.is_file():
            return False
        stat = path.stat()
        return stat.st_size == row.get("bytes") and stat.st_mtime_ns == row.get("mtime_ns")
    except (OSError, TypeError, ValueError):
        return False


def list_materials(query="", kind="all"):
    if kind not in {"all", "image", "video"}:
        raise ValueError("请选择图片、视频或全部素材。")
    terms = re.split(r"[\s,，]+", unicodedata.normalize("NFKC", str(query)).casefold().strip())
    result = []
    for row in store.list_records(_KIND):
        if not _owned(row) or kind != "all" and row["kind"] != kind:
            continue
        text = " ".join([row.get("label", ""), row.get("original_name", ""), *row.get("tags", [])])
        text = unicodedata.normalize("NFKC", text).casefold()
        if all(not term or term.lstrip("#") in text for term in terms):
            result.append(row)
    return result


def get_material(ident):
    row = store.get_record(_KIND, str(ident))
    return row if _owned(row) else None
