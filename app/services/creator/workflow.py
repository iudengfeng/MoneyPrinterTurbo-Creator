"""Durable creator works shared by guided and automatic generation.

The background job is only a worker. SQLite owns configuration, checkpoints and
stage results; an OS-held owner lock distinguishes a restart from another live
instance. A remote request with an unknown outcome is never retried silently.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import avatar, brand_profiles, competitors, content_templates, extract, jobs, narration, processing, quality, release_assets, rendering, spoken_library, store, topics

STAGES = ("script", "voice", "visuals", "render", "release")
_KIND = "creator_projects"
_OWNER = store.new_id()
_OWNER_HANDLES = {}
_SUBMIT_LOCK = threading.RLock()
_DEFAULTS = {
    "input_mode": "script", "input_text": "", "kind": "knowledge",
    "voice_id": "edge:zh-CN-XiaoxiaoNeural", "speed": 1.0, "avatar_id": "",
    "avatar_mode": "mixed", "allow_reference_reuse": False,
    "materials": [], "aspect": "9:16", "template": "talking", "style": "knowledge",
    "subtitle_style": "clean", "bgm_path": "", "bgm_volume": 0.12,
    "source_video_path": "", "audio_path": "", "allow_paid": False,
    "release_title": "", "description": "", "hashtags": [], "cover_style": "clean",
    "cover_frame_time": 0.0, "writing_style": "科普干货", "account_id": "",
    "color_grade": "cool", "video_fit": "cover", "image_path": "",
    "language": "zh", "model_size": "small",
    "brand_profile_id": "", "brand_snapshot": {}, "video_purpose": "",
    "content_template": "", "target_duration": 0,
}
_BRIEF_FIELDS = {"brand_profile_id", "brand_snapshot", "video_purpose", "content_template", "target_duration"}
_FIELDS = {
    "script": {"input_mode", "input_text", "writing_style", "account_id"},
    "voice": {"voice_id", "speed", "avatar_id", "audio_path", "source_video_path", "language", "model_size", "kind", "aspect", "avatar_mode", "allow_reference_reuse"},
    "visuals": {"materials", "kind", "aspect", "avatar_mode", "avatar_id"},
    "render": {"template", "style", "subtitle_style", "bgm_path", "bgm_volume", "color_grade", "video_fit", "image_path"},
    "release": {"release_title", "description", "hashtags", "cover_style", "cover_frame_time"},
}
_DEFAULTS.update(copy.deepcopy(processing.DEFAULTS))
_FIELDS["render"].update(processing.RENDER_FIELDS)
_FIELDS["release"].update(processing.RELEASE_FIELDS)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _empty_stage():
    return {"state": "pending", "result": {}, "error": "", "fingerprint": ""}


def _normalize(config, *, previous=None):
    if not isinstance(config, dict):
        raise ValueError("作品设置必须是对象。")
    unknown = set(config) - set(_DEFAULTS) - {"title"}
    if unknown:
        raise ValueError("未知作品设置：" + ", ".join(sorted(unknown)))
    result = copy.deepcopy(_DEFAULTS)
    result.update(copy.deepcopy(config))
    result.update(processing.normalize(result))
    for field, choices in {
        "input_mode": {"script", "topic"}, "kind": {"knowledge", "montage", "product", "avatar"},
        "avatar_mode": {"mixed", "full"},
        "aspect": {"9:16", "16:9", "1:1"}, "template": {"talking", "pip", "cards"},
        "style": {"clean", "bold", "knowledge", "business"},
        "subtitle_style": {"clean", "bold", "yellow", "none"},
        "color_grade": {"none", "warm", "cool", "vivid"}, "video_fit": {"contain", "cover"},
        "cover_style": {row["id"] for row in release_assets.list_styles()},
        "writing_style": set(topics.STYLE_PRESETS), "model_size": {"tiny", "base", "small", "medium", "large-v3"},
    }.items():
        if result[field] not in choices:
            raise ValueError(f"不支持的 {field} 设置。")
    for field, limit in {"input_text": 6000, "title": 120, "release_title": 120, "description": 2000,
                         "voice_id": 200, "avatar_id": 200, "account_id": 200, "language": 30,
                         "brand_profile_id": 200, "video_purpose": 60, "content_template": 100}.items():
        value = result.get(field, "")
        if not isinstance(value, str) or len(value) > limit or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
            raise ValueError(f"{field} 内容无效或过长。")
        result[field] = value.strip()
    if not result["input_text"]:
        raise ValueError("请先输入主题或口播文案。")
    if result["input_mode"] == "topic" and not result["video_purpose"] and len(result["input_text"]) > 300:
        raise ValueError("视频主题最多支持 300 字；长资料请整理为主题，或选择现成文案。")
    purposes = {row["id"] for row in content_templates.list_purposes()}
    if result["video_purpose"] and result["video_purpose"] not in purposes:
        raise ValueError("请选择可用的视频用途。")
    if result["content_template"]:
        template = content_templates.get_template(result["content_template"])
        if not template:
            raise ValueError("请选择可用的内容模板。")
        if result["video_purpose"] and template["purpose_id"] != result["video_purpose"]:
            raise ValueError("内容模板与视频用途不匹配，请重新选择。")
    duration = result["target_duration"]
    if isinstance(duration, bool) or not isinstance(duration, int) or not (duration == 0 or 10 <= duration <= 300):
        raise ValueError("目标时长必须为 10～300 秒的整数，或留空。")
    supplied_snapshot = result["brand_snapshot"]
    if not isinstance(supplied_snapshot, dict):
        raise ValueError("品牌资料快照必须是对象。")
    profile_id = result["brand_profile_id"]
    retained = bool(previous and profile_id and profile_id == previous.get("brand_profile_id")
                    and supplied_snapshot == previous.get("brand_snapshot") and supplied_snapshot)
    if profile_id and not retained:
        local_snapshot = brand_profiles.snapshot(profile_id)
        if supplied_snapshot and supplied_snapshot != local_snapshot:
            raise ValueError("品牌资料与本机档案不一致，请重新选择品牌或更新本次资料。")
        result["brand_snapshot"] = local_snapshot
    elif not profile_id and supplied_snapshot:
        raise ValueError("请为品牌资料选择本机档案。")
    if not isinstance(result["allow_paid"], bool):
        raise ValueError("付费服务开关必须为布尔值。")
    if not isinstance(result["allow_reference_reuse"], bool):
        raise ValueError("人物动作复用开关必须为布尔值。")
    for field, lower, upper in (("speed", 0.8, 1.2), ("bgm_volume", 0, 0.4), ("cover_frame_time", 0, 1800)):
        value = result[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"{field} 必须在 {lower}～{upper} 之间。")
    for field in ("audio_path", "source_video_path", "bgm_path", "image_path"):
        if not isinstance(result[field], str):
            raise ValueError(f"{field} 必须是文件路径。")
        result[field] = str(Path(result[field]).expanduser().resolve()) if result[field] else ""
    if not isinstance(result["materials"], list) or len(result["materials"]) > 100:
        raise ValueError("素材必须是列表，最多支持 100 个文件。")
    for item in result["materials"]:
        if not isinstance(item, (str, dict)):
            raise ValueError("素材必须是文件路径或带路径的素材信息。")
        path = item if isinstance(item, str) else item.get("path", item.get("media_path", item.get("file_path", "")))
        if not isinstance(path, str) or not path:
            raise ValueError("素材缺少文件路径。")
    if not isinstance(result["hashtags"], list) or len(result["hashtags"]) > 20 or any(not isinstance(tag, str) or len(tag) > 100 for tag in result["hashtags"]):
        raise ValueError("话题标签格式无效。")
    return result


def _mutate(ident, operation):
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT data FROM records WHERE kind=? AND id=?", (_KIND, ident)).fetchone()
        if row is None:
            raise KeyError(ident)
        project = json.loads(row[0])
        operation(project)
        project["updated_at"] = _now()
        conn.execute("UPDATE records SET data=?,updated=? WHERE kind=? AND id=?",
                     (json.dumps(project, ensure_ascii=False, default=str), project["updated_at"], _KIND, ident))
    return project


def _lock_file(handle, unlock=False):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _owner_lock():
    root = store.data_root()
    key = str(root)
    with _SUBMIT_LOCK:
        if key not in _OWNER_HANDLES:
            folder = root / "workflow_locks"
            folder.mkdir(exist_ok=True)
            handle = (folder / f"{_OWNER}.lock").open("a+b")
            handle.write(b"1")
            handle.flush()
            _lock_file(handle)
            _OWNER_HANDLES[key] = handle
    return _OWNER


def _owner_alive(owner):
    if not owner or not re.fullmatch(r"[0-9a-f]{32}", owner):
        return False
    if owner == _OWNER and str(store.data_root()) in _OWNER_HANDLES:
        return True
    path = store.data_root() / "workflow_locks" / f"{owner}.lock"
    if not path.is_file():
        return False
    try:
        with path.open("r+b") as handle:
            try:
                _lock_file(handle)
            except OSError:
                return True
            _lock_file(handle, unlock=True)
    except OSError:
        # If a lock cannot be checked, recovery must not steal the live work.
        return True
    return False


def _event(project, message):
    project.setdefault("history", []).append({"at": _now(), "message": message})
    project["history"] = project["history"][-50:]


def create_project(config: dict) -> dict:
    config = _normalize(config)
    ident = store.new_id()
    return store.save_record(_KIND, ident, {
        "title": config.get("title") or config["input_text"].splitlines()[0][:60],
        "config": config, "state": "draft", "current_stage": "script", "result": {},
        "stages": {stage: _empty_stage() for stage in STAGES}, "cancel_requested": False,
        "job_id": "", "owner": "", "claim_token": "", "history": [], "progress": 0, "remote_pending": None,
    })


def list_projects() -> list[dict]:
    recover_projects()
    return store.list_records(_KIND)


def get_project(ident) -> dict:
    recover_projects()
    result = store.get_record(_KIND, ident)
    if result is None:
        raise KeyError(ident)
    return result


def _preserve_voice(project):
    result = project["stages"]["voice"].get("result", {})
    if result.get("audio_fingerprint") and _exists(result.get("audio_path")):
        project["voice_checkpoint"] = {key: copy.deepcopy(value) for key, value in result.items() if key != "avatar_video_path"}


def _invalidate(project, first):
    if STAGES.index(first) <= STAGES.index("voice"):
        _preserve_voice(project)
    for stage in STAGES[STAGES.index(first):]:
        previous = project["stages"][stage].get("result", {})
        project["stages"][stage] = _empty_stage()
        if stage == "voice" and first == "voice" and previous.get("audio_fingerprint") == _audio_fingerprint(project):
            project["stages"][stage]["result"] = {key: value for key, value in previous.items() if key != "avatar_video_path"}
    project.update(state="draft", current_stage=first, result=_collect_result(project), error="", progress=0)


def update_project(ident, changes: dict) -> dict:
    if not isinstance(changes, dict):
        raise ValueError("作品修改必须是对象。")
    changes = copy.deepcopy(changes)
    acknowledge = changes.pop("acknowledge_paid_retry", False)
    if not isinstance(acknowledge, bool):
        raise ValueError("付费重试确认必须为布尔值。")
    def update(project):
        if project["state"] in {"running", "queued"}:
            raise ValueError("作品正在生成，请等当前步骤结束或取消后再修改。")
        supplied = changes.get("config", changes)
        if not isinstance(supplied, dict):
            raise ValueError("作品设置必须是对象。")
        if "config" in changes and set(changes) - {"config", "title"}:
            raise ValueError("作品修改格式无效。")
        merged = dict(project["config"], **supplied)
        if "brand_profile_id" in supplied and supplied["brand_profile_id"] != project["config"].get("brand_profile_id", "") and "brand_snapshot" not in supplied:
            merged["brand_snapshot"] = {}
        updated = _normalize(merged, previous=project["config"])
        altered = {key for key in updated if updated[key] != project["config"].get(key, _DEFAULTS.get(key))}
        project["config"] = updated
        if "title" in changes or "title" in supplied:
            project["title"] = str(changes.get("title", updated.get("title", ""))).strip()[:120]
        first = next((stage for stage in STAGES if altered & _FIELDS[stage]), None)
        if updated["input_mode"] == "topic" and altered & _BRIEF_FIELDS:
            first = "script"
        if first and not acknowledge and (project.get("remote_pending") or any(saved.get("remote_request", {}).get("state") == "sending" for saved in project["stages"].values())):
            raise ValueError("存在结果未知的远端请求，请先核查并明确授权，不能通过修改参数自动重发。")
        if first:
            _invalidate(project, first)
            _event(project, f"设置已修改，从 {first} 重新生成；较早步骤已保留。")
        if acknowledge:
            project["remote_pending"] = None
            for stage in STAGES:
                saved = project["stages"][stage]
                if saved.get("remote_request", {}).get("state") == "sending":
                    saved["remote_request"]["state"] = "retry_authorized"
                    saved.update(state="pending", error="")
                    project.update(state="paused", error="", current_stage=stage)
                    _event(project, f"用户确认远端结果后，明确授权 {stage} 再次请求，可能重新计费。")
    return _mutate(ident, update)


def _file_stamp(path):
    if not path:
        return None
    source = Path(path).expanduser().resolve()
    try:
        stat = source.stat()
        return [str(source), stat.st_size, stat.st_mtime_ns]
    except OSError:
        return [str(source), "missing"]


def _fingerprint(project, stage):
    config = project["config"]
    values = {key: config.get(key) for key in sorted(_FIELDS[stage])}
    for key, default in processing.DEFAULTS.items():
        if key in values and (key not in config or values[key] == default):
            values.pop(key)
    # Empty defaults are omitted so opening an existing work never invalidates
    # a completed paid request. Pasted text does not depend on a writing brief.
    if stage == "script" and config["input_mode"] == "topic" and any(config.get(key) for key in _BRIEF_FIELDS):
        values["content_brief"] = {key: config.get(key, _DEFAULTS[key]) for key in sorted(_BRIEF_FIELDS)}
    if stage == "render":
        values["renderer_revision"] = "hyperframes-overlay-v1"
    for key in ("audio_path", "source_video_path", "bgm_path", "image_path"):
        if key in values:
            values[key] = _file_stamp(values[key])
    if "materials" in values:
        values["materials"] = [{"metadata": item, "file": _file_stamp(item if isinstance(item, str) else item.get("path", item.get("media_path", item.get("file_path"))))} for item in values["materials"]]
    if "pip_items" in values:
        values["pip_items"] = [{"metadata": item, "file": _file_stamp(item["path"])} for item in (values["pip_items"] or [])]
    if "green_background_path" in values:
        values["green_background_path"] = _file_stamp(values["green_background_path"])
    previous = STAGES[:STAGES.index(stage)]
    values["upstream"] = [{"fingerprint": project["stages"][name].get("fingerprint"), "result": project["stages"][name].get("result", {})} for name in previous]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _exists(path):
    try:
        return bool(path) and Path(path).is_file() and Path(path).stat().st_size > 0
    except OSError:
        return False


def _valid_result(stage, result):
    required = {
        "script": ("txt_path",), "voice": ("audio_path", "srt_path"),
        "visuals": ("video_path", "plan_path"), "render": ("video_path",),
        "release": ("video_path", "cover_path"),
    }[stage]
    return all(_exists(result.get(key)) for key in required) and (
        stage != "voice" or not result.get("avatar_video_path") or _exists(result["avatar_video_path"])
    ) and (stage != "render" or (bool(result.get("quality", {}).get("pass")) and (not result.get("srt_path") or _exists(result["srt_path"]))))


def _collect_result(project):
    result = {}
    for stage in STAGES:
        saved = project["stages"][stage]
        if saved.get("state") == "done":
            for key in ("text", "audio_path", "srt_path", "video_path", "plan_path", "cover_path", "title", "duration", "quality", "release_assets_id"):
                if key in saved.get("result", {}):
                    result[key] = saved["result"][key]
    return result


def recover_projects() -> None:
    for candidate in store.list_records(_KIND):
        if candidate.get("state") not in {"queued", "running"} or _owner_alive(candidate.get("owner")):
            continue
        def recover(project):
            if project.get("state") not in {"queued", "running"} or _owner_alive(project.get("owner")):
                return
            stage = project.get("current_stage", "script")
            saved = project["stages"][stage]
            unknown = bool(project.get("remote_pending")) or saved.get("remote_request", {}).get("state") == "sending"
            state = "needs_user" if unknown else "interrupted"
            if saved["state"] == "running":
                saved.update(state=state, error="远端请求结果未确认，请核查后再决定是否重试。" if unknown else "程序中断，已保存结果可继续使用。")
            project.update(state=state, owner="", claim_token="", job_id="", error=saved.get("error", ""))
            _event(project, "已恢复作品记录；" + ("远端结果需核查，暂不重发。" if unknown else "可从未完成步骤继续。"))
        _mutate(candidate["id"], recover)


def cancel_project(ident) -> dict:
    def cancel(project):
        if project["state"] in {"queued", "running"}:
            project["cancel_requested"] = True
            _event(project, "已申请取消，将在当前步骤结束后停止。")
        elif project["state"] != "done":
            project.update(state="cancelled", cancel_requested=True)
    return _mutate(ident, cancel)


def _check_until(until_stage):
    if until_stage is not None and until_stage not in STAGES:
        raise ValueError("停止步骤无效。")
    return until_stage or "release"


def _claim(ident, token, queued=False):
    owner = _owner_lock()
    def claim(project):
        if project["state"] in {"running", "queued"}:
            raise ValueError("这份作品已在生成，不能重复启动。")
        if project.get("remote_pending"):
            raise ValueError("上一次远端请求结果未知，请先核查，不能直接重新计费生成。")
        for saved in project["stages"].values():
            if saved.get("remote_request", {}).get("state") == "sending":
                raise ValueError("上一次远端请求结果未知，请先核查，不能直接重新计费生成。")
        project.update(state="queued" if queued else "running", status="", owner=owner, claim_token=token,
                       job_id="", cancel_requested=False, error="", request_snapshot=copy.deepcopy(project["config"]))
    return _mutate(ident, claim)


def submit_project(ident, *, until_stage=None) -> str:
    until_stage = _check_until(until_stage)
    with _SUBMIT_LOCK:
        recover_projects()
        current = get_project(ident)
        if current["state"] in {"queued", "running"}:
            if current.get("job_id"):
                return current["job_id"]
            raise ValueError("作品任务正在启动，请稍后查看。")
        token = store.new_id()
        project = _claim(ident, token, queued=True)
        try:
            job_id = jobs.submit("自动制作 · " + project["title"], _run_claimed, ident, until_stage, token)
        except Exception:
            def undo(saved):
                if saved.get("claim_token") == token and saved["state"] == "queued":
                    saved.update(state="paused", owner="", claim_token="")
            _mutate(ident, undo)
            raise
        _mutate(ident, lambda saved: saved.update(job_id=job_id))
        return job_id


def _run_claimed(ident, until_stage, token, progress=None):
    try:
        return run_project(ident, until_stage=until_stage, progress=progress, _claim_token=token)
    except Exception:
        project = get_project(ident)
        if project["state"] == "needs_user":
            return dict(project, status="needs_user", message=project["error"])
        raise


def _checkpoint(ident, stage, result=None, **changes):
    def save(project):
        saved = project["stages"][stage]
        if result is not None:
            saved.setdefault("result", {}).update(result)
        saved.update(changes)
    return _mutate(ident, save)


def _remote(ident, stage, provider, operation):
    marker = {"stage": stage, "provider": provider, "state": "sending", "started_at": _now()}
    def starting(project):
        project["remote_pending"] = marker
        project["stages"][stage]["remote_request"] = marker
    _mutate(ident, starting)
    result = operation()
    # Persist the provider result before moving on to local processing.
    def completed(project):
        project["remote_pending"] = None
        project["stages"][stage].setdefault("result", {}).update(result)
        project["stages"][stage]["remote_request"] = {"provider": provider, "state": "complete", "ended_at": _now()}
    _mutate(ident, completed)
    return result


def _voice_paid(config):
    ident = config["voice_id"]
    if ident.startswith(("edge:", "duix:")):
        return False
    if ident.startswith("saved:"):
        profile = store.get_record("voices", ident.split(":", 1)[1])
        if profile and profile.get("provider") == "duix":
            return False
    return True


def _audio_fingerprint(project):
    config = project["config"]
    values = {key: config[key] for key in ("voice_id", "speed", "audio_path", "source_video_path", "language", "model_size")}
    for key in ("audio_path", "source_video_path"):
        values[key] = _file_stamp(values[key])
    values["text"] = project["stages"]["script"]["result"]["text"]
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _content_brief(config):
    if not any(config.get(key) for key in _BRIEF_FIELDS):
        return None
    selected = content_templates.get_template(config["content_template"]) if config.get("content_template") else None
    template = {key: copy.deepcopy(selected[key]) for key in ("id", "name", "purpose_id", "structure", "material_clues") if key in selected} if selected else None
    return {"brand": copy.deepcopy(config.get("brand_snapshot", {})),
            "video_purpose": config.get("video_purpose", ""), "content_template": template,
            "campaign_content": config["input_text"], "target_duration": config.get("target_duration", 0)}


def _execute(ident, stage, project, progress):
    config = project["config"]
    saved = copy.deepcopy(project["stages"][stage].get("result", {}))
    folder = store.data_root() / "projects" / ident / stage / project["stages"][stage]["fingerprint"][:16]
    folder.mkdir(parents=True, exist_ok=True)
    if stage == "script":
        if config["input_mode"] == "topic":
            if saved.get("text"):
                text = saved["text"]
            else:
                if not config["allow_paid"]:
                    raise ValueError("主题写稿需要调用已配置的大模型；请明确开启付费/云服务，或直接粘贴现成文案。")
                content_brief = _content_brief(config)
                draft_options = {"content_brief": content_brief} if content_brief else {}
                draft = _remote(ident, stage, "configured_llm", lambda: topics.generate_draft(
                    config["input_text"][:300], style=config["writing_style"], account_id=config["account_id"] or None,
                    progress=progress, **draft_options))
                text = draft["text"]
        else:
            text = config["input_text"]
        path = folder / "script.txt"
        path.write_text(text, "utf-8")
        brief = spoken_library.structure_text(text, industry_id=competitors.get_settings().get("spoken_profile", "spoken_general"))
        return {"text": text, "txt_path": str(path), "video_brief": brief}
    if stage == "voice":
        script = project["stages"]["script"]["result"]["text"]
        audio_fingerprint = _audio_fingerprint(project)
        reusable = project.get("voice_checkpoint", {})
        if not _exists(saved.get("audio_path")) and reusable.get("audio_fingerprint") == audio_fingerprint:
            saved.update(copy.deepcopy(reusable))
        saved["audio_fingerprint"] = audio_fingerprint
        audio = saved.get("audio_path", "")
        if not _exists(audio):
            existing = config["audio_path"] or config["source_video_path"]
            if existing:
                info = rendering.probe_source(existing)
                if not info["has_audio"]:
                    raise ValueError("导入的视频或配音没有有效音轨，请先添加配音。")
                result = {"audio_path": info["path"], "duration": info["duration"], "provider": "imported"}
            elif _voice_paid(config):
                if not config["allow_paid"]:
                    raise ValueError("所选声音可能产生云端费用，请开启付费/云服务或选择 Edge 标准音色。")
                result = _remote(ident, stage, config["voice_id"], lambda: narration.generate(script, config["voice_id"], speed=config["speed"], progress=progress))
            else:
                result = narration.generate(script, config["voice_id"], speed=config["speed"], progress=progress)
            saved.update(result)
            audio = saved["audio_path"]
            _checkpoint(ident, stage, result=saved)
        if not _exists(saved.get("srt_path")):
            transcription = extract.extract_media(audio, language=config["language"], model_size=config["model_size"], progress=progress)
            saved.update(srt_path=transcription["srt_path"], segments=transcription["segments"])
            _checkpoint(ident, stage, result=saved)
        if config["kind"] == "avatar" and config.get("avatar_mode", "mixed") == "full" and not config["source_video_path"]:
            if config["avatar_id"] and not _exists(saved.get("avatar_video_path")):
                result = avatar.generate(audio, config["avatar_id"], script=script, aspect=config["aspect"], progress=progress,
                                         allow_reference_reuse=config.get("allow_reference_reuse", False))
                saved["avatar_video_path"] = result["video_path"]
                _checkpoint(ident, stage, result=saved)
        return saved
    if stage == "visuals":
        from . import composition, planning
        voice = project["stages"]["voice"]["result"]
        script = project["stages"]["script"]["result"]
        # Older completed works retain their voice checkpoints. Deriving their
        # local brief here does not repeat a model, TTS or ASR request.
        brief = script.get("video_brief") or spoken_library.structure_text(script["text"])
        if config["kind"] == "avatar" and config.get("avatar_mode", "mixed") == "mixed":
            from . import avatar_mixed, avatar_mixed_generation
            if config["source_video_path"]:
                source = rendering.probe_source(config["source_video_path"])
                if not source["has_video"] or source["duration"] < voice["duration"] - 0.15:
                    raise ValueError("已有口播视频短于完整配音，请选择人物重新生成，不能循环画面补齐。")
                prepared = avatar_mixed.prepare(project["stages"]["script"]["result"]["text"], voice["segments"],
                                                voice["audio_path"], source["duration"], folder,
                                                aspect=config["aspect"], progress=progress,
                                                materials=config["materials"], video_brief=brief)
                plan = avatar_mixed.attach_avatar(prepared, source["path"])
                for shot in plan["segments"]:
                    if shot.get("avatar_cameo"):
                        shot["media_start"] = shot["visual_start"]
                plan["shots"] = plan["segments"]
                result = composition.compose(plan, prepared["full_audio_path"], folder, progress=progress)
                path = folder / "plan.json"
                path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")
                return dict(result, plan=plan, plan_path=str(path), mixed=True)
            if not _exists(saved.get("video_path")):
                saved = avatar_mixed_generation.generate(
                    voice["audio_path"], config["avatar_id"], script=project["stages"]["script"]["result"]["text"],
                    aspect=config["aspect"], segments=voice["segments"], output_dir=folder, progress=progress,
                    materials=config["materials"], video_brief=brief)
                _checkpoint(ident, stage, result=saved)
            return saved
        plan = planning.build_plan(project["stages"]["script"]["result"]["text"], voice["segments"],
                                   config["materials"], kind=config["kind"], aspect=config["aspect"],
                                   duration=voice["duration"], progress=progress, video_brief=brief)
        path = folder / "plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")
        base = voice.get("avatar_video_path") or config["source_video_path"] or None
        result = composition.compose(plan, voice["audio_path"], folder, base_video_path=base, progress=progress)
        return dict(result, plan_path=str(path), plan=plan)
    if stage == "render":
        voice = project["stages"]["voice"]["result"]
        srt = voice["srt_path"] if config["subtitle_style"] != "none" else None
        if not _exists(saved.get("video_path")):
            saved = rendering.render_video(
                project["stages"]["visuals"]["result"]["video_path"], audio_path=voice["audio_path"],
                subtitle_path=srt, title="", template=config["template"], style=config["style"],
                subtitle_style=config["subtitle_style"], bgm_path=config["bgm_path"] or None,
                bgm_volume=config["bgm_volume"], color_grade=config["color_grade"], aspect=config["aspect"],
                video_fit=config["video_fit"], image_path=config["image_path"] or None, progress=progress,
                pip_items=config.get("pip_items", []), processing_options={key: config.get(key, value) for key, value in processing.DEFAULTS.items() if key in processing.RENDER_FIELDS})
            _checkpoint(ident, stage, result=saved)
        report = quality.inspect_video(saved["video_path"], subtitle_path=saved.get("srt_path") or srt,
                                       expected_duration=saved.get("duration", voice["duration"]), progress=progress)
        saved["quality"] = report
        _checkpoint(ident, stage, result=saved)
        if not report["pass"]:
            raise RuntimeError("成片检查未通过：" + "；".join(report.get("errors", [])))
        return saved
    render = project["stages"]["render"]["result"]
    if not render.get("quality", {}).get("pass"):
        raise ValueError("成片尚未通过检查，不能生成发布包。")
    text = project["stages"]["script"]["result"]["text"]
    title = config["release_title"] or re.split(r"[。！？!?\n]", text)[0].strip()[:50] or project["title"][:50]
    if not _exists(saved.get("cover_path")):
        frame_time = config.get("cover_frame_fraction", 0) * render.get("duration", 0) or config["cover_frame_time"]
        saved.update(release_assets.generate_cover(render["video_path"], config.get("cover_title") or title, style=config["cover_style"],
                                                   aspect=config.get("cover_aspect") or config["aspect"], frame_time=frame_time, progress=progress))
        _checkpoint(ident, stage, result=saved)
    materials = release_assets.save_materials(render["video_path"], title, description=config["description"],
                                              hashtags=config["hashtags"], cover_path=saved["cover_path"], source_text=text)
    return dict(materials, release_assets_id=materials["id"], srt_path=render.get("srt_path", ""),
                message="成片与发布资料已保存；尚未对外发布。")


def run_project(ident, *, until_stage=None, progress=None, _claim_token=None) -> dict:
    until_stage = _check_until(until_stage)
    recover_projects()
    token = _claim_token or store.new_id()
    if _claim_token:
        def start(project):
            if project.get("claim_token") != token or project.get("owner") != _OWNER or project["state"] != "queued":
                raise ValueError("作品任务所有权已变化，不能重复运行。")
            project["state"] = "running"
        _mutate(ident, start)
    else:
        _claim(ident, token)
    try:
        project = get_project(ident)
        if STAGES.index(until_stage) >= STAGES.index("visuals"):
            config = project["config"]
            missing = []
            if config["kind"] in {"product", "montage"} and not config["materials"]:
                missing.append("请先上传商品图片、视频或混剪素材。")
            if config["kind"] == "avatar" and not config["avatar_id"] and not config["source_video_path"]:
                missing.append("请先选择数字人形象，或导入已有口播视频。")
            for item in config["materials"]:
                path = item if isinstance(item, str) else item.get("path", item.get("media_path", item.get("file_path")))
                if not _exists(path):
                    missing.append("部分素材文件已缺失，请重新上传。")
                    break
            if config["template"] == "pip" and not _exists(config["image_path"]):
                missing.append("画中画模板需要有效背景图片。")
            if missing:
                message = " ".join(missing)
                return _mutate(ident, lambda row: row.update(state="needs_user", status="needs_user", error=message,
                                                            message=message, owner="", claim_token=""))
        for index, stage in enumerate(STAGES[:STAGES.index(until_stage) + 1]):
            project = get_project(ident)
            if project["cancel_requested"]:
                return _mutate(ident, lambda row: row.update(state="cancelled", owner="", claim_token="", result=_collect_result(row)))
            fingerprint = _fingerprint(project, stage)
            saved = project["stages"][stage]
            if saved.get("state") == "done" and saved.get("fingerprint") == fingerprint and _valid_result(stage, saved.get("result", {})):
                continue
            if saved.get("remote_request", {}).get("state") == "sending":
                raise ValueError("远端请求结果未知，请核查后明确授权重试。")
            def begin(row):
                current = row["stages"][stage]
                if index <= STAGES.index("voice"):
                    _preserve_voice(row)
                if current.get("fingerprint") != fingerprint:
                    old_result = current.get("result", {})
                    current = _empty_stage()
                    if stage == "voice" and old_result.get("audio_fingerprint") == _audio_fingerprint(row):
                        # A new avatar/aspect can reuse the same paid narration
                        # and transcript without another synthesis request.
                        current["result"] = {key: value for key, value in old_result.items() if key != "avatar_video_path"}
                    row["stages"][stage] = current
                # A rerun of this stage makes every later result obsolete.
                for later in STAGES[index + 1:]:
                    row["stages"][later] = _empty_stage()
                current.update(state="running", fingerprint=fingerprint, error="", started_at=_now())
                row.update(current_stage=stage, result=_collect_result(row))
            project = _mutate(ident, begin)
            def stage_progress(message, percent=None):
                value = index * 20 + max(0, min(100, float(percent or 0))) / 5
                _mutate(ident, lambda row: row.update(message=str(message)[:600], progress=value))
                if progress:
                    progress(str(message), value)
            result = _execute(ident, stage, project, stage_progress)
            if not _valid_result(stage, result):
                raise RuntimeError("步骤没有返回完整可用的文件，请检查结果后重试。")
            def finish(row):
                row["stages"][stage].update(state="done", result=result, error="", ended_at=_now())
                row.update(result=_collect_result(row), progress=(index + 1) * 20)
            _mutate(ident, finish)
        return _mutate(ident, lambda row: row.update(state="cancelled" if row["cancel_requested"] else ("done" if until_stage == "release" else "paused"),
                                                    owner="", claim_token="", result=_collect_result(row), error=""))
    except BaseException as exc:
        error = str(exc)[:1200]
        def fail(row):
            stage = row["current_stage"]
            saved = row["stages"][stage]
            unknown = bool(row.get("remote_pending")) or saved.get("remote_request", {}).get("state") == "sending"
            state = "needs_user" if unknown else "failed"
            message = ("远端请求结果未确认，请核查服务端记录；直接重试可能再次计费。 " if unknown else "") + error
            saved.update(state=state, error=message, ended_at=_now())
            row.update(state=state, error=message, owner="", claim_token="", result=_collect_result(row))
            _event(row, message)
        _mutate(ident, fail)
        raise
