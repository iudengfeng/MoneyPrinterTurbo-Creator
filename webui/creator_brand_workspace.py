"""Reusable customer facts, with explicit attachment to the current project."""
from __future__ import annotations

import streamlit as st

from app.services.creator import brand_profiles


BUSINESS_LABELS = {
    "general": "通用客户／品牌", "local_store": "门店与本地生活", "ecommerce": "商品与电商",
    "service": "服务与机构", "knowledge": "知识分享与个人品牌",
}
CORE_LABELS = {
    "offering": "你提供什么商品、服务或内容？",
    "audience": "主要希望哪些人看到？",
    "differentiators": "有什么特点或优势？（可先留空）",
    "desired_action": "看完后，希望观众做什么？（可先留空）",
}
EXTRA_LABELS = {
    "address": "地址", "opening_hours": "营业时间", "activity": "活动资料与有效期",
    "product_features": "商品规格与特点", "price": "价格与套餐", "purchase_url": "购买入口",
    "service_area": "服务范围", "case_notes": "真实案例与反馈", "contact": "联系或预约方式",
    "expertise": "擅长的领域", "content_style": "希望的表达风格",
}
EXTRA_BY_TYPE = {
    "general": ("activity", "contact"),
    "local_store": ("address", "opening_hours", "activity", "price", "contact"),
    "ecommerce": ("product_features", "price", "purchase_url", "activity"),
    "service": ("service_area", "price", "case_notes", "contact"),
    "knowledge": ("expertise", "content_style", "case_notes", "contact"),
}


def _render_editor(profile_id="", attach_to_studio=False):
    original = brand_profiles.get_profile(profile_id) if profile_id else {}
    if profile_id and not original:
        st.error("这份档案已不存在，请重新选择。")
        return
    original = original or {}
    marker = (profile_id, original.get("version", 0))
    if st.session_state.get("brand_editor_loaded") != marker:
        for field in ("name", "industry", "business_type", *CORE_LABELS):
            st.session_state["brand_editor_" + field] = original.get(field, "general" if field == "business_type" else "")
        for field in EXTRA_LABELS:
            st.session_state["brand_editor_extra_" + field] = original.get("extras", {}).get(field, "")
        st.session_state["brand_editor_loaded"] = marker
    st.caption("填写一次，后续视频可以直接复用。只填写你确认的真实资料。")
    name, kind = st.columns(2)
    with name:
        st.text_input("档案名称（可选）", key="brand_editor_name", placeholder="例如：我的品牌／客户名称")
    with kind:
        business_type = st.selectbox("客户类型", list(BUSINESS_LABELS), format_func=BUSINESS_LABELS.get, key="brand_editor_business_type")
    st.text_area(CORE_LABELS["offering"], key="brand_editor_offering", height=80,
                 placeholder="用一句话说明你实际提供的东西。")
    st.text_area(CORE_LABELS["audience"], key="brand_editor_audience", height=80,
                 placeholder="例如：第一次装修的家庭、希望提高效率的上班族。")
    st.text_area(CORE_LABELS["differentiators"], key="brand_editor_differentiators", height=80,
                 placeholder="写具体特点；没有确认的效果或数据可以先不填。")
    st.text_input(CORE_LABELS["desired_action"], key="brand_editor_desired_action",
                  placeholder="例如：留言提问、了解商品、预约体验。")
    with st.expander("补充资料（可选）"):
        st.text_input("行业", key="brand_editor_industry", placeholder="可留空")
        relevant = list(EXTRA_BY_TYPE[business_type])
        for field in original.get("extras", {}):
            if field in EXTRA_LABELS and field not in relevant:
                relevant.append(field)
        for field in relevant:
            st.text_area(EXTRA_LABELS[field], key="brand_editor_extra_" + field, height=70)
    if st.button("保存档案", key="brand_editor_save", type="primary", use_container_width=True):
        try:
            extras = dict(original.get("extras", {}))
            for field in relevant:
                extras[field] = st.session_state.get("brand_editor_extra_" + field, "")
            payload = {field: st.session_state.get("brand_editor_" + field, "")
                       for field in ("name", "industry", "business_type", *CORE_LABELS)}
            payload["extras"] = extras
            saved = brand_profiles.save_profile(payload, profile_id or None,
                                                 expected_version=original.get("version") if profile_id else None)
            st.session_state["brand_editor_saved_id"] = saved["id"]
            if attach_to_studio:
                st.session_state["studio_pending_brand"] = {"id": saved["id"], "snapshot": brand_profiles.snapshot(saved["id"])}
            st.session_state["creator_brand_notice"] = "档案已保存，可以在创作中心反复使用。"
            st.session_state.pop("creator_brand_editor_request", None)
            st.session_state.pop("brand_editor_loaded", None)
            st.rerun(scope="app")
        except (ValueError, RuntimeError) as exc:
            st.error(str(exc))


def _dismiss_editor():
    st.session_state.pop("creator_brand_editor_request", None)
    st.session_state.pop("brand_editor_loaded", None)


@st.dialog("客户／品牌档案", width="large", on_dismiss=_dismiss_editor)
def _profile_dialog(profile_id="", attach_to_studio=False):
    _render_editor(profile_id, attach_to_studio)


def edit_profile(profile_id="", attach_to_studio=False):
    # A fresh explicit opening starts from this customer's saved facts. The
    # dismiss callback can arrive after the browser has already hidden a modal;
    # do not rely on that callback to reset an old customer's unsaved inputs.
    # Fragment/full-page reruns use resume_editor and retain in-progress input.
    st.session_state.pop("brand_editor_loaded", None)
    st.session_state["creator_brand_editor_request"] = {"profile_id": profile_id, "attach_to_studio": attach_to_studio}
    _profile_dialog(profile_id, attach_to_studio)


def resume_editor():
    request = st.session_state.get("creator_brand_editor_request")
    if request:
        _profile_dialog(**request)


def render():
    st.subheader("客户／品牌档案")
    st.caption("保存商品、服务和受众资料，下次创作直接选择。修改档案后，旧作品仍保留当时使用的资料。")
    notice = st.session_state.pop("creator_brand_notice", "")
    if notice:
        st.success(notice)
    opened = st.button("新建档案", key="creator_brand_new", type="primary")
    if opened:
        edit_profile()
    profiles = brand_profiles.list_profiles()
    if not profiles:
        with st.container(border=True, key="creator_brand_empty"):
            st.markdown("**先保存第一份客户资料**")
            st.caption("提供什么、面向谁、有什么特点、希望观众做什么。先填写前两项也能保存。")
        if not opened:
            resume_editor()
        return
    for profile in profiles:
        with st.container(border=True, key="creator_brand_card_" + profile["id"]):
            details, action = st.columns([4, 1], vertical_alignment="center")
            with details:
                st.markdown("**" + str(profile.get("name", "未命名档案")) + "**")
                st.caption(BUSINESS_LABELS.get(profile.get("business_type"), "通用客户／品牌") + " · " + str(profile.get("audience", "")))
                st.write(profile.get("offering", ""))
            with action:
                if st.button("编辑档案", key="creator_brand_edit_" + profile["id"], use_container_width=True):
                    edit_profile(profile["id"])
                    opened = True
            with st.expander("查看已保存的资料"):
                for field, label in CORE_LABELS.items():
                    if profile.get(field):
                        st.write(label.split("？")[0] + "：" + str(profile[field]))
                for field, value in profile.get("extras", {}).items():
                    if value:
                        st.caption(EXTRA_LABELS.get(field, field) + "：" + str(value))
    if not opened:
        resume_editor()
