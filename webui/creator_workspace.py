from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st

from app.services.creator import jobs, store


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
                if row.get("txt_path"):Path(row["txt_path"]).write_text(text,"utf-8")
                st.success("文案已保存。")
            right.button("送到视频制作",key="creator_use_"+row["id"],on_click=_use_script,args=(text,))
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
                except Exception as exc:st.error(str(exc))
    accounts=topics.list_accounts()
    if not accounts:
        return
    account=st.selectbox("选择账号",accounts,format_func=lambda a:a["name"],key="creator_topic_account")
    count=st.slider("生成选题数量",1,20,8)
    if st.button("生成选题",type="primary"):
        def generate(progress=None):
            if progress:progress("根据账号定位生成选题",10)
            return topics.generate_topics(account["id"],count=count)
        _submit("账号选题 · "+account["name"],generate)
    st.caption("选题使用原设置中的文案模型和对应服务额度。首版根据你的资料生成，未接入实时热榜。")
    for topic in topics.list_topics(account["id"])[:30]:
        with st.expander(topic["title"]):
            st.write("开头："+topic.get("hook",""))
            st.caption(topic.get("reason",""))
            if st.button("生成口播稿",key="creator_write_"+topic["id"]):
                def write(progress=None,ident=topic["id"]):
                    if progress:progress("撰写口播文案",10)
                    return {"text":topics.write_script(ident)}
                _submit("口播稿 · "+topic["title"],write)
            if topic.get("script"):
                script=st.text_area("口播稿",value=topic["script"],height=180,key="creator_script_"+topic["id"])
                if st.button("保存稿件",key="creator_script_save_"+topic["id"]):
                    store.update_record("topics",topic["id"],{"script":script})
                    st.success("稿件已保存。")
                st.button("送到视频制作",key="creator_topic_use_"+topic["id"],on_click=_use_script,args=(script,))


