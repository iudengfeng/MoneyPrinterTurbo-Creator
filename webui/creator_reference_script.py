"""Reference learning and manuscript editing for the four-column home screen.

The column submits real creator services through its workspace context. Extracted
or generated text is installed by the parent before it renders the text widgets.
"""
from __future__ import annotations

from html import escape
import hashlib
from copy import deepcopy
from pathlib import Path

import streamlit as st

from app.services.creator import competitors, extract, script_review, store, topics


def _busy(ctx):
    return bool(getattr(ctx, "busy", False))


def _submit(ctx, kind, label, operation, **kwargs):
    try:
        ctx.queue(kind, label, operation, **kwargs)
        return True
    except Exception as exc:
        st.error(str(exc))
        return False


def _dismiss_dialog():
    st.session_state.pop("ref_script_dialog", None)
    st.session_state["ref_pending_learning_mode"] = "视频学习"


def _close_dialog():
    _dismiss_dialog()
    st.rerun(scope="app")


def _extract_source(link="", media_path="", title="", app_config=None, progress=None):
    """Download/recognize actual audio and retain its provenance in the library."""
    if not link and not media_path:
        raise ValueError("请填写分享链接或上传本地音视频。")
    path = extract.download_media(link, progress=progress) if link else Path(media_path)
    result = extract.extract_media(path, language="zh", progress=progress)
    reference = topics.save_reference(
        title.strip() or Path(result["media_path"]).stem,
        result["text"], source_url=link,
    )
    return dict(result, reference_id=reference["id"], source_url=link)


def _load_reference(ident, app_config=None, progress=None):
    row = store.get_record("references", ident)
    if not row:
        raise ValueError("参考文案不存在，请重新选择。")
    text = row.get("spoken_script") or row.get("full_content") or row.get("text", "")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("这条参考资料没有已读取的口播正文，请先导入音视频。")
    return dict(row, text=text)


def _load_draft(ident, app_config=None, progress=None):
    row = store.get_record("drafts", ident)
    if not row or not row.get("text"):
        raise ValueError("文案不存在或正文为空，请重新选择。")
    return row


def _import_manuscript(text, title="", app_config=None, progress=None):
    return topics.save_draft(title.strip() or text.strip()[:40], text)


def _counter(title, text, *, maximum=None, subtitle=False):
    count = len("".join(str(text).split()))
    label = f"{count}/{maximum}" if maximum else f"{count} 字符"
    css_class = "ref-card-subtitle" if subtitle else "ref-card-title"
    st.markdown(
        f'<div class="{css_class}"><span>{escape(title)}</span>'
        f'<span class="ref-counter">{label}</span></div>',
        unsafe_allow_html=True,
    )


@st.dialog("打开视频链接", width="large", on_dismiss=_dismiss_dialog)
def _video_dialog(ctx):
    st.caption("导入公开视频或本地音视频，识别真实口播原文。")
    left, right = st.columns(2, gap="medium")
    with left, st.container(border=True, key="ref_link_import"):
        st.markdown("**视频分享链接**")
        link = st.text_area(
            "分享链接", key="ref_video_link", height=124,
            placeholder="粘贴包含 http / https 地址的单条视频分享链接",
            label_visibility="collapsed", max_chars=3000, persist_state="session",
        )
        title = st.text_input("参考标题（可选）", key="ref_video_title", persist_state="session")
        if st.button("提取视频文案", key="ref_extract_link", type="primary", width="stretch", disabled=_busy(ctx)):
            if not link.strip():
                st.warning("请先粘贴视频分享链接。")
            elif _submit(ctx, "extract", "识别视频口播", _extract_source, link=link.strip(), title=title.strip()):
                _close_dialog()
    with right, st.container(border=True, key="ref_local_import"):
        st.markdown("**本地音视频**")
        upload = st.file_uploader(
            "上传音视频", type=["mp4", "mov", "mkv", "webm", "m4v", "wav", "mp3", "m4a", "aac", "ogg"],
            key="ref_source_upload", label_visibility="collapsed", disabled=_busy(ctx),
        )
        st.caption("选择有清晰人声的文件，识别结果会进入原文区与参考库。")
        if st.button("识别本地文案", key="ref_extract_upload", type="primary", width="stretch", disabled=_busy(ctx) or upload is None):
            try:
                media_path = ctx.stage_upload(upload)
            except Exception as exc:
                st.error(str(exc))
            else:
                if _submit(ctx, "extract", "识别本地口播", _extract_source, media_path=media_path, title=Path(upload.name).stem):
                    _close_dialog()
    st.caption("分享链接若需要平台登录、已过期或无法解析，请上传本地文件。")


