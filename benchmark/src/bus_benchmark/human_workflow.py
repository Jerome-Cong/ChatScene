"""Source-bound human review with one complete human gold per subject.

The active CARLA-first workflow accepts exactly one named reviewer submission
for each query, platform, calibration, or post-test subject. It never invents a
human verdict or turns a pending/uncertain template into benchmark gold.
"""

import copy
import json
from collections import Counter, defaultdict
from functools import lru_cache
from math import ceil, isfinite
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .errors import ValidationError
from .jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
)
from .oracle import REQUIRED_REVIEW_CHECKS
from .schema import validate_schema_instance, validate_schema_records
from .paths import asset_path, workspace_root

try:
    from jsonschema import Draft7Validator
except ImportError as exc:  # pragma: no cover - dependency failure is environmental
    Draft7Validator = None
    _JSONSCHEMA_IMPORT_ERROR = exc
else:
    _JSONSCHEMA_IMPORT_ERROR = None


SCHEMA_VERSION = "0.1"
QUERY_WORKFLOW = "query_oracle_review"
POST_TEST_WORKFLOW = "post_test_method_blind_audit"
PLATFORM_REVIEW_WORKFLOW = "platform_fixture_capability_review"
CALIBRATION_REVIEW_WORKFLOW = "judge_calibration_blind_review"
POST_TEST_SELECTION_ALGORITHM = (
    "method_stratified_terminal_status_sha256_v0.1"
)

HUMAN_SCHEMA_DIRECTORY = asset_path("schemas")
_REPOSITORY_ROOT = workspace_root()
_CARLA_STAGE_QUERY_SOURCES = (
    (
        _REPOSITORY_ROOT
        / "query_lib"
        / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl",
        _REPOSITORY_ROOT / "benchmark_artifacts" / "drafts" / "dev_oracle_draft.jsonl",
        48,
    ),
    (
        _REPOSITORY_ROOT
        / "query_lib"
        / "bus_ego_topdown_2d_query_library_v0_2.jsonl",
        _REPOSITORY_ROOT / "benchmark_artifacts" / "drafts" / "test_oracle_draft.jsonl",
        252,
    ),
)
HUMAN_SCHEMA_FILES = {
    "query_review_packet": "human_query_review_packet.schema.json",
    "query_review_submission": "human_query_review_submission.schema.json",
    "post_test_public": "human_post_test_public.schema.json",
    "post_test_private": "human_post_test_private.schema.json",
    "bound_review_packet": "human_bound_review_packet.schema.json",
    "bound_review_submission": "human_bound_review_submission.schema.json",
    "post_test_submission": "human_post_test_submission.schema.json",
    "post_test_report": "human_post_test_report.schema.json",
    "carla_stage_platform_report": "human_carla_stage_platform_report.schema.json",
    "carla_stage_calibration_roster": "human_carla_stage_calibration_roster.schema.json",
    "carla_stage_calibration_report": "human_carla_stage_calibration_report.schema.json",
    "query_gold_record": "human_query_gold_record.schema.json",
    "platform_gold_record": "human_platform_gold_record.schema.json",
    "calibration_gold_record": "human_calibration_gold_record.schema.json",
    "machine_draft_registry": "human_machine_draft_registry.schema.json",
    "machine_draft_summary": "human_machine_draft_summary.schema.json",
}

_BLIND_IDENTITY_KEYS = {
    "method_id",
    "run_id",
    "platform",
    "producer_id",
    "source_id",
    "model_id",
    "runner_id",
    "provider_request_id",
    "provider_response_id",
    "config_sha256",
    "path",
}


def _json_copy(value: Any, label: str) -> Any:
    try:
        return json.loads(canonical_json_bytes(value).decode("utf-8"))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("{} must be finite JSON-compatible data".format(label)) from exc


