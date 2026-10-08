"""Compact publishing column with immutable previews and explicit account approval."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re

import streamlit as st

from app.services.creator import publishing, release_assets


_PLATFORMS = (("douyin", "抖音"), ("kuaishou", "快手"),
              ("wechat", "视频号"), ("xiaohongshu", "小红书"))


def _config_snapshot():
    from app.config import config
    return deepcopy(config.snapshot_config_with_pending(config.app))


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


@st.dialog("封面设置")
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
    if st.button("保存设置", key="ref_cover_settings_save", type="primary", width="stretch"):
        ctx.submit_stage("release", {"cover_title": st.session_state["ref_cover_title"],
                                    "cover_style": st.session_state["ref_cover_style"],
                                    "cover_aspect": st.session_state["ref_cover_aspect"],
                                    "cover_frame_time": st.session_state["ref_cover_frame_time"]})
        st.rerun()


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
    with st.container(key="ref_release_info"):
        st.markdown("**发布信息**")
        with st.container(border=True, key="ref_release_copy_strip"):
            info, action = st.columns([1.65, 1], gap="small", vertical_alignment="center")
            with info:
                st.markdown("**✦ 标题与平台标签**")
                st.caption("生成后可继续编辑，发布时带入")
            with action:
                if st.button("✦ 智能生成", key="ref_publish_generate_copy", type="primary", width="stretch",
                             disabled=len(re.sub(r"\s+", "", source_text)) < 20):
                    ctx.queue("publish_copy", "生成发布标题与标签", _generate_copy,
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
                             disabled=not video or not st.session_state.get("ref_publish_title", "").strip()):
                    ctx.queue("cover", "生成封面", release_assets.generate_cover, video,
                              st.session_state.get("ref_cover_title") or st.session_state["ref_publish_title"],
                              style=st.session_state.get("ref_cover_style", "clean"),
                              aspect=st.session_state.get("ref_cover_aspect", "9:16"),
                              frame_time=float(st.session_state.get("ref_cover_frame_time", 0.0)))
            with settings:
                if st.button("⚙ 封面设置", key="ref_cover_settings", width="stretch"):
                    _cover_settings(ctx)
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
        with st.container(key="ref_release_publish_actions"):
            publish, schedule = st.columns(2, gap="small")
            if publish.button("发布", key="ref_publish_open", type="primary", width="stretch",
                              disabled=not video or not selected or not st.session_state["ref_publish_title"].strip()):
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
