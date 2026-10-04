"""Manual step 04: choose real editing options, render, then review a finished video."""
from __future__ import annotations

import hashlib
import html
from pathlib import Path

import streamlit as st

from app.services.creator import avatar, jobs, rendering, store


_SOURCES = ["本次视频", "历史口播", "上传本地视频"]
_SUBTITLES = ["自动识别生成", "使用字幕文件", "保留视频原有字幕", "不添加字幕"]
_POSITIONS = {"top-left": "左上", "top-right": "右上", "bottom-left": "左下", "bottom-right": "右下", "center": "居中"}


def _stage(upload, *, kind="video"):
    limit = {"video": 500, "audio": 128, "subtitle": 2, "pip": 200}[kind]
    if not 0 < upload.size <= limit * 1024 * 1024:
        raise ValueError(f"请选择不超过 {limit} MB 的文件。")
    content = upload.getvalue()
    folder = store.data_root() / "render_uploads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(content).hexdigest() + Path(upload.name).suffix.lower())
    if not path.is_file():
        path.write_bytes(content)
    return {"path": str(path), "name": upload.name}


def _upload_changed(key):
    if st.session_state.get(key + "_upload") is None:
        st.session_state.pop(key + "_import", None)


def _upload(label, types, key, *, kind, busy):
    upload = st.file_uploader(label, type=types, key=key + "_upload", disabled=busy, on_change=_upload_changed, args=(key,))
    if upload:
        try:
            st.session_state[key + "_import"] = _stage(upload, kind=kind)
        except Exception as exc:
            st.error(str(exc))
            return None
    saved = st.session_state.get(key + "_import")
    if saved and Path(saved.get("path", "")).is_file():
        st.caption("已保存素材：" + saved.get("name", Path(saved["path"]).name))
        return saved
    return None


def _probe(path):
    source = Path(path)
    if not source.is_file():
        raise ValueError("这份素材已移动或删除，请重新选择。")
    stat = source.stat()
    key = (str(source.resolve()), stat.st_size, stat.st_mtime_ns)
    cache = st.session_state.setdefault("creator_render_probe_cache", {})
    if key not in cache:
        # Keep this work outside the progress fragment. A navigation rerun uses
        # the existing result until the underlying file actually changes.
        cache[key] = rendering.probe_source(str(source))
        if len(cache) > 20:
            cache.pop(next(iter(cache)))
    return cache[key]


