"""A five-step customer workspace backed by persistent creator projects.

The UI only saves configuration and requests workflow stages. It never invokes
publication or keeps generation results exclusively in Streamlit session state.
"""
from __future__ import annotations

import hashlib
from html import escape
from importlib import import_module
from pathlib import Path

import streamlit as st

from app.services.creator import brand_profiles, content_templates, store


STEPS = {
    "script": "① 选题与文案",
    "voice": "② 配音与字幕",
    "visuals": "③ 素材与画面",
    "render": "④ 字幕包装与成片",
    "release": "⑤ 封面与发布",
}
STEP_NAMES = {"script": "文案", "voice": "配音", "visuals": "画面", "render": "成片", "release": "发布"}
KINDS = {"knowledge": "知识讲解", "montage": "素材混剪", "product": "商品介绍", "avatar": "数字人口播"}
PURPOSE_LABELS = {
    "product_service": "介绍产品或服务", "promotion": "推广活动", "knowledge": "分享专业知识",
    "case_feedback": "展示案例或客户反馈", "personal_brand": "打造个人形象", "quick_edit": "用已有素材快速成片",
}
STATES = {
    "draft": "尚未开始", "pending": "待生成", "queued": "排队中", "running": "生成中",
    "paused": "本步已完成", "done": "已完成", "failed": "需要重试", "interrupted": "已中断",
    "needs_user": "等待补资料", "cancelled": "已停止", "cancel_requested": "正在停止",
}
BUSY = {"queued", "running", "cancel_requested"}
DEFAULT_VOICE = "edge:zh-CN-XiaoxiaoNeural"
DEFAULTS = {
    "input_mode": "script", "input_text": "", "kind": "knowledge", "voice_id": DEFAULT_VOICE,
    "speed": 1.0, "avatar_id": "", "materials": [], "audio_path": "", "source_video_path": "",
    "avatar_mode": "mixed", "allow_reference_reuse": False,
    "aspect": "9:16", "template": "talking", "style": "knowledge", "subtitle_style": "clean",
    "bgm_path": "", "image_path": "", "allow_paid": False,
    "brand_profile_id": "", "brand_snapshot": {}, "video_purpose": "product_service",
    "content_template": "product_intro", "target_duration": 60,
}


def _workflow():
    return import_module("app.services.creator.workflow")


def _stage_upload(upload, *, media=False):
    limit = (500 if media else 128) * 1024 * 1024
    content = upload.getvalue()
    if not content or len(content) > limit:
        raise ValueError(f"请选择不超过 {500 if media else 128} MB 的有效文件。")
    suffix = Path(upload.name).suffix.lower()
    allowed = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".png", ".jpg", ".jpeg", ".webp"} if media else {".wav", ".mp3", ".m4a", ".aac", ".ogg"}
    if suffix not in allowed:
        raise ValueError("此文件类型暂不支持，请选择列出的音频、图片或视频。")
    folder = store.data_root() / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(content).hexdigest() + suffix)
    if not path.is_file():
        path.write_bytes(content)
    return str(path)


def _voices():
    from app.services.creator import narration
    try:
        rows = narration.list_options()
        return rows or [{"id": DEFAULT_VOICE, "name": "晓晓 · 标准女声", "provider": "edge"}]
    except Exception:
        return [{"id": DEFAULT_VOICE, "name": "晓晓 · 标准女声", "provider": "edge"}]


def _people():
    from app.services.creator import avatar
    try:
        return avatar.list_options()
    except Exception:
        return []


def _navigate(tab):
    st.session_state["creator_selected_tab"] = tab
    st.session_state["creator_navigation_target"] = tab
    st.session_state["creator_navigation_pending"] = True


def _choose_project(ident):
    st.session_state["studio_pending_project"] = ident
    if ident:
        st.query_params["project"] = ident
    else:
        st.query_params.pop("project", None)


def _open_project(ident):
    _choose_project(ident)
    _navigate("创作中心")


def _picker_changed():
    _choose_project(st.session_state["studio_project_choice"])


def _kind_changed():
    kind = st.session_state.get("studio_kind", "knowledge")
    st.session_state["studio_style"] = "business" if kind == "product" else "knowledge" if kind == "knowledge" else "clean"
    st.session_state["studio_kind_manual"] = True


def _recommended_kind():
    purpose = st.session_state.get("studio_video_purpose") or st.session_state.get("studio_purpose_choice", "knowledge")
    return content_templates.recommend_kind(
        purpose, profile=st.session_state.get("studio_brand_snapshot", {}),
        has_materials=bool(st.session_state.get("studio_saved_materials")),
        has_avatar=bool(st.session_state.get("studio_avatar_id")),
    )


def _apply_recommended_kind():
    st.session_state["studio_kind"] = _recommended_kind()
    _kind_changed()
    st.session_state["studio_kind_manual"] = False


def _purpose_changed():
    purpose = st.session_state["studio_purpose_choice"]
    st.session_state["studio_video_purpose"] = purpose
    templates = content_templates.list_templates(purpose)
    st.session_state["studio_content_template"] = next(row["id"] for row in templates if row.get("recommended"))
    if not st.session_state.get("studio_target_duration"):
        st.session_state["studio_target_duration"] = 60
    if not st.session_state.get("studio_kind_manual"):
        _apply_recommended_kind()


