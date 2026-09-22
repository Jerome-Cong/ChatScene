"""Query-library validation and development/test overlap auditing."""

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

from .atoms import normalize_count
from .constants import EXPECTED_SUPPORT, SURFACE_STYLES
from .errors import ValidationError
from .jsonio import read_jsonl, sha256_file


REQUIRED_FIELDS = {
    "query_id",
    "intent_group_id",
    "group_label",
    "subset",
    "query_library_version",
    "metadata_schema_version",
    "language",
    "query_text",
    "surface_style",
    "canonical_query",
    "view_mode",
    "benchmark_scope",
    "requested_ego_actor",
    "bus_operation_phase",
    "bus_stop_type",
    "road_topology",
    "lane_configuration",
    "counterpart_actors",
    "interaction_events",
    "event_temporal_structure",
    "risk_level",
    "rule_mode",
    "rule_hooks",
    "expected_support",
    "expected_reason_if_unsupported",
    "acceptable_response",
    "background_density",
    "queue_state",
    "evaluation_tags",
    "source_inventory",
    "created_date",
}

V02_REQUIRED_FIELDS = {
    "dataset_split",
    "active_workflow_status",
    "supersedes_query_id",
    "supersedes_intent_group_id",
    "tuning_allowed",
    "locked_test_usage",
    "surface_template_family",
    "unsupported_constraints",
    "core_event_signature",
    "policy_diagnostic_ref",
    "revised_date",
}

V02_DEV_REQUIRED_FIELDS = {
    "source_locked_test_library",
    "locked_test_novelty_note",
}

RULE_HOOK_SCOPES = (
    "scene_rules",
    "counterpart_actor_rules",
    "counterpart_actor_violations",
)

V02_SURFACE_TEMPLATE_FAMILIES = {
    "precise": "constraint_case_specification",
    "partial": "compact_scenario_brief",
    "vague": "natural_need_statement_or_fragment",
}

V02_EGO_REACTION_EVENT_PATTERN = re.compile(
    r"^(?:ego(?:_bus)?|bus)_", re.IGNORECASE
)

V02_STATIC_BUS_SCENE_RULE_PREFIXES = (
    "bus_bay_",
    "bus_lane_",
    "bus_stop_",
)

V02_REACTION_TEXT_PATTERN = re.compile(
    r"(?:forc(?:e|es|ing)|caus(?:e|es|ing)|requir(?:e|es|ing))\s+"
    r"(?:the\s+)?(?:ego\s+)?bus\s+to\s+"
    r"(?:brake|wait|yield|stop|avoid|abort|swerve|slow\s+down|decelerate)|"
    r"(?:the\s+)?(?:ego\s+)?bus\s+"
    r"(?:must|should|has\s+to|needs?\s+to|will)\s+"
    r"(?:brake|wait|yield|stop|avoid|abort|swerve|slow\s+down|decelerate)|"
    r"make(?:s|ing)?\s+(?:the\s+)?(?:ego\s+)?bus\s+"
    r"(?:brake|wait|yield|stop|avoid|abort|swerve|slow\s+down|decelerate)|"
    r"(?:the\s+)?(?:ego\s+)?bus\s+"
    r"(?:brakes?|braking|yields?|yielding|waits?|waiting|avoids?|avoiding|"
    r"aborts?|aborting|swerves?|swerving|slows?\s+down|slowing\s+down|"
    r"decelerates?|decelerating|reacts?\s+by)\b",
    re.IGNORECASE,
)

GROUP_VARIANT_FIELDS = {
    "query_id",
    "query_text",
    "surface_style",
    "surface_template_family",
    "supersedes_query_id",
}


def _require_type(record: Mapping[str, Any], field: str, expected: type) -> None:
    if not isinstance(record.get(field), expected):
        raise ValidationError(
            "{} must be {}, got {}".format(
                field, expected.__name__, type(record.get(field)).__name__
            )
        )


def _require_string_list(
    record: Mapping[str, Any], field: str, line_number: int, unique: bool = False
) -> None:
    _require_type(record, field, list)
    values = record[field]
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValidationError(
            "line {} {} must contain non-empty strings".format(line_number, field)
        )
    if unique and len(set(values)) != len(values):
        raise ValidationError("line {} {} must not contain duplicates".format(line_number, field))


