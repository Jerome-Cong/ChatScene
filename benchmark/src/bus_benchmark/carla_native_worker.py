"""CARLA native realization replay with the shared fixed ego controller."""

import hashlib
import math
from pathlib import Path
from types import SimpleNamespace

if __package__:
    from . import carla_native
else:
    import carla_native


def load(path, request):
    import carla

    d = carla_native.parse(Path(path).read_bytes())
    staged = next(
        b
        for b in request["carla_map_context"]["staged_catalog"]
        if b["path"].endswith(".xodr")
    )
    if (
        staged["sha256"] != d["map"]["sha256"]
        or Path(staged["path"]).read_text() != d["map"]["opendrive"]
    ):
        raise ValueError("native map differs from captured OpenDRIVE")
    return d, carla.Map(d["map"]["name"], d["map"]["opendrive"]), staged


def compile(path, request):
    d, road, binding = load(path, request)
    return {
        "ok": True,
        "mode": "compile",
        "native_type": "CarlaNativeRealization",
        "object_spec_count": len(d["actors"]),
        "static_opendrive": dict(binding),
    }


def sv(path, request, boxes):
    import carla

    d, road, staged = load(path, request)
    client = carla.Client(
        request["carla_endpoint"]["host"], request["carla_endpoint"]["port"]
    )
    client.set_timeout(request["carla_endpoint"]["timeout_seconds"])
    blueprints = client.get_world().get_blueprint_library()
    initial = []
    from scenic.domains.driving.roads import Network

    network = Network.fromFile(staged["path"], useCache=False, writeCache=False)
    regions = [
        getattr(network, k, None)
        for k in (
            "drivableRegion",
            "walkableRegion",
            "roadRegion",
            "intersectionRegion",
            "crossingRegion",
            "sidewalkRegion",
            "shoulderRegion",
        )
    ]
    regions = [r for r in regions if r is not None]
    for a in d["actors"]:
        try:
            blueprints.find(a["blueprint"])
        except RuntimeError as exc:
            raise ValueError("unresolvable blueprint: " + a["blueprint"]) from exc
        p = a["initial_transform"]["location_m"]
        h = math.radians(a["initial_transform"]["rotation_deg"][1])
        if (
            road.get_waypoint(
                carla.Location(*p), project_to_road=False, lane_type=carla.LaneType.Any
            )
            is None
        ):
            raise ValueError("initial actor is outside the native map")
        corners = boxes[0](p[:2], h, a["length_m"], a["width_m"])
        if any(
            not any(r.containsPoint((c[0], -c[1])) for r in regions) for c in corners
        ):
            raise ValueError("actor footprint is outside the native map")
        if any(boxes[1](corners, prior) for prior in initial):
            raise ValueError("initial physical overlap")
        initial.append(corners)
    canonical = Path(path).read_bytes()
    return {
        "ok": True,
        "mode": "sv",
        "seed": int(request["seed"]),
        "iterations": 1,
        "object_count": len(d["actors"]),
        "realization_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def ne(path, request, controls, map_evidence, write_trace):
    import carla

    d, _, _ = load(path, request)
    endpoint = request["carla_endpoint"]
    client = carla.Client(endpoint["host"], endpoint["port"])
    client.set_timeout(endpoint["timeout_seconds"])
    world = client.load_world(d["map"]["name"])
    previous = world.get_settings()
    actors = []
    try:
        if client.get_server_version() != d["carla_version"]:
            raise ValueError("native replay server version differs from capture")
        weather = world.get_weather()
        for k, v in d["weather"].items():
            if not hasattr(weather, k):
                raise ValueError("native weather attribute is unavailable")
            setattr(weather, k, v)
        world.set_weather(weather)
        settings = world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = d["time_step_s"]
        world.apply_settings(settings)
        library = world.get_blueprint_library()
        for a in d["actors"]:
            t = a["initial_transform"]
            p = t["location_m"]
            r = t["rotation_deg"]
            actor = world.try_spawn_actor(
                library.find(a["blueprint"]),
                carla.Transform(
                    carla.Location(*p), carla.Rotation(pitch=r[0], yaw=r[1], roll=r[2])
                ),
            )
            if actor is None:
                raise ValueError("native actor spawn rejected")
            actors.append(actor)
            v = d["frames"][0]["states"][len(actors) - 1]["velocity_m_s"]
            actor.set_target_velocity(carla.Vector3D(*v))
        native = map_evidence(SimpleNamespace(client=client, world=world), request)
        ego_controls = controls(actors[0], request["controller"]["parameters"])
        rows = []

        def row(step, action):
            values = []
            for i, (actor, a) in enumerate(zip(actors, d["actors"])):
                if actor is None or not actor.is_alive:
                    continue
                p = actor.get_location()
                v = actor.get_velocity()
                values.append(
                    {
                        "object_index": i,
                        "is_ego": i == 0,
                        "kind": a["kind"],
                        "position": [p.x, -p.y],
                        "speed_m_s": math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z),
                    }
                )
            return {
                "step": step,
                "simulated_time_seconds": step * d["time_step_s"],
                "objects": values,
                "ego_action": action,
            }

        budget = int(round(request["rollout_seconds"] / d["time_step_s"]))
        reason = (
            "fixed budget completed; source termination: " + d["termination"]["kind"]
        )
        steps = 0
        for step in range(budget):
            next_frame = d["frames"][min(step + 1, len(d["frames"]) - 1)]
            action = next(ego_controls)
            rows.append(row(step, action))
            actors[0].apply_control(
                carla.VehicleControl(
                    throttle=action["throttle"],
                    brake=action["brake"],
                    steer=action["steering"],
                )
            )
            for i, state in enumerate(next_frame["states"][1:], 1):
                actor = actors[i]
                if actor is None:
                    continue
                if not state["valid"]:
                    actor.destroy()
                    actors[i] = None
                    continue
                c = state["control"]
                if c["type"] == "vehicle":
                    actor.apply_control(
                        carla.VehicleControl(
                            **{k: v for k, v in c.items() if k != "type"}
                        )
                    )
                else:
                    actor.apply_control(
                        carla.WalkerControl(
                            direction=carla.Vector3D(*c["direction"]),
                            speed=c["speed"],
                            jump=c["jump"],
                        )
                    )
            world.tick()
            steps += 1
        rows.append(row(steps, None))
        write_trace(request["trace_path"], rows)
        return {
            "ok": True,
            "mode": "ne",
            "steps": steps,
            "simulated_seconds": steps * d["time_step_s"],
            "termination_reason": reason,
            "runtime_controller_class": "FixedIDMPIDBehavior",
            "native_runtime": native,
        }
    finally:
        for a in actors:
            if a is not None and a.is_alive:
                a.destroy()
        world.apply_settings(previous)
