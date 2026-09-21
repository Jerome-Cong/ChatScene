"""Platform-neutral constraint-preserving diversity calculations.

CPD candidate extraction is deliberately query blind.  The extractor emits
anonymous platform-neutral observations without receiving a query, role label,
or oracle; the evaluator alone resolves the frozen policy target signature.
This prevents a method-specific or query-specific actor choice from changing
which variation is rewarded.
"""

import copy
from itertools import combinations
from statistics import mean
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set

from .atoms import normalize_count
from .constants import REPETITIONS
from .errors import ValidationError
from .fixture_extractor_common import COMMON_CANDIDATE_EMITTERS
from .jsonio import canonical_json_bytes, sha256_bytes
from .roster import (
    roster_entries,
    validate_cpd_records_against_roster,
    validate_records_against_roster,
)
from .schema import validate_schema_records


QUERY_BLIND_TARGET_SELECTORS = {
    "actor_longitudinal_distance_bin": {
        "selector_id": "anonymous_road_user_distance_candidates_v1",
        "query_blind": True,
        "candidate_scope": "all_non_ego_road_users",
        "ranking": [
            "emit_all_anonymous_candidates",
            "unique_policy_signature_or_fail_closed",
        ],
        "supported_platforms": ["carla", "metadrive"],
        "missing_target": "fail_closed_dimension_unavailable",
    },
    "yield_realization_mode": {
        "selector_id": "anonymous_yield_subject_candidates_v1",
        "query_blind": True,
        "candidate_scope": "all_road_users_including_ego",
        "ranking": [
            "emit_all_anonymous_yield_subjects",
            "unique_policy_signature_or_fail_closed",
        ],
        "supported_platforms": ["carla", "metadrive"],
        "missing_target": "fail_closed_dimension_unavailable",
    },
    "merge_gap_relation": {
        "selector_id": "anonymous_merge_gap_candidates_v1",
        "query_blind": True,
        "candidate_scope": "all_non_ego_road_users",
        "ranking": [
            "emit_all_anonymous_gap_actors",
            "unique_policy_signature_or_fail_closed",
        ],
        "supported_platforms": ["carla", "metadrive"],
        "missing_target": "fail_closed_dimension_unavailable",
    },
}

TARGET_ACTOR_CLASSES = {"ego_bus", "motor_vehicle", "cyclist", "pedestrian"}


class CPDTargetResolutionError(ValidationError):
    """A valid anonymous candidate set cannot uniquely bind the frozen target."""


def _actor_class_from_oracle_type(actor_type: Any) -> str:
    value = str(actor_type).lower()
    if value in ("e_bike", "bicycle", "cyclist"):
        return "cyclist"
    if value in ("passenger", "pedestrian"):
        return "pedestrian"
    return "motor_vehicle"


def _minimum_required_actor_count(value: Any) -> int:
    normalized = normalize_count(value)
    if normalized == "multiple":
        return 2
    if normalized == "optional":
        return 0
    return int(normalized)


def _minimum_core_actor_class_count(
    oracle_record: Mapping[str, Any], actor_class: str
) -> int:
    return sum(
        _minimum_required_actor_count(atom.get("arguments", {}).get("count"))
        for atom in oracle_record.get("atoms", [])
        if atom.get("category") == "actor"
        and atom.get("predicate") == "actor_role_count"
        and atom.get("layer") == "core_required"
        and atom.get("polarity") == "present"
        and _actor_class_from_oracle_type(
            atom.get("arguments", {}).get("type")
        )
        == actor_class
    )


def _minimum_policy_target_class_count(
    oracle_record: Mapping[str, Any], actor_class: str
) -> int:
    if actor_class == "ego_bus":
        ego_gate = oracle_record.get("ego_gate", {})
        if ego_gate.get("semantic_role") != "ego_bus":
            return 0
        return _minimum_required_actor_count(ego_gate.get("count"))
    return _minimum_core_actor_class_count(oracle_record, actor_class)