def _brand_changed():
    ident = st.session_state.get("studio_brand_profile_id", "")
    st.session_state["studio_brand_snapshot"] = brand_profiles.snapshot(ident) if ident else {}
    if not st.session_state.get("studio_kind_manual"):
        _apply_recommended_kind()


def _prime(project):
    ident = project.get("id", "") if project else ""
    if st.session_state.get("studio_loaded_project") == ident:
        return
    config = {**DEFAULTS, **(project or {}).get("config", {})}
    if project:
        # Older works keep their original narration duration and production
        # route until the customer explicitly chooses a new goal or template.
        for field, value in (("video_purpose", ""), ("content_template", ""), ("target_duration", 0)):
            config[field] = project.get("config", {}).get(field, value)
    for key, value in config.items():
        if key in DEFAULTS:
            st.session_state["studio_" + key] = value
    for key in ("studio_audio_upload", "studio_video_upload", "studio_material_upload", "studio_background_upload"):
        st.session_state.pop(key, None)
    for key in list(st.session_state):
        if key.startswith("studio_material_tags_"):
            st.session_state.pop(key, None)
    st.session_state["studio_loaded_project"] = ident
    st.session_state["studio_saved_materials"] = list(config.get("materials") or [])
    inferred = {"knowledge": "knowledge", "product": "product_service", "montage": "quick_edit", "avatar": "personal_brand"}
    st.session_state["studio_purpose_choice"] = config.get("video_purpose") or inferred.get(config["kind"], "knowledge")
    st.session_state["studio_kind_manual"] = bool(project)


def _config():
    result = {key: st.session_state.get("studio_" + key, value) for key, value in DEFAULTS.items()}
    result["materials"] = list(st.session_state.get("studio_saved_materials", result["materials"]))
    return result


def _save(project):
    workflow = _workflow()
    config = _config()
    if not str(config.get("input_text", "")).strip():
        raise ValueError("请先输入文案或主题。")
    if project:
        saved = workflow.update_project(project["id"], {"config": config})
    else:
        saved = workflow.create_project(config)
    _choose_project(saved["id"])
    return saved


def _start(project, stage=None):
    try:
        saved = _save(project)
        job_id = _workflow().submit_project(saved["id"], until_stage=stage)
        st.session_state["creator_last_job"] = job_id
        st.session_state["studio_notice"] = "已开始生成，离开页面后作品会继续处理。"
        st.rerun()
    except Exception as exc:
        st.error(str(exc))


def _handoff(project):
    from webui.creator_publish_workspace import load_materials
    result = {**project.get("stages", {}).get("render", {}).get("result", {}),
              **project.get("stages", {}).get("release", {}).get("result", {}), **project.get("result", {})}
    result.setdefault("title", project.get("title", ""))
    load_materials(result)
    _navigate("发布中心")


def _input_fields(busy):
    st.radio("内容从哪里开始", ["script", "topic"], format_func=lambda value: "我已有文案" if value == "script" else "帮我写稿",
             key="studio_input_mode", horizontal=True, disabled=busy, persist_state="session")
    topic = st.session_state["studio_input_mode"] == "topic"
    purpose = st.session_state.get("studio_video_purpose") or st.session_state.get("studio_purpose_choice")
    label = "本次想讲什么" if topic else "口播文案"
    placeholder = "例如：这次想介绍的商品特点、实际活动安排，或客户经常问的一个问题。" if topic else "粘贴要讲的内容，或先选「帮我写稿」。"
    if purpose == "promotion" and topic:
        label, placeholder = "本次活动与想讲的内容", "填写真实活动内容、适用条件和参与方式；不确定的价格或期限可以先不写。"
    elif purpose == "quick_edit":
        placeholder = "说明素材里有什么、希望怎么串起来；当前需要一段简短说明或口播稿。"
    st.text_area(label, key="studio_input_text", height=180, placeholder=placeholder,
                 disabled=busy, persist_state="session")
    if topic:
        st.caption("根据你的主题写稿；近期热度与平台播放数据尚未接入。")
    if purpose == "quick_edit":
        st.caption("上传已有图片或视频，并填写简短说明。当前流程会制作配音和字幕，也可导入已有配音。")


def _goal_picker(project, busy):
    purposes = {row["id"]: row for row in content_templates.list_purposes()}
    with st.container(key="studio_goal_selector"):
        st.radio("你想做什么视频？", list(purposes), format_func=PURPOSE_LABELS.get, horizontal=True,
                 key="studio_purpose_choice", on_change=_purpose_changed, disabled=busy, persist_state="session")
        st.caption(purposes[st.session_state["studio_purpose_choice"]]["description"])
        if project and not st.session_state.get("studio_video_purpose"):
            st.caption("这是已有作品，仍沿用原来的制作设置；切换目的后才会使用新的内容模板。")


