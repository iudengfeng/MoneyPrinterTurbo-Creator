from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st

from app.services.creator import jobs, store


CATEGORIES = {
    "script": "① 选题与文案", "audio": "② 配音与字幕", "visuals": "③ 素材与画面",
    "render": "④ 字幕包装与成片", "publish": "⑤ 封面与发布",
}
CATEGORY_PAGES = {
    "script": ("创作中心", "选题和文案", "口播参考库", "账号选题", "作品库", "客户／品牌档案"),
    "audio": ("配音", "文案提取", "声音与数字人"),
    "visuals": ("数字人", "素材与分镜"),
    "render": ("模板剪辑",),
    "publish": ("标题和封面", "发布中心"),
}
PAGE_LABELS = {
    "创作中心": "一键创作", "选题和文案": "选题与文案", "口播参考库": "口播参考库", "账号选题": "账号定位与选题",
    "作品库": "作品库", "客户／品牌档案": "客户／品牌档案", "配音": "配音", "文案提取": "字幕与文案提取",
    "声音与数字人": "声音管理", "数字人": "我的数字人", "素材与分镜": "素材与分镜", "模板剪辑": "字幕包装与成片",
    "标题和封面": "封面与标题", "发布中心": "发布",
}
PAGE_CATEGORY = {page: category for category, pages in CATEGORY_PAGES.items() for page in pages}


def _stage(upload):
    data = upload.getvalue()
    suffix = Path(upload.name).suffix.lower()
    folder = store.data_root() / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(data).hexdigest() + suffix)
    if not path.exists():
        path.write_bytes(data)
    return str(path)


def _submit(label, operation, *args, **kwargs):
    try:
        ident = jobs.submit(label, operation, *args, **kwargs)
        st.session_state["creator_last_job"] = ident
        st.success("已加入任务中心，可以继续准备下一条内容。")
    except Exception as exc:
        st.error(str(exc))


def _use_script(text):
    st.session_state["video_script"] = text
    st.session_state["creator_duix_script"] = text
    st.session_state["creator_route"] = "视频制作"
    st.session_state["creator_navigation_pending"] = True


def _use_video(result):
    clean = result.get("clean_video_path")
    st.session_state["creator_video_path"] = clean or result["video_path"]
    st.session_state["creator_publish_video"] = result["video_path"]
    st.session_state["creator_srt_path"] = result.get("srt_path", "") if clean or not result.get("subtitles_burned") else ""
    st.session_state["creator_video_has_subtitles"] = bool(result.get("subtitles_burned") and not clean)
    st.session_state["creator_render_audio"] = ""
    st.session_state["creator_render_source"] = "本次视频"
    st.session_state["creator_render_replace_audio"] = False
    for key in ("creator_render_material_fingerprint", "creator_render_audio_import", "creator_render_subtitle_import"):
        st.session_state.pop(key, None)
    for key in ("creator_video_path_upload", "creator_srt_path_upload", "creator_render_subtitle_upload", "creator_render_audio_upload"):
        st.session_state.pop(key, None)
    if result.get("aspect"):
        st.session_state["creator_render_aspect"] = result["aspect"]
    st.session_state["creator_selected_tab"] = "模板剪辑"
    st.session_state["creator_navigation_pending"] = True


def _use_audio(path):
    st.session_state["creator_render_audio"] = path
    st.session_state["creator_render_replace_audio"] = True
    st.session_state.pop("creator_render_audio_import", None)
    st.session_state.pop("creator_render_subtitle_import", None)
    st.session_state.pop("creator_render_subtitle_upload", None)
    st.session_state.pop("creator_render_audio_upload", None)
    st.session_state["creator_render_subtitle_mode"] = "自动识别生成"
    st.session_state["creator_srt_path"] = ""
    st.session_state["creator_video_has_subtitles"] = False
    st.session_state["creator_selected_tab"] = "模板剪辑"
    st.session_state["creator_navigation_pending"] = True


