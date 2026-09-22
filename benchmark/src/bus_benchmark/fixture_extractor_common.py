"""Oracle-blind semantic extractor for arbitrary platform-native artifacts.

The artifact is first accepted by the platform's own parser/loader.  The rules
below use relative geometry and trajectory shape rather than fixture coordinates,
case IDs, query text, or a requirement oracle.  The formal fixtures exercise this
same implementation used for development evidence and locked-test scoring.
"""

import json
import math
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OBSERVER = ROOT / "native_observer.py"
SCENIC_PYTHON = os.environ.get(
    "BUS_BENCHMARK_SCENIC_PYTHON",
    sys.executable,
)
METADRIVE_PYTHON = os.environ.get(
    "BUS_BENCHMARK_METADRIVE_PYTHON",
    "/home/shijie20/micromamba/envs/scenarionet/bin/python",
)

ALL_CATEGORIES = ("actor", "road", "spatial", "event", "temporal", "normative")

COMMON_CANDIDATE_EMITTERS = {
    "actor_longitudinal_distance_bin": {
        "selector_id": "anonymous_road_user_distance_candidates_v1",
        "supported_platforms": ["carla", "metadrive"],
        "anonymous": True,
        "target_signature_fields": ["actor_class"],
    },
    "yield_realization_mode": {
        "selector_id": "anonymous_yield_subject_candidates_v1",
        "supported_platforms": ["carla", "metadrive"],
        "anonymous": True,
        "target_signature_fields": ["actor_class"],
    },
    "merge_gap_relation": {
        "selector_id": "anonymous_merge_gap_candidates_v1",
        "supported_platforms": ["carla", "metadrive"],
        "anonymous": True,
        "target_signature_fields": ["actor_class"],
    },
}


def _observe(platform, text, token_registry_path=None):
    interpreter = SCENIC_PYTHON if platform == "carla" else METADRIVE_PYTHON
    argv = [interpreter, str(OBSERVER), platform]
    if platform == "metadrive":
        if not token_registry_path:
            raise ValueError("MetaDrive extraction requires one token registry dependency")
        argv.extend(["--token-registry", str(token_registry_path)])
    process = subprocess.run(
        argv,
        input=text.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "MPLCONFIGDIR": "/tmp",
            "PYTHONHASHSEED": "0",
        },
    )
    if process.returncode != 0:
        raise RuntimeError(
            "native {} validation failed: {}".format(
                platform, process.stderr.decode("utf-8", errors="replace")[-1000:]
            )
        )
    return json.loads(process.stdout.decode("utf-8"))


def _model_from_metadata(metadata):
    spawn = metadata.get("policy_spawn_info", {})
    kwargs = spawn.get("kwargs", {}) if isinstance(spawn, dict) else {}
    config = kwargs.get("vehicle_config", {}) if isinstance(kwargs, dict) else {}
    return config.get("vehicle_model", "") if isinstance(config, dict) else ""


def _normalize(platform, observation):
    tracks = []
    features = []
    if platform == "carla":
        for obj in observation["objects"]:
            tracks.append(
                {
                    "id": "o{}".format(obj["object_index"]),
                    "kind": obj["kind"],
                    "is_ego": obj["is_ego"],
                    "positions": obj["trajectory"],
                    "heading_rad": float(obj.get("heading_rad", 0.0)),
                    "length_m": obj["length_m"],
                    "width_m": obj["width_m"],
                    "blueprint": obj["blueprint"],
                    "vehicle_model": "",
                }
            )
    else:
        sdc_id = observation["sdc_id"]
        for track_id, track in observation["tracks"].items():
            state = track["state"]
            kind = track["type"]
            if (
                kind == "VEHICLE"
                and float(state["length"][0]) <= 2.5
                and float(state["width"][0]) <= 1.2
            ):
                kind = "Motorcycle"
            tracks.append(
                {
                    "id": track_id,
                    "kind": kind,
                    "is_ego": track_id == sdc_id,
                    "positions": [point[:2] for point in state["position"]],
                    "heading_rad": float(state.get("heading", [0.0])[0]),
                    "length_m": float(state["length"][0]),
                    "width_m": float(state["width"][0]),
                    "blueprint": "",
                    "vehicle_model": _model_from_metadata(track["metadata"]),
                }
            )
        features = list(observation["map_features"].values())
    return tracks, features


def _atom(category, predicate, arguments):
    return {
        "category": category,
        "predicate": predicate,
        "arguments": arguments,
        "polarity": "present",
        "evidence_source": "deterministic",
    }


def _distance(left, right):
    return math.hypot(left[0] - right[0], left[1] - right[1])


def _padded_positions(track, length):
    values = list(track["positions"])
    if not values:
        values = [[0.0, 0.0]]
    return values + [values[-1]] * (length - len(values))


def _is_stationary(track, tolerance=0.25):
    positions = track.get("positions", [])
    return bool(positions) and all(
        _distance(point, positions[0]) <= tolerance for point in positions
    )


