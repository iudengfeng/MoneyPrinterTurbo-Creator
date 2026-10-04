"""Manual step 05: prepare truthful titles, topics and covers for one finished video."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import html
from pathlib import Path
import re

import streamlit as st

from app.services.creator import extract, jobs, release_assets, rendering, store


_SOURCES = ["本次成片", "历史成片", "上传本地视频"]
_COVER_MODES = ["使用生成封面", "上传自己的封面", "暂不使用封面"]
_FIELDS = {"text": "", "title": "", "description": "", "hashtags": "", "cover_title": ""}


def _set_draft(field, value):
    key = "creator_release_" + field
    st.session_state[key] = value
    st.session_state[key + "_buffer"] = value


def _remember(key):
    st.session_state[key + "_buffer"] = st.session_state.get(key, "")


def _draft(field, label, *, area=False, busy=False, **kwargs):
    key = "creator_release_" + field
    st.session_state.setdefault(key + "_buffer", st.session_state.get(key, _FIELDS[field]))
    st.session_state[key] = st.session_state[key + "_buffer"]
    widget = st.text_area if area else st.text_input
    return widget(label, key=key, disabled=busy, on_change=_remember, args=(key,), persist_state="session", **kwargs)


def _source_identity(path):
    return str(Path(path).expanduser().resolve()) if path else ""


def _clear_source_drafts(row, text=""):
    for field in _FIELDS:
        _set_draft(field, text if field == "text" else row.get("title", "") if field in {"title", "cover_title"} else "")
    for key in ("creator_release_cover_result", "creator_release_metadata_result", "creator_release_pending", "creator_release_saved",
                "creator_release_cover_import", "creator_release_cover_upload", "creator_release_notice"):
        st.session_state.pop(key, None)
    st.session_state["creator_release_cover_mode"] = "使用生成封面"
    st.session_state["creator_release_cover_mode_buffer"] = "使用生成封面"
    st.session_state["creator_release_frame_time"] = 0.0
    st.session_state["creator_release_frame_time_buffer"] = 0.0
    aspect = row.get("aspect")
    if aspect in {"9:16", "16:9", "1:1"}:
        st.session_state["creator_release_aspect"] = aspect
        st.session_state["creator_release_aspect_buffer"] = aspect


def load_video(row, text=""):
    """Explicit step-four handoff. Never borrow narration from another video."""
    path = row.get("video_path", "")
    st.session_state["creator_release_video"] = path
    st.session_state["creator_release_current"] = dict(row)
    st.session_state["creator_release_source"] = "本次成片"
    st.session_state["creator_release_source_buffer"] = "本次成片"
    st.session_state["creator_release_source_fingerprint"] = _source_identity(path)
    st.session_state.pop("creator_release_history_id", None)
    st.session_state.pop("creator_release_history_id_buffer", None)
    for key in ("creator_release_local_video_import", "creator_release_local_video_upload"):
        st.session_state.pop(key, None)
    _clear_source_drafts(row, text)


def _upload_changed(key):
    if st.session_state.get(key + "_upload") is None:
        st.session_state.pop(key + "_import", None)


def _upload(label, types, key, *, busy, image=False):
    uploaded = st.file_uploader(label, type=types, key=key + "_upload", disabled=busy, on_change=_upload_changed, args=(key,))
    if uploaded:
        try:
            limit = 20 if image else 500
            if not 0 < uploaded.size <= limit * 1024 * 1024:
                raise ValueError(f"请选择不超过 {limit} MB 的文件。")
            content = uploaded.getvalue()
            if image:
                from io import BytesIO
                from PIL import Image
                with Image.open(BytesIO(content)) as picture:
                    if picture.format not in {"JPEG", "PNG", "WEBP"} or picture.width * picture.height > 40_000_000:
                        raise ValueError("请选择 4000 万像素以内的 PNG、JPG 或 WebP 图片。")
                    picture.verify()
            folder = store.data_root() / "release_uploads"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (hashlib.sha256(content).hexdigest() + Path(uploaded.name).suffix.lower())
            if not path.is_file():
                path.write_bytes(content)
            st.session_state[key + "_import"] = {"path": str(path), "name": uploaded.name}
        except Exception as exc:
            st.error(str(exc))
            return None
    saved = st.session_state.get(key + "_import")
    if saved and Path(saved.get("path", "")).is_file():
        st.caption("已保存素材：" + saved.get("name", Path(saved["path"]).name))
        return saved["path"]
    return None


def _probe(path):
    source = Path(path)
    if not source.is_file():
        raise ValueError("视频已移动或删除，请重新选择。")
    stat = source.stat()
    identity = (str(source.resolve()), stat.st_size, stat.st_mtime_ns)
    cache = st.session_state.setdefault("creator_release_probe_cache", {})
    if identity not in cache:
        cache[identity] = rendering.probe_source(str(source))
        if len(cache) > 20:
            cache.pop(next(iter(cache)))
    info = cache[identity]
    if not info.get("has_video"):
        raise ValueError("这份文件没有有效的视频画面，请重新选择。")
    return info


def _material_input(busy):
    current_path = st.session_state.get("creator_release_video", "")
    history = rendering.list_renders()
    initial = "本次成片" if current_path else "历史成片" if history else "上传本地视频"
    st.session_state.setdefault("creator_release_source_buffer", st.session_state.get("creator_release_source", initial))
    st.session_state["creator_release_source"] = st.session_state["creator_release_source_buffer"]
    source = st.radio("成片来源", _SOURCES, key="creator_release_source", horizontal=True, disabled=busy,
                      on_change=_remember, args=("creator_release_source",), persist_state="session")
    selected = None
    if source == "本次成片":
        if current_path:
            current = st.session_state.get("creator_release_current", {})
            selected = dict(current if isinstance(current, dict) else {}, video_path=current_path)
        else:
            st.info("还没有送到这里的成片。可以去第四步完成剪辑，或上传本地视频。")
    elif source == "历史成片":
        by_id = {row["id"]: row for row in history}
        st.session_state.setdefault("creator_release_history_id_buffer", st.session_state.get("creator_release_history_id"))
        st.session_state["creator_release_history_id"] = st.session_state["creator_release_history_id_buffer"]
        if st.session_state.get("creator_release_history_id") not in by_id:
            st.session_state["creator_release_history_id"] = None
            st.session_state["creator_release_history_id_buffer"] = None
        if by_id:
            ident = st.selectbox("选择历史成片", list(by_id), index=None, placeholder="选择一份已完成的成片",
                                 format_func=lambda value: (by_id[value].get("title") or "模板成片") + " · " + by_id[value].get("aspect", "9:16"),
                                 key="creator_release_history_id", disabled=busy, on_change=_remember,
                                 args=("creator_release_history_id",), persist_state="session")
            selected = by_id.get(ident)
        else:
            st.info("还没有已完成的剪辑作品，也可以上传本地视频。")
    else:
        path = _upload("上传成片", ["mp4", "mov", "mkv", "webm"], "creator_release_local_video", busy=busy)
        st.caption("支持最长 30 分钟、最大 500 MB 的视频。")
        if path:
            selected = {"video_path": path}
    if not selected:
        if st.session_state.get("creator_release_source_fingerprint"):
            st.session_state["creator_release_source_fingerprint"] = ""
            _clear_source_drafts({})
        return None, None
    try:
        info = _probe(selected["video_path"])
    except Exception as exc:
        st.warning(str(exc))
        return None, None
    identity = _source_identity(selected["video_path"])
    previous = st.session_state.get("creator_release_source_fingerprint")
    if previous != identity:
        st.session_state["creator_release_source_fingerprint"] = identity
        if previous:
            _clear_source_drafts(selected)
        else:
            # An explicit first-visit handoff may already include confirmed text.
            for field in ("title", "cover_title"):
                if not st.session_state.get("creator_release_" + field + "_buffer", st.session_state.get("creator_release_" + field)):
                    _set_draft(field, selected.get("title", ""))
            if selected.get("aspect") in {"9:16", "16:9", "1:1"}:
                st.session_state["creator_release_aspect"] = selected["aspect"]
                st.session_state["creator_release_aspect_buffer"] = selected["aspect"]
    st.caption(f'已选成片：{selected.get("title") or Path(selected["video_path"]).name} · {info.get("duration", 0):.1f} 秒')
    return selected, info


def _config_snapshot():
    from app.config import config
    return deepcopy(config.snapshot_config_with_pending(config.app))


def _enqueue(kind, video_path, operation, *args, **kwargs):
    try:
        labels = {"metadata": "发布素材 · 生成标题与话题", "extract": "发布素材 · 提取成片文案", "cover": "发布素材 · 生成封面"}
        ident = jobs.submit(labels[kind], operation, *args, **kwargs)
        st.session_state["creator_release_pending"] = {"id": ident, "kind": kind, "source_video_path": video_path}
        st.session_state["creator_last_job"] = ident
    except Exception as exc:
        st.error(str(exc))


def _collect_result():
    pending = st.session_state.get("creator_release_pending")
    if not pending:
        return
    row = jobs.get_job(pending["id"])
    if not row or row["state"] in {"running", "queued"}:
        return
    st.session_state.pop("creator_release_pending", None)
    if _source_identity(pending["source_video_path"]) != st.session_state.get("creator_release_source_fingerprint", ""):
        return
    result = row.get("result")
    if row["state"] != "done" or not isinstance(result, dict):
        st.session_state["creator_release_notice"] = ("error", row.get("message") or "没有完成，请检查素材或模型设置后重试。")
        return
    if pending["kind"] == "extract":
        _set_draft("text", result.get("text", ""))
        message = "已提取这份成片的口播文案，请检查识别文字。"
    elif pending["kind"] == "metadata":
        st.session_state["creator_release_metadata_result"] = result
        titles = result.get("titles", [])
        _set_draft("title", titles[0] if titles else "")
        _set_draft("description", result.get("description", ""))
        _set_draft("hashtags", " ".join("#" + str(tag).lstrip("#") for tag in result.get("hashtags", [])))
        _set_draft("cover_title", result.get("cover_title", titles[0] if titles else ""))
        message = "标题、正文和话题已生成，可以选一个标题再自行修改。"
    else:
        st.session_state["creator_release_cover_result"] = result
        message = "封面已生成，请检查文字和画面。"
    st.session_state["creator_release_notice"] = ("success", message)


@st.fragment(run_every="2s")
def _progress():
    pending = st.session_state.get("creator_release_pending")
    if not pending:
        return
    row = jobs.get_job(pending["id"])
    if row and row["state"] not in {"running", "queued"}:
        st.rerun(scope="app")
    if row:
        st.info(row.get("message") or "正在准备发布素材。")
        st.progress(float(row.get("progress", 0)) / 100)


def _select_title(title):
    _set_draft("title", title)


def _style_picker(busy):
    styles = release_assets.list_styles()
    by_id = {item["id"]: item for item in styles}
    if st.session_state.get("creator_release_style") not in by_id:
        st.session_state["creator_release_style"] = next(iter(by_id), "clean")
    with st.container(key="creator_release_style_gallery"):
        for column, style in zip(st.columns(len(styles), gap="small"), styles):
            ident = style["id"]
            selected = st.session_state["creator_release_style"] == ident
            with column:
                name = html.escape(style["name"])
                st.markdown(f'<div class="release-cover-card release-cover-{html.escape(ident)} {"is-selected" if selected else ""}"><div class="release-cover-figure"><span class="release-cover-person"></span><span class="release-cover-kicker">发布素材</span><strong>{name}</strong><span class="release-cover-footer">把重点放在封面上</span></div></div>', unsafe_allow_html=True)
                if st.button(("✓ " if selected else "") + style["name"], key="creator_release_style_" + ident, width="stretch", type="primary" if selected else "secondary", disabled=busy):
                    st.session_state["creator_release_style"] = ident
                    st.rerun()
    with st.expander("全部风格"):
        st.caption("目前提供以下 4 种可生成的原创样式。上方为风格示意，生成后右侧显示实际封面。")
        for style in styles:
            st.markdown("**" + style["name"] + "** · " + style.get("description", ""))
    return st.session_state["creator_release_style"]


def _persistent_choice(key, label, values, *, busy, default=None, radio=False, **kwargs):
    st.session_state.setdefault(key + "_buffer", st.session_state.get(key, default or values[0]))
    st.session_state[key] = st.session_state[key + "_buffer"]
    widget = st.radio if radio else st.selectbox
    return widget(label, values, key=key, disabled=busy, on_change=_remember, args=(key,), persist_state="session", **kwargs)


def _cover_matches(row, request):
    return bool(row and Path(row.get("cover_path", "")).is_file()
                and _source_identity(row.get("source_video_path")) == _source_identity(request["video_path"])
                and row.get("title", "") == request["title"]
                and row.get("style") == request["style"] and row.get("aspect") == request["aspect"]
                and abs(float(row.get("frame_time", 0)) - request["frame_time"]) < 0.001)


def _hashtags(text):
    return [part.lstrip("#") for part in re.split(r"[\s,，;；]+", text.strip()) if part.lstrip("#")]


def _use_materials(row):
    """Hand off these exact files and values, including intentional empty fields."""
    from webui.creator_publish_workspace import load_materials
    load_materials(row)
    st.session_state["creator_selected_tab"] = "发布中心"
    st.session_state["creator_navigation_pending"] = True


def _save(video_path, title, description, hashtags, cover_path, source_text, publish=False):
    try:
        row = release_assets.save_materials(video_path, title, description=description, hashtags=hashtags,
                                           cover_path=cover_path, source_text=source_text)
        st.session_state["creator_release_saved"] = row
        if publish:
            _use_materials(row)
        else:
            st.session_state["creator_release_notice"] = ("success", "发布素材已保存，可以在历史记录中继续使用。")
    except Exception as exc:
        st.session_state["creator_release_notice"] = ("error", str(exc))


def _save_current_for_publish(video_path):
    # The callback runs before the shared navigation radio is instantiated.
    # Read current widget values here: arguments captured by the previous page
    # render would miss a text edit or an intentional clearing in this event.
    try:
        state = st.session_state
        mode = state.get("creator_release_cover_mode", "使用生成封面")
        cover_path = None
        if mode == "使用生成封面":
            cover = state.get("creator_release_cover_result")
            request = {"video_path": video_path, "title": state.get("creator_release_cover_title", ""),
                       "style": state.get("creator_release_style", "clean"), "aspect": state.get("creator_release_aspect", "9:16"),
                       "frame_time": float(state.get("creator_release_frame_time", 0))}
            if not _cover_matches(cover, request):
                raise ValueError("封面设置已改变，请重新生成封面后继续。")
            cover_path = cover["cover_path"]
        elif mode == "上传自己的封面":
            cover_path = state.get("creator_release_cover_import", {}).get("path")
            if not cover_path:
                raise ValueError("请先上传封面图片。")
        _save(video_path, state.get("creator_release_title", ""), state.get("creator_release_description", ""),
              _hashtags(state.get("creator_release_hashtags", "")), cover_path, state.get("creator_release_text", ""), publish=True)
    except Exception as exc:
        st.session_state["creator_release_notice"] = ("error", str(exc))


def _load_materials(row):
    load_video({"video_path": row["video_path"], "title": row.get("title", "")}, text=row.get("source_text", ""))
    for field in ("title", "description"):
        _set_draft(field, row.get(field, ""))
    _set_draft("hashtags", " ".join("#" + tag.lstrip("#") for tag in row.get("hashtags", [])))
    cover = row.get("cover_path")
    if cover and Path(cover).is_file():
        st.session_state["creator_release_cover_import"] = {"path": cover, "name": "历史素材封面"}
        st.session_state["creator_release_cover_mode"] = "上传自己的封面"
        st.session_state["creator_release_cover_mode_buffer"] = "上传自己的封面"


def render():
    _collect_result()
    st.markdown('<div class="script-eyebrow">STEP 05 · 发布素材</div>', unsafe_allow_html=True)
    st.markdown('<div class="release-brief"><h2>发布素材，标题与封面。</h2><p>围绕这份成片准备标题、话题与封面，检查后带入发布中心。</p></div>', unsafe_allow_html=True)
    notice = st.session_state.pop("creator_release_notice", None)
    if notice:
        getattr(st, notice[0])(notice[1])
    _progress()
    busy = bool(st.session_state.get("creator_release_pending"))
    with st.container(key="creator_release_grid"):
        left, right = st.columns([2.2, 1], gap="large")
    with left:
        with st.container(border=True, key="creator_release_materials"):
            st.markdown("**视频素材**")
            material, info = _material_input(busy)
        video_path = (material or {}).get("video_path", "")
        with st.container(border=True, key="creator_release_metadata"):
            st.markdown("**标题与话题**")
            with st.expander("这份成片的口播文案", expanded=not st.session_state.get("creator_release_text_buffer", st.session_state.get("creator_release_text", ""))):
                text = _draft("text", "口播文案", area=True, height=160, max_chars=6000, busy=busy,
                              placeholder="填写或核对这份成片的真实口播文案，用它生成标题和话题。")
                can_extract = bool(material and (info or {}).get("has_audio"))
                if st.button("从成片提取文案", key="creator_release_extract", width="stretch", disabled=busy or not can_extract):
                    _enqueue("extract", video_path, extract.extract_media, video_path)
                    st.rerun()
                if material and not (info or {}).get("has_audio"):
                    st.caption("成片没有音轨，可以直接填写口播文案。")
            c1, c2 = st.columns(2)
            generate = c1.button("生成标题", key="creator_release_generate_metadata", type="primary", width="stretch", disabled=busy or len(text.strip()) < 20)
            regenerate = c2.button("重新生成", key="creator_release_regenerate_metadata", width="stretch", disabled=busy or len(text.strip()) < 20)
            if generate or regenerate:
                _enqueue("metadata", video_path, release_assets.generate_metadata, text, count=3, app_config=_config_snapshot())
                st.rerun()
            generated = st.session_state.get("creator_release_metadata_result", {})
            if generated.get("titles"):
                st.caption("标题候选 · 点击选用，再自行修改")
                for index, candidate in enumerate(generated["titles"][:3]):
                    st.button(candidate, key="creator_release_candidate_" + str(index), width="stretch", disabled=busy,
                              on_click=_select_title, args=(candidate,))
            title = _draft("title", "发布标题", max_chars=120, busy=busy, placeholder="一句话说清这条视频的重点")
            description = _draft("description", "发布正文", area=True, height=100, max_chars=2000, busy=busy)
            tags_text = _draft("hashtags", "话题标签", busy=busy, max_chars=300, placeholder="#知识分享 #创作记录（用空格分隔）")
            if not text.strip():
                st.caption("填写真实口播文案后可以自动生成，也可以直接手工填写标题和正文。")
            elif len(text.strip()) < 20:
                st.caption("自动生成需要至少 20 个字符的口播文案，也可以直接手工填写。")
        with st.container(border=True, key="creator_release_cover_settings"):
            st.markdown("**封面风格**")
            style = _style_picker(busy)
            mode = _persistent_choice("creator_release_cover_mode", "封面来源", _COVER_MODES, busy=busy, radio=True)
            cover_title = _draft("cover_title", "封面文字", max_chars=120, busy=busy or mode != "使用生成封面",
                                 placeholder="可以与发布标题不同，突出画面重点")
            aspect = _persistent_choice("creator_release_aspect", "封面画幅", ["9:16", "16:9", "1:1"], busy=busy or mode != "使用生成封面",
                                        format_func=lambda value: {"9:16": "竖屏 9:16", "16:9": "横屏 16:9", "1:1": "方形 1:1"}[value])
            maximum = max(0.0, round((info or {}).get("duration", 0) - 0.05, 3))
            st.session_state.setdefault("creator_release_frame_time_buffer", st.session_state.get("creator_release_frame_time", 0.0))
            value = min(maximum, max(0.0, float(st.session_state["creator_release_frame_time_buffer"])))
            st.session_state["creator_release_frame_time"] = value
            st.session_state["creator_release_frame_time_buffer"] = value
            if maximum > 0:
                frame_time = st.slider("封面取帧时间（秒）", min_value=0.0, max_value=maximum, step=0.01,
                                       key="creator_release_frame_time", disabled=busy or mode != "使用生成封面",
                                       on_change=_remember, args=("creator_release_frame_time",), persist_state="session")
            else:
                frame_time = 0.0
                st.caption("选择有效的视频后，可以指定封面截取哪一帧。")
            uploaded_cover = None
            if mode == "上传自己的封面":
                uploaded_cover = _upload("上传封面图片", ["png", "jpg", "jpeg", "webp"], "creator_release_cover", busy=busy, image=True)
                st.caption("使用这张图片作为最终封面，不再叠加模板文字。")
            request = {"video_path": video_path, "title": cover_title, "style": style, "aspect": aspect, "frame_time": frame_time}
            if st.button("生成封面", type="primary", key="creator_release_generate_cover", width="stretch", disabled=busy or not material or mode != "使用生成封面"):
                _enqueue("cover", video_path, release_assets.generate_cover, video_path, cover_title, style=style, aspect=aspect, frame_time=frame_time)
                st.rerun()
    with right:
        with st.container(border=True, key="creator_release_preview"):
            st.markdown("**成片与封面预览**")
            if material:
                st.video(video_path)
            else:
                st.markdown('<div class="release-empty-preview">先选择成片，<br>在这里检查画面。</div>', unsafe_allow_html=True)
            cover = st.session_state.get("creator_release_cover_result")
            matches = _cover_matches(cover, request)
            chosen_cover = None
            ready_cover = mode == "暂不使用封面"
            if mode == "使用生成封面":
                if cover and Path(cover.get("cover_path", "")).is_file():
                    st.image(cover["cover_path"], width="stretch")
                    if matches:
                        chosen_cover, ready_cover = cover["cover_path"], True
                        st.caption(f'已生成封面 · {cover.get("aspect", "9:16")} · {cover.get("width", 0)} × {cover.get("height", 0)}')
                    else:
                        st.warning("之前生成的封面，修改后请重新生成。")
                else:
                    st.caption("选择风格并生成封面，或上传自己的封面。")
            elif mode == "上传自己的封面":
                chosen_cover, ready_cover = uploaded_cover, bool(uploaded_cover)
                if uploaded_cover:
                    st.image(uploaded_cover, width="stretch")
            else:
                st.caption("这份发布素材不带封面，可以在发布中心继续添加。")
            if chosen_cover:
                path = Path(chosen_cover)
                st.download_button("下载封面", path.read_bytes(), file_name="发布封面" + path.suffix,
                                   key="creator_release_download_cover", width="stretch")
            ready = bool(material and title.strip() and ready_cover)
            kwargs = {"video_path": video_path, "title": title, "description": description, "hashtags": _hashtags(tags_text),
                      "cover_path": chosen_cover, "source_text": text}
            if st.button("保存发布素材", key="creator_release_save", width="stretch", disabled=busy or not ready):
                _save(**kwargs)
                st.rerun()
            st.button("下一步，发布中心 →", key="creator_release_publish", type="primary", width="stretch", disabled=busy or not ready,
                      on_click=_save_current_for_publish, args=(video_path,))
            if not title.strip():
                st.caption("填写发布标题后可以保存并继续。")
            elif not ready_cover:
                st.caption("请生成或选择封面，也可以选择暂不使用封面。")
    history = release_assets.list_materials()
    if history:
        with st.expander("我的发布素材 · 历史记录"):
            for row in history[:12]:
                with st.expander(row.get("title") or "发布素材"):
                    if row.get("cover_path") and Path(row["cover_path"]).is_file():
                        st.image(row["cover_path"], width=180)
                    st.caption(row.get("publish_description") or "没有发布正文和话题。")
                    first, second = st.columns(2)
                    first.button("恢复编辑", key="creator_release_restore_" + row["id"], width="stretch", disabled=busy,
                                 on_click=_load_materials, args=(row,))
                    second.button("带入发布中心 →", key="creator_release_history_publish_" + row["id"], width="stretch", disabled=busy,
                                  on_click=_use_materials, args=(row,))