def flatten_rule_hooks(rule_hooks: Any) -> Tuple[Tuple[str, int, str], ...]:
    """Return rule hooks in a stable scope/list order.

    v0.2 assigns rule semantics to three explicit scopes.  The fixed scope order
    keeps hashes and semantic-feature extraction independent of JSON object key
    insertion order.  Legacy v0.1 lists remain readable as unscoped metadata.
    """

    if isinstance(rule_hooks, list):
        if any(not isinstance(value, str) or not value.strip() for value in rule_hooks):
            raise ValidationError("legacy rule_hooks must contain non-empty strings")
        return tuple(("legacy_unscoped", index, value) for index, value in enumerate(rule_hooks))
    if not isinstance(rule_hooks, dict):
        raise ValidationError("rule_hooks must be a list for v0.1 or an object for v0.2")
    if set(rule_hooks) != set(RULE_HOOK_SCOPES):
        raise ValidationError(
            "v0.2 rule_hooks must contain exactly: {}".format(", ".join(RULE_HOOK_SCOPES))
        )
    flattened = []
    for scope in RULE_HOOK_SCOPES:
        hooks = rule_hooks[scope]
        if not isinstance(hooks, list):
            raise ValidationError("rule_hooks.{} must be a list".format(scope))
        if any(not isinstance(value, str) or not value.strip() for value in hooks):
            raise ValidationError(
                "rule_hooks.{} must contain non-empty strings".format(scope)
            )
        if len(set(hooks)) != len(hooks):
            raise ValidationError("rule_hooks.{} must not contain duplicates".format(scope))
        flattened.extend((scope, index, value) for index, value in enumerate(hooks))
    return tuple(flattened)


def _validate_v02_rule_hook_subjects(
    flattened_hooks: Tuple[Tuple[str, int, str], ...], line_number: int
) -> None:
    """Reject rule hooks which assign reactive behavior to the ego bus.

    A ``bus_*`` hook is only valid in the scene scope when it names a static
    bus-bay, bus-lane, or bus-stop facility.  Counterpart scopes must start
    from the counterpart actor, while ``ego_*`` is forbidden in every scope.
    This is deliberately a closed structural guard; the immutable release
    hashes remain the authority for the curated natural-language semantics.
    """

    for scope, _, value in flattened_hooks:
        normalized = value.strip().lower()
        if normalized.startswith(("ego_", "ego_bus_")):
            raise ValidationError(
                "line {} rule_hooks must not assign rules to the ego bus: {}".format(
                    line_number, value
                )
            )
        if normalized.startswith("bus_"):
            is_static_scene_rule = scope == "scene_rules" and normalized.startswith(
                V02_STATIC_BUS_SCENE_RULE_PREFIXES
            )
            if not is_static_scene_rule:
                raise ValidationError(
                    "line {} rule_hooks must not assign reactive behavior to the bus: {}".format(
                        line_number, value
                    )
                )


