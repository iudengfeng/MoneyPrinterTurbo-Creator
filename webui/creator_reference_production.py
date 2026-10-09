"""Reference-layout production columns backed by the shared creator workflow."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from app.services.creator import avatar, narration


_SPEED_NAMES = {0.8: "舒缓", 1.0: "标准", 1.2: "快速"}
_HIGHLIGHT_FIELDS = {
    "主词": "ref_highlight_main",
    "描述词": "ref_highlight_description",
    "行动词": "ref_highlight_action",
    "情绪词": "ref_highlight_emotion",
}


def _existing_file(value):
    if not value:
        return None
    try:
        path = Path(str(value)).expanduser()
        return path if path.is_file() and path.stat().st_size else None
    except (OSError, ValueError):
        return None


def _busy(ctx):
    project = getattr(ctx, "project", None) or {}
    pending = getattr(ctx, "busy", False)
    if callable(pending):
        pending = pending()
    return bool(pending) or project.get("state") in {"queued", "running"}


def _options(provider):
    try:
        return provider.list_options()
    except (RuntimeError, ValueError, OSError) as exc:
        st.error(str(exc))
        return []


def _select_asset(label, rows, key, *, busy=False):
    by_id = {row["id"]: row for row in rows}
    if st.session_state.get(key) not in by_id:
        st.session_state[key] = None
    ident = st.selectbox(
        label, list(by_id), format_func=lambda value: by_id[value]["name"],
        index=None, placeholder=label, key=key, disabled=busy,
        label_visibility="collapsed", persist_state="session",
    )
    return by_id.get(ident)


def _stage_result(ctx, name):
    row = ctx.stage(name) or {}
    return row.get("result", row)


def _attempt(operation, *args, **kwargs):
    try:
        operation(*args, **kwargs)
    except Exception as exc:
        st.error(str(exc))


def _voice_request(ctx, option):
    text = str(st.session_state.get("ref_script_text", "")).strip()
    if not text:
        raise ValueError("请先在文案创作中输入完整口播文案。")
    if not option:
        raise ValueError("请选择可用音色；可在声音管理中保存音色或检查本机配音服务。")
    if option.get("provider") == "voxcpm" and not st.session_state.get("ref_allow_paid", False):
        raise ValueError("请先允许使用已配置的云端克隆服务，再生成配音。")
    ctx.queue(
        "voice_audio", "生成语音", narration.generate, text, option["id"],
        speed=float(st.session_state.get("ref_speed", 1.0)), emotion="自然",
    )


def _avatar_request(ctx, option):
    if st.session_state.get("ref_resolution", "720P") not in {"576P", "720P", "1080P"}:
        raise ValueError("请选择可用的导出分辨率。")
    if not option:
        raise ValueError("请选择形象；可在数字人管理中导入人物参考视频。")
    if not option.get("available", True):
        raise ValueError(option.get("reason") or "形象参考视频已缺失，请在数字人管理中重新导入。")
    state = avatar.status()
    if not state.get("available"):
        raise ValueError(state.get("reason") or "数字人服务尚未就绪，请检查本机 Duix 与 Docker 配置。")
    ctx.submit_stage("visuals", changes={
        "kind": "avatar", "avatar_id": option["id"],
        "avatar_mode": st.session_state.get("ref_avatar_mode", "mixed"),
        "allow_reference_reuse": False, "aspect": "9:16", "source_video_path": "",
    })


def _audio_preview(ctx):
    path = _existing_file(ctx.current_audio())
    try:
        rows = [row for row in narration.list_narrations() if _existing_file(row.get("audio_path"))]
    except (RuntimeError, ValueError, OSError) as exc:
        st.caption("音频历史暂时不可用：" + str(exc))
        rows = []
    with st.container(key="ref_audio_preview", height=100, border=True):
        if path:
            st.audio(str(path))
        else:
            st.caption("音频预览 / 历史")
            st.caption("暂无语音，生成后可在此试听")
        if rows:
            with st.popover("历史音频", use_container_width=True):
                _audio_history(ctx, rows)


def _audio_history(ctx, rows):
    if rows:
        with st.container():
            by_id = {row["id"]: row for row in rows[:20]}
            if st.session_state.get("ref_audio_history") not in by_id:
                st.session_state["ref_audio_history"] = None
            selected = st.selectbox(
                "选择完整配音", list(by_id), index=None, placeholder="选择历史音频",
                format_func=lambda ident: by_id[ident].get("voice_name", "配音") + " · " + by_id[ident].get("text", "")[:24],
                key="ref_audio_history", persist_state="session",
            )
            if selected:
                row = by_id[selected]
                st.audio(row["audio_path"])
                changed = row.get("text", "").strip() != st.session_state.get("ref_script_text", "").strip()
                if changed:
                    st.caption("这份配音对应另一篇文案，选用时会一起载入对应正文。")
                    st.text(row.get("text", ""))
                if st.button("使用配音及对应文案" if changed else "使用这份语音", key="ref_use_audio_history", width="stretch", disabled=_busy(ctx)):
                    st.session_state["ref_audio_choice_pending"] = {"project_id": ctx.project.get("id", ""), "audio_path": row["audio_path"], "text": row.get("text", "")}
                    st.rerun()


@st.dialog("视频预览", width="large", on_dismiss="rerun")
def _expanded_video(value, rendered=False):
    path = _existing_file(value)
    if not path:
        st.warning("视频文件已移动、删除或为空，请重新生成或导入。")
        return
    try:
        data = path.read_bytes()
    except OSError:
        st.warning("视频文件暂时无法读取，请重新生成或导入。")
        return
    with st.container(key="ref_video_dialog"):
        st.caption(("成片" if rendered else "口播视频") + " · " + path.name)
        st.video(data, width="stretch")
        st.download_button(
            "下载成片" if rendered else "下载口播视频", data=data, file_name=path.name,
            mime="video/mp4" if path.suffix.lower() == ".mp4" else None,
            key="ref_video_dialog_download_render" if rendered else "ref_video_dialog_download_avatar",
            width="stretch",
        )


def _video_preview(ctx, *, rendered=False):
    path = _existing_file(ctx.current_video(rendered=rendered))
    key = "ref_render_preview" if rendered else "ref_avatar_preview"
    with st.container(key=key, height=350, border=True):
        columns = st.columns([2.1, 1.4] if rendered else [2.1, 1.4, 1.2], gap="small", vertical_alignment="center")
        heading, expand = columns[:2]
        heading.markdown("**" + ("画面处理预览" if rendered else "口播视频预览") + "**")
        if expand.button(
            "放大预览", key="ref_render_preview_expand" if rendered else "ref_avatar_preview_expand",
            width="stretch", disabled=not path,
        ) and path:
            _expanded_video(str(path), rendered)
        if not rendered:
            with columns[2], st.popover("导入视频", use_container_width=True):
                _import_video(ctx)
        if path:
            heading.caption("当前已有视频")
            _, middle, _ = st.columns([1, 1.8, 1], gap="small")
            with middle:
                st.video(str(path), width="stretch")
        else:
            heading.caption("请先生成或导入视频")
            st.markdown('<div class="ref-portrait-empty"><span class="ref-preview-play">▧</span>'
                        '<strong>暂无视频预览</strong><small>生成或导入后在这里查看</small></div>', unsafe_allow_html=True)
    if path and rendered:
        st.download_button(
            "下载成片", data=path.read_bytes(), file_name="最终成片" + path.suffix,
            mime="video/mp4" if path.suffix.lower() == ".mp4" else None,
            key="ref_download_render", width="stretch",
        )


def _import_video(ctx):
    scope = str(st.session_state.get("ref_video_import_scope", "draft"))
    busy = _busy(ctx)
    upload = st.file_uploader("导入口播视频", type=["mp4", "mov", "mkv", "webm"],
                              key="ref_voice_video_upload_" + scope, disabled=busy)
    if upload:
        try:
            path = str(ctx.stage_upload(upload))
        except Exception as exc:
            st.error(str(exc))
            return
        failure = st.session_state.get("ref_video_import_failure") or {}
        project = getattr(ctx, "project", None) or {}
        if failure.get("project_id") == project.get("id", "") and failure.get("source_video_path") == path:
            st.warning(failure.get("message") or "视频导入未完成，请检查后重新导入。")
            if st.button("重新导入", key="ref_voice_video_retry_" + scope, width="stretch", disabled=busy):
                _apply_video_import(ctx, path)
        elif st.session_state.get("ref_imported_voice_video") != path and not busy:
            _apply_video_import(ctx, path)


def _apply_video_import(ctx, path):
    if _busy(ctx):
        st.info("当前任务正在处理，请完成后再导入。")
        return
    st.session_state["ref_imported_voice_video"] = path
    try:
        ctx.use_media("video", path)
    except Exception as exc:
        project = getattr(ctx, "project", None) or {}
        st.session_state["ref_video_import_failure"] = {
            "project_id": project.get("id", ""), "source_video_path": path, "message": str(exc),
        }
        st.error(str(exc))
        return
    st.session_state.pop("ref_video_import_failure", None)
    st.rerun()


def _resolution_picker(busy):
    if st.session_state.get("ref_resolution", "720P") not in {"576P", "720P", "1080P"}:
        st.session_state["ref_resolution"] = "720P"
        st.caption("已恢复默认 720P。")
    else:
        st.session_state.setdefault("ref_resolution", "720P")
    with st.container(key="ref_resolution"):
        columns = st.columns(3, gap="small")
        for column, resolution in zip(columns, ("576P", "720P", "1080P")):
            supported = resolution == st.session_state["ref_resolution"]
            if column.button(
                resolution, key="ref_resolution_" + resolution.lower(),
                type="primary" if supported else "secondary", width="stretch",
                disabled=busy,
            ):
                st.session_state["ref_resolution"] = resolution
                st.rerun()
    st.caption("选择导出尺寸；人物源画质取决于原素材，1080P 不增加原片细节。")


def render_voice_column(ctx):
    """Render voice selection, real narration and portrait video production."""
    busy = _busy(ctx)
    with st.container(key="ref_voice_card", border=True):
        heading, manage = st.columns([3, 1])
        heading.markdown("**音色**")
        if manage.button("音色库", key="ref_manage_voice", disabled=busy):
            st.session_state["ref_asset_browser"] = "voice"
        selection, action = st.columns([2.2, 1], gap="small", vertical_alignment="center")
        with selection:
            option = _select_asset("选择音色", _options(narration), "ref_voice_id", busy=busy)
        with action:
            if st.button("生成语音", key="ref_generate_voice", type="primary", width="stretch", disabled=busy):
                _attempt(_voice_request, ctx, option)
        if not option:
            st.caption("请选择音色")
        elif option.get("provider") == "voxcpm":
            st.session_state.setdefault("ref_allow_paid", False)
            st.checkbox("允许使用已配置的云端克隆服务", key="ref_allow_paid", disabled=busy, persist_state="session")

    with st.container(key="ref_voice_settings", border=True):
        st.markdown("**语音设置**")
        language, emotion, pace = st.columns([1.6, 1, 1], gap="small")
        with language:
            st.session_state.setdefault("ref_language", "zh")
            st.selectbox("语种", ["zh"], format_func=lambda _: "中文（普通话）", key="ref_language", disabled=True, persist_state="session")
        with emotion:
            st.session_state.setdefault("ref_emotion", "自然")
            st.selectbox("情绪", ["自然"], key="ref_emotion", disabled=True, persist_state="session")
        with pace:
            st.session_state.setdefault("ref_speed", 1.0)
            speeds = sorted({0.8, 1.0, 1.2, float(st.session_state["ref_speed"])})
            st.selectbox(
                "语速", speeds, format_func=lambda value: _SPEED_NAMES.get(value, f"{value:.2f}×"),
                key="ref_speed", disabled=busy, persist_state="session",
            )
    _audio_preview(ctx)

    with st.container(key="ref_avatar_card", border=True):
        heading, manage = st.columns([3, 1], gap="small", vertical_alignment="center")
        heading.markdown("**选择形象**")
        if manage.button("管理", key="ref_manage_avatar", width="stretch", disabled=busy):
            st.session_state["ref_asset_browser"] = "avatar"
        selection, action = st.columns([2.2, 1], gap="small", vertical_alignment="center")
        with selection:
            profile = _select_asset("请选择形象", _options(avatar), "ref_avatar_id", busy=busy)
        with action:
            if st.button("生成口播", key="ref_generate_avatar", type="primary", width="stretch", disabled=busy):
                _attempt(_avatar_request, ctx, profile)

    with st.container(key="ref_avatar_parameters", border=True):
        heading, options = st.columns([2, 1], gap="small", vertical_alignment="center")
        heading.markdown("**口播参数**")
        with options, st.popover("9:16 竖屏", use_container_width=True):
            st.session_state.setdefault("ref_avatar_mode", "mixed")
            st.radio("画面组织", ["mixed", "full"], horizontal=True,
                     format_func=lambda value: "人物 + 图文" if value == "mixed" else "全程人物",
                     key="ref_avatar_mode", disabled=busy, persist_state="session")
            st.caption("人物动作使用连续参考视频；素材不足时会提示补充，不循环人物画面。")
        _resolution_picker(busy)
    _video_preview(ctx)
    ctx.report_job_state()


def _music_upload(ctx, busy):
    upload = st.file_uploader(
        "上传背景音乐", type=["mp3", "wav", "m4a", "aac", "ogg"],
        key="ref_bgm_upload", disabled=busy, label_visibility="collapsed",
    )
    if upload:
        try:
            path = ctx.stage_upload(upload)
            if st.session_state.get("ref_imported_bgm") != path:
                st.session_state["ref_bgm_path"] = path
                st.session_state["ref_imported_bgm"] = path
        except Exception as exc:
            st.error(str(exc))
    path = _existing_file(st.session_state.get("ref_bgm_path"))
    if path:
        st.caption("已选择：" + path.name)


def _render_request(ctx):
    if st.session_state.get("ref_resolution", "720P") not in {"576P", "720P", "1080P"}:
        raise ValueError("请选择可用的导出分辨率。")
    enabled = bool(st.session_state.get("ref_bgm_enabled", False))
    bgm = _existing_file(st.session_state.get("ref_bgm_path")) if enabled else None
    if enabled and not bgm:
        raise ValueError("已开启背景音乐，请先上传可用音乐文件。")
    source = _stage_result(ctx, "visuals")
    if st.session_state.get("ref_subtitles", True) and source.get("subtitles_burned"):
        raise ValueError("原视频已经包含字幕，请关闭视频字幕，避免重复叠加。")
    mode = st.session_state.get("ref_creation_mode", "one_click")
    if mode not in {"one_click", "step_by_step"}:
        raise ValueError("请选择一键成片或分步制作。")
    ctx.submit_stage("release" if mode == "one_click" else "render", changes={
        "subtitle_style": st.session_state.get("ref_subtitle_style", "clean") if st.session_state.get("ref_subtitles", True) else "none",
        "bgm_path": str(bgm) if bgm else "",
        "bgm_volume": float(st.session_state.get("ref_bgm_volume", 0.12)),
        "color_grade": st.session_state.get("ref_color_grade", "none"),
        "video_fit": st.session_state.get("ref_video_fit", "contain"),
    })


def _dismiss_processing():
    st.session_state["ref_processing_open"] = False


@st.dialog("视频处理设置", width="medium", on_dismiss=_dismiss_processing)
def _processing_dialog(ctx):
    busy = _busy(ctx)
    with st.container(key="ref_processing_settings"):
        st.markdown("**01 画中画**")
        st.caption(f"已设置 {len(st.session_state.get('ref_pip_items', []))} 段素材，按真实口播时间出现。")
        if st.button("设置画中画", key="ref_open_pip", disabled=busy, width="stretch"):
            st.session_state["ref_pip_open"] = True
            st.session_state["ref_processing_open"] = False
            st.rerun(scope="app")
        st.markdown("**02 自动剪气口**")
        st.toggle("剪掉较长停顿", key="ref_silence_trim", disabled=busy, persist_state="session")
        if st.session_state.get("ref_silence_trim"):
            threshold, minimum = st.columns(2)
            threshold.number_input("静音阈值（dB）", -70., -15., key="ref_silence_threshold", disabled=busy, persist_state="session")
            minimum.number_input("最短静音（秒）", .2, 3., step=.1, key="ref_silence_min_duration", disabled=busy, persist_state="session")
            st.caption("保留短呼吸；音画、字幕和画中画一起调整。")
        st.markdown("**03 自动绿幕切换**")
        st.toggle("替换绿幕背景", key="ref_green_screen", disabled=busy, persist_state="session")
        if st.session_state.get("ref_green_screen"):
            background = st.file_uploader("上传背景图片或视频", type=["jpg", "png", "webp", "mp4", "mov"], key="ref_green_background_upload", disabled=busy)
            if background:
                st.session_state["ref_green_background_path"] = ctx.stage_upload(background)
            st.color_picker("绿幕颜色", key="ref_green_color", disabled=busy, persist_state="session")
            st.slider("颜色容差", .01, 1., key="ref_green_similarity", disabled=busy, persist_state="session")
            st.caption("适用于真实纯色幕布素材；保留原口播声音。")
        st.slider("画面柔化与亮肤", 0., .5, key="ref_beauty_strength", disabled=busy, persist_state="session")
        st.session_state.setdefault("ref_color_grade", "none")
        st.selectbox(
            "画面色调", ["none", "warm", "cool", "vivid"],
            format_func=lambda value: {"none": "原色", "warm": "暖色", "cool": "冷色", "vivid": "鲜明"}[value],
            key="ref_color_grade", disabled=busy, persist_state="session",
        )
        st.session_state.setdefault("ref_video_fit", "contain")
        st.selectbox(
            "画面适配", ["contain", "cover"],
            format_func=lambda value: "保留完整画面" if value == "contain" else "铺满画面",
            key="ref_video_fit", disabled=busy, persist_state="session",
        )
        st.session_state.setdefault("ref_subtitle_style", "clean")
        if st.session_state["ref_subtitle_style"] == "none":
            st.session_state["ref_subtitle_style"] = "clean"
        st.selectbox(
            "字幕样式", ["clean", "bold", "yellow"],
            format_func=lambda value: {"clean": "清晰字幕", "bold": "醒目字幕", "yellow": "黄色字幕"}[value],
            key="ref_subtitle_style", disabled=busy, persist_state="session",
        )

    if st.button("保存设置", key="ref_processing_save", type="primary", width="stretch", disabled=busy):
        try:
            if st.session_state.get("ref_script_text", "").strip():
                ctx._ensure_project()
            _dismiss_processing()
            st.rerun(scope="app")
        except Exception as exc:
            st.error(str(exc))


def render_processing_column(ctx):
    """Render truthful subtitle/music controls and the downloadable final file."""
    busy = _busy(ctx)
    if st.button("视频处理设置", key="ref_open_processing_settings", width="stretch", disabled=busy):
        st.session_state["ref_processing_open"] = True

    with st.container(key="ref_subtitle_card", border=True):
        title, control = st.columns([3, 1], gap="small", vertical_alignment="center")
        title.markdown("**视频字幕**")
        st.session_state.setdefault("ref_subtitles", True)
        with control:
            st.toggle("视频字幕", key="ref_subtitles", label_visibility="collapsed", disabled=busy, persist_state="session")
        st.selectbox(
            "字幕生成方式", ["智能字幕"], key="ref_subtitle_method",
            label_visibility="collapsed", disabled=True, persist_state="session",
        )

    with st.container(key="ref_music_card", border=True):
        title, control = st.columns([3, 1], gap="small", vertical_alignment="center")
        title.markdown("**背景音乐**")
        st.session_state.setdefault("ref_bgm_enabled", False)
        with control:
            enabled = st.toggle("背景音乐", key="ref_bgm_enabled", label_visibility="collapsed", disabled=busy, persist_state="session")
        _music_upload(ctx, busy)
        if enabled:
            st.session_state.setdefault("ref_bgm_volume", 0.12)
            st.slider("音乐音量", 0.0, 0.4, step=0.01, key="ref_bgm_volume", disabled=busy, persist_state="session")

    with st.container(key="ref_highlight_card", border=True):
        heading, detect = st.columns([1.5, 1])
        heading.markdown("**高亮关键词**")
        if detect.button("智能提取", key="ref_extract_keywords", disabled=busy or not st.session_state.get("ref_script_text", "").strip()):
            from app.services.creator.script_review import extract_keywords
            for name, words in extract_keywords(st.session_state["ref_script_text"]).items():
                st.session_state["ref_highlight_" + name] = "\n".join(words)
        st.session_state.setdefault("ref_highlight_group", "主词")
        group = st.radio(
            "关键词类型", list(_HIGHLIGHT_FIELDS), horizontal=True,
        key="ref_highlight_group", label_visibility="collapsed", disabled=busy, persist_state="session",
        )
        key = _HIGHLIGHT_FIELDS[group]
        st.session_state.setdefault(key, "")
        st.text_area(
            "高亮词", key=key, height=100, placeholder="每行输入一个关键词",
            label_visibility="collapsed", disabled=busy, persist_state="session",
        )
        st.caption("字幕中的匹配词会按分组颜色标记，可手动增删。")

    with st.container(key="ref_creation_controls"):
        st.session_state.setdefault("ref_creation_mode", "one_click")
        if st.session_state["ref_creation_mode"] not in {"one_click", "step_by_step"}:
            st.session_state["ref_creation_mode"] = "one_click"
        mode = st.radio(
            "制作模式", ["one_click", "step_by_step"], horizontal=True,
            format_func=lambda value: "一键成片" if value == "one_click" else "分步制作",
            key="ref_creation_mode", disabled=busy, persist_state="session", label_visibility="collapsed",
        )
        st.caption("生成成片与本地封面、发布资料；发布需另行确认。" if mode == "one_click" else
                   "先生成成片，再到发布制作准备封面与发布资料。")
    with st.container(key="ref_render_action"):
        label, action = st.columns([1.45, 1], gap="small", vertical_alignment="center")
        label.markdown("**生成最终成片**")
        with action:
            if st.button("一键成片" if mode == "one_click" else "生成成片", type="primary", key="ref_generate_render", width="stretch", disabled=busy):
                _attempt(_render_request, ctx)
    _video_preview(ctx, rendered=True)
    if st.session_state.get("ref_pip_open") and not st.session_state.get("ref_script_review_open"):
        from webui.creator_reference_media import render_dialog
        render_dialog(ctx)

    elif st.session_state.get("ref_processing_open") and not st.session_state.get("ref_script_review_open"):
        _processing_dialog(ctx)
    elif st.session_state.get("ref_asset_browser") and not st.session_state.get("ref_script_review_open"):
        from webui.creator_reference_assets import avatar_browser, voice_browser
        (voice_browser if st.session_state["ref_asset_browser"] == "voice" else avatar_browser)(ctx)
