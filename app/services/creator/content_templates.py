"""Six video goals and original examples; examples are never customer facts."""
from __future__ import annotations

import copy

_DURATIONS = [30, 60, 90, 120]
_EXAMPLE_LABEL = "示例，仅用于理解模板，不是客户事实"
_PURPOSES = (
    {"id": "product_service", "name": "产品 / 服务介绍", "description": "说明卖什么、适合谁，以及客户为什么选择你。", "recommended_template": "product_intro"},
    {"id": "promotion", "name": "活动推广", "description": "把真实活动的内容、适用条件和参与方式讲清楚。", "recommended_template": "promotion_offer"},
    {"id": "knowledge", "name": "知识分享", "description": "回答一个问题，用清晰的建议建立专业信任。", "recommended_template": "knowledge_steps"},
    {"id": "case_feedback", "name": "案例 / 客户反馈", "description": "根据已提供的实际案例，呈现问题、过程与结果。", "recommended_template": "case_before_after"},
    {"id": "personal_brand", "name": "个人 IP", "description": "表达你的经历、观点和专业方向，让观众认识你。", "recommended_template": "personal_story"},
    {"id": "quick_edit", "name": "现有素材快剪", "description": "把已有图片或视频整理成一条节奏清晰的成片。", "recommended_template": "quick_highlights"},
)


def _template(id, name, purpose, structure, clues, business_type, context, script, recommended=False):
    return {"id": id, "name": name, "purpose_id": purpose, "purpose": purpose, "structure": structure,
            "material_clues": clues, "recommended": recommended, "target_duration": 60,
            "supported_durations": list(_DURATIONS),
            "example": {"label": _EXAMPLE_LABEL, "business_type": business_type, "context": context, "script": script}}


_TEMPLATES = (
    _template("product_intro", "场景 → 特点 → 下一步", "product_service",
              ["提出目标客户的使用场景", "介绍产品或服务", "解释已提供的特点", "说明适合谁", "给出期望行动"],
              ["产品或服务的真实画面", "特点信息卡", "实际使用过程"], "ecommerce", "示例商家：便携水杯",
              "出门想少带点东西，水杯怎么选？这款示例水杯主打便携和便于清洗。先看杯身尺寸，再看拆洗方式，确认符合你的日常需求后，再查看商品详情。", True),
    _template("product_compare", "问题 → 选择标准 → 适用人群", "product_service",
              ["提出客户常见选择问题", "列出比较标准", "说明提供的产品或服务如何满足标准", "说明适用范围", "给出期望行动"],
              ["选择标准清单", "已提供的参数对比", "产品细节或服务流程"], "service", "示例服务：上门清洁",
              "上门清洁该怎么选？先问清服务范围，再核对项目清单和预约方式。示例服务会提前说明清洁区域，你可以按自己的需要确认项目，再咨询具体安排。"),
    _template("promotion_offer", "活动内容 → 条件 → 参与方式", "promotion",
              ["说明这次活动的价值", "列出客户已提供的活动内容", "讲清实际适用条件", "提示已提供的期限", "说明参与方式"],
              ["实际活动画面", "活动信息卡", "参与步骤"], "local_store", "示例门店：餐饮体验活动",
              "周末想约朋友吃饭，可以先看门店这次体验活动。示例活动包含指定套餐，参与前请确认使用时段和适用条件，再按门店公布的方式预约。", True),
    _template("promotion_countdown", "提醒 → 适合谁 → 活动说明", "promotion",
              ["说明实际活动安排", "点出适合参与的人群", "解释已提供的活动内容", "提示真实参与条件", "给出期望行动"],
              ["活动现场或商品画面", "时间和条件信息卡", "报名或预约步骤"], "knowledge", "示例创作者：公开分享活动",
              "如果你正在学习如何整理知识，可以留意这场示例分享活动。我们会讨论信息分类和复习方法，参加前请查看公布的时间与报名说明，确认适合你再报名。"),
    _template("knowledge_steps", "问题 → 三条建议 → 行动", "knowledge",
              ["提出一个明确问题", "先给结论", "分步讲清可操作建议", "补充适用条件", "引导观众实践或提问"],
              ["问题标题卡", "操作步骤或演示", "建议清单"], "knowledge", "示例创作者：阅读方法",
              "读完一本书，怎么留下有用的内容？先记下一个核心问题，再挑出能回答它的观点，最后用自己的话写一段总结。不要急着抄满整页，先从下一次阅读试起。", True),
    _template("knowledge_mistakes", "误区 → 原因 → 正确做法", "knowledge",
              ["提出一个常见误区", "解释为什么容易踩坑", "给出判断标准", "说明可行做法", "引导进一步提问"],
              ["误区对照卡", "实际操作过程", "判断标准清单"], "service", "示例服务：家庭收纳建议",
              "收纳前先买一堆盒子，为什么常常越收越乱？因为尺寸和物品数量还没有确认。先分类，再测量柜子空间，最后按实际需要选择容器，能省下反复调整的时间。"),
    _template("case_before_after", "原来问题 → 处理过程 → 实际结果", "case_feedback",
              ["交代客户提供的案例背景", "说明原来的实际问题", "展示已记录的处理过程", "呈现可证实的结果", "说明适用范围和下一步"],
              ["经客户许可的案例原始画面", "真实处理过程", "有依据的前后对比"], "service", "示例案例：收纳整理",
              "这是一个示例整理案例：客户希望更方便地找到常用物品。我们先按使用频率分类，再调整摆放位置，最后用标签帮助查找。是否适合你的空间，需要结合实际情况确认。", True),
    _template("case_process", "需求 → 关键动作 → 经验", "case_feedback",
              ["说明案例中的具体需求", "展示关键动作", "呈现客户提供的反馈", "总结可借鉴的方法", "邀请咨询相似需求"],
              ["真实案例过程", "关键动作卡", "获授权的客户反馈"], "ecommerce", "示例案例：商品包装沟通",
              "这个示例从客户的包装需求开始。先确认商品尺寸和运输要求，再核对包装方案，最后根据实际收到的反馈调整。展示案例时，应以真实记录为准，不把个别反馈写成所有人的结果。"),
    _template("personal_story", "经历 → 选择 → 我的做法", "personal_brand",
              ["讲述已提供的一段真实经历", "说明当时的问题或选择", "表达自己的认识", "介绍现在的做法", "邀请同类观众交流"],
              ["本人或已选择的数字人", "已提供的工作场景", "经历与观点信息卡"], "local_store", "示例店主：日常经营",
              "我是示例门店的经营者。对我来说，开店不只是准备产品，也要认真听顾客的问题。我希望把日常经营中的选择讲清楚，让大家知道我们为什么这样做，也欢迎你提出建议。", True),
    _template("personal_viewpoint", "观点 → 理由 → 实践", "personal_brand",
              ["提出本人的明确观点", "结合已提供的经历说明理由", "给出实践方法", "交代适用边界", "邀请讨论"],
              ["本人或已选择的数字人", "实际工作演示", "观点与方法卡"], "knowledge", "示例创作者：持续学习",
              "我的示例观点是：学得多，不如先把一个方法用起来。先选一个实际问题，再用新知识解决它，最后记录哪里有效、哪里需要调整。你最近想把哪项知识用到生活里？"),
    _template("quick_highlights", "精选片段 → 亮点 → 收尾", "quick_edit",
              ["从现有素材选出开头片段", "按内容整理关键画面", "突出素材中可见的亮点", "补充已提供的说明", "以期望行动或画面收尾"],
              ["客户已经上传的视频或图片", "素材可见细节", "简短事实信息卡"], "local_store", "示例素材：门店实拍",
              "用已有门头、店内和产品实拍作为示例：先展示最清楚的一段画面，再按到店体验的顺序串起素材，配上已确认的文字说明，最后给出门店实际提供的联系方式。", True),
    _template("quick_sequence", "按顺序 → 过程 → 结果", "quick_edit",
              ["交代素材呈现的具体场景", "按时间或步骤排列画面", "保留关键过程", "呈现素材中实际可见的结果", "以简短说明收尾"],
              ["客户已上传的过程素材", "实际操作顺序", "素材中可见的结果"], "service", "示例素材：服务过程记录",
              "这组示例素材记录了一个服务过程。先展示准备，再保留关键操作，最后呈现素材中看得到的结果。旁白只解释真实画面，不额外补出没有记录的效果。"),
)


