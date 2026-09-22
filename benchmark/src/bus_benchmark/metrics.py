"""Frozen semantic and robustness metric definitions."""

from collections import defaultdict
import base64
import json
from pathlib import Path
import re
import subprocess
from statistics import mean
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .atoms import atom_key, normalize_count
from .constants import (
    ATOM_CATEGORIES,
    CARLA_EGO_BLUEPRINT,
    EGO_PROXY_LENGTH_M,
    EGO_PROXY_WIDTH_M,
    REPETITIONS,
    SURFACE_STYLES,
)
from .errors import ValidationError
from .jsonio import canonical_json_bytes, sha256_bytes, strict_json_object_bytes


def safe_mean(values: Sequence[float]) -> Optional[float]:
    return mean(values) if values else None


def harmonic_mean(left: float, right: float) -> float:
    if left <= 0.0 or right <= 0.0:
        return 0.0
    return 2.0 * left * right / (left + right)


def _bounded_metric(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("{} must be numeric".format(label))
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValidationError("{} must be in [0, 1]".format(label))
    return value


def set_f1(gold: Iterable[str], predicted: Iterable[str]) -> Dict[str, float]:
    gold_set = set(gold)
    predicted_set = set(predicted)
    true_positive = len(gold_set & predicted_set)
    precision = true_positive / len(predicted_set) if predicted_set else (1.0 if not gold_set else 0.0)
    recall = true_positive / len(gold_set) if gold_set else (1.0 if not predicted_set else 0.0)
    f1 = harmonic_mean(precision, recall)
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "true_positive": true_positive,
        "gold_count": len(gold_set),
        "predicted_count": len(predicted_set),
    }


def _valid_ego_gate(ego: Mapping[str, Any], platform: str = None) -> bool:
    """Require deterministic evidence for the approved semantic proxy and footprint."""

    try:
        length = float(ego.get("length_m"))
        width = float(ego.get("width_m"))
    except (TypeError, ValueError):
        return False
    common = (
        ego.get("count") == 1
        and ego.get("semantic_role") in ("ego_bus", "bus", "bus_proxy")
        and ego.get("approved_proxy") is True
        and ego.get("deterministic") is True
        and abs(length - EGO_PROXY_LENGTH_M) <= 1e-6
        and abs(width - EGO_PROXY_WIDTH_M) <= 1e-6
    )
    if not common:
        return False
    if platform == "carla":
        return ego.get("blueprint") == CARLA_EGO_BLUEPRINT
    if platform == "metadrive":
        return ego.get("vehicle_model") in ("xl", "varying_dynamics", "bounding_box")
    return bool(ego.get("proxy_id"))


def _scored_oracle_atoms(
    oracle: Mapping[str, Any], allow_draft_oracle: bool
) -> List[Mapping[str, Any]]:
    participating = [
        atom
        for atom in oracle.get("atoms", [])
        if atom.get("layer") in ("core_required", "surface_required", "forbidden", "permitted")
    ]
    atoms = [
        atom
        for atom in participating
        if atom.get("layer") in ("core_required", "surface_required", "forbidden")
    ]
    if not allow_draft_oracle:
        unfinished = [
            atom.get("atom_id")
            for atom in participating
            if atom.get("decision_status") != "confirmed"
        ]
        if unfinished:
            raise ValidationError(
                "formal semantic scoring requires confirmed atoms; unfinished: {}".format(unfinished[:5])
            )
    return [
        atom
        for atom in atoms
        if atom.get("decision_status") == "confirmed" or allow_draft_oracle
    ]


def _actor_counts_match(gold: Any, predicted: Any) -> bool:
    gold_count = normalize_count(gold)
    predicted_count = normalize_count(predicted)
    if gold_count == "multiple":
        return predicted_count == "multiple" or (
            predicted_count.isdigit() and int(predicted_count) >= 2
        )
    if gold_count == "optional":
        return True
    return gold_count == predicted_count


def atoms_match(gold: Mapping[str, Any], predicted: Mapping[str, Any]) -> bool:
    if gold.get("category") != predicted.get("category") or gold.get("predicate") != predicted.get(
        "predicate"
    ):
        return False
    if gold.get("category") == "actor" and gold.get("predicate") == "actor_role_count":
        gold_args = gold.get("arguments", {})
        predicted_args = predicted.get("arguments", {})
        if (
            gold.get("polarity") == "absent"
            and normalize_count(gold_args.get("count")) == "0"
        ):
            predicted_count = normalize_count(predicted_args.get("count"))
            count_matches = predicted_count == "multiple" or (
                predicted_count.isdigit() and int(predicted_count) > 0
            )
        else:
            count_matches = _actor_counts_match(
                gold_args.get("count"), predicted_args.get("count")
            )
        return (
            gold_args.get("type") == predicted_args.get("type")
            and gold_args.get("role") == predicted_args.get("role")
            and count_matches
        )
    return atom_key(gold) == atom_key(predicted)


def deterministic_atom_verdict(
    oracle_atom: Mapping[str, Any], evidence: Mapping[str, Any]
) -> str:
    """Derive an atom verdict exclusively from oracle-independent observations."""

    matched = any(
        atoms_match(oracle_atom, candidate)
        for candidate in evidence.get("atoms", [])
        if isinstance(candidate, Mapping)
    )
    complete = oracle_atom.get("category") in set(
        evidence.get("complete_categories", [])
    )
    if oracle_atom.get("polarity", "present") == "present":
        if matched:
            return "satisfied"
        return "violated" if complete else "unknown"
    if complete:
        return "violated" if matched else "satisfied"
    return "unknown"


