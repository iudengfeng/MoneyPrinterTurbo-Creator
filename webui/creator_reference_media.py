"""Sentence-timed picture-in-picture editing using the customer's material library."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import streamlit as st

from app.services.creator import media_library, processing

POSITION_NAMES = {"top-left": "左上", "top-center": "上中", "top-right": "右上", "middle-left": "左中",
                  "center": "居中", "middle-right": "右中", "bottom-left": "左下", "bottom-center": "下中", "bottom-right": "右下"}


def timed_rows(ctx):
    voice = ctx.stage("voice")
    rows = voice.get("segments") or []
    return [row for row in rows if isinstance(row, dict) and row.get("text") and
            isinstance(row.get("start"), (int, float)) and isinstance(row.get("end"), (int, float)) and
            0 <= row["start"] < row["end"]]


def _library():
    st.caption("素材保存在本机，可复用；填写内容标签后更容易检索。")
    upload = st.file_uploader("上传自己的图片或视频", type=["png", "jpg", "jpeg", "webp", "mp4", "mov", "mkv", "webm"], key="ref_pip_library_upload")
    name, tags = st.columns(2)
    label = name.text_input("素材名称", key="ref_pip_library_label")
    labels = tags.text_input("内容标签", placeholder="例如：火锅、锅底、门店", key="ref_pip_library_tags")
    if st.button("添加素材", key="ref_pip_register_material", disabled=not upload):
        try:
            media_library.register_upload(upload, label, labels)
            st.success("素材已加入本机素材库。")
        except Exception as exc:
            st.error(str(exc))
    query = st.text_input("搜索素材名称或标签", key="ref_pip_library_search")
    rows = media_library.list_materials(query)
    if not rows:
        st.info("暂无匹配素材，先上传自己的图片或视频。")
    for offset in range(0, min(len(rows), 36), 3):
        for column, row in zip(st.columns(3), rows[offset:offset+3]):
            with column, st.container(border=True):
                st.image(row["thumbnail_path"], width="stretch")
                st.write(row["label"])
                st.caption("图片" if row["kind"] == "image" else f"视频 · {row['duration']:.1f} 秒")
                st.caption(" / ".join(row.get("tags", [])))


def _dismiss():
    st.session_state["ref_pip_open"] = False


@st.dialog("画中画设置", width="large", on_dismiss=_dismiss)
def render_dialog(ctx):
    ident = ctx.project.get("id", "draft")
    key = "ref_pip_draft_" + ident
    st.session_state.setdefault(key, deepcopy(ctx.project.get("config", {}).get("pip_items", [])))
    draft = st.session_state[key]
    setup, library = st.tabs(["精准文字画中画", "素材库"])
    with library:
        _library()
    with setup, st.container(key="ref_pip_dialog"):
        rows = timed_rows(ctx)
        duration = float(ctx.stage("voice").get("duration", 0))
        if not rows or duration <= 0:
            st.info("请先完成配音和口播生成，再按真实音轨时间设置画中画。")
            return
        st.caption(f"{len(rows)} 个文字段落 · 已选 {len(draft)} 段素材 · 口播 {duration:.1f} 秒")
        materials = media_library.list_materials()
        by_id = {row["id"]: row for row in materials}
        if not materials:
            st.info("到“素材库”上传自己的图片或视频，再返回选择。")
        first, second = st.columns(2)
        first_index = first.selectbox("起始段", range(len(rows)), format_func=lambda i: f"{i+1:02d} · {rows[i]['start']:.1f}s · {rows[i]['text'][:32]}", key="ref_pip_start_" + ident)
        end_key = "ref_pip_end_" + ident
        if st.session_state.get(end_key, first_index) < first_index:
            st.session_state[end_key] = first_index
        last_index = second.selectbox("结束段（可合并连续段落）", range(first_index, len(rows)), format_func=lambda i: f"{i+1:02d} · {rows[i]['end']:.1f}s · {rows[i]['text'][:32]}", key=end_key)
        st.text("\n".join(row["text"] for row in rows[first_index:last_index+1]))
        material_id = st.selectbox("选择素材", list(by_id), format_func=lambda i: by_id[i]["label"], index=None, key="ref_pip_choose_" + ident)
        if material_id:
            selected = by_id[material_id]
            st.image(selected["thumbnail_path"], width=180)
        a, b, c = st.columns(3)
        mode = a.radio("显示方式", ["window", "full"], format_func=lambda x: "窗口模式" if x == "window" else "全屏模式", key="ref_pip_mode_" + ident)
        position = b.selectbox("位置", list(POSITION_NAMES), format_func=POSITION_NAMES.get, index=8, key="ref_pip_position_" + ident, disabled=mode == "full")
        size = c.slider("窗口大小", .15, .6, .34, .01, key="ref_pip_size_" + ident, disabled=mode == "full")
        gap = st.slider("边距", 0., .2, .02, .01, key="ref_pip_padding_" + ident, disabled=mode == "full")
        a, b = st.columns(2)
        start = a.number_input("开始时间（秒）", 0., duration, float(rows[first_index]["start"]), .1, key=f"ref_pip_time_start_{ident}_{first_index}")
        end = b.number_input("结束时间（秒）", 0., duration, min(duration, float(rows[last_index]["end"])), .1, key=f"ref_pip_time_end_{ident}_{last_index}")
        if st.button("添加这一段", key="ref_pip_add_segment", type="primary", disabled=not material_id or len(draft) >= 32):
            try:
                item = {"path": by_id[material_id]["path"], "start": start, "end": end, "mode": mode, "position": position, "size": size, "padding": gap}
                items = processing.normalize({"pip_items": [*draft, item]})["pip_items"]
                st.session_state[key] = items
                st.rerun(scope="app")
            except Exception as exc:
                st.error(str(exc))
        with st.container(key="ref_pip_coverage", border=True):
            st.markdown("**素材覆盖范围**")
            if not draft:
                st.caption("未选择素材。人物原画面保留。")
            for i, item in enumerate(draft):
                text, remove = st.columns([5, 1])
                text.write(f"{i+1:02d} · {item['start']:.1f}–{item['end']:.1f}s · {Path(item['path']).name[:30]} · {'全屏' if item.get('mode') == 'full' else POSITION_NAMES.get(item['position'])}")
                if remove.button("删除", key=f"ref_pip_remove_{ident}_{i}"):
                    st.session_state[key] = [row for j, row in enumerate(draft) if j != i]
                    st.rerun(scope="app")
        st.caption("素材只替换或覆盖画面，保留完整口播声音；视频素材不足时停留在末帧。")
        if st.button("保存画中画", key="ref_pip_save", type="primary", disabled=ctx.busy):
            try:
                ctx._ensure_project({"pip_items": draft})
                st.session_state["ref_pip_items_pending"] = {"project_id": ctx.project["id"], "items": deepcopy(draft)}
                st.session_state.pop(key, None)
                _dismiss()
                st.rerun(scope="app")
            except Exception as exc:
                st.error(str(exc))
