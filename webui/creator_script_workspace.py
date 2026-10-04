"""The manual's first step: discover a topic, write, edit, then hand off."""
from __future__ import annotations

from copy import deepcopy
from html import escape
from pathlib import Path
from urllib.parse import quote

import streamlit as st

from app.services.creator import jobs, store, topics


def _config_snapshot():
    from app.config import config
    return deepcopy(config.snapshot_config_with_pending(config.app))


def _enqueue(label, operation, action, **kwargs):
    try:
        ident = jobs.submit(label, operation, app_config=_config_snapshot(), **kwargs)
        st.session_state["creator_script_pending"] = {"id": ident, "action": action}
        st.session_state["creator_last_job"] = ident
    except Exception as exc:
        st.error(str(exc))


def _load_draft(row):
    st.session_state["creator_script_draft_id"] = row["id"]
    st.session_state["creator_script_editor"] = row.get("text", "")
    st.session_state["creator_script_editor_buffer"] = row.get("text", "")
    st.session_state["creator_script_title"] = row.get("title", "新文案")
    st.session_state["creator_script_context"] = dict(row)
    st.session_state["creator_script_style"] = row.get("style") if row.get("style") in topics.STYLE_PRESETS else next(iter(topics.STYLE_PRESETS))
    st.session_state["creator_script_length"] = row.get("target_length", 400)
    st.session_state["creator_script_instructions"] = row.get("instructions", "")
    st.session_state["creator_script_account"] = row.get("account_id")
    st.session_state["creator_script_view"] = "文案编辑"


def _compose(row=None, mode="original", kind="manual"):
    row = row or {}
    st.session_state["creator_script_context"] = {
        "mode": mode,
        "reference_text": row.get("text", "") if kind == "reference" else "",
        "topic_id": row.get("id") if kind == "topic" else None,
        "source_label": row.get("source_label", "直接创作"),
    }
    st.session_state["creator_script_draft_id"] = None
    st.session_state["creator_script_title"] = row.get("title", "")
    st.session_state["creator_script_editor"] = ""
    st.session_state["creator_script_editor_buffer"] = ""
    st.session_state["creator_script_style"] = next(iter(topics.STYLE_PRESETS))
    st.session_state["creator_script_length"] = 400
    st.session_state["creator_script_instructions"] = ""
    st.session_state["creator_script_view"] = "文案编辑"


def _collect_result():
    pending = st.session_state.get("creator_script_pending")
    if not pending:
        return
    row = jobs.get_job(pending["id"])
    if not row or row["state"] in {"queued", "running"}:
        return
    st.session_state.pop("creator_script_pending", None)
    if row["state"] != "done":
        st.session_state["creator_script_notice"] = ("error", row.get("message", "任务未完成，请重试。"))
        return
    result = row.get("result")
    if pending["action"] == "douyin" and isinstance(result, list):
        st.session_state["creator_douyin_batch"] = [item["id"] for item in result]
        st.session_state["creator_script_notice"] = ("success", f"已获取 {len(result)} 条抖音搜索结果，数据以平台返回为准。")
    elif pending["action"] == "reference" and isinstance(result, dict):
        st.session_state["creator_script_view"] = "参考文案"
        st.session_state["creator_script_notice"] = ("success", "口播原文已提取并加入参考库，可以查看全文和仿写。")
    elif pending["action"] == "search" and isinstance(result, list):
        st.session_state["creator_script_batch"] = [item["id"] for item in result]
        st.session_state["creator_script_notice"] = ("success", f"已生成 {len(result)} 个选题，选择一个开始写文案。")
    elif pending["action"] == "draft" and isinstance(result, dict) and result.get("text") and result.get("id"):
        _load_draft(result)
        st.session_state["creator_script_notice"] = ("success", "文案已生成并保存，可以直接修改。")


@st.fragment(run_every="2s")
def _progress():
    pending = st.session_state.get("creator_script_pending")
    if not pending:
        return
    row = jobs.get_job(pending["id"])
    if row and row["state"] not in {"queued", "running"}:
        st.rerun(scope="app")
    if row:
        st.info(row.get("message", "正在生成，请稍候。"))
        st.progress(float(row.get("progress", 0)) / 100)