def _customer_context(busy):
    from webui.creator_brand_workspace import edit_profile, resume_editor

    profiles = {row["id"]: row for row in brand_profiles.list_profiles()}
    current = st.session_state.get("studio_brand_profile_id", "")
    snapshot = st.session_state.get("studio_brand_snapshot", {})
    options = ["", *profiles]
    if current and current not in profiles:
        options.append(current)

    def label(ident):
        if not ident:
            return "先不选择，直接创作"
        if ident == current and isinstance(snapshot, dict) and snapshot.get("name"):
            return str(snapshot["name"])
        return str(profiles.get(ident, {}).get("name", "已保存的客户资料"))

    with st.container(key="studio_brand_context"):
        opened = False
        selection, create, edit = st.columns([2.4, 1, 1], vertical_alignment="bottom")
        with selection:
            st.selectbox("本次为谁创作", options, format_func=label, key="studio_brand_profile_id", on_change=_brand_changed,
                         disabled=busy, persist_state="session")
        with create:
            if st.button("新建档案", key="studio_new_brand", disabled=busy, use_container_width=True):
                edit_profile(attach_to_studio=True)
                opened = True
        with edit:
            if st.button("编辑档案", key="studio_edit_brand", disabled=busy or current not in profiles, use_container_width=True):
                edit_profile(current, attach_to_studio=True)
                opened = True
        if snapshot:
            with st.expander("查看本次使用的客户资料"):
                for field, name in (("offering", "提供内容"), ("audience", "客户或观众"),
                                    ("differentiators", "特点与优势"), ("desired_action", "期望行动")):
                    if snapshot.get(field):
                        st.write(name + "：" + str(snapshot[field]))
                if current in profiles and profiles[current].get("version") != snapshot.get("version"):
                    st.caption("档案有更新。本作品仍保留当时的资料；需要同步时，请更新本次资料。")
                    if st.button("更新本次资料", key="studio_refresh_brand", disabled=busy):
                        st.session_state["studio_brand_snapshot"] = brand_profiles.snapshot(current)
                        st.rerun()
        else:
            st.caption("客户资料可以保存一次、以后复用；也可以直接在下方填写本次内容。")
        if not opened:
            resume_editor()


def _content_settings(busy):
    purpose = st.session_state.get("studio_video_purpose") or st.session_state.get("studio_purpose_choice", "knowledge")
    templates = {row["id"]: row for row in content_templates.list_templates(purpose)}
    current = st.session_state.get("studio_content_template", "")
    options = list(templates)
    if current not in options:
        options.insert(0, current)
    with st.container(key="studio_template_section"):
        template, duration = st.columns([1.45, 1])
        with template:
            st.selectbox("内容模板", options,
                         format_func=lambda value: (templates[value]["name"] + ("（推荐）" if templates[value].get("recommended") else ""))
                         if value in templates else "沿用原作品设置" if not value else "已保存的模板",
                         key="studio_content_template", disabled=busy, persist_state="session")
        with duration:
            times = [30, 60, 90, 120]
            current_time = st.session_state.get("studio_target_duration", 60)
            if current_time not in times:
                times.insert(0, current_time)
            st.selectbox("目标时长", times, format_func=lambda value: "按文案原时长" if value == 0 else f"约 {value} 秒",
                         key="studio_target_duration", disabled=busy, persist_state="session")
        st.caption("目标时长用于帮你写稿时控制篇幅；已有文案仍按实际配音时长生成。")
        ideas = content_templates.suggest_materials(purpose, st.session_state.get("studio_brand_snapshot", {}))
        st.caption("建议准备：" + "、".join(ideas))
        chosen = st.session_state.get("studio_content_template", "")
        if chosen in templates:
            row = templates[chosen]
            with st.expander("看看模板结构与示例"):
                st.markdown("**内容结构**")
                for index, point in enumerate(row.get("structure", []), 1):
                    st.caption(f"{index}. {point}")
                example = row.get("example", {})
                st.markdown("**示例，需替换为你确认的真实资料**")
                st.caption(str(example.get("context", "")))
                st.write(str(example.get("script", "")))


def _production_settings(busy):
    with st.expander("制作方式与更多设置"):
        st.selectbox("视频类型", list(KINDS), format_func=lambda value: KINDS[value], key="studio_kind", on_change=_kind_changed,
                     disabled=busy, persist_state="session")
        recommended = _recommended_kind()
        st.caption("根据当前素材推荐：" + KINDS[recommended] + "。你也可以手动选择其他方式。")
        if recommended != st.session_state.get("studio_kind"):
            st.button("采用推荐方式", key="studio_use_recommended_kind", on_click=_apply_recommended_kind, disabled=busy)


