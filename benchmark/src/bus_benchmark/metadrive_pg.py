"""Strict native MetaDrive PG block-sequence artifact contract.

This module intentionally depends only on the Python standard library.  It is
importable both as ``bus_benchmark.metadrive_pg`` and as ``metadrive_pg`` when
``bus_benchmark/`` is the script working directory.

The submitted generation artifact contains one audited built-in PG block-token
sequence plus deterministic actor trajectories.  A runtime interpreter may
translate this document into ``MetaDriveEnv`` calls, but paths, Python classes,
map features, and the implicit first-block token are never accepted as method
output.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = "0.1"
ARTIFACT_TYPE = "metadrive_pg_block_scene_v0.1"
REGISTRY_ID = "metadrive-pg-token-registry-v0.1"
FINALIZATION_POLICY_ID = "metadrive_pg_ego_proxy_v0.1"
IMPLICIT_FIRST_TOKEN = "I"
EGO_VEHICLE_MODEL = "xl"
EGO_LENGTH_M = 5.33
EGO_WIDTH_M = 2.10
AUDITED_TOKEN_CLASSES = {
    "$": "TollGate",
    "B": "Bidirection",
    "C": "Curve",
    "O": "Roundabout",
    "P": "ParkingLot",
    "R": "OutRampOnStraight",
    "S": "Straight",
    "T": "StdTInterSection",
    "U": "StdInterSectionWithUTurn",
    "X": "StdInterSection",
    "Y": "Split",
    "r": "InRampOnStraight",
    "y": "Merge",
}
AUDITED_TOKEN_LANE_NUMS = {
    token: ((1,) if token == "P" else (1, 2, 3, 4, 5))
    for token in AUDITED_TOKEN_CLASSES
}

_HASH_KEYS = frozenset({"path", "sha256", "bytes"})
_STATE_KEYS = frozenset(
    {"step", "position_m", "heading_rad", "speed_m_s", "valid"}
)
_ACTOR_TYPES = frozenset(
    {"vehicle", "pedestrian", "e_bike", "bicycle", "motorcycle"}
)
_VEHICLE_MODELS = frozenset({"default", "s", "m", "l", "xl"})
_REGISTRY_KEYS = frozenset(
    {
        "schema_version",
        "decision_status",
        "registry_id",
        "artifact_type",
        "metadrive_version",
        "constructor_contract",
        "source_files",
        "tokens",
    }
)
_MAP_KEYS = frozenset(
    {
        "generation_type",
        "block_sequence",
        "map_seed",
        "lane_num",
        "lane_width_m",
        "exit_length_m",
        "token_registry_id",
        "token_registry_sha256",
    }
)
_EGO_KEYS = frozenset(
    {
        "actor_id",
        "actor_type",
        "vehicle_model",
        "length_m",
        "width_m",
        "reference_trajectory",
    }
)
_ACTOR_KEYS = frozenset(
    {
        "actor_id",
        "actor_type",
        "length_m",
        "width_m",
        "motion_mode",
        "trajectory",
    }
)
_ARTIFACT_KEYS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "time_step_s",
        "horizon_steps",
        "map",
        "ego",
        "actors",
    }
)
_OPTIONAL_ARTIFACT_KEYS = frozenset({"semantic_regions"})
_REGION_KEYS = frozenset({"region_id", "region_type", "polygon_m"})
_REGION_TYPES = frozenset(
    {
        "bus_stop_area",
        "bus_stop_no_parking_zone",
        "crosswalk",
        "sidewalk",
        "motor_lane",
        "non_motor_lane",
        "bus_only_lane",
        "queue_area",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class MetaDrivePGError(ValueError):
    """Raised when a native PG registry or artifact fails closed."""


@dataclass(frozen=True)
class FinalizationResult:
    """Canonical final artifact and its three-leaf whitelist patch."""

    final_bytes: bytes
    diff_bytes: bytes
    changes: Tuple[str, ...]


def canonical_json_bytes(value: Any) -> bytes:
    """Return the single canonical byte representation used for hashing."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _has_symlink_component(path: Path) -> bool:
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MetaDrivePGError("duplicate JSON object key: {}".format(key))
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise MetaDrivePGError("non-finite JSON number: {}".format(value))