def _save_current():
    text = st.session_state.get("creator_script_editor", st.session_state.get("creator_script_editor_buffer", ""))
    st.session_state["creator_script_editor_buffer"] = text
    title = st.session_state.get("creator_script_title", "") or "未命名文案"
    if len(title.strip()) > 300:
        raise ValueError("文案标题最多支持 300 个字符。")
    ident = st.session_state.get("creator_script_draft_id")
    if ident:
        # Save the controls as well: regenerate uses the currently selected style.
        topics.update_draft(ident, text)
        row = store.update_record("drafts", ident, {
            "title": title,
            "style": st.session_state.get("creator_script_style", next(iter(topics.STYLE_PRESETS))),
            "target_length": st.session_state.get("creator_script_length", 400),
            "instructions": st.session_state.get("creator_script_instructions", ""),
            "account_id": st.session_state.get("creator_script_account"),
        })
    else:
        context = st.session_state.get("creator_script_context", {})
        row = topics.save_draft(title, text,
            mode=context.get("mode", "original"),
            reference_text=context.get("reference_text", ""),
            topic_id=context.get("topic_id"),
            account_id=st.session_state.get("creator_script_account"),
            style=st.session_state.get("creator_script_style", next(iter(topics.STYLE_PRESETS))),
            target_length=st.session_state.get("creator_script_length", 400),
            instructions=st.session_state.get("creator_script_instructions", ""),
            source_label=context.get("source_label", "直接创作"))
    st.session_state["creator_script_draft_id"] = row["id"]
    st.session_state["creator_script_context"] = dict(row)
    return row


def _handoff(destination):
    try:
        row = _save_current()
        text = row["text"]
        st.session_state["creator_duix_script"] = text
        st.session_state["creator_voice_text"] = text
        st.session_state["creator_voice_draft"] = text
        if destination == "voice":
            st.session_state["creator_selected_tab"] = "配音"
        else:
            st.session_state["video_script"] = text
            st.session_state["creator_route"] = "视频制作"
        st.session_state["creator_navigation_pending"] = True
    except Exception as exc:
        st.session_state["creator_script_notice"] = ("error", str(exc))


@st.dialog("查看全文", width="large")
def _reference_dialog(row):
    st.subheader(row["title"])
    st.caption(row.get("source_label", "本地参考文案") + (" · " + row["author"] if row.get("author") else ""))
    st.text_area("参考原文", value=row["text"], height=330, disabled=True, key="creator_reference_full_" + row["id"])
    if row.get("source_url"):
        st.link_button("查看来源", row["source_url"])
    c1, c2 = st.columns(2)
    if c1.button("基于原文仿写", key="creator_ref_dialog_imitate", width="stretch"):
        _compose(row, "imitate", "reference")
        st.rerun(scope="app")
    if c2.button("选题原创", key="creator_ref_dialog_original", type="primary", width="stretch"):
        _compose(row, "original", "reference")
        st.rerun(scope="app")


@st.dialog("选题详情", width="large")
def _topic_dialog(row):
    st.subheader(row["title"])
    st.caption("AI 选题建议")
    st.write("**开头切入**")
    st.write(row.get("hook", ""))
    st.write("**内容角度**")
    st.write(row.get("reason", ""))
    if st.button("选择风格，创作原稿", type="primary", width="stretch"):
        _compose(row, "original", "topic")
        st.rerun(scope="app")


def _cards(rows, kind):
    for offset in range(0, len(rows), 3):
        columns = st.columns(3, gap="medium")
        for col, row in zip(columns, rows[offset:offset + 3]):
            with col, st.container(border=True, key="script_card_" + row["id"]):
                badge = "参考文案" if kind == "reference" else "AI 选题"
                title = escape(row["title"])
                summary = escape((row.get("text", "") if kind == "reference" else row.get("hook", ""))[:95])
                st.markdown(f'<div class="script-card"><span class="script-badge">{badge}</span><h3>{title}</h3><p>{summary}</p></div>', unsafe_allow_html=True)
                if st.button("查看全文" if kind == "reference" else "查看选题", key="script_view_" + row["id"], width="stretch"):
                    (_reference_dialog if kind == "reference" else _topic_dialog)(row)
                if kind == "reference":
                    left, right = st.columns(2)
                    left.button("仿写", key="script_imitate_" + row["id"], on_click=_compose, args=(row, "imitate", kind), width="stretch")
                    right.button("选题原创", key="script_original_" + row["id"], on_click=_compose, args=(row, "original", kind), type="primary", width="stretch")
                else:
                    st.button("选题原创", key="script_original_" + row["id"], on_click=_compose, args=(row, "original", kind), type="primary", width="stretch")