def _extract():
    from app.services.creator import extract
    st.write("导入参考视频，提取可编辑的口播文案和带时间轴的字幕。")
    source = st.radio("导入方式", ["上传视频或音频", "分享链接"], horizontal=True)
    uploaded = None
    link = ""
    if source == "上传视频或音频":
        uploaded = st.file_uploader("选择参考视频或音频", type=["mp4","mov","mkv","webm","mp3","wav","m4a","aac"], key="creator_extract_upload")
    else:
        link = st.text_area("粘贴视频分享链接", help="解析能力取决于平台和链接是否有效。无法解析时可以上传本地视频。")
    with st.expander("识别设置"):
        model = st.selectbox("识别模型", ["small","base","medium","large-v3"], format_func=lambda x:{"small":"标准（推荐）","base":"快速","medium":"更精细","large-v3":"最高精度，耗时较长"}[x])
        language = st.selectbox("口播语言", ["zh","en",""], format_func=lambda x:{"zh":"中文","en":"英文","":"自动识别"}[x])
        st.caption("识别使用 CPU；首次使用所选模型需要下载，之后可离线使用。")
    if st.button("提取文案", type="primary", key="creator_extract_start"):
        if uploaded:
            _submit("提取口播文案",extract.extract_media,_stage(uploaded),language=language,model_size=model)
        elif link.strip():
            def from_link(progress=None):
                media=extract.download_media(link,progress=progress)
                return extract.extract_media(media,language=language,model_size=model,progress=progress)
            _submit("链接提取文案",from_link)
        else:
            st.warning("请先选择文件或输入分享链接。")
    for row in store.list_records("extracts")[:10]:
        with st.expander("已提取文案 · "+Path(row.get("media_path", "参考视频")).name):
            text=st.text_area("编辑文案",value=row.get("text",""),height=200,key="creator_text_"+row["id"])
            left,right=st.columns(2)
            if left.button("保存修改",key="creator_save_"+row["id"]):
                store.update_record("extracts",row["id"],{"text":text})
                if row.get("txt_path"):
                    Path(row["txt_path"]).write_text(text,"utf-8")
                st.success("文案已保存。")
            right.button("送到速片工厂",key="creator_use_"+row["id"],on_click=_use_script,args=(text,))
            st.download_button("下载文案",text,file_name="口播文案.txt",key="creator_txt_"+row["id"])
            if row.get("srt_path") and Path(row["srt_path"]).is_file():
                st.download_button("下载原视频字幕",Path(row["srt_path"]).read_bytes(),file_name="参考字幕.srt",key="creator_srt_"+row["id"])


def _topics():
    from app.services.creator import topics
    st.write("根据账号定位和参考文案生成选题，保存后可以继续写口播稿。")
    with st.expander("新增账号定位",expanded=not topics.list_accounts()):
        with st.form("creator_account_form"):
            name=st.text_input("账号名称")
            industry=st.text_input("行业或领域",placeholder="例如：家居装修、职场经验")
            audience=st.text_input("目标受众",placeholder="内容主要帮助谁")
            positioning=st.text_area("账号定位",placeholder="你擅长什么，内容要解决什么问题")
            references=st.text_area("参考文案或作品笔记",placeholder="可粘贴上一步提取的文案")
            if st.form_submit_button("保存账号定位"):
                try:
                    topics.save_account(name,industry,audience,positioning,references)
                    st.success("账号定位已保存。")
                except Exception as exc:
                    st.error(str(exc))
    accounts=topics.list_accounts()
    if not accounts:
        return
    account=st.selectbox("选择账号",accounts,format_func=lambda a:a["name"],key="creator_topic_account")
    count=st.slider("生成选题数量",1,20,8)
    if st.button("生成选题",type="primary"):
        def generate(progress=None):
            if progress:
                progress("根据账号定位生成选题",10)
            return topics.generate_topics(account["id"],count=count)
        _submit("账号选题 · "+account["name"],generate)
    st.caption("选题使用原设置中的文案模型和对应服务额度。首版根据你的资料生成，未接入实时热榜。")
    for topic in topics.list_topics(account["id"])[:30]:
        with st.expander(topic["title"]):
            st.write("开头："+topic.get("hook",""))
            st.caption(topic.get("reason",""))
            if st.button("生成口播稿",key="creator_write_"+topic["id"]):
                def write(progress=None,ident=topic["id"]):
                    if progress:
                        progress("撰写口播文案",10)
                    return {"text":topics.write_script(ident)}
                _submit("口播稿 · "+topic["title"],write)
            if topic.get("script"):
                script=st.text_area("口播稿",value=topic["script"],height=180,key="creator_script_"+topic["id"])
                if st.button("保存稿件",key="creator_script_save_"+topic["id"]):
                    store.update_record("topics",topic["id"],{"script":script})
                    st.success("稿件已保存。")
                st.button("送到速片工厂",key="creator_topic_use_"+topic["id"],on_click=_use_script,args=(script,))


