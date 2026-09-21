"""Build source-bound, machine-only semantic review drafts for query oracles.

This module is deliberately outside the human-gold finalization path.  It
expands independently written intent-level findings into query-subject form so
one assigned human reviewer has concrete, prioritized drafting context without
silently populating a submission or creating gold.
"""

import copy
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .errors import ValidationError
from .jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
)
from .oracle import REQUIRED_REVIEW_CHECKS
from .schema import validate_schema_instance


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AGENT_REVIEW_FRAGMENT_PATHS = (
    REPOSITORY_ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "agent_review_fragments"
    / "dev_intents.json",
    REPOSITORY_ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "agent_review_fragments"
    / "test_A_C_intents.json",
    REPOSITORY_ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "agent_review_fragments"
    / "test_D_G_intents.json",
)
EXPECTED_INTENT_GROUPS = 100
EXPECTED_SUBJECTS = 300

_SURFACE_STYLES = ("precise", "partial", "vague")
_OVERALL_DISPOSITIONS = ("accept", "accept_with_attention", "revise")
_FINDING_RECOMMENDATIONS = ("accept", "revise", "reject", "human_judgment")
_DECISION_RECOMMENDATIONS = (
    "accept_current",
    "revise_current",
    "reject_current",
    "human_judgment_required",
)
_RECOMMENDATION_MAP = {
    "accept": "accept_current",
    "revise": "revise_current",
    "reject": "reject_current",
    "human_judgment": "human_judgment_required",
}
_RECOMMENDATION_PRIORITY = {
    "accept_current": 0,
    "human_judgment_required": 1,
    "revise_current": 2,
    "reject_current": 3,
}