def _extract_reference(link, title, keyword, result_id=None, app_config=None, progress=None):
    from app.services.creator import extract
    media = extract.download_media(link, progress=progress)
    result = extract.extract_media(media, progress=progress)
    reference = topics.save_reference(title or Path(result["media_path"]).stem, result["text"], keyword=keyword, source_url=link)
    if result_id and store.get_record("douyin_results", result_id):
        store.update_record("douyin_results", result_id, {"reference_id": reference["id"], "has_text": True})
    return reference


def _douyin_settings():
    from app.config import config
    settings = _config_snapshot().get("creator_douyin_search", {})
    st.caption("已有抖音开放平台应用并获批视频搜索权限后，可在这里连接。没有权限时先使用网页搜索。")
    with st.form("creator_douyin_settings_form"):
        client_key = st.text_input("应用 Client Key", value=settings.get("client_key", ""))
        client_secret = st.text_input("应用 Client Secret", type="password", help="留空时保留已保存的密钥。")
        device = st.text_input("设备 ID", value=str(settings.get("device_id", "")))
        if st.form_submit_button("保存接口设置"):
            try:
                if not client_key.strip() or not (client_secret.strip() or settings.get("client_secret")):
                    raise ValueError("请填写应用信息。")
                if not device.strip().isdigit() or not 0 < int(device) < 2**63:
                    raise ValueError("设备 ID 必须为有效正整数。")
                updated = {"client_key": client_key.strip(), "client_secret": client_secret.strip() or settings.get("client_secret", ""), "device_id": int(device)}
                config.update_config_nonblocking(config.app, "creator_douyin_search", updated)
                config.try_save_config()
                st.success("接口设置已提交保存。请确认应用已经获得视频搜索权限。")
            except Exception as exc:
                st.error(str(exc))
    st.link_button("查看抖音官方接入说明", "https://developer.open-douyin.com/product/search")


def _douyin_entry(keyword):
    from app.services.creator import douyin_search
    with st.expander("从抖音寻找真实选题"):
        query = keyword.strip()
        st.link_button("在抖音搜索真实视频 ↗", "https://www.douyin.com/search/" + quote(query or "教育", safe="") + "?type=video", width="stretch")
        st.caption("在抖音网页中筛选发布时间与排序，再复制选中的视频分享链接。登录或验证需要你在抖音页面完成。")
        with st.form("creator_douyin_reference_link"):
            link = st.text_input("选中视频的分享链接", placeholder="粘贴抖音分享链接")
            title = st.text_input("参考标题（可选）", placeholder="方便以后在参考库中找到它")
            if st.form_submit_button("提取口播并加入参考库", disabled=bool(st.session_state.get("creator_script_pending"))):
                if not link.strip():
                    st.warning("请粘贴视频链接。")
                else:
                    _enqueue("抖音参考文案提取", _extract_reference, "reference", link=link.strip(), title=title.strip(), keyword=query)
                    st.rerun()
        st.divider()
        configured = douyin_search.is_configured(_config_snapshot())
        if st.button("官方接口搜索 · 近 30 天 / 点赞排序", disabled=not configured or not query or bool(st.session_state.get("creator_script_pending")), width="stretch"):
            st.session_state["creator_script_query"] = query
            st.session_state.pop("creator_douyin_batch", None)
            _enqueue("抖音官方搜索 · " + query, douyin_search.search, "douyin", keyword=query, count=9, days=30, sort="likes")
            st.rerun()
        st.caption("官方搜索最多读取前 100 条后筛选近 30 天，并按返回的点赞数排序；不代表整个平台的爆款榜。")
        with st.expander("连接抖音官方搜索接口"):
            _douyin_settings()