def _voice_fields(busy):
    voices = _voices()
    options = {row["id"]: row for row in voices}
    selected = st.session_state.get("studio_voice_id", DEFAULT_VOICE)
    if selected not in options:
        options[selected] = {"id": selected, "name": "已保存声音（请确认可用）"}
    st.selectbox("选择声音", list(options), format_func=lambda value: options[value]["name"], key="studio_voice_id", disabled=busy, persist_state="session")
    st.caption("标准音色需要联网；自己的音色可以在配音页保存后重复使用。")
    if st.session_state.get("studio_kind") == "avatar":
        people = {str(row["id"]): row for row in _people()}
        current = str(st.session_state.get("studio_avatar_id", ""))
        if current and current not in people:
            people[current] = {"id": current, "name": "已保存人物（请确认可用）"}
        st.session_state["studio_avatar_id"] = current
        st.selectbox("选择我的人物", ["", *people], format_func=lambda value: people[value]["name"] if value else "先不选择人物",
                     key="studio_avatar_id", disabled=busy, persist_state="session")
        st.caption("使用已保存的视频形象，或在下方导入已有口播视频；人物页可上传形象视频。")
        st.radio("人物画面", ["mixed", "full"],
                 format_func=lambda value: "人物＋图文（推荐）" if value == "mixed" else "全程人物",
                 key="studio_avatar_mode", horizontal=True, disabled=busy, persist_state="session")
        if st.session_state.get("studio_avatar_mode") == "mixed":
            st.caption("在开头、中间和结尾安排人物，其余时间匹配上传素材或使用文案信息卡，动作素材不重复。")
        else:
            with st.expander("人物动作素材不足时"):
                st.checkbox("允许在整段人物模板用完后复用动作", key="studio_allow_reference_reuse", disabled=busy, persist_state="session")
                st.caption("默认不循环。全程人物需要足够长的参考视频；只改变口型不会创造新的身体动作。")
        st.button("管理我的人物", key="studio_manage_people", on_click=_navigate, args=("数字人",), disabled=busy)
    with st.expander("语速与已有音视频"):
        st.slider("语速", 0.8, 1.2, step=0.1, key="studio_speed", disabled=busy, persist_state="session")
        audio = st.file_uploader("已有配音（可选）", type=["wav", "mp3", "m4a", "aac", "ogg"], key="studio_audio_upload", disabled=busy)
        video = st.file_uploader("已有口播视频（可选）", type=["mp4", "mov", "mkv", "webm"], key="studio_video_upload", disabled=busy)
        for upload, field in ((audio, "audio_path"), (video, "source_video_path")):
            if upload:
                try:
                    st.session_state["studio_" + field] = _stage_upload(upload, media=field == "source_video_path")
                except ValueError as exc:
                    st.error(str(exc))
        for field, label in (("audio_path", "配音"), ("source_video_path", "口播视频")):
            if st.session_state.get("studio_" + field):
                st.caption(f"已保存{label}：{Path(st.session_state['studio_' + field]).name[:40]}")
                if st.button("取消使用已有" + label, key="studio_clear_" + field, disabled=busy):
                    st.session_state["studio_" + field] = ""
                    st.session_state.pop("studio_audio_upload" if field == "audio_path" else "studio_video_upload", None)
                    st.rerun()


