"""Shared annotator wording; descriptions do not change oracle semantics."""
import copy

# Embedded in the packet dictionary, so changes require migration/reconfirmation.
WORDING = {
    "version": "2",
    "heading": "草稿描述",
    "positive": "{condition}。",
    "negative": "场景中不应出现以下情况：“{condition}”。",
    "unknown_direction": "草稿尚未说明是否要求以下情况成立：“{condition}”。",
    "layers": {
        "core_required": "同一意图的各个题目都必须满足上面的描述。请核对当前原文是否支持这项共同要求。",
        "surface_required": "当前题目必须满足上面的描述。请核对这项要求是否来自当前原文。",
        "permitted": "本条是可选描述。场景是否符合这项描述，都不会因本条扣分。",
        "forbidden": "本条是禁止项，场景必须遵守上面的“不应出现”要求。请核对原文是否支持这项禁止要求。",
    },
    "conflict": "草稿把本条归为禁止项，却又要求描述中的情况成立。两种设置有冲突，请暂存并交给专家核对。",
    "unknown_layer": "草稿尚未明确本条是否为强制要求，请暂存并核对。",
    "templates": {
        "actor_role_count": "场景中类型为“{type}”、角色为“{role}”的参与者，其数量为{count}",
        "actor_relative_region": "角色为“{role}”的参与者，其相对位置为“{relation}”",
        "road_topology": "场景中的道路结构为“{value}”",
        "lane_configuration": "场景中的“{feature}”设置为“{value}”",
        "bus_stop_type": "场景中的公交站类型为“{value}”",
        "surface_numeric_constraint": "当前文字片段“{context}”给出的数值为{value}，单位为{unit}",
        "event_spec": "草稿记录的事件或状态为“{event}”",
        "temporal_structure": "场景中各事件的整体时序为“{value}”",
        "before": "事件“{first}”发生在事件“{second}”之前",
        "parallel_group": "以下事件同时发生：{events}",
        "rule_mode": "场景的规则状态为“{value}”",
        "risk_level": "草稿给出的风险等级为“{value}”",
        "rule_hook": "草稿要求在“{scope}”范围内核对“{value}”",
    },
    "feature_present": "场景中有{feature}",
    "feature_absent": "场景中没有{feature}",
    "zero_absent": "场景中不应出现类型为“{type}”、角色为“{role}”的参与者。",
    "fallback": "草稿记录了一项“{label}”，具体内容见下方逐项参数",
    "missing_event": "草稿未明确的参与者、触发条件和结束条件不能仅凭事件名称推断，请对照原文核对。",
    "notes": {
        "Draft records absence metadata without scoring it as forbidden.": "机器把这项设施记录为不存在，但没有把设施的出现列为本题禁止项。",
        "Cardinality normalization is a draft decision.": "参与者数量是机器整理的草稿值，需要对照原文确认。",
        "Metadata label may be event/state/trigger/constraint; classify during review.": "机器标签可能表示事件、状态、触发条件或约束，需要对照原文判断。",
        "Rule-hook scope is preserved; this atom does not prescribe an ego reaction.": "本条保留了规则适用范围，没有指定自车应采取什么反应。",
        "Monolithic topology label must be reviewed against operational evidence.": "道路结构目前只有一个整体标签，需要核对其具体含义与依据。",
        "Confirm whether risk is requested semantics or analysis-only metadata.": "请确认风险等级是原文要求，还是仅供分析的附加信息。",
        "Heuristic role-to-space mapping; human confirmation required.": "参与者所在区域是机器根据角色推测的，需要对照原文确认。",
        "List-order edge is a machine draft and requires explicit confirmation.": "先后关系是机器按列表顺序提出的，需要对照原文确认。",
        "Target binding and tolerance require human review.": "数值对应的对象和允许误差需要人工核对。",
        "Parallel membership requires review.": "哪些事件同时发生，需要对照原文确认。",
    },
}


def wording_catalog():
    return copy.deepcopy(WORDING)


def describe_atom(atom, token):
    """Render one unambiguous description plus its separate scoring scope."""
    from .review_registry import PREDICATE_DEFINITIONS
    args = atom.get("arguments")
    args = args if isinstance(args, dict) else {}
    values = {key: token(value) for key, value in args.items()}
    predicate = atom.get("predicate")
    spec = PREDICATE_DEFINITIONS.get(predicate)
    label = spec[1] if spec else str(predicate)
    if predicate == "before":
        for key, aliases in (("first", ("first", "before", "source")), ("second", ("second", "after", "target"))):
            values[key] = token(next((args[k] for k in aliases if k in args), None))
    if predicate == "parallel_group":
        values["events"] = token(args.get("events", args.get("members")))
    template = WORDING["templates"].get(predicate)
    # Missing/unknown parameters remain visible below, never guessed into prose.
    try:
        condition = template.format(**values) if template else WORDING["fallback"].format(label=label)
    except KeyError:
        condition = WORDING["fallback"].format(label=label)
    if predicate == "lane_configuration" and isinstance(args.get("value"), bool) and isinstance(args.get("feature"), str) and args["feature"].startswith("has_"):
        condition = WORDING["feature_present" if args["value"] else "feature_absent"].format(feature=token(args["feature"]))
    direction = "positive" if atom.get("polarity") == "present" else "negative" if atom.get("polarity") == "absent" else "unknown_direction"
    description = WORDING[direction].format(condition=condition)
    if predicate == "actor_role_count" and atom.get("polarity") == "absent" and str(args.get("count")) == "0" and all(k in values for k in ("type", "role")):
        description = WORDING["zero_absent"].format(**values)
    scope = WORDING["layers"].get(atom.get("layer"), WORDING["unknown_layer"])
    if atom.get("layer") == "forbidden" and atom.get("polarity") == "present":
        scope = WORDING["conflict"]
    return {"description": description, "scope": scope}
