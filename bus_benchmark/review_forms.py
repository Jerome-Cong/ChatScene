"""Pure review form compiler; formal validation remains in human_workflow."""
import copy
import json
from typing import Any, Dict, List, Mapping
from .atoms import make_atom
from .errors import ValidationError
from .human_workflow import atom_semantic_projection, query_check_projection
from .jsonio import canonical_json_bytes, sha256_bytes
from .oracle import REQUIRED_REVIEW_CHECKS
from .review_vocabulary import (
    AUTO_REASON_PREFIX,
    CHECK_ACCEPT_REASON,
    CHECK_REVISE_REASON,
    CPD_ACCEPT_REASON,
    CPD_REVISION_REASON_TEXT,
    DERIVED_REQUIRED_CHECKS,
    MECHANICAL_REASON_PREFIX,
    STRUCTURED_REASON_PREFIX,
)


def _json_copy(value: Any, label: str) -> Any:
    try:
        return json.loads(canonical_json_bytes(value).decode("utf-8"))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("{} must be finite JSON data".format(label)) from exc


def _json_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _atom_batch_id(atom: Mapping[str, Any]) -> str:
    """Identify one exact scoring requirement without workflow provenance."""

    return _json_sha256(
        {
            "kind": "atom_semantic_batch",
            "atom": atom_semantic_projection(atom),
        }
    )


def _cpd_review_projection(policy: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in policy.items()
        if key != "decision_status"
    }


def _cpd_batch_id(surface_style: str, policy: Mapping[str, Any]) -> str:
    """Keep CPD batching surface-scoped even when policies match byte-for-byte."""

    return _json_sha256(
        {
            "kind": "cpd_policy_batch",
            "surface_style": surface_style,
            "policy": _cpd_review_projection(policy),
        }
    )


def _pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _is_generated_reason(value: Any) -> bool:
    text = str(value or "").strip()
    return text.startswith(
        (AUTO_REASON_PREFIX, MECHANICAL_REASON_PREFIX, STRUCTURED_REASON_PREFIX)
    )


def _normalized_auto_reason(value: Any, canonical: str) -> str:
    """Preserve legacy human text while replacing blank/machine boilerplate."""

    text = str(value or "").strip()
    return canonical if not text or _is_generated_reason(text) else text


def _cpd_revision_reason_code(value: Any) -> str:
    text = str(value or "").strip()
    for code, canonical in CPD_REVISION_REASON_TEXT.items():
        if text == canonical:
            return code
    if text and not _is_generated_reason(text):
        return "other"
    return ""


def _reason_for_verdict(
    verdict: str, value: Any, auto_reasons: Mapping[str, str]
) -> str:
    canonical = auto_reasons.get(verdict)
    if canonical is not None:
        return _normalized_auto_reason(value, canonical)
    return str(value or "").strip()


def _read_json_text(value: str, label: str, expected_type: type) -> Any:
    text = value.strip()
    if not text:
        text = "[]" if expected_type is list else "{}"
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("{} 不是有效 JSON".format(label)) from exc
    if not isinstance(parsed, expected_type):
        raise ValidationError("{} 必须是 {}".format(label, expected_type.__name__))
    return parsed


def _blank_form(task: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "required_check_decisions": {
            check: {"verdict": "", "reason": ""} for check in REQUIRED_REVIEW_CHECKS
        },
        "atom_decisions": [
            {
                "atom_id": atom["atom_id"],
                "verdict": "",
                "reason": "",
                "replacement_atoms_json": "[]",
            }
            for atom in task["oracle_draft"]["atoms"]
        ],
        "cpd_decision": {
            "verdict": "",
            "reason": "",
            "replacement_policy_json": "{}",
        },
        "added_atoms_json": "[]",
        "notes": "",
    }


def _form_from_response(response: Mapping[str, Any]) -> Dict[str, Any]:
    derived_ids = set()
    for item in response["atom_decisions"]:
        if item["verdict"] == "accept":
            derived_ids.add(item["atom_id"])
        else:
            derived_ids.update(
                atom["atom_id"] for atom in item.get("replacement_atoms", [])
            )
    proposed_atoms = response["proposed_oracle"]["atoms"]
    added = [atom for atom in proposed_atoms if atom["atom_id"] not in derived_ids]
    return {
        "required_check_decisions": copy.deepcopy(
            response["required_check_decisions"]
        ),
        "atom_decisions": [
            {
                "atom_id": item["atom_id"],
                "verdict": item["verdict"],
                "reason": item["reason"],
                "replacement_atoms_json": _pretty(item.get("replacement_atoms", [])),
            }
            for item in response["atom_decisions"]
        ],
        "cpd_decision": {
            "verdict": response["cpd_decision"]["verdict"],
            "reason": response["cpd_decision"]["reason"],
            "replacement_policy_json": _pretty(
                response["cpd_decision"].get("replacement_policy") or {}
            ),
        },
        "added_atoms_json": _pretty(added),
        "notes": response.get("notes", ""),
    }


