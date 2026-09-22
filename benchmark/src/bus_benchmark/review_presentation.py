"""Read-only human presentation of existing review atoms."""
from typing import Any, Mapping
from .review_vocabulary import ATOM_CATEGORY_LABELS, TOKEN_LABELS


def _human_token(value: Any) -> str:
    """Render a machine label for a reviewer without changing stored data."""

    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "待确认"
    text = str(value)
    return TOKEN_LABELS.get(text, text.replace("_", " "))


def _human_atom_statement(atom: Mapping[str, Any]) -> str:
    """Explain one draft atom as a scoring requirement in plain language."""

    predicate = atom.get("predicate")
    arguments = atom.get("arguments", {})
    if predicate == "actor_role_count":
        count = arguments.get("count", "?")
        actor_type = _human_token(arguments.get("type", "参与者"))
        role = _human_token(arguments.get("role", "未指定"))
        if count == "multiple":
            return "场景中包含多个{}，但未限定精确数量（角色：{}）".format(
                actor_type, role
            )
        if count == "optional":
            return "场景中可以包含{}，但它不是必需参与者（角色：{}）".format(
                actor_type, role
            )
        return "场景中需要 {} 个{}（角色：{}）".format(
            _human_token(count), actor_type, role
        )
    if predicate == "actor_relative_region":
        return "{}应位于{}对应的区域".format(
            _human_token(arguments.get("role", "该参与者")),
            _human_token(arguments.get("relation", "指定空间")),
        )
    if predicate == "road_topology":
        return "道路结构应为：{}".format(_human_token(arguments.get("value")))
    if predicate == "lane_configuration":
        feature = _human_token(arguments.get("feature", "道路设施"))
        value = arguments.get("value")
        if isinstance(value, bool):
            return "结构化道路信息：{}{}".format("具备" if value else "不具备", feature)
        if value == "optional":
            return "{}是可选配置（可以有，也可以没有）".format(feature)
        return "{}应为 {}".format(feature, _human_token(value))
    if predicate == "surface_numeric_constraint":
        return "当前 query 明确给出的数值约束：{} {}".format(
            _human_token(arguments.get("value")),
            _human_token(arguments.get("unit", "")),
        ).strip()
    if predicate == "event_spec":
        return "场景应包含外部事件：{}（不规定公交如何反应）".format(
            _human_token(arguments.get("event"))
        )
    if predicate == "temporal_structure":
        return "外部事件的整体时序为：{}".format(
            _human_token(arguments.get("value"))
        )
    if predicate == "before":
        left = arguments.get("first", arguments.get("before", arguments.get("source")))
        right = arguments.get("second", arguments.get("after", arguments.get("target")))
        return "外部事件时序：{}发生在{}之前".format(
            _human_token(left), _human_token(right)
        )
    if predicate == "parallel_group":
        events = arguments.get("events", arguments.get("members", []))
        if isinstance(events, list):
            events = "、".join(_human_token(item) for item in events)
        return "这些外部事件应同时发生：{}".format(_human_token(events))
    if predicate == "rule_mode":
        return "场景中的规则状态应为：{}".format(
            _human_token(arguments.get("value"))
        )
    if predicate == "risk_level":
        return "风险等级被标为：{}；请确认它是否真是生成要求".format(
            _human_token(arguments.get("value"))
        )
    if predicate == "rule_hook":
        return "场景应体现规则/违规：{}（范围：{}）".format(
            _human_token(arguments.get("value")),
            _human_token(arguments.get("scope")),
        )
    rendered = "、".join(
        "{}={}".format(_human_token(key), _human_token(value))
        for key, value in sorted(arguments.items())
    )
    return "{}：{}{}".format(
        ATOM_CATEGORY_LABELS.get(atom.get("category"), _human_token(atom.get("category"))),
        _human_token(predicate),
        "（{}）".format(rendered) if rendered else "",
    )


def _human_atom_evidence(atom: Mapping[str, Any], query_text: str) -> str:
    provenance = atom.get("provenance", {})
    source = provenance.get("source")
    field = provenance.get("field")
    if source == "query_text_regex":
        span = provenance.get("span", [])
        excerpt = ""
        if (
            isinstance(span, list)
            and len(span) == 2
            and all(type(item) is int for item in span)
        ):
            start = max(0, span[0] - 32)
            end = min(len(query_text), span[1] + 32)
            excerpt = query_text[start:end]
        return "来自当前 query 文本{}".format(
            "：“{}”".format(excerpt) if excerpt else ""
        )
    if field:
        return "机器根据结构化元数据字段 {} 提取；仍需对照当前 query 判断是否应计分".format(
            field
        )
    return "机器草稿来源未细分；必须由人工对照当前 query 判断"


def _human_agent_recommendation(value: Any) -> str:
    return {
        "accept_current": "机器建议保留，但仍需人工确认",
        "human_judgment_required": "机器无法可靠判断，需要人工决定",
        "revise_current": "机器怀疑需要修改",
        "reject_current": "机器怀疑不应计分",
    }.get(str(value), "没有可用的机器建议")