def _motion_axis(track):
    positions = track.get("positions", [])
    if positions:
        origin = positions[0]
        for point in positions[1:]:
            dx = point[0] - origin[0]
            dy = point[1] - origin[1]
            norm = math.hypot(dx, dy)
            if norm > 0.5:
                return (dx / norm, dy / norm)
    heading = track.get("heading_rad")
    if isinstance(heading, (int, float)) and math.isfinite(float(heading)):
        return (math.cos(float(heading)), math.sin(float(heading)))
    return (1.0, 0.0)


def _relative_components(origin, point, axis):
    dx = point[0] - origin[0]
    dy = point[1] - origin[1]
    return (
        dx * axis[0] + dy * axis[1],
        -dx * axis[1] + dy * axis[0],
    )


def _feature_mean_lateral(feature, ego):
    points = feature.get("polyline") or feature.get("polygon") or []
    if not points:
        return None
    origin = ego["positions"][0]
    axis = _motion_axis(ego)
    return sum(
        _relative_components(origin, point, axis)[1] for point in points
    ) / len(points)


def _lateral_offsets(ego):
    origin = ego["positions"][0]
    axis = _motion_axis(ego)
    return [
        _relative_components(origin, point, axis)[1]
        for point in ego.get("positions", [])
    ]


def _lane_excursion_returns(ego):
    offsets = _lateral_offsets(ego)
    return (
        len(offsets) >= 3
        and max(abs(value - offsets[0]) for value in offsets[1:-1]) >= 1.5
        and abs(offsets[-1] - offsets[0]) <= 0.5
    )


def _changes_lane(ego):
    offsets = _lateral_offsets(ego)
    return len(offsets) >= 2 and abs(offsets[-1] - offsets[0]) >= 1.5


def _ends_stopped(track):
    positions = track.get("positions", [])
    if len(positions) < 2:
        return True
    return _distance(positions[-1], positions[-2]) <= 0.25


def _non_ego(tracks, kinds=None):
    values = [track for track in tracks if not track["is_ego"]]
    if kinds is not None:
        values = [track for track in values if track["kind"] in kinds]
    return values


ROAD_USER_KINDS = {
    "car",
    "vehicle",
    "motorcycle",
    "bicycle",
    "cyclist",
    "pedestrian",
}


def _is_road_user(track):
    return str(track.get("kind", "")).lower() in ROAD_USER_KINDS


def _road_users(tracks, include_ego=False):
    return [
        track
        for track in tracks
        if _is_road_user(track) and (include_ego or not track["is_ego"])
    ]


def _actor_class(track):
    if track.get("is_ego"):
        return "ego_bus"
    kind = str(track.get("kind", "")).lower()
    if kind in ("bicycle", "cyclist"):
        return "cyclist"
    if kind == "pedestrian":
        return "pedestrian"
    return "motor_vehicle"


def _common_candidate(dimension, value, track):
    return {
        "dimension": dimension,
        "value": value,
        "target_signature": {"actor_class": _actor_class(track)},
    }


def _minimum_synchronized_distance(left, right):
    length = max(len(left.get("positions", [])), len(right.get("positions", [])))
    if length == 0:
        return float("inf")
    left_positions = _padded_positions(left, length)
    right_positions = _padded_positions(right, length)
    return min(
        _distance(left_positions[index], right_positions[index])
        for index in range(length)
    )


def _track_moves(track, tolerance=0.5):
    positions = track.get("positions", [])
    return bool(positions) and any(
        _distance(point, positions[0]) > tolerance for point in positions[1:]
    )


def _paths_conflict(left, right):
    left_positions = left.get("positions", [])
    right_positions = right.get("positions", [])
    if not left_positions or not right_positions:
        return False
    # Do not use a stationary actor's simulator-specific spawn heading.  A
    # broad path-proximity gate is platform neutral; synchronized proximity is
    # checked separately before a yield candidate is emitted.
    return min(
        _distance(left_point, right_point)
        for left_point in left_positions
        for right_point in right_positions
    ) <= 10.0