def _reference_library(ctx, *, prefix="ref_library"):
    keyword = st.text_input("筛选参考文案", key=prefix + "_query", placeholder="搜索标题、行业或正文关键词")
    rows = topics.list_references(keyword)
    if not rows:
        st.info("还没有符合条件的参考文案。可先导入视频，或保存自己的参考资料。")
    else:
        by_id = {row["id"]: row for row in rows}
        selection = st.selectbox(
            "参考文案", list(by_id),
            format_func=lambda ident: by_id[ident].get("title", "未命名参考"),
            key=prefix + "_selection",
        )
        row = by_id[selection]
        st.caption(row.get("source_label", "本地参考资料"))
        if row.get("source") == "public_competitor":
            counts = row.get("hot_metrics") or {}
            for column, (field, label) in zip(st.columns(3), (("like", "点赞"), ("comment", "评论"), ("collect", "收藏"))):
                value = counts.get(field)
                column.metric(label, "未取得" if value is None else str(value))
            st.caption("只显示页面公开的互动数据，不估算播放量或保证爆款。")
        text = row.get("spoken_script") or row.get("full_content") or row.get("text", "")
        st.text_area("已读取的参考正文", value=text, height=220, disabled=True, key=prefix + "_preview_" + selection)
        if row.get("source_url"):
            st.link_button("查看原始来源", row["source_url"], width="stretch")
        if st.button("使用这篇原文", key=prefix + "_use", type="primary", width="stretch", disabled=_busy(ctx) or not text):
            if _submit(ctx, "extract", "载入参考原文", _load_reference, ident=selection):
                _close_dialog()
    with st.expander("保存自己的参考资料"):
        title = st.text_input("参考标题", key=prefix + "_new_title", max_chars=300)
        text = st.text_area("参考正文", key=prefix + "_new_text", height=150, max_chars=10000)
        source = st.text_input("来源链接（可选）", key=prefix + "_new_source", max_chars=2000)
        if st.button("保存参考文案", key=prefix + "_save", disabled=_busy(ctx)):
            try:
                topics.save_reference(title, text, keyword=keyword, source_url=source)
            except Exception as exc:
                st.error(str(exc))
            else:
                st.success("参考文案已保存，可从列表中选择使用。")
                st.rerun(scope="app")