def strict_json_document(payload: bytes, *, label: str) -> Dict[str, Any]:
    """Parse UTF-8 JSON while rejecting duplicates, NaN, and non-objects."""

    if not isinstance(payload, bytes):
        raise MetaDrivePGError("{} must be bytes".format(label))
    try:
        text = payload.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, MetaDrivePGError) as exc:
        raise MetaDrivePGError("{} is not strict UTF-8 JSON: {}".format(label, exc)) from exc
    if not isinstance(value, dict):
        raise MetaDrivePGError("{} must contain one JSON object".format(label))
    return value


def _exact_keys(value: Any, expected: Iterable[str], label: str) -> Mapping[str, Any]:
    expected_set = frozenset(expected)
    if not isinstance(value, Mapping) or frozenset(value) != expected_set:
        missing = sorted(expected_set - frozenset(value) if isinstance(value, Mapping) else expected_set)
        unknown = sorted(frozenset(value) - expected_set) if isinstance(value, Mapping) else []
        raise MetaDrivePGError(
            "{} has missing/unknown fields (missing={}, unknown={})".format(
                label, missing, unknown
            )
        )
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetaDrivePGError("{} must be a non-empty string".format(label))
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise MetaDrivePGError(
            "{} must match the restricted identifier syntax".format(label)
        )
    return value