def _json_sha256(value: Any) -> str:
    try:
        return sha256_bytes(canonical_json_bytes(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError("agent review contains non-canonical JSON data") from exc


def _relative_path(path: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(REPOSITORY_ROOT))
    except ValueError:
        return str(resolved)


def _source_binding(path: Path, role: str) -> Dict[str, Any]:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise ValidationError("agent-review source is missing: {}".format(resolved))
    return {
        "role": role,
        "path": _relative_path(resolved),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def _resolve_declared_path(value: str) -> Path:
    path = Path(value)
    return (REPOSITORY_ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def _validate_fragment_summary(fragment: Mapping[str, Any]) -> None:
    reviews = fragment["intent_reviews"]
    expected = Counter(review["overall_disposition"] for review in reviews)
    declared = fragment["summary"]
    if declared["intent_count"] != len(reviews):
        raise ValidationError("agent review fragment declares a stale intent count")
    if declared["disposition_counts"] != {
        disposition: expected.get(disposition, 0)
        for disposition in _OVERALL_DISPOSITIONS
    }:
        raise ValidationError("agent review fragment declares stale disposition counts")


def _finding_id(intent_group_id: str, finding: Mapping[str, Any]) -> str:
    return _json_sha256(
        {
            "intent_group_id": intent_group_id,
            "finding": finding,
        }
    )


def _applicable_findings(
    intent_review: Mapping[str, Any], surface_style: str
) -> List[Dict[str, Any]]:
    findings = []
    for raw in intent_review["semantic_findings"]:
        if raw["scope"] not in ("all_surfaces", surface_style):
            continue
        finding = copy.deepcopy(raw)
        finding["finding_id"] = _finding_id(intent_review["intent_group_id"], raw)
        findings.append(finding)
    if findings:
        return findings

    # A fragment may contain only a precise-surface exception.  The remaining
    # surfaces still need an explicit, traceable group-level recommendation.
    synthesized = {
        "scope": surface_style,
        "target_type": "oracle_record",
        "target": "overall_oracle",
        "recommendation": (
            "accept"
            if intent_review["overall_disposition"] == "accept"
            else "human_judgment"
        ),
        "reason": (
            "No surface-specific exception was recorded; apply the intent-level "
            "disposition to this surface and verify it as the final human reviewer."
        ),
        "suggested_change": (
            "Inspect this surface against the shared intent findings before adopting "
            "or revising the current oracle."
        ),
    }
    synthesized["finding_id"] = _finding_id(
        intent_review["intent_group_id"], synthesized
    )
    return [synthesized]


def _decision_from_findings(
    findings: Sequence[Mapping[str, Any]], default_reason: str
) -> Dict[str, Any]:
    if not findings:
        return {
            "recommendation": "accept_current",
            "basis": "source_projection_default",
            "finding_ids": [],
            "reason": default_reason,
        }
    mapped = [_RECOMMENDATION_MAP[finding["recommendation"]] for finding in findings]
    recommendation = max(mapped, key=lambda item: _RECOMMENDATION_PRIORITY[item])
    reasons = []
    for finding in findings:
        reason = "{} Suggested action: {}".format(
            finding["reason"].strip(), finding["suggested_change"].strip()
        )
        if reason not in reasons:
            reasons.append(reason)
    return {
        "recommendation": recommendation,
        "basis": "intent_semantic_finding",
        "finding_ids": sorted(finding["finding_id"] for finding in findings),
        "reason": " ".join(reasons),
    }


def _required_check_decisions(
    findings: Sequence[Mapping[str, Any]], intent_group_id: str
) -> Dict[str, Any]:
    result = {}
    for check in REQUIRED_REVIEW_CHECKS:
        targeted = [
            finding
            for finding in findings
            if (
                finding["target_type"] == "required_check"
                and finding["target"] == check
            )
            or (
                check == "cpd_common_eligibility"
                and finding["target_type"] == "cpd_policy"
            )
        ]
        result[check] = _decision_from_findings(
            targeted,
            (
                "The intent-level semantic pass for {} did not target this check; "
                "retain the source projection as drafting context and verify it manually."
            ).format(intent_group_id),
        )
    return result


def _atom_decisions(
    oracle: Mapping[str, Any], findings: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    result = []
    for atom in oracle["atoms"]:
        targeted = [
            finding
            for finding in findings
            if finding["target_type"] == "atom_predicate"
            and finding["target"] == atom["predicate"]
        ]
        note = str(atom.get("notes") or "").strip()
        default_reason = (
            "No intent-level finding targeted predicate {!r}; retain this exact "
            "source-derived atom for human verification."
        ).format(atom["predicate"])
        if note:
            default_reason += " Existing draft caution: {}".format(note)
        decision = _decision_from_findings(targeted, default_reason)
        result.append(
            {
                "atom_id": atom["atom_id"],
                "predicate": atom["predicate"],
                **decision,
            }
        )
    return result


def _cpd_decision(
    findings: Sequence[Mapping[str, Any]], intent_group_id: str
) -> Dict[str, Any]:
    targeted = [
        finding
        for finding in findings
        if finding["target_type"] == "cpd_policy"
        or (
            finding["target_type"] == "required_check"
            and finding["target"] == "cpd_common_eligibility"
        )
    ]
    return _decision_from_findings(
        targeted,
        (
            "The intent-level semantic pass for {} did not identify a CPD-specific "
            "exception; retain the draft policy only as human-review context."
        ).format(intent_group_id),
    )


def _validate_finding_targets(
    review: Mapping[str, Any], queries_by_id: Mapping[str, Mapping[str, Any]],
    oracles_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    for finding in review["semantic_findings"]:
        target_type = finding["target_type"]
        target = finding["target"]
        if target_type == "required_check" and target not in REQUIRED_REVIEW_CHECKS:
            raise ValidationError(
                "agent finding targets unknown required check {!r}".format(target)
            )
        if target_type == "cpd_policy" and target != "cpd_policy":
            raise ValidationError("CPD finding target must be 'cpd_policy'")
        if target_type != "atom_predicate":
            continue
        applicable_ids = [
            query_id
            for query_id in review["query_ids"]
            if finding["scope"] in (
                "all_surfaces",
                queries_by_id[query_id]["surface_style"],
            )
        ]
        if not applicable_ids:
            raise ValidationError("agent finding does not apply to any declared surface")
        for query_id in applicable_ids:
            predicates = {atom["predicate"] for atom in oracles_by_id[query_id]["atoms"]}
            if target not in predicates:
                raise ValidationError(
                    "agent finding targets predicate {!r} absent from {}".format(
                        target, query_id
                    )
                )


def _load_query_sources(
    query_sources: Sequence[Tuple[str, Path, Path]]
) -> Tuple[
    Dict[str, Mapping[str, Any]],
    Dict[str, Mapping[str, Any]],
    Dict[str, List[str]],
    Dict[str, Tuple[Path, Path]],
]:
    queries_by_id: Dict[str, Mapping[str, Any]] = {}
    oracles_by_id: Dict[str, Mapping[str, Any]] = {}
    group_query_ids: Dict[str, List[str]] = {}
    split_paths: Dict[str, Tuple[Path, Path]] = {}
    for split, library_path, oracle_path in query_sources:
        if split not in ("development", "test") or split in split_paths:
            raise ValidationError("query sources require unique development/test splits")
        library_path = Path(library_path).resolve()
        oracle_path = Path(oracle_path).resolve()
        split_paths[split] = (library_path, oracle_path)
        queries = read_jsonl(library_path)
        oracles = read_jsonl(oracle_path)
        if len(queries) != len(oracles):
            raise ValidationError("query library/oracle record counts differ")
        local_queries = {}
        for query in queries:
            query_id = query.get("query_id")
            if not isinstance(query_id, str) or not query_id or query_id in queries_by_id:
                raise ValidationError("query IDs must be unique and non-empty")
            local_queries[query_id] = query
            queries_by_id[query_id] = query
            group_query_ids.setdefault(query["intent_group_id"], []).append(query_id)
        for oracle in oracles:
            validate_schema_instance(oracle, "oracle_record", context="agent-review oracle")
            query_id = oracle["query_id"]
            query = local_queries.get(query_id)
            if query is None or query_id in oracles_by_id:
                raise ValidationError("oracle IDs must match query-library IDs exactly")
            for field in ("intent_group_id", "surface_style"):
                if oracle.get(field) != query.get(field):
                    raise ValidationError(
                        "query/oracle {} mismatch for {}".format(field, query_id)
                    )
            oracles_by_id[query_id] = oracle
    if set(queries_by_id) != set(oracles_by_id):
        raise ValidationError("query library/oracle ID sets differ")
    if set(split_paths) != {"development", "test"}:
        raise ValidationError("agent review requires both development and test sources")
    for intent_group_id, query_ids in group_query_ids.items():
        styles = {queries_by_id[query_id]["surface_style"] for query_id in query_ids}
        if len(query_ids) != 3 or styles != set(_SURFACE_STYLES):
            raise ValidationError(
                "intent {} is not one precise/partial/vague triplet".format(
                    intent_group_id
                )
            )
    return queries_by_id, oracles_by_id, group_query_ids, split_paths


def build_agent_query_review_draft(
    query_sources: Sequence[Tuple[str, Path, Path]],
    fragment_paths: Sequence[Path],
) -> Dict[str, Any]:
    """Consolidate semantic fragments into 300 machine-only subject drafts."""

    queries_by_id, oracles_by_id, group_query_ids, split_paths = _load_query_sources(
        query_sources
    )
    if len(queries_by_id) != EXPECTED_SUBJECTS or len(group_query_ids) != EXPECTED_INTENT_GROUPS:
        raise ValidationError("agent review requires exactly 300 subjects in 100 intents")
    if len(fragment_paths) != 3:
        raise ValidationError("agent review requires exactly three disjoint fragments")

    intent_reviews: Dict[str, Mapping[str, Any]] = {}
    fragment_bindings = []
    for fragment_path in sorted(Path(path).resolve() for path in fragment_paths):
        fragment = read_json(fragment_path)
        validate_schema_instance(
            fragment,
            "agent_semantic_review_fragment",
            context="agent semantic review fragment",
        )
        _validate_fragment_summary(fragment)
        split = fragment["scope"]["split"]
        expected_library, expected_oracle = split_paths[split]
        if (
            _resolve_declared_path(fragment["scope"]["library_path"])
            != expected_library
            or _resolve_declared_path(fragment["scope"]["oracle_path"])
            != expected_oracle
        ):
            raise ValidationError("agent fragment scope does not match current query sources")
        for review in fragment["intent_reviews"]:
            intent_group_id = review["intent_group_id"]
            if intent_group_id in intent_reviews or intent_group_id not in group_query_ids:
                raise ValidationError("agent fragments repeat or invent an intent group")
            expected_ids = set(group_query_ids[intent_group_id])
            if set(review["query_ids"]) != expected_ids:
                raise ValidationError(
                    "agent intent review does not cover its exact query triplet"
                )
            if any(
                queries_by_id[query_id].get("dataset_split", "test") != split
                for query_id in review["query_ids"]
            ):
                raise ValidationError("agent fragment assigns an intent to the wrong split")
            _validate_finding_targets(review, queries_by_id, oracles_by_id)
            intent_reviews[intent_group_id] = review
        fragment_bindings.append(
            _source_binding(fragment_path, "agent_review_fragment")
        )
    if set(intent_reviews) != set(group_query_ids):
        missing = sorted(set(group_query_ids) - set(intent_reviews))
        raise ValidationError(
            "agent fragments do not cover every intent; first missing: {}".format(
                missing[0] if missing else "unknown"
            )
        )

    reviews = []
    recommendation_counts: Counter = Counter()
    basis_counts: Counter = Counter()
    overall_counts: Counter = Counter()
    atom_count = 0
    for query_id in sorted(queries_by_id):
        query = queries_by_id[query_id]
        oracle = oracles_by_id[query_id]
        intent_review = intent_reviews[query["intent_group_id"]]
        findings = _applicable_findings(intent_review, query["surface_style"])
        checks = _required_check_decisions(findings, query["intent_group_id"])
        atoms = _atom_decisions(oracle, findings)
        cpd = _cpd_decision(findings, query["intent_group_id"])
        for decision in list(checks.values()) + atoms + [cpd]:
            recommendation_counts[decision["recommendation"]] += 1
            basis_counts[decision["basis"]] += 1
        overall_counts[intent_review["overall_disposition"]] += 1
        atom_count += len(atoms)
        reviews.append(
            {
                "query_id": query_id,
                "intent_group_id": query["intent_group_id"],
                "surface_style": query["surface_style"],
                "source_query_sha256": _json_sha256(query),
                "source_oracle_sha256": _json_sha256(oracle),
                "source_intent_review_sha256": _json_sha256(intent_review),
                "overall_disposition": intent_review["overall_disposition"],
                "confidence": intent_review["confidence"],
                "semantic_findings": findings,
                "required_check_decisions": checks,
                "atom_decisions": atoms,
                "cpd_decision": cpd,
                "explicit_decision_entries": len(checks) + len(atoms) + 1,
                "human_action_required": True,
            }
        )

    source_bindings = []
    for split, library_path, oracle_path in query_sources:
        prefix = "development" if split == "development" else "test"
        source_bindings.extend(
            (
                _source_binding(library_path, "{}_query_library".format(prefix)),
                _source_binding(oracle_path, "{}_oracle_draft".format(prefix)),
            )
        )
    source_bindings.extend(fragment_bindings)
    result = {
        "schema_version": "0.1",
        "artifact_type": "agent_query_oracle_semantic_review_draft",
        "decision_status": "draft",
        "agent_generated": True,
        "human_gold": False,
        "human_submission_populated": False,
        "review_policy": "one_complete_human_gold_per_subject",
        "review_level": "intent_semantic_review_plus_subject_decision_expansion",
        "source_bindings": source_bindings,
        "reviews": reviews,
        "summary": {
            "intent_groups": len(intent_reviews),
            "subjects": len(reviews),
            "atom_decisions": atom_count,
            "required_check_decisions": len(reviews) * len(REQUIRED_REVIEW_CHECKS),
            "cpd_decisions": len(reviews),
            "explicit_decision_entries": sum(
                review["explicit_decision_entries"] for review in reviews
            ),
            "overall_disposition_counts": {
                disposition: overall_counts.get(disposition, 0)
                for disposition in _OVERALL_DISPOSITIONS
            },
            "recommendation_counts": {
                recommendation: recommendation_counts.get(recommendation, 0)
                for recommendation in _DECISION_RECOMMENDATIONS
            },
            "basis_counts": {
                basis: basis_counts.get(basis, 0)
                for basis in (
                    "intent_semantic_finding",
                    "source_projection_default",
                )
            },
            "admissible_human_gold_records_created": 0,
        },
    }
    result["artifact_id"] = _json_sha256(result)
    validate_agent_query_review_draft(
        result,
        query_sources=query_sources,
        fragment_paths=fragment_paths,
    )
    return result


def validate_agent_query_review_draft(
    value: Mapping[str, Any],
    *,
    query_sources: Sequence[Tuple[str, Path, Path]] = (),
    fragment_paths: Sequence[Path] = (),
) -> Mapping[str, Any]:
    """Validate schema, self-hash, counts, uniqueness, and optional live sources."""

    validate_schema_instance(
        value,
        "agent_query_review_draft",
        context="agent query semantic review draft",
    )
    core = {key: child for key, child in value.items() if key != "artifact_id"}
    if value["artifact_id"] != _json_sha256(core):
        raise ValidationError("agent query review artifact ID does not match its content")
    reviews = value["reviews"]
    query_ids = [review["query_id"] for review in reviews]
    if len(set(query_ids)) != len(query_ids):
        raise ValidationError("agent query review repeats a query subject")
    if any(
        review["explicit_decision_entries"]
        != len(review["required_check_decisions"])
        + len(review["atom_decisions"])
        + 1
        for review in reviews
    ):
        raise ValidationError("agent query review declares a stale subject decision count")

    summary = value["summary"]
    atom_count = sum(len(review["atom_decisions"]) for review in reviews)
    required_check_count = sum(
        len(review["required_check_decisions"]) for review in reviews
    )
    cpd_count = len(reviews)
    explicit_count = sum(review["explicit_decision_entries"] for review in reviews)
    intent_count = len({review["intent_group_id"] for review in reviews})
    if (
        atom_count != summary["atom_decisions"]
        or required_check_count != summary["required_check_decisions"]
        or cpd_count != summary["cpd_decisions"]
        or explicit_count != summary["explicit_decision_entries"]
        or intent_count != summary["intent_groups"]
        or len(reviews) != summary["subjects"]
    ):
        raise ValidationError("agent query review summary does not match its records")

    actual_recommendations: Counter = Counter()
    actual_bases: Counter = Counter()
    actual_overall: Counter = Counter()
    for review in reviews:
        actual_overall[review["overall_disposition"]] += 1
        atom_ids = [decision["atom_id"] for decision in review["atom_decisions"]]
        if len(set(atom_ids)) != len(atom_ids):
            raise ValidationError("agent query review repeats an atom decision")
        for decision in (
            list(review["required_check_decisions"].values())
            + review["atom_decisions"]
            + [review["cpd_decision"]]
        ):
            actual_recommendations[decision["recommendation"]] += 1
            actual_bases[decision["basis"]] += 1
            if (decision["basis"] == "intent_semantic_finding") != bool(
                decision["finding_ids"]
            ):
                raise ValidationError("agent decision basis/finding IDs are inconsistent")
    if summary["overall_disposition_counts"] != {
        disposition: actual_overall.get(disposition, 0)
        for disposition in _OVERALL_DISPOSITIONS
    }:
        raise ValidationError("agent query review has stale overall counts")
    if summary["recommendation_counts"] != {
        recommendation: actual_recommendations.get(recommendation, 0)
        for recommendation in _DECISION_RECOMMENDATIONS
    }:
        raise ValidationError("agent query review has stale recommendation counts")
    if summary["basis_counts"] != {
        basis: actual_bases.get(basis, 0)
        for basis in ("intent_semantic_finding", "source_projection_default")
    }:
        raise ValidationError("agent query review has stale decision-basis counts")

    if bool(query_sources) != bool(fragment_paths):
        raise ValidationError(
            "live agent-review validation requires query sources and fixed fragment paths together"
        )
    if query_sources:
        queries, oracles, group_query_ids, split_paths = _load_query_sources(
            query_sources
        )
        if set(query_ids) != set(queries):
            raise ValidationError("agent query review does not cover current query sources")
        expected_atom_count = sum(len(oracle["atoms"]) for oracle in oracles.values())
        expected_required_check_count = len(queries) * len(REQUIRED_REVIEW_CHECKS)
        expected_cpd_count = len(queries)
        expected_explicit_count = (
            expected_atom_count
            + expected_required_check_count
            + expected_cpd_count
        )
        if (
            summary["atom_decisions"] != expected_atom_count
            or summary["required_check_decisions"]
            != expected_required_check_count
            or summary["cpd_decisions"] != expected_cpd_count
            or summary["explicit_decision_entries"] != expected_explicit_count
        ):
            raise ValidationError(
                "agent query review counts do not match the bound oracle sources"
            )
        by_id = {review["query_id"]: review for review in reviews}
        normalized_fragment_paths = tuple(
            sorted(Path(path).resolve() for path in fragment_paths)
        )
        if (
            len(normalized_fragment_paths) != 3
            or len(set(normalized_fragment_paths)) != 3
        ):
            raise ValidationError(
                "live agent-review validation requires three unique fixed fragments"
            )
        expected_fragment_bindings = [
            _source_binding(path, "agent_review_fragment")
            for path in normalized_fragment_paths
        ]
        expected_source_bindings = []
        for split, library_path, oracle_path in query_sources:
            prefix = "development" if split == "development" else "test"
            expected_source_bindings.extend(
                (
                    _source_binding(library_path, "{}_query_library".format(prefix)),
                    _source_binding(oracle_path, "{}_oracle_draft".format(prefix)),
                )
            )
        expected_source_bindings.extend(expected_fragment_bindings)
        if value["source_bindings"] != expected_source_bindings:
            raise ValidationError(
                "agent query review does not bind the exact ordered fixed sources"
            )

        fragment_reviews = {}
        for path in normalized_fragment_paths:
            fragment = read_json(path)
            validate_schema_instance(
                fragment,
                "agent_semantic_review_fragment",
                context="bound agent semantic review fragment",
            )
            _validate_fragment_summary(fragment)
            split = fragment["scope"]["split"]
            expected_library, expected_oracle = split_paths[split]
            if (
                _resolve_declared_path(fragment["scope"]["library_path"])
                != expected_library
                or _resolve_declared_path(fragment["scope"]["oracle_path"])
                != expected_oracle
            ):
                raise ValidationError(
                    "bound agent fragment scope does not match current query sources"
                )
            for intent_review in fragment["intent_reviews"]:
                intent_group_id = intent_review["intent_group_id"]
                if (
                    intent_group_id in fragment_reviews
                    or intent_group_id not in group_query_ids
                    or set(intent_review["query_ids"])
                    != set(group_query_ids[intent_group_id])
                ):
                    raise ValidationError(
                        "bound agent fragments repeat, invent, or misbind an intent"
                    )
                _validate_finding_targets(intent_review, queries, oracles)
                fragment_reviews[intent_group_id] = intent_review
        if set(fragment_reviews) != set(group_query_ids):
            raise ValidationError("bound agent fragments do not cover every intent")

        for query_id, query in queries.items():
            review = by_id[query_id]
            oracle = oracles[query_id]
            intent_review = fragment_reviews[query["intent_group_id"]]
            expected_findings = _applicable_findings(
                intent_review, query["surface_style"]
            )
            if (
                review["intent_group_id"] != query["intent_group_id"]
                or review["surface_style"] != query["surface_style"]
                or review["source_query_sha256"] != _json_sha256(query)
                or review["source_oracle_sha256"] != _json_sha256(oracle)
                or review["source_intent_review_sha256"]
                != _json_sha256(intent_review)
                or review["overall_disposition"]
                != intent_review["overall_disposition"]
                or review["confidence"] != intent_review["confidence"]
                or review["semantic_findings"] != expected_findings
            ):
                raise ValidationError("agent query review is stale for {}".format(query_id))
            expected_checks = _required_check_decisions(
                expected_findings, query["intent_group_id"]
            )
            expected_atoms = _atom_decisions(oracle, expected_findings)
            expected_cpd = _cpd_decision(
                expected_findings, query["intent_group_id"]
            )
            if (
                review["required_check_decisions"] != expected_checks
                or review["atom_decisions"] != expected_atoms
                or review["cpd_decision"] != expected_cpd
            ):
                raise ValidationError(
                    "agent query review decisions are stale for {}".format(query_id)
                )
    return value


__all__ = [
    "build_agent_query_review_draft",
    "DEFAULT_AGENT_REVIEW_FRAGMENT_PATHS",
    "validate_agent_query_review_draft",
]