def _account_profile(ctx):
    from app.services.creator import competitors
    with st.expander("主页链接学习", expanded=False):
        homepage = st.text_input("参考博主的公开主页或作品列表", key="ref_ip_homepage", max_chars=2000)
        st.caption("读取可访问的公开正文并加入参考库。登录限制或未公开完整口播时，可导入作品分享链接、本地视频或自己提供的原文。")
        if st.button("读取公开内容", key="ref_ip_collect", type="primary", disabled=_busy(ctx) or not homepage.strip()):
            try:
                settings = competitors.get_settings()
                sources = settings["sources"]
                if not any((item if isinstance(item, str) else item.get("url")) == homepage.strip() for item in sources):
                    competitors.save_settings({"sources": [*sources, {"url": homepage.strip()}]})
                if _submit(ctx, "ip_collect", "读取公开参考内容", competitors.collect_once):
                    _close_dialog()
            except Exception as exc:
                st.error(str(exc))
    rows = topics.list_accounts()
    names = {row["id"]: row["name"] for row in rows}
    options = [None, *names]
    if st.session_state.get("ref_account_id") not in options:
        st.session_state["ref_account_id"] = None
    st.selectbox(
        "本次使用的账号定位", options, key="ref_account_id",
        format_func=lambda ident: names.get(ident, "暂不使用账号定位"), persist_state="session",
    )
    st.text_input("本次创作主题", key="ref_writing_topic", max_chars=300, placeholder="例如：怎样把复杂知识解释清楚", persist_state="session")
    st.text_area("补充创作要求", key="ref_writing_instructions", height=90, max_chars=3000, placeholder="受众、表达方式或你想重点讲的事实", persist_state="session")
    with st.expander("新建账号定位", expanded=not rows):
        name = st.text_input("账号名称", key="ref_account_name", max_chars=200)
        industry = st.text_input("行业", key="ref_account_industry", max_chars=1000)
        audience = st.text_input("目标受众", key="ref_account_audience", max_chars=1000)
        positioning = st.text_area("账号定位", key="ref_account_positioning", height=100, max_chars=3000)
        references = st.text_area("补充资料（可选）", key="ref_account_references", height=90, max_chars=10000)
        if st.button("保存账号定位", key="ref_account_save", type="primary", disabled=_busy(ctx)):
            try:
                row = topics.save_account(name, industry, audience, positioning, references)
            except Exception as exc:
                st.error(str(exc))
            else:
                # The selectbox has already rendered in this dialog; apply the
                # saved choice on the next full render instead of mutating it.
                st.session_state["ref_pending_account"] = row["id"]
                _close_dialog()


def _existing_manuscripts(ctx):
    rows = topics.list_drafts()
    if rows:
        by_id = {row["id"]: row for row in rows}
        selection = st.selectbox("已保存文案", list(by_id), key="ref_saved_draft", format_func=lambda ident: by_id[ident].get("title", "未命名文案"))
        st.text_area("文案全文", value=by_id[selection]["text"], height=190, disabled=True, key="ref_saved_draft_preview_" + selection)
        if st.button("继续编辑这篇文案", key="ref_use_draft", type="primary", width="stretch", disabled=_busy(ctx)):
            if _submit(ctx, "script", "载入已保存文案", _load_draft, ident=selection):
                st.rerun(scope="app")
    else:
        st.caption("还没有已保存文案，也可以直接粘贴正文。")
    st.text_input("文案标题（可选）", key="ref_import_title", max_chars=300)
    text = st.text_area("粘贴现成文案", key="ref_import_text", height=170, max_chars=12000)
    if st.button("使用粘贴文案", key="ref_use_manuscript", type="primary", width="stretch", disabled=_busy(ctx) or not text.strip()):
        if _submit(ctx, "script", "导入现成文案", _import_manuscript, text=text, title=st.session_state.get("ref_import_title", "")):
            _close_dialog()


@st.dialog("IP 学习", width="large", on_dismiss=_dismiss_dialog)
def _ip_dialog(ctx):
    account, library, manuscripts = st.tabs(["账号定位", "参考资料", "现成文案"])
    with account:
        _account_profile(ctx)
    with library:
        _reference_library(ctx, prefix="ref_ip_library")
    with manuscripts:
        _existing_manuscripts(ctx)


