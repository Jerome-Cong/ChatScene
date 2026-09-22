"""Diagnostic sampling-only utilities; not a human assignment or gold workflow."""

import hashlib
from collections import defaultdict
from math import ceil
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .jsonio import canonical_json_bytes


def _stable_order_key(seed: int, value: Mapping[str, Any]) -> str:
    payload = {"seed": seed, "run_id": value.get("run_id"), "query_id": value.get("query_id")}
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def select_stratified_audit_sample(
    records: Iterable[Mapping[str, Any]], rate: float = 0.10, seed: int = 0
) -> Dict[str, Any]:
    records = list(records)
    if not 0.0 < rate <= 1.0:
        raise ValueError("audit rate must be in (0, 1]")
    run_ids = [record.get("run_id") for record in records]
    if any(not run_id for run_id in run_ids) or len(set(run_ids)) != len(run_ids):
        raise ValueError("audit population requires unique non-empty run_id values")
    by_method = defaultdict(list)
    for record in records:
        method_id = record.get("method_id")
        if not method_id:
            raise ValueError("audit population requires method_id")
        by_method[method_id].append(record)

    selected = []
    method_targets = {}
    for method_id, method_records in sorted(by_method.items()):
        method_target = ceil(len(method_records) * rate)
        method_targets[method_id] = method_target
        strata = defaultdict(list)
        for record in method_records:
            score = record.get("srs")
            if isinstance(score, (int, float)):
                score_band = "low" if score < 0.4 else ("mid" if score < 0.8 else "high")
            else:
                score_band = "unscored"
            key = (
                record.get("platform", "unknown"),
                record.get("surface_style", "unknown"),
                record.get("stage_status", "unknown"),
                score_band,
            )
            strata[key].append(record)
        method_selected = []
        for _, members in sorted(strata.items(), key=lambda item: str(item[0])):
            allocation = max(1, round(len(members) * rate))
            ordered = sorted(members, key=lambda value: _stable_order_key(seed, value))
            method_selected.extend(ordered[: min(allocation, len(ordered))])
        if len(method_selected) > method_target:
            method_selected = sorted(
                method_selected, key=lambda value: _stable_order_key(seed, value)
            )[:method_target]
        elif len(method_selected) < method_target:
            chosen_ids = {record["run_id"] for record in method_selected}
            remaining = [record for record in method_records if record["run_id"] not in chosen_ids]
            remaining.sort(key=lambda value: _stable_order_key(seed, value))
            method_selected.extend(remaining[: method_target - len(method_selected)])
        selected.extend(method_selected)

    target = sum(method_targets.values())
    allocations = defaultdict(int)
    for record in selected:
        score = record.get("srs")
        score_band = (
            "low"
            if isinstance(score, (int, float)) and score < 0.4
            else (
                "mid"
                if isinstance(score, (int, float)) and score < 0.8
                else ("high" if isinstance(score, (int, float)) else "unscored")
            )
        )
        key = "|".join(
            map(
                str,
                (
                    record["method_id"],
                    record.get("platform", "unknown"),
                    record.get("surface_style", "unknown"),
                    record.get("stage_status", "unknown"),
                    score_band,
                ),
            )
        )
        allocations[key] += 1
    tasks = []
    for record in selected:
        tasks.append(
            {
                "run_id": record.get("run_id"),
                "query_id": record.get("query_id"),
                "method_id": record.get("method_id"),
                "platform": record.get("platform"),
                "blind_review_id": hashlib.sha256(
                    canonical_json_bytes({"seed": seed, "run_id": record.get("run_id")})
                ).hexdigest()[:16],
                "review_status": "pending",
                "human_srs": None,
                "human_arc": None,
                "human_rsc": None,
                "human_iec_spec": None,
                "error_taxonomy": [],
                "notes": "",
            }
        )
    return {
        "population": len(records),
        "rate": rate,
        "target": target,
        "selected": len(tasks),
        "seed": seed,
        "method_targets": method_targets,
        "allocations": dict(allocations),
        "tasks": tasks,
    }


def collect_draft_decisions(documents: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    decisions = []
    for document in documents:
        source_id = document.get("query_id") or document.get("intent_group_id") or document.get("id")
        if document.get("decision_status") == "draft":
            decisions.append({"source_id": source_id, "scope": "document", "document": document})
        cpd = document.get("cpd_policy", {})
        if cpd.get("decision_status") == "draft":
            decisions.append({"source_id": source_id, "scope": "cpd_policy", "document": cpd})
        for atom in document.get("atoms", []):
            if atom.get("decision_status") == "draft":
                decisions.append(
                    {
                        "source_id": source_id,
                        "scope": "requirement_atom",
                        "atom_id": atom.get("atom_id"),
                        "category": atom.get("category"),
                        "predicate": atom.get("predicate"),
                    }
                )
    return decisions
