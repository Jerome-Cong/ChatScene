"""Versioned display/edit definitions; labels never add missing semantics."""

from typing import Any, Dict, Mapping

REGISTRY_VERSION = "1"
# label, explanation, editor value type. Original field keys remain canonical.
FIELD_DEFINITIONS = {
    "count": ("数量", "精确数量、未限定的多个或可选参与者，不能互换。", "string"),
    "role": ("参与者角色", "草稿的角色标记，必须对照当前原文核实。", "string"),
    "type": ("参与者类型", "车辆、行人或其他参与者的类型。", "string"),
    "relation": ("相对关系", "与其他对象或道路区域之间的关系。", "string"),
    "value": ("取值", "本要求给出的值；未给出不等于否定或零。", "json"),
    "feature": ("道路特征", "被判断的车道或设施特征。", "string"),
    "context": ("草稿截取的上下文", "机器截取片段不自动成为可信原文依据。", "string"),
    "unit": ("单位", "数值的单位；不能替缺失单位作假设。", "string"),
    "event": ("外部事件", "事件标签本身不证明角色、触发或结束条件。", "string"),
    "first": ("先发生的事件", "先后关系的起点，不代表列表顺序天然正确。", "string"),
    "second": ("后发生的事件", "先后关系的终点。", "string"),
    "before": ("前置事件", "需要先发生的事件。", "string"),
    "after": ("后续事件", "需要后发生的事件。", "string"),
    "source": ("关系起点", "关系或事件的起点对象。", "string"),
    "target": ("关系终点", "关系或事件的目标对象。", "string"),
    "events": ("同时发生的事件", "列表成员全部显示；列表位置不额外表示先后。", "string-list"),
    "members": ("事件组成员", "同一组的全部事件。", "string-list"),
    "scope": ("适用范围", "作用于场景规则、其他参与者规则或违规。", "string"),
    "actor": ("事件参与者", "执行该事件的对象标记。", "string"),
    "subject": ("主体", "承担此要求的对象。", "string"),
    "object": ("关联对象", "此要求关联的另一对象。", "string"),
    "trigger": ("触发条件", "事件从何时或何种条件开始。", "string"),
    "condition": ("条件", "要求成立的前提；缺失时不得补写。", "string"),
    "start": ("开始条件", "事件的开始时刻或条件。", "json"),
    "end": ("结束条件", "事件的结束时刻或条件。", "json"),
    "duration": ("持续时间", "持续多久，须同时核对单位。", "number"),
    "time_unit": ("时间单位", "秒等时间单位。", "string"),
    "speed": ("速度", "速度数值，须同时核对单位。", "number"),
    "distance": ("距离", "距离数值，须同时核对单位及参照对象。", "number"),
    "parameters": ("其他参数", "全部保留展示；嵌套结构需专家核对。", "json"),
}

# category, label, required fields, optional fields; aliases are checked below.
PREDICATE_DEFINITIONS = {
    "actor_role_count": ("actor", "参与者与数量", ("count", "role", "type"), ()),
    "actor_relative_region": ("spatial", "参与者相对位置", ("role", "relation"), ("target", "distance", "unit")),
    "road_topology": ("road", "道路结构", ("value",), ()),
    "lane_configuration": ("road", "道路配置", ("feature", "value"), ()),
    "bus_stop_type": ("road", "公交站类型", ("value",), ()),
    "surface_numeric_constraint": ("spatial", "当前文字中的数值约束", ("value", "unit", "context"), ("role", "relation", "target")),
    "event_spec": ("event", "外部事件要求", ("event",), ("actor", "role", "subject", "object", "target", "trigger", "condition", "start", "end", "duration", "time_unit", "speed", "distance", "unit", "parameters")),
    "temporal_structure": ("temporal", "整体时序", ("value",), ("trigger", "condition")),
    "before": ("temporal", "事件先后", (), ("first", "second", "before", "after", "source", "target")),
    "parallel_group": ("temporal", "同时发生", (), ("events", "members")),
    "rule_mode": ("normative", "规则状态", ("value",), ()),
    "risk_level": ("normative", "风险元数据建议", ("value",), ()),
    "rule_hook": ("normative", "规则或违规要求", ("value", "scope"), ("actor", "role", "target")),
}