@st.dialog("爆款文案参考库", width="large", on_dismiss=_dismiss_dialog)
def _library_dialog(ctx):
    st.caption("采集公开内容、查看真实来源，再选用已读取的文案。")
    with st.container(key="ref_viral_scrapling", border=True):
        st.markdown("**从平台寻找参考文案**")
        keyword, platform = st.columns([2, 1])
        query = keyword.text_input("行业／关键词", key="ref_viral_keyword", max_chars=100, placeholder="例如：火锅、装修避坑、AI工具", disabled=_busy(ctx), persist_state="session")
        target = platform.selectbox("平台", ["douyin", "xiaohongshu", "bilibili"],
                                    format_func=lambda x: {"douyin": "抖音", "xiaohongshu": "小红书", "bilibili": "B站"}[x],
                                    key="ref_viral_platform", disabled=_busy(ctx), persist_state="session")
        with st.expander("指定公开来源（可选）"):
            links = st.text_area("作品、主页或公开内容页面链接", key="ref_viral_urls", height=90, max_chars=5000,
                                 placeholder="每行一条，最多5条。留空则尝试平台关键词搜索。", disabled=_busy(ctx), persist_state="session")
        ready = competitors.dependency_ready()
        st.caption("公开内容采集引擎已就绪" if ready else "Scrapling 未就绪，请在运行环境中准备采集组件。")
        if st.button("Scrapling 采集", key="ref_viral_collect", type="primary", width="stretch", disabled=_busy(ctx) or not ready):
            urls = [value.strip() for value in links.splitlines() if value.strip()]
            if len(urls) > 5:
                st.warning("一次最多填写5条公开来源。")
            elif not query.strip() and not urls and not competitors.get_settings()["sources"]:
                st.warning("请填写关键词或公开来源链接。")
            elif _submit(ctx, "viral_collect", "采集公开参考文案", competitors.collect_selection,
                         keyword=query.strip(), source_urls=urls, platform=target):
                st.rerun(scope="app")
        st.caption("遇到登录、验证或限流会停止并显示原因；未取得正文的页面不会当作完整口播。")
    result = st.session_state.get("ref_viral_result")
    if isinstance(result, dict):
        st.caption("最近一次采集 · " + (result.get("keyword") or "已选来源"))
        status = result.get("status")
        getattr(st, "success" if status == "done" else "warning")(result.get("message", "采集结束"))
        for error in result.get("errors", []):
            st.caption(error.get("message", "来源未能读取"))
            if error.get("source_url"):
                st.link_button("打开来源核查", error["source_url"])
        if result.get("filtered"):
            st.caption("未加入正文参考的内容：" + "；".join(f"{reason}（{count}）" for reason, count in result.get("filter_reasons", {}).items()))
        for url in result.get("source_urls", []):
            if status != "done":
                st.link_button("打开搜索／公开来源", url)
    _reference_library(ctx)


def _commit_script(ctx):
    try:
        ctx.save_script(st.session_state.get("ref_script_text", ""))
    except Exception as exc:
        st.session_state["ref_script_notice"] = str(exc)


def _generate(ctx, *, rewrite):
    original = st.session_state.get("ref_original_text", "").strip()
    script = st.session_state.get("ref_script_text", "").strip()
    reference = (script or original) if rewrite else (original or script)
    title = st.session_state.get("ref_writing_topic", "").strip()
    if not reference and not title:
        st.warning("请先导入原文、填写文案，或在 IP 学习中设置创作主题。")
        return
    if rewrite and not reference:
        st.warning("请先输入要改写的原文或文案。")
        return
    title = title or (reference or script).replace("\n", " ")[:60]
    instructions = st.session_state.get("ref_writing_instructions", "").strip()
    if rewrite:
        instructions += "\n保持已提供的核心事实，以自然的普通话重新组织表达，避免照抄较长原文。"
    if _submit(
        ctx, "script", "AI 洗稿" if rewrite else "撰写文案", topics.generate_draft,
        title=title, mode="imitate" if rewrite else "original", reference_text=reference,
        account_id=st.session_state.get("ref_account_id"), style="科普干货",
        target_length=int(st.session_state.get("ref_target_length", 300)), instructions=instructions,
    ):
        st.rerun(scope="app")


def _request_review(ctx):
    text = st.session_state.get("ref_script_text", "")
    if not text.strip():
        st.warning("请先填写要审阅的口播文案。")
        return
    from app.config import config
    snapshot = deepcopy(config.snapshot_config_with_pending(config.app))
    if _submit(ctx, "script_review", "AI 法务 · 文案风险审阅", script_review.review_script,
               text=text, app_config=snapshot):
        st.session_state.pop("ref_script_dialog", None)
        st.rerun(scope="app")


def _dismiss_review():
    st.session_state["ref_script_review_open"] = False


