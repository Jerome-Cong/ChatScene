"""Frozen, map-free MetaDrive PGMap sequence-block review contract.

The native extractor fixtures remain executable benchmark assets. Human
platform review receives only the deterministic PG block scene, bound to the
native token artifact hash and to this registry's frozen file hash.
"""

import copy
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .errors import ValidationError
from .jsonio import canonical_json_bytes, sha256_bytes, sha256_file
from .schema import validate_schema_instance
from .metadrive_pg import (
    AUDITED_TOKEN_LANE_NUMS,
    MetaDrivePGError,
    parse_artifact,
    validate_token_registry,
)


PG_BLOCK_SCENE_CONTRACT = "metadrive_pg_block_scene_v0.1"
PG_ROAD_SOURCE_CONTRACT = "builtin_sequence_block_tokens_only"

_FORBIDDEN_REAL_MAP_KEYS = {
    "map_id",
    "map_name",
    "map_path",
    "map_file",
    "real_map",
    "real_map_id",
    "opendrive_map",
    "scenario_description",
    "map_features",
}


def assert_metadrive_review_payload_is_map_free(
    value: Any, path: str = "metadrive_review_subject"
) -> None:
    """Reject native-map and ScenarioDescription material from human packets."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered_key = str(key).lower()
            if lowered_key in _FORBIDDEN_REAL_MAP_KEYS:
                raise ValidationError(
                    "MetaDrive human review payload contains forbidden field {}.{}".format(
                        path, key
                    )
                )
            assert_metadrive_review_payload_is_map_free(
                child, "{}.{}".format(path, key)
            )
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            assert_metadrive_review_payload_is_map_free(
                child, "{}[{}]".format(path, index)
            )
        return
    if isinstance(value, str):
        lowered = value.lower()
        forbidden_fragments = (
            '"map_id"',
            '"map_name"',
            '"map_features"',
            "scenariodescription",
            "scenario_description",
            ".xodr",
        )
        if any(fragment in lowered for fragment in forbidden_fragments):
            raise ValidationError(
                "MetaDrive human review payload embeds native-map material at {}".format(
                    path
                )
            )


def _record_map(rows: Sequence[Mapping[str, Any]], key: str, label: str) -> Dict[str, Any]:
    result = {}
    for row in rows:
        value = row.get(key)
        if not isinstance(value, str) or not value or value in result:
            raise ValidationError("{} requires unique non-empty {} values".format(label, key))
        result[value] = row
    return result


def _parameter_map(rows: Sequence[Mapping[str, Any]], label: str) -> Dict[str, Any]:
    return _record_map(rows, "name", label)


def _matches_type(value: Any, value_type: str) -> bool:
    if value_type == "boolean":
        return type(value) is bool
    if value_type == "integer":
        return type(value) is int
    if value_type == "number":
        return type(value) in (int, float) and type(value) is not bool
    if value_type == "string":
        return isinstance(value, str)
    return False


def _validate_parameter_value(
    name: str, value: Any, contract: Mapping[str, Any]
) -> None:
    if not _matches_type(value, contract["value_type"]):
        raise ValidationError(
            "MetaDrive PG parameter {} violates its frozen type".format(name)
        )
    if "minimum" in contract and value < contract["minimum"]:
        raise ValidationError(
            "MetaDrive PG parameter {} is below its frozen minimum".format(name)
        )
    if "maximum" in contract and value > contract["maximum"]:
        raise ValidationError(
            "MetaDrive PG parameter {} exceeds its frozen maximum".format(name)
        )
    if "allowed_values" in contract and canonical_json_bytes(value) not in {
        canonical_json_bytes(allowed) for allowed in contract["allowed_values"]
    }:
        raise ValidationError(
            "MetaDrive PG parameter {} is outside its frozen values".format(name)
        )


def validate_metadrive_pg_registry(
    registry: Mapping[str, Any],
    token_registry: Mapping[str, Any],
    token_registry_binding: Mapping[str, Any],
    *,
    verify_implementation_files: bool = True
) -> Dict[str, Any]:
    """Validate token identity, parameter domains, files, and non-topology regions."""

    validate_schema_instance(
        registry,
        "metadrive_pg_block_registry",
        context="MetaDrive PG block registry",
    )
    assert_metadrive_review_payload_is_map_free(registry, "metadrive_pg_registry")
    try:
        runtime_tokens = validate_token_registry(
            token_registry,
            formal=True,
            verify_files=verify_implementation_files,
        )
    except MetaDrivePGError as exc:
        raise ValidationError(
            "MetaDrive runtime token registry is invalid: {}".format(exc)
        ) from exc
    expected_token_binding = {
        "path": str(Path(token_registry_binding["path"]).resolve()),
        "sha256": token_registry_binding["sha256"],
        "bytes": token_registry_binding["bytes"],
    }
    actual_token_binding = {
        "path": str(Path(registry["token_registry"]["path"]).resolve()),
        "sha256": registry["token_registry"]["sha256"],
        "bytes": registry["token_registry"]["bytes"],
    }
    if (
        registry["token_registry_id"] != token_registry.get("registry_id")
        or actual_token_binding != expected_token_binding
    ):
        raise ValidationError(
            "MetaDrive capability registry is not bound to the exact runtime token registry"
        )
    implementation_hashes = set()
    implementation_paths = set()
    implementation_by_path = {}
    for binding in registry["implementation_files"]:
        path = str(Path(binding["path"]).resolve())
        if path in implementation_paths:
            raise ValidationError("MetaDrive PG registry repeats an implementation file")
        implementation_paths.add(path)
        implementation_hashes.add(binding["sha256"])
        implementation_by_path[path] = binding
        if verify_implementation_files:
            source = Path(path)
            if (
                not source.is_file()
                or sha256_file(source) != binding["sha256"]
                or source.stat().st_size != binding["bytes"]
            ):
                raise ValidationError(
                    "MetaDrive PG registry implementation binding is stale: {}".format(
                        path
                    )
                )
    runtime_implementation_bindings = {
        (
            str(Path(binding["path"]).resolve()),
            binding["sha256"],
            binding["bytes"],
        )
        for binding in token_registry["source_files"]
    }
    semantic_implementation_bindings = {
        (path, binding["sha256"], binding["bytes"])
        for path, binding in implementation_by_path.items()
    }
    if semantic_implementation_bindings != runtime_implementation_bindings:
        raise ValidationError(
            "MetaDrive semantic registry must bind the exact runtime implementation files"
        )
    _parameter_map(registry["global_parameters"], "MetaDrive PG global parameters")
    tokens = _record_map(registry["tokens"], "token", "MetaDrive PG tokens")
    block_token_classes = {
        token: definition["class_name"] for token, definition in tokens.items()
    }
    if block_token_classes != runtime_tokens:
        raise ValidationError(
            "MetaDrive capability and runtime token/class registries have drifted"
        )
    runtime_lane_num_constraints = {
        definition["token"]: tuple(
            definition["map_parameter_constraints"]["lane_num_allowed"]
        )
        for definition in token_registry["tokens"]
    }
    for token, definition in tokens.items():
        if definition["block_id"] != token:
            raise ValidationError("MetaDrive PG token must equal the registered block ID")
        if definition["pg_block_class"].rsplit(".", 1)[-1] != definition["class_name"]:
            raise ValidationError(
                "MetaDrive PG semantic mapping class differs from the runtime whitelist"
            )
        if definition["implementation_file_sha256"] not in implementation_hashes:
            raise ValidationError("MetaDrive PG token is not bound to a frozen source file")
        module_name = definition["pg_block_class"].split(".")[-2]
        matching_sources = [
            binding
            for path, binding in implementation_by_path.items()
            if path.endswith("/{}.py".format(module_name))
        ]
        if (
            len(matching_sources) != 1
            or matching_sources[0]["sha256"]
            != definition["implementation_file_sha256"]
        ):
            raise ValidationError(
                "MetaDrive PG token class is not bound to its exact implementation module"
            )
        contracts = _parameter_map(
            definition["parameter_contract"],
            "MetaDrive PG token parameter contract",
        )
        allowed_lane_nums = definition["map_parameter_constraints"][
            "lane_num_allowed"
        ]
        if allowed_lane_nums != sorted(set(allowed_lane_nums)):
            raise ValidationError(
                "MetaDrive PG token lane_num constraint must be sorted and unique"
            )
        if tuple(allowed_lane_nums) != runtime_lane_num_constraints[token]:
            raise ValidationError(
                "MetaDrive PG token lane_num constraint differs from the runtime token registry"
            )
        for name, contract in contracts.items():
            if (
                "minimum" in contract
                and "maximum" in contract
                and contract["minimum"] > contract["maximum"]
            ):
                raise ValidationError(
                    "MetaDrive PG parameter {} has an inverted range".format(name)
                )
            if contract["value_type"] not in ("number", "integer") and any(
                field in contract for field in ("minimum", "maximum")
            ):
                raise ValidationError(
                    "MetaDrive PG non-numeric parameter {} declares a numeric range".format(
                        name
                    )
                )
            for allowed in contract.get("allowed_values", []):
                _validate_parameter_value(name, allowed, contract)
    bindings = _record_map(
        registry["fixture_bindings"], "case_id", "MetaDrive PG fixture bindings"
    )
    for binding in bindings.values():
        if set(binding["block_sequence"]) - set(tokens):
            raise ValidationError("MetaDrive fixture uses an unregistered PG token")
        lane_num = binding["map_parameters"]["lane_num"]
        if any(
            lane_num not in AUDITED_TOKEN_LANE_NUMS[token]
            for token in binding["block_sequence"]
        ):
            raise ValidationError(
                "MetaDrive fixture violates a token lane_num constraint"
            )
        if binding["semantic_regions_topology_effect"] is not False:
            raise ValidationError(
                "MetaDrive semantic_regions may annotate semantics but cannot change topology"
            )
    return copy.deepcopy(dict(registry))


def validate_metadrive_registry_fixture_coverage(
    registry: Mapping[str, Any],
    token_registry: Mapping[str, Any],
    token_registry_sha256: str,
    semantic_fixture: Mapping[str, Any],
    cross_platform_fixture: Mapping[str, Any],
) -> None:
    """Require exactly one PG scene binding for every fixture with a MetaDrive side."""

    bindings = _record_map(
        registry["fixture_bindings"], "case_id", "MetaDrive PG fixture bindings"
    )
    expected = {}
    for case in semantic_fixture.get("cases", []):
        if case.get("platform") == "metadrive":
            expected[case["case_id"]] = (
                set(case["concept_ids"]),
                case["input"],
            )
    for case in cross_platform_fixture.get("cases", []):
        expected[case["case_id"]] = (
            {case["concept_id"]},
            case["right_input"],
        )
    if set(bindings) != set(expected):
        raise ValidationError(
            "MetaDrive PG registry must cover exactly every MetaDrive fixture case"
        )
    for case_id, (concept_ids, fixture_input) in expected.items():
        binding = bindings[case_id]
        artifact_record = fixture_input["artifact"]
        artifact_payload = artifact_record["text"].encode("utf-8")
        if (
            set(binding["concept_ids"]) != concept_ids
            or fixture_input.get("artifact_format") != PG_BLOCK_SCENE_CONTRACT
            or binding["native_artifact_sha256"] != artifact_record["sha256"]
            or sha256_bytes(artifact_payload) != artifact_record["sha256"]
            or len(artifact_payload) != artifact_record["bytes"]
        ):
            raise ValidationError(
                "MetaDrive PG fixture binding differs from case {}".format(case_id)
            )
        try:
            artifact = parse_artifact(
                artifact_payload, token_registry, token_registry_sha256
            )
        except MetaDrivePGError as exc:
            raise ValidationError(
                "MetaDrive fixture is not the frozen PG block-scene contract: {}".format(
                    exc
                )
            ) from exc
        expected_map = {
            "generation_type": "block_sequence",
            "block_sequence": binding["block_sequence"],
            **copy.deepcopy(binding["map_parameters"]),
            "token_registry_id": registry["token_registry_id"],
            "token_registry_sha256": registry["token_registry"]["sha256"],
        }
        if (
            canonical_json_bytes(artifact["map"])
            != canonical_json_bytes(expected_map)
            or canonical_json_bytes(artifact.get("semantic_regions", []))
            != canonical_json_bytes(binding["semantic_regions"])
        ):
            raise ValidationError(
                "MetaDrive human fixture view differs from its actual PG artifact"
            )


def _scene_input(
    binding: Mapping[str, Any], registry: Mapping[str, Any], registry_sha256: str
) -> Dict[str, Any]:
    value = {
        "input_contract": PG_BLOCK_SCENE_CONTRACT,
        "artifact_type": PG_BLOCK_SCENE_CONTRACT,
        "native_artifact_sha256": binding["native_artifact_sha256"],
        "capability_registry_sha256": registry_sha256,
        "map": {
            "generation_type": "block_sequence",
            "block_sequence": binding["block_sequence"],
            **copy.deepcopy(binding["map_parameters"]),
            "token_registry_id": registry["token_registry_id"],
            "token_registry_sha256": registry["token_registry"]["sha256"],
        },
        "road_semantics": copy.deepcopy(binding["road_semantics"]),
        "semantic_regions": copy.deepcopy(binding["semantic_regions"]),
        "semantic_regions_contract": "annotation_only_no_topology_change",
    }
    assert_metadrive_review_payload_is_map_free(value)
    return value


def build_metadrive_fixture_audit_subject(
    case: Mapping[str, Any],
    registry: Mapping[str, Any],
    registry_sha256: str,
    *,
    cross_platform: bool,
) -> Dict[str, Any]:
    bindings = _record_map(
        registry["fixture_bindings"], "case_id", "MetaDrive PG fixture bindings"
    )
    binding = bindings.get(case.get("case_id"))
    if binding is None:
        raise ValidationError("MetaDrive fixture lacks a frozen PG scene binding")
    if cross_platform:
        value = {
            "case_id": case["case_id"],
            "concept_id": case["concept_id"],
            "semantic_category": case["semantic_category"],
            "changed_concept_ids": copy.deepcopy(case["changed_concept_ids"]),
            "expected_equal": case["expected_equal"],
            "left_platform": "carla",
            "left_input_sha256": sha256_bytes(canonical_json_bytes(case["left_input"])),
            "right_platform": "metadrive",
            "right_input": _scene_input(binding, registry, registry_sha256),
            "expected_left": copy.deepcopy(case["expected_left"]),
            "expected_right": copy.deepcopy(case["expected_right"]),
        }
    else:
        value = {
            "case_id": case["case_id"],
            "platform": "metadrive",
            "concept_ids": copy.deepcopy(case["concept_ids"]),
            "test_kind": case["test_kind"],
            "input": _scene_input(binding, registry, registry_sha256),
            "expected": copy.deepcopy(case["expected"]),
        }
    assert_metadrive_review_payload_is_map_free(value)
    return value


def build_metadrive_capability_audit_subject(
    concept: Mapping[str, Any],
    registry: Mapping[str, Any],
    registry_sha256: str,
) -> Dict[str, Any]:
    fixture_ids = set(concept.get("fixture_case_ids", []))
    bindings = [
        binding
        for binding in registry["fixture_bindings"]
        if binding["case_id"] in fixture_ids
    ]
    value = {
        "capability": copy.deepcopy(dict(concept)),
        "input_contract": PG_BLOCK_SCENE_CONTRACT,
        "capability_registry_sha256": registry_sha256,
        "token_registry_id": registry["token_registry_id"],
        "token_registry_sha256": registry["token_registry"]["sha256"],
        "pg_fixture_bindings": [
            {
                "case_id": binding["case_id"],
                "concept_ids": copy.deepcopy(binding["concept_ids"]),
                "map": _scene_input(binding, registry, registry_sha256)["map"],
                "road_semantics": copy.deepcopy(binding["road_semantics"]),
                "semantic_regions": copy.deepcopy(binding["semantic_regions"]),
                "semantic_regions_contract": "annotation_only_no_topology_change",
            }
            for binding in sorted(bindings, key=lambda item: item["case_id"])
        ],
    }
    assert_metadrive_review_payload_is_map_free(value)
    return value


def build_metadrive_registry_audit_subject(
    registry: Mapping[str, Any],
    registry_sha256: str,
    token_registry: Mapping[str, Any],
    token_registry_binding: Mapping[str, Any],
) -> Dict[str, Any]:
    value = {
        "input_contract": PG_BLOCK_SCENE_CONTRACT,
        "capability_registry_sha256": registry_sha256,
        "token_registry_id": token_registry["registry_id"],
        "token_registry": copy.deepcopy(dict(token_registry_binding)),
        "runtime_token_whitelist": copy.deepcopy(dict(token_registry)),
        "human_reviewed_semantic_mapping": copy.deepcopy(dict(registry)),
    }
    assert_metadrive_review_payload_is_map_free(value)
    return value


__all__ = [
    "PG_BLOCK_SCENE_CONTRACT",
    "PG_ROAD_SOURCE_CONTRACT",
    "assert_metadrive_review_payload_is_map_free",
    "build_metadrive_capability_audit_subject",
    "build_metadrive_fixture_audit_subject",
    "build_metadrive_registry_audit_subject",
    "validate_metadrive_pg_registry",
    "validate_metadrive_registry_fixture_coverage",
]
