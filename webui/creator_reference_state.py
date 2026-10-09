"""Connect the four-column creator surface to persisted jobs and projects."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import streamlit as st

from app.services.creator import extract, jobs, processing, rendering, store, workflow


def _file(value):
    return str(value) if value and Path(str(value)).is_file() else ""


def _media_stamp(path):
    source = Path(path).expanduser().resolve()
    stat = source.stat()
    return [str(source), stat.st_size, stat.st_mtime_ns]


def _defaults(project):
    config = project.get("config", {})
    stages = project.get("stages", {})
    script = stages.get("script", {}).get("result", {}).get("text") or config.get("input_text", "")
    return {
        "ref_original_text": script, "ref_script_text": script,
        "ref_voice_id": config.get("voice_id", "edge:zh-CN-XiaoxiaoNeural"),
        "ref_avatar_id": config.get("avatar_id", ""), "ref_speed": config.get("speed", 1.0),
        "ref_allow_paid": config.get("allow_paid", False),
        "ref_avatar_mode": config.get("avatar_mode", "mixed"),
        "ref_allow_reference_reuse": config.get("allow_reference_reuse", False),
        "ref_subtitles": config.get("subtitle_style", "clean") != "none",
        "ref_subtitle_style": config.get("subtitle_style", "clean") if config.get("subtitle_style") != "none" else "clean",
        "ref_color_grade": config.get("color_grade", "cool"), "ref_video_fit": config.get("video_fit", "cover"),
        "ref_bgm_volume": config.get("bgm_volume", 0.12), "ref_cover_style": config.get("cover_style", "clean"),
        "ref_cover_title": config.get("cover_title", ""), "ref_cover_frame_time": config.get("cover_frame_time", 0.),
        "ref_cover_aspect": config.get("cover_aspect") or config.get("aspect", "9:16"),
        "ref_bgm_enabled": bool(config.get("bgm_path")), "ref_bgm_path": config.get("bgm_path", ""),
        "ref_resolution": config.get("output_resolution", "720P"), "ref_publish_title": config.get("release_title", ""),
        "ref_publish_tags": " ".join(config.get("hashtags", [])),
        "ref_publish_description": config.get("description", ""), "ref_cover_path": "",
        "ref_word_count": 300, "ref_sidebar_view": "首页", "ref_theme": "light",
        "ref_creation_mode": "one_click",
        "ref_pending_actions": {}, "ref_last_message": "",
        "ref_pip_items": config.get("pip_items", []),
        "ref_silence_trim": config.get("silence_trim", False), "ref_silence_threshold": config.get("silence_threshold", -40.),
        "ref_silence_min_duration": config.get("silence_min_duration", .7),
        "ref_green_screen": config.get("green_screen", False), "ref_green_background_path": config.get("green_background_path", ""),
        "ref_green_color": config.get("green_color", "#00ff00"), "ref_green_similarity": config.get("green_similarity", .12),
        "ref_beauty_strength": config.get("beauty_strength", 0.),
        **{"ref_highlight_" + group: "\n".join(config.get("highlight_keywords", {}).get(name, [])) for group, name in
           (("main", "main"), ("description", "description"), ("action", "action"), ("emotion", "emotion"))},
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
        changed = st.session_state.get("ref_loaded_project") != marker or pending == ""
        if changed or "ref_video_import_scope" not in st.session_state:
            st.session_state["ref_video_import_scope"] = store.new_id()
        values = _defaults(self.project)
        persistent = {"ref_pending_actions", "ref_theme", "ref_sidebar_view", "ref_last_message"}
        for key, value in values.items():
            if key not in st.session_state or changed and key not in persistent:
                st.session_state[key] = value
        if changed:
            for key in ("ref_voice_audio_result", "ref_imported_video", "ref_imported_voice_video", "ref_audio_history",
                        "ref_media_preferences_pending", "ref_video_import_failure", "ref_script_review_result", "ref_script_review_open",
                        "ref_script_review_adopt_pending", "ref_asset_choice_pending", "ref_audio_choice_pending", "ref_pip_items_pending", "ref_imported_transcript", "ref_cover_binding"):
                st.session_state.pop(key, None)
            for key in ("ref_asset_browser", "ref_processing_open", "ref_pip_open"):
                st.session_state.pop(key, None)
            for key in ("ref_cover_settings_open", "ref_cover_settings_before", "ref_cover_cancel_pending"):
                st.session_state.pop(key, None)
            st.session_state.pop("ref_native_release_bundle", None)
        st.session_state["ref_loaded_project"] = marker
        self._consume_completed()
        self._apply_media_preferences()
        self._apply_pending_edits()

    def _apply_pending_edits(self):
        canceled = st.session_state.pop("ref_cover_cancel_pending", None)
        if canceled and canceled.get("project_id", "") == self.project.get("id", ""):
            for key, value in canceled["values"].items():
                if value is not None:
                    st.session_state[key] = value
        for pending_key in ("ref_asset_choice_pending", "ref_audio_choice_pending", "ref_pip_items_pending", "ref_script_review_adopt_pending"):
            value = st.session_state.pop(pending_key, None)
            if not isinstance(value, dict) or value.get("project_id", "") != self.project.get("id", ""):
                continue
            if pending_key == "ref_asset_choice_pending" and value.get("field") in {"ref_voice_id", "ref_avatar_id"}:
                st.session_state[value["field"]] = value["value"]
            elif pending_key == "ref_audio_choice_pending":
                if self.busy or not _file(value.get("audio_path")):
                    st.session_state["ref_last_error"] = "历史配音暂不可用，请等待任务完成或重新选择。"
                    continue
                text = value.get("text", "").strip()
                if text:
                    st.session_state["ref_script_text"] = text
                    self.save_script(text)
                self.use_media("audio", value["audio_path"])
            elif pending_key == "ref_pip_items_pending":
                st.session_state["ref_pip_items"] = value["items"]
            elif pending_key == "ref_script_review_adopt_pending":
                if value.get("source_text") != st.session_state.get("ref_script_text") or self.busy:
                    st.session_state["ref_last_error"] = "文案已变化或有任务运行，请重新审阅后采用。"
                    continue
                st.session_state["ref_script_text"] = value["optimized_text"]
                self.save_script(value["optimized_text"])

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
        config = {
            "input_mode": "script", "input_text": str(st.session_state.get("ref_script_text", "")).strip(),
            "voice_id": st.session_state.get("ref_voice_id") or "edge:zh-CN-XiaoxiaoNeural",
            "speed": st.session_state.get("ref_speed", 1.0),
            "allow_paid": bool(st.session_state.get("ref_allow_paid", False)),
            "avatar_id": st.session_state.get("ref_avatar_id") or "",
            "kind": "avatar" if st.session_state.get("ref_avatar_id") else self.project.get("config", {}).get("kind", "knowledge"),
            "avatar_mode": st.session_state.get("ref_avatar_mode", "mixed"),
            "allow_reference_reuse": bool(st.session_state.get("ref_allow_reference_reuse", False)),
            "subtitle_style": st.session_state.get("ref_subtitle_style", "clean") if st.session_state.get("ref_subtitles", True) else "none",
            "color_grade": st.session_state.get("ref_color_grade", self.project.get("config", {}).get("color_grade", "cool")),
            "video_fit": st.session_state.get("ref_video_fit", self.project.get("config", {}).get("video_fit", "cover")),
            "bgm_volume": st.session_state.get("ref_bgm_volume", .12),
            "bgm_path": st.session_state.get("ref_bgm_path", "") if st.session_state.get("ref_bgm_enabled", False) else "",
            "release_title": str(st.session_state.get("ref_publish_title", "")).strip(),
            "description": str(st.session_state.get("ref_publish_description", "")).strip(),
            "hashtags": [tag.lstrip("#") for tag in tags[:20]],
            "cover_title": st.session_state.get("ref_cover_title", ""),
            "cover_style": st.session_state.get("ref_cover_style", "clean"),
            "cover_aspect": st.session_state.get("ref_cover_aspect", "9:16"),
            "cover_frame_time": st.session_state.get("ref_cover_frame_time", 0.),
        }
        if self._pending_media_preferences():
            # The import may run after this event's radio was already rendered.
            # Keep its old mixed value from undoing the saved full-video choice.
            config.update(kind="avatar", avatar_mode="full", allow_reference_reuse=False)
        config.update({key: st.session_state.get("ref_" + key, self.project.get("config", {}).get(key, default))
                       for key, default in processing.DEFAULTS.items() if key in processing.RENDER_FIELDS and key not in {"output_resolution", "highlight_keywords"}})
        config["output_resolution"] = st.session_state.get("ref_resolution", "720P")
        config["highlight_keywords"] = {name: [term.strip() for term in str(st.session_state.get("ref_highlight_" + name, "")).splitlines() if term.strip()]
                                       for name in ("main", "description", "action", "emotion")}
        return config

    def _pending_media_preferences(self):
        pending = st.session_state.get("ref_media_preferences_pending")
        if not isinstance(pending, dict) or pending.get("project_id") != self.project.get("id"):
            return None
        source = self.project.get("config", {}).get("source_video_path")
        if not source or str(Path(source).resolve()) != pending.get("source_video_path"):
            return None
        return pending

    def _apply_media_preferences(self):
        pending = self._pending_media_preferences()
        st.session_state.pop("ref_media_preferences_pending", None)
        if pending:
            for key, value in pending["preferences"].items():
                st.session_state[key] = value

    def _ensure_project(self, changes=None):
        config = self._form_config()
        config.update(changes or {})
        if not str(config.get("input_text", "")).strip():
            raise ValueError("请先在左侧填写或提取口播文案。")
        if self.project:
            old_video = self.stage("render").get("video_path")
            self.project = workflow.update_project(self.project["id"], config)
            if old_video and not self.stage("render").get("video_path"):
                st.session_state["ref_cover_path"] = ""
                st.session_state.pop("ref_cover_binding", None)
                st.session_state.pop("ref_native_release_bundle", None)
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
            st.session_state.pop("ref_imported_transcript", None)
            st.session_state["ref_cover_path"] = ""
            st.session_state.pop("ref_native_release_bundle", None)
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
        if kind == "publish_copy":
            pending[ident]["copy_snapshot"] = {key: st.session_state.get(key, "") for key in
                                               ("ref_script_text", "ref_publish_title", "ref_publish_tags", "ref_publish_description")}
        elif kind == "cover" and args and _file(args[0]):
            pending[ident]["video_stamp"] = _media_stamp(args[0])
            pending[ident]["cover_snapshot"] = {key: st.session_state.get(key, "") for key in
                                                ("ref_cover_title", "ref_publish_title", "ref_cover_style", "ref_cover_aspect", "ref_cover_frame_time")}
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
            st.session_state.pop("ref_imported_transcript", None)
        elif kind == "video":
            if self.busy:
                raise ValueError("当前任务正在处理，请完成后再继续导入。")
            info = rendering.probe_source(path)
            if not info.get("has_video"):
                raise ValueError("请选择包含有效画面的口播视频。")
            path = str(Path(path).expanduser().resolve())
            st.session_state.pop("ref_video_import_failure", None)
            if str(st.session_state.get("ref_script_text", "")).strip():
                self._bind_imported_video(path, info)
            elif info.get("has_audio"):
                stamp = _media_stamp(path)
                scope = st.session_state["ref_video_import_scope"]
                config = self.project.get("config", {})
                ident = self.queue("video_import_extract", "识别导入口播视频", extract.extract_media, path,
                                   language=config.get("language", "zh"), model_size=config.get("model_size", "small"))
                st.session_state["ref_pending_actions"][ident].update(
                    source_video_path=path, media_stamp=stamp, import_scope=scope,
                    input_text=str(st.session_state.get("ref_script_text", "")))
                return ident
            else:
                raise ValueError("这段视频没有声音，也没有口播文案。请先填写文案并准备配音，或导入带声口播视频。")
        else:
            self._ensure_project({"materials": [*self.project.get("config", {}).get("materials", []), path]})

    def _bind_imported_video(self, path, info, text=None):
        changes = {"source_video_path": path, "kind": "avatar", "avatar_mode": "full", "allow_reference_reuse": False}
        if info.get("has_audio"):
            changes["audio_path"] = ""
        if text is not None:
            changes.update(input_mode="script", input_text=text)
        self._ensure_project(changes)
        st.session_state.pop("ref_voice_audio_result", None)
        st.session_state.pop("ref_imported_transcript", None)
        st.session_state["ref_cover_path"] = ""
        st.session_state["ref_imported_video"] = path
        st.session_state["ref_imported_voice_video"] = path
        st.session_state["ref_media_preferences_pending"] = {
            "project_id": self.project["id"], "source_video_path": path,
            "preferences": {"ref_avatar_mode": "full", "ref_allow_reference_reuse": False},
        }

    def _consume_video_import(self, meta, job):
        if (meta.get("project_id", "") != self.project.get("id", "")
                or meta.get("import_scope") != st.session_state.get("ref_video_import_scope")):
            return
        st.session_state["ref_last_message"] = meta["label"] + " · " + job.get("message", "")
        path = meta.get("source_video_path", "")
        try:
            if job.get("state") != "done":
                raise ValueError(job.get("message") or "导入文案识别未完成，请检查声音后重新导入。")
            if str(st.session_state.get("ref_script_text", "")) != meta.get("input_text", ""):
                raise ValueError("文案已修改，导入识别结果没有覆盖当前内容。请核对后重新导入视频。")
            result = job.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not result["text"].strip():
                raise ValueError("未识别到可用口播。请填写文案或导入有清晰人声的视频。")
            if not isinstance(result.get("media_path"), str) or not result["media_path"] or str(Path(result["media_path"]).resolve()) != path:
                raise ValueError("识别结果与导入的视频不一致，请重新导入。")
            if not _file(path):
                raise ValueError("视频已移动或删除，请重新导入。")
            if _media_stamp(path) != meta.get("media_stamp"):
                raise ValueError("视频在识别期间已改动，请重新导入。")
            info = rendering.probe_source(path)
            if not info.get("has_video") or not info.get("has_audio"):
                raise ValueError("导入的视频已无法读取完整画面和声音，请重新导入。")
            text = result["text"].strip()
            self._bind_imported_video(path, info, text=text)
            st.session_state["ref_original_text"] = text
            st.session_state["ref_script_text"] = text
            st.session_state["ref_imported_transcript"] = {
                "project_id": self.project["id"], "source_video_path": path, "media_stamp": _media_stamp(path),
                "duration": result.get("duration", info["duration"]), "segments": deepcopy(result.get("segments", [])),
            }
        except (ValueError, OSError) as exc:
            # Retain the attempted-upload marker. The uploader keeps its file
            # across reruns, so clearing it would silently queue ASR forever.
            st.session_state["ref_imported_voice_video"] = path
            st.session_state["ref_video_import_failure"] = {
                "project_id": self.project.get("id", ""), "source_video_path": path, "message": str(exc),
            }
            st.session_state["ref_last_error"] = str(exc)

    def current_audio(self):
        return (_file(st.session_state.get("ref_voice_audio_result", {}).get("audio_path"))
                or _file(self.stage("voice").get("audio_path")) or _file(self.project.get("config", {}).get("audio_path")))

    def current_video(self, rendered=False):
        if rendered:
            return _file(self.stage("render").get("video_path"))
        return (_file(self.stage("voice").get("avatar_video_path")) or _file(self.stage("visuals").get("video_path"))
                or _file(st.session_state.get("ref_imported_video")) or _file(self.project.get("config", {}).get("source_video_path")))

    def current_cover(self):
        binding = st.session_state.get("ref_cover_binding")
        path = _file(st.session_state.get("ref_cover_path"))
        if binding:
            video = self.current_video(rendered=True)
            if not video or binding.get("project_id") != self.project.get("id", "") or binding.get("video_stamp") != _media_stamp(video):
                path = ""
        return path or _file(self.stage("release").get("cover_path"))

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

    def _can_open_result_dialog(self):
        return not any(st.session_state.get(key) for key in ("ref_active_tool", "ref_asset_browser", "ref_processing_open", "ref_pip_open", "ref_cover_settings_open"))

    def _consume_completed(self):
        pending = dict(st.session_state.get("ref_pending_actions", {}))
        for ident, meta in list(pending.items()):
            job = jobs.get_job(ident)
            if not job:
                del pending[ident]
                if meta.get("project_id", "") == self.project.get("id", ""):
                    st.session_state["ref_last_message"] = "任务记录已不可用，请到任务中心核查，系统没有自动重试。"
                continue
            if job.get("state") in {"queued", "running"}:
                continue
            del pending[ident]
            if meta["kind"] == "video_import_extract":
                self._consume_video_import(meta, job)
                continue
            if meta["kind"] == "viral_collect":
                result = job.get("result")
                if job.get("state") == "done" and isinstance(result, dict):
                    st.session_state["ref_viral_result"] = result
                    st.session_state["ref_last_message"] = result.get("message", "采集已结束")
                    if meta.get("project_id", "") == self.project.get("id", "") and self._can_open_result_dialog():
                        st.session_state["ref_script_dialog"] = "library"
                        st.session_state["ref_pending_learning_mode"] = "爆款文案"
                else:
                    st.session_state["ref_viral_result"] = {"status": "failed", "message": job.get("message", "采集未完成")}
                continue
            if meta.get("project_id", "") != self.project.get("id", ""):
                continue
            st.session_state["ref_last_message"] = meta["label"] + " · " + job.get("message", "")
            if job.get("state") != "done":
                st.session_state["ref_last_error"] = job.get("message", "处理未完成")
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
                if meta.get("video_stamp"):
                    video = self.current_video(rendered=True)
                    snapshot = meta.get("cover_snapshot", {})
                    if (not video or _media_stamp(video) != meta["video_stamp"] or
                            any(st.session_state.get(key, "") != value for key, value in snapshot.items())):
                        st.session_state["ref_last_message"] = "封面对应的成片或设置已变化，请重新生成。"
                        continue
                    st.session_state["ref_cover_binding"] = {"project_id": self.project.get("id", ""), "video_stamp": meta["video_stamp"]}
                st.session_state["ref_cover_path"] = _file(result.get("cover_path"))
            elif kind == "script_review" and isinstance(result, dict):
                st.session_state["ref_script_review_result"] = result
                st.session_state["ref_script_review_open"] = self._can_open_result_dialog() and not st.session_state.get("ref_script_dialog")
            elif kind in {"voice_sample", "avatar_profile"}:
                st.session_state["ref_last_message"] = "素材已保存，可在音色或形象列表中选择。"
            elif kind == "publish_copy" and isinstance(result, dict):
                snapshot = meta.get("copy_snapshot", {})
                if snapshot and st.session_state.get("ref_script_text", "") != snapshot["ref_script_text"]:
                    st.session_state["ref_last_message"] = "正文已修改，已保留现有标题，请按新文案重新生成。"
                    continue
                titles = result.get("titles") or []
                tags = result.get("tags") or result.get("hashtags") or []
                fields = {"ref_publish_title": result.get("title") or (titles[0] if titles else ""),
                          "ref_publish_tags": tags if isinstance(tags, str) else " ".join(tags),
                          "ref_publish_description": result.get("description", "")}
                for key, value in fields.items():
                    if key not in snapshot or st.session_state.get(key, "") == snapshot[key]:
                        st.session_state[key] = value
            elif kind == "pipeline":
                self.project = workflow.get_project(self.project["id"]) or self.project
                released = self.stage("release")
                if released.get("cover_path"):
                    st.session_state["ref_cover_path"] = released["cover_path"]
                fields = {
                    "ref_publish_title": released.get("title", ""),
                    "ref_publish_description": released.get("description", ""),
                    "ref_publish_tags": " ".join("#" + tag.lstrip("#") for tag in released.get("hashtags", [])),
                }
                for key, value in fields.items():
                    if not str(st.session_state.get(key, "")).strip():
                        st.session_state[key] = value
        st.session_state["ref_pending_actions"] = pending


@st.fragment(run_every="3s")
def poll_reference_jobs():
    for ident in st.session_state.get("ref_pending_actions", {}):
        job = jobs.get_job(ident)
        if job and job.get("state") not in {"queued", "running"}:
            st.rerun(scope="app")