def validate_repetition_grid(
    records: Sequence[Mapping[str, Any]], expected_query_count: int = None
) -> Dict[str, int]:
    groups = defaultdict(set)
    methods = set()
    for record in records:
        query_id = record.get("query_id")
        repetition = record.get("repetition")
        method_id = record.get("method_id", "__single_method__")
        methods.add(method_id)
        if not query_id:
            raise ValidationError("formal aggregation requires query_id")
        if (
            isinstance(repetition, bool)
            or not isinstance(repetition, int)
            or not 0 <= repetition < REPETITIONS
        ):
            raise ValidationError("formal aggregation requires repetition 0..4")
        key = (method_id, query_id)
        if repetition in groups[key]:
            raise ValidationError("duplicate repetition {} for {}".format(repetition, key))
        groups[key].add(repetition)
    expected = set(range(REPETITIONS))
    incomplete = [key for key, repetitions in groups.items() if repetitions != expected]
    if incomplete:
        raise ValidationError("each query requires five finalized records; incomplete: {}".format(incomplete[:5]))
    if len(methods) != 1:
        raise ValidationError("aggregate one method at a time, got {}".format(sorted(methods)))
    query_count = len(groups)
    if expected_query_count is not None and query_count != expected_query_count:
        raise ValidationError(
            "expected {} queries, got {}".format(expected_query_count, query_count)
        )
    return {"query_count": query_count, "record_count": len(records), "method_count": len(methods)}


def _diagnostic_f1(
    oracle_atoms: Sequence[Mapping[str, Any]],
    generated_atoms: Sequence[Mapping[str, Any]],
    atom_results: Sequence[Mapping[str, Any]],
    categories: Tuple[str, ...],
    complete_categories: Iterable[str],
) -> Dict[str, Any]:
    """Compute an open-set F1 only from a proven-complete prediction inventory.

    A requirement-by-requirement Judge can establish whether gold atoms are
    satisfied, but it cannot discover extra generated tuples.  Treating those
    Judge verdicts as a complete predicted set would make precision
    systematically optimistic.  The native extractor must therefore declare
    every participating category complete before ARC/RSC/IEC_spec is available.
    """

    complete = set(complete_categories or ())
    missing = sorted(set(categories) - complete)
    if missing:
        return {
            "availability": "unavailable",
            "reason": "prediction inventory is not complete for: {}".format(
                ", ".join(missing)
            ),
            "missing_complete_categories": missing,
        }
    result_by_id = {result["atom_id"]: result for result in atom_results}
    gold = [
        atom
        for atom in oracle_atoms
        if atom.get("category") in categories
        and atom.get("polarity", "present") == "present"
        and atom.get("layer") in ("core_required", "surface_required")
    ]
    permitted = [
        atom
        for atom in oracle_atoms
        if atom.get("category") in categories and atom.get("layer") == "permitted"
    ]
    predicted = [atom for atom in generated_atoms if atom.get("category") in categories]

    # Match observed predictions to satisfied gold atoms one-to-one.  A bound Judge
    # verdict is itself the semantic observation for atoms that the deterministic
    # extractor cannot decide, so a Judge-satisfied atom must also enter the
    # predicted side of the F1 accounting.  Otherwise TP can be positive while the
    # reported prediction set is empty.
    unmatched_gold = set(range(len(gold)))
    true_positive = 0
    false_positive = 0
    for candidate in predicted:
        matching_index = next(
            (
                index
                for index in sorted(unmatched_gold)
                if result_by_id.get(gold[index].get("atom_id"), {}).get("verdict")
                == "satisfied"
                and atoms_match(gold[index], candidate)
            ),
            None,
        )
        if matching_index is not None:
            unmatched_gold.remove(matching_index)
            true_positive += 1
            continue
        if any(atoms_match(atom, candidate) for atom in permitted):
            continue
        false_positive += 1

    judge_true_positive = 0
    for index in tuple(sorted(unmatched_gold)):
        result = result_by_id.get(gold[index].get("atom_id"), {})
        if result.get("verdict_source") == "judge" and result.get("verdict") == "satisfied":
            unmatched_gold.remove(index)
            true_positive += 1
            judge_true_positive += 1

    false_negative = len(unmatched_gold)
    scored_predicted_count = true_positive + false_positive
    precision = (
        true_positive / scored_predicted_count
        if scored_predicted_count
        else (1.0 if not gold else 0.0)
    )
    recall = true_positive / len(gold) if gold else (1.0 if not false_positive else 0.0)
    return {
        "availability": "available",
        "precision": precision,
        "recall": recall,
        "f1": harmonic_mean(precision, recall),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "gold_count": len(gold),
        "predicted_count": scored_predicted_count,
        "judge_true_positive": judge_true_positive,
    }


