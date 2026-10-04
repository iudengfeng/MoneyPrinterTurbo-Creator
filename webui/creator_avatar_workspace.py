"""Manual step 03: use a video identity and an explicitly confirmed audio track."""
from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st

from app.services.creator import avatar, jobs, narration, store


_AUDIO_SOURCES = ["本次完整配音", "历史完整配音", "上传本地音频"]


def _stage(upload, *, video=False):
    limit = (500 if video else 128) * 1024 * 1024
    if not 0 < upload.size <= limit:
        raise ValueError(f"请选择不超过 {500 if video else 128} MB 的{'视频' if video else '音频'}文件。")
    content = upload.getvalue()
    folder = store.data_root() / "avatar_uploads"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.name).suffix.lower()
    path = folder / (hashlib.sha256(content).hexdigest() + suffix)
    if not path.is_file():
        path.write_bytes(content)
    return str(path)


@st.dialog("上传形象", width="large")
def _upload_profile():
    st.caption("为形象命名，保存一段清晰的单人出镜视频。建议正面拍摄，保持光线、背景和机位稳定。")
    name = st.text_input("形象名称", placeholder="例如：我的形象 · 日常口播", key="creator_avatar_upload_name")
    upload = st.file_uploader("上传形象视频", type=["mp4", "mov"], key="creator_avatar_upload_video")
    st.caption("视频最长 2 分钟、最大 500 MB。这里只保存人物素材，生成时使用所选完整配音驱动嘴型。")
    if upload:
        st.video(upload.getvalue())
    if st.button("保存形象", type="primary", width="stretch"):
        try:
            if not upload:
                raise ValueError("请先上传形象视频。")
            row = avatar.import_profile(name, _stage(upload, video=True))
            st.session_state["creator_avatar_model"] = row["id"]
            st.session_state["creator_avatar_profile_saved"] = row
            st.success("形象已保存到人物库。")
        except Exception as exc:
            st.error(str(exc))
    if st.button("完成，返回人物库", width="stretch"):
        st.rerun(scope="app")


def _use_video(row):
    clean = row.get("clean_video_path")
    st.session_state["creator_video_path"] = clean or row["video_path"]
    st.session_state["creator_publish_video"] = row["video_path"]
    st.session_state["creator_srt_path"] = row.get("srt_path", "") if clean or not row.get("subtitles_burned") else ""
    st.session_state["creator_video_has_subtitles"] = bool(row.get("subtitles_burned") and not clean)
    # The generated video already contains the chosen audio. Remove any previous
    # editing replacement instead of silently substituting an older narration.
    st.session_state["creator_render_audio"] = ""
    st.session_state["creator_render_source"] = "本次视频"
    st.session_state["creator_render_replace_audio"] = False
    for key in ("creator_render_material_fingerprint", "creator_render_audio_import", "creator_render_subtitle_import"):
        st.session_state.pop(key, None)
    for key in ("creator_video_path_upload", "creator_srt_path_upload", "creator_render_subtitle_upload", "creator_render_audio_upload"):
        st.session_state.pop(key, None)
    st.session_state["creator_render_aspect"] = row.get("aspect", "9:16")
    st.session_state["creator_selected_tab"] = "模板剪辑"
    st.session_state["creator_navigation_pending"] = True


def _submit(option, audio, script, aspect):
    try:
        ident = jobs.submit("数字人口播 · " + option["name"], avatar.generate,
            audio["audio_path"], option["id"], script=script, aspect=aspect,
            source_narration_id=audio.get("id", ""))
        st.session_state["creator_avatar_pending"] = {"id": ident, "action": "generate"}
        st.session_state["creator_last_job"] = ident
    except Exception as exc:
        st.error(str(exc))


def _recover_engine():
    try:
        ident = jobs.submit("恢复数字人引擎", avatar.recover_engine)
        st.session_state["creator_avatar_pending"] = {"id": ident, "action": "recover"}
        st.session_state["creator_last_job"] = ident
    except Exception as exc:
        st.error(str(exc))


def _collect_result():
    pending = st.session_state.get("creator_avatar_pending")
    if not pending:
        return
    row = jobs.get_job(pending["id"])
    if not row or row["state"] in {"queued", "running"}:
        return
    st.session_state.pop("creator_avatar_pending", None)
    if row["state"] == "done" and pending.get("action") == "recover":
        result = row.get("result") or {}
        message = result.get("message") or "数字人引擎已恢复，请重新检查生成设置。"
        warnings = result.get("warnings") or []
        st.session_state["creator_avatar_notice"] = ("warning" if warnings else "success", "\n".join([message, *map(str, warnings)]))
    elif row["state"] == "done" and isinstance(row.get("result"), dict):
        st.session_state["creator_avatar_result"] = row["result"]
        st.session_state["creator_avatar_notice"] = ("success", "口播视频已生成，请播放检查嘴型和声音。")
    else:
        fallback = "数字人引擎没有恢复，请检查本机服务。" if pending.get("action") == "recover" else "口播视频没有完成，请检查本机数字人服务。"
        st.session_state["creator_avatar_notice"] = ("error", row.get("message") or fallback)