def _stage_review_adoption(ctx, report):
    if _busy(ctx):
        st.warning("当前任务正在处理，请完成后再采用。")
        return False
    source = report.get("source_text")
    optimized = report.get("optimized_text")
    if not isinstance(source, str) or source != st.session_state.get("ref_script_text", ""):
        st.warning("文案已修改，请重新审阅当前稿件后再采用。")
        return False
    if not isinstance(optimized, str) or not optimized.strip() or optimized == source:
        st.info("本次报告保留原文，可按风险建议手动修改后重新审阅。")
        return False
    st.session_state["ref_script_review_adopt_pending"] = {
        "source_text": source, "optimized_text": optimized,
        "project_id": (getattr(ctx, "project", None) or {}).get("id", ""),
    }
    _dismiss_review()
    return True


def _highlight_review_source(report):
    source = report.get("source_text", "")
    cursor, parts = 0, []
    for risk in sorted(report.get("risks", []), key=lambda row: row.get("start", 0)):
        start, end = risk.get("start"), risk.get("end")
        if (type(start) is not int or type(end) is not int or not cursor <= start < end <= len(source)
                or source[start:end] != risk.get("quote")):
            continue
        parts.extend((escape(source[cursor:start]), '<mark style="background:#ffe4e8;color:#8e223c;border-radius:3px">',
                      escape(source[start:end]), "</mark>"))
        cursor = end
    parts.append(escape(source[cursor:]))
    return '<div style="white-space:pre-wrap;max-height:245px;overflow:auto;padding:12px;border:1px solid var(--ref-line,#dce2ec);border-radius:10px">' + "".join(parts) + "</div>"


@st.dialog("AI 法务审校报告", width="medium", on_dismiss=_dismiss_review)
def _review_dialog(ctx):
    report = st.session_state.get("ref_script_review_result")
    if not isinstance(report, dict):
        st.info("尚无审阅报告，请先提交当前文案。")
        return
    local = report.get("engine") == "local_rules"
    st.caption("来源：本地措辞初筛" if local else "来源：已配置文案模型辅助审阅")
    st.caption("仅提示潜在风险与待核查事项；事实、适用规则和平台审核结果仍需核对。")
    risks = report.get("risks") or []
    st.warning(f"标注 {len(risks)} 处待核查表达") if risks else st.info("本次未标注风险，仍需核查事实与发布要求。")
    st.markdown("**01 原文案分析**")
    st.markdown(_highlight_review_source(report), unsafe_allow_html=True)
    st.markdown("**02 优化后文案**")
    optimized = report.get("optimized_text", "")
    key = hashlib.sha256((report.get("source_text", "") + optimized).encode("utf-8")).hexdigest()[:16]
    st.text_area("优化后文案", value=optimized, height=200, disabled=True,
                 key="ref_review_optimized_" + key, label_visibility="collapsed")
    st.markdown("**03 审阅解读**")
    st.text(report.get("summary", ""))
    for index, risk in enumerate(risks, 1):
        with st.expander(f"{index:02d} {risk.get('category', '待核查')} · {risk.get('quote', '')[:35]}"):
            st.text("原因：" + risk.get("reason", ""))
            st.text("建议：" + risk.get("suggestion", ""))
    stale = report.get("source_text") != st.session_state.get("ref_script_text", "")
    if stale:
        st.warning("当前稿件与审阅原文不同，重新审阅后才能采用。")
    close, adopt = st.columns(2, gap="small")
    if close.button("关闭", key="ref_review_close", width="stretch"):
        _dismiss_review()
        st.rerun(scope="app")
    if adopt.button("采用优化文案", key="ref_review_adopt", type="primary", width="stretch",
                    disabled=_busy(ctx) or stale or optimized == report.get("source_text") or not optimized.strip()):
        if _stage_review_adoption(ctx, report):
            st.rerun(scope="app")


