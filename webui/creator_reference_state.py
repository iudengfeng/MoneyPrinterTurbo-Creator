"""Connect the four-column creator surface to persisted jobs and projects."""
from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st

from app.services.creator import jobs, store, workflow


def _file(value):
    return str(value) if value and Path(str(value)).is_file() else ""


def _defaults(project):
    config = project.get("config", {})
    stages = project.get("stages", {})
    script = stages.get("script", {}).get("result", {}).get("text") or config.get("input_text", "")
    return {
        "ref_original_text": script, "ref_script_text": script,
        "ref_voice_id": config.get("voice_id", "edge:zh-CN-XiaoxiaoNeural"),
        "ref_avatar_id": config.get("avatar_id", ""), "ref_speed": config.get("speed", 1.0),
        "ref_avatar_mode": config.get("avatar_mode", "mixed"),
        "ref_allow_reference_reuse": config.get("allow_reference_reuse", False),
        "ref_subtitles": config.get("subtitle_style", "clean") != "none",
        "ref_subtitle_style": config.get("subtitle_style", "clean") if config.get("subtitle_style") != "none" else "clean",
        "ref_color_grade": config.get("color_grade", "cool"), "ref_video_fit": config.get("video_fit", "cover"),
        "ref_bgm_volume": config.get("bgm_volume", 0.12), "ref_cover_style": config.get("cover_style", "clean"),
        "ref_cover_aspect": config.get("aspect", "9:16"),
        "ref_bgm_enabled": bool(config.get("bgm_path")), "ref_bgm_path": config.get("bgm_path", ""),
        "ref_resolution": "720P", "ref_publish_title": config.get("release_title", ""),
        "ref_publish_tags": " ".join(config.get("hashtags", [])),
        "ref_publish_description": config.get("description", ""), "ref_cover_path": "",
        "ref_word_count": 300, "ref_sidebar_view": "首页", "ref_theme": "light",
        "ref_pending_actions": {}, "ref_last_message": "",
    }


