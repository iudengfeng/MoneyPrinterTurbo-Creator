"""Manual step 02: pick a voice, set its pace, listen, then continue."""
from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st

from app.services.creator import jobs, narration, store


def _stage(upload):
    data = upload.getvalue()
    folder = store.data_root() / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (hashlib.sha256(data).hexdigest() + Path(upload.name).suffix.lower())
    if not target.is_file():
        target.write_bytes(data)
    return str(target)


def _use_narration(row, destination):
    st.session_state["creator_render_audio"] = row["audio_path"]
    st.session_state["creator_srt_path"] = ""
    st.session_state["creator_video_has_subtitles"] = False
    if destination == "avatar":
        st.session_state["creator_narration_result"] = row
        st.session_state["creator_avatar_audio_source"] = "本次完整配音"
        st.session_state["creator_selected_tab"] = "数字人"
    elif destination == "render":
        st.session_state["creator_render_replace_audio"] = True
        st.session_state.pop("creator_render_audio_import", None)
        st.session_state.pop("creator_render_subtitle_import", None)
        st.session_state.pop("creator_render_subtitle_upload", None)
        st.session_state.pop("creator_render_audio_upload", None)
        st.session_state["creator_render_subtitle_mode"] = "自动识别生成"
        st.session_state["creator_selected_tab"] = "模板剪辑"
    else:
        st.session_state["custom_audio_file_path"] = row["audio_path"]
        st.session_state["video_script"] = row.get("text", "")
        st.session_state["creator_route"] = "视频制作"
    st.session_state["creator_navigation_pending"] = True


@st.dialog("声音资产管理", width="large")
def _upload_voice():
    st.caption("为声音命名，上传一段清晰、单人说话的样音。建议先准备约 10～15 秒录音。")
    name = st.text_input("音色名称", placeholder="例如：我的声音 · 自然", key="creator_narration_sample_name")
    mode = st.radio("使用方式", ["在 Duix 中创建本机音色", "VoxCPM 云端声音克隆"], key="creator_narration_sample_mode")
    upload = st.file_uploader("上传声音样本", type=["wav", "mp3", "m4a", "aac"], key="creator_narration_sample")
    transcript = st.text_area("样音对应的文字（可选）", key="creator_narration_sample_text", height=100)
    if upload:
        st.audio(upload.getvalue())
    if mode == "在 Duix 中创建本机音色":
        st.info("保存后会建立样音档案。请在 Duix 中用这段录音创建音色，完成后刷新音色列表。")
    else:
        st.caption("云端克隆使用原设置中的 VoxCPM API Key 和模型 ID，录音及配音文字会提交给该服务。")
    if st.button("保存音色", type="primary", width="stretch"):
        try:
            if not upload:
                raise ValueError("请先上传声音样本。")
            row = narration.save_sample(name, _stage(upload), transcript=transcript, mode="archive" if mode == "在 Duix 中创建本机音色" else "voxcpm")
            st.session_state["creator_narration_sample_saved"] = row
            st.success("样音档案已保存。" if mode == "在 Duix 中创建本机音色" else "云端克隆音色已保存，可在列表中选择。")
        except Exception as exc:
            st.error(str(exc))
    if st.session_state.get("creator_narration_sample_saved"):
        row = st.session_state["creator_narration_sample_saved"]
        sample = row.get("sample_path") or row.get("audio_path")
        if sample and Path(sample).is_file():
            st.download_button("下载已保存的样音", Path(sample).read_bytes(), file_name="声音样本" + Path(sample).suffix)
    if st.button("完成，刷新音色列表", width="stretch"):
        st.rerun(scope="app")


def _submit(text, option, speed, preview=False):
    try:
        def generate(progress=None):
            return narration.generate(text, option["id"], speed=speed, emotion="自然", progress=progress, preview=preview)
        ident = jobs.submit(("音色试听" if preview else "文案配音") + " · " + option["name"], generate)
        st.session_state["creator_narration_pending"] = {"id": ident, "preview": preview}
        st.session_state["creator_last_job"] = ident
    except Exception as exc:
        st.error(str(exc))


def _collect_result():
    pending = st.session_state.get("creator_narration_pending")
    if not pending:
        return
    row = jobs.get_job(pending["id"])
    if not row or row["state"] in {"queued", "running"}:
        return
    st.session_state.pop("creator_narration_pending", None)
    if row["state"] == "done" and isinstance(row.get("result"), dict):
        key = "creator_narration_preview_result" if pending.get("preview") else "creator_narration_result"
        st.session_state[key] = row["result"]
        st.session_state["creator_narration_notice"] = ("success", "试听已生成。" if pending.get("preview") else "配音已生成并保存，请试听确认。")
    else:
        st.session_state["creator_narration_notice"] = ("error", row.get("message", "配音没有完成，请检查音色服务。"))


@st.fragment(run_every="2s")
def _progress():
    pending = st.session_state.get("creator_narration_pending")
    if not pending:
        return
    row = jobs.get_job(pending["id"])
    if row and row["state"] not in {"running", "queued"}:
        st.rerun(scope="app")
    if row:
        st.info(row.get("message", "正在生成配音。"))
        st.progress(float(row.get("progress", 0)) / 100)


