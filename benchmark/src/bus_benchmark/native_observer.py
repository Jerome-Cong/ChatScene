"""Validate platform-native fixture artifacts and emit label-free observations.

This module is intentionally executable as a standalone helper under the Python
environment which owns Scenic or MetaDrive.  It never imports benchmark oracles,
ontologies, or expected projections.
"""

import argparse
import contextlib
import json
import math
import sys
from collections.abc import Mapping


def _xy(value):
    if hasattr(value, "x") and hasattr(value, "y"):
        return [float(value.x), float(value.y)]
    return [float(value[0]), float(value[1])]


def _numeric_or_zero(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _json_native(value):
    """Convert native simulator containers to strict JSON-compatible values.

    MetaDrive's ``BaseMap.get_map_features`` returns NumPy arrays even though
    the surrounding feature record is a plain mapping.  Keeping those arrays
    in the observer output made the otherwise valid native observation fail at
    the final ``json.dumps`` boundary.  Use the protocol exposed by NumPy (and
    similar array/scalar containers) without importing it into this trusted
    helper, then reject non-finite numbers instead of silently emitting NaN.
    """

    if isinstance(value, Mapping):
        return {str(key): _json_native(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_native(child) for child in value]
    if isinstance(value, (str, bool)) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("native observation contains a non-finite number")
        return float(value)
    if hasattr(value, "tolist"):
        return _json_native(value.tolist())
    if hasattr(value, "item"):
        return _json_native(value.item())
    raise TypeError(
        "native observation contains an unsupported {} value".format(
            type(value).__name__
        )
    )


def _carla_observation(text):
    from scenic.syntax.translator import scenarioFromString

    scenario = scenarioFromString(text)
    objects = []
    for index, actor in enumerate(scenario.objects):
        behavior = getattr(actor, "behavior", None)
        trajectory = []
        if behavior is not None:
            args = getattr(behavior, "_args", ())
            if args and isinstance(args[0], (list, tuple)):
                trajectory = [_xy(point) for point in args[0]]
        initial = _xy(actor.position)
        if not trajectory:
            trajectory = [initial]
        blueprint = getattr(actor, "blueprint", "")
        if not isinstance(blueprint, str):
            blueprint = ""
        objects.append(
            {
                "object_index": index,
                "kind": type(actor).__name__,
                "is_ego": actor is scenario.egoObject,
                "initial_position": initial,
                "trajectory": trajectory,
                "heading_rad": _numeric_or_zero(getattr(actor, "heading", 0.0)),
                "length_m": float(getattr(actor, "length", 0.0)),
                "width_m": float(getattr(actor, "width", 0.0)),
                "blueprint": blueprint,
            }
        )
    return {"platform": "carla", "objects": objects}


def _metadrive_document(text, token_registry_path):
    from pathlib import Path

    import metadrive_pg

    registry_path = Path(token_registry_path).resolve()
    registry, registry_sha256 = metadrive_pg.load_token_registry(
        registry_path, formal=False, verify_files=True
    )
    artifact = metadrive_pg.strict_json_document(
        text.encode("utf-8"), label="MetaDrive PG artifact"
    )
    document = metadrive_pg.validate_artifact(
        artifact,
        registry,
        registry_sha256,
    )
    return document


def _metadrive_environment(document):
    from metadrive.envs.metadrive_env import MetaDriveEnv

    map_spec = document["map"]
    ego_state = document["ego"]["reference_trajectory"][0]
    environment = MetaDriveEnv(
        {
            "map_config": {
                "type": "block_sequence",
                "config": map_spec["block_sequence"],
                "lane_num": map_spec["lane_num"],
                "lane_width": map_spec["lane_width_m"],
                "exit_length": map_spec["exit_length_m"],
            },
            "agent_configs": {
                "default_agent": {
                    "vehicle_model": document["ego"]["vehicle_model"],
                    "top_down_length": document["ego"]["length_m"],
                    "top_down_width": document["ego"]["width_m"],
                    "spawn_position_heading": [
                        list(ego_state["position_m"]),
                        float(ego_state["heading_rad"]),
                    ],
                    "spawn_velocity": [float(ego_state["speed_m_s"]), 0.0],
                    "spawn_velocity_car_frame": True,
                }
            },
            "num_scenarios": 1,
            "start_seed": int(map_spec["map_seed"]),
            "random_spawn_lane_index": False,
            "traffic_density": 0.0,
            "random_traffic": False,
            "random_lane_width": False,
            "random_lane_num": False,
            "decision_repeat": 5,
            "physics_world_step_size": 0.02,
            "use_render": False,
            "show_terrain": False,
            "show_skybox": False,
            "show_interface": False,
            "log_level": 50,
            "horizon": max(1, int(document["horizon_steps"])),
            "truncate_as_terminate": False,
        }
    )
    environment.reset(seed=int(map_spec["map_seed"]))
    actual_blocks = [block.ID for block in environment.current_map.blocks]
    if actual_blocks != ["I"] + list(map_spec["block_sequence"]):
        environment.close()
        raise ValueError("MetaDrive PGMap changed the submitted block-token sequence")
    return environment


def _metadrive_track(actor, trajectory, kind, vehicle_model=""):
    return {
        "type": kind,
        "state": {
            "position": [list(state["position_m"]) for state in trajectory],
            "heading": [float(state["heading_rad"]) for state in trajectory],
            "valid": [bool(state["valid"]) for state in trajectory],
            "length": [float(actor["length_m"])] * len(trajectory),
            "width": [float(actor["width_m"])] * len(trajectory),
        },
        "metadata": {
            "policy_spawn_info": {
                "kwargs": {"vehicle_config": {"vehicle_model": vehicle_model}}
            }
        },
    }


def _metadrive_observation(text, token_registry_path):
    document = _metadrive_document(text, token_registry_path)
    environment = None
    try:
        environment = _metadrive_environment(document)
        tracks = {
            document["ego"]["actor_id"]: _metadrive_track(
                document["ego"],
                document["ego"]["reference_trajectory"],
                "VEHICLE",
                document["ego"]["vehicle_model"],
            )
        }
        kind_by_type = {
            "vehicle": "VEHICLE",
            "motorcycle": "VEHICLE",
            "e_bike": "CYCLIST",
            "bicycle": "CYCLIST",
            "pedestrian": "PEDESTRIAN",
        }
        for actor in document["actors"]:
            tracks[actor["actor_id"]] = _metadrive_track(
                actor,
                actor["trajectory"],
                kind_by_type[actor["actor_type"]],
            )
        return {
            "platform": "metadrive",
            "length": document["horizon_steps"],
            "tracks": tracks,
            "dynamic_map_states": {},
            "map_features": _json_native(
                environment.current_map.get_map_features()
            ),
            "sdc_id": document["ego"]["actor_id"],
            "actual_block_ids": [block.ID for block in environment.current_map.blocks],
            "semantic_regions": document.get("semantic_regions", []),
        }
    finally:
        if environment is not None:
            environment.close()


def _metadrive_runtime_probe(text, token_registry_path):
    import metadrive

    document = _metadrive_document(text, token_registry_path)
    environment = None
    try:
        environment = _metadrive_environment(document)
        vehicle = environment.agent
        result = {
            "platform": "metadrive",
            "metadrive_module_path": str(
                __import__("pathlib").Path(metadrive.__file__).resolve()
            ),
            "reset_succeeded": True,
            "vehicle_class": type(vehicle).__name__,
            "vehicle_model": vehicle.config["vehicle_model"],
            "physical_length_m": float(vehicle.LENGTH),
            "physical_width_m": float(vehicle.WIDTH),
            "physical_height_m": float(vehicle.HEIGHT),
            "top_down_length_m": float(vehicle.top_down_length),
            "top_down_width_m": float(vehicle.top_down_width),
            "actual_block_ids": [block.ID for block in environment.current_map.blocks],
            "block_sequence": document["map"]["block_sequence"],
        }
        environment.step([0.0, 0.0])
        result["step_succeeded"] = True
        return result
    finally:
        if environment is not None:
            environment.close()


def observe(platform, text, token_registry_path=None):
    if platform == "carla":
        return _carla_observation(text)
    if platform == "metadrive":
        if not token_registry_path:
            raise ValueError("MetaDrive observation requires --token-registry")
        return _metadrive_observation(text, token_registry_path)
    raise ValueError("unsupported platform: {}".format(platform))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("platform", choices=("carla", "metadrive"))
    parser.add_argument("--runtime-probe", action="store_true")
    parser.add_argument("--token-registry")
    args = parser.parse_args(argv)
    text = sys.stdin.read()
    if args.runtime_probe:
        if args.platform != "metadrive":
            parser.error("--runtime-probe is only available for metadrive")
        with contextlib.redirect_stdout(sys.stderr):
            if not args.token_registry:
                parser.error("MetaDrive runtime probe requires --token-registry")
            result = _metadrive_runtime_probe(text, args.token_registry)
    else:
        if args.platform == "carla" and args.token_registry:
            parser.error("CARLA observation cannot receive --token-registry")
        result = observe(args.platform, text, args.token_registry)
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
