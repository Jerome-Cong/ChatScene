"""Strict, query-blind CARLA realization format shared by native methods."""

import copy
import hashlib
import json
import math
import re

ARTIFACT_TYPE = "carla_native_scene_v0.1"


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _number(v):
    try:
        return type(v) in (int, float) and math.isfinite(v)
    except (OverflowError, ValueError):
        return False


def _vector(v, n):
    return (
        isinstance(v, list)
        and len(v) == n
        and all(_number(x) and abs(x) <= 1e6 for x in v)
    )


def _transform(v):
    return (
        isinstance(v, dict)
        and set(v) == {"location_m", "rotation_deg"}
        and _vector(v["location_m"], 3)
        and _vector(v["rotation_deg"], 3)
    )


def validate(document):
    _require(
        isinstance(document, dict)
        and set(document)
        == {
            "artifact_type",
            "coordinate_frame",
            "time_step_s",
            "carla_version",
            "map",
            "weather",
            "actors",
            "frames",
            "termination",
        },
        "native artifact fields are invalid",
    )
    _require(
        document["artifact_type"] == ARTIFACT_TYPE
        and document["coordinate_frame"] == "carla_left_handed_xyz_m",
        "unsupported native format or coordinates",
    )
    _require(
        _number(document["time_step_s"]) and abs(document["time_step_s"] - 0.1) < 1e-8,
        "native CARLA timestep must be 0.1 seconds",
    )
    _require(
        isinstance(document["carla_version"], str) and document["carla_version"],
        "native server version missing",
    )
    m = document["map"]
    _require(
        isinstance(m, dict)
        and set(m) == {"name", "opendrive", "sha256"}
        and isinstance(m["name"], str)
        and re.fullmatch(r"Town[0-9]+(?:HD)?", m["name"]),
        "native map metadata invalid",
    )
    _require(
        isinstance(m["opendrive"], str)
        and "<OpenDRIVE" in m["opendrive"]
        and hashlib.sha256(m["opendrive"].encode()).hexdigest() == m["sha256"],
        "native OpenDRIVE binding invalid",
    )
    _require(
        isinstance(document["weather"], dict)
        and document["weather"]
        and all(
            isinstance(k, str) and _number(v) for k, v in document["weather"].items()
        ),
        "native weather invalid",
    )
    actors = document["actors"]
    _require(
        isinstance(actors, list) and 0 < len(actors) <= 1000,
        "native actor count invalid",
    )
    seen = set()
    for actor in actors:
        _require(
            isinstance(actor, dict)
            and set(actor)
            == {
                "id",
                "is_ego",
                "blueprint",
                "kind",
                "length_m",
                "width_m",
                "initial_transform",
            },
            "native actor fields invalid",
        )
        _require(
            isinstance(actor["id"], str) and actor["id"] not in seen and actor["id"],
            "native actor ID invalid or duplicated",
        )
        seen.add(actor["id"])
        _require(
            type(actor["is_ego"]) is bool
            and isinstance(actor["blueprint"], str)
            and actor["blueprint"].startswith(("vehicle.", "walker.")),
            "native actor role/blueprint invalid",
        )
        _require(
            actor["kind"] in ("vehicle", "motorcycle", "cyclist", "pedestrian")
            and _number(actor["length_m"])
            and 0 < actor["length_m"] <= 100
            and _number(actor["width_m"])
            and 0 < actor["width_m"] <= 100
            and _transform(actor["initial_transform"]),
            "native actor geometry invalid",
        )
    _require(
        sum(a["is_ego"] for a in actors) == 1 and actors[0]["is_ego"],
        "native scene must have one first ego actor",
    )
    frames = document["frames"]
    _require(
        isinstance(frames, list) and 1 < len(frames) <= 10000,
        "native frames must contain a rollout",
    )
    alive = [True] * len(actors)
    for i, frame in enumerate(frames):
        _require(
            isinstance(frame, dict)
            and set(frame) == {"step", "elapsed_seconds", "states"}
            and type(frame["step"]) is int
            and frame["step"] == i
            and _number(frame["elapsed_seconds"])
            and abs(frame["elapsed_seconds"] - i * document["time_step_s"]) < 1e-4,
            "native frame timeline invalid",
        )
        _require(
            isinstance(frame["states"], list) and len(frame["states"]) == len(actors),
            "native actor masks are incomplete",
        )
        for j, state in enumerate(frame["states"]):
            _require(
                isinstance(state, dict) and type(state.get("valid")) is bool,
                "native state mask invalid",
            )
            if not state["valid"]:
                _require(
                    set(state) == {"valid"},
                    "invalid native actor must not contain invented state",
                )
                alive[j] = False
                continue
            _require(
                alive[j]
                and set(state) == {"valid", "transform", "velocity_m_s", "control"}
                and _transform(state["transform"])
                and _vector(state["velocity_m_s"], 3),
                "native actor state invalid or reappears",
            )
            c = state["control"]
            if actors[j]["kind"] == "pedestrian":
                _require(
                    isinstance(c, dict)
                    and set(c) == {"type", "speed", "direction", "jump"}
                    and c["type"] == "walker"
                    and _number(c["speed"])
                    and 0 <= c["speed"] <= 20
                    and _vector(c["direction"], 3)
                    and type(c["jump"]) is bool,
                    "native walker control invalid",
                )
            else:
                _require(
                    isinstance(c, dict)
                    and set(c)
                    == {
                        "type",
                        "throttle",
                        "brake",
                        "steer",
                        "hand_brake",
                        "reverse",
                        "manual_gear_shift",
                        "gear",
                    }
                    and c["type"] == "vehicle"
                    and all(_number(c[k]) for k in ("throttle", "brake", "steer"))
                    and 0 <= c["throttle"] <= 1
                    and 0 <= c["brake"] <= 1
                    and -1 <= c["steer"] <= 1
                    and all(
                        type(c[k]) is bool
                        for k in ("hand_brake", "reverse", "manual_gear_shift")
                    )
                    and type(c["gear"]) is int
                    and -1 <= c["gear"] <= 20,
                    "native vehicle control invalid",
                )
    _require(
        all(s["valid"] for s in frames[0]["states"]),
        "all native actors must exist initially",
    )
    _require(
        all(
            a["initial_transform"] == state["transform"]
            for a, state in zip(actors, frames[0]["states"])
        ),
        "native initial transform differs from authoritative first frame",
    )
    terminal = document["termination"]
    _require(
        isinstance(terminal, dict)
        and type(terminal.get("step")) is int
        and terminal["step"] == len(frames) - 1
        and all(not s["valid"] for s in frames[-1]["states"]),
        "native capture lacks observed cleanup termination",
    )
    if terminal.get("kind") == "native_program_complete":
        _require(
            set(terminal) == {"kind", "step"}, "native completion metadata invalid"
        )
    else:
        _require(
            set(terminal) == {"kind", "step", "budget_seconds"}
            and terminal["kind"] == "native_observation_budget_complete"
            and terminal["budget_seconds"] == 30.0
            and 301 <= terminal["step"] <= 304
            and frames[300]["states"][0]["valid"]
            and all(not frame["states"][0]["valid"] for frame in frames[301:]),
            "native observation budget must contain actual complete 30-second observations and cleanup",
        )
    return document