def _voices():
    from app.services.creator import voices,duix
    left,right=st.columns(2)
    with left:
        st.write("保存声音档案")
        provider=st.radio("声音来源",["本机 Duix 已有音色","上传样音克隆"],key="creator_voice_source")
        name=st.text_input("声音名称",key="creator_voice_name")
        sample=None
        selected=None
        transcript=""
        if provider=="本机 Duix 已有音色":
            try:
                available=duix.list_profiles()["voices"]
                if available:
                    selected=st.selectbox("已有音色",available,format_func=lambda a:a["name"])
                else:
                    st.info("Duix 中还没有音色，请先在原软件中创建。")
            except Exception as exc:
                st.warning(str(exc))
        else:
            sample=st.file_uploader("上传清晰样音",type=["wav","mp3","m4a","aac"],key="creator_voice_sample")
            transcript=st.text_area("样音对应文字",help="准确文字有助于克隆声音的发音和节奏。")
            st.caption("使用已有 VoxCPM 云端克隆接口，需要在原基础设置中配置对应服务。")
        if st.button("保存声音",key="creator_voice_save",type="primary"):
            try:
                if provider=="本机 Duix 已有音色":
                    if not selected:
                        raise ValueError("请先选择音色。")
                    voices.save_voice(name,"",provider="duix",duix_voice_id=selected["id"])
                else:
                    if not sample:
                        raise ValueError("请先上传样音。")
                    voices.save_voice(name,_stage(sample),transcript=transcript,provider="voxcpm")
                st.success("声音档案已保存。")
            except Exception as exc:
                st.error(str(exc))
    with right:
        saved=voices.list_voices()
        if saved:
            selected_voice=st.selectbox("我的声音",saved,format_func=lambda a:a["name"])
            text=st.text_area("试听或生成配音的文字",value="你好，这是我的声音试听。",key="creator_voice_text")
            if st.button("生成配音并试听",key="creator_voice_preview"):
                def preview(progress=None):
                    return {"audio_path":voices.preview_voice(selected_voice["id"],text,progress=progress)}
                _submit("声音试听 · "+selected_voice["name"],preview)
        else:
            st.info("保存第一个声音档案后，就可以生成配音。")


def _rendering():
    from webui.creator_render_workspace import render as render_editor
    render_editor()


def _publishing():
    from webui.creator_publish_workspace import render as render_publish
    render_publish()


