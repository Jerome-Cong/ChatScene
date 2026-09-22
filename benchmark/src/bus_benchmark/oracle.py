"""Generate reviewable layered-oracle and CPD-policy drafts from query metadata."""

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .atoms import make_atom, normalize_count
from .constants import SCHEMA_VERSION, SURFACE_STYLES
from .cpd import query_blind_target_selector
from .library import flatten_rule_hooks, validate_record


SPATIAL_ROLE_RULES = (
    (("following", "rear", "behind"), "behind_ego"),
    (("leading", "ahead", "front"), "ahead_of_ego"),
    (("adjacent", "target_lane", "overtaking", "passing"), "adjacent_lane"),
    (("oncoming", "opposing", "opposed"), "opposing_lane"),
    (("nonmotor", "cycle", "cyclist", "e_bike", "bicycle"), "nonmotor_space"),
    (("sidewalk", "waiting", "boarding", "passenger"), "curb_or_sidewalk"),
    (("crossing", "crosswalk"), "crossing_zone"),
    (("parked", "parking", "obstruction"), "roadside_or_stop_zone"),
)

NUMERIC_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>m|meter(?:s)?|s|sec(?:ond)?s?|km/h|kph|mph)",
    re.IGNORECASE,
)

REQUIRED_REVIEW_CHECKS = (
    "support_and_response_disposition",
    "cardinality_and_polarity",
    "road_and_spatial_decomposition",
    "event_graph_and_actor_binding",
    "surface_layering",
    "cpd_common_eligibility",
)


def _provenance(
    record: Mapping[str, Any], field: str, index: Optional[int] = None
) -> Dict[str, Any]:
    value = {"source": "query_library_metadata", "field": field}
    if index is not None:
        value["index"] = index
    value["query_id"] = record["query_id"]
    return value


def _role_relation(role: str) -> Optional[str]:
    normalized = role.lower()
    for tokens, relation in SPATIAL_ROLE_RULES:
        if any(token in normalized for token in tokens):
            return relation
    return None


