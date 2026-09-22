"""Complete read-only semantic projection; never invent missing requirements."""

import json
from typing import Any, Mapping

from .review_registry import PREDICATE_DEFINITIONS, TOKEN_TRANSLATIONS, field_definition, presentation_warnings
from .review_vocabulary import TOKEN_LABELS


def _human_token(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "草稿未明确"
    if isinstance(value, list):
        return "、".join(_human_token(v) for v in value) if value else "空列表（没有条目）"
    if isinstance(value, dict):
        return "；".join(field_definition(k)[0] + "：" + _human_token(v) for k, v in sorted(value.items())) if value else "空对象"
    text = str(value)
    if text == "":
        return "空值（草稿未明确）"
    fallback = text.replace("_", " ") + "（原标记：" + text + "）" if "_" in text else text
    return TOKEN_TRANSLATIONS.get(text, TOKEN_LABELS.get(text, fallback))


def _human_atom_statement(atom: Mapping[str, Any]) -> str:
    """Show every argument, polarity and scoring layer without semantic inference."""
    spec = PREDICATE_DEFINITIONS.get(atom.get("predicate"))
    label = spec[1] if spec else "未登记要求：" + str(atom.get("predicate"))
    layers = {
        "core_required": "同一意图各表述均必须满足以下条件",
        "surface_required": "当前文字必须满足以下条件",
        "permitted": "允许但不强制满足以下条件（缺失不扣分）",
        "forbidden": "禁止项（按下列出现方向判断，不再额外取反）",
    }
    polarity = {"present": "出现或成立", "absent": "不出现或不成立"}.get(atom.get("polarity"), "极性未明确")
    arguments = atom.get("arguments", {})
    fields = "；".join(field_definition(k)[0] + "：" + _human_token(v) for k, v in sorted(arguments.items())) if isinstance(arguments, Mapping) else repr(arguments)
    statement = "{}：{}；{}。{}".format(layers.get(atom.get("layer"), "计分层级未明确"), polarity, label, fields)
    if atom.get("predicate") == "actor_role_count" and atom.get("polarity") == "absent" and isinstance(arguments, Mapping) and str(arguments.get("count")) == "0":
        statement += "；此处数量 0 表示该类型/角色不得出现，不是对“零个”再取反"
    if atom.get("notes"):
        statement += "；备注：" + str(atom["notes"])
    if atom.get("weight", 1.0) != 1.0:
        statement += "；权重：" + str(atom["weight"])
    warnings = presentation_warnings(atom)
    if warnings:
        statement += "；⚠ " + "；".join(warnings)
    extras = {k: v for k, v in atom.items() if k not in {"atom_id", "category", "predicate", "arguments", "layer", "polarity", "weight", "provenance", "decision_status", "notes"}}
    if extras:
        statement += "；未登记原值：" + json.dumps(extras, ensure_ascii=False, sort_keys=True)
    if atom.get("predicate") == "event_spec" and isinstance(arguments, Mapping):
        missing = [label for keys, label in ((("actor", "role", "subject"), "参与者绑定"), (("trigger", "condition", "start"), "触发条件"), (("end",), "结束条件")) if not any(key in arguments for key in keys)]
        if missing:
            statement += "；草稿未明确" + "、".join(missing) + "，展示层不补写"
    return statement


def atom_evidence(atom: Mapping[str, Any], query_text: str):
    provenance = atom.get("provenance", {})
    if not isinstance(provenance, Mapping):
        return {"kind": "unresolved", "text": "来源记录无效，需核对当前原文"}
    span = provenance.get("span")
    if provenance.get("source") == "query_text_regex" and isinstance(span, list) and len(span) == 2 and all(type(n) is int for n in span) and 0 <= span[0] < span[1] <= len(query_text):
        return {"kind": "query_text", "text": query_text[span[0]:span[1]], "span": span}
    if provenance.get("source") == "query_text_regex":
        return {"kind": "unresolved", "text": "原文位置无效；不能以机器截取片段充当原文依据"}
    return {"kind": "metadata", "text": "机器元数据建议，尚无逐条原文依据", "field": provenance.get("field"), "source": provenance.get("source")}


def _human_atom_evidence(atom: Mapping[str, Any], query_text: str) -> str:
    evidence = atom_evidence(atom, query_text)
    if evidence["kind"] == "query_text":
        return "当前 query 的精确原文依据：“{}”".format(evidence["text"])
    return evidence["text"] + ("；字段：" + str(evidence["field"]) if evidence.get("field") else "")


def project_atom(atom: Mapping[str, Any], query_text: str):
    arguments = atom.get("arguments", {})
    if not isinstance(arguments, Mapping):
        arguments = {}
    evidence = atom_evidence(atom, query_text)
    warnings = presentation_warnings(atom)
    if evidence["kind"] == "unresolved":
        warnings.append(evidence["text"])
    return {
        "atom_id": atom.get("atom_id"),
        "statement": _human_atom_statement(atom),
        "fields": [{"key": k, "label": field_definition(k)[0], "help": field_definition(k)[1], "editor_type": field_definition(k)[2], "value": v} for k, v in arguments.items()],
        "evidence": evidence,
        "warnings": warnings,
        "quick_confirm_allowed": not warnings,
    }


def quick_confirmation_issues(atoms, query_text=""):
    """Mandatory guard for whole-question/group shortcuts; expert edits remain separate."""
    projected = [project_atom(atom, query_text) for atom in atoms]
    return [{"atom_id": item["atom_id"], "warnings": item["warnings"]} for item in projected if not item["quick_confirm_allowed"]]


def _human_agent_recommendation(value: Any) -> str:
    return {
        "accept_current": "机器建议保留，但仍需人工确认",
        "human_judgment_required": "机器无法可靠判断，需要人工决定",
        "revise_current": "机器怀疑需要修改",
        "reject_current": "机器怀疑不应计分",
    }.get(str(value), "没有可用的机器建议")