@st.fragment(run_every="3s")
def _tasks():
    if st.session_state.pop("creator_navigation_pending", False):
        st.rerun(scope="app")
    st.subheader("任务中心")
    rows=jobs.list_jobs()[:15]
    if not rows:
        st.caption("提交任务后，这里会显示进度和结果。")
    labels={"queued":"排队中","running":"处理中","done":"已完成","failed":"失败","interrupted":"已中断","needs_user":"等待处理"}
    for row in rows:
        with st.expander(row["label"]+" · "+labels.get(row["state"],row["state"]),expanded=row["id"]==st.session_state.get("creator_last_job")):
            st.write(row.get("message",""))
            if row["state"] in {"queued","running"}:
                st.progress(float(row.get("progress",0))/100)
            result=row.get("result")
            if isinstance(result,dict):
                if result.get("text"):
                    st.text_area("生成文案",value=result["text"],key="creator_jobtext_"+row["id"],height=160)
                    st.button("送到速片工厂",key="creator_job_use_"+row["id"],on_click=_use_script,args=(result["text"],))
                if result.get("audio_path") and Path(result["audio_path"]).is_file():
                    st.audio(result["audio_path"])
                    if result.get("preview"):
                        st.caption("这是短试听。制作视频请先生成完整配音。")
                    else:
                        st.button("用于模板配音",key="creator_job_audio_use_"+row["id"],on_click=_use_audio,args=(result["audio_path"],))
                    st.download_button("下载试听" if result.get("preview") else "下载配音",Path(result["audio_path"]).read_bytes(),file_name=("试听" if result.get("preview") else "配音")+Path(result["audio_path"]).suffix,key="creator_job_audio_dl_"+row["id"])
                if result.get("video_path") and Path(result["video_path"]).is_file():
                    st.video(result["video_path"])
                    st.button("用于剪辑或发布",key="creator_job_video_"+row["id"],on_click=_use_video,args=(result,))
                    st.download_button("下载成片",Path(result["video_path"]).read_bytes(),file_name="成片.mp4",key="creator_job_download_"+row["id"])
                if result.get("srt_path") and Path(result["srt_path"]).is_file():
                    st.download_button("下载字幕",Path(result["srt_path"]).read_bytes(),file_name="字幕.srt",key="creator_job_srt_"+row["id"])
            elif isinstance(result,list):
                st.success("已保存 "+str(len(result))+" 条结果，请刷新对应页面查看。")
    if st.button("刷新工作台",key="creator_refresh"):
        st.rerun(scope="app")


def _select_workspace(tab):
    st.session_state["creator_selected_tab"] = tab
    st.session_state["creator_navigation_target"] = tab
    st.session_state["creator_navigation_pending"] = True


def _category_changed():
    category = st.session_state["creator_category"]
    _select_workspace(CATEGORY_PAGES[category][0])


def _subpage_changed():
    _select_workspace(st.session_state["creator_selected_tab"])


def _sync_navigation():
    """Resolve old handoff routes before creating either navigation widget."""
    pending = st.session_state.pop("creator_navigation_target", None)
    selected = pending if pending is not None else st.session_state.get("creator_selected_tab", "创作中心")
    category = st.session_state.get("creator_category")
    if category not in CATEGORIES:
        category = next((key for key, label in CATEGORIES.items() if label == category), None)
    previous_category = st.session_state.get("creator_last_rendered_category") or PAGE_CATEGORY.get(
        st.session_state.get("creator_last_rendered_page"))
    if pending is None and category in CATEGORIES and previous_category and category != previous_category:
        # Automatic task fragments can consume a changed widget state before
        # its full-page callback is processed. A changed category remains a
        # navigation request even when there is no callback handoff marker.
        selected = CATEGORY_PAGES[category][0]
    project_query = str(st.query_params.get("project", ""))
    if pending is None and project_query and project_query != st.session_state.get("creator_project_query"):
        # An external deep link opens the saved work. Internal Studio picking
        # and saving already carry their own pending marker and retain the
        # current tool (including the material stage in category three).
        if st.session_state.get("studio_pending_project") != project_query:
            selected = "创作中心"
            st.session_state["studio_pending_project"] = project_query
    st.session_state["creator_project_query"] = project_query
    if selected == "竞品素材库":
        selected = "口播参考库"
    if selected not in PAGE_CATEGORY:
        # A late fragment may carry the previous category's formatted local
        # option. Keep the current category instead of resetting the sidebar.
        visible_page = next((page for page, label in PAGE_LABELS.items() if label == selected), None)
        if visible_page and (category is None or PAGE_CATEGORY[visible_page] == category):
            selected = visible_page
        else:
            previous_page = st.session_state.get("creator_last_rendered_page")
            selected = previous_page if previous_page in PAGE_CATEGORY and PAGE_CATEGORY[previous_page] == category else CATEGORY_PAGES.get(
                category, CATEGORY_PAGES["script"])[0]
    active_category = PAGE_CATEGORY[selected]
    if st.session_state.get("creator_selected_tab") != selected:
        st.session_state["creator_selected_tab"] = selected
    if st.session_state.get("creator_category") != active_category:
        st.session_state["creator_category"] = active_category
    if selected == "素材与分镜" and st.session_state.get("creator_last_rendered_page") != selected:
        st.session_state["studio_mode"] = "分步制作"
        st.session_state["studio_step"] = "visuals"
    st.session_state["creator_last_rendered_page"] = selected
    st.session_state["creator_last_rendered_category"] = active_category
    return selected


