"""Real local identity and voice galleries, including sample import and comparison."""
from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st

from app.services.creator import avatar, extract, narration, store, voices


def _dismiss():
    st.session_state.pop("ref_asset_browser", None)


def _choose(ctx, field, value):
    st.session_state["ref_asset_choice_pending"] = {"project_id": ctx.project.get("id", ""), "field": field, "value": value}
    _dismiss()
    st.rerun(scope="app")


def _thumbnail(path):
    source = Path(path or "")
    if not source.is_file():
        return ""
    stamp = str(source.resolve())+str(source.stat().st_mtime_ns)
    folder = store.data_root() / "gallery"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / (hashlib.sha256(stamp.encode()).hexdigest()+".jpg")
    if not output.is_file():
        result = extract._run([extract.ffmpeg_binary(), "-v", "error", "-nostdin", "-y", "-i", str(source),
                               "-frames:v", "1", "-an", "-vf", "scale=180:300:force_original_aspect_ratio=decrease", str(output)])
        if result.returncode:
            return ""
    return str(output) if output.is_file() else ""


def _voice_sample(ctx, upload):
    source = Path(ctx.stage_upload(upload))
    extract.probe_media(source)
    target = source.with_name(source.stem + "-sample.wav")
    valid = False
    if target.is_file():
        try:
            voices._wav_valid(target)
            valid = True
        except ValueError:
            pass
    if not valid:
        response = extract._run([extract.ffmpeg_binary(), "-v", "error", "-nostdin", "-y", "-i", str(source),
                                 "-t", "30", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)])
        if response.returncode:
            target.unlink(missing_ok=True)
            raise ValueError("样音提取失败，请上传清晰音频或带声音的视频。")
    voices._wav_valid(target)
    return str(target)


@st.dialog("音色与声音克隆", width="large", on_dismiss=_dismiss)
def voice_browser(ctx):
    select, clone = st.tabs(["选择音色", "克隆与原声对比"])
    with select:
        options = narration.list_options()
        for row in options:
            text, action = st.columns([4, 1])
            text.markdown("**" + row["name"] + "**")
            text.caption(row.get("description", ""))
            if action.button("选择", key="ref_pick_voice_"+row["id"], disabled=ctx.busy):
                _choose(ctx, "ref_voice_id", row["id"])
        path = ctx.current_audio()
        if path:
            st.markdown("**当前生成结果**")
            st.audio(path)
    with clone:
        st.caption("上传自己有权使用的清晰录音或有声视频；视频提取前 30 秒作为样音。")
        upload = st.file_uploader("上传样音／视频", type=["wav", "mp3", "m4a", "ogg", "mp4", "mov"], key="ref_clone_upload_"+st.session_state.get("ref_video_import_scope", "draft"), disabled=ctx.busy)
        recording = st.audio_input("录制自己的样音", key="ref_clone_record_"+st.session_state.get("ref_video_import_scope", "draft"), disabled=ctx.busy)
        if recording:
            if not upload or st.radio("使用哪份样音", ["录制", "上传"], horizontal=True, key="ref_clone_source") == "录制":
                upload = recording
        name = st.text_input("音色名称", key="ref_clone_name", max_chars=80)
        transcript = st.text_area("样音说了什么（可选）", key="ref_clone_transcript", height=90)
        mode = st.radio("克隆方式", ["voxcpm", "archive"], format_func=lambda x: "VoxCPM 云端克隆" if x == "voxcpm" else "本机样音存档", horizontal=True, key="ref_clone_mode")
        st.caption("云端克隆需在配音设置填写自己的 API Key 和模型 ID，生成时提交样音与文案。" if mode == "voxcpm" else "样音保存在本机；请在 Duix 创建音色后刷新音色列表。")
        sample = ""
        if upload:
            try:
                sample = _voice_sample(ctx, upload)
                st.markdown("**原始声音**")
                st.audio(sample)
            except Exception as exc:
                st.error(str(exc))
        if st.button("保存克隆音色" if mode == "voxcpm" else "保存样音", key="ref_clone_save", type="primary", disabled=ctx.busy or not sample or not name.strip()):
            try:
                ctx.queue("voice_sample", "保存声音样本", narration.save_sample, name, sample, transcript=transcript, mode=mode)
                _dismiss()
                st.rerun(scope="app")
            except Exception as exc:
                st.error(str(exc))
        current = ctx.current_audio()
        if current:
            st.markdown("**生成声音（与原声比较）**")
            st.audio(current)


@st.dialog("选择形象", width="large", on_dismiss=_dismiss)
def avatar_browser(ctx):
    gallery, upload_tab = st.tabs(["视频形象", "上传新形象"])
    with gallery:
        try:
            rows = avatar.list_options()
        except (RuntimeError, ValueError, OSError) as exc:
            rows = []
            st.info(str(exc))
        if not rows:
            st.info("暂无视频形象，到“上传新形象”添加自己的参考视频。")
        for start in range(0, len(rows), 4):
            for column, row in zip(st.columns(4), rows[start:start+4]):
                with column, st.container(border=True):
                    try:
                        thumb = _thumbnail(row.get("video_path"))
                    except (RuntimeError, ValueError, OSError):
                        thumb = ""
                    if thumb:
                        st.image(thumb, width="stretch")
                    st.write(row["name"])
                    st.caption(row.get("reason") or "连续人物参考视频")
                    if st.button("选择", key="ref_pick_avatar_"+row["id"], width="stretch", disabled=ctx.busy or not row.get("available", True)):
                        _choose(ctx, "ref_avatar_id", row["id"])
    with upload_tab:
        name = st.text_input("形象名称", key="ref_new_avatar_name", max_chars=80)
        upload = st.file_uploader("自己的正面人物参考视频", type=["mp4", "mov", "mkv", "webm"], key="ref_new_avatar_upload_"+st.session_state.get("ref_video_import_scope", "draft"), disabled=ctx.busy)
        st.caption("光线稳定、脸部清楚；完整人物模式需要足够长的动作素材，避免循环画面。")
        if st.button("保存形象", key="ref_new_avatar_save", type="primary", disabled=ctx.busy or not upload or not name.strip()):
            try:
                ctx.queue("avatar_profile", "保存人物形象", avatar.import_profile, name, ctx.stage_upload(upload))
                _dismiss()
                st.rerun(scope="app")
            except Exception as exc:
                st.error(str(exc))