def _normalize_human_atom(raw: Mapping[str, Any], query_id: str) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationError("replacement/new atom 必须是 JSON object")
    try:
        return make_atom(
            raw["category"],
            raw["predicate"],
            raw["arguments"],
            layer=raw.get("layer", "core_required"),
            polarity=raw.get("polarity", "present"),
            weight=raw.get("weight", 1.0),
            provenance={"source": "human_review", "query_id": query_id},
            decision_status="draft",
            notes=str(raw.get("notes", "")),
        )
    except KeyError as exc:
        raise ValidationError("atom 缺少字段 {}".format(exc.args[0])) from exc


def response_from_form(task: Mapping[str, Any], form: Mapping[str, Any]) -> Dict[str, Any]:
    """Build one formal response candidate from a workbench form.

    Four high-level consistency checks are deterministic projections of the
    reviewed atom proposal.  The workbench therefore derives them here instead
    of asking the reviewer to repeat the atom decisions.  Support/disposition
    remains an independent human decision, while the CPD check mirrors the
    separately reviewed CPD policy.
    """

    form = _json_copy(form, "workbench form")
    checks = form.get("required_check_decisions", {})
    if set(checks) != set(REQUIRED_REVIEW_CHECKS):
        raise ValidationError("六项 required checks 必须全部存在")
    support = checks["support_and_response_disposition"]
    support_verdict = str(support.get("verdict", "")).strip()
    support_reason = str(support.get("reason", "")).strip()
    if support_verdict not in ("accept", "revise", "reject") or not support_reason:
        raise ValidationError(
            "support_and_response_disposition 尚未给出 verdict 与理由"
        )

    draft_atoms = {atom["atom_id"]: atom for atom in task["oracle_draft"]["atoms"]}
    raw_decisions = form.get("atom_decisions", [])
    if not isinstance(raw_decisions, list):
        raise ValidationError("atom decisions 必须是列表")
    decision_ids = [item.get("atom_id") for item in raw_decisions]
    if len(set(decision_ids)) != len(decision_ids) or set(decision_ids) != set(
        draft_atoms
    ):
        raise ValidationError("每个 draft atom 必须恰好审核一次")

    final_atoms: List[Dict[str, Any]] = []
    normalized_decisions = []
    for item in raw_decisions:
        atom_id = item["atom_id"]
        verdict = str(item.get("verdict", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if verdict not in ("accept", "reject", "modify", "split", "merge") or not reason:
            raise ValidationError("atom {} 尚未给出 verdict 与理由".format(atom_id))
        replacements_raw = _read_json_text(
            str(item.get("replacement_atoms_json", "[]")),
            "atom {} replacements".format(atom_id),
            list,
        )
        replacements = [
            _normalize_human_atom(raw, task["subject_id"])
            for raw in replacements_raw
        ]
        if verdict in ("accept", "reject") and replacements:
            raise ValidationError("accept/reject atom 不能携带 replacements")
        if verdict == "accept":
            final_atoms.append(copy.deepcopy(draft_atoms[atom_id]))
        elif verdict in ("modify", "split", "merge"):
            final_atoms.extend(copy.deepcopy(replacements))
        normalized_decisions.append(
            {
                "atom_id": atom_id,
                "verdict": verdict,
                "reason": reason,
                "replacement_atoms": replacements,
            }
        )

    added_raw = _read_json_text(
        str(form.get("added_atoms_json", "[]")), "added atoms", list
    )
    final_atoms.extend(
        _normalize_human_atom(raw, task["subject_id"]) for raw in added_raw
    )
    unique_atoms = {}
    for atom in final_atoms:
        existing = unique_atoms.get(atom["atom_id"])
        if existing is not None and canonical_json_bytes(existing) != canonical_json_bytes(
            atom
        ):
            raise ValidationError("相同 atom_id 对应了不同内容")
        unique_atoms[atom["atom_id"]] = atom

    cpd_form = form.get("cpd_decision", {})
    cpd_verdict = str(cpd_form.get("verdict", "")).strip()
    cpd_reason = str(cpd_form.get("reason", "")).strip()
    if cpd_verdict not in ("accept", "revise", "reject") or not cpd_reason:
        raise ValidationError("CPD 尚未给出 verdict 与理由")
    proposed = copy.deepcopy(task["oracle_draft"])
    proposed["atoms"] = sorted(unique_atoms.values(), key=lambda atom: atom["atom_id"])
    proposed["decision_status"] = "draft"
    replacement_policy = None
    if cpd_verdict == "revise":
        replacement_policy = _read_json_text(
            str(cpd_form.get("replacement_policy_json", "{}")),
            "CPD replacement policy",
            dict,
        )
        replacement_policy["decision_status"] = "draft"
        proposed["cpd_policy"] = copy.deepcopy(replacement_policy)
    else:
        proposed["cpd_policy"] = copy.deepcopy(task["oracle_draft"]["cpd_policy"])
        proposed["cpd_policy"]["decision_status"] = "draft"
    for atom in proposed["atoms"]:
        atom["decision_status"] = "draft"

    normalized_checks = {
        "support_and_response_disposition": {
            "verdict": support_verdict,
            "reason": support_reason,
        },
        "cpd_common_eligibility": {
            "verdict": cpd_verdict,
            "reason": cpd_reason,
        },
    }
    draft = task["oracle_draft"]
    for check in DERIVED_REQUIRED_CHECKS:
        submitted = checks[check]
        submitted_verdict = str(submitted.get("verdict", "")).strip()
        submitted_reason = str(submitted.get("reason", "")).strip()
        if submitted_verdict == "reject":
            if not submitted_reason:
                raise ValidationError("{} 的人工异常缺少理由".format(check))
            normalized_checks[check] = {
                "verdict": "reject",
                "reason": submitted_reason,
            }
            continue
        changed = query_check_projection(check, draft) != query_check_projection(
            check, proposed
        )
        normalized_checks[check] = {
            "verdict": "revise" if changed else "accept",
            "reason": CHECK_REVISE_REASON if changed else CHECK_ACCEPT_REASON,
        }

    return {
        "required_check_decisions": normalized_checks,
        "atom_decisions": normalized_decisions,
        "cpd_decision": {
            "verdict": cpd_verdict,
            "reason": cpd_reason,
            "replacement_policy": replacement_policy,
        },
        "proposed_oracle": proposed,
        "notes": str(form.get("notes", "")),
    }


def _inherited_surface_form(
    precise_task: Mapping[str, Any],
    precise_form: Mapping[str, Any],
    target_task: Mapping[str, Any],
    target_mechanical_form: Mapping[str, Any],
) -> Dict[str, Any]:
    """Rebase only after identical text/source semantics establish applicability.

    Any unexplained surface or requirement difference returns a blank draft for
    full review. Existing target work is preserved by the caller's merge step.
    Even applicable inheritance remains unconfirmed and never creates gold.
    """

    from .surface_diff import inheritance_assessment

    precise_record = precise_task["query_record"]
    target_record = target_task["query_record"]
    if precise_record.get("surface_style") != "precise":
        raise ValidationError("inheritance source must be the precise surface")
    if target_record.get("surface_style") not in ("partial", "vague"):
        raise ValidationError("inheritance target must be partial or vague")
    if precise_record.get("intent_group_id") != target_record.get("intent_group_id"):
        raise ValidationError("inheritance source and target must share one intent")

    reviewed_atoms = response_from_form(precise_task, precise_form)["proposed_oracle"]["atoms"]
    applicability = inheritance_assessment(precise_task, target_task, reviewed_atoms)
    if not applicability["can_inherit"]:
        return _blank_form(target_task)

    inherited = _json_copy(target_mechanical_form, "target mechanical form")
    source_decisions = {
        item["atom_id"]: item for item in precise_form["atom_decisions"]
    }
    target_atom_ids = {
        item["atom_id"] for item in inherited["atom_decisions"]
    }
    merge_groups: Dict[str, set] = {}
    for item in precise_form["atom_decisions"]:
        if item.get("verdict") != "merge":
            continue
        replacements = _read_json_text(
            str(item.get("replacement_atoms_json", "[]")),
            "precise merge replacements",
            list,
        )
        if len(replacements) != 1:
            raise ValidationError("completed precise merge must have one target")
        target = _normalize_human_atom(
            replacements[0], target_task["subject_id"]
        )
        merge_groups.setdefault(target["atom_id"], set()).add(item["atom_id"])
    unsafe_merge_sources = set()
    for source_ids in merge_groups.values():
        if not source_ids.issubset(target_atom_ids):
            unsafe_merge_sources.update(source_ids.intersection(target_atom_ids))
    for target_decision in inherited["atom_decisions"]:
        source = source_decisions.get(target_decision["atom_id"])
        if (
            source is not None
            and target_decision["atom_id"] not in unsafe_merge_sources
        ):
            target_decision.update(copy.deepcopy(source))

    inherited["added_atoms_json"] = str(
        precise_form.get("added_atoms_json", "[]")
    )
    inherited["notes"] = str(precise_form.get("notes", ""))
    precise_cpd = copy.deepcopy(precise_form["cpd_decision"])
    if precise_cpd.get("verdict") == "accept":
        # Accept means “accept this surface's source policy”, not “replace the
        # target policy with the precise surface's policy”.
        inherited["cpd_decision"]["verdict"] = "accept"
        inherited["cpd_decision"]["reason"] = CPD_ACCEPT_REASON
        inherited["cpd_decision"]["replacement_policy_json"] = "{}"
    else:
        inherited["cpd_decision"] = precise_cpd
        if precise_cpd.get("verdict") == "revise":
            replacement = _read_json_text(
                str(precise_cpd.get("replacement_policy_json", "{}")),
                "precise CPD replacement policy",
                dict,
            )
            edited_target = copy.deepcopy(target_task["oracle_draft"])
            edited_target["cpd_policy"] = replacement
            if query_check_projection(
                "cpd_common_eligibility", target_task["oracle_draft"]
            ) == query_check_projection(
                "cpd_common_eligibility", edited_target
            ):
                inherited["cpd_decision"] = {
                    "verdict": "accept",
                    "reason": CPD_ACCEPT_REASON,
                    "replacement_policy_json": "{}",
                }

    source_support = precise_form["required_check_decisions"][
        "support_and_response_disposition"
    ]
    inherited["required_check_decisions"] = {
        check: {
            "verdict": "accept",
            "reason": CHECK_ACCEPT_REASON,
        }
        for check in REQUIRED_REVIEW_CHECKS
    }
    inherited["required_check_decisions"][
        "support_and_response_disposition"
    ] = copy.deepcopy(source_support)
    inherited["required_check_decisions"]["cpd_common_eligibility"] = {
        "verdict": inherited["cpd_decision"]["verdict"],
        "reason": inherited["cpd_decision"]["reason"],
    }

    proposal = response_from_form(target_task, inherited)["proposed_oracle"]
    draft = target_task["oracle_draft"]
    for check in REQUIRED_REVIEW_CHECKS:
        if check in (
            "support_and_response_disposition",
            "cpd_common_eligibility",
        ):
            continue
        changed = query_check_projection(check, draft) != query_check_projection(
            check, proposal
        )
        inherited["required_check_decisions"][check] = {
            "verdict": "revise" if changed else "accept",
            "reason": CHECK_REVISE_REASON if changed else CHECK_ACCEPT_REASON,
        }
    if not applicability["cpd_compatible"]:
        inherited["cpd_decision"] = {"verdict": "", "reason": "", "replacement_policy_json": "{}"}
        inherited["required_check_decisions"]["cpd_common_eligibility"] = {"verdict": "", "reason": ""}
    return inherited


def _atom_form_decision_has_content(decision: Mapping[str, Any]) -> bool:
    return bool(
        decision.get("verdict")
        or decision.get("reason")
        or str(decision.get("replacement_atoms_json", "[]")).strip()
        not in ("", "[]")
    )


def _cpd_form_decision_has_content(decision: Mapping[str, Any]) -> bool:
    return bool(
        decision.get("verdict")
        or decision.get("reason")
        or str(decision.get("replacement_policy_json", "{}")).strip()
        not in ("", "{}")
    )


def _merge_inherited_surface_form(
    existing_form: Mapping[str, Any], inherited_form: Mapping[str, Any]
) -> Dict[str, Any]:
    """Fill only blank inherited slots, preserving every instance-level exception."""

    merged = _json_copy(existing_form, "existing target form")
    inherited = _json_copy(inherited_form, "inherited target form")
    inherited_atoms = {
        item["atom_id"]: item for item in inherited["atom_decisions"]
    }
    for decision in merged["atom_decisions"]:
        if not _atom_form_decision_has_content(decision):
            decision.update(copy.deepcopy(inherited_atoms[decision["atom_id"]]))
    for check in REQUIRED_REVIEW_CHECKS:
        current = merged["required_check_decisions"][check]
        if not current.get("verdict") and not current.get("reason"):
            merged["required_check_decisions"][check] = copy.deepcopy(
                inherited["required_check_decisions"][check]
            )
    if not _cpd_form_decision_has_content(merged["cpd_decision"]):
        merged["cpd_decision"] = copy.deepcopy(inherited["cpd_decision"])
    if str(merged.get("added_atoms_json", "[]")).strip() in ("", "[]"):
        merged["added_atoms_json"] = str(inherited.get("added_atoms_json", "[]"))
    if not str(merged.get("notes", "")).strip():
        merged["notes"] = str(inherited.get("notes", ""))
    return merged