def _json_sha256(value: Any) -> str:
    try:
        return sha256_bytes(canonical_json_bytes(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError("cannot hash non-JSON workflow data") from exc


def _assert_canonical_review_bundle(
    supplied: Mapping[str, Any], expected: Mapping[str, Any], label: str
) -> None:
    """Reject reviewer-visible content not rematerialized from current sources."""

    try:
        supplied_bytes = canonical_json_bytes(supplied)
        expected_bytes = canonical_json_bytes(expected)
    except (TypeError, ValueError) as exc:
        raise ValidationError("{} review bundle is not canonical JSON".format(label)) from exc
    if supplied_bytes != expected_bytes:
        raise ValidationError(
            "{} review bundle differs from canonical current sources".format(label)
        )


@lru_cache(maxsize=None)
def _load_human_schema(schema_name: str) -> Mapping[str, Any]:
    filename = HUMAN_SCHEMA_FILES.get(schema_name)
    if filename is None:
        raise ValidationError("unknown human-workflow schema {!r}".format(schema_name))
    try:
        with (HUMAN_SCHEMA_DIRECTORY / filename).open(
            "r", encoding="utf-8"
        ) as stream:
            schema = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "cannot load human-workflow schema {}: {}".format(filename, exc)
        ) from exc
    if not isinstance(schema, dict):
        raise ValidationError("human-workflow schema {} is not an object".format(filename))
    return schema


def _validate_machine_draft_count_contract(
    value: Mapping[str, Any], schema_name: str
) -> None:
    """Cross-check dynamic v0.2 workload counts after structural validation."""

    if schema_name not in {"machine_draft_registry", "machine_draft_summary"}:
        return
    expected_agent_path = (
        _REPOSITORY_ROOT
        / "benchmark_artifacts"
        / "drafts"
        / "query_agent_semantic_review_v0_2.json"
    ).resolve()
    declared_agent_path = Path(
        value["query_agent_semantic_review"]["artifact"]["path"]
    )
    if not declared_agent_path.is_absolute():
        declared_agent_path = _REPOSITORY_ROOT / declared_agent_path
    if declared_agent_path.resolve() != expected_agent_path:
        raise ValidationError(
            "machine draft document binds a non-canonical Agent review artifact"
        )
    agent_binding = value["query_agent_semantic_review"]["artifact"]
    if (
        not expected_agent_path.is_file()
        or (
            "bytes" in agent_binding
            and agent_binding["bytes"] != expected_agent_path.stat().st_size
        )
        or agent_binding.get("sha256") != sha256_file(expected_agent_path)
    ):
        raise ValidationError(
            "machine draft document differs from the live Agent review artifact"
        )

    # Internal arithmetic is not a sufficient trust boundary: an attacker can
    # change every total coherently and recompute the document hash. Rebuild the
    # authoritative counts from the current v0.2 Agent artifact and its bound
    # query/oracle sources on every registry/summary validation.
    from .agent_query_review import (
        DEFAULT_AGENT_REVIEW_FRAGMENT_PATHS,
        validate_agent_query_review_draft,
    )

    live_agent_review = read_json(expected_agent_path)
    query_sources = (
        (
            "development",
            _CARLA_STAGE_QUERY_SOURCES[0][0],
            _CARLA_STAGE_QUERY_SOURCES[0][1],
        ),
        (
            "test",
            _CARLA_STAGE_QUERY_SOURCES[1][0],
            _CARLA_STAGE_QUERY_SOURCES[1][1],
        ),
    )
    validate_agent_query_review_draft(
        live_agent_review,
        query_sources=query_sources,
        fragment_paths=DEFAULT_AGENT_REVIEW_FRAGMENT_PATHS,
    )
    live_summary = live_agent_review["summary"]
    split_contract = {}
    for split, (_library_path, oracle_path, expected_subjects) in zip(
        ("development", "test"), _CARLA_STAGE_QUERY_SOURCES
    ):
        oracle_records = read_jsonl(oracle_path)
        if len(oracle_records) != expected_subjects:
            raise ValidationError(
                "live v0.2 oracle subject count differs from its source contract"
            )
        atom_count = sum(len(record["atoms"]) for record in oracle_records)
        split_contract[split] = {
            "subjects": expected_subjects,
            "atoms": atom_count,
            "explicit": atom_count
            + expected_subjects * (len(REQUIRED_REVIEW_CHECKS) + 1),
        }
    live_agent_contract = {
        "reviewed_intent_groups": live_summary["intent_groups"],
        "reviewed_subjects": live_summary["subjects"],
        "draft_decision_entries": live_summary["explicit_decision_entries"],
    }
    live_query_contract = {
        "atoms": live_summary["atom_decisions"],
        "required_checks": live_summary["required_check_decisions"],
        "cpd": live_summary["cpd_decisions"],
        "explicit": live_summary["explicit_decision_entries"],
    }

    if schema_name == "machine_draft_registry":
        workload = value["workload"]
        packets = value["packets"]
        by_name = {packet["packet_name"]: packet for packet in packets}
        expected_names = {
            "dev_query_review",
            "test_query_review",
            "carla_platform_review",
            "carla_calibration_review",
        }
        if len(by_name) != len(packets) or set(by_name) != expected_names:
            raise ValidationError(
                "machine draft registry must contain four unique canonical packets"
            )
        expected_packet_contract = {
            "dev_query_review": (
                split_contract["development"]["subjects"],
                split_contract["development"]["explicit"],
            ),
            "test_query_review": (
                split_contract["test"]["subjects"],
                split_contract["test"]["explicit"],
            ),
            "carla_platform_review": (52, 52),
            "carla_calibration_review": (90, 90),
        }
        for name, (subjects, explicit) in expected_packet_contract.items():
            packet = by_name[name]
            if (
                packet["expected_subjects"] != subjects
                or packet["expected_explicit_verdict_entries"] != explicit
            ):
                raise ValidationError(
                    "machine draft registry differs from the live Agent/oracle contract"
                )
        for name in ("dev_query_review", "test_query_review"):
            packet = by_name[name]
            if (
                packet["packet_ready_subjects"] != packet["expected_subjects"]
                or packet["packet_ready_explicit_verdict_entries"]
                != packet["expected_explicit_verdict_entries"]
            ):
                raise ValidationError(
                    "machine draft registry query packets must be fully source-ready"
                )
        query_expected = sum(
            by_name[name]["expected_explicit_verdict_entries"]
            for name in ("dev_query_review", "test_query_review")
        )
        expected_explicit = sum(
            packet["expected_explicit_verdict_entries"] for packet in packets
        )
        ready_explicit = sum(
            packet["packet_ready_explicit_verdict_entries"] for packet in packets
        )
        expected_subjects = sum(packet["expected_subjects"] for packet in packets)
        ready_subjects = sum(packet["packet_ready_subjects"] for packet in packets)
        if (
            workload["query_atom_verdict_entries"]
            + workload["query_required_check_verdict_entries"]
            + workload["query_cpd_verdict_entries"]
            != query_expected
            or workload["query_machine_recommendation_entries"] != query_expected
            or workload["expected_explicit_verdict_entries"] != expected_explicit
            or workload["packet_ready_explicit_verdict_entries"] != ready_explicit
            or workload["blocked_explicit_verdict_entries"]
            != expected_explicit - ready_explicit
            or workload["expected_subjects"] != expected_subjects
            or workload["packet_ready_subjects"] != ready_subjects
            or workload["blocked_subjects"] != expected_subjects - ready_subjects
            or value["query_agent_semantic_review"]["draft_decision_entries"]
            != query_expected
            or {
                key: value["query_agent_semantic_review"][key]
                for key in live_agent_contract
            }
            != live_agent_contract
            or workload["query_atom_verdict_entries"] != live_query_contract["atoms"]
            or workload["query_required_check_verdict_entries"]
            != live_query_contract["required_checks"]
            or workload["query_cpd_verdict_entries"] != live_query_contract["cpd"]
            or query_expected != live_query_contract["explicit"]
        ):
            raise ValidationError(
                "machine draft registry workload counts are not internally exact"
            )
        return

    if schema_name != "machine_draft_summary":
        return
    workload = value["workload"]
    entries = value["entries"]
    by_id = {entry["draft_id"]: entry for entry in entries}
    expected_ids = {
        "dev_query_oracle",
        "test_query_oracle",
        "carla_platform_fixture_capability",
        "carla_judge_calibration",
        "metadrive_token_only_platform_infrastructure",
        "final_cross_platform_protocol",
    }
    if len(by_id) != len(entries) or set(by_id) != expected_ids:
        raise ValidationError(
            "machine draft summary must contain six unique canonical entries"
        )
    expected_library_paths = {
        "dev_query_oracle": (
            _REPOSITORY_ROOT
            / "query_lib"
            / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
        ).resolve(),
        "test_query_oracle": (
            _REPOSITORY_ROOT
            / "query_lib"
            / "bus_ego_topdown_2d_query_library_v0_2.jsonl"
        ).resolve(),
    }
    for entry_id, expected_library_path in expected_library_paths.items():
        declared_paths = set()
        for binding in by_id[entry_id].get("source_bindings", []):
            declared = Path(binding["path"])
            if not declared.is_absolute():
                declared = _REPOSITORY_ROOT / declared
            declared_paths.add(declared.resolve())
        if expected_library_path not in declared_paths:
            raise ValidationError(
                "machine draft summary query entry does not bind the v0.2 library"
            )
    query_entries = by_id["dev_query_oracle"]["explicit_verdict_entries"] + by_id[
        "test_query_oracle"
    ]["explicit_verdict_entries"]
    platform_entries = by_id["carla_platform_fixture_capability"][
        "explicit_verdict_entries"
    ]
    calibration_entries = by_id["carla_judge_calibration"][
        "explicit_verdict_entries"
    ]
    query_subjects = by_id["dev_query_oracle"]["subject_records"] + by_id[
        "test_query_oracle"
    ]["subject_records"]
    blocked_subjects = by_id["carla_platform_fixture_capability"][
        "subject_records"
    ] + by_id["carla_judge_calibration"]["subject_records"]
    agent_review = value["query_agent_semantic_review"]
    expected_summary_entries = {
        "dev_query_oracle": (
            split_contract["development"]["subjects"],
            split_contract["development"]["explicit"],
        ),
        "test_query_oracle": (
            split_contract["test"]["subjects"],
            split_contract["test"]["explicit"],
        ),
        "carla_platform_fixture_capability": (52, 52),
        "carla_judge_calibration": (90, 90),
    }
    for entry_id, (subjects, explicit) in expected_summary_entries.items():
        entry = by_id[entry_id]
        if (
            entry["subject_records"] != subjects
            or entry["explicit_verdict_entries"] != explicit
        ):
            raise ValidationError(
                "machine draft summary differs from the live Agent/oracle contract"
            )
    expected_agent_summary = dict(live_agent_contract)
    expected_agent_summary.update(
        {
            "intent_semantic_finding_entries": live_summary["basis_counts"][
                "intent_semantic_finding"
            ],
            "source_projection_default_entries": live_summary["basis_counts"][
                "source_projection_default"
            ],
            "overall_disposition_counts": live_summary[
                "overall_disposition_counts"
            ],
            "recommendation_counts": live_summary["recommendation_counts"],
        }
    )
    if (
        workload["query_atom_verdict_entries"]
        + workload["query_required_check_verdict_entries"]
        + workload["query_cpd_verdict_entries"]
        != query_entries
        or workload["query_machine_recommendation_entries"] != query_entries
        or workload["currently_packet_generatable_explicit_verdict_entries"]
        != query_entries
        or workload["expected_explicit_verdict_entries"]
        != query_entries + platform_entries + calibration_entries
        or workload["carla_platform_verdict_entries"] != platform_entries
        or workload["carla_calibration_verdict_entries"] != calibration_entries
        or workload["currently_packet_generatable_after_reviewer_assignment"]
        != query_subjects
        or workload["currently_blocked_subject_records"] != blocked_subjects
        or workload["expected_subject_records"] != query_subjects + blocked_subjects
        or agent_review["draft_decision_entries"] != query_entries
        or sum(agent_review["recommendation_counts"].values()) != query_entries
        or agent_review["intent_semantic_finding_entries"]
        + agent_review["source_projection_default_entries"]
        != query_entries
        or sum(agent_review["overall_disposition_counts"].values())
        != agent_review["reviewed_subjects"]
        or {
            key: agent_review[key] for key in expected_agent_summary
        }
        != expected_agent_summary
        or workload["query_atom_verdict_entries"] != live_query_contract["atoms"]
        or workload["query_required_check_verdict_entries"]
        != live_query_contract["required_checks"]
        or workload["query_cpd_verdict_entries"] != live_query_contract["cpd"]
        or query_entries != live_query_contract["explicit"]
    ):
        raise ValidationError(
            "machine draft summary workload counts are not internally exact"
        )


def validate_human_document(value: Any, schema_name: str) -> Any:
    """Validate one workflow document against a local ``human_*`` schema."""

    if schema_name == "query_gold_record":
        validate_schema_instance(
            value, "human_query_gold", context="query human-gold record"
        )
        return value
    if _JSONSCHEMA_IMPORT_ERROR is not None:
        raise ValidationError(
            "human-workflow schema validation requires jsonschema: {}".format(
                _JSONSCHEMA_IMPORT_ERROR
            )
        )
    schema = _load_human_schema(schema_name)
    try:
        Draft7Validator.check_schema(schema)
    except Exception as exc:
        raise ValidationError(
            "invalid human-workflow schema {}: {}".format(schema_name, exc)
        ) from exc
    errors = sorted(
        Draft7Validator(schema).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        details = []
        for error in errors[:5]:
            path = "$" + "".join("[{}]".format(repr(part)) for part in error.absolute_path)
            details.append("{}: {}".format(path, error.message))
        raise ValidationError(
            "human document violates {} schema: {}".format(
                schema_name, "; ".join(details)
            )
        )
    _validate_machine_draft_count_contract(value, schema_name)
    stage_record_contract = {
        "carla_stage_platform_report": (
            "platform_gold_record",
            "subject_id",
            "subject_count",
        ),
        "carla_stage_calibration_report": (
            "calibration_gold_record",
            "item_id",
            "item_count",
        ),
    }.get(schema_name)
    if stage_record_contract is not None:
        record_schema, subject_field, count_field = stage_record_contract
        records = value["human_gold_records"]
        declared_count = value["source_binding"].get(count_field)
        if type(declared_count) is not int or declared_count != len(records):
            raise ValidationError(
                "{} declares a stale human-gold count".format(schema_name)
            )
        subject_ids = set()
        gold_ids = set()
        for record in records:
            validate_human_document(record, record_schema)
            subject_id = record[subject_field]
            gold_id = record["gold_record_id"]
            expected_gold_id = _json_sha256(
                {
                    key: child
                    for key, child in record.items()
                    if key != "gold_record_id"
                }
            )
            if (
                gold_id != expected_gold_id
                or subject_id in subject_ids
                or gold_id in gold_ids
            ):
                raise ValidationError(
                    "{} repeats or mutates a human-gold record".format(schema_name)
                )
            subject_ids.add(subject_id)
            gold_ids.add(gold_id)
    return value


def _materialize_jsonl_source(
    source: Any, label: str
) -> Tuple[List[Dict[str, Any]], str, str]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        records = read_jsonl(path)
        return _json_copy(records, label), sha256_file(path), "file_bytes"
    if isinstance(source, Mapping) or isinstance(source, (bytes, bytearray)):
        raise ValidationError("{} must be a JSONL path or iterable of records".format(label))
    try:
        records = list(source)
    except TypeError as exc:
        raise ValidationError("{} is not iterable".format(label)) from exc
    if not records or any(not isinstance(record, Mapping) for record in records):
        raise ValidationError("{} must contain JSON objects".format(label))
    normalized = _json_copy(records, label)
    return normalized, _json_sha256(normalized), "canonical_records"


def _materialize_json_source(
    source: Any, label: str
) -> Tuple[Dict[str, Any], str, str]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        value = read_json(path)
        if not isinstance(value, dict):
            raise ValidationError("{} must contain a JSON object".format(label))
        return _json_copy(value, label), sha256_file(path), "file_bytes"
    if not isinstance(source, Mapping):
        raise ValidationError("{} must be a JSON path or object".format(label))
    normalized = _json_copy(source, label)
    return normalized, _json_sha256(normalized), "canonical_object"


def _query_source_binding(
    library_sha256: str,
    library_scope: str,
    oracle_sha256: str,
    oracle_scope: str,
    count: int,
) -> Dict[str, Any]:
    return {
        "library_sha256": library_sha256,
        "library_hash_scope": library_scope,
        "oracle_sha256": oracle_sha256,
        "oracle_hash_scope": oracle_scope,
        "record_count": count,
    }


def _query_response_template(oracle: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "required_check_decisions": {
            check: {"verdict": None, "reason": ""}
            for check in REQUIRED_REVIEW_CHECKS
        },
        "atom_decisions": [
            {
                "atom_id": atom["atom_id"],
                "verdict": None,
                "reason": "",
                "replacement_atoms": [],
            }
            for atom in oracle["atoms"]
        ],
        "cpd_decision": {
            "verdict": None,
            "reason": "",
            "replacement_policy": None,
        },
        "proposed_oracle": None,
        "notes": "",
    }


def _query_machine_recommendation(oracle: Mapping[str, Any]) -> Dict[str, Any]:
    """Return an exact mechanical retention proposal without populating a submission.

    This proposal makes every form field concrete while preserving the current
    oracle byte-for-byte.  It is not the independent semantic review: that is a
    separate source-bound Agent artifact keyed by query ID.  Neither artifact
    has reviewer identity or completion state, and neither creates gold.
    """

    response = {
        "required_check_decisions": {
            check: {
                "verdict": "accept",
                "reason": (
                    "Mechanical retention proposal: retain the current {} "
                    "projection; consult the Agent semantic review and verify it "
                    "independently."
                ).format(check),
            }
            for check in REQUIRED_REVIEW_CHECKS
        },
        "atom_decisions": [
            {
                "atom_id": atom["atom_id"],
                "verdict": "accept",
                "reason": (
                    "Mechanical retention proposal: {} Consult the Agent semantic "
                    "review and verify independently."
                ).format(
                    atom.get("notes")
                    or "retain this source-derived atom pending review."
                ),
                "replacement_atoms": [],
            }
            for atom in oracle["atoms"]
        ],
        "cpd_decision": {
            "verdict": "accept",
            "reason": (
                "Mechanical retention proposal: retain the current CPD policy; "
                "consult the Agent semantic review and verify independently."
            ),
            "replacement_policy": None,
        },
        "proposed_oracle": _json_copy(oracle, "machine-recommended oracle"),
        "notes": (
            "Machine-only, non-gold mechanical form proposal. Semantic findings "
            "are in the source-bound Agent query-review artifact. The assigned "
            "reviewer must inspect both and submit an independent complete response."
        ),
    }
    return {
        "recommendation_status": "machine_only_non_gold",
        "machine_generated": True,
        "human_gold": False,
        "human_submission_populated": False,
        "human_review_required": True,
        "explicit_verdict_entries": (
            len(response["required_check_decisions"])
            + len(response["atom_decisions"])
            + 1
        ),
        "recommended_response": response,
    }


def _review_packet_id(packet: Mapping[str, Any]) -> str:
    core = {
        key: packet[key]
        for key in (
            "schema_version",
            "workflow_type",
            "reviewer_id",
            "decision_status",
            "source_binding",
            "tasks",
        )
    }
    return _json_sha256(core)


def _make_query_packet(
    reviewer_id: str,
    source_binding: Mapping[str, Any],
    pairs: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> Dict[str, Any]:
    tasks = []
    for query, oracle in pairs:
        subject = {
            "query_record": _json_copy(query, "query record"),
            "oracle_draft": _json_copy(oracle, "oracle draft"),
        }
        subject_sha256 = _json_sha256(subject)
        task_id = _json_sha256(
            {
                "workflow_type": QUERY_WORKFLOW,
                "reviewer_id": reviewer_id,
                "source_binding": source_binding,
                "subject_id": query["query_id"],
                "subject_sha256": subject_sha256,
            }
        )
        tasks.append(
            {
                "task_id": task_id,
                "subject_id": query["query_id"],
                "subject_sha256": subject_sha256,
                "source_query_sha256": _json_sha256(query),
                "source_oracle_sha256": _json_sha256(oracle),
                "query_text": query["query_text"],
                "query_record": subject["query_record"],
                "oracle_draft": subject["oracle_draft"],
                "response_template": _query_response_template(oracle),
                "machine_recommendation": _query_machine_recommendation(oracle),
            }
        )
    packet = {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": QUERY_WORKFLOW,
        "reviewer_id": reviewer_id,
        "decision_status": "pending",
        "source_binding": _json_copy(source_binding, "source binding"),
        "tasks": tasks,
    }
    packet["packet_id"] = _review_packet_id(packet)
    packet["submission_template"] = {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": QUERY_WORKFLOW,
        "packet_id": packet["packet_id"],
        "reviewer_id": reviewer_id,
        "source_binding": _json_copy(source_binding, "source binding"),
        "review_assignment_policy": "single_reviewer_final",
        "submission_status": "pending",
        "responses": [
            {
                "task_id": task["task_id"],
                "subject_id": task["subject_id"],
                "subject_sha256": task["subject_sha256"],
                "response": None,
            }
            for task in tasks
        ],
    }
    validate_human_document(packet, "query_review_packet")
    return packet


def export_query_review_bundle(
    library_source: Any,
    oracle_source: Any,
    *,
    reviewer_id: str,
) -> Dict[str, Any]:
    """Export one source-bound query packet for one final human reviewer."""

    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise ValidationError("reviewer ID must be a non-empty string")
    library, library_sha256, library_scope = _materialize_jsonl_source(
        library_source, "query library"
    )
    oracle, oracle_sha256, oracle_scope = _materialize_jsonl_source(
        oracle_source, "oracle draft"
    )
    library_by_id = {}
    for record in library:
        query_id = record.get("query_id")
        if (
            not isinstance(query_id, str)
            or not query_id
            or query_id in library_by_id
            or not isinstance(record.get("query_text"), str)
            or not record["query_text"]
        ):
            raise ValidationError("query library IDs/text must be unique and non-empty")
        library_by_id[query_id] = record
    oracle_by_id = {}
    for record in oracle:
        validate_schema_instance(record, "oracle_record", context="oracle draft")
        query_id = record["query_id"]
        if query_id in oracle_by_id or record.get("decision_status") != "draft":
            raise ValidationError("oracle input must contain unique draft records")
        if any(atom.get("decision_status") != "draft" for atom in record["atoms"]):
            raise ValidationError("oracle task export requires draft atoms")
        if record["cpd_policy"].get("decision_status") != "draft":
            raise ValidationError("oracle task export requires a draft CPD policy")
        oracle_by_id[query_id] = record
    if set(library_by_id) != set(oracle_by_id):
        raise ValidationError("query library and oracle draft query IDs differ")
    pairs = []
    for query_id in sorted(library_by_id):
        query = library_by_id[query_id]
        oracle_record = oracle_by_id[query_id]
        for field in ("intent_group_id", "surface_style"):
            if query.get(field) != oracle_record.get(field):
                raise ValidationError("query/oracle {} mismatch for {}".format(field, query_id))
        pairs.append((query, oracle_record))
    binding = _query_source_binding(
        library_sha256,
        library_scope,
        oracle_sha256,
        oracle_scope,
        len(pairs),
    )
    packet = _make_query_packet(reviewer_id, binding, pairs)
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": QUERY_WORKFLOW,
        "source_binding": binding,
        "review_policy": "one_complete_human_gold_per_subject",
        "reviewer_packet": packet,
    }


def _assert_current_query_sources(
    binding: Mapping[str, Any], library_source: Any, oracle_source: Any
) -> None:
    _, library_sha256, library_scope = _materialize_jsonl_source(
        library_source, "current query library"
    )
    _, oracle_sha256, oracle_scope = _materialize_jsonl_source(
        oracle_source, "current oracle draft"
    )
    if (
        binding.get("library_sha256") != library_sha256
        or binding.get("library_hash_scope") != library_scope
        or binding.get("oracle_sha256") != oracle_sha256
        or binding.get("oracle_hash_scope") != oracle_scope
    ):
        raise ValidationError("current query/oracle sources differ from the task packet")


def validate_query_review_submission(
    packet: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    library_source: Any = None,
    oracle_source: Any = None,
) -> Dict[str, Any]:
    """Validate one completed query submission against its assigned packet."""

    validate_human_document(packet, "query_review_packet")
    validate_human_document(submission, "query_review_submission")
    if packet.get("packet_id") != _review_packet_id(packet):
        raise ValidationError("query review packet ID does not match its content")
    for field in (
        "workflow_type",
        "packet_id",
        "reviewer_id",
        "source_binding",
    ):
        if submission.get(field) != packet.get(field):
            raise ValidationError("query submission {} differs from its packet".format(field))
    if (library_source is None) != (oracle_source is None):
        raise ValidationError("both current query and oracle sources are required together")
    if library_source is not None:
        _assert_current_query_sources(
            packet["source_binding"], library_source, oracle_source
        )
    tasks = {task["task_id"]: task for task in packet["tasks"]}
    responses = {response["task_id"]: response for response in submission["responses"]}
    if len(responses) != len(submission["responses"]) or set(tasks) != set(responses):
        raise ValidationError("query submission must cover every task exactly once")
    for task_id, task in tasks.items():
        item = responses[task_id]
        if (
            item.get("subject_id") != task["subject_id"]
            or item.get("subject_sha256") != task["subject_sha256"]
        ):
            raise ValidationError("query submission subject binding mismatch")
        validate_query_review_task_response(
            task,
            item["response"],
            reviewer_id=packet["reviewer_id"],
        )
    return _json_copy(submission, "query review submission")


def _bound_response_template(workflow_type: str) -> Dict[str, Any]:
    if workflow_type == PLATFORM_REVIEW_WORKFLOW:
        return {"verdict": None, "reason": ""}
    if workflow_type == CALIBRATION_REVIEW_WORKFLOW:
        return {"verdict": None, "evidence": [], "rationale": ""}
    raise ValidationError("unknown bound human-review workflow {!r}".format(workflow_type))


def _make_bound_review_packet(
    workflow_type: str,
    reviewer_id: str,
    source_binding: Mapping[str, Any],
    subjects: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    tasks = []
    seen_subjects = set()
    for item in subjects:
        subject_id = item.get("subject_id")
        subject = item.get("subject")
        if (
            not isinstance(subject_id, str)
            or not subject_id
            or subject_id in seen_subjects
            or not isinstance(subject, Mapping)
            or not subject
        ):
            raise ValidationError("bound review subjects require unique IDs and content")
        seen_subjects.add(subject_id)
        normalized_subject = _json_copy(subject, "bound review subject")
        subject_sha256 = _json_sha256(normalized_subject)
        task_id = _json_sha256(
            {
                "workflow_type": workflow_type,
                "reviewer_id": reviewer_id,
                "source_binding": source_binding,
                "subject_id": subject_id,
                "subject_sha256": subject_sha256,
            }
        )
        tasks.append(
            {
                "task_id": task_id,
                "subject_id": subject_id,
                "subject_sha256": subject_sha256,
                "subject": normalized_subject,
                "response_template": _bound_response_template(workflow_type),
            }
        )
    if not tasks:
        raise ValidationError("bound review packet cannot be empty")
    tasks.sort(key=lambda task: task["subject_id"])
    packet = {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": workflow_type,
        "reviewer_id": reviewer_id,
        "decision_status": "pending",
        "source_binding": _json_copy(source_binding, "bound source binding"),
        "tasks": tasks,
    }
    packet["packet_id"] = _review_packet_id(packet)
    packet["submission_template"] = {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": workflow_type,
        "packet_id": packet["packet_id"],
        "reviewer_id": reviewer_id,
        "source_binding": copy.deepcopy(packet["source_binding"]),
        "review_assignment_policy": "single_reviewer_final",
        "submission_status": "pending",
        "responses": [
            {
                "task_id": task["task_id"],
                "subject_id": task["subject_id"],
                "subject_sha256": task["subject_sha256"],
                "response": None,
            }
            for task in tasks
        ],
    }
    validate_human_document(packet, "bound_review_packet")
    return packet


def _export_single_bound_review_bundle(
    workflow_type: str,
    source_binding: Mapping[str, Any],
    subjects: Sequence[Mapping[str, Any]],
    *,
    reviewer_id: str,
) -> Dict[str, Any]:
    """Export the sole production packet under the one-gold policy."""

    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise ValidationError("reviewer ID must be a non-empty string")
    binding = _json_copy(source_binding, "single-review source binding")
    binding["review_policy"] = "one_complete_human_gold_per_subject"
    packet = _make_bound_review_packet(
        workflow_type,
        reviewer_id,
        binding,
        subjects,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": workflow_type,
        "source_binding": binding,
        "review_policy": "one_complete_human_gold_per_subject",
        "reviewer_packet": packet,
    }


def _validate_bound_response(workflow_type: str, response: Mapping[str, Any]) -> None:
    if workflow_type == PLATFORM_REVIEW_WORKFLOW:
        if (
            set(response) != {"verdict", "reason"}
            or response.get("verdict") not in ("approve", "reject")
            or not isinstance(response.get("reason"), str)
            or not response["reason"]
        ):
            raise ValidationError("bound approval response is malformed")
        return
    if workflow_type == CALIBRATION_REVIEW_WORKFLOW:
        evidence = response.get("evidence")
        if (
            set(response) != {"verdict", "evidence", "rationale"}
            or response.get("verdict") not in ("satisfied", "violated", "unknown")
            or not isinstance(evidence, list)
            or not evidence
            or any(not isinstance(value, str) or not value for value in evidence)
            or not isinstance(response.get("rationale"), str)
            or not response["rationale"]
        ):
            raise ValidationError("blind calibration response is malformed")
        return
    raise ValidationError("unknown bound human-review workflow")


def validate_bound_review_submission(
    packet: Mapping[str, Any], submission: Mapping[str, Any]
) -> Dict[str, Any]:
    validate_human_document(packet, "bound_review_packet")
    validate_human_document(submission, "bound_review_submission")
    if packet.get("packet_id") != _review_packet_id(packet):
        raise ValidationError("bound review packet ID does not match its content")
    for field in (
        "workflow_type",
        "packet_id",
        "reviewer_id",
        "source_binding",
    ):
        if submission.get(field) != packet.get(field):
            raise ValidationError("bound review submission {} mismatch".format(field))
    tasks = {task["task_id"]: task for task in packet["tasks"]}
    responses = {item["task_id"]: item for item in submission["responses"]}
    if len(responses) != len(submission["responses"]) or set(responses) != set(tasks):
        raise ValidationError("bound review submission must cover every task exactly once")
    for task_id, task in tasks.items():
        item = responses[task_id]
        if (
            item.get("subject_id") != task["subject_id"]
            or item.get("subject_sha256") != task["subject_sha256"]
        ):
            raise ValidationError("bound review submission subject binding mismatch")
        _validate_bound_response(packet["workflow_type"], item["response"])
    return _json_copy(submission, "bound review submission")


_ATOM_SEMANTIC_FIELDS = (
    "category",
    "predicate",
    "arguments",
    "layer",
    "polarity",
    "weight",
)

QUERY_CHECK_PROJECTION_SPECS = {
    "support_and_response_disposition": {
        "kind": "oracle_fields",
        "oracle_fields": (
            "expected_support",
            "acceptable_response",
            "unsupported_reasons",
        ),
    },
    "cardinality_and_polarity": {
        "kind": "atoms",
        "atom_categories": (
            "actor",
            "road",
            "spatial",
            "event",
            "temporal",
            "normative",
        ),
        "atom_fields": ("category", "predicate", "arguments", "polarity"),
        "oracle_fields": ("ego_gate",),
    },
    "road_and_spatial_decomposition": {
        "kind": "atoms",
        "atom_categories": ("road", "spatial"),
        "atom_fields": _ATOM_SEMANTIC_FIELDS,
        "oracle_fields": (),
    },
    "event_graph_and_actor_binding": {
        "kind": "atoms",
        "atom_categories": (
            "actor",
            "spatial",
            "event",
            "temporal",
            "normative",
        ),
        "atom_fields": _ATOM_SEMANTIC_FIELDS,
        "oracle_fields": (),
    },
    "surface_layering": {
        "kind": "atoms",
        "atom_categories": (
            "actor",
            "road",
            "spatial",
            "event",
            "temporal",
            "normative",
        ),
        "atom_fields": (
            "category",
            "predicate",
            "arguments",
            "layer",
            "polarity",
            "weight",
        ),
        "oracle_fields": ("surface_style",),
    },
    "cpd_common_eligibility": {
        "kind": "cpd_policy",
        "oracle_fields": (),
    },
}

_QUERY_CHECK_CATEGORY_LABELS_ZH = {
    "actor": "参与者",
    "road": "道路结构",
    "spatial": "空间关系",
    "event": "外部事件",
    "temporal": "事件时序",
    "normative": "规则与风险",
}

_QUERY_CHECK_ATOM_FIELD_LABELS_ZH = {
    "category": "卡片内容参数",
    "predicate": "卡片内容参数",
    "arguments": "卡片内容参数",
    "layer": "计分层级",
    "polarity": "要求出现/要求不存在",
    "weight": "权重",
}

_QUERY_CHECK_ORACLE_FIELD_LABELS_ZH = {
    "expected_support": "这条 query 是否应被处理",
    "acceptable_response": "可接受的响应",
    "unsupported_reasons": "无法支持的原因",
    "ego_gate": "冻结的 ego bus 身份门槛",
    "surface_style": "当前 precise/partial/vague 表述类型",
}


def query_check_scope_description_zh(check: str) -> str:
    """Describe one canonical projection scope for the reviewer UI."""

    try:
        spec = QUERY_CHECK_PROJECTION_SPECS[check]
    except KeyError as exc:
        raise ValidationError(
            "unknown required query-review check {!r}".format(check)
        ) from exc
    kind = spec.get("kind")
    oracle_fields = tuple(spec.get("oracle_fields", ()))
    allowed_oracle_fields = {
        "oracle_fields": {
            "expected_support",
            "acceptable_response",
            "unsupported_reasons",
        },
        "atoms": {"ego_gate", "surface_style"},
        "cpd_policy": set(),
    }
    if kind not in allowed_oracle_fields:
        raise ValidationError(
            "required-check {!r} has unknown projection kind {!r}".format(
                check, kind
            )
        )
    unknown_oracle_fields = set(oracle_fields) - allowed_oracle_fields[kind]
    if unknown_oracle_fields:
        raise ValidationError(
            "required-check {!r} has undescribed oracle fields: {}".format(
                check, ", ".join(sorted(unknown_oracle_fields))
            )
        )
    if kind == "oracle_fields":
        labels = [_QUERY_CHECK_ORACLE_FIELD_LABELS_ZH[field] for field in oracle_fields]
        return "读取{}；它不由 requirement 卡决定。".format("、".join(labels))
    if kind == "cpd_policy":
        return "只读取第5步的合理多样性规则，不读取 requirement 卡。"

    atom_categories = tuple(spec.get("atom_categories", ()))
    unknown_categories = set(atom_categories) - set(_QUERY_CHECK_CATEGORY_LABELS_ZH)
    if unknown_categories:
        raise ValidationError(
            "required-check {!r} has undescribed atom categories: {}".format(
                check, ", ".join(sorted(unknown_categories))
            )
        )
    atom_fields = tuple(spec.get("atom_fields", ()))
    unknown_atom_fields = set(atom_fields) - set(_QUERY_CHECK_ATOM_FIELD_LABELS_ZH)
    if unknown_atom_fields:
        raise ValidationError(
            "required-check {!r} has undescribed atom fields: {}".format(
                check, ", ".join(sorted(unknown_atom_fields))
            )
        )
    categories = "、".join(
        _QUERY_CHECK_CATEGORY_LABELS_ZH[category] for category in atom_categories
    )
    aspects = []
    for field in atom_fields:
        label = _QUERY_CHECK_ATOM_FIELD_LABELS_ZH[field]
        if label not in aspects:
            aspects.append(label)
    description = "读取{}卡的{}。".format(categories, "、".join(aspects))
    if "ego_gate" in oracle_fields:
        description += (
            "此外还检查冻结的 ego bus 身份门槛；它没有对应的 requirement 卡，"
            "若源数据错误只能将 subject 标为阻塞。"
        )
    if "surface_style" in oracle_fields:
        description += "同时绑定当前 precise/partial/vague 表述类型。"
    return description


def atom_semantic_projection(atom: Mapping[str, Any]) -> Dict[str, Any]:
    return {field: copy.deepcopy(atom.get(field)) for field in _ATOM_SEMANTIC_FIELDS}


def _atom_projection_hash(atoms: Sequence[Mapping[str, Any]]) -> str:
    return _json_sha256([atom_semantic_projection(atom) for atom in atoms])


def _atom_identity_id(atom: Mapping[str, Any]) -> str:
    return _json_sha256(
        {
            field: copy.deepcopy(atom.get(field))
            for field in ("category", "predicate", "arguments", "polarity")
        }
    )[:16]


def _cpd_semantic_projection(policy: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the CPD content that a reviewer can accept or revise.

    ``decision_status`` is workflow state, not CPD semantics, so it cannot be
    used to fake a substantive revision.
    """

    return {
        key: copy.deepcopy(value)
        for key, value in policy.items()
        if key != "decision_status"
    }


def _ordered_atom_projection(
    oracle: Mapping[str, Any], categories: Sequence[str], fields: Sequence[str]
) -> List[Dict[str, Any]]:
    category_set = set(categories)
    rows = []
    for atom in oracle.get("atoms", []):
        if atom.get("category") not in category_set:
            continue
        rows.append(
            {
                "atom_id": atom.get("atom_id"),
                **{field: copy.deepcopy(atom.get(field)) for field in fields},
            }
        )
    return sorted(rows, key=lambda row: str(row.get("atom_id")))


def query_check_projection(
    check: str, oracle: Mapping[str, Any]
) -> Dict[str, Any]:
    """Project one required-check scope from an oracle proposal.

    The projections deliberately overlap.  A change may legitimately close
    more than one review check, but a ``revise`` verdict must change at least
    the declared scope; a reason string alone is never sufficient.
    """

    try:
        spec = QUERY_CHECK_PROJECTION_SPECS[check]
    except KeyError as exc:
        raise ValidationError(
            "unknown required query-review check {!r}".format(check)
        ) from exc
    if spec["kind"] == "cpd_policy":
        return {"cpd_policy": _cpd_semantic_projection(oracle.get("cpd_policy", {}))}
    projection = {
        field: copy.deepcopy(oracle.get(field))
        for field in spec.get("oracle_fields", ())
    }
    if spec["kind"] == "atoms":
        projection["atoms"] = _ordered_atom_projection(
            oracle,
            spec["atom_categories"],
            spec["atom_fields"],
        )
    return projection


def _validate_query_decision_closure(
    task: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    require_confirmation: bool,
) -> None:
    """Bind high-level review verdicts to the submitted oracle proposal."""

    draft = task["oracle_draft"]
    proposed = response["proposed_oracle"]
    checks = response["required_check_decisions"]
    if set(checks) != set(REQUIRED_REVIEW_CHECKS):
        raise ValidationError("query review must decide every required check")
    for check in REQUIRED_REVIEW_CHECKS:
        verdict = checks[check]["verdict"]
        before = query_check_projection(check, draft)
        after = query_check_projection(check, proposed)
        if verdict == "accept" and before != after:
            raise ValidationError(
                "accepted required check {!r} drifts in proposed_oracle".format(
                    check
                )
            )
        if verdict == "revise" and before == after:
            raise ValidationError(
                "required-check revision {!r} is not realized in proposed_oracle".format(
                    check
                )
            )
        if verdict == "reject" and require_confirmation:
            raise ValidationError(
                "rejected required check {!r} cannot produce a confirmed oracle".format(
                    check
                )
            )

    cpd_decision = response["cpd_decision"]
    cpd_check_verdict = checks["cpd_common_eligibility"]["verdict"]
    if cpd_decision["verdict"] != cpd_check_verdict:
        raise ValidationError("CPD decision conflicts with cpd_common_eligibility check")
    before_cpd = _cpd_semantic_projection(draft["cpd_policy"])
    after_cpd = _cpd_semantic_projection(proposed["cpd_policy"])
    replacement = cpd_decision.get("replacement_policy")
    if cpd_decision["verdict"] == "accept":
        if replacement is not None or before_cpd != after_cpd:
            raise ValidationError("accepted CPD policy must remain unchanged")
    elif cpd_decision["verdict"] == "revise":
        if not isinstance(replacement, Mapping):
            raise ValidationError("revised CPD decision requires replacement_policy")
        if before_cpd == after_cpd:
            raise ValidationError("CPD revision is not realized in proposed_oracle")
        if _cpd_semantic_projection(replacement) != after_cpd:
            raise ValidationError("CPD replacement_policy differs from proposed_oracle")
    elif cpd_decision["verdict"] == "reject":
        if replacement is not None:
            raise ValidationError("rejected CPD decision cannot carry replacement_policy")
        if require_confirmation:
            raise ValidationError("rejected CPD policy cannot produce a confirmed oracle")
    else:  # schema validation should make this unreachable
        raise ValidationError("invalid CPD decision verdict")


def _confirmed_oracle(
    proposed: Mapping[str, Any],
    draft: Mapping[str, Any],
    query_record: Mapping[str, Any],
) -> Dict[str, Any]:
    decision = _json_copy(proposed, "human-proposed oracle")
    for field in ("query_id", "intent_group_id", "surface_style"):
        if decision.get(field) != draft.get(field) or decision.get(field) != query_record.get(field):
            raise ValidationError("human oracle changes query identity field {}".format(field))
    for oracle_field, query_field in (
        ("expected_support", "expected_support"),
        ("acceptable_response", "acceptable_response"),
        ("unsupported_reasons", "expected_reason_if_unsupported"),
    ):
        if decision.get(oracle_field, []) != query_record.get(query_field, []):
            raise ValidationError("human oracle changes frozen support metadata")
    decision["decision_status"] = "confirmed"
    decision["cpd_policy"]["decision_status"] = "confirmed"
    seen = set()
    for atom in decision["atoms"]:
        atom["decision_status"] = "confirmed"
        if atom.get("atom_id") != _atom_identity_id(atom) or atom["atom_id"] in seen:
            raise ValidationError("human oracle contains a forged or repeated atom ID")
        seen.add(atom["atom_id"])
    validate_schema_instance(decision, "oracle_record", context="confirmed human oracle")
    return decision


def _formal_query_review_payload(
    task: Mapping[str, Any], response: Mapping[str, Any], reviewer_id: str
) -> Dict[str, Any]:
    _validate_query_decision_closure(
        task,
        response,
        require_confirmation=True,
    )
    draft = task["oracle_draft"]
    decision = _confirmed_oracle(response["proposed_oracle"], draft, task["query_record"])
    draft_by_id = {atom["atom_id"]: atom for atom in draft["atoms"]}
    final_by_id = {atom["atom_id"]: atom for atom in decision["atoms"]}
    atom_decisions = {item["atom_id"]: item for item in response["atom_decisions"]}
    if set(atom_decisions) != set(draft_by_id):
        raise ValidationError("formal query review does not cover every draft atom")

    reviews = []
    derived_targets = set()
    for draft_atom_id in sorted(draft_by_id):
        item = atom_decisions[draft_atom_id]
        verdict = item["verdict"]
        replacement_atoms = item.get("replacement_atoms", [])
        replacement_ids = [atom.get("atom_id") for atom in replacement_atoms]
        if any(not isinstance(atom_id, str) for atom_id in replacement_ids):
            raise ValidationError("replacement atoms require atom IDs")
        if len(set(replacement_ids)) != len(replacement_ids):
            raise ValidationError("replacement atoms repeat an atom ID")
        for replacement in replacement_atoms:
            target = final_by_id.get(replacement["atom_id"])
            if target is None or atom_semantic_projection(target) != atom_semantic_projection(replacement):
                raise ValidationError("replacement atom differs from proposed oracle")
        if verdict == "accept":
            if replacement_ids:
                raise ValidationError("accepted atom cannot list replacements")
            if draft_atom_id not in final_by_id:
                raise ValidationError("accepted atom is missing from proposed oracle")
            after_atoms = [final_by_id[draft_atom_id]]
        elif verdict == "reject":
            if replacement_ids or draft_atom_id in final_by_id:
                raise ValidationError("rejected atom remains in proposed oracle")
            after_atoms = []
        elif verdict == "modify":
            if len(replacement_ids) != 1:
                raise ValidationError("modified atom requires exactly one replacement")
            after_atoms = [final_by_id[replacement_ids[0]]]
        elif verdict == "split":
            if len(replacement_ids) < 2:
                raise ValidationError("split atom requires at least two replacements")
            after_atoms = [final_by_id[atom_id] for atom_id in replacement_ids]
        elif verdict == "merge":
            if len(replacement_ids) != 1:
                raise ValidationError("merged atom requires exactly one replacement")
            after_atoms = [final_by_id[replacement_ids[0]]]
        else:
            raise ValidationError("invalid query atom decision")
        changed_fields = sorted(
            {
                field
                for target in after_atoms
                for field in _ATOM_SEMANTIC_FIELDS
                if draft_by_id[draft_atom_id].get(field) != target.get(field)
            }
        )
        if verdict == "accept" and changed_fields:
            raise ValidationError("accepted atom has a semantic change")
        if verdict in ("modify", "split", "merge") and not changed_fields:
            raise ValidationError("changed atom decision has no semantic change")
        operation_group_id = (
            "merge:{}".format(replacement_ids[0]) if verdict == "merge" else None
        )
        reviews.append(
            {
                "draft_atom_id": draft_atom_id,
                "verdict": verdict,
                "replacement_atom_ids": replacement_ids,
                "operation_group_id": operation_group_id,
                "reason": item["reason"],
                "before_sha256": _atom_projection_hash([draft_by_id[draft_atom_id]]),
                "after_sha256": _atom_projection_hash(after_atoms),
                "changed_fields": changed_fields,
            }
        )
        if verdict == "accept":
            derived_targets.add(draft_atom_id)
        else:
            derived_targets.update(replacement_ids)

    target_uses = {}
    merge_groups = {}
    for review in reviews:
        verdict = review["verdict"]
        target_ids = (
            [review["draft_atom_id"]]
            if verdict == "accept"
            else review["replacement_atom_ids"]
        )
        for target_id in target_ids:
            target_uses.setdefault(target_id, []).append(review)
        if verdict == "merge":
            merge_groups.setdefault(review["operation_group_id"], []).append(review)
    valid_merge_targets = set()
    for group_id, members in merge_groups.items():
        targets = {
            target_id
            for member in members
            for target_id in member["replacement_atom_ids"]
        }
        if len(members) < 2 or len(targets) != 1:
            raise ValidationError("merge operation requires at least two atoms and one target")
        target_id = next(iter(targets))
        if target_uses.get(target_id) != members:
            raise ValidationError("merge target is also used outside its operation group")
        valid_merge_targets.add(target_id)
    if any(
        len(uses) != 1 and target_id not in valid_merge_targets
        for target_id, uses in target_uses.items()
    ):
        raise ValidationError("final atom is reused without an explicit merge")

    added_ids = set(final_by_id) - derived_targets
    for atom_id in added_ids:
        if final_by_id[atom_id].get("provenance", {}).get("source") != "human_review":
            raise ValidationError("new human atom lacks human_review provenance")
    payload = {
        "status": "complete",
        "reviewer_id": reviewer_id,
        "required_check_status": {
            check: "complete" for check in REQUIRED_REVIEW_CHECKS
        },
        "draft_atom_reviews": reviews,
        "added_atom_reviews": [
            {
                "atom_id": atom_id,
                "reason": response.get("notes") or "explicit human-added atom",
            }
            for atom_id in sorted(added_ids)
        ],
        "decision": decision,
    }
    return payload


def _validate_query_response_document(response: Mapping[str, Any]) -> None:
    """Validate one query response without pretending it is a full submission."""

    if _JSONSCHEMA_IMPORT_ERROR is not None:
        raise ValidationError(
            "human-workflow schema validation requires jsonschema: {}".format(
                _JSONSCHEMA_IMPORT_ERROR
            )
        )
    submission_schema = _load_human_schema("query_review_submission")
    response_schema = {
        "$schema": submission_schema.get(
            "$schema", "http://json-schema.org/draft-07/schema#"
        ),
        "$ref": "#/definitions/response",
        "definitions": copy.deepcopy(submission_schema["definitions"]),
    }
    errors = sorted(
        Draft7Validator(response_schema).iter_errors(response),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        details = []
        for error in errors[:5]:
            path = "$" + "".join(
                "[{}]".format(repr(part)) for part in error.absolute_path
            )
            details.append("{}: {}".format(path, error.message))
        raise ValidationError(
            "query task response violates the active submission schema: {}".format(
                "; ".join(details)
            )
        )


def validate_query_review_task_response(
    task: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    reviewer_id: str,
    require_confirmation: bool = False,
) -> Dict[str, Any]:
    """Validate one completed workbench subject with production semantics.

    A partial workbench checkpoint is not a formal submission. This helper lets
    a UI validate one completed subject without filling other subjects with
    machine recommendations or duplicating the finalizer's closure rules.
    """

    if not isinstance(task, Mapping) or not isinstance(response, Mapping):
        raise ValidationError("query task and response must be JSON objects")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise ValidationError("reviewer ID must be a non-empty string")
    if not isinstance(task.get("subject_id"), str) or not task["subject_id"]:
        raise ValidationError("query task lacks a subject ID")
    if not isinstance(task.get("oracle_draft"), Mapping):
        raise ValidationError("query task lacks its oracle draft")
    if not isinstance(task.get("query_record"), Mapping):
        raise ValidationError("query task lacks its query record")

    _validate_query_response_document(response)
    atom_ids = [decision["atom_id"] for decision in response["atom_decisions"]]
    expected_atom_ids = [atom["atom_id"] for atom in task["oracle_draft"]["atoms"]]
    if len(set(atom_ids)) != len(atom_ids) or set(atom_ids) != set(expected_atom_ids):
        raise ValidationError("query response must decide every draft atom exactly once")

    proposed = response["proposed_oracle"]
    validate_schema_instance(proposed, "oracle_record", context="proposed oracle")
    if (
        proposed.get("query_id") != task["subject_id"]
        or proposed.get("decision_status") != "draft"
        or proposed.get("cpd_policy", {}).get("decision_status") != "draft"
        or any(
            atom.get("decision_status") != "draft"
            for atom in proposed.get("atoms", [])
        )
    ):
        raise ValidationError(
            "individual query responses must remain source-bound draft proposals"
        )

    normalized = _json_copy(response, "query task response")
    _validate_query_decision_closure(
        task,
        normalized,
        require_confirmation=require_confirmation,
    )
    if require_confirmation:
        _formal_query_review_payload(task, normalized, reviewer_id)
    return normalized


def finalize_query_review_bundle(
    bundle: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    library_source: Any,
    oracle_source: Any,
) -> Dict[str, Any]:
    """Produce one complete human-gold record per reviewed query."""

    if bundle.get("review_policy") != "one_complete_human_gold_per_subject":
        raise ValidationError("query bundle does not use the single-gold policy")
    packet = bundle.get("reviewer_packet")
    if (
        bundle.get("workflow_type") != QUERY_WORKFLOW
        or not isinstance(packet, Mapping)
        or packet.get("source_binding") != bundle.get("source_binding")
    ):
        raise ValidationError("query bundle lacks its single reviewer packet")
    expected_bundle = export_query_review_bundle(
        library_source,
        oracle_source,
        reviewer_id=packet.get("reviewer_id"),
    )
    _assert_canonical_review_bundle(bundle, expected_bundle, "query")
    validated = validate_query_review_submission(
        packet,
        submission,
        library_source=library_source,
        oracle_source=oracle_source,
    )
    by_subject = {
        item["subject_id"]: item["response"] for item in validated["responses"]
    }
    tasks = {task["subject_id"]: task for task in packet["tasks"]}
    gold_records = []
    confirmed = []
    for query_id in sorted(tasks):
        task = tasks[query_id]
        payload = _formal_query_review_payload(
            task, by_subject[query_id], validated["reviewer_id"]
        )
        record = {
            "schema_version": SCHEMA_VERSION,
            "workflow_type": "query_oracle_human_gold",
            "query_id": query_id,
            "subject_sha256": task["subject_sha256"],
            "reviewer_id": validated["reviewer_id"],
            "review_status": "complete",
            "canonical_source_revalidated": True,
            "source_binding": copy.deepcopy(bundle["source_binding"]),
            "review_payload": payload,
            "decision_status": "confirmed",
        }
        record["gold_record_id"] = _json_sha256(record)
        validate_human_document(record, "query_gold_record")
        gold_records.append(record)
        confirmed.append(copy.deepcopy(payload["decision"]))
    validate_schema_records(confirmed, "oracle_record", context="confirmed human oracles")
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": "query_oracle_review_finalized",
        "source_binding": copy.deepcopy(bundle["source_binding"]),
        "decision_status": "confirmed",
        "review_policy": "one_complete_human_gold_per_subject",
        "canonical_bundle_revalidated": True,
        "confirmed_oracles": confirmed,
        "human_gold_records": gold_records,
    }


def _approval_label(
    response: Mapping[str, Any], reviewer_id: str, label: str
) -> Dict[str, Any]:
    _validate_bound_response(PLATFORM_REVIEW_WORKFLOW, response)
    if response["verdict"] != "approve":
        raise ValidationError("{} subject was not approved; revise assets and re-review".format(label))
    return {
        "reviewer_id": reviewer_id,
        "verdict": "approve",
        "reason": response["reason"],
    }


def _assert_platform_neutral_cpd_ontology(ontology: Mapping[str, Any]) -> None:
    for concept in ontology["concepts"]:
        if concept.get("kind") != "cpd_dimension":
            continue
        name = str(concept.get("definition", {}).get("name", "")).lower()
        serialized_definition = canonical_json_bytes(
            concept.get("definition", {})
        ).decode("utf-8").lower()
        if (
            any(token in name for token in ("map", "sequence_block", "block_token"))
            or any(
                token in serialized_definition
                for token in (
                    "metadrive",
                    "pgmap",
                    "block_sequence",
                    "token_registry",
                    "sequence-block",
                )
            )
        ):
            raise ValidationError(
                "platform map/token implementation choices cannot enter CPD_common"
            )


def export_carla_stage_platform_review_bundle(
    ontology_source: Any,
    semantic_fixture_source: Any,
    carla_capability_source: Any,
    *,
    reviewer_id: str,
) -> Dict[str, Any]:
    """Export a CARLA-only review stage without claiming cross-platform freeze."""

    ontology, ontology_sha256, ontology_scope = _materialize_json_source(
        ontology_source, "CARLA-stage ontology"
    )
    semantic, semantic_sha256, semantic_scope = _materialize_json_source(
        semantic_fixture_source, "CARLA-stage semantic fixtures"
    )
    capability, capability_sha256, capability_scope = _materialize_json_source(
        carla_capability_source, "CARLA-stage capability"
    )
    validate_schema_instance(ontology, "common_ontology", context="CARLA-stage ontology")
    validate_schema_instance(
        semantic, "semantic_fixture_corpus", context="CARLA-stage fixtures"
    )
    validate_schema_instance(
        capability, "platform_capability", context="CARLA-stage capability"
    )
    if capability.get("platform") != "carla":
        raise ValidationError("CARLA-stage capability must target CARLA")
    if capability.get("ontology_id") != ontology.get("ontology_id"):
        raise ValidationError("CARLA-stage capability binds a different ontology")
    if capability.get("ontology_sha256") != ontology_sha256:
        raise ValidationError("CARLA-stage capability has a stale ontology hash")
    _assert_platform_neutral_cpd_ontology(ontology)
    ontology_by_id = {
        concept["concept_id"]: concept for concept in ontology["concepts"]
    }
    subjects = []
    seen_subject_ids = set()

    def append_subject(
        review_kind: str,
        subject_id: str,
        formal_subject: Mapping[str, Any],
        definition_sha256: str,
        input_contract: str,
    ) -> None:
        scoped_id = "{}:{}".format(review_kind, subject_id)
        if scoped_id in seen_subject_ids:
            raise ValidationError("CARLA-stage platform subject is duplicated")
        seen_subject_ids.add(scoped_id)
        audit_subject = _json_copy(formal_subject, "CARLA-stage audit subject")
        subjects.append(
            {
                "subject_id": scoped_id,
                "subject": {
                    "review_kind": review_kind,
                    "formal_subject_id": subject_id,
                    "platform": "carla",
                    "formal_subject_sha256": _json_sha256(formal_subject),
                    "audit_subject_sha256": _json_sha256(audit_subject),
                    "ontology_definition_sha256": definition_sha256,
                    "metadrive_pg_registry_sha256": None,
                    "metadrive_pg_token_registry_sha256": None,
                    "input_contract": input_contract,
                    "audit_subject": audit_subject,
                    "machine_draft_decision": "approve",
                    "metadrive_road_source_contract": "not_applicable",
                },
            }
        )

    for case in semantic["cases"]:
        if case.get("platform") != "carla":
            continue
        concept_ids = case.get("concept_ids", [])
        if len(concept_ids) != 1 or concept_ids[0] not in ontology_by_id:
            raise ValidationError("CARLA fixture must bind exactly one known concept")
        append_subject(
            "fixture_case",
            case["case_id"],
            case,
            ontology_by_id[concept_ids[0]]["definition_sha256"],
            semantic["input_contract"],
        )
    for concept in capability["concepts"]:
        ontology_concept = ontology_by_id.get(concept.get("concept_id"))
        if (
            ontology_concept is None
            or concept.get("definition_sha256")
            != ontology_concept.get("definition_sha256")
        ):
            raise ValidationError("CARLA capability has a stale concept definition")
        append_subject(
            "capability_status",
            "carla:{}".format(concept["concept_id"]),
            concept,
            ontology_concept["definition_sha256"],
            "raw_platform_artifact_v0.1",
        )
    if not subjects:
        raise ValidationError("CARLA-stage platform review has no subjects")
    count_by_kind = dict(
        Counter(subject["subject"]["review_kind"] for subject in subjects)
    )
    binding = {
        "ontology_sha256": ontology_sha256,
        "ontology_hash_scope": ontology_scope,
        "semantic_fixture_sha256": semantic_sha256,
        "semantic_fixture_hash_scope": semantic_scope,
        "capability_carla_sha256": capability_sha256,
        "capability_carla_hash_scope": capability_scope,
        "subject_count": len(subjects),
        "subject_count_by_kind": count_by_kind,
        "review_scope": "carla_stage_only",
        "stage_asset": True,
        "formal_freeze_eligible": False,
        "included_platforms": ["carla"],
        "deferred_platforms": ["metadrive"],
        "platform_workload_count_rule": "carla_fixture_cases_plus_carla_capability_statuses",
        "platform_implementation_parameters_in_cpd_common": False,
    }
    return _export_single_bound_review_bundle(
        PLATFORM_REVIEW_WORKFLOW,
        binding,
        subjects,
        reviewer_id=reviewer_id,
    )


def _bound_bundle_subject_map(
    bundle: Mapping[str, Any], expected_workflow: str
) -> Dict[str, str]:
    """Validate the sole production reviewer packet and return exact subjects."""

    if bundle.get("workflow_type") != expected_workflow:
        raise ValidationError(
            "human inventory expected {} bundle".format(expected_workflow)
        )
    if bundle.get("review_policy") != "one_complete_human_gold_per_subject":
        raise ValidationError("human inventory rejects legacy multi-review bundles")
    packet = bundle.get("reviewer_packet")
    if not isinstance(packet, Mapping):
        raise ValidationError("human inventory bundle lacks its reviewer packet")
    schema_name = (
        "query_review_packet"
        if expected_workflow == QUERY_WORKFLOW
        else "bound_review_packet"
    )
    validate_human_document(packet, schema_name)
    if (
        packet.get("source_binding") != bundle.get("source_binding")
        or packet.get("packet_id") != _review_packet_id(packet)
    ):
        raise ValidationError("human inventory bundle has a stale reviewer binding")

    def task_map(packet: Mapping[str, Any]) -> Dict[str, str]:
        result = {}
        for task in packet["tasks"]:
            subject_id = task["subject_id"]
            if subject_id in result:
                raise ValidationError("human inventory bundle repeats a subject")
            result[subject_id] = task["subject_sha256"]
        return result

    subjects = task_map(packet)
    if not subjects:
        raise ValidationError("human inventory reviewer packet is empty")
    binding = bundle["source_binding"]
    declared_count = binding.get(
        "record_count" if expected_workflow == QUERY_WORKFLOW else "item_count"
    )
    if expected_workflow == PLATFORM_REVIEW_WORKFLOW:
        declared_count = binding.get("subject_count")
    if type(declared_count) is not int or declared_count != len(subjects):
        raise ValidationError("human inventory bundle declares a stale subject count")
    return subjects


def _assert_carla_stage_query_sources(
    query_bundles: Sequence[Mapping[str, Any]],
) -> None:
    expected_sources = {
        (sha256_file(library_path), sha256_file(oracle_path)): (
            library_path,
            oracle_path,
            record_count,
        )
        for library_path, oracle_path, record_count in _CARLA_STAGE_QUERY_SOURCES
    }
    actual = {}
    for bundle in query_bundles:
        binding = bundle["source_binding"]
        if (
            binding.get("library_hash_scope") != "file_bytes"
            or binding.get("oracle_hash_scope") != "file_bytes"
        ):
            raise ValidationError(
                "complete carla_stage query bundles must bind checked-in file bytes"
            )
        key = (binding.get("library_sha256"), binding.get("oracle_sha256"))
        if key in actual:
            raise ValidationError("complete carla_stage repeats a query source pair")
        actual[key] = binding.get("record_count")
    expected_counts = {
        key: source_spec[2] for key, source_spec in expected_sources.items()
    }
    if actual != expected_counts:
        raise ValidationError(
            "complete carla_stage must bind the checked-in 48/252 query libraries and oracle drafts"
        )
    for bundle in query_bundles:
        binding = bundle["source_binding"]
        key = (binding["library_sha256"], binding["oracle_sha256"])
        library_path, oracle_path, _ = expected_sources[key]
        expected_bundle = export_query_review_bundle(
            library_path,
            oracle_path,
            reviewer_id=bundle["reviewer_packet"]["reviewer_id"],
        )
        if bundle != expected_bundle:
            raise ValidationError(
                "complete carla_stage query tasks differ from checked-in source materialization"
            )


def _assert_bound_source_bytes(
    binding: Mapping[str, Any],
    *,
    hash_field: str,
    scope_field: str,
    source: Any,
    label: str,
    jsonl: bool,
) -> None:
    if source is None:
        raise ValidationError(
            "complete carla_stage requires current {} source input".format(label)
        )
    materializer = _materialize_jsonl_source if jsonl else _materialize_json_source
    _, digest, scope = materializer(source, label)
    if binding.get(hash_field) != digest or binding.get(scope_field) != scope:
        raise ValidationError(
            "complete carla_stage {} source differs from its review bundle".format(label)
        )


def refresh_human_work_inventory(
    inventory: Mapping[str, Any],
    *,
    query_bundles: Sequence[Mapping[str, Any]],
    platform_bundle: Mapping[str, Any],
    calibration_bundle: Mapping[str, Any],
    protocol_bundle: Mapping[str, Any] = None,
    profile: str = "carla_stage",
    platform_sources: Mapping[str, Any] = None,
    calibration_sources: Mapping[str, Any] = None,
) -> Dict[str, Any]:
    """Recompute workload from packets; default requires the complete CARLA stage."""

    if profile not in ("carla_stage", "partial_dev"):
        raise ValidationError("human inventory profile must be carla_stage or partial_dev")
    if protocol_bundle is not None:
        raise ValidationError("CARLA-first inventory excludes deferred protocol subjects")

    if (
        isinstance(query_bundles, Mapping)
        or isinstance(query_bundles, (str, bytes, bytearray))
        or not query_bundles
    ):
        raise ValidationError("human inventory requires one or more query bundles")
    query_subjects = {}
    query_tasks = []
    query_bundle_bindings = []
    for bundle in query_bundles:
        subjects = _bound_bundle_subject_map(bundle, QUERY_WORKFLOW)
        overlap = set(query_subjects).intersection(subjects)
        if overlap:
            raise ValidationError("human inventory query bundles overlap")
        query_subjects.update(subjects)
        query_tasks.extend(bundle["reviewer_packet"]["tasks"])
        query_bundle_bindings.append(
            {
                "bundle_sha256": _json_sha256(bundle),
                "source_binding": copy.deepcopy(bundle["source_binding"]),
                "subject_count": len(subjects),
            }
        )
    platform_subjects = _bound_bundle_subject_map(
        platform_bundle, PLATFORM_REVIEW_WORKFLOW
    )
    calibration_subjects = _bound_bundle_subject_map(
        calibration_bundle, CALIBRATION_REVIEW_WORKFLOW
    )
    platform_binding = platform_bundle["source_binding"]
    calibration_binding = calibration_bundle["source_binding"]
    if (
        platform_binding.get("review_scope") != "carla_stage_only"
        or calibration_binding.get("review_scope") != "carla_stage_only"
    ):
        raise ValidationError("CARLA-first inventory requires CARLA-stage source bundles")
    workload_scope = "carla_first_stage" if profile == "carla_stage" else "partial_dev"

    count_by_kind = Counter(
        task["subject"]["review_kind"]
        for task in platform_bundle["reviewer_packet"]["tasks"]
    )
    if dict(count_by_kind) != platform_binding.get("subject_count_by_kind"):
        raise ValidationError("platform review subject-kind counts are stale")

    query_count = len(query_subjects)
    platform_count = len(platform_subjects)
    calibration_count = len(calibration_subjects)
    protocol_count = 0
    total_subjects = query_count + platform_count + calibration_count + protocol_count
    value = _json_copy(inventory, "human work inventory")
    try:
        current = value["current_human_review_state"]
        oracle_inventory = value["pre_freeze_oracle"]
        platform_inventory = value["platform_capability_review"]
        calibration_inventory = value["judge_calibration"]
        protocol_inventory = value["protocol_decision_review"]
        workload = value["pre_freeze_human_workload"]
    except (KeyError, TypeError) as exc:
        raise ValidationError("human work inventory lacks required sections") from exc
    for section, legacy_fields in (
        (
            current,
            ("pending_judge_calibration_adjudications",),
        ),
        (
            oracle_inventory,
            (
                "independent_annotators",
                "independent_query_reviews",
                "adjudication_records",
                "independent_atom_review_entries",
                "adjudicated_atom_review_entries",
            ),
        ),
        (
            platform_inventory,
            (
                "independent_platform_review_entries",
                "platform_review_adjudications",
            ),
        ),
        (
            calibration_inventory,
            (
                "independent_annotators",
                "requires_adjudication",
                "independent_review_entries",
                "adjudications",
            ),
        ),
        (
            protocol_inventory,
            ("independent_review_entries", "adjudications"),
        ),
        (
            workload,
            ("independent_review_entries", "adjudications"),
        ),
    ):
        for field in legacy_fields:
            section.pop(field, None)

    query_records = [task["query_record"] for task in query_tasks]
    oracle_records = [task["oracle_draft"] for task in query_tasks]
    development_queries = sum(
        str(record.get("query_id", "")).startswith("BSG_DEV_")
        for record in query_records
    )
    test_queries = query_count - development_queries
    if profile == "carla_stage" and (
        query_count != 300
        or development_queries != 48
        or test_queries != 252
        or platform_count != 52
        or count_by_kind != Counter({"fixture_case": 38, "capability_status": 14})
        or calibration_count != 90
    ):
        raise ValidationError(
            "complete carla_stage profile requires 300 query + 52 platform + 90 calibration subjects"
        )
    if profile == "carla_stage":
        _assert_carla_stage_query_sources(query_bundles)
        if not isinstance(platform_sources, Mapping):
            raise ValidationError(
                "complete carla_stage requires current platform source inputs"
            )
        if not isinstance(calibration_sources, Mapping):
            raise ValidationError(
                "complete carla_stage requires current calibration source inputs"
            )
        for hash_field, scope_field, source_key, label in (
            ("ontology_sha256", "ontology_hash_scope", "ontology", "ontology"),
            (
                "semantic_fixture_sha256",
                "semantic_fixture_hash_scope",
                "semantic_fixtures",
                "semantic fixtures",
            ),
            (
                "capability_carla_sha256",
                "capability_carla_hash_scope",
                "carla_capability",
                "CARLA capability",
            ),
        ):
            _assert_bound_source_bytes(
                platform_binding,
                hash_field=hash_field,
                scope_field=scope_field,
                source=platform_sources.get(source_key),
                label=label,
                jsonl=False,
            )
        for hash_field, scope_field, source_key, label, jsonl in (
            (
                "calibration_roster_sha256",
                "calibration_roster_hash_scope",
                "roster",
                "CARLA calibration roster",
                False,
            ),
            (
                "reviewer_subjects_sha256",
                "reviewer_subjects_hash_scope",
                "reviewer_subjects",
                "CARLA calibration reviewer subjects",
                True,
            ),
        ):
            _assert_bound_source_bytes(
                calibration_binding,
                hash_field=hash_field,
                scope_field=scope_field,
                source=calibration_sources.get(source_key),
                label=label,
                jsonl=jsonl,
            )
        expected_platform_bundle = export_carla_stage_platform_review_bundle(
            platform_sources["ontology"],
            platform_sources["semantic_fixtures"],
            platform_sources["carla_capability"],
            reviewer_id=platform_bundle["reviewer_packet"]["reviewer_id"],
        )
        if platform_bundle != expected_platform_bundle:
            raise ValidationError(
                "complete carla_stage platform tasks differ from current source materialization"
            )
        expected_calibration_bundle = (
            export_carla_stage_judge_calibration_review_bundle(
                calibration_sources["roster"],
                calibration_sources["reviewer_subjects"],
                reviewer_id=calibration_bundle["reviewer_packet"]["reviewer_id"],
            )
        )
        if calibration_bundle != expected_calibration_bundle:
            raise ValidationError(
                "complete carla_stage calibration tasks differ from current source materialization"
            )
    atom_count = sum(len(record["atoms"]) for record in oracle_records)
    unique_atom_candidates = len(
        {
            (record["intent_group_id"], atom["atom_id"])
            for record in oracle_records
            for atom in record["atoms"]
        }
    )
    oracle_inventory.update(
        {
            "test_queries": test_queries,
            "development_queries": development_queries,
            "total_queries": query_count,
            "intent_groups": len(
                {record["intent_group_id"] for record in query_records}
            ),
            "reviewer_submissions": query_count,
            "human_gold_records_required": query_count,
            "test_atom_instances": sum(
                len(oracle["atoms"])
                for query, oracle in zip(query_records, oracle_records)
                if not str(query.get("query_id", "")).startswith("BSG_DEV_")
            ),
            "development_atom_instances": sum(
                len(oracle["atoms"])
                for query, oracle in zip(query_records, oracle_records)
                if str(query.get("query_id", "")).startswith("BSG_DEV_")
            ),
            "total_atom_instances": atom_count,
            "required_check_decisions": len(REQUIRED_REVIEW_CHECKS) * query_count,
            "cpd_decisions": query_count,
            "total_draft_decisions": (
                atom_count + len(REQUIRED_REVIEW_CHECKS) * query_count + query_count
            ),
            "single_reviewer_atom_entries": atom_count,
            "intent_local_unique_atom_candidates": unique_atom_candidates,
            "bound_query_bundle_counts": [
                binding["subject_count"] for binding in query_bundle_bindings
            ],
        }
    )
    current.update(
        {
            "completed_real_human_reviews": 0,
            "completed_admissible_human_gold_records": 0,
            "pending_query_review_subjects": query_count,
            "pending_platform_review_subjects": platform_count,
            "pending_judge_calibration_subjects": calibration_count,
            "pending_protocol_decision_subjects": protocol_count,
        }
    )
    platform_inventory.update(
        {
            "fixture_case_review_subjects": count_by_kind.get("fixture_case", 0),
            "capability_status_review_subjects": count_by_kind.get(
                "capability_status", 0
            ),
            "registry_implementation_review_subjects": count_by_kind.get(
                "registry_implementation", 0
            ),
            "total_platform_review_subjects": platform_count,
            "reviewer_submissions": platform_count,
            "human_gold_records_required": platform_count,
            "platform_review_subject_count_by_kind": dict(count_by_kind),
            "platform_workload_count_rule": platform_binding[
                "platform_workload_count_rule"
            ],
            "review_scope": platform_binding["review_scope"],
        }
    )
    calibration_inventory.update(
        {
            "total_calibration_items": calibration_count,
            "reviewer_submissions": calibration_count,
            "human_gold_records_required": calibration_count,
            "review_scope": calibration_binding["review_scope"],
        }
    )
    protocol_inventory.update(
        {
            "subjects": protocol_count,
            "reviewer_submissions": 0,
            "human_gold_records_required": 0,
            "deferred_until_final_assets": True,
        }
    )
    workload.update(
        {
            "workload_scope": workload_scope,
            "subjects": total_subjects,
            "reviewer_submissions": total_subjects,
            "human_gold_records_required": total_subjects,
            "total_human_decision_records": total_subjects,
            "formal_stage_profile_complete": profile == "carla_stage",
            "formula": (
                "{} query + {} platform + {} calibration + {} protocol subjects, "
                "one complete human gold per subject"
            ).format(query_count, platform_count, calibration_count, protocol_count),
            "source_bundle_bindings": {
                "query": query_bundle_bindings,
                "platform_bundle_sha256": _json_sha256(platform_bundle),
                "calibration_bundle_sha256": _json_sha256(calibration_bundle),
                "protocol_bundle_sha256": (
                    _json_sha256(protocol_bundle)
                    if protocol_bundle is not None
                    else None
                ),
            },
        }
    )
    value["decision_status"] = (
        "stage_bound" if profile == "carla_stage" else "partial_dev"
    )
    return value


def _materialize_carla_stage_platform_review(
    bundle: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    ontology_source: Any,
    semantic_fixture_source: Any,
    carla_capability_source: Any,
) -> Dict[str, Any]:
    """Finalize one complete CARLA human-gold record per bound subject."""

    binding = bundle.get("source_binding", {})
    if (
        binding.get("review_scope") != "carla_stage_only"
        or binding.get("stage_asset") is not True
        or binding.get("formal_freeze_eligible") is not False
    ):
        raise ValidationError("not a CARLA-only stage review bundle")
    if bundle.get("review_policy") != "one_complete_human_gold_per_subject":
        raise ValidationError("CARLA platform bundle does not use the single-gold policy")
    packet = bundle.get("reviewer_packet")
    if (
        bundle.get("workflow_type") != PLATFORM_REVIEW_WORKFLOW
        or not isinstance(packet, Mapping)
        or packet.get("source_binding") != binding
    ):
        raise ValidationError("CARLA platform bundle lacks its reviewer packet")
    expected_bundle = export_carla_stage_platform_review_bundle(
        ontology_source,
        semantic_fixture_source,
        carla_capability_source,
        reviewer_id=packet.get("reviewer_id"),
    )
    _assert_canonical_review_bundle(bundle, expected_bundle, "CARLA platform")
    validated = validate_bound_review_submission(packet, submission)
    responses = {
        item["subject_id"]: item["response"] for item in validated["responses"]
    }
    records = []
    for task in packet["tasks"]:
        subject = task["subject"]
        reviewer_label = _approval_label(
            responses[task["subject_id"]], validated["reviewer_id"], "platform"
        )
        record = {
            "schema_version": SCHEMA_VERSION,
            "workflow_type": "platform_fixture_capability_human_gold",
            "subject_id": subject["formal_subject_id"],
            "review_kind": subject["review_kind"],
            "platform": "carla",
            "subject_sha256": subject["formal_subject_sha256"],
            "audit_subject_sha256": subject["audit_subject_sha256"],
            "ontology_definition_sha256": subject["ontology_definition_sha256"],
            "input_contract": subject["input_contract"],
            "reviewer": reviewer_label,
            "review_status": "complete",
            "canonical_source_revalidated": True,
            "decision_status": "confirmed",
        }
        record["gold_record_id"] = _json_sha256(record)
        validate_human_document(record, "platform_gold_record")
        records.append(record)
    document = {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": "carla_stage_platform_review_finalized",
        "stage_scope": "carla_first",
        "included_platforms": ["carla"],
        "deferred_platforms": ["metadrive"],
        "formal_freeze_eligible": False,
        "decision_status": "stage_confirmed",
        "review_policy": "one_complete_human_gold_per_subject",
        "canonical_bundle_revalidated": True,
        "source_binding": copy.deepcopy(binding),
        "human_gold_records": records,
    }
    validate_human_document(document, "carla_stage_platform_report")
    return document


def validate_carla_stage_platform_report(
    report: Mapping[str, Any],
    bundle: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    ontology_source: Any,
    semantic_fixture_source: Any,
    carla_capability_source: Any,
) -> Mapping[str, Any]:
    """Exact-rebuild a CARLA platform report from its human-review sources."""

    validate_human_document(report, "carla_stage_platform_report")
    expected = _materialize_carla_stage_platform_review(
        bundle,
        submission,
        ontology_source=ontology_source,
        semantic_fixture_source=semantic_fixture_source,
        carla_capability_source=carla_capability_source,
    )
    if canonical_json_bytes(report) != canonical_json_bytes(expected):
        raise ValidationError(
            "CARLA platform report differs from exact source-bound reconstruction"
        )
    return report


def finalize_carla_stage_platform_review(
    bundle: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    ontology_source: Any,
    semantic_fixture_source: Any,
    carla_capability_source: Any,
) -> Dict[str, Any]:
    report = _materialize_carla_stage_platform_review(
        bundle,
        submission,
        ontology_source=ontology_source,
        semantic_fixture_source=semantic_fixture_source,
        carla_capability_source=carla_capability_source,
    )
    validate_carla_stage_platform_report(
        report,
        bundle,
        submission,
        ontology_source=ontology_source,
        semantic_fixture_source=semantic_fixture_source,
        carla_capability_source=carla_capability_source,
    )
    return report


_CALIBRATION_FORBIDDEN_KEYS = {
    "judge_prediction",
    "prediction",
    "predicted_verdict",
    "judge_response_id",
    "judge_response_record_sha256",
    "gold_verdict",
}


def _assert_calibration_blind(value: Any, path: str = "calibration_subject") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in _CALIBRATION_FORBIDDEN_KEYS:
                raise ValidationError(
                    "blind calibration packet exposes {} at {}".format(key, path)
                )
            _assert_calibration_blind(child, "{}.{}".format(path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_calibration_blind(child, "{}[{}]".format(path, index))


def _assert_calibration_roster_integrity(roster: Mapping[str, Any]) -> None:
    items = roster.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError("calibration roster contains no items")
    item_ids = [item.get("item_id") for item in items if isinstance(item, Mapping)]
    if len(item_ids) != len(items) or len(set(item_ids)) != len(items):
        raise ValidationError("calibration roster repeats or omits item IDs")
    if roster.get("items_sha256") != _json_sha256(items):
        raise ValidationError("calibration roster items_sha256 is stale")


def _build_calibration_reviewer_subjects_from_roster(
    roster: Mapping[str, Any],
    query_library_source: Any,
    oracle_source: Any,
    response_records_source: Any,
    evidence_records_source: Any,
) -> List[Dict[str, Any]]:
    queries, _, _ = _materialize_jsonl_source(
        query_library_source, "calibration query library"
    )
    oracles, _, _ = _materialize_jsonl_source(
        oracle_source, "calibration oracle"
    )
    responses, _, _ = _materialize_jsonl_source(
        response_records_source, "calibration responses"
    )
    evidence_records, _, _ = _materialize_jsonl_source(
        evidence_records_source, "calibration semantic evidence"
    )
    query_by_id = {record.get("query_id"): record for record in queries}
    oracle_by_id = {record.get("query_id"): record for record in oracles}
    response_by_run = {record.get("run_id"): record for record in responses}
    evidence_by_run = {record.get("run_id"): record for record in evidence_records}
    if any(
        len(mapping) != len(records)
        for mapping, records in (
            (query_by_id, queries),
            (oracle_by_id, oracles),
            (response_by_run, responses),
            (evidence_by_run, evidence_records),
        )
    ):
        raise ValidationError("calibration sources repeat query or run IDs")
    subjects = []
    for item in roster["items"]:
        query = query_by_id.get(item["query_id"])
        oracle = oracle_by_id.get(item["query_id"])
        response = response_by_run.get(item["run_id"])
        evidence = evidence_by_run.get(item["run_id"])
        if any(value is None for value in (query, oracle, response, evidence)):
            raise ValidationError("calibration roster item lacks frozen reviewer evidence")
        if (
            _json_sha256(response) != item["source_response_sha256"]
            or _json_sha256(evidence) != item["source_evidence_sha256"]
        ):
            raise ValidationError("calibration roster source hashes are stale")
        oracle_atom = next(
            (atom for atom in oracle.get("atoms", []) if atom.get("atom_id") == item["atom_id"]),
            None,
        )
        if oracle_atom is None:
            raise ValidationError("calibration roster atom is outside the oracle")
        artifact = response.get("artifact")
        if not isinstance(artifact, Mapping) or not isinstance(artifact.get("path"), str):
            raise ValidationError("calibration response lacks a reviewer-visible artifact")
        artifact_path = Path(artifact["path"])
        if sha256_file(artifact_path) != artifact.get("sha256"):
            raise ValidationError("calibration artifact hash mismatch")
        try:
            artifact_text = artifact_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError("cannot read calibration artifact") from exc
        payload = {
            "query_text": query.get("query_text"),
            "platform": item["platform"],
            "artifact": {
                "sha256": artifact["sha256"],
                "bytes": artifact_path.stat().st_size,
                "text": artifact_text,
            },
            "semantic_evidence": copy.deepcopy(evidence),
            "oracle_atom": copy.deepcopy(oracle_atom),
        }
        if not isinstance(payload["query_text"], str) or not payload["query_text"]:
            raise ValidationError("calibration query lacks reviewer text")
        _assert_calibration_blind(payload)
        subjects.append({"item_id": item["item_id"], "review_payload": payload})
    return subjects


def build_carla_stage_calibration_reviewer_subjects(
    calibration_roster_source: Any,
    query_library_source: Any,
    oracle_source: Any,
    response_records_source: Any,
    evidence_records_source: Any,
) -> List[Dict[str, Any]]:
    """Build 90 CARLA-stage views without claiming final calibration."""

    roster, _, _ = _materialize_json_source(
        calibration_roster_source, "CARLA-stage calibration roster"
    )
    validate_human_document(roster, "carla_stage_calibration_roster")
    _assert_calibration_roster_integrity(roster)
    return _build_calibration_reviewer_subjects_from_roster(
        roster,
        query_library_source,
        oracle_source,
        response_records_source,
        evidence_records_source,
    )


def export_carla_stage_judge_calibration_review_bundle(
    calibration_roster_source: Any,
    reviewer_subject_source: Any,
    *,
    reviewer_id: str,
) -> Dict[str, Any]:
    """Export exactly 90 CARLA items as a non-formal calibration stage."""

    roster, roster_sha256, roster_scope = _materialize_json_source(
        calibration_roster_source, "CARLA-stage calibration roster"
    )
    validate_human_document(roster, "carla_stage_calibration_roster")
    _assert_calibration_roster_integrity(roster)
    subject_rows, subject_sha256, subject_scope = _materialize_jsonl_source(
        reviewer_subject_source, "CARLA-stage blind reviewer subjects"
    )
    by_item = {}
    for row in subject_rows:
        item_id = row.get("item_id")
        payload = row.get("review_payload")
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id in by_item
            or not isinstance(payload, Mapping)
            or not payload
            or set(row) != {"item_id", "review_payload"}
        ):
            raise ValidationError("CARLA-stage calibration subject row is malformed")
        _assert_calibration_blind(payload)
        by_item[item_id] = payload
    roster_items = {item["item_id"]: item for item in roster["items"]}
    if len(roster_items) != 90 or set(by_item) != set(roster_items):
        raise ValidationError(
            "CARLA-stage blind subjects must exactly cover the 90-item roster"
        )
    subjects = []
    for item_id, roster_item in sorted(roster_items.items()):
        if roster_item.get("platform") != "carla":
            raise ValidationError("CARLA-stage roster contains another platform")
        subject = {
            "roster_item": copy.deepcopy(roster_item),
            "review_payload": _json_copy(
                by_item[item_id], "CARLA-stage calibration payload"
            ),
            "label_instruction": (
                "Independently label satisfied, violated, or unknown from the supplied "
                "evidence; no Judge prediction or machine-suggested verdict is included."
            ),
        }
        _assert_calibration_blind(subject)
        subjects.append({"subject_id": item_id, "subject": subject})
    binding = {
        "calibration_roster_sha256": roster_sha256,
        "calibration_roster_hash_scope": roster_scope,
        "reviewer_subjects_sha256": subject_sha256,
        "reviewer_subjects_hash_scope": subject_scope,
        "item_count": len(subjects),
        "judge_prediction_exposed": False,
        "review_scope": "carla_stage_only",
        "stage_asset": True,
        "formal_freeze_eligible": False,
        "included_platforms": ["carla"],
        "deferred_platforms": ["metadrive"],
    }
    bundle = _export_single_bound_review_bundle(
        CALIBRATION_REVIEW_WORKFLOW,
        binding,
        subjects,
        reviewer_id=reviewer_id,
    )
    _assert_calibration_blind(bundle)
    return bundle


def _calibration_label(
    response: Mapping[str, Any], reviewer_id: str
) -> Dict[str, Any]:
    _validate_bound_response(CALIBRATION_REVIEW_WORKFLOW, response)
    return {
        "reviewer_id": reviewer_id,
        "verdict": response["verdict"],
        "evidence": copy.deepcopy(response["evidence"]),
        "rationale": response["rationale"],
    }


def _materialize_carla_stage_judge_calibration_review(
    bundle: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    calibration_roster_source: Any,
    reviewer_subject_source: Any,
) -> Dict[str, Any]:
    binding = bundle.get("source_binding", {})
    if (
        binding.get("review_scope") != "carla_stage_only"
        or binding.get("stage_asset") is not True
        or binding.get("formal_freeze_eligible") is not False
    ):
        raise ValidationError("not a CARLA-only calibration stage bundle")
    if bundle.get("review_policy") != "one_complete_human_gold_per_subject":
        raise ValidationError("CARLA calibration bundle does not use the single-gold policy")
    _assert_calibration_blind(bundle)
    packet = bundle.get("reviewer_packet")
    if (
        bundle.get("workflow_type") != CALIBRATION_REVIEW_WORKFLOW
        or not isinstance(packet, Mapping)
        or packet.get("source_binding") != binding
    ):
        raise ValidationError("CARLA calibration bundle lacks its reviewer packet")
    expected_bundle = export_carla_stage_judge_calibration_review_bundle(
        calibration_roster_source,
        reviewer_subject_source,
        reviewer_id=packet.get("reviewer_id"),
    )
    _assert_canonical_review_bundle(bundle, expected_bundle, "CARLA calibration")
    validated = validate_bound_review_submission(packet, submission)
    responses = {
        item["subject_id"]: item["response"] for item in validated["responses"]
    }
    task_by_id = {task["subject_id"]: task for task in packet["tasks"]}
    records = []
    for item_id, task in sorted(task_by_id.items()):
        label = _calibration_label(responses[item_id], validated["reviewer_id"])
        record = {
            "schema_version": SCHEMA_VERSION,
            "workflow_type": "judge_calibration_human_gold",
            **copy.deepcopy(task["subject"]["roster_item"]),
            "reviewer_id": label["reviewer_id"],
            "gold_verdict": label["verdict"],
            "gold_evidence": label["evidence"],
            "gold_rationale": label["rationale"],
            "review_status": "complete",
            "canonical_source_revalidated": True,
            "reviewer_blind_to_judge_prediction": True,
            "decision_status": "confirmed",
        }
        record["gold_record_id"] = _json_sha256(record)
        validate_human_document(record, "calibration_gold_record")
        records.append(record)
    document = {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": "carla_stage_judge_calibration_finalized",
        "stage_scope": "carla_first",
        "included_platforms": ["carla"],
        "deferred_platforms": ["metadrive"],
        "formal_freeze_eligible": False,
        "decision_status": "stage_confirmed",
        "review_policy": "one_complete_human_gold_per_subject",
        "canonical_bundle_revalidated": True,
        "source_binding": copy.deepcopy(binding),
        "human_gold_records": records,
    }
    validate_human_document(document, "carla_stage_calibration_report")
    return document


def validate_carla_stage_calibration_report(
    report: Mapping[str, Any],
    bundle: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    calibration_roster_source: Any,
    reviewer_subject_source: Any,
) -> Mapping[str, Any]:
    """Exact-rebuild a CARLA calibration report from blind-review sources."""

    validate_human_document(report, "carla_stage_calibration_report")
    expected = _materialize_carla_stage_judge_calibration_review(
        bundle,
        submission,
        calibration_roster_source=calibration_roster_source,
        reviewer_subject_source=reviewer_subject_source,
    )
    if canonical_json_bytes(report) != canonical_json_bytes(expected):
        raise ValidationError(
            "CARLA calibration report differs from exact source-bound reconstruction"
        )
    return report


def finalize_carla_stage_judge_calibration_review(
    bundle: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    calibration_roster_source: Any,
    reviewer_subject_source: Any,
) -> Dict[str, Any]:
    report = _materialize_carla_stage_judge_calibration_review(
        bundle,
        submission,
        calibration_roster_source=calibration_roster_source,
        reviewer_subject_source=reviewer_subject_source,
    )
    validate_carla_stage_calibration_report(
        report,
        bundle,
        submission,
        calibration_roster_source=calibration_roster_source,
        reviewer_subject_source=reviewer_subject_source,
    )
    return report


def _score_band(value: Any) -> str:
    if value is None:
        return "unscored"
    if type(value) not in (int, float) or not isfinite(float(value)):
        raise ValidationError("post-test SRS must be a finite number or null")
    if not 0.0 <= float(value) <= 1.0:
        raise ValidationError("post-test SRS must be in [0, 1]")
    if value < 0.4:
        return "low"
    if value < 0.8:
        return "mid"
    return "high"


def _assert_method_blind_payload(value: Any, path: str = "review_payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in _BLIND_IDENTITY_KEYS:
                raise ValidationError(
                    "method-blind payload exposes {} at {}".format(key, path)
                )
            _assert_method_blind_payload(child, "{}.{}".format(path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_method_blind_payload(child, "{}[{}]".format(path, index))


def _post_test_order_key(
    seed: int, population_sha256: str, record: Mapping[str, Any]
) -> str:
    return _json_sha256(
        {
            "seed": seed,
            "population_sha256": population_sha256,
            "run_id": record["run_id"],
            "query_id": record["query_id"],
        }
    )


def build_method_blind_post_test_bundle(
    population_source: Any, *, rate: float = 0.10, seed: int = 0
) -> Dict[str, Any]:
    """Select by private method strata and return separate public/private bundles.

    Each source record must contain a ``review_payload`` with the query text and
    reviewer-facing artifact/evidence.  Identity-bearing fields are rejected at
    every depth of that payload.  Method, run, platform, SRS, and stratum remain
    exclusively in the private linkage document.
    """

    if (
        type(rate) not in (int, float)
        or not isfinite(float(rate))
        or not 0.10 <= float(rate) <= 1
    ):
        raise ValidationError("post-test audit rate must be in [0.10, 1]")
    if type(seed) is not int:
        raise ValidationError("post-test audit seed must be an integer")
    records, population_sha256, population_hash_scope = _materialize_jsonl_source(
        population_source, "post-test audit population"
    )
    run_ids = set()
    by_method = defaultdict(list)
    for record in records:
        for field in (
            "run_id",
            "method_id",
            "query_id",
            "platform",
            "surface_style",
            "terminal_status",
        ):
            if not isinstance(record.get(field), str) or not record[field]:
                raise ValidationError("post-test record lacks {}".format(field))
        if record["run_id"] in run_ids:
            raise ValidationError("post-test audit population repeats a run_id")
        run_ids.add(record["run_id"])
        payload = record.get("review_payload")
        if not isinstance(payload, Mapping):
            raise ValidationError("post-test record lacks reviewer-facing payload")
        if not isinstance(payload.get("query_text"), str) or not payload["query_text"]:
            raise ValidationError("post-test review payload lacks query_text")
        _assert_method_blind_payload(payload)
        _score_band(record.get("srs"))
        by_method[record["method_id"]].append(record)

    selected = []
    method_targets = {}
    allocations = defaultdict(int)
    for method_id, method_records in sorted(by_method.items()):
        target = ceil(len(method_records) * rate)
        method_targets[method_id] = target
        strata = defaultdict(list)
        for record in method_records:
            stratum = (
                record["platform"],
                record["surface_style"],
                record["terminal_status"],
                _score_band(record.get("srs")),
            )
            strata[stratum].append(record)
        method_selected = []
        for _, members in sorted(strata.items(), key=lambda item: str(item[0])):
            allocation = max(1, round(len(members) * rate))
            ordered = sorted(
                members,
                key=lambda record: _post_test_order_key(
                    seed, population_sha256, record
                ),
            )
            method_selected.extend(ordered[: min(allocation, len(ordered))])
        if len(method_selected) > target:
            method_selected = sorted(
                method_selected,
                key=lambda record: _post_test_order_key(
                    seed, population_sha256, record
                ),
            )[:target]
        elif len(method_selected) < target:
            chosen = {record["run_id"] for record in method_selected}
            remaining = [
                record for record in method_records if record["run_id"] not in chosen
            ]
            remaining.sort(
                key=lambda record: _post_test_order_key(
                    seed, population_sha256, record
                )
            )
            method_selected.extend(remaining[: target - len(method_selected)])
        for record in method_selected:
            stratum = (
                record["platform"],
                record["surface_style"],
                record["terminal_status"],
                _score_band(record.get("srs")),
            )
            allocations["|".join((method_id,) + stratum)] += 1
        selected.extend(method_selected)

    public_tasks = []
    private_links = []
    blind_ids = set()
    for record in selected:
        source_record_sha256 = _json_sha256(record)
        blind_review_id = _json_sha256(
            {
                "workflow_type": POST_TEST_WORKFLOW,
                "seed": seed,
                "population_sha256": population_sha256,
                "run_id": record["run_id"],
            }
        )
        if blind_review_id in blind_ids:
            raise ValidationError("post-test blind review ID collision")
        blind_ids.add(blind_review_id)
        task = {
            "blind_review_id": blind_review_id,
            "query_id": record["query_id"],
            "surface_style": record["surface_style"],
            "terminal_status": record["terminal_status"],
            "review_payload": _json_copy(record["review_payload"], "review payload"),
            "response_template": {
                "review_status": "pending",
                "human_srs": None,
                "human_arc": None,
                "human_rsc": None,
                "human_iec_spec": None,
                "error_taxonomy": [],
                "notes": "",
            },
        }
        _assert_method_blind_payload(task, path="public_task")
        public_tasks.append(task)
        private_links.append(
            {
                "blind_review_id": blind_review_id,
                "run_id": record["run_id"],
                "method_id": record["method_id"],
                "query_id": record["query_id"],
                "platform": record["platform"],
                "source_record_sha256": source_record_sha256,
                "public_task_sha256": _json_sha256(task),
                "stratum": {
                    "platform": record["platform"],
                    "surface_style": record["surface_style"],
                    "terminal_status": record["terminal_status"],
                    "score_band": _score_band(record.get("srs")),
                },
            }
        )
    public_tasks.sort(key=lambda task: task["blind_review_id"])
    private_links.sort(key=lambda link: link["blind_review_id"])
    public = {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": POST_TEST_WORKFLOW,
        "review_policy": "one_complete_human_gold_per_subject",
        "selection_algorithm": POST_TEST_SELECTION_ALGORITHM,
        "population_sha256": population_sha256,
        "population_hash_scope": population_hash_scope,
        "rate": float(rate),
        "seed": seed,
        "method_blind": True,
        "review_status": "pending",
        "selected": len(public_tasks),
        "tasks": public_tasks,
    }
    public["packet_id"] = _json_sha256(public)
    private = {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": POST_TEST_WORKFLOW,
        "public_packet_id": public["packet_id"],
        "population_sha256": population_sha256,
        "population_hash_scope": population_hash_scope,
        "selection_algorithm": POST_TEST_SELECTION_ALGORITHM,
        "rate": float(rate),
        "seed": seed,
        "population_count": len(records),
        "selected": len(private_links),
        "method_targets": dict(method_targets),
        "allocations": dict(allocations),
        "links": private_links,
    }
    validate_human_document(public, "post_test_public")
    validate_human_document(private, "post_test_private")
    _assert_method_blind_payload(public, path="public_bundle")
    return {"public_tasks": public, "private_linkage": private}


def build_post_test_submission_template(
    public_packet: Mapping[str, Any], reviewer_id: str
) -> Dict[str, Any]:
    validate_human_document(public_packet, "post_test_public")
    if not isinstance(reviewer_id, str) or not reviewer_id:
        raise ValidationError("post-test reviewer ID must be non-empty")
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": POST_TEST_WORKFLOW,
        "public_packet_id": public_packet["packet_id"],
        "reviewer_id": reviewer_id,
        "review_assignment_policy": "single_reviewer_final",
        "submission_status": "pending",
        "responses": [
            {
                "blind_review_id": task["blind_review_id"],
                "review_status": "pending",
                "human_srs": None,
                "human_arc": None,
                "human_rsc": None,
                "human_iec_spec": None,
                "error_taxonomy": [],
                "notes": "",
            }
            for task in public_packet["tasks"]
        ],
    }


def validate_post_test_submission(
    public_packet: Mapping[str, Any], submission: Mapping[str, Any]
) -> Dict[str, Any]:
    validate_human_document(public_packet, "post_test_public")
    validate_human_document(submission, "post_test_submission")
    expected_packet_id = _json_sha256(
        {key: value for key, value in public_packet.items() if key != "packet_id"}
    )
    if public_packet.get("packet_id") != expected_packet_id:
        raise ValidationError("post-test public packet ID does not match its content")
    if (
        submission.get("public_packet_id") != public_packet["packet_id"]
        or submission.get("workflow_type") != public_packet["workflow_type"]
    ):
        raise ValidationError("post-test submission binds a different public packet")
    task_ids = {task["blind_review_id"] for task in public_packet["tasks"]}
    response_ids = [item["blind_review_id"] for item in submission["responses"]]
    if len(set(response_ids)) != len(response_ids) or set(response_ids) != task_ids:
        raise ValidationError("post-test submission must cover every blind task exactly once")
    _assert_method_blind_payload(submission, path="post_test_submission")
    return _json_copy(submission, "post-test submission")


def _audited_metric(value: Any) -> Any:
    if type(value) not in (int, float) or not isfinite(float(value)):
        return None
    value = float(value)
    return value if 0.0 <= value <= 1.0 else None


def _post_test_gold_record_id(record: Mapping[str, Any]) -> str:
    return _json_sha256(
        {key: value for key, value in record.items() if key != "gold_record_id"}
    )


def validate_post_test_audit_report(
    report: Mapping[str, Any],
    public_packet: Mapping[str, Any],
    private_linkage: Mapping[str, Any],
    submission: Mapping[str, Any],
    population_source: Any,
) -> Mapping[str, Any]:
    """Exact-rebuild a post-test report from all source-bound inputs."""

    validate_human_document(report, "post_test_report")
    expected = _materialize_post_test_audit_report(
        public_packet,
        private_linkage,
        submission,
        population_source,
    )
    try:
        matches = canonical_json_bytes(report) == canonical_json_bytes(expected)
    except (TypeError, ValueError) as exc:
        raise ValidationError("post-test report is not canonical JSON") from exc
    if not matches:
        raise ValidationError(
            "post-test report differs from exact source-bound reconstruction"
        )
    return report


def _materialize_post_test_audit_report(
    public_packet: Mapping[str, Any],
    private_linkage: Mapping[str, Any],
    submission: Mapping[str, Any],
    population_source: Any,
) -> Dict[str, Any]:
    """Build one source-bound post-test report without trusting a prior report."""

    validated = validate_post_test_submission(public_packet, submission)
    if any(item["review_status"] != "complete" for item in validated["responses"]):
        raise ValidationError(
            "uncertain post-test review is not human gold; every subject must be complete"
        )
    validate_human_document(private_linkage, "post_test_private")
    if private_linkage.get("public_packet_id") != public_packet["packet_id"]:
        raise ValidationError("post-test private linkage binds a different public packet")
    for field in (
        "population_sha256",
        "population_hash_scope",
        "selection_algorithm",
        "rate",
        "seed",
        "selected",
    ):
        if private_linkage.get(field) != public_packet.get(field):
            raise ValidationError(
                "post-test public/private {} binding differs".format(field)
            )
    if float(public_packet["rate"]) < 0.10:
        raise ValidationError("post-test audit rate is below the 10% minimum")
    rebuilt = build_method_blind_post_test_bundle(
        population_source,
        rate=float(public_packet["rate"]),
        seed=public_packet["seed"],
    )
    if (
        public_packet != rebuilt["public_tasks"]
        or private_linkage != rebuilt["private_linkage"]
    ):
        raise ValidationError(
            "post-test sample does not match deterministic rate/seed recomputation"
        )
    records, population_sha256, population_scope = _materialize_jsonl_source(
        population_source, "post-test audit population"
    )
    if (
        private_linkage.get("population_sha256") != population_sha256
        or private_linkage.get("population_hash_scope") != population_scope
        or private_linkage.get("population_count") != len(records)
    ):
        raise ValidationError("post-test population differs from private linkage")
    source_by_run = {}
    for record in records:
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id in source_by_run:
            raise ValidationError("post-test population has invalid run IDs")
        source_by_run[run_id] = record
    tasks = {task["blind_review_id"]: task for task in public_packet["tasks"]}
    links = {link["blind_review_id"]: link for link in private_linkage["links"]}
    responses = {item["blind_review_id"]: item for item in validated["responses"]}
    if (
        len(links) != len(private_linkage["links"])
        or set(tasks) != set(links)
        or set(tasks) != set(responses)
    ):
        raise ValidationError("post-test public/private/submission coverage differs")
    human_gold_records = []
    finalized = []
    by_method_records = defaultdict(list)
    metric_fields = {
        "srs": "human_srs",
        "arc": "human_arc",
        "rsc": "human_rsc",
        "iec_spec": "human_iec_spec",
    }
    for blind_id in sorted(tasks):
        task = tasks[blind_id]
        link = links[blind_id]
        response = responses[blind_id]
        source = source_by_run.get(link["run_id"])
        if source is None:
            raise ValidationError("post-test private link references a missing run")
        if (
            link["source_record_sha256"] != _json_sha256(source)
            or link["public_task_sha256"] != _json_sha256(task)
            or source.get("method_id") != link["method_id"]
            or source.get("query_id") != link["query_id"]
            or source.get("platform") != link["platform"]
        ):
            raise ValidationError("post-test private linkage is stale or forged")
        gold_record = {
            "schema_version": SCHEMA_VERSION,
            "workflow_type": "post_test_method_blind_human_gold",
            "blind_review_id": blind_id,
            "subject_sha256": link["public_task_sha256"],
            "query_id": task["query_id"],
            "surface_style": task["surface_style"],
            "terminal_status": task["terminal_status"],
            "reviewer_id": validated["reviewer_id"],
            "review_status": "complete",
            "decision_status": "confirmed",
            "reviewer_method_blind": True,
            "canonical_source_revalidated": True,
            "source_binding": {
                "public_packet_id": public_packet["packet_id"],
                "population_sha256": population_sha256,
                "population_hash_scope": population_scope,
                "private_link_sha256": _json_sha256(link),
                "source_record_sha256": link["source_record_sha256"],
                "review_response_sha256": _json_sha256(response),
            },
            "human_srs": float(response["human_srs"]),
            "human_arc": float(response["human_arc"]),
            "human_rsc": float(response["human_rsc"]),
            "human_iec_spec": float(response["human_iec_spec"]),
            "error_taxonomy": copy.deepcopy(response["error_taxonomy"]),
            "notes": response["notes"],
        }
        gold_record["gold_record_id"] = _post_test_gold_record_id(gold_record)
        human_gold_records.append(gold_record)
        errors = {}
        for automated_field, human_field in metric_fields.items():
            automatic = _audited_metric(source.get(automated_field))
            human = gold_record[human_field]
            errors[automated_field] = {
                "automatic": automatic,
                "human": human,
                "signed_error": None if automatic is None else automatic - human,
                "absolute_error": None if automatic is None else abs(automatic - human),
            }
        record = {
            "human_gold_record_id": gold_record["gold_record_id"],
            "blind_review_id": blind_id,
            "run_id": link["run_id"],
            "method_id": link["method_id"],
            "query_id": link["query_id"],
            "platform": link["platform"],
            "stratum": copy.deepcopy(link["stratum"]),
            "reviewer_id": validated["reviewer_id"],
            "review_status": response["review_status"],
            "metric_errors": errors,
            "error_taxonomy": copy.deepcopy(response["error_taxonomy"]),
            "notes": response["notes"],
        }
        finalized.append(record)
        by_method_records[link["method_id"]].append(record)
    by_method = {}
    for method_id, method_records in sorted(by_method_records.items()):
        metric_summary = {}
        for metric in metric_fields:
            comparable = [
                record["metric_errors"][metric]
                for record in method_records
                if record["metric_errors"][metric]["automatic"] is not None
            ]
            metric_summary[metric] = {
                "comparable_count": len(comparable),
                "mean_signed_error": (
                    sum(item["signed_error"] for item in comparable) / len(comparable)
                    if comparable
                    else None
                ),
                "mean_absolute_error": (
                    sum(item["absolute_error"] for item in comparable) / len(comparable)
                    if comparable
                    else None
                ),
            }
        taxonomy = Counter(
            label for record in method_records for label in record["error_taxonomy"]
        )
        by_method[method_id] = {
            "sample_count": len(method_records),
            "uncertain_count": 0,
            "metric_error": metric_summary,
            "error_taxonomy_counts": dict(sorted(taxonomy.items())),
        }
    report = {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": "post_test_method_blind_audit_report",
        "review_policy": "one_complete_human_gold_per_subject",
        "public_packet_id": public_packet["packet_id"],
        "reviewer_id": validated["reviewer_id"],
        "review_status": "complete",
        "canonical_bundle_revalidated": True,
        "decision_status": "confirmed",
        "selected": len(finalized),
        "human_gold_records": human_gold_records,
        "records": finalized,
        "by_method": by_method,
    }
    validate_human_document(report, "post_test_report")
    return report


def finalize_post_test_audit(
    public_packet: Mapping[str, Any],
    private_linkage: Mapping[str, Any],
    submission: Mapping[str, Any],
    population_source: Any,
) -> Dict[str, Any]:
    """Finalize and independently exact-rebuild a post-test audit report."""

    stable_population_source = population_source
    if not isinstance(population_source, (str, Path)):
        if isinstance(population_source, (Mapping, bytes, bytearray)):
            raise ValidationError(
                "post-test audit population must be a JSONL path or iterable of records"
            )
        try:
            stable_population_source = list(population_source)
        except TypeError as exc:
            raise ValidationError("post-test audit population is not iterable") from exc
    report = _materialize_post_test_audit_report(
        public_packet,
        private_linkage,
        submission,
        stable_population_source,
    )
    validate_post_test_audit_report(
        report,
        public_packet,
        private_linkage,
        submission,
        stable_population_source,
    )
    return report


__all__ = [
    "build_carla_stage_calibration_reviewer_subjects",
    "build_method_blind_post_test_bundle",
    "build_post_test_submission_template",
    "export_carla_stage_judge_calibration_review_bundle",
    "export_carla_stage_platform_review_bundle",
    "export_query_review_bundle",
    "finalize_carla_stage_judge_calibration_review",
    "finalize_carla_stage_platform_review",
    "finalize_post_test_audit",
    "finalize_query_review_bundle",
    "refresh_human_work_inventory",
    "validate_bound_review_submission",
    "validate_carla_stage_calibration_report",
    "validate_carla_stage_platform_report",
    "validate_human_document",
    "validate_post_test_submission",
    "validate_post_test_audit_report",
    "validate_query_review_submission",
]