def _validate_v02_record(record: Mapping[str, Any], line_number: int) -> None:
    missing = sorted(V02_REQUIRED_FIELDS - set(record))
    if missing:
        raise ValidationError(
            "line {} missing v0.2 fields: {}".format(line_number, ", ".join(missing))
        )
    split = record.get("dataset_split")
    if split not in ("development", "test"):
        raise ValidationError("line {} has invalid dataset_split".format(line_number))
    if split == "development":
        missing = sorted(V02_DEV_REQUIRED_FIELDS - set(record))
        if missing:
            raise ValidationError(
                "line {} missing v0.2 development fields: {}".format(
                    line_number, ", ".join(missing)
                )
            )
        expected_library = "BSG_DEV_v0_2"
        current_prefix = "BSG_DEV_v0_2_"
        superseded_prefix = "BSG_DEV_v0_1_"
        expected_tuning = True
        expected_locked_test_usage = "design_visible_reference_no_post_freeze_tuning"
        if record.get("source_locked_test_library") != "BSG_v0_2":
            raise ValidationError(
                "line {} has invalid source_locked_test_library".format(line_number)
            )
        if not isinstance(record.get("locked_test_novelty_note"), str) or not record[
            "locked_test_novelty_note"
        ].strip():
            raise ValidationError(
                "line {} has empty locked_test_novelty_note".format(line_number)
            )
    else:
        expected_library = "BSG_v0_2"
        current_prefix = "BSG_v0_2_"
        superseded_prefix = "BSG_v0_1_"
        expected_tuning = False
        expected_locked_test_usage = "held_out_test_only_not_for_tuning"

    for field in (
        "dataset_split",
        "active_workflow_status",
        "supersedes_query_id",
        "supersedes_intent_group_id",
        "locked_test_usage",
        "surface_template_family",
        "policy_diagnostic_ref",
        "revised_date",
    ):
        _require_type(record, field, str)
        if not record[field].strip():
            raise ValidationError("line {} has empty {}".format(line_number, field))
    if record.get("query_library_version") != expected_library:
        raise ValidationError("line {} has invalid v0.2 query_library_version".format(line_number))
    if record.get("active_workflow_status") != "active":
        raise ValidationError("line {} is not an active v0.2 record".format(line_number))
    if record.get("locked_test_usage") != expected_locked_test_usage:
        raise ValidationError("line {} has invalid locked_test_usage".format(line_number))
    if (
        not isinstance(record.get("tuning_allowed"), bool)
        or record["tuning_allowed"] is not expected_tuning
    ):
        raise ValidationError("line {} has invalid tuning_allowed".format(line_number))
    if record.get("surface_template_family") != V02_SURFACE_TEMPLATE_FAMILIES.get(
        record.get("surface_style")
    ):
        raise ValidationError("line {} has invalid surface_template_family".format(line_number))

    query_id = record["query_id"]
    group_id = record["intent_group_id"]
    style = record["surface_style"]
    if not query_id.startswith(current_prefix) or query_id != "{}_{}".format(
        group_id, style
    ):
        raise ValidationError(
            "line {} query_id does not match its v0.2 group/style".format(line_number)
        )
    if not group_id.startswith(current_prefix):
        raise ValidationError("line {} has invalid v0.2 intent_group_id".format(line_number))
    expected_superseded_query = query_id.replace(current_prefix, superseded_prefix, 1)
    expected_superseded_group = group_id.replace(current_prefix, superseded_prefix, 1)
    if record["supersedes_query_id"] != expected_superseded_query:
        raise ValidationError("line {} has invalid supersedes_query_id".format(line_number))
    if record["supersedes_intent_group_id"] != expected_superseded_group:
        raise ValidationError("line {} has invalid supersedes_intent_group_id".format(line_number))
    if record["policy_diagnostic_ref"] != "PD_{}".format(group_id):
        raise ValidationError("line {} has invalid policy_diagnostic_ref".format(line_number))

    if not isinstance(record.get("rule_hooks"), dict):
        raise ValidationError(
            "line {} v0.2 rule_hooks must be a scoped object".format(line_number)
        )
    try:
        flattened_rule_hooks = flatten_rule_hooks(record["rule_hooks"])
    except ValidationError as error:
        raise ValidationError("line {} {}".format(line_number, error))
    _validate_v02_rule_hook_subjects(flattened_rule_hooks, line_number)
    _require_string_list(record, "unsupported_constraints", line_number, unique=True)
    _require_string_list(record, "interaction_events", line_number, unique=True)
    if bool(record["unsupported_constraints"]) != (
        record["expected_support"] == "unsupported"
    ):
        raise ValidationError(
            "line {} unsupported_constraints disagree with expected_support".format(line_number)
        )

    for event in record["interaction_events"]:
        if V02_EGO_REACTION_EVENT_PATTERN.search(event):
            raise ValidationError(
                "line {} interaction_events must not specify ego reaction: {}".format(
                    line_number, event
                )
            )
    for field in ("query_text", "canonical_query"):
        if V02_REACTION_TEXT_PATTERN.search(record[field]):
            raise ValidationError(
                "line {} {} must not prescribe ego reaction".format(
                    line_number, field
                )
            )

    _require_type(record, "core_event_signature", dict)
    expected_signature = {
        "operation": record["bus_operation_phase"],
        "topology": record["road_topology"],
        "actor_roles": [
            "{}:{}".format(actor["type"], actor["role"])
            for actor in record["counterpart_actors"]
        ],
        "events": list(record["interaction_events"]),
        "temporal_structure": record["event_temporal_structure"],
        "support": record["expected_support"],
    }
    if record["core_event_signature"] != expected_signature:
        raise ValidationError("line {} core_event_signature drift".format(line_number))