@st.fragment(run_every="2s")
def _progress():
    pending = st.session_state.get("creator_avatar_pending")
    if not pending:
        return
    row = jobs.get_job(pending["id"])
    if row and row["state"] not in {"queued", "running"}:
        st.rerun(scope="app")
    if row:
        st.info(row.get("message", "正在生成口播视频，请稍候。"))
        st.progress(float(row.get("progress", 0)) / 100)


def _result(row, key):
    path = Path(row.get("video_path", ""))
    if not path.is_file():
        st.warning("这份口播视频已移动或删除，请重新生成。")
        return
    st.caption(f'{row.get("model_name", "数字人")} · {row.get("aspect", "9:16")} · {row.get("duration", 0):.1f} 秒')
    st.video(str(path))
    for message in row.get("warnings", []):
        st.caption(str(message))
    left, right = st.columns(2)
    left.download_button("下载口播视频", path.read_bytes(), file_name="数字人口播.mp4", key=key + "_download", width="stretch")
    right.button("下一步，模板剪辑 →", type="primary", key=key + "_render", width="stretch", on_click=_use_video, args=(row,))


def _audio_input(busy):
    current = st.session_state.get("creator_narration_result")
    if not isinstance(current, dict) or current.get("preview") or current.get("state", "done") != "done":
        current = None
    history = [row for row in narration.list_narrations() if not row.get("preview") and row.get("state") == "done"]
    st.session_state.setdefault("creator_avatar_audio_source", "本次完整配音" if current else "历史完整配音" if history else "上传本地音频")
    source = st.radio("配音来源", _AUDIO_SOURCES, horizontal=True, key="creator_avatar_audio_source", disabled=busy, persist_state="session")
    row = None
    if source == "本次完整配音":
        if not current:
            st.info("第二步还没有完整配音。可以先去配音，也可以选择历史配音或上传音频。")
        else:
            row = current
    elif source == "历史完整配音":
        options = {row["id"]: row for row in history}
        wanted = st.session_state.pop("creator_avatar_narration_id", None)
        if wanted in options:
            st.session_state["creator_avatar_history_id"] = wanted
        if st.session_state.get("creator_avatar_history_id") not in options:
            st.session_state["creator_avatar_history_id"] = None
        if not options:
            st.info("还没有已完成的完整配音记录。短试听不会用于生成口播视频。")
        else:
            ident = st.selectbox("选择已完成的配音", list(options), index=None, placeholder="请选择一份完整配音", key="creator_avatar_history_id",
                format_func=lambda key: options[key].get("text", "配音")[:32] + " · " + options[key].get("voice_name", ""), disabled=busy, persist_state="session")
            row = options.get(ident)
    else:
        upload = st.file_uploader("上传口播配音", type=["wav", "mp3", "m4a", "aac", "flac", "ogg"], key="creator_avatar_audio_upload", disabled=busy)
        st.caption("使用完整口播音频，最长 10 分钟、最大 128 MB。")
        if upload:
            try:
                row = {"audio_path": _stage(upload), "source_audio_name": upload.name, "text": "", "id": ""}
            except Exception as exc:
                st.error(str(exc))
    if not row:
        st.session_state["creator_avatar_audio_confirmed"] = False
        return None, "", False
    path = Path(row.get("audio_path", ""))
    if not path.is_file():
        st.warning("这份配音文件已移动或删除，请在第二步重新生成或重新上传。")
        st.session_state["creator_avatar_audio_confirmed"] = False
        return None, "", False
    script = row.get("text", "")
    draft = st.session_state.get("creator_voice_draft", "")
    fingerprint = hashlib.sha256((source + str(path) + str(row.get("id", "")) + script + draft).encode("utf-8")).hexdigest()
    if st.session_state.get("creator_avatar_audio_fingerprint") != fingerprint:
        st.session_state["creator_avatar_audio_confirmed"] = False
        st.session_state["creator_avatar_audio_fingerprint"] = fingerprint
        if source == "上传本地音频":
            st.session_state["creator_avatar_uploaded_script"] = ""
    title = row.get("voice_name") or row.get("source_audio_name") or path.name
    duration = row.get("duration")
    st.caption("已选配音：" + title + (f" · {duration:.1f} 秒" if duration is not None else ""))
    st.audio(str(path))
    if source == "上传本地音频":
        script = st.text_area("对应文案（可选）", key="creator_avatar_uploaded_script", height=110, disabled=busy, persist_state="session")
        st.caption("文案用于保存记录；人物实际说话内容、语速和停顿以这份音频为准。")
    else:
        st.text_area("这份配音的原文", value=script, key="creator_avatar_audio_script_" + fingerprint, height=110, disabled=True)
        if draft.strip() and script.strip() != draft.strip():
            st.warning("这份配音与第二步当前编辑的文案不同。将按上面这份已生成配音制作；若要使用新文案，请回到第二步重新配音。")
        st.caption("直接使用这份完整配音，保留已选音色、语速和停顿。")
    confirmed = st.checkbox("确认使用上面这份配音", key="creator_avatar_audio_confirmed", disabled=busy, persist_state="session")
    return row, script, confirmed