def render_script_column(ctx):
    """Render only the body of column 01; the parent supplies its heading."""
    st.session_state.setdefault("ref_original_text", "")
    st.session_state.setdefault("ref_script_text", "")
    st.session_state.setdefault("ref_learning_mode", "视频学习")
    st.session_state.setdefault("ref_target_length", 300)
    st.session_state.setdefault("ref_script_language", "中文（普通话）")
    if "ref_pending_account" in st.session_state:
        st.session_state["ref_account_id"] = st.session_state.pop("ref_pending_account")
    if "ref_pending_learning_mode" in st.session_state:
        st.session_state["ref_learning_mode"] = st.session_state.pop("ref_pending_learning_mode")

    with st.container(key="ref_script_controls"):
        mode = st.segmented_control(
            "学习方式", ["IP学习", "视频学习", "爆款文案"],
            key="ref_learning_mode", label_visibility="collapsed", width="stretch", persist_state="session",
        )
        previous_mode = st.session_state.get("ref_previous_learning_mode", "视频学习")
        st.session_state["ref_previous_learning_mode"] = mode
        open_video = st.button("打开视频链接", key="ref_open_video", type="primary", width="stretch", disabled=_busy(ctx))

    with st.container(border=True, key="ref_script_origin"):
        _counter("识别的原始内容", st.session_state["ref_original_text"], maximum=10000)
        st.text_area(
            "识别的原始内容", key="ref_original_text", height=202, max_chars=10000,
            label_visibility="collapsed", disabled=_busy(ctx),
            placeholder="导入视频后，识别的口播原文将显示在这里。", persist_state="session",
        )
    with st.container(border=True, key="ref_script_editor"):
        st.markdown('<div class="ref-card-title"><span>视频文案编辑</span></div>', unsafe_allow_html=True)
        _counter("文章内容", st.session_state["ref_script_text"], subtitle=True)
        st.text_area(
            "文章内容", key="ref_script_text", height=305, max_chars=12000,
            label_visibility="collapsed", disabled=_busy(ctx),
            on_change=_commit_script, args=(ctx,), persist_state="session",
        )
        with st.container(key="ref_script_footer"):
            length, language = st.columns([1, 1.05], gap="small")
            length.number_input("目标字数", min_value=100, max_value=1500, step=50, key="ref_target_length", label_visibility="collapsed", disabled=_busy(ctx), persist_state="session")
            language.selectbox("语言", ["中文（普通话）"], key="ref_script_language", label_visibility="collapsed", disabled=_busy(ctx), persist_state="session")
            write, rewrite = st.columns(2, gap="small")
            if write.button("撰写文案", key="ref_write_script", type="primary", width="stretch", disabled=_busy(ctx), help="使用现有设置中的文案模型创作。"):
                _generate(ctx, rewrite=False)
            if rewrite.button("AI洗稿", key="ref_rewrite_script", type="primary", width="stretch", disabled=_busy(ctx), help="使用现有文案模型改写已提供的正文。"):
                _generate(ctx, rewrite=True)
            review, report = st.columns(2, gap="small")
            if review.button("AI法务", key="ref_review_script", width="stretch",
                             disabled=_busy(ctx) or not st.session_state["ref_script_text"].strip(),
                             help="审阅当前文案的潜在风险，查看建议后再决定采用。"):
                _request_review(ctx)
            if report.button("查看报告", key="ref_review_report", width="stretch",
                             disabled=not st.session_state.get("ref_script_review_result")):
                st.session_state["ref_script_review_open"] = True
    if notice := st.session_state.pop("ref_script_notice", None):
        st.error(notice)
    if open_video:
        st.session_state["ref_script_dialog"] = "video"
    elif mode != previous_mode and mode == "IP学习":
        st.session_state["ref_script_dialog"] = "ip"
    elif mode != previous_mode and mode == "爆款文案":
        st.session_state["ref_script_dialog"] = "library"
    dialog = st.session_state.get("ref_script_dialog")
    if dialog:
        {"video": _video_dialog, "ip": _ip_dialog, "library": _library_dialog}[dialog](ctx)
    elif st.session_state.get("ref_script_review_open"):
        _review_dialog(ctx)