def score_semantic_output(
    oracle: Mapping[str, Any],
    evidence: Mapping[str, Any],
    allow_draft_oracle: bool = False,
    judge_verdicts: Mapping[str, Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Score one finalized artifact without using runtime success as a semantic gate."""

    if oracle.get("query_id") != evidence.get("query_id"):
        raise ValidationError("oracle/evidence query_id mismatch")
    if oracle.get("expected_support") != "supported":
        return {
            "query_id": oracle.get("query_id"),
            "intent_group_id": oracle.get("intent_group_id"),
            "statistical_intent_cluster_id": oracle.get(
                "statistical_intent_cluster_id", oracle.get("intent_group_id")
            ),
            "surface_style": oracle.get("surface_style"),
            "expected_support": "unsupported",
            "semantic_metrics": None,
            "reason": "unsupported queries are scored through UQH only",
        }
    if oracle.get("decision_status") != "confirmed" and not allow_draft_oracle:
        raise ValidationError("semantic scoring requires a confirmed oracle")

    gate = _valid_ego_gate(evidence.get("ego", {}), evidence.get("platform"))
    oracle_atoms = _scored_oracle_atoms(oracle, allow_draft_oracle)
    permitted_atoms = [
        atom
        for atom in oracle.get("atoms", [])
        if atom.get("layer") == "permitted"
        and (allow_draft_oracle or atom.get("decision_status") == "confirmed")
    ]
    generated_atoms = list(evidence.get("atoms", []))
    complete_categories = evidence.get("complete_categories", [])
    judge_verdicts = dict(judge_verdicts or {})
    oracle_atom_ids = {atom["atom_id"] for atom in oracle_atoms}
    if not set(judge_verdicts) <= oracle_atom_ids:
        raise ValidationError("judge verdicts contain atoms outside the scored oracle")
    atom_results = []
    category_values = defaultdict(list)
    for atom in oracle_atoms:
        category = atom["category"]
        verdict = deterministic_atom_verdict(atom, evidence)
        judge_binding = judge_verdicts.get(atom["atom_id"])
        judge_verdict = (
            judge_binding.get("verdict")
            if isinstance(judge_binding, Mapping)
            else None
        )
        if judge_binding is not None and (
            not isinstance(judge_binding, Mapping)
            or set(judge_binding)
            != {
                "verdict",
                "judge_response_id",
                "judge_response_record_sha256",
            }
            or not isinstance(judge_binding.get("judge_response_id"), str)
            or re.fullmatch(r"[0-9a-f]{64}", judge_binding["judge_response_id"])
            is None
            or not isinstance(
                judge_binding.get("judge_response_record_sha256"), str
            )
            or re.fullmatch(
                r"[0-9a-f]{64}", judge_binding["judge_response_record_sha256"]
            )
            is None
        ):
            raise ValidationError("judge verdict lacks an exact raw-response binding")
        if judge_verdict not in (None, "satisfied", "violated", "unknown"):
            raise ValidationError("invalid judge verdict {!r}".format(judge_verdict))
        verdict_source = "deterministic" if verdict != "unknown" else "unresolved"
        if judge_verdict is not None:
            if verdict != "unknown":
                raise ValidationError("judge cannot override deterministic evidence")
            verdict = judge_verdict
            verdict_source = "judge"
        satisfied = verdict == "satisfied"
        weight = float(atom.get("weight", 1.0))
        if weight != 1.0:
            raise ValidationError("benchmark v0.1 requires atom weight=1.0")
        category_values[category].append(1.0 if satisfied else 0.0)
        atom_result = {
            "atom_id": atom["atom_id"],
            "category": category,
            "layer": atom["layer"],
            "polarity": atom.get("polarity", "present"),
            "verdict": verdict,
            "verdict_source": verdict_source,
            "satisfied": satisfied,
            "weight": weight,
        }
        if judge_binding is not None:
            atom_result["judge_response_id"] = judge_binding["judge_response_id"]
            atom_result["judge_response_record_sha256"] = judge_binding[
                "judge_response_record_sha256"
            ]
        atom_results.append(atom_result)

    category_scores = {}
    for category in ATOM_CATEGORIES:
        values = category_values.get(category, [])
        if not values:
            continue
        category_scores[category] = mean(values)
    srs = mean(category_scores.values()) if category_scores and gate else 0.0

    common_category_values = defaultdict(list)
    for atom, result in zip(oracle_atoms, atom_results):
        if atom.get("layer") == "core_required":
            common_category_values[atom["category"]].append(
                1.0 if result["satisfied"] else 0.0
            )
    common_category_scores = {
        category: mean(values) for category, values in common_category_values.items()
    }
    srs_common = (
        mean(common_category_scores.values()) if common_category_scores and gate else 0.0
    )
    common_results = [
        result for result in atom_results if result["layer"] == "core_required"
    ]

    diagnostic_oracle_atoms = list(oracle_atoms) + permitted_atoms
    actor_f1 = _diagnostic_f1(
        diagnostic_oracle_atoms,
        generated_atoms,
        atom_results,
        ("actor",),
        complete_categories,
    )
    road_spatial_f1 = _diagnostic_f1(
        diagnostic_oracle_atoms,
        generated_atoms,
        atom_results,
        ("road", "spatial"),
        complete_categories,
    )
    event_f1 = _diagnostic_f1(
        diagnostic_oracle_atoms,
        generated_atoms,
        atom_results,
        ("event", "temporal"),
        complete_categories,
    )
    return {
        "query_id": oracle["query_id"],
        "intent_group_id": oracle["intent_group_id"],
        "surface_style": oracle["surface_style"],
        "statistical_intent_cluster_id": oracle.get(
            "statistical_intent_cluster_id", oracle["intent_group_id"]
        ),
        "expected_support": "supported",
        "ego_gate_passed": gate,
        "srs": srs,
        "srs_common": srs_common,
        "common_core_preservation": {
            "all_common_core_satisfied": bool(common_results)
            and all(result["satisfied"] for result in common_results),
            "no_common_forbidden": all(
                result["satisfied"]
                for result in atom_results
                if result["layer"] == "forbidden"
            ),
            "evidence_complete": bool(common_results)
            and all(
                result["verdict"] in ("satisfied", "violated")
                and result["verdict_source"] in ("deterministic", "judge")
                for result in common_results
            ),
        },
        "srs_category_scores": category_scores,
        "srs_common_category_scores": common_category_scores,
        "arc": actor_f1,
        "rsc": road_spatial_f1,
        "iec_spec": event_f1,
        "atom_results": atom_results,
        "judge_bindings_sha256": sha256_bytes(
            canonical_json_bytes(
                [
                    {
                        "atom_id": atom_id,
                        "judge_response_id": binding["judge_response_id"],
                        "judge_response_record_sha256": binding[
                            "judge_response_record_sha256"
                        ],
                    }
                    for atom_id, binding in sorted(judge_verdicts.items())
                ]
            )
        ),
    }


def _uqh_bound_file_bytes(
    binding: Mapping[str, Any], label: str, *, require_nonempty: bool = False
) -> bytes:
    """Read and verify a claimed file binding instead of trusting its metadata."""

    if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256", "bytes"}:
        raise ValidationError("{} binding is malformed".format(label))
    path_value = binding.get("path")
    digest = binding.get("sha256")
    byte_count = binding.get("bytes")
    if (
        not isinstance(path_value, str)
        or not path_value.strip()
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or type(byte_count) is not int
        or byte_count < 0
    ):
        raise ValidationError("{} binding is malformed".format(label))
    path = Path(path_value)
    if not path.is_file():
        raise ValidationError("{} is not a regular readable file: {}".format(label, path))
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValidationError("{} is not readable: {}".format(label, path)) from exc
    if len(payload) != byte_count or sha256_bytes(payload) != digest:
        raise ValidationError("{} byte binding does not match the file".format(label))
    if require_nonempty and not payload:
        raise ValidationError("{} must not be empty".format(label))
    return payload


def _uqh_artifact_is_valid(record: Mapping[str, Any]) -> bool:
    """Verify the artifact bytes and apply the non-empty acceptance contract."""

    artifact = record.get("artifact")
    if artifact is None:
        return False
    payload = _uqh_bound_file_bytes(artifact, "UQH artifact")
    return bool(payload)


def _uqh_oracles_by_query(
    oracle_records: Iterable[Mapping[str, Any]],
) -> Dict[str, Mapping[str, Any]]:
    result = {}
    for oracle in oracle_records:
        query_id = oracle.get("query_id")
        if not isinstance(query_id, str) or not query_id or query_id in result:
            raise ValidationError("UQH oracle records require unique non-empty query IDs")
        expected_support = oracle.get("expected_support")
        if expected_support not in ("supported", "unsupported"):
            raise ValidationError("UQH oracle record missing valid expected_support")
        if expected_support == "unsupported":
            reasons = oracle.get("unsupported_reasons")
            acceptable = oracle.get("acceptable_response")
            if (
                not isinstance(reasons, list)
                or not reasons
                or any(not isinstance(reason, str) or not reason for reason in reasons)
                or not isinstance(acceptable, list)
                or not acceptable
            ):
                raise ValidationError(
                    "unsupported UQH oracle requires reasons and acceptable responses"
                )
        result[query_id] = oracle
    if not result:
        raise ValidationError("UQH requires oracle records")
    return result


_UQH_ASSESSMENT_BINDING_CONTRACT = "uqh_assessment_binding_v0.1"
_UQH_ASSESSOR_REQUEST_CONTRACT = "uqh_assessor_request_v0.1"
_UQH_ATTESTATION_CONTRACT = "uqh_assessor_attestation_v0.1"
_UQH_ASSESSMENT_CONTENT_FIELDS = (
    "query_id",
    "disposition",
    "verdict",
    "reason_codes",
    "assessment_source",
    "evidence",
    "rationale",
    "provenance",
)


def uqh_assessment_content_sha256(assessment: Mapping[str, Any]) -> str:
    """Hash every evaluator-controlled decision and provenance field."""

    return sha256_bytes(
        canonical_json_bytes(
            {field: assessment.get(field) for field in _UQH_ASSESSMENT_CONTENT_FIELDS}
        )
    )


def _uqh_assessment_binding_id(
    run_id: str,
    response_record_sha256: str,
    raw_response_envelope_sha256: str,
    oracle_record_sha256: str,
    assessment_content_sha256: str,
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "contract": _UQH_ASSESSMENT_BINDING_CONTRACT,
                "run_id": run_id,
                "response_record_sha256": response_record_sha256,
                "raw_response_envelope_sha256": raw_response_envelope_sha256,
                "oracle_record_sha256": oracle_record_sha256,
                "assessment_content_sha256": assessment_content_sha256,
            }
        )
    )


def uqh_assessment_id(assessment: Mapping[str, Any]) -> str:
    """Return the content-addressed ID required by the v0.1 assessment schema."""

    return _uqh_assessment_binding_id(
        str(assessment.get("run_id", "")),
        str(assessment.get("response_record_sha256", "")),
        str(assessment.get("raw_response_envelope_sha256", "")),
        str(assessment.get("oracle_record_sha256", "")),
        str(assessment.get("assessment_content_sha256", "")),
    )


def uqh_assessor_request_payload(
    assessment: Mapping[str, Any],
    response_record: Mapping[str, Any],
    oracle_record: Mapping[str, Any],
    response_stream: str,
    response_bytes: bytes,
) -> Dict[str, Any]:
    """Build the exact replayable input supplied to the independent assessor."""

    provenance = assessment.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    return {
        "contract": _UQH_ASSESSOR_REQUEST_CONTRACT,
        "run_id": assessment.get("run_id"),
        "query_id": assessment.get("query_id"),
        "disposition": assessment.get("disposition"),
        "assessment_source": assessment.get("assessment_source"),
        "assessor_id": provenance.get("assessor_id"),
        "assessor_version": provenance.get("assessor_version"),
        "assessor_registry_sha256": provenance.get("assessor_registry_sha256"),
        "assessment_config_sha256": provenance.get("assessment_config_sha256"),
        "assessor_source_sha256": provenance.get("assessor_source_sha256"),
        "response_record": dict(response_record),
        "oracle_record": dict(oracle_record),
        "selected_method_response": {
            "response_stream": response_stream,
            "bytes_sha256": sha256_bytes(response_bytes),
            "bytes_base64": base64.b64encode(response_bytes).decode("ascii"),
        },
    }


def uqh_assessor_request_sha256(
    assessment: Mapping[str, Any],
    response_record: Mapping[str, Any],
    oracle_record: Mapping[str, Any],
    response_stream: str,
    response_bytes: bytes,
) -> str:
    """Hash the exact replayable assessor request."""

    return sha256_bytes(
        canonical_json_bytes(
            uqh_assessor_request_payload(
                assessment,
                response_record,
                oracle_record,
                response_stream,
                response_bytes,
            )
        )
    )


def uqh_attestation_payload(assessment: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the canonical payload an independent assessor must sign."""

    provenance = assessment.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    raw_assessor_response = provenance.get("raw_assessor_response")
    raw_assessor_response_sha256 = (
        raw_assessor_response.get("sha256")
        if isinstance(raw_assessor_response, Mapping)
        else None
    )
    raw_assessor_request = provenance.get("raw_assessor_request")
    raw_assessor_request_sha256 = (
        raw_assessor_request.get("sha256")
        if isinstance(raw_assessor_request, Mapping)
        else None
    )
    return {
        "contract": _UQH_ATTESTATION_CONTRACT,
        "algorithm": "rsa_pss_sha256",
        "assessment_id": assessment.get("assessment_id"),
        "assessment_content_sha256": assessment.get("assessment_content_sha256"),
        "run_id": assessment.get("run_id"),
        "response_record_sha256": assessment.get("response_record_sha256"),
        "raw_response_envelope_sha256": assessment.get(
            "raw_response_envelope_sha256"
        ),
        "oracle_record_sha256": assessment.get("oracle_record_sha256"),
        "assessor_id": provenance.get("assessor_id"),
        "assessor_version": provenance.get("assessor_version"),
        "assessor_registry_sha256": provenance.get("assessor_registry_sha256"),
        "assessment_config_sha256": provenance.get("assessment_config_sha256"),
        "assessor_source_sha256": provenance.get("assessor_source_sha256"),
        "assessor_request_sha256": provenance.get("assessor_request_sha256"),
        "raw_assessor_request_sha256": raw_assessor_request_sha256,
        "raw_assessor_response_sha256": raw_assessor_response_sha256,
        "external_execution_id": provenance.get("external_execution_id"),
        "created_at_utc": provenance.get("created_at_utc"),
    }


def _uqh_validate_rsa_public_key(public_key_bytes: bytes) -> None:
    try:
        with tempfile.NamedTemporaryFile() as public_key_file:
            public_key_file.write(public_key_bytes)
            public_key_file.flush()
            completed = subprocess.run(
                [
                    "openssl",
                    "pkey",
                    "-pubin",
                    "-in",
                    public_key_file.name,
                    "-text",
                    "-noout",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError("UQH assessor public-key verification is unavailable") from exc
    key_description = completed.stdout.decode("utf-8", errors="replace")
    bit_match = re.search(r"Public-Key:\s*\((\d+) bit\)", key_description)
    if (
        completed.returncode != 0
        or bit_match is None
        or int(bit_match.group(1)) < 2048
    ):
        raise ValidationError("UQH assessor attestation public key is invalid")


def _uqh_verify_rsa_pss_attestation(
    public_key_bytes: bytes, signature_bytes: bytes, payload_bytes: bytes
) -> None:
    try:
        with tempfile.NamedTemporaryFile() as public_key_file, tempfile.NamedTemporaryFile() as signature_file:
            public_key_file.write(public_key_bytes)
            public_key_file.flush()
            signature_file.write(signature_bytes)
            signature_file.flush()
            completed = subprocess.run(
                [
                    "openssl",
                    "dgst",
                    "-sha256",
                    "-verify",
                    public_key_file.name,
                    "-signature",
                    signature_file.name,
                    "-sigopt",
                    "rsa_padding_mode:pss",
                    "-sigopt",
                    "rsa_pss_saltlen:-1",
                ],
                input=payload_bytes,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError("UQH assessor attestation verification is unavailable") from exc
    if completed.returncode != 0:
        raise ValidationError("UQH assessor attestation signature is invalid")


def _uqh_raw_method_response(
    record: Mapping[str, Any],
) -> Tuple[Mapping[str, Any], Dict[str, bytes]]:
    """Validate the immutable process envelope used as UQH source evidence."""

    raw_response = record.get("raw_method_response")
    if not isinstance(raw_response, Mapping):
        raise ValidationError("UQH response is missing raw_method_response")
    if set(raw_response) != {
        "stdout",
        "stderr",
        "exit_code",
        "timed_out",
        "envelope_sha256",
    }:
        raise ValidationError("UQH raw response envelope has unexpected fields")
    envelope = {}
    stream_bytes = {}
    for stream_name in ("stdout", "stderr"):
        stream = raw_response.get(stream_name)
        if not isinstance(stream, Mapping):
            raise ValidationError("UQH raw response requires stdout and stderr bindings")
        if set(stream) != {"path", "sha256", "bytes"}:
            raise ValidationError("UQH raw response stream binding has unexpected fields")
        path = stream.get("path")
        digest = stream.get("sha256")
        byte_count = stream.get("bytes")
        if (
            not isinstance(path, str)
            or not path.strip()
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or type(byte_count) is not int
            or byte_count < 0
        ):
            raise ValidationError("UQH raw response stream binding is malformed")
        envelope[stream_name] = {
            "path": path,
            "sha256": digest,
            "bytes": byte_count,
        }
        stream_bytes[stream_name] = _uqh_bound_file_bytes(
            stream, "UQH raw {}".format(stream_name)
        )
    exit_code = raw_response.get("exit_code")
    timed_out = raw_response.get("timed_out")
    if exit_code is not None and type(exit_code) is not int:
        raise ValidationError("UQH raw response exit_code must be an integer or null")
    if type(timed_out) is not bool:
        raise ValidationError("UQH raw response timed_out must be boolean")
    envelope.update({"exit_code": exit_code, "timed_out": timed_out})
    expected_digest = sha256_bytes(canonical_json_bytes(envelope))
    if raw_response.get("envelope_sha256") != expected_digest:
        raise ValidationError("UQH raw response envelope hash does not match its fields")
    return raw_response, stream_bytes


def _uqh_assessor_registry(
    registry: Mapping[str, Any],
) -> Tuple[Dict[str, Mapping[str, Any]], str, Dict[str, bytes]]:
    """Validate the frozen assessor identities and their live file bindings."""

    from .schema import validate_schema_instance

    validate_schema_instance(registry, "uqh_assessor_registry", context="UQH assessor registry")
    if registry.get("status") != "frozen":
        raise ValidationError("formal UQH requires a frozen assessor registry")
    if registry.get("controlled_degradation_credit") is not False:
        raise ValidationError("UQH v0.1 controlled degradation credit must remain disabled")
    from .generation import PRODUCER_ID

    assessors = {}
    public_keys = {}
    for assessor in registry.get("assessors", []):
        assessor_id = assessor["assessor_id"]
        if assessor_id in assessors:
            raise ValidationError("UQH assessor registry contains duplicate assessor IDs")
        _uqh_bound_file_bytes(
            assessor["assessment_config"],
            "UQH assessor {} config".format(assessor_id),
            require_nonempty=True,
        )
        _uqh_bound_file_bytes(
            assessor["assessor_source"],
            "UQH assessor {} source".format(assessor_id),
            require_nonempty=True,
        )
        if (
            assessor.get("attestation_algorithm") != "rsa_pss_sha256"
            or assessor.get("key_custody") != "external_to_method_runner"
            or assessor.get("independent_from_producer_ids") != [PRODUCER_ID]
        ):
            raise ValidationError("formal UQH assessor attestation policy is invalid")
        public_key_bytes = _uqh_bound_file_bytes(
            assessor.get("attestation_public_key"),
            "UQH assessor {} attestation public key".format(assessor_id),
            require_nonempty=True,
        )
        _uqh_validate_rsa_public_key(public_key_bytes)
        assessors[assessor_id] = assessor
        public_keys[assessor_id] = public_key_bytes
    return assessors, sha256_bytes(canonical_json_bytes(registry)), public_keys


def validate_uqh_response_producers(
    response_records: Iterable[Mapping[str, Any]],
) -> None:
    """Reject records that did not come from the immutable formal runner."""

    from .generation import PRODUCER_ID

    rogue_runs = sorted(
        str(record.get("run_id", ""))
        for record in response_records
        if record.get("provenance", {}).get("producer_id") != PRODUCER_ID
    )
    if rogue_runs:
        raise ValidationError(
            "formal UQH response producer must be {}; rogue runs={}".format(
                PRODUCER_ID, rogue_runs[:5]
            )
        )


def _uqh_method_handling_is_bound(
    record: Mapping[str, Any], raw_response: Mapping[str, Any]
) -> bool:
    handling = record.get("unsupported_handling")
    if not isinstance(handling, Mapping):
        return False
    if set(handling) != {"response_stream", "response_sha256"}:
        return False
    response_stream = handling.get("response_stream")
    if response_stream not in ("stdout", "stderr"):
        return False
    stream = raw_response.get(response_stream)
    return isinstance(stream, Mapping) and handling.get("response_sha256") == stream.get(
        "sha256"
    )


def _uqh_assessments_by_run(
    assessment_records: Iterable[Mapping[str, Any]],
    response_records: Sequence[Mapping[str, Any]],
    raw_responses: Mapping[str, Mapping[str, Any]],
    raw_streams: Mapping[str, Mapping[str, bytes]],
    assessor_registry: Mapping[str, Any],
    oracles: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Mapping[str, Any]]:
    """Validate independent assessments and their exact immutable-source bindings."""

    assessment_records = list(assessment_records)
    from .schema import validate_schema_records

    validate_schema_records(
        assessment_records,
        "uqh_assessment",
        context="UQH assessments",
    )
    registered_assessors, registry_sha256, public_keys = _uqh_assessor_registry(
        assessor_registry
    )
    responses_by_run = {}
    for record in response_records:
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id in responses_by_run:
            raise ValidationError("UQH responses require unique non-empty run IDs")
        responses_by_run[run_id] = record

    required_runs = {
        record["run_id"]
        for record in response_records
        if record.get("expected_support") == "unsupported"
        and record.get("disposition") in ("reject", "clarification")
    }
    result = {}
    assessment_ids = set()
    for assessment in assessment_records:
        run_id = assessment["run_id"]
        assessment_id = assessment["assessment_id"]
        if run_id in result or assessment_id in assessment_ids:
            raise ValidationError("UQH assessments require unique run and assessment IDs")
        record = responses_by_run.get(run_id)
        if record is None:
            raise ValidationError("UQH assessment references an absent response run")
        if (
            record.get("expected_support") != "unsupported"
            or record.get("disposition") not in ("reject", "clarification")
            or record.get("terminal_status") != "complete"
        ):
            raise ValidationError(
                "UQH assessment may target only a completed unsupported handling response"
            )
        raw_response = raw_responses[run_id]
        response_digest = sha256_bytes(canonical_json_bytes(record))
        envelope_digest = raw_response["envelope_sha256"]
        oracle_digest = sha256_bytes(canonical_json_bytes(oracles[record["query_id"]]))
        if (
            assessment["query_id"] != record.get("query_id")
            or assessment["disposition"] != record.get("disposition")
            or assessment["response_record_sha256"] != response_digest
            or assessment["raw_response_envelope_sha256"] != envelope_digest
            or assessment["oracle_record_sha256"] != oracle_digest
        ):
            raise ValidationError("UQH assessment source binding does not match the response")
        expected_id = _uqh_assessment_binding_id(
            run_id,
            response_digest,
            envelope_digest,
            oracle_digest,
            assessment["assessment_content_sha256"],
        )
        if assessment["assessment_content_sha256"] != uqh_assessment_content_sha256(
            assessment
        ):
            raise ValidationError("UQH assessment content hash does not match its decision")
        if assessment_id != expected_id:
            raise ValidationError("UQH assessment ID does not match its source and content")
        response_provenance = record.get("provenance")
        assessment_provenance = assessment.get("provenance")
        producer_id = (
            response_provenance.get("producer_id")
            if isinstance(response_provenance, Mapping)
            else None
        )
        assessor_id = assessment_provenance.get("assessor_id") if isinstance(
            assessment_provenance, Mapping
        ) else None
        registered = registered_assessors.get(assessor_id)
        if (
            not isinstance(producer_id, str)
            or not producer_id
            or not isinstance(assessor_id, str)
            or not assessor_id
            or assessor_id == producer_id
            or not isinstance(registered, Mapping)
            or producer_id not in registered.get("independent_from_producer_ids", [])
            or assessment.get("assessment_source") != registered.get("assessment_source")
            or assessment_provenance.get("assessor_version")
            != registered.get("assessor_version")
            or assessment_provenance.get("assessor_registry_sha256") != registry_sha256
            or assessment_provenance.get("assessment_config_sha256")
            != registered.get("assessment_config", {}).get("sha256")
            or assessment_provenance.get("assessor_source_sha256")
            != registered.get("assessor_source", {}).get("sha256")
        ):
            raise ValidationError(
                "UQH assessment is not produced by a frozen assessor independent of the response producer"
            )
        handling = record.get("unsupported_handling")
        selected_stream = (
            handling.get("response_stream") if isinstance(handling, Mapping) else None
        )
        if selected_stream not in ("stdout", "stderr"):
            raise ValidationError("UQH assessor request lacks a selected method response")
        selected_response_bytes = raw_streams[run_id][selected_stream]
        expected_request_payload = canonical_json_bytes(
            uqh_assessor_request_payload(
                assessment,
                record,
                oracles[record["query_id"]],
                selected_stream,
                selected_response_bytes,
            )
        )
        expected_request_sha256 = sha256_bytes(expected_request_payload)
        if assessment_provenance.get("assessor_request_sha256") != expected_request_sha256:
            raise ValidationError("UQH assessor request is not bound to its exact inputs")
        raw_assessor_request = _uqh_bound_file_bytes(
            assessment_provenance.get("raw_assessor_request"),
            "UQH raw assessor request",
            require_nonempty=True,
        )
        if raw_assessor_request != expected_request_payload:
            raise ValidationError("UQH raw assessor request differs from its exact inputs")
        raw_assessor_response = _uqh_bound_file_bytes(
            assessment_provenance.get("raw_assessor_response"),
            "UQH raw assessor response",
            require_nonempty=True,
        )
        raw_assessor_output = strict_json_object_bytes(
            raw_assessor_response, "UQH raw assessor response"
        )
        from .schema import validate_schema_instance

        validate_schema_instance(
            raw_assessor_output,
            "uqh_assessor_output",
            context="UQH raw assessor response",
        )
        expected_assessor_output = {
            "schema_version": assessment["schema_version"],
            "verdict": assessment["verdict"],
            "reason_codes": assessment["reason_codes"],
            "evidence": assessment["evidence"],
            "rationale": assessment["rationale"],
        }
        if canonical_json_bytes(raw_assessor_output) != canonical_json_bytes(
            expected_assessor_output
        ):
            raise ValidationError(
                "UQH assessment decision differs from the raw assessor response"
            )
        attestation = assessment.get("attestation")
        if (
            not isinstance(attestation, Mapping)
            or attestation.get("algorithm")
            != registered.get("attestation_algorithm")
        ):
            raise ValidationError("UQH assessment lacks the frozen attestation algorithm")
        attestation_payload = canonical_json_bytes(uqh_attestation_payload(assessment))
        if attestation.get("signed_payload_sha256") != sha256_bytes(
            attestation_payload
        ):
            raise ValidationError("UQH attestation payload hash is invalid")
        signature_bytes = _uqh_bound_file_bytes(
            attestation.get("signature"),
            "UQH assessor attestation signature",
            require_nonempty=True,
        )
        _uqh_verify_rsa_pss_attestation(
            public_keys[assessor_id], signature_bytes, attestation_payload
        )
        has_nonempty_selected_evidence = False
        for evidence in assessment["evidence"]:
            stream_name = evidence["response_stream"]
            payload = raw_streams[run_id][stream_name]
            start = evidence["byte_start"]
            end = evidence["byte_end"]
            if end < start or end > len(payload):
                raise ValidationError("UQH assessment evidence byte range is invalid")
            excerpt = payload[start:end]
            if sha256_bytes(excerpt) != evidence["bytes_sha256"]:
                raise ValidationError("UQH assessment evidence hash does not match raw output")
            if stream_name == selected_stream and excerpt:
                has_nonempty_selected_evidence = True
        if assessment.get("verdict") == "valid" and not has_nonempty_selected_evidence:
            raise ValidationError(
                "valid UQH assessment requires non-empty evidence from the selected response stream"
            )
        result[run_id] = assessment
        assessment_ids.add(assessment_id)
    if set(result) != required_runs:
        missing = sorted(required_runs - set(result))
        orphan = sorted(set(result) - required_runs)
        raise ValidationError(
            "UQH assessment pipeline is incomplete; missing={}, orphan={}".format(
                missing[:5], orphan[:5]
            )
        )
    return result


def _uqh_unsupported_handling_is_valid(
    record: Mapping[str, Any],
    oracle: Mapping[str, Any],
    raw_response: Mapping[str, Any],
    assessment: Optional[Mapping[str, Any]],
) -> bool:
    """Credit only independently assessed handling bound to immutable method output."""

    disposition = record.get("disposition")
    if disposition == "controlled_degradation":
        return False
    if (
        disposition not in ("reject", "clarification")
        or record.get("terminal_status") != "complete"
        or raw_response.get("timed_out") is not False
        or raw_response.get("exit_code") != 0
    ):
        return False
    oracle_disposition = {
        "reject": "reject",
        "clarification": "ask_for_clarification",
    }[disposition]
    if oracle_disposition not in oracle.get("acceptable_response", []):
        return False
    if not _uqh_method_handling_is_bound(record, raw_response):
        return False
    selected_stream = record["unsupported_handling"]["response_stream"]
    if raw_response[selected_stream].get("bytes", 0) <= 0:
        return False
    if (
        not isinstance(assessment, Mapping)
        or assessment.get("verdict") != "valid"
        or set(assessment.get("reason_codes", []))
        != set(oracle.get("unsupported_reasons", []))
    ):
        return False
    return record.get("artifact") is None


def compute_uqh(
    response_records: Iterable[Mapping[str, Any]],
    oracle_records: Iterable[Mapping[str, Any]],
    assessment_records: Iterable[Mapping[str, Any]],
    roster: Mapping[str, Any],
    assessor_registry: Mapping[str, Any],
    expected_config_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    response_records = list(response_records)
    oracle_records = list(oracle_records)
    assessment_records = list(assessment_records)
    from .roster import roster_entries, validate_records_against_roster
    from .schema import validate_schema_instance, validate_schema_records

    validate_schema_records(response_records, "response_record", context="UQH responses")
    validate_schema_records(oracle_records, "oracle_record", context="UQH oracle")
    validate_schema_instance(roster, "query_roster", context="UQH query roster")
    validate_records_against_roster(response_records, roster, require_confirmed=True)
    validate_uqh_response_producers(response_records)
    config_hashes = {record.get("config_sha256") for record in response_records}
    if len(config_hashes) != 1:
        raise ValidationError("UQH responses must share one frozen main configuration")
    config_sha256 = next(iter(config_hashes))
    if expected_config_sha256 is not None and config_sha256 != expected_config_sha256:
        raise ValidationError("UQH responses were not produced by the frozen method config")
    entries = roster_entries(roster)
    raw_responses = {}
    raw_streams = {}
    for record in response_records:
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id in raw_responses:
            raise ValidationError("UQH responses require unique non-empty run IDs")
        raw_responses[run_id], raw_streams[run_id] = _uqh_raw_method_response(record)
        if record.get("artifact") is not None:
            _uqh_artifact_is_valid(record)
    oracles = _uqh_oracles_by_query(oracle_records)
    if set(oracles) != set(entries):
        raise ValidationError("UQH oracle must exactly cover the frozen query roster")
    for query_id, oracle in oracles.items():
        entry = entries[query_id]
        if oracle.get("decision_status") != "confirmed" or any(
            oracle.get(field) != entry.get(field)
            for field in ("intent_group_id", "surface_style", "expected_support")
        ):
            raise ValidationError("UQH oracle differs from the frozen roster")
    assessments = _uqh_assessments_by_run(
        assessment_records,
        response_records,
        raw_responses,
        raw_streams,
        assessor_registry,
        oracles,
    )
    supported = []
    unsupported = []
    for record in response_records:
        query_id = record.get("query_id")
        oracle = oracles.get(query_id)
        if oracle is None:
            raise ValidationError("UQH response references a query absent from the oracle")
        expected = oracle["expected_support"]
        if record.get("expected_support") != expected:
            raise ValidationError("UQH response expected_support differs from the oracle")
        if expected == "supported":
            supported.append(record)
        elif expected == "unsupported":
            unsupported.append((record, oracle))
        else:
            raise ValidationError("response record missing valid expected_support")
    if not supported or not unsupported:
        raise ValidationError("UQH requires both supported and unsupported responses")
    acceptance = sum(
        record.get("disposition") == "generate"
        and record.get("terminal_status") == "complete"
        and raw_responses[record["run_id"]].get("timed_out") is False
        and raw_responses[record["run_id"]].get("exit_code") == 0
        and _uqh_artifact_is_valid(record)
        for record in supported
    ) / len(supported)
    valid_handling_count = sum(
        _uqh_unsupported_handling_is_valid(
            record,
            oracle,
            raw_responses[record["run_id"]],
            assessments.get(record["run_id"]),
        )
        for record, oracle in unsupported
    )
    handling = valid_handling_count / len(unsupported)
    normalized_responses = sorted(response_records, key=lambda value: value["run_id"])
    normalized_oracles = sorted(oracle_records, key=lambda value: value["query_id"])
    normalized_assessments = sorted(
        assessment_records, key=lambda value: value["assessment_id"]
    )
    return {
        "supported_acceptance": acceptance,
        "valid_unsupported_handling": handling,
        "uqh": harmonic_mean(acceptance, handling),
        "supported_count": len(supported),
        "unsupported_count": len(unsupported),
        "required_assessment_count": len(assessments),
        "assessment_count": len(assessment_records),
        "provenance": {
            "assessment_binding_contract": _UQH_ASSESSMENT_BINDING_CONTRACT,
            "response_records_sha256": sha256_bytes(
                canonical_json_bytes(normalized_responses)
            ),
            "oracle_records_sha256": sha256_bytes(
                canonical_json_bytes(normalized_oracles)
            ),
            "assessment_records_sha256": sha256_bytes(
                canonical_json_bytes(normalized_assessments)
            ),
            "roster_sha256": sha256_bytes(canonical_json_bytes(roster)),
            "assessor_registry_sha256": sha256_bytes(
                canonical_json_bytes(assessor_registry)
            ),
            "method_config_sha256": config_sha256,
            "assessor_request_contract": _UQH_ASSESSOR_REQUEST_CONTRACT,
            "attestation_contract": _UQH_ATTESTATION_CONTRACT,
        },
    }


def compute_rqs(score_records: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    score_records = list(score_records)
    validate_repetition_grid(score_records)
    groups = defaultdict(lambda: defaultdict(list))
    source_intents = set()
    for record in score_records:
        if record.get("expected_support") != "supported":
            continue
        style = record.get("surface_style")
        if style not in SURFACE_STYLES:
            raise ValidationError("invalid surface style in score record")
        srs = record.get("srs")
        srs = _bounded_metric(srs, "supported score srs")
        source_intents.add(record["intent_group_id"])
        cluster_id = record.get("statistical_intent_cluster_id", record["intent_group_id"])
        groups[cluster_id][style].append(srs)
    group_results = []
    for group_id, style_values in sorted(groups.items()):
        if set(style_values) != set(SURFACE_STYLES):
            raise ValidationError("{} lacks one or more specificity styles".format(group_id))
        style_means = {style: mean(style_values[style]) for style in SURFACE_STYLES}
        group_results.append(
            {
                "statistical_intent_cluster_id": group_id,
                "style_means": style_means,
                "specificity_floor": min(style_means.values()),
                "style_mean": mean(style_means.values()),
                "vague_gap": style_means["precise"] - style_means["vague"],
            }
        )
    if not group_results:
        raise ValidationError("RQS requires supported score records")
    return {
        "rqs": mean(result["specificity_floor"] for result in group_results),
        "rqs_mean": mean(result["style_mean"] for result in group_results),
        "vague_gap": mean(result["vague_gap"] for result in group_results),
        "aggregation_unit": "statistical_intent_cluster_id",
        "source_intent_count": len(source_intents),
        "statistical_cluster_count": len(group_results),
        "intent_count": len(group_results),
        "intent_results": group_results,
    }


def aggregate_semantic_metrics(score_records: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    score_records = list(score_records)
    validate_repetition_grid(score_records)
    supported = [record for record in score_records if record.get("expected_support") == "supported"]
    if not supported:
        raise ValidationError("no supported semantic score records")
    clusters = defaultdict(list)
    for record in supported:
        clusters[record.get("statistical_intent_cluster_id", record["intent_group_id"])].append(record)

    def cluster_macro(getter, label):
        return mean(
            mean(_bounded_metric(getter(record), label) for record in members)
            for members in clusters.values()
        )

    def diagnostic_macro(field):
        available = [
            record
            for record in supported
            if isinstance(record.get(field), Mapping)
            and record[field].get("availability") == "available"
        ]
        coverage = len(available) / len(supported)
        if len(available) != len(supported):
            return {
                "value": None,
                "availability": "pipeline_incomplete",
                "available_output_count": len(available),
                "output_count": len(supported),
                "coverage": coverage,
            }
        return {
            "value": cluster_macro(
                lambda record: record[field]["f1"], "{}.f1".format(field)
            ),
            "availability": "available",
            "available_output_count": len(available),
            "output_count": len(supported),
            "coverage": coverage,
        }

    arc = diagnostic_macro("arc")
    rsc = diagnostic_macro("rsc")
    iec_spec = diagnostic_macro("iec_spec")
    return {
        "srs": cluster_macro(lambda record: record["srs"], "srs"),
        "arc": arc["value"],
        "rsc": rsc["value"],
        "iec_spec": iec_spec["value"],
        "diagnostic_availability": {
            "arc": {key: value for key, value in arc.items() if key != "value"},
            "rsc": {key: value for key, value in rsc.items() if key != "value"},
            "iec_spec": {
                key: value for key, value in iec_spec.items() if key != "value"
            },
        },
        "ego_gate_rate": cluster_macro(
            lambda record: int(record["ego_gate_passed"]), "ego_gate_passed"
        ),
        "output_count": len(supported),
        "statistical_intent_cluster_count": len(clusters),
    }