def _voices():
    from app.services.creator import voices,duix
    left,right=st.columns(2)
    with left:
        st.write("保存声音档案")
        provider=st.radio("声音来源",["本机 Duix 已有音色","上传样音克隆"],key="creator_voice_source")
        name=st.text_input("声音名称",key="creator_voice_name")
        sample=None; selected=None;transcript=""
        if provider=="本机 Duix 已有音色":
            try:
                available=duix.list_profiles()["voices"]
                if available:selected=st.selectbox("已有音色",available,format_func=lambda a:a["name"])
                else:st.info("Duix 中还没有音色，请先在原软件中创建。")
            except Exception as exc:st.warning(str(exc))
        else:
            sample=st.file_uploader("上传清晰样音",type=["wav","mp3","m4a","aac"],key="creator_voice_sample")
            transcript=st.text_area("样音对应文字",help="准确文字有助于克隆声音的发音和节奏。")
            st.caption("使用已有 VoxCPM 云端克隆接口，需要在原基础设置中配置对应服务。")
        if st.button("保存声音",key="creator_voice_save",type="primary"):
            try:
                if provider=="本机 Duix 已有音色":
                    if not selected:raise ValueError("请先选择音色。")
                    voices.save_voice(name,"",provider="duix",duix_voice_id=selected["id"])
                else:
                    if not sample:raise ValueError("请先上传样音。")
                    voices.save_voice(name,_stage(sample),transcript=transcript,provider="voxcpm")
                st.success("声音档案已保存。")
            except Exception as exc:st.error(str(exc))
    with right:
        saved=voices.list_voices()
        if saved:
            selected_voice=st.selectbox("我的声音",saved,format_func=lambda a:a["name"])
            text=st.text_area("试听或生成配音的文字",value="你好，这是我的声音试听。",key="creator_voice_text")
            if st.button("生成配音并试听",key="creator_voice_preview"):
                def preview(progress=None):
                    return {"audio_path":voices.preview_voice(selected_voice["id"],text,progress=progress)}
                _submit("声音试听 · "+selected_voice["name"],preview)
        else:st.info("保存第一个声音档案后，就可以生成配音。")
    st.divider()
    st.write("数字人口播")
    try:
        profiles=duix.list_profiles()
        if not profiles["models"] or not profiles["voices"]:
            st.info("请先在 Duix 中创建数字人和音色。")
            return
        c1,c2=st.columns(2)
        model=c1.selectbox("选择数字人",profiles["models"],format_func=lambda a:a["name"])
        voice=c2.selectbox("选择本机音色",profiles["voices"],format_func=lambda a:a["name"])
        script=st.text_area("数字人口播文案",key="creator_duix_script",height=160)
        aspect=st.selectbox("数字人画幅",["9:16","16:9"],format_func=lambda a:"竖屏" if a=="9:16" else "横屏")
        if st.button("生成数字人口播",type="primary",key="creator_avatar_start"):
            _submit("数字人口播",duix.generate,script,model["id"],voice["id"],aspect=aspect)
        st.caption("复用本机 Duix 和现有融合任务队列，分阶段使用显存。")
    except Exception as exc:st.warning(str(exc))


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
    if not rows:st.caption("提交任务后，这里会显示进度和结果。")
    labels={"queued":"排队中","running":"处理中","done":"已完成","failed":"失败","interrupted":"已中断","needs_user":"等待处理"}
    for row in rows:
        with st.expander(row["label"]+" · "+labels.get(row["state"],row["state"]),expanded=row["id"]==st.session_state.get("creator_last_job")):
            st.write(row.get("message",""))
            if row["state"] in {"queued","running"}:st.progress(float(row.get("progress",0))/100)
            result=row.get("result")
            if isinstance(result,dict):
                if result.get("text"):
                    st.text_area("生成文案",value=result["text"],key="creator_jobtext_"+row["id"],height=160)
                    st.button("送到视频制作",key="creator_job_use_"+row["id"],on_click=_use_script,args=(result["text"],))
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
                if result.get("srt_path") and Path(result["srt_path"]).is_file():st.download_button("下载字幕",Path(result["srt_path"]).read_bytes(),file_name="字幕.srt",key="creator_job_srt_"+row["id"])
            elif isinstance(result,list):st.success("已保存 "+str(len(result))+" 条结果，请刷新对应页面查看。")
    if st.button("刷新工作台",key="creator_refresh"):
        st.rerun(scope="app")


def render():
    st.markdown('<style>'+Path(__file__).with_name("creator_styles.css").read_text("utf-8")+'</style>',unsafe_allow_html=True)
    st.markdown('<div class="creator-shell"><strong>创作工作台</strong><span> / 从选题开始，把想法做成作品</span></div>',unsafe_allow_html=True)
    st.sidebar.caption("创作流程")
    selected=st.sidebar.radio("工作步骤",["选题和文案","配音","数字人","模板剪辑","标题和封面","发布中心","文案提取","账号选题","声音与数字人"],key="creator_selected_tab",label_visibility="collapsed")
    if selected=="选题和文案":
        from webui.creator_script_workspace import render as render_scripts
        render_scripts()
    elif selected=="配音":
        from webui.creator_voice_workspace import render as render_voice
        render_voice()
    elif selected=="数字人":
        from webui.creator_avatar_workspace import render as render_avatar
        render_avatar()
    elif selected=="模板剪辑":
        _rendering()
    elif selected=="标题和封面":
        from webui.creator_release_workspace import render as render_release
        render_release()
    elif selected=="发布中心":
        _publishing()
    else:
        st.subheader(selected)
        {"文案提取":_extract,"账号选题":_topics,"声音与数字人":_voices,"模板剪辑":_rendering,"发布中心":_publishing}[selected]()
    st.divider()
    with st.expander("任务中心",expanded=selected not in {"选题和文案","配音","数字人","模板剪辑","标题和封面","发布中心"}):
        _tasks()
