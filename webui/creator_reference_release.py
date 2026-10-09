"""Compact publishing column with immutable previews and explicit account approval."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re
import io
import zipfile
import hashlib

import streamlit as st

from app.services.creator import publishing, release_assets


_PLATFORMS = (("douyin", "抖音"), ("kuaishou", "快手"),
              ("wechat", "视频号"), ("xiaohongshu", "小红书"))


def _config_snapshot():
    from app.config import config
    return deepcopy(config.snapshot_config_with_pending(config.app))


def _busy(ctx):
    return bool(getattr(ctx, "busy", False)) or (getattr(ctx, "project", {}) or {}).get("state") in {"queued", "running"}


def _queue(ctx, *args, **kwargs):
    try:
        ctx.queue(*args, **kwargs)
    except Exception as exc:
        st.error(str(exc))


def _generate_copy(text, progress=None, app_config=None):
    result = release_assets.generate_metadata(text, count=3, progress=progress, app_config=app_config)
    return dict(result, title=result["titles"][0],
                tags=" ".join("#" + tag.lstrip("#") for tag in result["hashtags"]))


def _tags(text):
    return [part.lstrip("#") for part in re.split(r"[\s,，;；]+", text.strip()) if part.lstrip("#")]


def _path(value, field):
    if isinstance(value, dict):
        value = value.get(field, "")
    return str(value) if value else ""


def _source_text(ctx):
    # The editor is the authoritative draft; never derive copy from another video.
    if "ref_script_text" in st.session_state:
        return st.session_state["ref_script_text"]
    stage = ctx.stage("script") or {}
    return stage.get("text", "")


def _snapshot(ctx, selected):
    video = _path(ctx.current_video(rendered=True), "video_path")
    cover = _path(ctx.current_cover(), "cover_path")
    title = st.session_state.get("ref_publish_title", "").strip()
    description = st.session_state.get("ref_publish_description", "").strip()
    tags = " ".join("#" + tag for tag in _tags(st.session_state.get("ref_publish_tags", "")))
    if not video or not Path(video).is_file():
        raise ValueError("请先生成或导入完整成片。")
    if not title:
        raise ValueError("请先填写发布标题。")
    if not selected:
        raise ValueError("请先选择发布平台和账号。")
    return {"video_path": video, "cover_path": cover or None, "title": title,
            "description": "\n".join(part for part in (description, tags) if part),
            "accounts": list(selected)}


def _prepare_snapshot(snapshot):
    """Stage copies and persist immutable review tasks; never start a publisher."""
    return publishing.prepare_publish(**deepcopy(snapshot))


def _show_notice():
    notice = st.session_state.pop("creator_publish_notice", None)
    if notice:
        getattr(st, notice[0])(notice[1])


@st.dialog("发布前确认", width="large")
def _publish_dialog(task_ids):
    from webui import creator_publish_workspace as workspace
    workspace._collect()
    _show_notice()
    accounts = {row["id"]: row for row in publishing.list_accounts()}
    tasks = publishing.list_tasks()
    st.caption("逐项核对这份视频、最终文案和账号，确认后才会上传并发布。")
    for ident in task_ids:
        task = publishing.get_task(ident)
        if not task:
            st.warning("该发布预览已不存在，请重新准备。")
            continue
        with st.container(border=True, key="ref_release_confirm_" + ident):
            st.markdown("**" + task.get("platform_name", "") + " · " + task.get("account_name", "") + "**")
            video = Path(task.get("video_path", ""))
            if video.is_file():
                st.video(str(video))
            else:
                st.warning("这份预览的视频已移动或删除，请重新准备。")
            cover = task.get("cover_path")
            if cover and Path(cover).is_file():
                st.image(cover, width=160)
            st.text("标题：" + task.get("title", ""))
            st.text(task.get("description") or "无正文与标签")
            account = accounts.get(task.get("account_id"))
            blocked = not account or workspace._busy_account(account, tasks) or not video.is_file()
            status = task.get("status", "prepared")
            if status in workspace._RETRYABLE and not task.get("submit_started"):
                st.caption("确认后会上传此预览；平台页面与文案会交给已配置的 AI 服务协助操作。")
                st.button("确认发布到" + task.get("platform_name", "平台") + "（" + task.get("account_name", "账号") + "）",
                          key="ref_publish_confirm_" + ident,
                          type="primary", width="stretch", disabled=bool(blocked),
                          on_click=workspace._confirm, args=(ident,))
                if blocked:
                    st.caption("请完成账号登录并关闭窗口，或等待当前发布任务结束。")
            elif status == "submission_unknown" or task.get("submit_started") and status not in {"submitted", "reviewing", "published"}:
                st.warning("提交结果待核查，请到平台作品列表确认，系统不会重发。")
            else:
                st.info(task.get("message") or workspace._STATUS_NAMES.get(status, status))
            if task.get("url"):
                st.link_button("查看平台作品", task["url"])
    if st.button("关闭预览", key="ref_publish_preview_close", width="stretch"):
        st.rerun()


def _open_accounts(ctx):
    from webui import creator_publish_workspace as workspace
    show_dialog = getattr(workspace, "show_account_dialog", None)
    if show_dialog:
        show_dialog()
    else:
        ctx.open_tool("发布中心")


def _dismiss_cover_settings():
    snapshot = st.session_state.pop("ref_cover_settings_before", None)
    if snapshot:
        st.session_state["ref_cover_cancel_pending"] = snapshot
    st.session_state["ref_cover_settings_open"] = False


@st.dialog("封面设置", on_dismiss=_dismiss_cover_settings)
def _cover_settings(ctx):
    styles = {row["id"]: row for row in release_assets.list_styles()}
    st.session_state.setdefault("ref_cover_style", "clean")
    st.session_state.setdefault("ref_cover_aspect", "9:16")
    st.session_state.setdefault("ref_cover_frame_time", 0.0)
    st.session_state.setdefault("ref_cover_title", st.session_state.get("ref_publish_title", ""))
    st.text_input("封面标题", key="ref_cover_title", max_chars=120, persist_state="session")
    st.selectbox("封面风格", list(styles), key="ref_cover_style",
                 format_func=lambda ident: styles[ident]["name"], persist_state="session")
    st.selectbox("画面比例", ["9:16", "16:9", "1:1"], key="ref_cover_aspect", persist_state="session")
    st.number_input("截取位置（秒）", min_value=0.0, step=0.5, key="ref_cover_frame_time", persist_state="session")
    if st.button("保存设置", key="ref_cover_settings_save", type="primary", width="stretch", disabled=_busy(ctx)):
        try:
            if st.session_state.get("ref_script_text", "").strip():
                ctx._ensure_project({"cover_title": st.session_state["ref_cover_title"],
                                     "cover_style": st.session_state["ref_cover_style"],
                                     "cover_aspect": st.session_state["ref_cover_aspect"],
                                     "cover_frame_time": st.session_state["ref_cover_frame_time"]})
            st.session_state["ref_last_message"] = "封面设置已保存；点击自动生成封面查看效果。"
            st.session_state["ref_cover_settings_open"] = False
            st.session_state.pop("ref_cover_settings_before", None)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


def _export_bundle(video, cover, title, description, tags):
    """Export only the approved media and text, never account or service secrets."""
    if not video or not Path(video).is_file():
        raise ValueError("请先生成可播放的成片。")
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.write(video, "成片" + Path(video).suffix)
        if cover and Path(cover).is_file():
            archive.write(cover, "封面" + Path(cover).suffix)
        archive.writestr("发布文案.txt", "标题：" + title + "\n\n" + description + "\n\n" + tags)
        archive.writestr("使用说明.txt", "上传成片和封面，粘贴发布文案；登录自己的账号并按平台要求检查，确认后提交。此包没有执行发布，也不包含账号登录或服务配置。")
    return memory.getvalue()


@st.dialog("快手／视频号发布助手", width="large")
def _native_publish_assistant(ctx):
    st.caption("使用平台原生后台发布：先下载视频、封面与文案，再登录自己的账号并上传确认。")
    left, right = st.columns(2)
    left.link_button("打开快手创作者后台", "https://cp.kuaishou.com/", width="stretch")
    right.link_button("打开视频号助手", "https://channels.weixin.qq.com/", width="stretch")
    video, cover = _path(ctx.current_video(rendered=True), "video_path"), _path(ctx.current_cover(), "cover_path")
    if not video:
        st.info("请先生成成片，再下载发布包。")
        return
    title = st.session_state.get("ref_publish_title", "")
    description = st.session_state.get("ref_publish_description", "")
    tags = st.session_state.get("ref_publish_tags", "")
    stamps = [title, description, tags]
    for value in (video, cover):
        path = Path(value) if value else None
        stamps.append(str(path.resolve()) + str(path.stat().st_mtime_ns) + str(path.stat().st_size) if path and path.is_file() else "")
    fingerprint = hashlib.sha256("\n".join(stamps).encode("utf-8")).hexdigest()
    if st.session_state.get("ref_native_bundle_source") != fingerprint:
        st.session_state.pop("ref_native_release_bundle", None)
    st.session_state["ref_native_bundle_source"] = fingerprint
    st.text("标题：" + title)
    st.text(description + "\n" + tags)
    if st.button("准备发布包", key="ref_native_prepare_package", type="primary"):
        st.session_state["ref_native_release_bundle"] = _export_bundle(video, cover, title, description, tags)
    data = st.session_state.get("ref_native_release_bundle")
    if data:
        st.download_button("下载视频、封面和文案", data, file_name="发布素材包.zip", mime="application/zip", key="ref_native_download_bundle")


@st.dialog("封面预览")
def _cover_preview(path):
    st.image(path, width="stretch")


@st.dialog("定时发布")
def _schedule_dialog(ctx, selected):
    st.info("暂未接入自动定时发布。可下载发布清单，按选定时间手动发布。")
    state = st.session_state
    planned = datetime.combine(state["ref_publish_date"], state["ref_publish_time"])
    accounts = {row["id"]: row for row in publishing.list_accounts()}
    text = ("计划发布时间：" + planned.strftime("%Y-%m-%d %H:%M") + "\n"
            "标题：" + state.get("ref_publish_title", "") + "\n"
            "正文：" + state.get("ref_publish_description", "") + "\n"
            "标签：" + state.get("ref_publish_tags", "") + "\n"
            "成片：" + _path(ctx.current_video(rendered=True), "video_path") + "\n"
            "账号：" + "、".join(accounts[ident]["name"] for ident in selected if ident in accounts) + "\n"
            "此清单仅供手动发布参考，没有创建自动发布任务。\n")
    st.download_button("下载发布清单", text.encode("utf-8-sig"), file_name="发布清单.txt",
                       mime="text/plain", key="ref_publish_schedule_export", width="stretch")


def _platform_picker(accounts):
    selected = []
    for offset in (0, 2):
        columns = st.columns(2, gap="small")
        for column, (platform, label) in zip(columns, _PLATFORMS[offset:offset + 2]):
            supported = platform in publishing.PLATFORMS
            choices = {row["id"]: row for row in accounts if row.get("platform") == platform}
            check_key, account_key = "ref_publish_" + platform, "ref_publish_account_" + platform
            st.session_state.setdefault(check_key, False)
            if st.session_state.get(account_key) not in choices:
                st.session_state[account_key] = None
            if not supported:
                st.session_state[check_key] = False
            with column, st.container(border=True, key="ref_release_platform_" + platform):
                tick, picker = st.columns([1.2, 1.3], gap="small", vertical_alignment="center")
                with tick:
                    enabled = st.checkbox(label, key=check_key, disabled=not supported,
                                          help="暂未接入" if not supported else None)
                with picker:
                    ident = st.selectbox(label + "发布账号", list(choices), index=None,
                                         placeholder="选择账号", key=account_key,
                                         label_visibility="collapsed", disabled=not supported or not choices,
                                         format_func=lambda value, rows=choices: rows[value]["name"],
                                         help="暂未接入" if not supported else None)
                if supported and enabled and ident:
                    selected.append(ident)
    return selected


def render(ctx):
    """Render the fourth reference column; its heading and CSS belong to the shell."""
    st.session_state.setdefault("ref_publish_title", "")
    st.session_state.setdefault("ref_publish_tags", "")
    st.session_state.setdefault("ref_publish_description", "")
    now = datetime.now()
    st.session_state.setdefault("ref_publish_date", now.date())
    st.session_state.setdefault("ref_publish_time", now.time().replace(second=0, microsecond=0))
    video = _path(ctx.current_video(rendered=True), "video_path")
    source_text = _source_text(ctx)
    busy = _busy(ctx)
    with st.container(key="ref_release_info"):
        st.markdown("**发布信息**")
        with st.container(border=True, key="ref_release_copy_strip"):
            info, action = st.columns([1.65, 1], gap="small", vertical_alignment="center")
            with info:
                st.markdown("**✦ 标题与平台标签**")
                st.caption("生成后可继续编辑，发布时带入")
            with action:
                if st.button("✦ 智能生成", key="ref_publish_generate_copy", type="primary", width="stretch",
                             disabled=busy or len(re.sub(r"\s+", "", source_text)) < 20):
                    _queue(ctx, "publish_copy", "生成发布标题与标签", _generate_copy,
                              source_text, app_config=_config_snapshot())
        st.caption("✓ 标题 " + str(len(st.session_state["ref_publish_title"])) + " 字　　✓ 标签 "
                   + str(len(_tags(st.session_state["ref_publish_tags"]))) + " 个")
        title, tags = st.columns(2, gap="small")
        with title, st.container(border=True, key="ref_release_title"):
            st.markdown("**H₂ 发布标题**")
            st.text_area("发布标题", key="ref_publish_title", height=68, max_chars=120,
                         placeholder="输入或智能生成标题…", label_visibility="collapsed")
        with tags, st.container(border=True, key="ref_release_tags"):
            st.markdown("**◉ 平台标签**")
            st.text_area("平台标签", key="ref_publish_tags", height=68,
                         placeholder="例如：数字人、短视频、获客…", label_visibility="collapsed")
    with st.container(key="ref_release_cover"):
        st.markdown("**封面制作**")
        with st.container(key="ref_release_cover_actions"):
            generate, settings = st.columns([1.8, 1], gap="small")
            with generate:
                if st.button("自动生成封面", key="ref_cover_generate", type="primary", width="stretch",
                             disabled=busy or not video or not st.session_state.get("ref_publish_title", "").strip()):
                    _queue(ctx, "cover", "生成封面", release_assets.generate_cover, video,
                              st.session_state.get("ref_cover_title") or st.session_state["ref_publish_title"],
                              style=st.session_state.get("ref_cover_style", "clean"),
                              aspect=st.session_state.get("ref_cover_aspect", "9:16"),
                              frame_time=float(st.session_state.get("ref_cover_frame_time", 0.0)))
            with settings:
                if st.button("⚙ 封面设置", key="ref_cover_settings", width="stretch", disabled=busy):
                    st.session_state["ref_cover_settings_before"] = {
                        "project_id": (getattr(ctx, "project", {}) or {}).get("id", ""),
                        "values": {key: st.session_state.get(key) for key in ("ref_cover_title", "ref_cover_style", "ref_cover_aspect", "ref_cover_frame_time")},
                    }
                    st.session_state["ref_cover_settings_open"] = True
        cover = _path(ctx.current_cover(), "cover_path")
        with st.container(key="ref_release_cover_preview"):
            label, expand, download = st.columns([6, 1, 1], gap="small", vertical_alignment="center")
            label.markdown("**封面预览**")
            has_cover = bool(cover and Path(cover).is_file())
            if expand.button("↗", key="ref_cover_expand", disabled=not has_cover, help="放大封面"):
                _cover_preview(cover)
            if has_cover:
                path = Path(cover)
                download.download_button("⇩", path.read_bytes(), file_name=path.name, mime="image/png",
                                         key="ref_cover_download", help="下载封面")
                _, center, _ = st.columns([1, 2.2, 1], gap="small")
                with center:
                    st.image(cover, width=200)
            else:
                download.button("⇩", key="ref_cover_download_empty", disabled=True, help="下载封面")
                st.markdown('<div class="ref-cover-empty">暂无封面预览</div>', unsafe_allow_html=True)
    with st.container(key="ref_release_publish"):
        heading, accounts_button = st.columns([1.7, 1], gap="small", vertical_alignment="center")
        heading.markdown("**视频发布**")
        if accounts_button.button("♙ 账号管理", key="ref_publish_accounts", width="stretch"):
            _open_accounts(ctx)
        selected = _platform_picker(publishing.list_accounts())
        if st.button("快手／视频号发布助手", key="ref_native_publish_assistant", width="stretch"):
            _native_publish_assistant(ctx)
        with st.container(key="ref_release_publish_actions"):
            publish, schedule = st.columns(2, gap="small")
            if publish.button("发布", key="ref_publish_open", type="primary", width="stretch",
                              disabled=busy or not video or not selected or not st.session_state["ref_publish_title"].strip()):
                try:
                    snapshot = _snapshot(ctx, selected)
                    with st.spinner("正在准备发布预览…"):
                        prepared = _prepare_snapshot(snapshot)
                    st.session_state["ref_publish_preview_ids"] = [task["id"] for task in prepared]
                    _publish_dialog(st.session_state["ref_publish_preview_ids"])
                except Exception as exc:
                    st.error(str(exc))
            if schedule.button("定时发布", key="ref_publish_schedule", type="primary", width="stretch",
                               help="下载清单，按选定时间手动发布"):
                _schedule_dialog(ctx, selected)
        with st.container(key="ref_release_schedule"):
            label, date, clock = st.columns([1, 1.7, 1.1], gap="small", vertical_alignment="center")
            label.caption("发布时间")
            date.date_input("发布日期", key="ref_publish_date", label_visibility="collapsed", format="YYYY-MM-DD")
            clock.time_input("发布时间", key="ref_publish_time", label_visibility="collapsed")
    if st.session_state.get("ref_cover_settings_open"):
        _cover_settings(ctx)