def list_purposes():
    return [{**copy.deepcopy(row), "target_duration": 60} for row in _PURPOSES]


def _purpose(id):
    if not isinstance(id, str) or id not in {row["id"] for row in _PURPOSES}:
        raise ValueError("请选择产品服务、活动、知识、案例、个人 IP 或现有素材快剪。")
    return id


def list_templates(purpose):
    _purpose(purpose)
    return copy.deepcopy([row for row in _TEMPLATES if row["purpose_id"] == purpose])


def get_template(id):
    if not isinstance(id, str):
        raise ValueError("内容模板标识无效。")
    for row in _TEMPLATES:
        if row["id"] == id:
            return copy.deepcopy(row)
    raise ValueError("内容模板不存在，请重新选择。")


def recommend_kind(purpose, profile=None, has_materials=False, has_avatar=False):
    """Select a supported production route from available assets, not industry."""
    _purpose(purpose)
    if purpose == "quick_edit":
        return "montage"
    if purpose == "personal_brand":
        return "avatar" if has_avatar else "knowledge"
    if purpose == "case_feedback":
        return "montage" if has_materials else "knowledge"
    if purpose in {"product_service", "promotion"}:
        return "product" if has_materials else "knowledge"
    return "knowledge"


def suggest_materials(purpose, profile=None):
    """Give upload ideas for a business, without claiming those assets exist."""
    _purpose(purpose)
    profile = profile if isinstance(profile, dict) else {}
    business = profile.get("business_type", "general")
    context = str(profile.get("industry", "")) + " " + str(profile.get("offering", ""))
    if purpose == "quick_edit":
        return ["已经拍好的图片或视频", "素材中的实际细节", "需要保留的说明文字"]
    if business == "local_store":
        if any(word in context for word in ("火锅", "餐饮", "菜", "美食", "咖啡")):
            return ["门头与店内环境", "锅底、菜品或商品实拍（按实际业务选择）", "真实菜单、套餐或活动说明"]
        return ["门头与环境", "实际商品或服务过程", "真实活动与预约信息"]
    if business == "ecommerce":
        return ["商品外观和细节", "实际使用或操作过程", "真实规格、价格与购买说明"]
    if business == "service":
        return ["经客户许可的真实案例", "实际施工或服务过程", "真实服务范围与咨询方式"]
    if business == "knowledge":
        return ["本人或已选择的人物形象", "与主题相关的演示素材", "知识要点与步骤卡"]
    return ["与你提供的产品、服务或内容相关的画面", "实际过程或细节", "本次内容的信息卡"]