def _douyin_cards(query):
    rows = [r for r in store.list_records("douyin_results") if r.get("keyword", "").casefold() == query.casefold()]
    batch = st.session_state.get("creator_douyin_batch")
    if batch is not None:
        by_id = {r["id"]: r for r in rows}
        rows = [by_id[ident] for ident in batch if ident in by_id]
    else:
        rows.sort(key=lambda r: (r.get("digg_count") is not None, r.get("digg_count") or 0), reverse=True)
    if not rows:
        return
    st.markdown("#### 抖音真实搜索结果")
    for offset in range(0, min(len(rows), 12), 3):
        for col, row in zip(st.columns(3), rows[offset:offset + 3]):
            with col, st.container(border=True):
                st.write(row.get("title", "抖音视频"))
                likes = row.get("digg_count")
                st.caption(row.get("author", "") + (f" · 点赞 {likes:,}" if isinstance(likes, int) else " · 点赞数据未提供"))
                if row.get("source_url"):
                    st.link_button("查看原视频", row["source_url"], width="stretch")
                reference = store.get_record("references", row["reference_id"]) if row.get("reference_id") else None
                if reference:
                    if st.button("查看口播原文", key="creator_douyin_full_" + row["id"], width="stretch"):
                        _reference_dialog(reference)
                    st.button("仿写", key="creator_douyin_write_" + row["id"], on_click=_compose, args=(reference, "imitate", "reference"), type="primary", width="stretch")
                else:
                    st.caption("平台未提供全文，可提取视频口播或手动导入原文。")
                    if st.button("提取口播原文", key="creator_douyin_extract_" + row["id"], disabled=bool(st.session_state.get("creator_script_pending")), width="stretch"):
                        _enqueue("提取抖音参考原文", _extract_reference, "reference", link=row["source_url"], title=row.get("title", ""), keyword=query, result_id=row["id"])
                        st.rerun()


def _search():
    searched = st.session_state.get("creator_script_query", "")
    if not searched:
        st.markdown('<div class="script-hero"><div class="script-eyebrow">STEP 01 · 选题和文案</div><h1>你做哪个行业？</h1><h2>从一个选题，开始你的下一条视频。</h2><p>输入行业或赛道关键词，寻找参考，写出适合你的口播文案。</p></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="script-eyebrow">STEP 01 · 选题和文案</div>', unsafe_allow_html=True)
        st.subheader("为下一条视频找到方向")
    with st.container(key="creator_script_search"):
        with st.form("creator_script_search_form"):
            cols = st.columns([6, 1, 1.5, 1.8], vertical_alignment="bottom")
            keyword = cols[0].text_input("行业或赛道关键词", placeholder="例如：教育、玻璃门、家居装修", key="creator_script_keyword")
            count = cols[1].selectbox("数量", [6, 9, 12], key="creator_script_count")
            find = cols[2].form_submit_button("搜索参考库", width="stretch")
            generate = cols[3].form_submit_button("AI 拓展选题", type="primary", width="stretch", disabled=bool(st.session_state.get("creator_script_pending")))
        if find or generate:
            if not keyword.strip():
                st.warning("请输入一个行业或赛道关键词。")
            else:
                st.session_state["creator_script_query"] = keyword.strip()
                st.session_state.pop("creator_script_batch", None)
                if generate:
                    _enqueue("关键词选题 · " + keyword.strip(), topics.search_topics, "search", keyword=keyword.strip(), count=count, account_id=st.session_state.get("creator_script_account"))
                st.rerun()
    st.caption("参考库搜索你导入的真实文案；AI 拓展选题生成创作建议。近期热度和播放量需要接入数据源。")
    _douyin_entry(keyword)
    query = st.session_state.get("creator_script_query", "")
    if not query:
        with st.container(key="creator_script_start_links", horizontal=True, horizontal_alignment="center"):
            st.button("我有参考文案", on_click=lambda: st.session_state.update(creator_script_view="参考文案"))
            st.button("直接写文案", on_click=_compose)
        return
    refs = topics.list_references(query)
    batch = st.session_state.get("creator_script_batch")
    suggestions = [r for r in topics.list_topics() if r.get("source") == "ai" and r.get("keyword", "").casefold() == query.casefold()]
    if batch is not None:
        suggestions = [r for r in suggestions if r["id"] in batch]
    brief, results = st.columns([1, 3.4], gap="large")
    with brief, st.container(key="creator_script_brief"):
        st.markdown(f'<div class="script-brief"><span>当前赛道</span><h2>{escape(query)}</h2><p>先选方向<br>再写出你的表达</p></div>', unsafe_allow_html=True)
        st.caption(f"参考文案 {len(refs)} 条 · AI 选题 {len(suggestions[:12])} 条")
        st.button("导入参考文案", width="stretch", on_click=lambda: st.session_state.update(creator_script_view="参考文案"))
    with results:
        _douyin_cards(query)
        if refs:
            st.markdown("#### 参考文案")
            _cards(refs[:12], "reference")
        if suggestions:
            st.markdown("#### AI 选题建议")
            _cards(suggestions[:12], "topic")
        if not refs and not suggestions and not st.session_state.get("creator_script_pending"):
            st.info("参考库中还没有匹配文案。可以导入参考，或点击「AI 拓展选题」开始创作。")