def parse(raw):
    def pairs(values):
        result = {}
        for k, v in values:
            _require(k not in result, "duplicate native JSON key")
            result[k] = v
        return result

    try:
        return validate(
            json.loads(
                raw,
                object_pairs_hook=pairs,
                parse_constant=lambda v: (_ for _ in ()).throw(
                    ValueError("nonfinite native JSON")
                ),
            )
        )
    except RecursionError as exc:
        raise ValueError("native JSON nesting exceeds the parser limit") from exc


def finalize(raw):
    document = copy.deepcopy(parse(raw))
    ego = document["actors"][0]
    ego.update(blueprint="vehicle.chevrolet.impala", length_m=5.33, width_m=2.1)
    final = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return final


def observe(document):
    validate(document)
    # Use a common actually-observed prefix, never pad a disappeared actor.
    end = next(
        (
            i
            for i, f in enumerate(document["frames"])
            if not all(s["valid"] for s in f["states"])
        ),
        len(document["frames"]),
    )
    objects = []
    for i, a in enumerate(document["actors"]):
        positions = [
            [
                f["states"][i]["transform"]["location_m"][0],
                -f["states"][i]["transform"]["location_m"][1],
            ]
            for f in document["frames"][:end]
        ]
        objects.append(
            {
                "object_index": i,
                "kind": {
                    "vehicle": "Car",
                    "motorcycle": "Motorcycle",
                    "cyclist": "Bicycle",
                    "pedestrian": "Pedestrian",
                }[a["kind"]],
                "is_ego": a["is_ego"],
                "initial_position": positions[0],
                "trajectory": positions,
                "heading_rad": -math.radians(a["initial_transform"]["rotation_deg"][1]),
                "length_m": a["length_m"],
                "width_m": a["width_m"],
                "blueprint": a["blueprint"],
            }
        )
    return {"platform": "carla", "objects": objects}