def _visual_fields(busy):
    from app.services.creator import rendering
    kind = st.session_state.get("studio_kind", "knowledge")
    if kind == "knowledge":
        st.caption("不上传素材也能生成文字讲解画面；图片和视频可以让内容更丰富。")
    elif kind == "avatar" and st.session_state.get("studio_avatar_mode", "mixed") == "mixed":
        st.caption("可上传商品、场景或过程素材，穿插在人物镜头之间；没有相关素材时使用口播信息卡。")
    elif kind in {"montage", "product"}:
        st.info("请上传要使用的图片或视频，软件会根据分镜安排画面。商品资料请写入文案。")
    uploads = st.file_uploader("图片与视频素材（可多选）", type=["png", "jpg", "jpeg", "webp", "mp4", "mov", "mkv", "webm"],
                               accept_multiple_files=True, key="studio_material_upload", disabled=busy)
    saved = list(st.session_state.get("studio_saved_materials", []))
    previous_count = len(saved)
    for upload in uploads or []:
        try:
            path = _stage_upload(upload, media=True)
            if path not in [row.get("path") if isinstance(row, dict) else row for row in saved]:
                saved.append({"path": path, "name": Path(upload.name).name, "tags": []})
        except ValueError as exc:
            st.error(str(exc))
    st.session_state["studio_saved_materials"] = saved
    if len(saved) != previous_count and st.session_state.get("studio_video_purpose") and not st.session_state.get("studio_kind_manual"):
        _apply_recommended_kind()
    if saved:
        st.caption(f"已保存 {len(saved)} 份素材，下次打开作品可继续使用。")
        with st.expander("素材名称与标签"):
            st.caption("画面匹配参考文件名和标签。可补充商品名、场景等关键词，帮助选择相关画面。")
            for index, material in enumerate(saved):
                row = material if isinstance(material, dict) else {"path": material, "name": Path(material).name, "tags": []}
                st.caption(str(row.get("name") or Path(row["path"]).name))
                tags = row.get("tags", [])
                initial = "、".join(tags) if isinstance(tags, list) else str(tags)
                tag_key = "studio_material_tags_" + hashlib.sha256(str(row["path"]).encode()).hexdigest()[:16]
                if tag_key not in st.session_state:
                    st.session_state[tag_key] = initial
                text = st.text_input("素材标签", key=tag_key, disabled=busy, placeholder="例如：厨房、收纳、商品名称")
                row["tags"] = [value.strip() for value in text.replace("，", ",").replace("、", ",").split(",") if value.strip()]
                saved[index] = row
            st.session_state["studio_saved_materials"] = saved
        if st.button("清空本作品素材", key="studio_clear_materials", disabled=busy):
            st.session_state["studio_saved_materials"] = []
            st.session_state.pop("studio_material_upload", None)
            st.rerun()
    presets = {row["id"]: row for row in rendering.list_presets()}
    if st.session_state.get("studio_style") not in presets:
        st.session_state["studio_style"] = next(iter(presets))
    st.selectbox("画面风格", list(presets), format_func=lambda value: presets[value]["name"], key="studio_style", disabled=busy, persist_state="session")
    with st.expander("画幅、字幕与音乐"):
        st.selectbox("画幅", ["9:16", "16:9", "1:1"], format_func=lambda value: {"9:16": "竖屏", "16:9": "横屏", "1:1": "方形"}[value],
                     key="studio_aspect", disabled=busy, persist_state="session")
        st.selectbox("字幕样式", ["clean", "bold", "yellow", "none"], format_func=lambda value: {"clean": "清晰简洁", "bold": "醒目大字", "yellow": "黄色强调", "none": "不加字幕"}[value],
                     key="studio_subtitle_style", disabled=busy, persist_state="session")
        st.selectbox("画面布局", ["talking", "pip", "cards"], format_func=lambda value: {"talking": "完整画面", "pip": "画中画", "cards": "卡片布局"}[value],
                     key="studio_template", disabled=busy, persist_state="session")
        if st.session_state.get("studio_template") == "pip":
            background = st.file_uploader("画中画背景图片", type=["png", "jpg", "jpeg", "webp"], key="studio_background_upload", disabled=busy)
            if background:
                try:
                    st.session_state["studio_image_path"] = _stage_upload(background, media=True)
                except ValueError as exc:
                    st.error(str(exc))
            if st.session_state.get("studio_image_path"):
                st.caption("已保存画中画背景。")
            else:
                st.warning("画中画布局需要一张背景图片；也可以选择完整画面或卡片布局。")
        music = {row["path"]: row["name"] for row in rendering.list_bgm()}
        current = st.session_state.get("studio_bgm_path", "")
        if current and current not in music:
            music[current] = "已保存音乐"
        st.selectbox("背景音乐", ["", *music], format_func=lambda value: music[value] if value else "不加背景音乐", key="studio_bgm_path", disabled=busy, persist_state="session")


def _preview_data(project):
    stages = project.get("stages", {})
    script = stages.get("script", {}).get("result", {})
    voice = stages.get("voice", {}).get("result", {})
    visual = stages.get("visuals", {}).get("result", {})
    render = stages.get("render", {}).get("result", {})
    release = stages.get("release", {}).get("result", {})
    ready = stages.get("render", {}).get("state") == "done" and bool(render.get("quality", {}).get("pass"))
    final = {**render, **release, **project.get("result", {})} if ready else {}
    if ready:
        final["video_path"] = render.get("video_path", "")
        final["quality"] = render["quality"]
    base_video = visual.get("video_path")
    has_base_video = bool(base_video and Path(base_video).is_file())
    views = []
    if script.get("text") or script.get("script"):
        views.append("文案")
    if voice.get("audio_path") and Path(voice["audio_path"]).is_file():
        views.append("配音")
    if visual.get("shots") or visual.get("plan") or has_base_video:
        views.append("分镜")
    if final.get("video_path") and Path(final["video_path"]).is_file():
        views.append("成片")
    return script, voice, visual, final, base_video if has_base_video else "", views


def _empty_preview(message="先输入主题或文案，再点击左侧的生成按钮。"):
    st.markdown(
        '<div class="studio-empty-preview">'
        '<span class="studio-empty-label">等待生成</span>'
        '<strong>你的作品会显示在这里</strong>'
        f'<p>{escape(message)}</p>'
        '<small>可先查看文案与配音，再继续生成成片。</small>'
        '</div>', unsafe_allow_html=True,
    )


def _step_strip(stages=None, current_stage="", project_state="draft"):
    stages = stages or {}
    items = []
    for index, (key, name) in enumerate(STEP_NAMES.items(), 1):
        state = stages.get(key, {}).get("state", "pending")
        if state == "done":
            style = "done"
        elif state in {"failed", "needs_user", "interrupted"} or (
                key == current_stage and project_state in {"failed", "needs_user", "interrupted"}):
            style = "failed"
        elif state in BUSY or key == current_stage and project_state in BUSY:
            style = "active"
        else:
            style = "pending"
        items.append(
            f'<li class="studio-step studio-step--{style}">'
            f'<span class="studio-step-number">{index}</span>'
            f'<span class="studio-step-name">{escape(name)}</span>'
            f'<small>{escape(STATES.get(state, "待生成"))}</small></li>'
        )
    st.markdown('<ol class="studio-stepper" aria-label="制作进度">' + "".join(items) + '</ol>', unsafe_allow_html=True)