class ReferenceContext:
    def __init__(self):
        requested = str(st.query_params.get("project", ""))
        pending = st.session_state.pop("studio_pending_project", None)
        if pending is not None:
            st.session_state["ref_sidebar_view"] = "首页"
            st.session_state.pop("ref_active_tool", None)
            if pending:
                st.query_params["project"] = pending
            else:
                st.query_params.pop("project", None)
        ident = pending if pending is not None else requested or st.session_state.get("ref_current_project", "")
        try:
            self.project = workflow.get_project(ident) if ident else {}
        except KeyError:
            self.project = {}
        self.project = self.project or {}
        st.session_state["ref_current_project"] = self.project.get("id", "")
        marker = self.project.get("id", "")
        changed = st.session_state.get("ref_loaded_project") != marker
        values = _defaults(self.project)
        persistent = {"ref_pending_actions", "ref_theme", "ref_sidebar_view", "ref_last_message"}
        for key, value in values.items():
            if key not in st.session_state or changed and key not in persistent:
                st.session_state[key] = value
        if changed:
            for key in ("ref_voice_audio_result", "ref_imported_video", "ref_imported_voice_video", "ref_audio_history"):
                st.session_state.pop(key, None)
        st.session_state["ref_loaded_project"] = marker
        self._consume_completed()

    def stage(self, name):
        return dict(self.project.get("stages", {}).get(name, {}).get("result", {}))

    @property
    def busy(self):
        if self.project.get("state") in {"queued", "running"}:
            return True
        for ident in st.session_state.get("ref_pending_actions", {}):
            job = jobs.get_job(ident)
            if job and job.get("state") in {"queued", "running"}:
                return True
        return False

    def _form_config(self):
        tags = str(st.session_state.get("ref_publish_tags", "")).replace("，", " ").replace(",", " ").split()
        return {
            "input_mode": "script", "input_text": str(st.session_state.get("ref_script_text", "")).strip(),
            "voice_id": st.session_state.get("ref_voice_id") or "edge:zh-CN-XiaoxiaoNeural",
            "speed": st.session_state.get("ref_speed", 1.0),
            "avatar_id": st.session_state.get("ref_avatar_id") or "",
            "kind": "avatar" if st.session_state.get("ref_avatar_id") else self.project.get("config", {}).get("kind", "knowledge"),
            "avatar_mode": st.session_state.get("ref_avatar_mode", "mixed"),
            "allow_reference_reuse": bool(st.session_state.get("ref_allow_reference_reuse", False)),
            "subtitle_style": "clean" if st.session_state.get("ref_subtitles", True) else "none",
            "bgm_path": st.session_state.get("ref_bgm_path", "") if st.session_state.get("ref_bgm_enabled", False) else "",
            "release_title": str(st.session_state.get("ref_publish_title", "")).strip(),
            "description": str(st.session_state.get("ref_publish_description", "")).strip(),
            "hashtags": [tag.lstrip("#") for tag in tags[:20]],
        }

    def _ensure_project(self, changes=None):
        config = self._form_config()
        config.update(changes or {})
        if not str(config.get("input_text", "")).strip():
            raise ValueError("请先在左侧填写或提取口播文案。")
        if self.project:
            self.project = workflow.update_project(self.project["id"], config)
        else:
            self.project = workflow.create_project(config)
            st.session_state["ref_current_project"] = self.project["id"]
            st.session_state["ref_loaded_project"] = self.project["id"]
            st.query_params["project"] = self.project["id"]
        return self.project

    def save_script(self, text):
        text = str(text or "").strip()
        if not text:
            # Empty editable drafts are valid; production still requires text.
            return
        changes = {"input_mode": "script", "input_text": text}
        if self.project and self.project.get("config", {}).get("input_text") != text:
            changes.update(audio_path="", source_video_path="")
            st.session_state.pop("ref_voice_audio_result", None)
            st.session_state.pop("ref_imported_video", None)
        self._ensure_project(changes)

    def queue(self, kind, label, operation, *args, **kwargs):
        if self.busy:
            raise ValueError("当前任务正在处理，请完成后再继续。")
        if kind == "voice_audio":
            if self.project.get("stages", {}).get("voice", {}).get("remote_request", {}).get("state") == "sending":
                raise ValueError("上一次配音请求结果待核查，请先到服务端确认后再生成，避免重复计费。")
            self._ensure_project({"audio_path": "", "source_video_path": ""})
        ident = jobs.submit(label, operation, *args, **kwargs)
        pending = dict(st.session_state.get("ref_pending_actions", {}))
        pending[ident] = {"kind": kind, "project_id": self.project.get("id", ""), "label": label}
        st.session_state["ref_pending_actions"] = pending
        st.session_state["ref_last_message"] = label + " · 已加入任务"
        st.toast("已加入任务，完成后会显示结果。")
        return ident

    def submit_stage(self, stage, changes=None):
        if self.busy:
            raise ValueError("当前任务正在处理，请完成后再继续。")
        self._ensure_project(changes)
        ident = workflow.submit_project(self.project["id"], until_stage=stage)
        pending = dict(st.session_state.get("ref_pending_actions", {}))
        pending[ident] = {"kind": "pipeline", "project_id": self.project["id"], "label": "视频制作"}
        st.session_state["ref_pending_actions"] = pending
        st.session_state["ref_last_message"] = "视频制作 · 已加入任务"
        st.toast("已开始制作，可以继续查看当前进度。")
        return ident

    def stage_upload(self, upload):
        data = upload.getvalue()
        folder = store.data_root() / "imports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (hashlib.sha256(data).hexdigest() + Path(upload.name).suffix.lower())
        if not path.exists():
            path.write_bytes(data)
        return str(path)

    def use_media(self, kind, path):
        path = _file(path)
        if not path:
            raise ValueError("选择的文件已不存在，请重新导入。")
        if kind == "audio":
            self._ensure_project({"audio_path": path, "source_video_path": ""})
            st.session_state["ref_voice_audio_result"] = {"audio_path": path}
        elif kind == "video":
            self._ensure_project({"source_video_path": path})
            st.session_state["ref_imported_video"] = path
        else:
            self._ensure_project({"materials": [*self.project.get("config", {}).get("materials", []), path]})

    def current_audio(self):
        return (_file(st.session_state.get("ref_voice_audio_result", {}).get("audio_path"))
                or _file(self.stage("voice").get("audio_path")) or _file(self.project.get("config", {}).get("audio_path")))

    def current_video(self, rendered=False):
        if rendered:
            return _file(self.stage("render").get("video_path"))
        return (_file(self.stage("voice").get("avatar_video_path")) or _file(self.stage("visuals").get("video_path"))
                or _file(st.session_state.get("ref_imported_video")) or _file(self.project.get("config", {}).get("source_video_path")))

    def current_cover(self):
        return _file(st.session_state.get("ref_cover_path")) or _file(self.stage("release").get("cover_path"))

    def open_tool(self, name):
        st.session_state["ref_active_tool"] = name
        st.rerun()

    def report_job_state(self):
        pending = st.session_state.get("ref_pending_actions", {})
        if pending:
            job = jobs.get_job(next(reversed(pending))) or {}
            if job.get("state") in {"queued", "running"}:
                st.caption(job.get("message", "正在处理"))
                st.progress(max(0.0, min(1.0, float(job.get("progress", 0)) / 100)))
        elif st.session_state.get("ref_last_message"):
            st.caption(st.session_state["ref_last_message"])

    def _consume_completed(self):
        pending = dict(st.session_state.get("ref_pending_actions", {}))
        for ident, meta in list(pending.items()):
            job = jobs.get_job(ident)
            if not job or job.get("state") in {"queued", "running"}:
                continue
            del pending[ident]
            st.session_state["ref_last_message"] = meta["label"] + " · " + job.get("message", "")
            if job.get("state") != "done":
                st.session_state["ref_last_error"] = job.get("message", "处理未完成")
                continue
            if meta.get("project_id", "") != self.project.get("id", ""):
                continue
            result = job.get("result") or {}
            kind = meta["kind"]
            if kind in {"extract", "script"} and isinstance(result, dict) and result.get("text"):
                if kind == "extract":
                    st.session_state["ref_original_text"] = result["text"]
                st.session_state["ref_script_text"] = result["text"]
                self.save_script(result["text"])
            elif kind == "voice_audio" and isinstance(result, dict):
                self.use_media("audio", result.get("audio_path", ""))
            elif kind == "cover" and isinstance(result, dict):
                st.session_state["ref_cover_path"] = _file(result.get("cover_path"))
            elif kind == "publish_copy" and isinstance(result, dict):
                titles = result.get("titles") or []
                st.session_state["ref_publish_title"] = result.get("title") or (titles[0] if titles else "")
                tags = result.get("tags") or result.get("hashtags") or []
                st.session_state["ref_publish_tags"] = tags if isinstance(tags, str) else " ".join(tags)
                st.session_state["ref_publish_description"] = result.get("description", "")
            elif kind == "pipeline":
                self.project = workflow.get_project(self.project["id"]) or self.project
                if self.stage("release").get("cover_path"):
                    st.session_state["ref_cover_path"] = self.stage("release")["cover_path"]
        st.session_state["ref_pending_actions"] = pending


@st.fragment(run_every="3s")
def poll_reference_jobs():
    for ident in st.session_state.get("ref_pending_actions", {}):
        job = jobs.get_job(ident)
        if job and job.get("state") not in {"queued", "running"}:
            st.rerun(scope="app")
