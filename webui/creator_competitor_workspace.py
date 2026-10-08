"""A spoken-script and visual-clue library for research before creation."""
from __future__ import annotations

import streamlit as st

from app.services.creator import competitors, spoken_library


def _settings():
    saved = competitors.get_settings()
    profile_names = {row["industry_id"]: row["industry_name"] for row in spoken_library.list_profiles()}
    current_profile = saved.get("spoken_profile", "spoken_general")
    if current_profile not in profile_names:
        current_profile = "spoken_general"
    with st.form("competitor_settings_form"):
        template_col, industry_col = st.columns(2, gap="medium")
        profile = template_col.selectbox("口播模板", list(profile_names), index=list(profile_names).index(current_profile),
                                          format_func=profile_names.get, key="competitor_spoken_profile")
        industry = industry_col.text_input("行业", value=saved["industry"], key="competitor_industry")
        source_col, keyword_col = st.columns([3, 2], gap="medium")
        urls = source_col.text_area("公开来源链接（每行一条）", value="\n".join(row["url"] for row in saved["sources"]),
                                   height=110, key="competitor_sources", placeholder="粘贴抖音、小红书作品链接或公开文章链接",
                                   help="填写无需登录即可查看的作品或网页，不填写发布后台或音视频文件。")
        keywords = keyword_col.text_area("调研关键词（每行一个，可选）", value="\n".join(saved["keywords"]), height=110,
                                         key="competitor_keywords", placeholder="育儿\n家装\n职场",
                                         help="用于标记和匹配参考内容。仅填写关键词不会自动查询平台热榜。")
        with st.expander("更新与筛选设置"):
            city_col, limit_col = st.columns(2, gap="medium")
            city = city_col.text_input("城市（留空表示不限）", value=saved["city"], key="competitor_city")
            limit = limit_col.number_input("每次最多读取条数", min_value=1, max_value=100, value=saved["max_items"], key="competitor_max_items")
            update_col, interval_col = st.columns(2, gap="medium")
            with update_col:
                automatic = st.checkbox("软件运行时自动更新参考库", value=bool(saved["sources"]) and saved["auto_update"],
                                         key="competitor_auto_update", disabled=not bool(urls.strip()))
                st.caption("添加来源并保存后可开启。")
            interval = interval_col.selectbox("更新间隔", [1, 6, 12, 24, 48, 168], index=[1, 6, 12, 24, 48, 168].index(saved["interval_hours"]) if saved["interval_hours"] in [1, 6, 12, 24, 48, 168] else 3,
                                               format_func=lambda value: f"每 {value} 小时", key="competitor_interval")
            minimum_col, preference_col = st.columns(2, gap="medium")
            minimum = minimum_col.number_input("口播正文最少字数", min_value=0, max_value=12000, value=saved.get("min_text_length", 80),
                                      step=10, key="competitor_min_text_length", help="默认 80 字。短于此值的片段保留来源记录，但不送入写稿参考；0 表示关闭长度过滤。")
            prefer_comments = preference_col.checkbox("优先参考已取得的高评论内容", value=saved.get("prefer_high_comment", True),
                                                      key="competitor_prefer_high_comment", help="只使用公开页实际提供的评论数；缺失时显示未取得，不推算热度。")
        submitted = st.form_submit_button("保存采集设置", type="primary")
    if submitted:
        try:
            # Retain existing selectors and labels without exposing crawler JSON.
            prior = {row["url"]: row for row in saved["sources"]}
            sources = [{**prior.get(url.strip(), {}), "url": url.strip()} for url in urls.splitlines() if url.strip()]
            competitors.save_settings({"sources": sources, "keywords": [word.strip() for word in keywords.splitlines() if word.strip()],
                                        "city": city, "industry": industry, "auto_update": automatic and bool(sources),
                                        "interval_hours": interval, "max_items": int(limit), "spoken_profile": profile,
                                        "min_text_length": int(minimum), "prefer_high_comment": prefer_comments})
            competitors.ensure_scheduler()
            st.success("设置已保存，可以更新参考库了。")
        except (ValueError, TypeError) as exc:
            st.error(str(exc))


@st.fragment(run_every="5s")
def _status():
    saved = competitors.get_settings()
    if saved.get("auto_paused"):
        st.warning("自动更新已暂停：" + saved.get("pause_reason", "请核查公开来源。"))
        st.caption("请确认来源可以公开查看，或更换链接后保存。")
    if saved["auto_update"] and saved["sources"] and not saved.get("auto_paused"):
        st.caption(f"自动更新已开启 · 软件运行时每 {saved['interval_hours']} 小时更新一次")
    runs = competitors.list_runs()
    if not runs:
        return
    latest = runs[0]
    state = latest.get("state")
    ident = latest.get("id")
    if state in {"queued", "running"} and ident:
        st.session_state["competitor_pending_run"] = ident
    elif ident and state in {"done", "failed", "needs_user", "interrupted"} and st.session_state.get("competitor_pending_run") == ident:
        st.session_state.pop("competitor_pending_run", None)
        # Refresh the library once when a background run ends, retaining form edits.
        st.rerun(scope="app")
    st.write(latest.get("message", ""))
    if state in {"queued", "running"}:
        st.progress(min(1.0, max(0.0, float(latest.get("progress", 0)) / 100)))
    elif state in {"failed", "needs_user", "interrupted"}:
        st.caption("本次采集未完整完成；之前保存的参考资料仍然可用。")
    if latest.get("filtered") is not None:
        st.caption(f"本次过滤 {latest['filtered']} 条；被过滤的内容保留来源记录，不送入写稿参考。")
    reasons = latest.get("filter_reasons", {})
    if reasons or latest.get("errors"):
        with st.expander("本次更新详情"):
            if isinstance(reasons, dict):
                for reason, count in reasons.items():
                    st.write(f"{reason}：{count} 条")
            for error in latest.get("errors", []):
                st.write(error.get("message", "未读取"))
                st.caption(error.get("source_url", ""))