def _material_input(busy):
    current_path = st.session_state.get("creator_video_path", "")
    current = st.session_state.get("creator_avatar_result")
    if not isinstance(current, dict) or current_path not in {current.get("video_path"), current.get("clean_video_path")}:
        current = {"video_path": current_path, "model_name": "本次视频", "aspect": st.session_state.get("creator_render_aspect", "9:16")}
    history = [row for row in avatar.list_jobs() if row.get("state") == "done"]
    st.session_state.setdefault("creator_render_source", "本次视频" if current_path else "历史口播" if history else "上传本地视频")
    source = st.radio("视频来源", _SOURCES, horizontal=True, key="creator_render_source", disabled=busy, persist_state="session")
    selected = None
    if source == "本次视频":
        if current_path:
            selected = dict(current, video_path=current_path, srt_path=st.session_state.get("creator_srt_path", ""),
                subtitles_burned=bool(st.session_state.get("creator_video_has_subtitles")))
        else:
            st.info("还没有送到剪辑的视频。可以去第三步生成口播，也可以上传本地视频。")
    elif source == "历史口播":
        by_id = {row["id"]: row for row in history}
        if st.session_state.get("creator_render_history_id") not in by_id:
            st.session_state["creator_render_history_id"] = None
        if by_id:
            ident = st.selectbox("选择口播视频", list(by_id), index=None, placeholder="请选择一份已完成的口播视频", key="creator_render_history_id",
                format_func=lambda key: by_id[key].get("model_name", "数字人") + " · " + by_id[key].get("script", "口播视频")[:30], disabled=busy, persist_state="session")
            row = by_id.get(ident)
            if row:
                clean = row.get("clean_video_path")
                selected = dict(row, video_path=clean or row["video_path"], subtitles_burned=bool(row.get("subtitles_burned") and not clean))
        else:
            st.info("还没有已完成的口播视频。可以先去数字人制作，或上传本地视频。")
    else:
        imported = _upload("上传要剪辑的视频", ["mp4", "mov", "mkv", "webm"], "creator_render_local_video", kind="video", busy=busy)
        st.caption("支持最长 30 分钟、最大 500 MB 的视频。文件会保留，切换页面后可以继续。")
        if imported:
            selected = {"video_path": imported["path"], "model_name": imported["name"], "subtitles_burned": False}
    if not selected:
        return None, None
    try:
        info = _probe(selected["video_path"])
        if not info.get("has_video"):
            raise ValueError("这份素材没有视频画面，请重新选择视频。")
    except Exception as exc:
        st.warning(str(exc))
        return None, None
    fingerprint = source + "|" + selected["video_path"] + "|" + str(selected.get("srt_path", "")) + "|" + str(selected.get("subtitles_burned", False))
    previous_fingerprint = st.session_state.get("creator_render_material_fingerprint")
    if previous_fingerprint != fingerprint:
        st.session_state["creator_render_material_fingerprint"] = fingerprint
        if previous_fingerprint:
            st.session_state.pop("creator_render_subtitle_import", None)
            st.session_state.pop("creator_render_audio_import", None)
            st.session_state["creator_render_replace_audio"] = False
            st.session_state["creator_render_audio"] = ""
            for key in ("creator_render_audio_upload", "creator_render_subtitle_upload"):
                st.session_state.pop(key, None)
        st.session_state["creator_render_subtitle_mode"] = "保留视频原有字幕" if selected.get("subtitles_burned") else "使用字幕文件" if selected.get("srt_path") else "自动识别生成"
        if selected.get("aspect") in {"9:16", "16:9", "1:1"}:
            st.session_state["creator_render_aspect"] = selected["aspect"]
    st.caption(f'已选素材：{selected.get("model_name") or Path(selected["video_path"]).name} · {info.get("duration", 0):.1f} 秒')
    return selected, info


def _preset_picker(busy):
    presets = rendering.list_presets()
    by_id = {row["id"]: row for row in presets}
    if st.session_state.get("creator_render_style") not in by_id:
        st.session_state["creator_render_style"] = next(iter(by_id), "clean")
    with st.container(key="creator_render_style_gallery"):
        columns = st.columns(len(presets), gap="small")
        for column, preset in zip(columns, presets):
            selected = preset["id"] == st.session_state["creator_render_style"]
            with column:
                name = html.escape(preset["name"])
                description = html.escape(preset.get("description", ""))
                st.markdown(f'<div class="render-style-card render-style-{html.escape(preset["id"])} {"is-selected" if selected else ""}"><div class="render-style-figure"><span class="render-style-person"></span><span class="render-style-title">{name}</span><span class="render-style-caption">把想法做成作品</span></div><p>{description}</p></div>', unsafe_allow_html=True)
                if st.button(("✓ " if selected else "") + preset["name"], key="creator_render_preset_" + preset["id"], type="primary" if selected else "secondary", width="stretch", disabled=busy):
                    st.session_state["creator_render_style"] = preset["id"]
                    st.rerun()
    st.caption("上方为样式示意。右侧先展示原素材，完成剪辑后再查看实际效果。")
    return by_id[st.session_state["creator_render_style"]]


def _remember_title():
    st.session_state["creator_render_title_buffer"] = st.session_state.get("creator_render_title", "")


def _title_input(busy):
    # Keep the draft outside the widget's lifecycle as well. Returning after a
    # page switch must restore both written titles and an intentional clearing.
    st.session_state.setdefault("creator_render_title_buffer", st.session_state.get("creator_render_title", ""))
    st.session_state["creator_render_title"] = st.session_state["creator_render_title_buffer"]
    return st.text_input("画面标题（可选）", max_chars=120, placeholder="简洁概括这篇内容", key="creator_render_title", disabled=busy, persist_state="session", on_change=_remember_title)