def _result(row, key):
    path = Path(row.get("audio_path", ""))
    if not path.is_file():
        st.warning("配音文件已移动或删除，请重新生成。")
        return
    st.caption(f'{row.get("voice_name", "配音")} · {row.get("speed", 1):.1f}× · {row.get("duration", 0):.1f} 秒')
    st.audio(str(path))
    left, right = st.columns(2)
    left.download_button("下载配音", path.read_bytes(), file_name="口播配音" + path.suffix, key=key + "_download", width="stretch")
    right.button("用于模板剪辑 →", key=key + "_render", type="primary", width="stretch", on_click=_use_narration, args=(row, "render"))
    st.button("确认配音，下一步数字人 →", key=key + "_avatar", type="primary", width="stretch", on_click=_use_narration, args=(row, "avatar"))


def _remember_text():
    st.session_state["creator_voice_draft"] = st.session_state.get("creator_voice_text", "")


def render():
    _collect_result()
    if "creator_voice_text" not in st.session_state:
        st.session_state["creator_voice_text"] = st.session_state.get("creator_voice_draft", "")
    st.markdown('<div class="script-eyebrow">STEP 02 · 配音</div>', unsafe_allow_html=True)
    notice = st.session_state.pop("creator_narration_notice", None)
    if notice:
        getattr(st, notice[0])(notice[1])
    _progress()
    options = narration.list_options()
    with st.container(key="creator_narration_grid"):
        left, right = st.columns([1, 2.5], gap="large")
    with left, st.container(key="creator_narration_voices"):
        st.markdown('<div class="voice-brief"><h2>用你的声音<br>来表达这篇内容。</h2></div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        if c1.button("上传音色", width="stretch"):
            _upload_voice()
        c2.button("刷新列表", width="stretch")
        ids = [row["id"] for row in options]
        names = {row["id"]: row["name"] for row in options}
        if st.session_state.get("creator_narration_voice") not in ids:
            st.session_state["creator_narration_voice"] = ids[0] if ids else None
        if not ids:
            st.warning("还没有可用音色，请保存声音或检查本机音色。")
            return
        selected = st.radio("选择音色", ids, format_func=lambda ident: names[ident], key="creator_narration_voice", label_visibility="collapsed",persist_state="session")
        option = next(row for row in options if row["id"] == selected)
        st.caption(option.get("description", ""))
        samples = store.list_records("voice_samples")
        if samples:
            with st.expander(f"待创建的本机音色 · {len(samples)}"):
                for row in samples[:10]:
                    st.write(row.get("name", "样音"))
                    path = Path(row.get("sample_path", ""))
                    if path.is_file():
                        st.audio(str(path))
                        st.download_button("下载样音", path.read_bytes(), file_name="声音样本.wav", key="voice_sample_" + row["id"])
                st.caption("在 Duix 中创建音色后，点击刷新列表即可选择。")
    with right:
        busy = bool(st.session_state.get("creator_narration_pending"))
        with st.container(border=True, key="creator_narration_text"):
            text = st.text_area("配音文案", key="creator_voice_text", height=180, placeholder="第一步确认的文案会自动带过来，也可以直接粘贴。", disabled=busy, on_change=_remember_text,persist_state="session")
            st.session_state["creator_voice_draft"] = text
            st.caption(f"当前 {len(''.join(text.split()))} 字 · 配音时完整保留文字")
        with st.container(border=True, key="creator_narration_emotion"):
            st.markdown("**情绪**")
            st.pills("情绪选择", ["自然"], default="自然", selection_mode="single", disabled=True, label_visibility="collapsed")
            st.caption("当前音色接口支持自然语气；其他情绪需要接入支持情绪控制的声音模型。")
        with st.container(border=True, key="creator_narration_speed_panel"):
            a, b = st.columns([4, 1])
            a.markdown("**语速**")
            st.session_state.setdefault("creator_narration_speed", 1.0)
            speed = st.slider("语速倍率", 0.8, 1.2, step=0.05, key="creator_narration_speed", disabled=busy, label_visibility="collapsed",persist_state="session")
            b.markdown(f"**{speed:.2f}×**")
            st.caption("语速调整会实际作用到音频；正常速度为 1.0×。")
            a, b = st.columns([1, 1.5])
            if a.button("试听前 80 字", width="stretch", disabled=busy or not text.strip()):
                _submit(text[:80], option, speed, preview=True)
                st.rerun()
            if b.button("开始配音", type="primary", width="stretch", disabled=busy or not text.strip()):
                _submit(text, option, speed)
                st.rerun()
        if st.session_state.get("creator_narration_result"):
            with st.container(border=True, key="creator_narration_result_panel"):
                st.markdown("**试听与下载**")
                _result(st.session_state["creator_narration_result"], "creator_narration_current")
        if st.session_state.get("creator_narration_preview_result"):
            row = st.session_state["creator_narration_preview_result"]
            with st.expander("短试听 · 前 80 字", expanded=not st.session_state.get("creator_narration_result")):
                path = Path(row.get("audio_path", ""))
                if path.is_file():
                    st.caption("用于检查音色和语速。制作视频请使用完整配音。")
                    st.audio(str(path))
                    st.download_button("下载试听",path.read_bytes(),file_name="音色试听.wav",key="creator_narration_preview_download")
    rows = [row for row in narration.list_narrations() if not row.get("preview")]
    if rows:
        with st.expander("我的配音 · 历史记录"):
            for row in rows[:15]:
                with st.expander(row.get("text", "配音")[:35] + " · " + row.get("voice_name", "")):
                    _result(row, "creator_narration_history_" + row["id"])