def _results(project):
    stages = project.get("stages", {})
    script, voice, visual, final, base_video, views = _preview_data(project)
    st.subheader("作品预览")
    if not views:
        _empty_preview("生成结果会显示在这里；作品和阶段进度自动保存。")
        return
    key = "studio_preview_" + project["id"]
    if st.session_state.get(key) not in views:
        st.session_state[key] = views[-1]
    view = st.radio("查看结果", views, horizontal=True, key=key, label_visibility="collapsed")
    if view == "文案":
        text = script.get("text", script.get("script", ""))
        script_key = "studio_script_edit_" + project["id"] + "_" + hashlib.sha256(str(text).encode()).hexdigest()[:12]
        edited = st.text_area("编辑口播稿", value=str(text), key=script_key, height=180, disabled=project.get("state") in BUSY)
        with st.container(key="studio_script_actions"):
            apply, download = st.columns([1.25, 1])
            with apply:
                if st.button("采用修改后的文案", key="studio_apply_script", disabled=project.get("state") in BUSY,
                             use_container_width=True):
                    try:
                        _workflow().update_project(project["id"], {"config": {"input_mode": "script", "input_text": edited}})
                        st.session_state.pop("studio_loaded_project", None)
                        st.session_state["studio_notice"] = "口播稿已保存；继续生成时会重做受文案影响的步骤。"
                        st.rerun(scope="app")
                    except Exception as exc:
                        st.error(str(exc))
            with download:
                st.download_button("下载文案", str(text), file_name="口播文案.txt", key="studio_download_script", use_container_width=True)
        brief = script.get("video_brief")
        if isinstance(brief, dict):
            with st.expander("查看开头、信息点与画面线索"):
                st.caption("从当前口播稿提取，供后续分镜使用；开头句不代表已经精确测量为三秒。")
                if brief.get("hook_3s"):
                    st.write("开头：" + str(brief["hook_3s"]))
                for point in brief.get("bullet_points", []):
                    st.write("• " + str(point))
                for clue in brief.get("material_clues", []):
                    if isinstance(clue, dict) and clue.get("description"):
                        st.caption(str(clue.get("type", "画面")) + "：" + str(clue["description"]))
    elif view == "配音":
        st.audio(voice["audio_path"])
    elif view == "分镜":
        if base_video:
            st.caption("基础画面，尚未生成最终视频。")
            with st.container(key="studio_video_frame"):
                st.video(base_video)
        plan = visual.get("plan", visual)
        shots = plan.get("shots", []) if isinstance(plan, dict) else plan
        if shots:
            with st.expander("分镜与素材明细", expanded=not bool(base_video)):
                st.caption(f"共 {len(shots)} 段画面 · 根据实际配音时间安排")
                with st.container(height=360, border=False, key="studio_storyboard_scroll"):
                    for index, shot in enumerate(shots, 1):
                        if isinstance(shot, dict):
                            with st.container(key=f"studio_shot_{index}"):
                                start, end = shot.get("visual_start", shot.get("start", 0)), shot.get("visual_end", shot.get("end", 0))
                                st.write(f"**画面 {index} · {float(start):.1f}–{float(end):.1f} 秒** · {shot.get('text', shot.get('narration', shot.get('caption', '')))}")
                                st.caption(str(shot.get("match_reason", shot.get("description", shot.get("purpose", "")))))
                                if shot.get("media_type") == "card" and shot.get("card_text"):
                                    st.caption("信息卡：" + str(shot["card_text"]))
                                media = shot.get("media_path")
                                if media and Path(media).is_file():
                                    if shot.get("media_type") == "image":
                                        st.image(media, width=200)
                                    elif shot.get("media_type") == "video":
                                        with st.expander("查看这段素材", expanded=False):
                                            st.video(media)
        if isinstance(plan, dict):
            for message in plan.get("warnings", []):
                st.caption(str(message))
    else:
        with st.container(key="studio_video_frame"):
            st.video(final["video_path"])
        with st.container(key="studio_preview_actions"):
            download, publish = st.columns([1, 1.2])
            with download:
                st.download_button("下载成片", Path(final["video_path"]).read_bytes(), file_name="我的作品.mp4", key="studio_download_video",
                                   use_container_width=True)
            with publish:
                if stages.get("release", {}).get("state") == "done":
                    if st.button("带到发布中心", key="studio_publish_handoff", on_click=_handoff, args=(project,), type="primary",
                                 use_container_width=True):
                        # A fragment must rerun the app after navigation changes.
                        st.rerun(scope="app")
        if stages.get("release", {}).get("state") == "done":
            st.caption("成片已准备好。进入发布中心后选择账号、预览并确认；这里不会自动发布。")
        report = final.get("quality", {})
        if report:
            with st.expander("成片检查"):
                if report.get("pass"):
                    st.success("音视频文件与时间轴检查通过。")
                for check in report.get("checks", []):
                    if check.get("state") in {"failed", "warning"}:
                        st.warning(check.get("message", "请检查预览。"))
                st.caption("自动检查覆盖文件、声音和时间轴；内容表达与画面效果请结合预览确认。")
        cover = final.get("cover_path")
        if (cover and Path(cover).is_file()) or final.get("title"):
            with st.expander("封面与发布资料"):
                if cover and Path(cover).is_file():
                    st.image(cover, caption="发布封面", width=200)
                if final.get("title"):
                    st.write("标题：" + str(final["title"]))
    errors = []
    for row in stages.values():
        error = row.get("error")
        if error:
            errors.append(str(error))
    for error in dict.fromkeys(errors):
        st.warning(error)