def _audio_input(busy):
    st.session_state.setdefault("creator_render_replace_audio", bool(st.session_state.get("creator_render_audio")))
    replace = st.checkbox("替换原视频配音", key="creator_render_replace_audio", disabled=busy, persist_state="session")
    if not replace:
        st.caption("保留原视频声音、音色和停顿。画中画素材不带入声音。")
        return None, True, None
    imported = _upload("上传新的完整配音", ["wav", "mp3", "m4a", "aac", "flac", "ogg"], "creator_render_audio", kind="audio", busy=busy)
    path = imported["path"] if imported else st.session_state.get("creator_render_audio", "")
    if not path or not Path(path).is_file():
        st.info("请选择替换配音；取消上面的勾选就会继续使用视频原音。")
        return None, False, None
    st.audio(path)
    st.caption("成片按这份新配音的时长制作。请同步更新字幕，或让系统重新识别。")
    st.caption("替换声音不会重新生成数字人的嘴型；要更换口播内容，请回到第三步重新生成。")
    try:
        info = _probe(path)
        if not info.get("has_audio"):
            raise ValueError("这份配音没有有效音轨，请重新上传。")
        return path, True, info.get("duration")
    except Exception as exc:
        st.warning(str(exc))
        return None, False, None


def _subtitle_input(busy, material, replace_audio):
    st.session_state.setdefault("creator_render_subtitle_mode", "自动识别生成")
    mode = st.selectbox("字幕处理", _SUBTITLES, key="creator_render_subtitle_mode", disabled=busy, persist_state="session")
    subtitle = None
    ready = True
    if mode == "使用字幕文件":
        imported = _upload("上传对应的字幕", ["srt"], "creator_render_subtitle", kind="subtitle", busy=busy)
        subtitle = imported["path"] if imported else (material or {}).get("srt_path")
        ready = bool(subtitle and Path(subtitle).is_file())
        if ready:
            st.caption("字幕按 SRT 时间轴显示。请确认文字与最终配音相符。")
        else:
            st.info("请上传带时间轴的 SRT 字幕，或选择自动识别。")
    elif mode == "自动识别生成":
        st.caption("识别最终使用的音轨生成字幕。首次使用识别模型可能需要下载。")
    elif mode == "保留视频原有字幕":
        st.caption("保留已经出现在视频画面里的字幕，不叠加第二层字幕。")
        if replace_audio:
            st.warning("正在替换配音，视频画面中的旧字幕仍会保留。请确认旧字幕与新配音同步。")
    else:
        st.caption("不新增字幕；已经在原视频画面里的文字仍会保留。")
    if (material or {}).get("subtitles_burned") and mode in {"自动识别生成", "使用字幕文件"}:
        st.warning("原视频已经带字幕，再添加会叠成两层。请选择保留视频原有字幕，或换用没有字幕的原片。")
        ready = False
    return mode, subtitle, ready


def _remember_music_choice():
    st.session_state["creator_render_bgm_preference"] = bool(st.session_state["creator_render_bgm_enabled"])