def render():
    _collect_result()
    st.markdown('<div class="script-eyebrow">STEP 03 · 数字人</div>', unsafe_allow_html=True)
    notice = st.session_state.pop("creator_avatar_notice", None)
    if notice:
        getattr(st, notice[0])(notice[1])
    _progress()
    busy = bool(st.session_state.get("creator_avatar_pending"))
    options = avatar.list_options()
    state = avatar.status()
    with st.container(key="creator_avatar_grid"):
        left, right = st.columns([1, 2.5], gap="large")
    with left, st.container(border=True, key="creator_avatar_models"):
        st.markdown('<div class="voice-brief"><h2>让你的形象<br>说出这篇内容。</h2></div>', unsafe_allow_html=True)
        a, b = st.columns(2)
        if a.button("上传形象", width="stretch", disabled=busy):
            _upload_profile()
        b.button("刷新人物库", width="stretch", disabled=busy)
        by_id = {row["id"]: row for row in options}
        if st.session_state.get("creator_avatar_model") not in by_id:
            st.session_state["creator_avatar_model"] = next(iter(by_id), None)
        if by_id:
            selected = st.radio("选择形象", list(by_id), format_func=lambda key: by_id[key]["name"], key="creator_avatar_model", label_visibility="collapsed", disabled=busy, persist_state="session")
            option = by_id[selected]
            st.caption("本机已有形象" if option.get("source") == "duix" else "已上传视频形象")
            preview = Path(option.get("video_path", ""))
            if preview.is_file():
                st.video(str(preview))
            else:
                st.info(option.get("reason") or "这份形象的原视频暂不可用，请重新上传素材。")
        else:
            option = None
            st.info("还没有可用的人物素材，请先上传形象视频。")
        with st.expander("素材拍摄建议"):
            st.write("单人、正面、脸部清晰。保持光线和机位稳定，避免遮挡嘴巴、频繁转头或多个镜头切换。")
            st.caption("当前接入视频数字人；照片生成、换服装和换背景尚未接入。")
    with right:
        with st.container(border=True, key="creator_avatar_audio_panel"):
            st.markdown("**口播配音**")
            audio, script, confirmed = _audio_input(busy)
        with st.container(border=True, key="creator_avatar_settings"):
            st.markdown("**生成设置**")
            aspect = st.radio("画面比例", ["9:16", "16:9"], format_func=lambda value: "竖屏 9:16" if value == "9:16" else "横屏 16:9", horizontal=True, key="creator_avatar_aspect", disabled=busy, persist_state="session")
            st.caption("保留配音中的自然停顿。剪辑气口和人物美颜尚未接入。")
            if not state.get("available"):
                st.warning(state.get("reason") or "本机数字人服务尚未就绪。人物素材和配音可以先保存，服务就绪后再生成。")
            if state.get("recovery_needed"):
                st.caption("上次口播任务没有正常收尾。恢复后会重新检查本机数字人服务，不会修改人物或音色资料。")
                if st.button("恢复数字人引擎", key="creator_avatar_recover", width="stretch", disabled=busy):
                    _recover_engine()
                    st.rerun()
            model_ready = bool(option and option.get("available", True))
            if option and not model_ready:
                st.warning(option.get("reason") or "所选人物素材暂不可用，请重新上传视频。")
            if st.button("生成口播视频", type="primary", key="creator_avatar_start", width="stretch", disabled=busy or not state.get("available") or not model_ready or not audio or not confirmed):
                _submit(option, audio, script, aspect)
                st.rerun()
            if audio and not confirmed:
                st.caption("请先试听，并确认使用这份配音。")
        if st.session_state.get("creator_avatar_result"):
            with st.container(border=True, key="creator_avatar_result_panel"):
                st.markdown("**口播视频 · 预览与下载**")
                _result(st.session_state["creator_avatar_result"], "creator_avatar_current")
    history = [row for row in avatar.list_jobs() if row.get("state") == "done"]
    if history:
        with st.expander("我的口播视频 · 历史记录"):
            for row in history[:12]:
                with st.expander(row.get("model_name", "数字人") + " · " + row.get("script", "口播视频")[:35]):
                    _result(row, "creator_avatar_history_" + row["id"])