def query_blind_target_selector(
    dimension_name: str, target_actor_class: str
) -> Dict[str, Any]:
    """Return a copy of the frozen selector contract for one actor dimension."""

    selector = QUERY_BLIND_TARGET_SELECTORS.get(dimension_name)
    if selector is None:
        raise ValidationError(
            "CPD dimension {} has no query-blind target selector".format(
                dimension_name
            )
        )
    if target_actor_class not in TARGET_ACTOR_CLASSES:
        raise ValidationError(
            "invalid CPD target actor class {!r}".format(target_actor_class)
        )
    result = copy.deepcopy(selector)
    result["target_signature"] = {"actor_class": target_actor_class}
    return result


def _validate_target_selector(dimension: Mapping[str, Any]) -> None:
    name = dimension.get("name")
    expected = QUERY_BLIND_TARGET_SELECTORS.get(name)
    actual = dimension.get("target_selector")
    if expected is None:
        if actual is not None:
            raise ValidationError(
                "non-actor CPD dimension {} cannot declare a target selector".format(
                    name
                )
            )
        return
    if not isinstance(actual, Mapping):
        raise ValidationError(
            "CPD dimension {} lacks a frozen query-blind target selector".format(
                name
            )
        )
    algorithm = {
        key: copy.deepcopy(value)
        for key, value in actual.items()
        if key != "target_signature"
    }
    signature = actual.get("target_signature")
    if (
        canonical_json_bytes(algorithm) != canonical_json_bytes(expected)
        or not isinstance(signature, Mapping)
        or set(signature) != {"actor_class"}
        or signature.get("actor_class") not in TARGET_ACTOR_CLASSES
    ):
        raise ValidationError(
            "CPD dimension {} does not use its frozen query-blind target selector".format(
                name
            )
        )