def _music_input(busy):
    library = rendering.list_bgm()
    # Save the user's preference independently from the widget lifecycle. The
    # library determines the first visit only; returning to this page must not
    # turn music back on after the user explicitly disabled it.
    st.session_state.setdefault("creator_render_bgm_preference", bool(st.session_state.get("creator_render_bgm_enabled", bool(library))))
    st.session_state["creator_render_bgm_enabled"] = st.session_state["creator_render_bgm_preference"]
    enabled = st.checkbox("添加背景音乐", key="creator_render_bgm_enabled", disabled=busy, persist_state="session", on_change=_remember_music_choice)
    if not enabled:
        return None, 0.12, True
    source = st.radio("音乐来源", ["本机音乐库", "上传本地音乐"], horizontal=True, key="creator_render_bgm_source", disabled=busy, persist_state="session")
    path = None
    if source == "本机音乐库":
        by_id = {row["id"]: row for row in library}
        if "creator_render_bgm_id" not in st.session_state:
            st.session_state["creator_render_bgm_id"] = next(iter(by_id), None)
        elif st.session_state.get("creator_render_bgm_id") not in by_id:
            st.session_state["creator_render_bgm_id"] = None
        if by_id:
            ident = st.selectbox("选择背景音乐", list(by_id), index=None, placeholder="试听后选择音乐", key="creator_render_bgm_id", format_func=lambda key: by_id[key]["name"], disabled=busy, persist_state="session")
            if ident:
                path = by_id[ident]["path"]
        else:
            st.info("本机音乐库还没有可用音乐，可以上传自己的音乐文件。")
    else:
        imported = _upload("上传背景音乐", ["wav", "mp3", "m4a", "aac", "flac", "ogg"], "creator_render_bgm", kind="audio", busy=busy)
        if imported:
            path = imported["path"]
    ready = bool(path and Path(path).is_file())
    if ready:
        st.audio(path)
    volume = st.slider("音乐音量", 0.0, 0.4, 0.12, 0.01, key="creator_render_bgm_volume", disabled=busy, persist_state="session")
    st.caption("音乐自动循环并淡入淡出。建议用较低音量，让口播更清楚。")
    return path, volume, ready


def _pip_input(busy, duration):
    enabled = st.checkbox("添加画中画素材", key="creator_render_pip_enabled", disabled=busy, persist_state="session")
    if not enabled:
        return [], True
    st.caption("图片或视频覆盖在指定时段。保留主视频配音，画中画视频使用静音。")
    count = st.number_input("画中画段数", min_value=1, max_value=5, value=1, step=1, key="creator_render_pip_count", disabled=busy, persist_state="session")
    items = []
    ready = True
    end_default = round(min(5.0, duration or 5.0), 2)
    for index in range(count):
        prefix = "creator_render_pip_" + str(index)
        with st.expander(f"画中画 {index + 1}", expanded=index == 0):
            imported = _upload("上传图片或视频", ["png", "jpg", "jpeg", "webp", "mp4", "mov", "mkv", "webm"], prefix, kind="pip", busy=busy)
            if imported:
                if Path(imported["path"]).suffix.lower() in {"png", "jpg", "jpeg", "webp"}:
                    st.image(imported["path"], width=180)
                else:
                    st.video(imported["path"])
            left, right = st.columns(2)
            start = left.number_input("开始秒数", min_value=0.0, value=0.0, step=0.1, key=prefix + "_start", disabled=busy, persist_state="session")
            end = right.number_input("结束秒数", min_value=0.1, value=max(0.1, end_default), step=0.1, key=prefix + "_end", disabled=busy, persist_state="session")
            position = st.selectbox("位置", list(_POSITIONS), index=1, format_func=lambda value: _POSITIONS[value], key=prefix + "_position", disabled=busy, persist_state="session")
            size = st.slider("画中画宽度占比", min_value=0.15, max_value=0.6, value=0.3, step=0.05, key=prefix + "_size", disabled=busy, persist_state="session")
            if end <= start or (duration and end > duration + 0.02):
                st.warning("结束时间应晚于开始时间，并在成片时长以内。")
                ready = False
            if not imported:
                ready = False
            else:
                items.append({"path": imported["path"], "start": start, "end": end, "position": position, "size": size})
    return items, ready


def _submit(video_path, kwargs):
    try:
        ident = jobs.submit("模板剪辑 · " + (kwargs.get("title") or "新视频"), rendering.render_video, video_path, **kwargs)
        st.session_state["creator_render_pending"] = {"id": ident}
        st.session_state["creator_last_job"] = ident
    except Exception as exc:
        st.error(str(exc))


def _collect_result():
    pending = st.session_state.get("creator_render_pending")
    if not pending:
        return
    row = jobs.get_job(pending["id"])
    if not row or row["state"] in {"queued", "running"}:
        return
    st.session_state.pop("creator_render_pending", None)
    if row["state"] == "done" and isinstance(row.get("result"), dict) and row["result"].get("video_path"):
        st.session_state["creator_render_result"] = row["result"]
        st.session_state["creator_render_notice"] = ("success", "成片已完成，请播放检查字幕、声音和画中画。")
    else:
        st.session_state["creator_render_notice"] = ("error", row.get("message") or "剪辑没有完成，请检查素材后重试。")