def _references():
    st.subheader("把好的参考，变成你的创作起点")
    st.caption("保存原文与来源，再查看全文、仿写或按选题原创。")
    with st.expander("导入参考文案", expanded=not topics.list_references()):
        with st.form("creator_reference_form", clear_on_submit=True):
            left, right = st.columns(2)
            title = left.text_input("参考标题")
            keyword = right.text_input("行业关键词")
            text = st.text_area("参考原文", height=200, placeholder="粘贴参考文案，或先提取视频中的口播文字。")
            left, right = st.columns(2)
            url = left.text_input("来源链接（可选）")
            author = right.text_input("作者（可选）")
            if st.form_submit_button("保存到参考库", type="primary"):
                try:
                    topics.save_reference(title, text, keyword=keyword, source_url=url, author=author)
                    st.success("参考文案已保存。")
                except Exception as exc:
                    st.error(str(exc))
    with st.expander("从已提取的口播文案导入"):
        extracts = store.list_records("extracts")
        if extracts:
            selected = st.selectbox("选择已提取文案", extracts, format_func=lambda r: Path(r.get("media_path", "参考视频")).name, key="creator_reference_extract")
            title = st.text_input("文案标题", key="creator_reference_extract_title")
            keyword = st.text_input("归属行业", key="creator_reference_extract_keyword")
            if st.button("加入参考库"):
                try:
                    topics.save_reference(title or Path(selected.get("media_path", "参考文案")).stem, selected.get("text", ""), keyword=keyword)
                    st.success("已加入参考库。")
                except Exception as exc:
                    st.error(str(exc))
        else:
            st.caption("还没有提取记录。可以先上传视频或分享链接提取口播。")
        st.button("去提取视频文案", on_click=lambda: st.session_state.update(creator_selected_tab="文案提取", creator_navigation_pending=True))
    filter_text = st.text_input("搜索参考文案", placeholder="搜索标题、行业或原文", key="creator_reference_filter")
    rows = topics.list_references(filter_text)
    if rows:
        _cards(rows[:30], "reference")
    else:
        st.caption("参考库为空，保存第一篇参考文案后即可仿写。")


def _drafts():
    st.subheader("我的文案")
    st.button("新建文案", type="primary", on_click=_compose)
    rows = topics.list_drafts()
    if not rows:
        st.caption("保存后的稿件会出现在这里，关闭页面后仍可继续编辑。")
    for row in rows[:50]:
        with st.container(border=True):
            left, right = st.columns([5, 1], vertical_alignment="center")
            left.markdown("**" + row.get("title", "未命名文案") + "**")
            left.caption(f'{row.get("style", "自写文案")} · {len(row.get("text", ""))} 字 · 版本 {row.get("version", 1)}')
            right.button("继续编辑", key="creator_draft_open_" + row["id"], on_click=_load_draft, args=(row,), width="stretch")


def _remember_editor():
    st.session_state["creator_script_editor_buffer"] = st.session_state.get("creator_script_editor", "")