def _texts(values):
    if isinstance(values, str):
        return [values] if values.strip() else []
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("description") or value.get("text") or ""
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
    return result


def _metric(item, name, legacy):
    metrics = item.get("hot_metrics")
    value = metrics.get(name) if isinstance(metrics, dict) else None
    if value is None:
        value = item.get(legacy)
    return "未取得" if value is None or isinstance(value, bool) or value == "" else str(value)


def _library():
    keyword = st.text_input("筛选参考内容", key="competitor_library_filter", placeholder="按标题、关键词、城市或行业筛选")
    items = competitors.list_items(keyword)
    st.caption(f"共 {len(items)} 条参考记录")
    if not items:
        with st.container(key="competitor_empty_state"):
            st.markdown("**还没有参考内容**")
            st.caption("① 添加公开作品或网页链接，保存设置。\n\n② 点击“立即更新参考库”，整理口播脚本和画面线索。")
        return
    names = {"douyin": "抖音", "xiaohongshu": "小红书", "web": "公开网页"}
    field_names = {"title": "标题", "public_caption": "公开文案/摘要", "comments": "评论片段", "likes": "点赞", "published_at": "发布时间"}
    for item in items[:50]:
        with st.expander((item.get("title") or "未公开标题")[:100]):
            platform = item.get("platform", "web")
            st.caption(f"{names.get(platform, platform)} · {item.get('industry') or '通用'} · {item.get('city') or '城市不限'}")
            if item.get("eligible") is False:
                st.info("已过滤，仅保留来源记录：" + "；".join(_texts(item.get("filter_reasons")) or ["未满足当前口播筛选条件"]))
            hook = item.get("hook_3s") or item.get("hook")
            if hook:
                st.markdown("**开头钩子**")
                st.write(hook)
            points = _texts(item.get("bullet_points"))
            clues = _texts(item.get("material_clues"))
            point_col, clue_col = st.columns(2, gap="medium")
            with point_col:
                if points:
                    st.markdown("**讲述要点**")
                    for point in points:
                        st.write("• " + point)
            with clue_col:
                if clues:
                    st.markdown("**画面线索**")
                    for clue in clues:
                        st.write("• " + clue)
            tags = _texts(item.get("tags"))
            if tags:
                st.caption("素材标签：" + "、".join(tags))
            if not hook and not points and not clues:
                st.caption("这是此前保存的公开参考，尚无口播拆解；原文和来源仍可查看。")
            content = item.get("spoken_script") or item.get("full_content") or item.get("public_caption")
            if content:
                with st.expander("查看口播原文 / 公开摘要"):
                    st.write(content)
            if item.get("comments"):
                with st.expander("页面可见评论片段"):
                    for comment in _texts(item["comments"]):
                        st.write(comment)
            source = item.get("source_url", "")
            if isinstance(source, str) and source.startswith(("https://", "http://")):
                st.link_button("查看原文来源", source)
            else:
                st.caption("原文来源：未取得")
            st.caption(f"公开点赞：{_metric(item, 'like', 'likes')} · 公开评论数：{_metric(item, 'comment', 'comment_count')} · 公开收藏数：{_metric(item, 'collect', 'collect_count')}")
            st.caption(f"发布时间：{item.get('published_at') or '未取得'} · 读取时间：{item.get('fetched_at') or '未取得'}")
            if item.get("missing_fields"):
                st.caption("未读取字段：" + "、".join(field_names.get(field, field) for field in item["missing_fields"]))
            retained = [field for field in item.get("latest_missing_fields", []) if field not in item.get("missing_fields", [])]
            if retained:
                st.caption("本次未公开、沿用此前记录：" + "、".join(field_names.get(field, field) for field in retained))


def render():
    header = st.container(key="competitor_header")
    with st.container(border=True, key="competitor_settings_card"):
        st.subheader("参考来源")
        _settings()
    saved = competitors.get_settings()
    # Fill the header after saving so its source count matches the active form.
    with header:
        title_col, state_col = st.columns([4, 2], gap="medium", vertical_alignment="center")
        with title_col:
            st.header("口播参考库")
            st.caption("整理开头、讲述要点和画面线索，让选题与写稿有据可依。")
        with state_col:
            if not saved["sources"]:
                st.caption("尚未配置来源")
            elif saved.get("auto_paused"):
                st.caption("自动更新已暂停")
            else:
                st.caption(f"已配置 {len(saved['sources'])} 个来源 · {'自动更新' if saved['auto_update'] else '手动更新'}")
    with st.container(border=True, key="competitor_library_card"):
        with st.container(key="competitor_results_toolbar"):
            heading_col, action_col = st.columns([4, 2], gap="medium", vertical_alignment="center")
            heading_col.subheader("脚本与画面线索")
            with action_col:
                if st.button("立即更新参考库", key="competitor_collect_now", disabled=not bool(saved["sources"]), use_container_width=True):
                    try:
                        st.session_state["competitor_pending_run"] = competitors.submit_collection()
                        st.success("参考更新已排队。")
                    except (ValueError, competitors.CollectionError) as exc:
                        st.error(str(exc))
        if not competitors.dependency_ready():
            st.warning("参考读取组件尚未就绪，已有内容仍可使用。")
        with st.container(key="competitor_collection_status"):
            _status()
        _library()
    st.caption("公开摘要不等于完整口播转写；互动数据未取得时会如实标记，不采集发布后的表现数据。")