def _finite_number(
    value: Any,
    label: str,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    if isinstance(value, bool) or type(value) not in (int, float) or not math.isfinite(value):
        raise MetaDrivePGError("{} must be a finite JSON number".format(label))
    number = float(value)
    if minimum is not None and number < minimum:
        raise MetaDrivePGError("{} must be >= {}".format(label, minimum))
    if maximum is not None and number > maximum:
        raise MetaDrivePGError("{} must be <= {}".format(label, maximum))
    return number


def _strict_integer(
    value: Any, label: str, *, minimum: Optional[int] = None, maximum: Optional[int] = None
) -> int:
    if type(value) is not int:
        raise MetaDrivePGError("{} must be a JSON integer".format(label))
    if minimum is not None and value < minimum:
        raise MetaDrivePGError("{} must be >= {}".format(label, minimum))
    if maximum is not None and value > maximum:
        raise MetaDrivePGError("{} must be <= {}".format(label, maximum))
    return value


def _point(value: Any, label: str) -> None:
    if not isinstance(value, list) or len(value) != 2:
        raise MetaDrivePGError("{} must be a two-number array".format(label))
    _finite_number(value[0], "{}[0]".format(label), minimum=-10000, maximum=10000)
    _finite_number(value[1], "{}[1]".format(label), minimum=-10000, maximum=10000)


def _orientation(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> float:
    return (float(b[0]) - float(a[0])) * (float(c[1]) - float(a[1])) - (
        float(b[1]) - float(a[1])
    ) * (float(c[0]) - float(a[0]))


def _on_segment(a: Sequence[float], b: Sequence[float], point: Sequence[float]) -> bool:
    epsilon = 1e-9
    return (
        min(float(a[0]), float(b[0])) - epsilon
        <= float(point[0])
        <= max(float(a[0]), float(b[0])) + epsilon
        and min(float(a[1]), float(b[1])) - epsilon
        <= float(point[1])
        <= max(float(a[1]), float(b[1])) + epsilon
    )


def _segments_intersect(
    a: Sequence[float],
    b: Sequence[float],
    c: Sequence[float],
    d: Sequence[float],
) -> bool:
    epsilon = 1e-9
    o1, o2 = _orientation(a, b, c), _orientation(a, b, d)
    o3, o4 = _orientation(c, d, a), _orientation(c, d, b)
    if (o1 > epsilon and o2 < -epsilon or o1 < -epsilon and o2 > epsilon) and (
        o3 > epsilon and o4 < -epsilon or o3 < -epsilon and o4 > epsilon
    ):
        return True
    return any(
        abs(orientation) <= epsilon and _on_segment(start, end, point)
        for orientation, start, end, point in (
            (o1, a, b, c),
            (o2, a, b, d),
            (o3, c, d, a),
            (o4, c, d, b),
        )
    )


def _simple_nonzero_polygon(polygon: Sequence[Sequence[float]], label: str) -> None:
    vertices = list(polygon)
    if vertices[0] == vertices[-1]:
        vertices = vertices[:-1]
    if len(vertices) < 3 or len({(float(point[0]), float(point[1])) for point in vertices}) < 3:
        raise MetaDrivePGError("{} requires at least three distinct vertices".format(label))
    twice_area = sum(
        float(vertices[index][0]) * float(vertices[(index + 1) % len(vertices)][1])
        - float(vertices[(index + 1) % len(vertices)][0]) * float(vertices[index][1])
        for index in range(len(vertices))
    )
    if abs(twice_area) <= 1e-9:
        raise MetaDrivePGError("{} must have non-zero area".format(label))
    edges = [
        (vertices[index], vertices[(index + 1) % len(vertices)])
        for index in range(len(vertices))
    ]
    for first in range(len(edges)):
        for second in range(first + 1, len(edges)):
            if second in {first, (first + 1) % len(edges)} or first == (second + 1) % len(edges):
                continue
            if _segments_intersect(*edges[first], *edges[second]):
                raise MetaDrivePGError("{} must not self-intersect".format(label))


def _file_binding(value: Any, label: str, *, verify_files: bool) -> None:
    binding = _exact_keys(value, _HASH_KEYS, label)
    path_text = _nonempty_string(binding["path"], "{}.path".format(label))
    digest = binding["sha256"]
    size = binding["bytes"]
    if not isinstance(digest, str) or len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise MetaDrivePGError("{}.sha256 must be a lowercase SHA-256".format(label))
    _strict_integer(size, "{}.bytes".format(label), minimum=0)
    if verify_files:
        path = Path(path_text)
        if not path.is_absolute():
            raise MetaDrivePGError("{}.path must be absolute".format(label))
        if _has_symlink_component(path) or not path.is_file():
            raise MetaDrivePGError("{} is not a regular non-symlink file".format(label))
        if path.stat().st_size != size or sha256_file(path) != digest:
            raise MetaDrivePGError("{} file binding does not match live bytes".format(label))


def validate_token_registry(
    registry: Mapping[str, Any], *, formal: bool = False, verify_files: bool = True
) -> Dict[str, str]:
    """Validate an audited token registry and return ``token -> class name``.

    ``formal=True`` additionally requires a frozen registry.  Source bindings
    cover the exact MetaDrive files which define the string-token interpreter;
    the runtime may re-run this check before constructing a map.
    """

    value = _exact_keys(registry, _REGISTRY_KEYS, "token registry")
    if value["schema_version"] != SCHEMA_VERSION:
        raise MetaDrivePGError("token registry schema_version is invalid")
    if value["decision_status"] not in ("draft", "frozen"):
        raise MetaDrivePGError("token registry decision_status is invalid")
    if formal and value["decision_status"] != "frozen":
        raise MetaDrivePGError("formal generation requires a frozen token registry")
    if value["registry_id"] != REGISTRY_ID or value["artifact_type"] != ARTIFACT_TYPE:
        raise MetaDrivePGError("token registry identity is invalid")
    _nonempty_string(value["metadrive_version"], "token registry metadrive_version")

    constructor = _exact_keys(
        value["constructor_contract"],
        {
            "generation_type",
            "map_config_type",
            "config_value_type",
            "implicit_first_token",
            "class_list_allowed",
            "path_input_allowed",
        },
        "token registry constructor_contract",
    )
    if constructor != {
        "generation_type": "block_sequence",
        "map_config_type": "block_sequence",
        "config_value_type": "string",
        "implicit_first_token": IMPLICIT_FIRST_TOKEN,
        "class_list_allowed": False,
        "path_input_allowed": False,
    }:
        raise MetaDrivePGError("token registry constructor contract is not approved")

    source_files = value["source_files"]
    if not isinstance(source_files, list) or not source_files:
        raise MetaDrivePGError("token registry source_files must be non-empty")
    source_paths = set()
    for index, binding in enumerate(source_files):
        _file_binding(binding, "token registry source_files[{}]".format(index), verify_files=verify_files)
        path = binding["path"]
        if path in source_paths:
            raise MetaDrivePGError("token registry source_files paths must be unique")
        source_paths.add(path)

    tokens = value["tokens"]
    if not isinstance(tokens, list) or not tokens:
        raise MetaDrivePGError("token registry tokens must be non-empty")
    mapping: Dict[str, str] = {}
    class_names = set()
    for index, item in enumerate(tokens):
        entry = _exact_keys(
            item,
            {"token", "class_name", "map_parameter_constraints"},
            "token registry tokens[{}]".format(index),
        )
        token = entry["token"]
        class_name = _nonempty_string(
            entry["class_name"], "token registry tokens[{}].class_name".format(index)
        )
        if not isinstance(token, str) or len(token) != 1 or token.isspace():
            raise MetaDrivePGError("registry tokens must be single non-whitespace characters")
        if token == IMPLICIT_FIRST_TOKEN:
            raise MetaDrivePGError("the implicit first token I must not be generator-selectable")
        if token in mapping or class_name in class_names:
            raise MetaDrivePGError("registry token and class names must be unique")
        constraints = _exact_keys(
            entry["map_parameter_constraints"],
            {"lane_num_allowed"},
            "token registry tokens[{}].map_parameter_constraints".format(index),
        )
        lane_num_allowed = constraints["lane_num_allowed"]
        if (
            not isinstance(lane_num_allowed, list)
            or not lane_num_allowed
            or any(type(lane_num) is not int for lane_num in lane_num_allowed)
            or lane_num_allowed != sorted(set(lane_num_allowed))
            or tuple(lane_num_allowed) != AUDITED_TOKEN_LANE_NUMS.get(token)
        ):
            raise MetaDrivePGError(
                "registry token lane_num constraint differs from the audited executable contract"
            )
        mapping[token] = class_name
        class_names.add(class_name)
    if any(AUDITED_TOKEN_CLASSES.get(token) != class_name for token, class_name in mapping.items()):
        raise MetaDrivePGError(
            "token registry contains a non-audited token/class mapping"
        )
    if formal and mapping != AUDITED_TOKEN_CLASSES:
        raise MetaDrivePGError(
            "formal token registry must equal the complete audited executable whitelist"
        )
    return mapping


def token_registry_hash(registry: Mapping[str, Any]) -> str:
    """Hash registry semantics independent of pretty-printing."""

    validate_token_registry(registry, formal=False, verify_files=False)
    return sha256_bytes(canonical_json_bytes(dict(registry)))


def load_token_registry(
    path: Path,
    *,
    expected_sha256: Optional[str] = None,
    formal: bool = False,
    verify_files: bool = True,
) -> Tuple[Dict[str, Any], str]:
    """Load, validate, and hash one registry file.

    The returned hash is the SHA-256 of the exact file bytes, because that is
    what method configs and generated artifacts bind.  Canonical registry
    semantics remain available through :func:`token_registry_hash`.
    """

    path = Path(path)
    if _has_symlink_component(path) or not path.is_file():
        raise MetaDrivePGError("token registry path must be a regular non-symlink file")
    payload = path.read_bytes()
    digest = sha256_bytes(payload)
    if expected_sha256 is not None and digest != expected_sha256:
        raise MetaDrivePGError("token registry file hash mismatch")
    registry = strict_json_document(payload, label="token registry")
    validate_token_registry(registry, formal=formal, verify_files=verify_files)
    return registry, digest


def _trajectory(value: Any, horizon_steps: int, label: str) -> None:
    if not isinstance(value, list) or len(value) != horizon_steps:
        raise MetaDrivePGError("{} must contain exactly horizon_steps states".format(label))
    for expected_step, state in enumerate(value):
        record = _exact_keys(state, _STATE_KEYS, "{}[{}]".format(label, expected_step))
        if record["step"] != expected_step:
            raise MetaDrivePGError("{} steps must be contiguous from zero".format(label))
        _point(record["position_m"], "{}[{}].position_m".format(label, expected_step))
        _finite_number(
            record["heading_rad"],
            "{}[{}].heading_rad".format(label, expected_step),
            minimum=-1000,
            maximum=1000,
        )
        _finite_number(
            record["speed_m_s"],
            "{}[{}].speed_m_s".format(label, expected_step),
            minimum=0,
            maximum=100,
        )
        if type(record["valid"]) is not bool:
            raise MetaDrivePGError("{}[{}].valid must be boolean".format(label, expected_step))


def validate_artifact(
    artifact: Mapping[str, Any],
    registry: Mapping[str, Any],
    registry_file_sha256: str,
) -> Dict[str, Any]:
    """Validate the complete native PG artifact and return it unchanged."""

    token_map = validate_token_registry(registry, formal=False, verify_files=False)
    if not isinstance(artifact, Mapping):
        raise MetaDrivePGError("MetaDrive PG artifact must be an object")
    keys = frozenset(artifact)
    if not _ARTIFACT_KEYS <= keys or keys - _ARTIFACT_KEYS - _OPTIONAL_ARTIFACT_KEYS:
        raise MetaDrivePGError("MetaDrive PG artifact has missing or unknown fields")
    value = artifact
    if value["schema_version"] != SCHEMA_VERSION or value["artifact_type"] != ARTIFACT_TYPE:
        raise MetaDrivePGError("MetaDrive PG artifact identity is invalid")
    if value["time_step_s"] != 0.1:
        raise MetaDrivePGError("time_step_s must be exactly 0.1")
    horizon_steps = _strict_integer(value["horizon_steps"], "horizon_steps", minimum=1, maximum=36000)

    map_config = _exact_keys(value["map"], _MAP_KEYS, "artifact map")
    if map_config["generation_type"] != "block_sequence":
        raise MetaDrivePGError("artifact map generation_type must be block_sequence")
    sequence = _nonempty_string(map_config["block_sequence"], "artifact map block_sequence")
    if len(sequence) > 64:
        raise MetaDrivePGError("artifact map block_sequence exceeds 64 tokens")
    if IMPLICIT_FIRST_TOKEN in sequence:
        raise MetaDrivePGError("block_sequence must not contain implicit token I")
    unknown = sorted(set(sequence) - set(token_map))
    if unknown:
        raise MetaDrivePGError("block_sequence contains non-whitelisted tokens: {}".format(unknown))
    if map_config["token_registry_id"] != registry["registry_id"]:
        raise MetaDrivePGError("artifact token_registry_id does not match the bound registry")
    if map_config["token_registry_sha256"] != registry_file_sha256:
        raise MetaDrivePGError("artifact token_registry_sha256 does not match the bound registry")
    _strict_integer(map_config["map_seed"], "artifact map map_seed", minimum=0, maximum=2**31 - 1)
    lane_num = _strict_integer(
        map_config["lane_num"], "artifact map lane_num", minimum=1, maximum=5
    )
    registry_lane_num_constraints = {
        entry["token"]: tuple(
            entry["map_parameter_constraints"]["lane_num_allowed"]
        )
        for entry in registry["tokens"]
    }
    incompatible_tokens = sorted(
        token
        for token in set(sequence)
        if lane_num not in registry_lane_num_constraints[token]
    )
    if incompatible_tokens:
        raise MetaDrivePGError(
            "block_sequence tokens {} do not allow lane_num {}".format(
                incompatible_tokens, lane_num
            )
        )
    _finite_number(map_config["lane_width_m"], "artifact map lane_width_m", minimum=2.0, maximum=6.0)
    _finite_number(map_config["exit_length_m"], "artifact map exit_length_m", minimum=5.0, maximum=500.0)

    ego = _exact_keys(value["ego"], _EGO_KEYS, "artifact ego")
    if ego["actor_id"] != "ego" or ego["actor_type"] != "vehicle":
        raise MetaDrivePGError("artifact ego must be the single vehicle actor_id ego")
    if ego["vehicle_model"] not in _VEHICLE_MODELS:
        raise MetaDrivePGError("artifact ego vehicle_model is not an approved native model")
    _finite_number(ego["length_m"], "artifact ego length_m", minimum=0.1, maximum=30.0)
    _finite_number(ego["width_m"], "artifact ego width_m", minimum=0.1, maximum=5.0)
    _trajectory(ego["reference_trajectory"], horizon_steps, "artifact ego reference_trajectory")
    if ego["reference_trajectory"][0]["valid"] is not True:
        raise MetaDrivePGError("ego reference_trajectory step 0 must be valid")

    actors = value["actors"]
    if not isinstance(actors, list) or len(actors) > 128:
        raise MetaDrivePGError("artifact actors must be an array with at most 128 entries")
    actor_ids = {"ego"}
    for index, actor in enumerate(actors):
        record = _exact_keys(actor, _ACTOR_KEYS, "artifact actors[{}]".format(index))
        actor_id = _identifier(record["actor_id"], "artifact actors[{}].actor_id".format(index))
        if actor_id in actor_ids:
            raise MetaDrivePGError("artifact actor_id values must be unique")
        actor_ids.add(actor_id)
        if record["actor_type"] not in _ACTOR_TYPES:
            raise MetaDrivePGError("artifact actor_type is not approved")
        if record["motion_mode"] != "replay_trajectory":
            raise MetaDrivePGError("artifact actors must use replay_trajectory motion")
        _finite_number(record["length_m"], "artifact actors[{}].length_m".format(index), minimum=0.1, maximum=30.0)
        _finite_number(record["width_m"], "artifact actors[{}].width_m".format(index), minimum=0.1, maximum=5.0)
        _trajectory(record["trajectory"], horizon_steps, "artifact actors[{}].trajectory".format(index))
        if not any(state["valid"] for state in record["trajectory"]):
            raise MetaDrivePGError(
                "artifact actors[{}].trajectory requires at least one valid state".format(
                    index
                )
            )

    regions = value.get("semantic_regions", [])
    if not isinstance(regions, list) or len(regions) > 128:
        raise MetaDrivePGError(
            "artifact semantic_regions must be an array with at most 128 entries"
        )
    region_ids = set()
    for index, region in enumerate(regions):
        record = _exact_keys(
            region, _REGION_KEYS, "artifact semantic_regions[{}]".format(index)
        )
        region_id = _identifier(
            record["region_id"],
            "artifact semantic_regions[{}].region_id".format(index),
        )
        if region_id in region_ids:
            raise MetaDrivePGError("artifact semantic region IDs must be unique")
        region_ids.add(region_id)
        if record["region_type"] not in _REGION_TYPES:
            raise MetaDrivePGError("artifact semantic region_type is not approved")
        polygon = record["polygon_m"]
        if not isinstance(polygon, list) or not 3 <= len(polygon) <= 128:
            raise MetaDrivePGError(
                "artifact semantic region polygon_m must contain 3..128 points"
            )
        for point_index, point in enumerate(polygon):
            _point(
                point,
                "artifact semantic_regions[{}].polygon_m[{}]".format(
                    index, point_index
                ),
            )
        _simple_nonzero_polygon(
            polygon, "artifact semantic_regions[{}].polygon_m".format(index)
        )
    return dict(artifact)


def parse_artifact(
    payload: bytes,
    registry: Mapping[str, Any],
    registry_file_sha256: str,
) -> Dict[str, Any]:
    artifact = strict_json_document(payload, label="MetaDrive PG artifact")
    validate_artifact(artifact, registry, registry_file_sha256)
    return artifact


def semantic_skeleton(artifact: Mapping[str, Any]) -> Dict[str, Any]:
    """Mask only the three post-generation ego proxy leaves."""

    skeleton = copy.deepcopy(dict(artifact))
    ego = skeleton.get("ego")
    if not isinstance(ego, dict):
        raise MetaDrivePGError("artifact ego is malformed")
    ego["vehicle_model"] = "<EGO_VEHICLE_MODEL>"
    ego["length_m"] = "<EGO_LENGTH_M>"
    ego["width_m"] = "<EGO_WIDTH_M>"
    return skeleton


def verify_finalization(
    raw_payload: bytes,
    final_payload: bytes,
    registry: Mapping[str, Any],
    registry_file_sha256: str,
) -> None:
    raw = parse_artifact(raw_payload, registry, registry_file_sha256)
    final = parse_artifact(final_payload, registry, registry_file_sha256)
    if canonical_json_bytes(semantic_skeleton(raw)) != canonical_json_bytes(
        semantic_skeleton(final)
    ):
        raise MetaDrivePGError(
            "MetaDrive PG finalization changed semantics outside approved ego proxy fields"
        )
    ego = final["ego"]
    if ego["vehicle_model"] != EGO_VEHICLE_MODEL:
        raise MetaDrivePGError("finalized ego vehicle_model must be xl")
    if ego["length_m"] != EGO_LENGTH_M:
        raise MetaDrivePGError("finalized ego length_m must be 5.33")
    if ego["width_m"] != EGO_WIDTH_M:
        raise MetaDrivePGError("finalized ego width_m must be 2.10")


def finalize_artifact(
    raw_payload: bytes,
    registry: Mapping[str, Any],
    registry_file_sha256: str,
) -> FinalizationResult:
    """Apply the exact three-leaf ego proxy whitelist and canonicalize JSON."""

    document = parse_artifact(raw_payload, registry, registry_file_sha256)
    ego = document["ego"]
    changes = []
    operations = []
    targets = (
        ("ego_vehicle_model", "/ego/vehicle_model", "vehicle_model", EGO_VEHICLE_MODEL),
        ("ego_length_m", "/ego/length_m", "length_m", EGO_LENGTH_M),
        ("ego_width_m", "/ego/width_m", "width_m", EGO_WIDTH_M),
    )
    for change_name, path, field, desired in targets:
        if ego[field] != desired:
            operations.append({"path": path, "before": ego[field], "after": desired})
            ego[field] = desired
            changes.append(change_name)
    final_payload = canonical_json_bytes(document)
    verify_finalization(raw_payload, final_payload, registry, registry_file_sha256)
    diff_payload = canonical_json_bytes(
        {
            "format": "json_leaf_patch_v0.1",
            "canonical_json_normalization": raw_payload != final_payload,
            "operations": operations,
        }
    )
    return FinalizationResult(final_payload, diff_payload, tuple(changes))


__all__ = [
    "ARTIFACT_TYPE",
    "AUDITED_TOKEN_CLASSES",
    "AUDITED_TOKEN_LANE_NUMS",
    "EGO_LENGTH_M",
    "EGO_VEHICLE_MODEL",
    "EGO_WIDTH_M",
    "FINALIZATION_POLICY_ID",
    "FinalizationResult",
    "IMPLICIT_FIRST_TOKEN",
    "MetaDrivePGError",
    "REGISTRY_ID",
    "canonical_json_bytes",
    "finalize_artifact",
    "load_token_registry",
    "parse_artifact",
    "semantic_skeleton",
    "sha256_bytes",
    "sha256_file",
    "strict_json_document",
    "token_registry_hash",
    "validate_artifact",
    "validate_token_registry",
    "verify_finalization",
]