def _validated_dimensions(policy: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    dimensions = {}
    for dimension in policy.get("dimensions", []):
        name = dimension.get("name")
        if not name or name in dimensions:
            raise ValidationError("CPD dimensions require unique non-empty names")
        if dimension.get("common_semantic") is not True:
            raise ValidationError("CPD dimension {} is not common semantic".format(name))
        allowed_values = dimension.get("allowed_values")
        if (
            not isinstance(allowed_values, list)
            or not allowed_values
            or len({canonical_json_bytes(value) for value in allowed_values}) != len(allowed_values)
        ):
            raise ValidationError("CPD dimension {} requires unique allowed_values".format(name))
        if dimension.get("cardinality", "exactly_one") != "exactly_one":
            raise ValidationError("benchmark v0.1 supports exactly_one CPD dimensions")
        _validate_target_selector(dimension)
        dimensions[name] = dimension
    return dimensions


def validate_cpd_policy_routing(
    oracle_records: Iterable[Mapping[str, Any]],
    expected_eligible_queries: int = None,
) -> Dict[str, Any]:
    """Statically prove that every eligible policy reaches both extractor tracks.

    This check examines frozen policy contracts, their machine-readable oracle
    cardinalities, and the registered anonymous extractor routes.  It does not
    use generated outputs and therefore cannot let a method change its CPD
    opportunity set.
    """

    records = list(oracle_records)
    eligible_ids = set()
    by_style = {"partial": 0, "vague": 0}
    dimension_counts = {}
    selector_counts = {}
    for record in records:
        policy = record.get("cpd_policy", {})
        if policy.get("eligible") is not True:
            continue
        query_id = record.get("query_id")
        if not query_id or query_id in eligible_ids:
            raise ValidationError("eligible CPD policies require unique query IDs")
        if (
            record.get("expected_support") != "supported"
            or record.get("surface_style") not in by_style
        ):
            raise ValidationError(
                "eligible CPD policy is outside supported partial/vague scope"
            )
        dimensions = _validated_dimensions(policy)
        if not dimensions:
            raise ValidationError("eligible CPD policy has no routable dimensions")
        for name, dimension in dimensions.items():
            dimension_counts[name] = dimension_counts.get(name, 0) + 1
            selector = dimension.get("target_selector")
            if selector is None:
                # Platform-neutral road-feature dimensions do not select actors.
                continue
            if selector.get("supported_platforms") != ["carla", "metadrive"]:
                raise ValidationError(
                    "CPD selector {} is not routed on both platforms".format(
                        selector.get("selector_id")
                    )
                )
            selector_id = selector["selector_id"]
            implementation = COMMON_CANDIDATE_EMITTERS.get(name)
            if (
                not isinstance(implementation, Mapping)
                or implementation.get("selector_id") != selector_id
                or implementation.get("supported_platforms")
                != ["carla", "metadrive"]
                or implementation.get("anonymous") is not True
                or implementation.get("target_signature_fields") != ["actor_class"]
            ):
                raise ValidationError(
                    "CPD selector {} has no matching anonymous extractor route".format(
                        selector_id
                    )
                )
            actor_class = selector["target_signature"]["actor_class"]
            if _minimum_policy_target_class_count(record, actor_class) != 1:
                raise ValidationError(
                    "CPD target requires minimum actor-class cardinality 1"
                )
            selector_counts[selector_id] = selector_counts.get(selector_id, 0) + 1
        eligible_ids.add(query_id)
        by_style[record["surface_style"]] += 1
    if (
        expected_eligible_queries is not None
        and len(eligible_ids) != expected_eligible_queries
    ):
        raise ValidationError(
            "expected {} statically routable CPD policies, got {}".format(
                expected_eligible_queries, len(eligible_ids)
            )
        )
    return {
        "eligible_query_count": len(eligible_ids),
        "by_style": by_style,
        "dimension_counts": dict(sorted(dimension_counts.items())),
        "selector_counts": dict(sorted(selector_counts.items())),
        "supported_platforms": ["carla", "metadrive"],
        "query_ids_sha256": sha256_bytes(
            canonical_json_bytes(sorted(eligible_ids))
        ),
    }


def project_common_atoms(
    atoms: Iterable[Mapping[str, Any]],
    approved_dimensions: Mapping[str, Mapping[str, Any]],
    require_complete: bool = True,
    known_dimensions: Iterable[str] = None,
) -> List[str]:
    """Project typed semantic choices while rejecting simulator-native dimensions."""

    approved = dict(approved_dimensions)
    known = set(approved) if known_dimensions is None else set(known_dimensions)
    if not set(approved) <= known:
        raise ValidationError("approved CPD dimensions are absent from the known registry")
    projected = set()
    candidates = {dimension: [] for dimension in approved}
    for atom in atoms:
        if not isinstance(atom, Mapping):
            raise ValidationError("common semantic atom must be an object")
        dimension = atom.get("dimension")
        if dimension not in known:
            raise ValidationError("unapproved CPD dimension {!r}".format(dimension))
        if dimension not in approved:
            continue
        if "value" not in atom:
            raise ValidationError("common semantic atom requires value")
        allowed_values = approved[dimension].get("allowed_values")
        if allowed_values is not None and atom["value"] not in allowed_values:
            raise ValidationError(
                "value {!r} is outside the frozen domain for {}".format(
                    atom["value"], dimension
                )
            )
        candidates[dimension].append(atom)
    unresolved = []
    for dimension, definition in approved.items():
        values = candidates[dimension]
        selector = definition.get("target_selector")
        if selector is not None:
            signature = selector["target_signature"]
            matched = [
                atom
                for atom in values
                if atom.get("target_signature") == signature
            ]
        else:
            matched = values
        if len(matched) != 1:
            unresolved.append(dimension)
            continue
        projected.add(
            canonical_json_bytes(
                {"dimension": dimension, "value": matched[0]["value"]}
            ).decode("utf-8")
        )
    if require_complete and unresolved:
        raise CPDTargetResolutionError(
            "CPD target is missing or ambiguous for dimensions: {}".format(
                ", ".join(sorted(unresolved))
            )
        )
    return sorted(projected)


def jaccard_distance(left: Iterable[str], right: Iterable[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    union = left_set | right_set
    if not union:
        return 0.0
    return 1.0 - len(left_set & right_set) / len(union)


def _preserves_common_core(output: Mapping[str, Any]) -> bool:
    preservation = output.get("preservation", {})
    return all(
        preservation.get(field) is True
        for field in (
            "ego_gate_passed",
            "all_common_core_satisfied",
            "no_common_forbidden",
            "evidence_complete",
        )
    )


def score_query_cpd(
    outputs: Iterable[Mapping[str, Any]],
    oracle_policy: Mapping[str, Any],
    allow_draft_policy: bool = False,
    known_dimensions: Iterable[str] = None,
) -> Dict[str, Any]:
    """Compute CPD over five registered outputs with the fixed ten-pair denominator."""

    if oracle_policy.get("expected_support") != "supported":
        raise ValidationError("CPD_common is defined only for supported queries")
    if oracle_policy.get("surface_style") not in ("partial", "vague"):
        raise ValidationError("precise queries report stability, not CPD_common")
    policy = oracle_policy.get("cpd_policy", {})
    if policy.get("decision_status") != "confirmed" and not allow_draft_policy:
        raise ValidationError("CPD_common requires a confirmed CPD policy")
    if policy.get("eligible") is not True:
        raise ValidationError("ineligible queries have CPD_common=N/A")
    if policy.get("cross_platform_judgeable") is not True and not allow_draft_policy:
        raise ValidationError("CPD policy is not cross-platform judgeable")
    approved_dimensions = _validated_dimensions(policy)
    if not approved_dimensions:
        raise ValidationError("eligible CPD policy has no approved common dimensions")

    by_repetition = {}
    for output in outputs:
        repetition = output.get("repetition")
        if not isinstance(repetition, int) or not 0 <= repetition < REPETITIONS:
            raise ValidationError("CPD repetition must be an integer from 0 to 4")
        if repetition in by_repetition:
            raise ValidationError("duplicate CPD repetition {}".format(repetition))
        if output.get("query_id") != oracle_policy.get("query_id"):
            raise ValidationError("CPD output/oracle query_id mismatch")
        srs_common = output.get("srs_common")
        if not isinstance(srs_common, (int, float)) or not 0.0 <= float(srs_common) <= 1.0:
            raise ValidationError("srs_common must be in [0, 1]")
        normalized = dict(output)
        normalized["preserves_core"] = _preserves_common_core(output)
        try:
            normalized["fingerprint"] = project_common_atoms(
                output.get("common_atoms", []),
                approved_dimensions,
                require_complete=normalized["preserves_core"],
                known_dimensions=known_dimensions,
            )
        except CPDTargetResolutionError:
            normalized["preserves_core"] = False
            normalized["fingerprint"] = project_common_atoms(
                output.get("common_atoms", []),
                approved_dimensions,
                require_complete=False,
                known_dimensions=known_dimensions,
            )
        by_repetition[repetition] = normalized
    if set(by_repetition) != set(range(REPETITIONS)):
        raise ValidationError(
            "CPD requires five finalized outputs; failures must be explicit records"
        )
    padded = []
    for repetition in range(REPETITIONS):
        padded.append(by_repetition[repetition])
    pair_terms = []
    for left, right in combinations(padded, 2):
        indicator = bool(left.get("preserves_core")) and bool(right.get("preserves_core"))
        distance = jaccard_distance(left.get("fingerprint", []), right.get("fingerprint", []))
        pair_terms.append(
            {
                "left": left["repetition"],
                "right": right["repetition"],
                "both_core_preserving": indicator,
                "distance": distance,
                "term": distance if indicator else 0.0,
            }
        )
    if len(pair_terms) != 10:
        raise AssertionError("five repetitions must produce ten CPD pairs")
    diversity = sum(term["term"] for term in pair_terms) / 10.0
    srs_common = mean(float(output.get("srs_common", 0.0)) for output in padded)
    return {
        "query_id": oracle_policy.get("query_id"),
        "surface_style": oracle_policy.get("surface_style"),
        "cpd_common": diversity * srs_common,
        "common_diversity": diversity,
        "mean_srs_common": srs_common,
        "pair_denominator": 10,
        "pair_terms": pair_terms,
        "received_outputs": len(by_repetition),
        "approved_dimensions": sorted(approved_dimensions),
        "decision_status": policy.get("decision_status"),
    }


def compute_coverage(
    oracle_records: Iterable[Mapping[str, Any]],
    allow_draft: bool = False,
    expected_per_style: int = 76,
) -> Dict[str, Any]:
    candidate = [
        record
        for record in oracle_records
        if record.get("expected_support") == "supported"
        and record.get("surface_style") in ("partial", "vague")
    ]
    query_ids = [record.get("query_id") for record in candidate]
    if len(set(query_ids)) != len(query_ids):
        raise ValidationError("CPD coverage candidates contain duplicate query IDs")
    if expected_per_style is not None:
        for style in ("partial", "vague"):
            actual = sum(record["surface_style"] == style for record in candidate)
            if actual != expected_per_style:
                raise ValidationError(
                    "CPD coverage requires {} {} candidates, got {}".format(
                        expected_per_style, style, actual
                    )
                )

    eligible_records = []
    for record in candidate:
        policy = record.get("cpd_policy", {})
        if policy.get("candidate") is not True:
            raise ValidationError("supported partial/vague record is not a CPD candidate")
        if policy.get("decision_status") != "confirmed" and not allow_draft:
            raise ValidationError("formal CPD coverage requires confirmed policies")
        if policy.get("eligible") is True:
            _validated_dimensions(policy)
            if policy.get("cross_platform_judgeable") is not True and not allow_draft:
                raise ValidationError("eligible CPD query is not cross-platform judgeable")
            eligible_records.append(record)

    by_style = {}
    for style in ("partial", "vague"):
        members = [record for record in candidate if record["surface_style"] == style]
        eligible = sum(record in eligible_records for record in members)
        by_style[style] = {
            "eligible": eligible,
            "candidate": len(members),
            "coverage": eligible / len(members) if members else None,
        }
    eligible_all = len(eligible_records)
    eligible_query_ids = sorted(record["query_id"] for record in eligible_records)
    joint = {
        "eligible": eligible_all,
        "candidate": len(candidate),
        "coverage": eligible_all / len(candidate) if candidate else None,
    }
    return {
        "eligible": eligible_all,
        "candidate": len(candidate),
        "coverage": eligible_all / len(candidate) if candidate else None,
        "by_style": by_style,
        "joint": joint,
        "eligible_query_ids": eligible_query_ids,
        "eligible_query_ids_sha256": sha256_bytes(
            canonical_json_bytes(eligible_query_ids)
        ),
        "decision_status": "draft" if allow_draft else "confirmed",
    }


def stability_from_precise(outputs: Iterable[Mapping[str, Any]]) -> Dict[str, float]:
    outputs = list(outputs)
    if len(outputs) < 2:
        return {"mean_pairwise_distance": 0.0, "pair_count": 0}
    distances = [
        jaccard_distance(left.get("fingerprint", []), right.get("fingerprint", []))
        for left, right in combinations(outputs, 2)
    ]
    return {"mean_pairwise_distance": mean(distances), "pair_count": len(distances)}


def _dimension_registry(
    oracle_records: Iterable[Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Union frozen value domains while keeping selector contracts identical."""

    registry: Dict[str, Dict[str, Any]] = {}
    for record in oracle_records:
        for name, dimension in _validated_dimensions(
            record.get("cpd_policy", {})
        ).items():
            if name not in registry:
                registry[name] = copy.deepcopy(dimension)
                continue
            current = registry[name]
            for field in ("common_semantic", "cardinality"):
                if canonical_json_bytes(current.get(field)) != canonical_json_bytes(
                    dimension.get(field)
                ):
                    raise ValidationError(
                        "CPD dimension {} has conflicting frozen contracts".format(name)
                    )
            current_selector = current.get("target_selector")
            next_selector = dimension.get("target_selector")
            if (current_selector is None) != (next_selector is None):
                raise ValidationError(
                    "CPD dimension {} has conflicting target-selector kinds".format(name)
                )
            if current_selector is not None:
                current_algorithm = {
                    key: value
                    for key, value in current_selector.items()
                    if key != "target_signature"
                }
                next_algorithm = {
                    key: value
                    for key, value in next_selector.items()
                    if key != "target_signature"
                }
                if canonical_json_bytes(current_algorithm) != canonical_json_bytes(
                    next_algorithm
                ):
                    raise ValidationError(
                        "CPD dimension {} has conflicting selector algorithms".format(
                            name
                        )
                    )
            encoded = {
                canonical_json_bytes(value): copy.deepcopy(value)
                for value in current["allowed_values"] + dimension["allowed_values"]
            }
            current["allowed_values"] = [encoded[key] for key in sorted(encoded)]
    return registry


def precise_fingerprint(
    common_atoms: Iterable[Mapping[str, Any]],
    oracle_records: Iterable[Mapping[str, Any]],
) -> List[str]:
    """Project a precise-query output onto every registered common dimension."""

    registry = _dimension_registry(oracle_records)
    projected = set()
    for atom in common_atoms:
        if not isinstance(atom, Mapping):
            raise ValidationError("precise common candidate must be an object")
        dimension = atom.get("dimension")
        if dimension not in registry:
            raise ValidationError("unapproved CPD dimension {!r}".format(dimension))
        if atom.get("value") not in registry[dimension]["allowed_values"]:
            raise ValidationError("precise common candidate value is outside its domain")
        value = {"dimension": dimension, "value": atom["value"]}
        if "target_signature" in atom:
            value["target_signature"] = atom["target_signature"]
        projected.add(canonical_json_bytes(value).decode("utf-8"))
    return sorted(projected)


def _macro_summary(values: Sequence[tuple]) -> Dict[str, Any]:
    by_cluster = {}
    for cluster_id, value in values:
        by_cluster.setdefault(cluster_id, []).append(value)
    cluster_values = [mean(items) for items in by_cluster.values()]
    return {
        "eligible_query_count": len(values),
        "scored_query_count": len(values),
        "statistical_cluster_count": len(by_cluster),
        "macro_mean": mean(cluster_values) if cluster_values else None,
    }


def aggregate_cpd_results(
    query_results: Mapping[str, Mapping[str, Any]],
    oracle_records: Iterable[Mapping[str, Any]],
    coverage: Mapping[str, Any],
    precise_outputs: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    method_id: str,
    platform: str,
    provenance: Mapping[str, Any],
    allow_draft_policy: bool = False,
) -> Dict[str, Any]:
    """Build the schema-ready CPD/coverage/stability result envelope."""

    if not method_id:
        raise ValidationError("CPD aggregate requires method_id")
    if platform not in ("carla", "metadrive"):
        raise ValidationError("CPD aggregate platform must be carla or metadrive")
    records = list(oracle_records)
    by_id = {}
    for record in records:
        query_id = record.get("query_id")
        if not query_id or query_id in by_id:
            raise ValidationError("CPD aggregate oracle query IDs must be unique")
        by_id[query_id] = record

    computed_coverage = compute_coverage(
        records,
        allow_draft=allow_draft_policy,
        expected_per_style=None,
    )
    if canonical_json_bytes(coverage) != canonical_json_bytes(computed_coverage):
        raise ValidationError("CPD coverage differs from the frozen oracle policies")
    eligible_ids = set(computed_coverage["eligible_query_ids"])
    if set(query_results) != eligible_ids:
        raise ValidationError("CPD query results do not exactly cover the eligible set")

    normalized_queries = {}
    values_by_style = {"partial": [], "vague": []}
    for query_id in sorted(eligible_ids):
        oracle = by_id[query_id]
        result = copy.deepcopy(dict(query_results[query_id]))
        if result.get("query_id") != query_id:
            raise ValidationError("CPD query result/query_id mismatch")
        style = oracle["surface_style"]
        if result.get("surface_style") != style:
            raise ValidationError("CPD query result surface style mismatch")
        value = result.get("cpd_common")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise ValidationError("CPD query result must be in [0, 1]")
        normalized_queries[query_id] = result
        cluster_id = result.get("statistical_intent_cluster_id")
        if not isinstance(cluster_id, str) or not cluster_id:
            raise ValidationError("CPD query result lacks a statistical cluster")
        values_by_style[style].append((cluster_id, float(value)))

    expected_precise_ids = {
        record["query_id"]
        for record in records
        if record.get("expected_support") == "supported"
        and record.get("surface_style") == "precise"
    }
    if set(precise_outputs) != expected_precise_ids:
        raise ValidationError(
            "precise stability outputs do not exactly cover supported precise queries"
        )
    precise_by_query = {}
    precise_values = []
    total_pairs = 0
    for query_id in sorted(expected_precise_ids):
        outputs = list(precise_outputs[query_id])
        repetitions = [output.get("repetition") for output in outputs]
        if sorted(repetitions) != list(range(REPETITIONS)) or len(set(repetitions)) != REPETITIONS:
            raise ValidationError(
                "precise stability requires repetitions 0..4 for {}".format(query_id)
            )
        for output in outputs:
            fingerprint = output.get("fingerprint")
            if (
                not isinstance(fingerprint, list)
                or len(fingerprint) != len(set(fingerprint))
                or any(not isinstance(value, str) for value in fingerprint)
            ):
                raise ValidationError("precise stability fingerprint is malformed")
        stability = stability_from_precise(outputs)
        if stability["pair_count"] != 10:
            raise AssertionError("five precise outputs must produce ten pairs")
        precise_by_query[query_id] = {
            "query_id": query_id,
            "mean_pairwise_distance": stability["mean_pairwise_distance"],
            "pair_count": stability["pair_count"],
            "received_outputs": len(outputs),
        }
        precise_values.append(stability["mean_pairwise_distance"])
        total_pairs += stability["pair_count"]

    all_values = values_by_style["partial"] + values_by_style["vague"]
    return {
        "schema_version": "0.1",
        "result_kind": "cpd_common_aggregate",
        "method_id": method_id,
        "platform": platform,
        "generation_chain": {
            "verified": provenance.get("generation_chain_verified") is True,
            "generation_run_manifest_sha256": provenance.get(
                "generation_run_manifest_sha256"
            ),
            "implementation_bundle_sha256": provenance.get(
                "implementation_bundle_sha256"
            ),
        },
        "cpd_common": {
            "partial": _macro_summary(values_by_style["partial"]),
            "vague": _macro_summary(values_by_style["vague"]),
            "joint": _macro_summary(all_values),
        },
        "coverage": copy.deepcopy(dict(coverage)),
        "precise_stability": {
            "supported_query_count": len(expected_precise_ids),
            "scored_query_count": len(precise_values),
            "macro_mean_pairwise_distance": (
                mean(precise_values) if precise_values else None
            ),
            "pair_count": total_pairs,
            "queries": precise_by_query,
        },
        "queries": normalized_queries,
        "provenance": copy.deepcopy(dict(provenance)),
    }


def recompute_cpd_aggregate(
    evidence_records: Iterable[Mapping[str, Any]],
    score_records: Iterable[Mapping[str, Any]],
    oracle_records: Iterable[Mapping[str, Any]],
    coverage: Mapping[str, Any],
    roster: Mapping[str, Any],
    *,
    provenance: Mapping[str, Any],
    allow_draft_policy: bool = False,
    expected_per_style: int = None,
) -> Dict[str, Any]:
    """Rebuild a CPD aggregate from the evidence/score source records.

    The formal result publisher uses this pure entry point instead of trusting
    child aggregate numbers.  The CLI also calls it so there is only one
    evidence-to-CPD transformation to audit.
    """

    evidence_records = list(evidence_records)
    score_records = list(score_records)
    oracle_records = list(oracle_records)
    validate_schema_records(
        evidence_records, "semantic_evidence", context="CPD semantic evidence"
    )
    validate_schema_records(
        score_records, "semantic_score", context="CPD semantic score"
    )
    validate_records_against_roster(
        evidence_records, roster, require_confirmed=not allow_draft_policy
    )
    validate_records_against_roster(
        score_records, roster, require_confirmed=not allow_draft_policy
    )
    validate_cpd_policy_routing(oracle_records)
    computed_coverage = compute_coverage(
        oracle_records,
        allow_draft=allow_draft_policy,
        expected_per_style=expected_per_style,
    )
    if canonical_json_bytes(dict(coverage)) != canonical_json_bytes(computed_coverage):
        raise ValidationError("CPD coverage differs from the oracle policies")

    policies = {record["query_id"]: record for record in oracle_records}
    if len(policies) != len(oracle_records):
        raise ValidationError("CPD oracle query IDs must be unique")
    scores_by_run = {record["run_id"]: record for record in score_records}
    if len(scores_by_run) != len(score_records):
        raise ValidationError("CPD score run IDs must be unique")

    normalized_records = []
    eligible_ids = set(coverage["eligible_query_ids"])
    created_at_utc = provenance.get("created_at_utc")
    for evidence in evidence_records:
        if evidence["query_id"] not in eligible_ids:
            continue
        score = scores_by_run.get(evidence["run_id"])
        evidence_sha256 = sha256_bytes(canonical_json_bytes(evidence))
        if score is None or score.get("provenance", {}).get(
            "evidence_record_sha256"
        ) != evidence_sha256:
            raise ValidationError(
                "CPD evidence is not the evidence used for semantic scoring"
            )
        preservation = score.get("common_core_preservation", {})
        normalized = {
            field: evidence[field]
            for field in (
                "schema_version",
                "run_id",
                "method_id",
                "platform",
                "query_id",
                "intent_group_id",
                "statistical_intent_cluster_id",
                "surface_style",
                "repetition",
                "expected_support",
                "terminal_status",
                "common_atoms",
            )
        }
        normalized["srs_common"] = score.get("srs_common")
        normalized["preservation"] = {
            "ego_gate_passed": score.get("ego_gate_passed") is True,
            "all_common_core_satisfied": preservation.get(
                "all_common_core_satisfied"
            )
            is True,
            "no_common_forbidden": preservation.get("no_common_forbidden") is True,
            "evidence_complete": preservation.get("evidence_complete") is True,
        }
        normalized["provenance"] = {
            "semantic_score_sha256": sha256_bytes(canonical_json_bytes(score)),
            "cpd_policy_sha256": sha256_bytes(
                canonical_json_bytes(policies[evidence["query_id"]]["cpd_policy"])
            ),
            "evidence_record_sha256": evidence_sha256,
            "extractor_manifest_sha256": score.get("provenance", {}).get(
                "extractor_manifest_sha256"
            ),
            "extractor_id": evidence.get("provenance", {}).get("extractor_id"),
            "extractor_version": evidence.get("provenance", {}).get(
                "extractor_version"
            ),
            "created_at_utc": created_at_utc,
        }
        normalized_records.append(normalized)

    validate_schema_records(normalized_records, "cpd_input_record", context="CPD input")
    validate_cpd_records_against_roster(
        normalized_records,
        roster,
        coverage["eligible_query_ids"],
        require_confirmed=not allow_draft_policy,
    )
    grouped = {}
    for record in normalized_records:
        grouped.setdefault(record["query_id"], []).append(record)
    entries = roster_entries(roster)
    known_dimensions = {
        dimension["name"]
        for policy in policies.values()
        for dimension in policy.get("cpd_policy", {}).get("dimensions", [])
    }
    query_results = {}
    for query_id, outputs in sorted(grouped.items()):
        query_results[query_id] = score_query_cpd(
            outputs,
            policies[query_id],
            allow_draft_policy=allow_draft_policy,
            known_dimensions=known_dimensions,
        )
        query_results[query_id]["statistical_intent_cluster_id"] = entries[query_id][
            "statistical_intent_cluster_id"
        ]

    precise_outputs = {}
    for evidence in evidence_records:
        policy = policies[evidence["query_id"]]
        if (
            policy.get("expected_support") != "supported"
            or policy.get("surface_style") != "precise"
        ):
            continue
        precise_outputs.setdefault(evidence["query_id"], []).append(
            {
                "repetition": evidence["repetition"],
                "fingerprint": precise_fingerprint(
                    evidence.get("common_atoms", []), oracle_records
                ),
            }
        )
    return aggregate_cpd_results(
        query_results,
        oracle_records,
        coverage,
        precise_outputs,
        method_id=roster["method_id"],
        platform=roster["platform"],
        provenance=provenance,
        allow_draft_policy=allow_draft_policy,
    )