def _refresh_after_completion(project):
    displayed = st.session_state.get("studio_page_state")
    latest = (project["id"], project.get("state"))
    if displayed and displayed[0] == latest[0] and displayed[1] in BUSY and latest[1] not in BUSY:
        # Update before the full rerun so a terminal state can never loop.
        st.session_state["studio_page_state"] = latest
        st.session_state["studio_preview_pending"] = project["id"]
        st.rerun(scope="app")


def _progress(ident):
    project = _workflow().get_project(ident)
    if not project:
        st.warning("这份作品已不存在，请在作品库重新选择。")
        return
    _refresh_after_completion(project)
    stages = project.get("stages", {})
    with st.container(border=True, key="studio_status_card"):
        st.subheader("生成进度")
        completed = sum(stages.get(key, {}).get("state") == "done" for key in STEPS)
        st.progress(completed / len(STEPS), text=f"{STATES.get(project.get('state'), '已保存')} · 已完成 {completed}/{len(STEPS)} 步")
        _step_strip(stages, project.get("current_stage", ""), project.get("state", "draft"))
        if project.get("state") in {"failed", "needs_user", "interrupted"}:
            current = stages.get(project.get("current_stage"), {})
            message = current.get("error") or project.get("error")
            if message:
                st.warning(str(message))
            if project.get("state") == "interrupted":
                st.info("上次生成已中断。继续生成会复用有效的已完成步骤。")
            if project.get("remote_pending") or any(row.get("remote_request", {}).get("state") == "sending" for row in stages.values()):
                with st.expander("处理上次服务请求"):
                    st.caption("上次请求结果尚未确认，请先核查服务记录。重新请求可能再次使用额度。")
                    confirmed = st.checkbox("我已核查并同意重新请求", key="studio_retry_confirm_" + ident)
                    if st.button("允许重新请求", key="studio_retry_authorize_" + ident, disabled=not confirmed):
                        _workflow().update_project(ident, {"acknowledge_paid_retry": True})
                        st.rerun(scope="app")
        if project.get("state") in BUSY:
            if st.button("停止后续生成", key="studio_cancel"):
                _workflow().cancel_project(ident)
                st.rerun(scope="app")
        elif st.button("刷新作品状态", key="studio_refresh"):
            st.rerun(scope="app")
    with st.container(border=True, key="studio_preview_card"):
        _results(project)


@st.fragment(run_every="3s")
def _poll_progress(ident):
    _progress(ident)


def render_library():
    st.subheader("作品库")
    st.caption("作品、输入资料与生成进度保存在本机。可以继续生成或修改后重做受影响的步骤。")
    try:
        projects = _workflow().list_projects()
    except (ImportError, AttributeError):
        st.info("创作中心正在准备，请稍后刷新。")
        return
    if not projects:
        with st.container(border=True, key="studio_library_empty"):
            st.markdown("**还没有作品，先创建第一条视频。**")
            st.caption("输入主题或文案即可开始；保存后可以随时回来继续制作。")
            st.button("开始创作", on_click=_open_project, args=("",), key="studio_library_new", type="primary")
    for row in projects:
        with st.container(border=True, key="studio_library_card_" + row["id"]):
            details, action = st.columns([4, 1], vertical_alignment="center")
            with details:
                st.write("**" + str(row.get("title") or "未命名作品") + "**")
                st.caption(KINDS.get(row.get("config", {}).get("kind"), "视频") + " · " + STATES.get(row.get("state"), "已保存"))
            with action:
                label = "打开作品" if row.get("state") == "done" else "继续制作"
                st.button(label, on_click=_open_project, args=(row["id"],), key="studio_open_" + row["id"], use_container_width=True)