def render():
    from app.services.creator import competitors
    competitors.ensure_scheduler()
    selected = _sync_navigation()
    st.markdown('<style>'+Path(__file__).with_name("creator_styles.css").read_text("utf-8")+'</style>',unsafe_allow_html=True)
    st.markdown('<div class="creator-shell"><strong>数字人口播</strong><span> / 从选题到发布，按五步完成作品</span></div>',unsafe_allow_html=True)
    with st.sidebar.container(key="creator_navigation"):
        st.markdown('<div class="creator-navigation-label">制作流程</div>', unsafe_allow_html=True)
        category = st.radio("制作流程", list(CATEGORIES), format_func=CATEGORIES.get, key="creator_category",
                            on_change=_category_changed, label_visibility="collapsed", persist_state="session")
    pages = CATEGORY_PAGES[category]
    if len(pages) > 1:
        with st.container(key="creator_local_navigation"):
            selected = st.radio("本步工具", list(pages), format_func=PAGE_LABELS.get, key="creator_selected_tab", horizontal=True,
                                on_change=_subpage_changed, label_visibility="collapsed", persist_state="session")
    if selected=="创作中心":
        from webui.creator_studio_workspace import render as render_studio
        render_studio()
    elif selected=="作品库":
        from webui.creator_studio_workspace import render_library
        render_library()
    elif selected=="客户／品牌档案":
        from webui.creator_brand_workspace import render as render_brands
        render_brands()
    elif selected=="选题和文案":
        from webui.creator_script_workspace import render as render_scripts
        render_scripts()
    elif selected=="口播参考库":
        from webui.creator_competitor_workspace import render as render_competitors
        render_competitors()
    elif selected=="配音":
        from webui.creator_voice_workspace import render as render_voice
        render_voice()
    elif selected=="数字人":
        from webui.creator_avatar_workspace import render as render_avatar
        render_avatar()
    elif selected=="素材与分镜":
        from webui.creator_studio_workspace import render as render_studio
        render_studio()
    elif selected=="模板剪辑":
        _rendering()
    elif selected=="标题和封面":
        from webui.creator_release_workspace import render as render_release
        render_release()
    elif selected=="发布中心":
        _publishing()
    else:
        st.subheader(PAGE_LABELS.get(selected, selected))
        {"文案提取":_extract,"账号选题":_topics,"声音与数字人":_voices,"模板剪辑":_rendering,"发布中心":_publishing}[selected]()
    st.divider()
    with st.expander("任务中心",expanded=selected not in {"创作中心","作品库","客户／品牌档案","选题和文案","口播参考库","配音","数字人","模板剪辑","标题和封面","发布中心"}):
        _tasks()