def _editor():
    context = st.session_state.get("creator_script_context", {})
    pending = st.session_state.get("creator_script_pending", {})
    busy = pending.get("action") == "draft"
    st.markdown('<div class="script-eyebrow">STEP 01 · 编辑文案</div>', unsafe_allow_html=True)
    st.subheader("把选题写成你的表达")
    st.session_state.setdefault("creator_script_editor_buffer", st.session_state.get("creator_script_editor", ""))
    # Keep an independent manuscript when an interrupted rerun cleans up the
    # textarea widget. Its change callback commits intentional edits, including
    # clearing the text, before this buffer restores the next render.
    st.session_state["creator_script_editor"] = st.session_state["creator_script_editor_buffer"]
    with st.container(border=True, key="creator_script_editor_panel"):
        st.text_input("文案标题 / 创作选题", key="creator_script_title", placeholder="输入你想讲的主题", disabled=busy, persist_state="session")
        left, middle, right = st.columns([2, 1, 2])
        mode = context.get("mode", "original")
        left.selectbox("创作风格", list(topics.STYLE_PRESETS), key="creator_script_style", disabled=busy, persist_state="session")
        middle.number_input("目标字数", min_value=100, max_value=1500, step=50, key="creator_script_length", disabled=busy, persist_state="session")
        right.caption("创作方式")
        right.write("参考仿写" if mode == "imitate" else "选题原创 / 直接输入")
        st.text_input("补充要求（可选）", key="creator_script_instructions", placeholder="例如：用新手能理解的表达，开头直接给结论", disabled=busy, persist_state="session")
        if context.get("reference_text"):
            with st.expander("查看参考原文"):
                st.text(context["reference_text"])
        editor = st.text_area("口播文案", height=360, key="creator_script_editor", placeholder="点击生成文案，或把准备好的文案直接粘贴到这里。", disabled=busy, on_change=_remember_editor, persist_state="session")
        count = len("".join(editor.split()))
        st.caption(f"当前 {count} 字 · 预计口播 {round(count / 4)} 秒（按每秒约 4 字估算） · 可手动编辑")
        left, middle, right = st.columns([1.25, 1, 1.6])
        generate = left.button("重新生成" if st.session_state.get("creator_script_draft_id") else "生成文案", key="creator_script_generate", width="stretch", disabled=busy)
        save = middle.button("保存文案", width="stretch", disabled=busy)
        right.button("确认文案，下一步配音 →", type="primary", width="stretch", disabled=busy, on_click=_handoff, args=("voice",))
        if save:
            try:
                _save_current()
                st.success("文案已保存。")
            except Exception as exc:
                st.error(str(exc))
        if generate:
            try:
                title = st.session_state.get("creator_script_title", "").strip()
                if not title:
                    raise ValueError("请先填写创作选题。")
                ident = st.session_state.get("creator_script_draft_id")
                if ident:
                    _save_current()
                    _enqueue("重新生成文案 · " + title, topics.regenerate_draft, "draft", ident=ident)
                else:
                    _enqueue("生成文案 · " + title, topics.generate_draft, "draft", title=title, mode=mode, style=st.session_state["creator_script_style"], reference_text=context.get("reference_text", ""), account_id=st.session_state.get("creator_script_account"), target_length=st.session_state["creator_script_length"], instructions=st.session_state["creator_script_instructions"], topic_id=context.get("topic_id"))
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        lower_left, lower_right = st.columns(2)
        lower_left.download_button("下载文案", editor, file_name=(st.session_state.get("creator_script_title", "口播文案") or "口播文案")[:80] + ".txt", mime="text/plain", disabled=not editor or busy)
        lower_right.button("送到视频制作", width="stretch", disabled=not editor or busy, on_click=_handoff, args=("video",))


def render():
    _collect_result()
    accounts = topics.list_accounts()
    with st.sidebar.expander("账号定位（可选）"):
        options = [None] + [a["id"] for a in accounts]
        names = {a["id"]: a["name"] for a in accounts}
        if st.session_state.get("creator_script_account") not in options:
            st.session_state["creator_script_account"] = None
        st.selectbox("写给哪个账号", options, format_func=lambda a: names.get(a, "暂不使用账号定位"), key="creator_script_account")
        st.caption("填写账号定位，可以让选题和文案更贴近你的受众。")
        st.button("管理账号定位", on_click=lambda: st.session_state.update(creator_selected_tab="账号选题", creator_navigation_pending=True))
    st.session_state.setdefault("creator_script_view", "关键词选题")
    st.session_state.setdefault("creator_script_style", next(iter(topics.STYLE_PRESETS)))
    st.session_state.setdefault("creator_script_length", 400)
    st.radio("选题与文案", ["关键词选题", "参考文案", "文案编辑", "我的文案"], key="creator_script_view", horizontal=True, label_visibility="collapsed")
    notice = st.session_state.pop("creator_script_notice", None)
    if notice:
        getattr(st, notice[0])(notice[1])
    _progress()
    {"关键词选题": _search, "参考文案": _references, "文案编辑": _editor, "我的文案": _drafts}[st.session_state["creator_script_view"]]()