@st.fragment(run_every="2s")
def _progress():
    pending = st.session_state.get("creator_render_pending")
    if not pending:
        return
    row = jobs.get_job(pending["id"])
    if row and row["state"] not in {"queued", "running"}:
        st.rerun(scope="app")
    if row:
        st.info(row.get("message") or "正在剪辑，可以继续准备其他内容。")
        st.progress(float(row.get("progress", 0)) / 100)


def _use_finished(row):
    from webui.creator_release_workspace import load_video
    text = ""
    subtitle = row.get("srt_path", "")
    if subtitle and Path(subtitle).is_file():
        try:
            text = "".join(cue["text"] for cue in rendering.read_srt(subtitle))
        except (ValueError, OSError):
            text = ""
    load_video(row, text=text)
    st.session_state["creator_selected_tab"] = "标题和封面"
    st.session_state["creator_navigation_pending"] = True


def _result_actions(row, key, *, preview=True):
    path = Path(row.get("video_path", ""))
    if not path.is_file():
        st.warning("这份成片已移动或删除，请重新生成。")
        return
    if preview:
        st.video(str(path))
    st.caption(f'{row.get("title") or "模板成片"} · {row.get("aspect", "9:16")} · {row.get("duration", 0):.1f} 秒')
    for message in row.get("warnings", []):
        st.caption(str(message))
    st.download_button("下载成片", path.read_bytes(), file_name="剪辑成片.mp4", key=key + "_download", width="stretch")
    subtitle = row.get("srt_path")
    if subtitle and Path(subtitle).is_file():
        st.download_button("下载字幕", Path(subtitle).read_bytes(), file_name="成片字幕.srt", key=key + "_subtitles", width="stretch")
    st.button("下一步，标题和封面 →", key=key + "_publish", type="primary", width="stretch", on_click=_use_finished, args=(row,))