def render():
    with st.container(key="studio_header"):
        st.subheader("创作中心")
        st.caption("输入主题或文案，一键生成视频；也可以分步预览和修改。")
    try:
        workflow = _workflow()
        projects = workflow.list_projects()
    except (ImportError, AttributeError):
        st.info("创作中心正在准备。已有制作页面仍可使用，请稍后刷新。")
        return
    rows = {row["id"]: row for row in projects}
    if "studio_pending_project" in st.session_state:
        st.session_state["studio_project_choice"] = st.session_state.pop("studio_pending_project")
    elif "studio_project_choice" not in st.session_state:
        st.session_state["studio_project_choice"] = str(st.query_params.get("project", ""))
    if st.session_state["studio_project_choice"] not in rows:
        st.session_state["studio_project_choice"] = ""
    selected = st.session_state["studio_project_choice"]
    project = workflow.get_project(selected) if selected else None
    st.session_state["studio_page_state"] = (project["id"], project.get("state")) if project else None
    _prime(project)
    pending_brand = st.session_state.pop("studio_pending_brand", None)
    if pending_brand:
        st.session_state["studio_brand_profile_id"] = pending_brand["id"]
        st.session_state["studio_brand_snapshot"] = pending_brand["snapshot"]
        if not st.session_state.get("studio_kind_manual"):
            _apply_recommended_kind()
    busy = bool(project and project.get("state") in BUSY)
    if project:
        with st.expander("本次视频目的"):
            _goal_picker(project, busy)
    else:
        _goal_picker(None, busy)
    with st.container(border=True, key="studio_toolbar"):
        current, creation_mode = st.columns([1.15, 1], gap="large")
        with current:
            selected = st.selectbox("当前作品", ["", *rows], format_func=lambda value: rows[value].get("title") or "未命名作品" if value else "＋ 创建新作品",
                                    key="studio_project_choice", on_change=_picker_changed, persist_state="session")
        with creation_mode:
            mode = st.radio("制作方式", ["一键成片", "分步制作"], horizontal=True, key="studio_mode", persist_state="session")
            st.caption("设置一次，自动完成。" if mode == "一键成片" else "每一步都能查看结果，随时继续。")
    if project and st.session_state.get("studio_preview_pending") == project["id"]:
        st.session_state.pop("studio_preview_pending", None)
        views = _preview_data(project)[-1]
        if views:
            # The pending marker comes from a fragment. Apply it only in the
            # full app, before the preview radio is instantiated this run.
            st.session_state["studio_preview_" + project["id"]] = views[-1]
    notice = st.session_state.pop("studio_notice", "") or st.session_state.pop("creator_brand_notice", "")
    if notice:
        st.success(notice)
    with st.container(key="studio_workbench"):
        inputs, preview = st.columns([1.12, 1], gap="large")
        with inputs:
            with st.container(border=True, key="studio_inputs"):
                st.subheader("内容与设置")
                _customer_context(busy)
                _content_settings(busy)
                stage = "script"
                if mode == "分步制作":
                    stage = st.radio("制作步骤", list(STEPS), format_func=lambda value: STEPS[value][0] + " " + STEP_NAMES[value],
                                     horizontal=True, key="studio_step", persist_state="session")
                    st.markdown("**" + STEPS[stage] + "**")
                    if stage == "script":
                        with st.container(key="studio_content_section"):
                            _input_fields(busy)
                    elif stage == "voice":
                        with st.container(key="studio_voice_section"):
                            _voice_fields(busy)
                    elif stage == "visuals":
                        with st.container(key="studio_visual_section"):
                            _visual_fields(busy)
                    elif stage == "render":
                        st.write("按已保存的文案、声音和画面设置生成成片。需要调整时，返回对应步骤再生成。")
                    else:
                        st.write("准备标题、封面与发布资料，再带到发布中心选择账号并确认。核对提交结果后，本条作品流程结束。")
                else:
                    with st.container(key="studio_content_section"):
                        _input_fields(busy)
                    with st.container(key="studio_voice_section"):
                        _voice_fields(busy)
                    with st.expander("图片与视频素材", expanded=st.session_state.get("studio_kind") in {"montage", "product"}
                                     or st.session_state.get("studio_purpose_choice") == "quick_edit"):
                        with st.container(key="studio_visual_section"):
                            _visual_fields(busy)
                _production_settings(busy)
                selected_voice = next((row for row in _voices() if row["id"] == st.session_state.get("studio_voice_id")), {})
                if st.session_state.get("studio_input_mode") == "topic" or selected_voice.get("provider") == "voxcpm" or st.session_state.get("studio_allow_paid"):
                    st.checkbox("允许使用已配置的云服务和对应额度", key="studio_allow_paid", disabled=busy, persist_state="session")
                with st.container(key="studio_generate_actions"):
                    if st.button("一键生成成片" if mode == "一键成片" else "生成本步骤", key="studio_generate", type="primary", disabled=busy,
                                 use_container_width=True):
                        _start(project, None if mode == "一键成片" else stage)
                    with st.container(key="studio_secondary_actions"):
                        finish, save = st.columns(2)
                        with finish:
                            if mode == "分步制作" and st.button("剩下的自动完成", key="studio_finish", disabled=busy, use_container_width=True):
                                _start(project)
                        with save:
                            if st.button("保存资料", key="studio_save", disabled=busy, use_container_width=True):
                                try:
                                    _save(project)
                                    st.session_state["studio_notice"] = "资料已保存，尚未开始生成。"
                                    st.rerun()
                                except Exception as exc:
                                    st.error(str(exc))
                if busy:
                    st.caption("这份作品正在生成；完成或停止后可以修改资料。")
                elif project:
                    st.caption("继续生成会复用未受修改影响的已完成步骤。")
        with preview:
            with st.container(key="studio_preview_column"):
                if project:
                    if busy:
                        _poll_progress(project["id"])
                    else:
                        _progress(project["id"])
                else:
                    with st.container(border=True, key="studio_status_card"):
                        st.subheader("生成进度")
                        st.caption("尚未开始 · 保存或开始生成后，进度会自动保留。")
                        _step_strip()
                    with st.container(border=True, key="studio_preview_card"):
                        st.subheader("作品预览")
                        _empty_preview()