def _validate_v01_record(record: Mapping[str, Any], line_number: int) -> None:
    library_version = record.get("query_library_version")
    if library_version not in ("BSG_v0_1", "BSG_DEV_v0_1"):
        raise ValidationError("line {} has invalid v0.1 query_library_version".format(line_number))
    prefix = "{}_".format(library_version)
    if not record["intent_group_id"].startswith(prefix) or record["query_id"] != (
        "{}_{}".format(record["intent_group_id"], record["surface_style"])
    ):
        raise ValidationError(
            "line {} query_id does not match its v0.1 group/style".format(line_number)
        )
    if not isinstance(record["rule_hooks"], list):
        raise ValidationError("line {} v0.1 rule_hooks must be a list".format(line_number))
    try:
        flatten_rule_hooks(record["rule_hooks"])
    except ValidationError as error:
        raise ValidationError("line {} {}".format(line_number, error))


def validate_record(record: Mapping[str, Any], line_number: int = 0) -> None:
    missing = sorted(REQUIRED_FIELDS - set(record))
    if missing:
        raise ValidationError("line {} missing fields: {}".format(line_number, ", ".join(missing)))
    for field in (
        "query_id",
        "intent_group_id",
        "query_library_version",
        "metadata_schema_version",
        "query_text",
        "surface_style",
        "canonical_query",
        "expected_support",
    ):
        _require_type(record, field, str)
        if not record[field].strip():
            raise ValidationError("line {} has empty {}".format(line_number, field))
    if record["surface_style"] not in SURFACE_STYLES:
        raise ValidationError("line {} has invalid surface_style".format(line_number))
    if record["expected_support"] not in EXPECTED_SUPPORT:
        raise ValidationError("line {} has invalid expected_support".format(line_number))
    for field in (
        "counterpart_actors",
        "interaction_events",
        "acceptable_response",
        "evaluation_tags",
        "expected_reason_if_unsupported",
    ):
        _require_type(record, field, list)
    for field in ("benchmark_scope", "lane_configuration"):
        _require_type(record, field, dict)
    for index, actor in enumerate(record["counterpart_actors"]):
        if not isinstance(actor, dict) or not {"type", "count", "role"} <= set(actor):
            raise ValidationError(
                "line {} actor {} must contain type/count/role".format(line_number, index)
            )
        if not isinstance(actor["type"], str) or not isinstance(actor["role"], str):
            raise ValidationError("line {} actor type/role must be strings".format(line_number))
        if not actor["type"].strip() or not actor["role"].strip():
            raise ValidationError("line {} actor type/role must be non-empty".format(line_number))
        normalize_count(actor["count"])
    schema_version = record["metadata_schema_version"]
    if schema_version == "0.2":
        _validate_v02_record(record, line_number)
    elif schema_version == "0.1":
        _validate_v01_record(record, line_number)
    else:
        raise ValidationError(
            "line {} has unsupported metadata_schema_version {}".format(
                line_number, schema_version
            )
        )


def _group_invariant_projection(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in record.items() if key not in GROUP_VARIANT_FIELDS}


def validate_library(
    path: Path,
    expected_rows: int = None,
    expected_groups: int = None,
    expected_supported_rows: int = None,
) -> Dict[str, Any]:
    records = read_jsonl(Path(path))
    if not records:
        raise ValidationError("query library is empty")
    seen_ids = set()
    groups = defaultdict(list)
    for index, record in enumerate(records, 1):
        validate_record(record, index)
        query_id = record["query_id"]
        if query_id in seen_ids:
            raise ValidationError("duplicate query_id {}".format(query_id))
        seen_ids.add(query_id)
        groups[record["intent_group_id"]].append(record)
    metadata_versions = {record["metadata_schema_version"] for record in records}
    library_versions = {record["query_library_version"] for record in records}
    if len(metadata_versions) != 1 or len(library_versions) != 1:
        raise ValidationError("query library mixes metadata or library versions")
    for group_id, members in sorted(groups.items()):
        styles = [member["surface_style"] for member in members]
        if len(members) != 3 or set(styles) != set(SURFACE_STYLES) or len(set(styles)) != 3:
            raise ValidationError("{} is not one complete surface triplet".format(group_id))
        projection = _group_invariant_projection(members[0])
        for member in members[1:]:
            if _group_invariant_projection(member) != projection:
                raise ValidationError("{} contains non-surface metadata drift".format(group_id))
    supported_rows = sum(record["expected_support"] == "supported" for record in records)
    checks = (
        ("rows", len(records), expected_rows),
        ("groups", len(groups), expected_groups),
        ("supported_rows", supported_rows, expected_supported_rows),
    )
    for name, actual, expected in checks:
        if expected is not None and actual != expected:
            raise ValidationError("{} expected {}, got {}".format(name, expected, actual))
    return {
        "path": str(Path(path).resolve()),
        "sha256": sha256_file(Path(path)),
        "bytes": Path(path).stat().st_size,
        "records": len(records),
        "intent_groups": len(groups),
        "supported_records": supported_rows,
        "unsupported_records": len(records) - supported_rows,
        "supported_intents": sum(
            members[0]["expected_support"] == "supported" for members in groups.values()
        ),
        "unsupported_intents": sum(
            members[0]["expected_support"] == "unsupported" for members in groups.values()
        ),
        "surface_counts": dict(Counter(record["surface_style"] for record in records)),
        "metadata_schema_version": next(iter(metadata_versions)),
        "query_library_version": next(iter(library_versions)),
        "dataset_split": records[0].get("dataset_split"),
        "triplets_complete": True,
        "group_metadata_invariant": True,
    }