def render():
    _collect_result()
    st.markdown('<div class="script-eyebrow">STEP 04 · 模板剪辑</div>', unsafe_allow_html=True)
    st.markdown('<div class="render-brief"><h2>准备就绪，<br>把口播做成作品。</h2><p>选择样式与素材，全自动剪辑，也可以自己安排画中画。</p></div>', unsafe_allow_html=True)
    notice = st.session_state.pop("creator_render_notice", None)
    if notice:
        getattr(st, notice[0])(notice[1])
    _progress()
    busy = bool(st.session_state.get("creator_render_pending"))
    with st.container(key="creator_render_grid"):
        left, right = st.columns([2.2, 1], gap="large")
    with left:
        with st.container(border=True, key="creator_render_materials"):
            st.markdown("**视频素材**")
            material, info = _material_input(busy)
        with st.container(border=True, key="creator_render_settings"):
            mode = st.radio("剪辑方式", ["全自动", "自定义"], horizontal=True, key="creator_render_mode", label_visibility="collapsed", disabled=busy, persist_state="session")
            st.markdown("**风格包装**")
            preset = _preset_picker(busy)
            title = _title_input(busy)
            with st.expander("配音与字幕", expanded=True):
                audio, audio_ready, audio_duration = _audio_input(busy)
                subtitle_mode, subtitle, subtitle_ready = _subtitle_input(busy, material, bool(audio))
            color_grade = preset.get("color_grade", "none")
            subtitle_style = preset.get("subtitle_style", "clean")
            pip_items = []
            pip_ready = True
            duration = audio_duration or (info or {}).get("duration")
            if mode == "自定义":
                with st.container(key="creator_render_custom"):
                    if st.session_state.get("creator_render_custom_preset") != preset["id"]:
                        st.session_state["creator_render_color_grade"] = color_grade
                        st.session_state["creator_render_subtitle_style"] = subtitle_style
                        st.session_state["creator_render_custom_preset"] = preset["id"]
                    with st.expander("画面调色与字幕样式", expanded=True):
                        color_grade = st.selectbox("画面调色", ["none", "warm", "cool", "vivid"], format_func=lambda value: {"none": "保留原色", "warm": "柔和暖色", "cool": "清爽冷色", "vivid": "清晰鲜明"}[value], key="creator_render_color_grade", disabled=busy, persist_state="session")
                        subtitle_style = st.selectbox("字幕样式", ["clean", "bold", "yellow"], format_func=lambda value: {"clean": "清爽白字", "bold": "网感大字", "yellow": "重点黄字"}[value], key="creator_render_subtitle_style", disabled=busy or subtitle_mode in {"保留视频原有字幕", "不添加字幕"}, persist_state="session")
                    with st.expander("画中画", expanded=True):
                        pip_items, pip_ready = _pip_input(busy, duration)
            with st.expander("背景音乐", expanded=False):
                bgm, bgm_volume, bgm_ready = _music_input(busy)
        ready = bool(material and audio_ready and subtitle_ready and bgm_ready and pip_ready)
        if subtitle_mode == "自动识别生成" and material and not audio and not (info or {}).get("has_audio"):
            st.warning("原视频没有音轨。请添加替换配音，或关闭自动字幕。")
            ready = False
        kwargs = {"audio_path": audio, "subtitle_path": subtitle, "title": title, "template": "talking", "style": preset["id"],
            "color_grade": color_grade, "subtitle_style": "none" if subtitle_mode in {"保留视频原有字幕", "不添加字幕"} else subtitle_style,
            "auto_subtitles": subtitle_mode == "自动识别生成", "source_subtitles_burned": bool((material or {}).get("subtitles_burned") or subtitle_mode == "保留视频原有字幕"),
            "bgm_path": bgm, "bgm_volume": bgm_volume, "pip_items": pip_items}
    with right:
        with st.container(border=True, key="creator_render_preview"):
            current_result = st.session_state.get("creator_render_result")
            if current_result and Path(current_result.get("video_path", "")).is_file():
                st.markdown("**成片预览**")
                st.video(current_result["video_path"])
                st.caption("这里展示上一次已完成的成片。修改设置后，点击开始剪辑生成新版本。")
            elif material:
                st.markdown("**素材预览**")
                st.video(material["video_path"])
                st.caption("素材预览；生成后展示实际效果。")
            else:
                st.markdown('<div class="render-empty-preview"><span>准备好视频素材，<br>在这里检查画面。</span></div>', unsafe_allow_html=True)
            st.session_state.setdefault("creator_render_aspect", "9:16")
            aspect = st.selectbox("成片画幅", ["9:16", "16:9", "1:1"], format_func=lambda value: {"9:16": "竖屏 9:16", "16:9": "横屏 16:9", "1:1": "方形 1:1"}[value], key="creator_render_aspect", disabled=busy, persist_state="session")
            video_fit = st.selectbox("画面适配", ["contain", "cover"], format_func=lambda value: {"contain": "保留完整画面", "cover": "铺满画面（居中裁切）"}[value], key="creator_render_video_fit", disabled=busy, persist_state="session")
            if duration:
                st.caption(f"预计成片时长：{duration:.1f} 秒")
            if st.button("开始剪辑", type="primary", key="creator_render_start", width="stretch", disabled=busy or not ready):
                _submit(material["video_path"], dict(kwargs, aspect=aspect, video_fit=video_fit))
                st.rerun()
            if busy:
                st.caption("剪辑任务已在后台运行，完成后可以预览和下载。")
            elif not material:
                st.caption("请先选择一份视频素材。")
        if st.session_state.get("creator_render_result"):
            with st.container(border=True, key="creator_render_result"):
                st.markdown("**成片 · 下载与发布**")
                _result_actions(st.session_state["creator_render_result"], "creator_render_current", preview=False)
    history = rendering.list_renders()
    if history:
        with st.expander("我的成片 · 历史记录"):
            for row in history[:12]:
                with st.expander((row.get("title") or "模板成片") + " · " + row.get("aspect", "9:16")):
                    _result_actions(row, "creator_render_history_" + row["id"])