TOKEN_TRANSLATIONS = {
    "multiple": "多个（未限定精确数量）", "optional": "可选配置（不是必需参与者或设施）",
    "bicycle": "自行车", "bus": "公交车", "e_bike": "电动自行车", "motorcycle": "摩托车",
    "passenger": "乘客", "pedestrian": "行人", "social_vehicle": "社会车辆",
    "adjacent_lane": "相邻车道", "ahead_of_ego": "公交前方", "behind_ego": "公交后方",
    "crossing_zone": "过街区域", "curb_or_sidewalk": "路缘或人行道", "nonmotor_space": "非机动车空间",
    "opposing_lane": "对向车道", "roadside_or_stop_zone": "路边或站点区域",
    "single_event": "单一事件", "parallel_events": "并行事件", "sequential_events": "顺序事件",
    "conditional_trigger": "条件触发（必须核对具体条件）", "compliant": "遵守规则",
    "aggressive_but_plausible": "激进但可发生", "low": "低", "medium": "中", "high": "高",
    "scene_rules": "场景规则", "counterpart_actor_rules": "其他参与者规则", "counterpart_actor_violations": "其他参与者违规",
    "m": "米（m）", "s": "秒（s）", "m/s": "米/秒（m/s）", "km/h": "千米/小时（km/h）",
}


def field_definition(key: str):
    return FIELD_DEFINITIONS.get(key, ("未登记字段：" + str(key), "词典未解释此字段，须保留并交由专家核对。", "json"))


def presentation_warnings(atom: Mapping[str, Any]):
    warnings = []
    spec = PREDICATE_DEFINITIONS.get(atom.get("predicate"))
    arguments = atom.get("arguments")
    if spec is None:
        warnings.append("未登记的要求类型：{}".format(atom.get("predicate")))
    elif atom.get("category") != spec[0]:
        warnings.append("要求类型与类别不一致")
    if not isinstance(arguments, Mapping):
        return warnings + ["参数不是对象，不能完整解释"]
    if spec:
        for key in spec[2]:
            if key not in arguments or arguments[key] is None or arguments[key] == "":
                warnings.append("草稿未明确：" + field_definition(key)[0])
        for key in sorted(set(arguments) - set(spec[2]) - set(spec[3])):
            warnings.append("未登记参数：" + str(key))
        if atom.get("predicate") == "before":
            for alternatives in (("first", "before", "source"), ("second", "after", "target")):
                if sum(key in arguments for key in alternatives) != 1 or not any(arguments.get(key) for key in alternatives):
                    warnings.append("先后关系端点缺失或存在多个冲突字段")
        if atom.get("predicate") == "parallel_group" and (sum(k in arguments for k in ("events", "members")) != 1 or not (arguments.get("events") or arguments.get("members"))):
            warnings.append("并行事件成员缺失或存在多个冲突字段")
    for key, value in arguments.items():
        if isinstance(value, dict) or isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value):
            warnings.append("嵌套参数需专家逐项核对：" + str(key))
    known = {"atom_id", "category", "predicate", "arguments", "layer", "polarity", "weight", "provenance", "decision_status", "notes"}
    warnings.extend("未登记顶层字段：" + str(k) for k in sorted(set(atom) - known))
    if atom.get("layer") not in ("core_required", "surface_required", "permitted", "forbidden"):
        warnings.append("未登记计分层级")
    if atom.get("polarity") not in ("present", "absent"):
        warnings.append("未登记极性")
    if atom.get("layer") == "forbidden" and atom.get("polarity") == "present":
        warnings.append("禁止层与正向极性组合需专家核对；现有评分按要求出现判定，不自动取反")
    return warnings