def _yield_mode(track):
    if _is_stationary(track):
        return "hold"
    positions = track.get("positions", [])
    if len(positions) < 4:
        return None
    axis = _motion_axis(track)
    steps = [
        _relative_components(left, right, axis)[0]
        for left, right in zip(positions, positions[1:])
    ]
    midpoint = max(1, len(steps) // 2)
    early = steps[:midpoint]
    late = steps[midpoint:]
    if not late or min(steps) < -0.05:
        return None
    early_mean = sum(early) / len(early)
    late_mean = sum(late) / len(late)
    if early_mean >= late_mean + 0.25 and early_mean >= 1.25 * max(late_mean, 0.05):
        return "decelerate"
    return None


def _ego_projection(platform, tracks):
    egos = [
        track
        for track in tracks
        if track["is_ego"] and track["kind"] in ("Car", "VEHICLE")
    ]
    if len(egos) != 1:
        return {
            "count": len(egos),
            "semantic_role": "missing" if not egos else "other",
            "approved_proxy": False,
            "deterministic": True,
            "length_m": 0.0 if not egos else egos[0]["length_m"],
            "width_m": 0.0 if not egos else egos[0]["width_m"],
            "blueprint": "" if not egos else egos[0]["blueprint"],
            "vehicle_model": "" if not egos else egos[0]["vehicle_model"],
        }
    ego = egos[0]
    proxy = (
        ego["blueprint"] == "vehicle.chevrolet.impala"
        if platform == "carla"
        else ego["vehicle_model"] == "xl"
    )
    blueprint = ego["blueprint"]
    vehicle_model = ego["vehicle_model"]
    if platform == "carla":
        vehicle_model = "xl" if proxy else "small"
    else:
        blueprint = "vehicle.chevrolet.impala" if proxy else "vehicle.wrong"
    return {
        "count": 1,
        "semantic_role": "bus_proxy" if proxy else "other",
        "approved_proxy": proxy,
        "deterministic": True,
        "length_m": ego["length_m"],
        "width_m": ego["width_m"],
        "blueprint": blueprint,
        "vehicle_model": vehicle_model,
    }


def _detect_atoms(platform, tracks, features):
    del platform, features
    atoms = []
    ego = next((track for track in tracks if track["is_ego"]), None)
    others = _non_ego(tracks)

    if ego is not None and others:
        length = max(len(ego["positions"]), *(len(track["positions"]) for track in others))
        ego_positions = _padded_positions(ego, length)
        minimum = min(
            _distance(ego_positions[index], _padded_positions(track, length)[index])
            for track in others
            for index in range(length)
        )
        if 0.25 <= minimum < 1.5:
            atoms.append(_atom("normative", "risk_level", {"value": "high"}))

    return atoms


def _detect_common(platform, tracks, features):
    del platform, features
    common = []
    ego = next((track for track in tracks if track["is_ego"]), None)
    if ego is None:
        return common
    road_users = _road_users(tracks)
    if road_users:
        axis = _motion_axis(ego)
        for target in road_users:
            distance = abs(
                _relative_components(
                    ego["positions"][0],
                    target["positions"][0],
                    axis,
                )[0]
            )
            value = "near" if distance <= 10.0 else "medium" if distance <= 30.0 else "far"
            common.append(
                _common_candidate(
                    "actor_longitudinal_distance_bin", value, target
                )
            )

    if road_users and _changes_lane(ego):
        axis = _motion_axis(ego)
        ego_x = _relative_components(
            ego["positions"][0], ego["positions"][-1], axis
        )[0]
        for target in road_users:
            actor_x = _relative_components(
                ego["positions"][0], target["positions"][-1], axis
            )[0]
            common.append(
                _common_candidate(
                    "merge_gap_relation",
                    "ahead_of_gap_actor" if ego_x > actor_x else "behind_gap_actor",
                    target,
                )
            )

    all_users = [ego] + road_users
    yield_candidates = []
    for subject in all_users:
        mode = _yield_mode(subject)
        counterparts = [
            value
            for value in all_users
            if value is not subject and _paths_conflict(subject, value)
        ]
        if mode is None or not counterparts:
            continue
        conflict_distance = min(
            _minimum_synchronized_distance(subject, counterpart)
            for counterpart in counterparts
        )
        if conflict_distance > 10.0:
            continue
        if mode == "hold" and not any(_track_moves(value) for value in counterparts):
            continue
        yield_candidates.append(
            _common_candidate("yield_realization_mode", mode, subject)
        )
    common.extend(yield_candidates)
    return sorted(
        common,
        key=lambda value: json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )


def extract(payload, platform):
    fields = set(payload)
    fields.discard("artifact_format")
    if fields != {
        "schema_version",
        "platform",
        "terminal_status",
        "disposition",
        "artifact",
    }:
        raise ValueError("extractor input contains fields outside the observation contract")
    if payload["platform"] != platform:
        raise ValueError("extractor platform mismatch")
    token_registry_path = None
    dependencies = payload["artifact"].get("dependencies", [])
    if platform == "metadrive":
        registries = [
            dependency
            for dependency in dependencies
            if dependency.get("kind") == "metadrive_pg_token_registry"
        ]
        if len(registries) != 1 or len(dependencies) != 1:
            raise ValueError(
                "MetaDrive extraction requires exactly one PG token registry dependency"
            )
        token_registry_path = registries[0].get("path")
    observation = _observe(
        platform, payload["artifact"]["text"], token_registry_path
    )
    tracks, features = _normalize(platform, observation)
    return {
        "ego": _ego_projection(platform, tracks),
        "atoms": _detect_atoms(platform, tracks, features),
        "common_atoms": _detect_common(platform, tracks, features),
        "complete_categories": [],
    }