def _semantic_features(record: Mapping[str, Any]) -> Set[str]:
    features = {
        "operation:" + str(record.get("bus_operation_phase", "")),
        "topology:" + str(record.get("road_topology", "")),
        "temporal:" + str(record.get("event_temporal_structure", "")),
        "support:" + str(record.get("expected_support", "")),
    }
    features.update("event:" + str(value) for value in record.get("interaction_events", []))
    features.update(
        "rule:{}:{}".format(scope, value)
        for scope, _, value in flatten_rule_hooks(record.get("rule_hooks", []))
    )
    features.update(
        "actor:{}:{}".format(actor.get("type"), actor.get("role"))
        for actor in record.get("counterpart_actors", [])
    )
    return features


def _jaccard(left: Set[str], right: Set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def audit_dev_test_overlap(dev_path: Path, test_path: Path) -> Dict[str, Any]:
    dev_records = [r for r in read_jsonl(Path(dev_path)) if r["surface_style"] == "precise"]
    test_records = [r for r in read_jsonl(Path(test_path)) if r["surface_style"] == "precise"]
    exact_canonical = sorted(
        set(record["canonical_query"] for record in dev_records)
        & set(record["canonical_query"] for record in test_records)
    )
    nearest = []
    for dev in dev_records:
        dev_features = _semantic_features(dev)
        candidates = []
        for test in test_records:
            candidates.append((_jaccard(dev_features, _semantic_features(test)), test))
        score, match = max(candidates, key=lambda item: (item[0], item[1]["intent_group_id"]))
        nearest.append(
            {
                "dev_intent_group_id": dev["intent_group_id"],
                "test_intent_group_id": match["intent_group_id"],
                "structured_jaccard": round(score, 6),
                "decision_status": "draft",
                "draft_decision": "non_overlapping",
                "requires_human_review": True,
                "novelty_note": dev.get("locked_test_novelty_note", ""),
            }
        )
    metadata_versions = {
        record["metadata_schema_version"] for record in dev_records + test_records
    }
    if len(metadata_versions) != 1:
        raise ValidationError("development/test libraries use different metadata schemas")
    return {
        "schema_version": next(iter(metadata_versions)),
        "dev_path": str(Path(dev_path).resolve()),
        "test_path": str(Path(test_path).resolve()),
        "exact_canonical_overlap": exact_canonical,
        "exact_canonical_overlap_count": len(exact_canonical),
        "nearest_candidates": sorted(
            nearest, key=lambda item: (-item["structured_jaccard"], item["dev_intent_group_id"])
        ),
        "overall_decision_status": "draft",
        "draft_decision": "accept_all_dev_intents_as_non_overlapping",
        "review_scope": "Confirm 16 novelty notes; structured similarity is advisory only.",
    }


def find_internal_duplicate_candidates(path: Path, threshold: float = 0.75) -> List[Dict[str, Any]]:
    records = [r for r in read_jsonl(Path(path)) if r["surface_style"] == "precise"]
    candidates = []
    for index, left in enumerate(records):
        for right in records[index + 1 :]:
            score = _jaccard(_semantic_features(left), _semantic_features(right))
            if score >= threshold:
                candidates.append(
                    {
                        "left": left["intent_group_id"],
                        "right": right["intent_group_id"],
                        "structured_jaccard": round(score, 6),
                        "decision_status": "draft",
                        "draft_decision": "review_possible_semantic_duplicate",
                    }
                )
    return sorted(candidates, key=lambda value: (-value["structured_jaccard"], value["left"]))
