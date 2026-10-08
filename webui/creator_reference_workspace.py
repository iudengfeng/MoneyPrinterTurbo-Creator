"""The reference desktop layout: navigation plus four simultaneous stages."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from webui.creator_reference_state import ReferenceContext, poll_reference_jobs

NAVIGATION = (
    ("首页", "home"), ("视频换人", "face"), ("文生图", "add_photo_alternate"),
    ("图生图", "photo_library"), ("文生视频", "movie"), ("图生视频", "videocam"),
    ("混剪", "shuffle"), ("素材", "image"), ("帮助", "help"),
)
HEADINGS = (
    ("01", "IP深度学习", "IP定位、内容学习与爆款文案"),
    ("02", "口播制作", "音色、语音与数字人口播生成"),
    ("03", "视频处理", "画面处理、字幕、背景音乐与最终成片"),
    ("04", "发布制作", "发布信息、封面制作与多平台发布"),
)

DARK_THEME_CSS = """
body:has(.st-key-reference_workbench) {
  color-scheme:dark;
  --ref-bg:#1c2237; --ref-surface:#252d45; --ref-card:#29324c;
  --ref-text:#e8edff; --ref-muted:#b6c1dd; --ref-line:#46516f;
  --ref-field:#202940; --ref-soft:#313657;
  --ref-bg-start:#17263a; --ref-bg-end:#29213c;
  --ref-sidebar-start:#1c2b42; --ref-sidebar-end:#25283d;
  --ref-header-start:#23324b; --ref-header-end:#302b47;
  --ref-heading-start:#293750; --ref-heading-end:#34304e;
  --ref-brand:#dceaff; --ref-brand-shadow:#354669;
  --ref-badge:#293c50; --ref-badge-text:#dceaff;
  --ref-nav:#d1def4; --ref-nav-icon:#abc5e4;
  --ref-nav-start:#344769; --ref-nav-end:#463b68;
  --ref-counter-start:#3b4768; --ref-counter-end:#4a456d;
  --ref-counter-text:#e0e6fa;
  --ref-empty-start:#28314c; --ref-empty-end:#302f4b;
  --ref-frame:#455271; --ref-scroll:#617299;
}
"""


def _navigate(name):
    st.session_state["ref_sidebar_view"] = name


def _switch_factory():
    st.session_state["creator_route"] = "视频制作"
    st.query_params["workspace"] = "factory"


def _dismiss_tool():
    st.session_state.pop("ref_active_tool", None)


@st.dialog("工作台工具", width="large", on_dismiss=_dismiss_tool)
def _tool_dialog(name):
    if name == "作品库":
        from webui.creator_studio_workspace import render_library
        render_library()
    elif name == "客户／品牌档案":
        from webui.creator_brand_workspace import render
        render()
    elif name == "数字人":
        from webui.creator_avatar_workspace import render
        render()
    elif name == "声音与数字人":
        from webui.creator_workspace import _voices
        _voices()
    elif name == "发布中心":
        from webui.creator_publish_workspace import render
        render()
    elif name == "口播参考库":
        from webui.creator_competitor_workspace import render
        render()


def _sidebar(ctx):
    with st.sidebar.container(key="ref_sidebar_navigation"):
        for name, icon in NAVIGATION:
            current = st.session_state["ref_sidebar_view"] == name
            with st.container(key="ref_nav_active" if current else "ref_nav_" + icon):
                st.button(name, key="ref_navigation_" + icon, icon=f":material/{icon}:",
                          width="stretch", on_click=_navigate, args=(name,))
    with st.sidebar.container(key="ref_sidebar_extras"):
        st.button("作品库", key="ref_open_library", icon=":material/folder_open:",
                  width="stretch", on_click=lambda: st.session_state.update(ref_active_tool="作品库"))
        st.button("客户／品牌档案", key="ref_open_brands", icon=":material/badge:",
                  width="stretch", on_click=lambda: st.session_state.update(ref_active_tool="客户／品牌档案"))
        st.button("速片工厂", key="ref_switch_factory", icon=":material/movie_filter:",
                  width="stretch", on_click=_switch_factory)


def _header(settings_callback=None):
    with st.container(key="ref_topbar"):
        brand, actions = st.columns([5, 2], vertical_alignment="center", gap="small")
        with brand:
            st.markdown('<div class="ref-brand"><span class="ref-logo"></span>'
                        '<strong>宏辉网络科技</strong><span class="ref-online"><i></i> 在线</span>'
                        '<span class="ref-agent">AI Agent</span></div>', unsafe_allow_html=True)
        with actions:
            with st.container(key="ref_header_actions", horizontal=True, horizontal_alignment="right", gap="small"):
                if st.button("任务", key="ref_header_tasks", icon=":material/checklist:", help="查看当前制作进度"):
                    st.session_state["ref_active_tool"] = "任务"
                if settings_callback:
                    st.button("设置", key="ref_header_settings", icon=":material/settings:", on_click=settings_callback)
                theme = "深色" if st.session_state["ref_theme"] == "dark" else "浅色"
                if st.button("换肤 " + theme, key="ref_toggle_theme", icon=":material/dark_mode:"):
                    st.session_state["ref_theme"] = "light" if theme == "深色" else "dark"
                    st.rerun()


def _task_dialog_body():
    from webui.creator_workspace import _tasks
    _tasks()


@st.dialog("任务中心", width="large", on_dismiss=_dismiss_tool)
def _task_dialog():
    _task_dialog_body()


def _auxiliary(ctx, name, factory_callback=None):
    with st.container(key="ref_auxiliary"):
        st.subheader(name)
        if name == "视频换人":
            from webui.creator_avatar_workspace import render
            render()
        elif name == "混剪":
            from webui.creator_render_workspace import render
            render()
        elif name == "素材":
            uploads = st.file_uploader("添加自己的图片或视频", type=["png", "jpg", "jpeg", "webp", "mp4", "mov", "mkv"],
                                       accept_multiple_files=True, key="ref_material_uploads")
            if st.button("保存到当前作品", key="ref_save_materials", type="primary", disabled=not uploads):
                try:
                    for upload in uploads:
                        ctx.use_media("material", ctx.stage_upload(upload))
                    st.success("素材已保存，可在首页继续生成画面。")
                except Exception as exc:
                    st.error(str(exc))
            for item in ctx.project.get("config", {}).get("materials", []):
                path = Path(item if isinstance(item, str) else item.get("path", ""))
                if path.is_file():
                    st.write(path.name)
                    if path.suffix.lower() in {"png", ".png", ".jpg", ".jpeg", ".webp"}:
                        st.image(str(path), width=180)
        elif name == "帮助":
            st.markdown("先导入视频或填写文案，再生成语音和人物口播，处理字幕与音乐，最后预览封面并发布。")
            st.markdown("**一键成片**：在视频处理栏选择“一键成片”，再点击同名按钮，程序会生成视频、封面与发布资料。发布前仍可预览确认。")
            st.markdown("**分步制作**：依次使用“撰写文案”“生成语音”“生成口播”“生成成片”，每一步都能预览。")
            st.markdown("已有作品和客户资料在左下角保存；数字人管理位于口播制作中的“管理形象”。")
        else:
            st.write("使用已有的图像与视频生成服务制作素材，再导入口播作品。")
            if name in {"图生图", "图生视频"}:
                st.info("该入口需要相应的图像编辑或图生视频服务。可以在设置中配置可用服务，并在速片工厂选择制作方式。")
            st.button("打开速片工厂", key="ref_aux_open_factory", type="primary", on_click=_switch_factory)
            if factory_callback:
                st.button("配置生成服务", key="ref_aux_settings", on_click=factory_callback)
        st.button("返回首页", key="ref_aux_home", on_click=_navigate, args=("首页",))


def render(settings_callback=None):
    ctx = ReferenceContext()
    stylesheet = Path(__file__).with_name("creator_reference.css").read_text("utf-8")
    st.markdown('<style>' + stylesheet + '</style>', unsafe_allow_html=True)
    if st.session_state["ref_theme"] == "dark":
        st.markdown('<style>' + DARK_THEME_CSS + '</style>', unsafe_allow_html=True)
    _sidebar(ctx)
    _header(settings_callback)
    with st.container(key="reference_workbench"):
        if error := st.session_state.pop("ref_last_error", None):
            st.error(error)
        if st.session_state["ref_sidebar_view"] == "首页":
            from webui.creator_reference_script import render_script_column
            from webui.creator_reference_production import render_voice_column, render_processing_column
            from webui.creator_reference_release import render as render_release_column
            renderers = (render_script_column, render_voice_column, render_processing_column, render_release_column)
            columns = st.columns(4, gap="small")
            for column, heading, renderer, slug in zip(columns, HEADINGS, renderers, ("script", "voice", "processing", "release")):
                with column:
                    with st.container(key="ref_panel_" + slug, border=True):
                        number, title, subtitle = heading
                        st.markdown(f'<div class="ref-step-heading"><span class="ref-step-number">{number}</span>'
                                    f'<div><strong>{title}</strong><small>{subtitle}</small></div></div>', unsafe_allow_html=True)
                        renderer(ctx)
        else:
            _auxiliary(ctx, st.session_state["ref_sidebar_view"], settings_callback)
    poll_reference_jobs()
    tool = st.session_state.get("ref_active_tool")
    if tool == "任务":
        _task_dialog()
    elif tool:
        _tool_dialog(tool)