def _actor_atoms(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for index, actor in enumerate(record.get("counterpart_actors", [])):
        count = normalize_count(actor["count"])
        layer = "permitted" if count == "optional" else "core_required"
        polarity = "absent" if count == "0" else "present"
        if polarity == "absent":
            layer = "forbidden"
        result.append(
            make_atom(
                "actor",
                "actor_role_count",
                {"type": actor["type"], "role": actor["role"], "count": count},
                layer=layer,
                polarity=polarity,
                provenance=_provenance(record, "counterpart_actors", index),
                notes="Cardinality normalization is a draft decision.",
            )
        )
        relation = _role_relation(actor["role"])
        if relation and polarity == "present" and layer != "permitted":
            result.append(
                make_atom(
                    "spatial",
                    "actor_relative_region",
                    {"role": actor["role"], "relation": relation},
                    provenance=_provenance(record, "counterpart_actors", index),
                    notes="Heuristic role-to-space mapping; human confirmation required.",
                )
            )
    return result


def _road_atoms(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    result = []
    if record.get("bus_stop_type") not in (None, "none"):
        result.append(
            make_atom(
                "road",
                "bus_stop_type",
                {"value": record["bus_stop_type"]},
                provenance=_provenance(record, "bus_stop_type"),
            )
        )
    topology = record.get("road_topology")
    if topology:
        result.append(
            make_atom(
                "road",
                "road_topology",
                {"value": topology},
                provenance=_provenance(record, "road_topology"),
                notes="Monolithic topology label must be reviewed against operational evidence.",
            )
        )
    for key, value in sorted(record.get("lane_configuration", {}).items()):
        provenance = _provenance(record, "lane_configuration.{}".format(key))
        if value == "optional":
            result.append(
                make_atom(
                    "road",
                    "lane_configuration",
                    {"feature": key, "value": value},
                    layer="permitted",
                    provenance=provenance,
                )
            )
        elif value == "unspecified":
            continue
        elif value is True or (isinstance(value, int) and not isinstance(value, bool)):
            result.append(
                make_atom(
                    "road",
                    "lane_configuration",
                    {"feature": key, "value": value},
                    provenance=provenance,
                )
            )
        elif value is False:
            # False metadata is not automatically a query-level prohibition.
            result.append(
                make_atom(
                    "road",
                    "lane_configuration",
                    {"feature": key, "value": False},
                    layer="permitted",
                    provenance=provenance,
                    notes="Draft records absence metadata without scoring it as forbidden.",
                )
            )
    return result


def _event_atoms(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    result = []
    events = record.get("interaction_events", [])
    for index, event in enumerate(events):
        result.append(
            make_atom(
                "event",
                "event_spec",
                {"event": event},
                provenance=_provenance(record, "interaction_events", index),
                notes="Metadata label may be event/state/trigger/constraint; classify during review.",
            )
        )
    temporal_structure = record.get("event_temporal_structure")
    if temporal_structure:
        result.append(
            make_atom(
                "temporal",
                "temporal_structure",
                {"value": temporal_structure},
                provenance=_provenance(record, "event_temporal_structure"),
            )
        )
    if temporal_structure in (
        "two_stage_chain",
        "three_stage_chain",
        "multi_stage_state_dependent",
    ):
        for left, right in zip(events, events[1:]):
            result.append(
                make_atom(
                    "temporal",
                    "before",
                    {"first": left, "second": right},
                    provenance=_provenance(record, "interaction_events"),
                    notes="List-order edge is a machine draft and requires explicit confirmation.",
                )
            )
    elif temporal_structure == "parallel_events" and len(events) > 1:
        result.append(
            make_atom(
                "temporal",
                "parallel_group",
                {"events": list(events)},
                provenance=_provenance(record, "interaction_events"),
                notes="Parallel membership requires review.",
            )
        )
    elif temporal_structure == "conditional_trigger":
        result.append(
            make_atom(
                "temporal",
                "conditional_trigger_unresolved",
                {"events": list(events)},
                provenance=_provenance(record, "interaction_events"),
                notes="Trigger and consequence binding must be supplied by a reviewer.",
            )
        )
    return result


def _normative_atoms(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    result = [
        make_atom(
            "normative",
            "rule_mode",
            {"value": record.get("rule_mode")},
            provenance=_provenance(record, "rule_mode"),
        ),
        make_atom(
            "normative",
            "risk_level",
            {"value": record.get("risk_level")},
            provenance=_provenance(record, "risk_level"),
            notes="Confirm whether risk is requested semantics or analysis-only metadata.",
        ),
    ]
    for scope, index, hook in flatten_rule_hooks(record.get("rule_hooks", [])):
        is_legacy = scope == "legacy_unscoped"
        result.append(
            make_atom(
                "normative",
                "rule_hook",
                {"value": hook} if is_legacy else {"scope": scope, "value": hook},
                provenance=_provenance(
                    record,
                    "rule_hooks" if is_legacy else "rule_hooks.{}".format(scope),
                    index,
                ),
                notes=(
                    ""
                    if is_legacy
                    else (
                        "Rule-hook scope is preserved; this atom does not prescribe "
                        "an ego reaction."
                    )
                ),
            )
        )
    return result


def _surface_numeric_atoms(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    text = record.get("query_text", "")
    result = []
    for index, match in enumerate(NUMERIC_PATTERN.finditer(text)):
        start = max(0, match.start() - 45)
        end = min(len(text), match.end() + 45)
        result.append(
            make_atom(
                "spatial",
                "surface_numeric_constraint",
                {
                    "value": float(match.group("value")),
                    "unit": match.group("unit").lower(),
                    "context": text[start:end].strip(),
                },
                layer="surface_required",
                provenance={
                    "source": "query_text_regex",
                    "query_id": record["query_id"],
                    "span": [match.start(), match.end()],
                },
                notes="Target binding and tolerance require human review.",
            )
        )
    return result


def _cpd_actor_class(actor_type: str) -> str:
    value = str(actor_type).lower()
    if value in ("e_bike", "bicycle", "cyclist"):
        return "cyclist"
    if value in ("passenger", "pedestrian"):
        return "pedestrian"
    return "motor_vehicle"


def _cpd_minimum_actor_count(value: Any) -> int:
    normalized = normalize_count(value)
    if normalized == "multiple":
        return 2
    if normalized == "optional":
        return 0
    return int(normalized)


def _first_cpd_actor_class(record: Mapping[str, Any]) -> str:
    for actor in record.get("counterpart_actors", []):
        if _cpd_minimum_actor_count(actor.get("count")) > 0:
            return _cpd_actor_class(actor.get("type", ""))
    raise ValueError("CPD actor-bound dimension requires one counterpart actor")


def _has_unique_cpd_target_class(record: Mapping[str, Any]) -> bool:
    target_class = _first_cpd_actor_class(record)
    minimum_count = sum(
        _cpd_minimum_actor_count(actor.get("count"))
        for actor in record.get("counterpart_actors", [])
        if _cpd_actor_class(actor.get("type", "")) == target_class
    )
    return minimum_count == 1


def _cpd_actor_class_count(record: Mapping[str, Any], actor_class: str) -> int:
    return sum(
        _cpd_minimum_actor_count(actor.get("count"))
        for actor in record.get("counterpart_actors", [])
        if _cpd_actor_class(actor.get("type", "")) == actor_class
    )


def _positive_yield_events(record: Mapping[str, Any]) -> List[str]:
    excluded = ("non_yield", "yield_violation", "failure_to_yield", "without_yield")
    legacy = record.get("metadata_schema_version") == "0.1"
    return [
        str(value).lower()
        for value in record.get("interaction_events", [])
        if "yield" in str(value).lower()
        and (legacy or not any(token in str(value).lower() for token in excluded))
    ]


def _yield_event_subject_actor_class(event: str) -> Optional[str]:
    subject = event.split("yield", 1)[0]
    if any(
        token in subject
        for token in ("e_bike", "e-bike", "cyclist", "bicycle", "nonmotor")
    ):
        return "cyclist"
    if any(token in subject for token in ("pedestrian", "passenger")):
        return "pedestrian"
    if any(token in subject for token in ("vehicle", "car", "motorcycle", "motor_vehicle")):
        return "motor_vehicle"
    return None


def _yield_subject_actor_class(record: Mapping[str, Any]) -> str:
    events = _positive_yield_events(record)
    if record.get("metadata_schema_version") == "0.1" and any(
        event.startswith("bus_") for event in events
    ):
        return "ego_bus"
    subjects = [
        subject
        for subject in (_yield_event_subject_actor_class(event) for event in events)
        if subject is not None
    ]
    if subjects and len(set(subjects)) == 1:
        return subjects[0]
    return _first_cpd_actor_class(record)


def _cpd_dimensions(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    text = record.get("query_text", "").lower()
    dimensions = []
    has_actor = any(
        normalize_count(actor["count"]) != "0"
        for actor in record.get("counterpart_actors", [])
    )
    has_metric = NUMERIC_PATTERN.search(text) is not None
    if has_actor and not has_metric and _has_unique_cpd_target_class(record):
        dimensions.append(
            {
                "name": "actor_longitudinal_distance_bin",
                "cardinality": "exactly_one",
                "allowed_values": ["near", "medium", "far"],
                "bin_definition_m": {"near_max": 10.0, "medium_max": 30.0},
                "target_selector": query_blind_target_selector(
                    "actor_longitudinal_distance_bin",
                    _first_cpd_actor_class(record),
                ),
                "reason": "Actor exists but the surface does not fix a metric distance.",
                "common_semantic": True,
            }
        )
    optional_lanes = sorted(
        key for key, value in record.get("lane_configuration", {}).items() if value == "optional"
    )
    if optional_lanes:
        dimensions.append(
            {
                "name": "optional_road_feature_presence",
                "cardinality": "exactly_one",
                "allowed_values": [
                    "{}:{}".format(feature, presence)
                    for feature in optional_lanes
                    for presence in ("present", "absent")
                ],
                "reason": "Metadata explicitly marks these road features optional.",
                "common_semantic": True,
            }
        )
    yield_events = _positive_yield_events(record)
    yield_subject = _yield_subject_actor_class(record) if yield_events else None
    if (
        yield_events
        and yield_subject is not None
        and (
            (
                record.get("metadata_schema_version") == "0.1"
                and yield_subject == "ego_bus"
            )
            or _cpd_actor_class_count(record, yield_subject) == 1
        )
        and not any(word in text for word in ("full stop", "hard brake"))
    ):
        dimensions.append(
            {
                "name": "yield_realization_mode",
                "cardinality": "exactly_one",
                "allowed_values": ["decelerate", "hold"],
                "target_selector": query_blind_target_selector(
                    "yield_realization_mode",
                    yield_subject,
                ),
                "reason": "Yield is required while its compliant realization is not fixed.",
                "common_semantic": True,
            }
        )
    merge_events = {
        event.lower()
        for event in record.get("interaction_events", [])
        if "merge" in event.lower()
    }
    merge_required = bool(merge_events) or (
        record.get("metadata_schema_version") == "0.2"
        and record.get("bus_operation_phase") == "departure_merge"
    )
    if (
        merge_required
        and has_actor
        and (
            record.get("metadata_schema_version") == "0.1"
            or _has_unique_cpd_target_class(record)
        )
        and "ahead of" not in text
        and "behind" not in text
    ):
        dimensions.append(
            {
                "name": "merge_gap_relation",
                "cardinality": "exactly_one",
                "allowed_values": ["ahead_of_gap_actor", "behind_gap_actor"],
                "target_selector": query_blind_target_selector(
                    "merge_gap_relation",
                    _first_cpd_actor_class(record),
                ),
                "reason": "Merge is required without a fixed accepted-gap relation.",
                "common_semantic": True,
            }
        )
    return dimensions


def draft_oracle_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    validate_record(record)
    atoms = []
    if record["expected_support"] == "supported":
        atoms.extend(_actor_atoms(record))
        atoms.extend(_road_atoms(record))
        atoms.extend(_event_atoms(record))
        atoms.extend(_normative_atoms(record))
        atoms.extend(_surface_numeric_atoms(record))
    is_cpd_candidate = record["expected_support"] == "supported" and record["surface_style"] in (
        "partial",
        "vague",
    )
    dimensions = _cpd_dimensions(record) if is_cpd_candidate else []
    return {
        "schema_version": SCHEMA_VERSION,
        "query_id": record["query_id"],
        "intent_group_id": record["intent_group_id"],
        "surface_style": record["surface_style"],
        "expected_support": record["expected_support"],
        "acceptable_response": list(record.get("acceptable_response", [])),
        "unsupported_reasons": list(record.get("expected_reason_if_unsupported", [])),
        "ego_gate": {
            "semantic_role": "ego_bus",
            "count": 1,
            "approved_proxy_allowed": True,
            "length_m": 5.33,
            "width_m": 2.10,
        },
        "atoms": atoms,
        "cpd_policy": {
            "candidate": is_cpd_candidate,
            "eligible": is_cpd_candidate and bool(dimensions),
            "dimensions": dimensions,
            "decision_status": "draft",
            "cross_platform_judgeable": None,
        },
        "decision_status": "draft",
        "draft_source": "metadata_rules_v{}".format(
            str(record.get("metadata_schema_version", "0.1")).replace(".", "_")
        ),
        "review_warnings": [
            "Event labels need event/state/trigger/constraint classification.",
            "Surface-specific non-numeric constraints are not exhaustively extracted.",
            "Spatial role mapping and CPD eligibility require human confirmation.",
        ],
    }


def draft_oracle(records: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [draft_oracle_record(record) for record in records]


def cpd_dimension_names(oracle_records: Iterable[Mapping[str, Any]]) -> set:
    """Return the semantic CPD dimension registry represented by an oracle set."""

    return {
        dimension["name"]
        for record in oracle_records
        for dimension in record.get("cpd_policy", {}).get("dimensions", [])
        if isinstance(dimension, Mapping) and isinstance(dimension.get("name"), str)
    }


def restrict_cpd_eligibility(
    oracle_records: Iterable[Dict[str, Any]],
    allowed_dimension_names: Iterable[str],
) -> List[Dict[str, Any]]:
    """Mark policies using dimensions absent from the frozen dev registry ineligible."""

    allowed = set(allowed_dimension_names)
    records = list(oracle_records)
    for record in records:
        policy = record.get("cpd_policy", {})
        names = {
            dimension.get("name")
            for dimension in policy.get("dimensions", [])
            if isinstance(dimension, Mapping)
        }
        missing = sorted(name for name in names if name not in allowed)
        if missing:
            policy["eligible"] = False
            policy["cross_platform_judgeable"] = False
            record.setdefault("review_warnings", []).append(
                "CPD draft is ineligible because the development registry lacks: {}.".format(
                    ", ".join(missing)
                )
            )
    return records


def build_review_tasks(oracle_records: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    tasks = []
    for record in oracle_records:
        tasks.append(
            {
                "query_id": record["query_id"],
                "intent_group_id": record["intent_group_id"],
                "surface_style": record["surface_style"],
                "review_type": "layered_oracle",
                "review_policy": "one_complete_human_gold_per_subject",
                "reviewer_status": "pending",
                "human_gold_status": "pending",
                "draft_atom_ids": [atom["atom_id"] for atom in record.get("atoms", [])],
                "required_checks": list(REQUIRED_REVIEW_CHECKS),
            }
        )
    return tasks
