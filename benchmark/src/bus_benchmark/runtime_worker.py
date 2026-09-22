"""Platform-native worker for the benchmark runtime stages.

The worker is deliberately oracle-free and query-free.  It consumes one JSON request
from stdin and emits one JSON result to stdout.  CARLA/Scenic and MetaDrive imports are
lazy so the same source can be executed by their separate Python environments.
"""

import hashlib
import json
import math
import os
import random
import sys
import traceback
from pathlib import Path


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object key")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("non-JSON numeric constant {}".format(value))


def _strict_json_object_bytes(payload):
    value = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("runtime worker request must be a JSON object")
    return value


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_file_binding(binding, label):
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256", "bytes"}:
        raise ValueError("{} binding is malformed".format(label))
    path = Path(str(binding.get("path", ""))).resolve()
    if (
        not path.is_file()
        or binding.get("sha256") != _sha256_file(path)
        or binding.get("bytes") != path.stat().st_size
    ):
        raise ValueError("{} binding changed".format(label))
    return path


def _verify_carla_server_request(request):
    binding = request.get("carla_server_binary")
    server_path = _verify_file_binding(binding, "CARLA server binary")
    try:
        with server_path.open("rb") as stream:
            magic = stream.read(4)
    except OSError as exc:
        raise ValueError("CARLA server binary cannot be read") from exc
    if (
        server_path.name != "CarlaUE4-Linux-Shipping"
        or not os.access(str(server_path), os.X_OK)
        or magic != b"\x7fELF"
    ):
        raise ValueError("CARLA server binding is not the executable shipping ELF")
    if request.get("mode") != "ne":
        return
    preflight = request.get("carla_server_preflight")
    if (
        not isinstance(preflight, dict)
        or set(preflight) != {"status", "failure_reason", "attestation"}
        or preflight.get("status") != "verified"
        or preflight.get("failure_reason") is not None
        or not isinstance(preflight.get("attestation"), dict)
    ):
        raise ValueError("CARLA NE lacks a verified local server preflight")
    attestation = preflight["attestation"]
    if set(attestation) != {
        "pid",
        "process_starttime_ticks",
        "listener_table",
        "listener_local_address_hex",
        "listener_port",
        "socket_inode",
        "process_exe",
    }:
        raise ValueError("CARLA server preflight attestation is malformed")
    endpoint = request["carla_endpoint"]
    if (
        type(attestation.get("pid")) is not int
        or attestation["pid"] < 1
        or type(attestation.get("process_starttime_ticks")) is not int
        or attestation["process_starttime_ticks"] < 1
        or attestation.get("listener_table") not in {"tcp", "tcp6"}
        or not isinstance(attestation.get("listener_local_address_hex"), str)
        or attestation.get("listener_port") != endpoint["port"]
        or not isinstance(attestation.get("socket_inode"), str)
        or not attestation["socket_inode"].isdigit()
        or attestation.get("process_exe") != binding
    ):
        raise ValueError("CARLA server preflight differs from the frozen endpoint/binary")


def _verify_metadrive_runtime_context(request):
    context = request.get("metadrive_runtime_context")
    if not isinstance(context, dict) or set(context) != {
        "metadrive_module",
        "source_tree",
    }:
        raise ValueError("MetaDrive runtime context is missing or malformed")
    module_binding = context["metadrive_module"]
    bound_module = _verify_file_binding(module_binding, "MetaDrive imported module")
    source = context["source_tree"]
    if not isinstance(source, dict) or set(source) != {
        "repository_path",
        "revision",
        "tracked_subpath",
        "tracked_tree_sha256",
        "tracked_file_count",
        "tracked_bytes",
        "policy",
    }:
        raise ValueError("MetaDrive tracked source context is malformed")
    repository = Path(str(source.get("repository_path", ""))).resolve()
    if (
        not repository.is_dir()
        or source.get("revision")
        != "85e5dadc6c7436d324348f6e3d8f8e680c06b4db"
        or source.get("tracked_subpath") != "metadrive"
        or not isinstance(source.get("tracked_tree_sha256"), str)
        or len(source["tracked_tree_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in source["tracked_tree_sha256"]
        )
        or type(source.get("tracked_file_count")) is not int
        or source["tracked_file_count"] < 1
        or type(source.get("tracked_bytes")) is not int
        or source["tracked_bytes"] < 1
        or source.get("policy") != "git_tracked_worktree_bytes_v0.1"
    ):
        raise ValueError("MetaDrive tracked source context is not audited")
    try:
        expected_module = (
            repository / source["tracked_subpath"] / "__init__.py"
        ).resolve()
        expected_module.relative_to(repository / source["tracked_subpath"])
    except ValueError as exc:
        raise ValueError("MetaDrive module binding escapes the source tree") from exc
    if bound_module != expected_module:
        raise ValueError("MetaDrive module binding is outside the source tree")
    import metadrive

    imported_module = Path(metadrive.__file__).resolve()
    if (
        imported_module != bound_module
        or _sha256_file(imported_module) != module_binding["sha256"]
        or imported_module.stat().st_size != module_binding["bytes"]
    ):
        raise ValueError("executing worker imported an unbound MetaDrive package")


def _verify_request(request):
    if not isinstance(request, dict):
        raise ValueError("runtime worker request must be an object")
    if request.get("platform") not in ("carla", "metadrive"):
        raise ValueError("unknown runtime platform")
    if request.get("mode") not in ("compile", "sv", "ne"):
        raise ValueError("unknown runtime mode")
    for field in (
        "source_response_sha256",
        "source_config_sha256",
        "source_generation_manifest_sha256",
        "source_implementation_bundle_sha256",
    ):
        value = request.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("runtime request lacks a valid {}".format(field))
    if type(request.get("generation_chain_verified")) is not bool:
        raise ValueError("runtime request lacks the generation-chain verification state")
    path = Path(str(request.get("artifact_path", ""))).resolve()
    if not path.is_file() or request.get("artifact_sha256") != _sha256_file(path):
        raise ValueError("runtime artifact is missing or changed")
    executor = request.get("runtime_executor")
    worker_path = Path(__file__).resolve()
    interpreter_path = Path(sys.executable).resolve()
    if (
        not isinstance(executor, dict)
        or executor.get("worker_path") != str(worker_path)
        or executor.get("worker_sha256") != _sha256_file(worker_path)
        or executor.get("worker_bytes") != worker_path.stat().st_size
        or executor.get("interpreter_path") != str(interpreter_path)
        or executor.get("interpreter_sha256") != _sha256_file(interpreter_path)
        or executor.get("interpreter_bytes") != interpreter_path.stat().st_size
    ):
        raise ValueError("runtime request is not bound to the executing worker")
    runtime_environment = request.get("runtime_environment")
    if (
        not isinstance(runtime_environment, dict)
        or set(runtime_environment) != {"values", "bindings"}
        or not isinstance(runtime_environment["values"], dict)
        or not isinstance(runtime_environment["bindings"], list)
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or os.environ.get(key) != value
            for key, value in runtime_environment["values"].items()
        )
    ):
        raise ValueError("runtime worker environment differs from its request binding")
    bound_paths = []
    for binding in runtime_environment["bindings"]:
        if not isinstance(binding, dict) or set(binding) != {"path", "sha256", "bytes"}:
            raise ValueError("runtime environment file binding is malformed")
        bound_path = Path(str(binding.get("path", ""))).resolve()
        if (
            not bound_path.is_file()
            or binding.get("sha256") != _sha256_file(bound_path)
            or binding.get("bytes") != bound_path.stat().st_size
        ):
            raise ValueError("runtime environment file binding changed")
        bound_paths.append(str(bound_path))
    environment_paths = []
    for key in ("CARLA_ROOT", "LD_LIBRARY_PATH", "PYTHONPATH"):
        for value in runtime_environment["values"].get(key, "").split(os.pathsep):
            if value:
                environment_paths.append(str(Path(value).resolve()))
    if sorted(bound_paths) != sorted(environment_paths):
        raise ValueError("runtime content-bearing environment is not bound exactly")
    if request.get("platform") == "carla":
        endpoint = request.get("carla_endpoint")
        if (
            not isinstance(endpoint, dict)
            or set(endpoint) != {"host", "port", "timeout_seconds"}
            or endpoint.get("host") != "127.0.0.1"
            or type(endpoint.get("port")) is not int
            or type(endpoint.get("timeout_seconds")) not in (int, float)
        ):
            raise ValueError("CARLA runtime request lacks the frozen loopback endpoint")
        _verify_carla_server_request(request)
        map_context = request.get("carla_map_context")
        if (
            not isinstance(map_context, dict)
            or set(map_context) != {
                "declared_town",
                "source_artifact",
                "staged_artifact",
                "source_catalog",
                "staged_catalog",
            }
            or not isinstance(map_context["declared_town"], str)
            or not isinstance(map_context["source_catalog"], list)
            or not isinstance(map_context["staged_catalog"], list)
            or len(map_context["source_catalog"]) != len(map_context["staged_catalog"])
            or not map_context["source_catalog"]
        ):
            raise ValueError("CARLA runtime request lacks an exact map context")
        source_artifact = _verify_file_binding(
            map_context["source_artifact"], "CARLA source artifact"
        )
        staged_artifact = _verify_file_binding(
            map_context["staged_artifact"], "CARLA staged artifact"
        )
        if (
            staged_artifact != path
            or map_context["source_artifact"]["sha256"]
            != map_context["staged_artifact"]["sha256"]
            or map_context["source_artifact"]["bytes"]
            != map_context["staged_artifact"]["bytes"]
            or os.path.samefile(str(source_artifact), str(staged_artifact))
        ):
            raise ValueError("CARLA staged artifact is not an independent byte copy")
        source_by_name = {}
        for binding in map_context["source_catalog"]:
            if not isinstance(binding, dict) or set(binding) != {"path", "sha256", "bytes"}:
                raise ValueError("CARLA source map binding is malformed")
            source = Path(str(binding.get("path", ""))).resolve()
            if (
                not source.is_file()
                or binding.get("sha256") != _sha256_file(source)
                or binding.get("bytes") != source.stat().st_size
                or source.name in source_by_name
            ):
                raise ValueError("CARLA source map binding changed or is duplicated")
            source_by_name[source.name] = binding
        expected_map_directory = path.parent.parent / "maps"
        staged_names = set()
        for binding in map_context["staged_catalog"]:
            if not isinstance(binding, dict) or set(binding) != {"path", "sha256", "bytes"}:
                raise ValueError("CARLA staged map binding is malformed")
            staged = Path(str(binding.get("path", ""))).resolve()
            source = source_by_name.get(staged.name)
            if (
                staged.parent != expected_map_directory.resolve()
                or not staged.is_file()
                or source is None
                or staged.name in staged_names
                or binding.get("sha256") != source.get("sha256")
                or binding.get("bytes") != source.get("bytes")
                or _sha256_file(staged) != source.get("sha256")
                or os.path.samefile(str(Path(source["path"])), str(staged))
            ):
                raise ValueError("CARLA staged map differs from its source catalog")
            staged_names.add(staged.name)
        if staged_names != set(source_by_name):
            raise ValueError("CARLA staged map catalog is incomplete")
        town = map_context["declared_town"]
        if (
            set(staged_names) - {"{}.xodr".format(town), "{}.snet".format(town)}
            or "{}.xodr".format(town) not in staged_names
        ):
            raise ValueError("CARLA map context differs from its declared Town")
    else:
        if runtime_environment != {"values": {}, "bindings": []}:
            raise ValueError("MetaDrive runtime environment must remain empty")
        registry = request.get("metadrive_token_registry")
        if not isinstance(registry, dict) or set(registry) != {
            "path",
            "sha256",
            "bytes",
        }:
            raise ValueError("MetaDrive runtime request lacks the frozen token registry")
        registry_path = Path(str(registry.get("path", ""))).resolve()
        if (
            registry_path.is_symlink()
            or not registry_path.is_file()
            or registry.get("sha256") != _sha256_file(registry_path)
            or registry.get("bytes") != registry_path.stat().st_size
        ):
            raise ValueError("MetaDrive token registry is missing or changed")
        _verify_metadrive_runtime_context(request)
    return path


def _finite_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _verify_carla_endpoint(scenario, request):
    endpoint = request["carla_endpoint"]
    params = scenario.params
    try:
        actual = {
            "host": str(params["address"]),
            "port": int(params["port"]),
            "timeout_seconds": float(params["timeout"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("CARLA scenario lacks a valid native endpoint") from exc
    expected = {
        "host": endpoint["host"],
        "port": endpoint["port"],
        "timeout_seconds": float(endpoint["timeout_seconds"]),
    }
    if actual != expected:
        raise ValueError("CARLA scenario endpoint differs from the frozen platform endpoint")


def _verify_carla_map(scenario, request):
    try:
        map_path = Path(str(scenario.params["map"])).resolve()
        carla_map = str(scenario.params["carla_map"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("CARLA scenario lacks a valid native map binding") from exc
    staged_by_path = {
        str(Path(binding["path"]).resolve()): binding
        for binding in request["carla_map_context"]["staged_catalog"]
    }
    binding = staged_by_path.get(str(map_path))
    if (
        binding is None
        or map_path.suffix != ".xodr"
        or map_path.stem != carla_map
        or not map_path.is_file()
        or binding["sha256"] != _sha256_file(map_path)
        or binding["bytes"] != map_path.stat().st_size
    ):
        raise ValueError("CARLA scenario map is outside or differs from the bound catalog")
    return dict(binding)


def _carla_compile(path, request):
    from scenic.syntax.translator import scenarioFromFile

    scenario = scenarioFromFile(str(path))
    _verify_carla_endpoint(scenario, request)
    static_opendrive = _verify_carla_map(scenario, request)
    return scenario, {
        "ok": True,
        "mode": "compile",
        "native_type": type(scenario).__name__,
        "object_spec_count": len(scenario.objects),
        "static_opendrive": static_opendrive,
    }


def _carla_runtime_map_evidence(simulator, request):
    client = simulator.client
    world_map = simulator.world.get_map()
    client_version = str(client.get_client_version())
    server_version = str(client.get_server_version())
    world_map_name = str(world_map.name)
    runtime_opendrive = world_map.to_opendrive().encode("utf-8")
    context = request["carla_map_context"]
    town = context["declared_town"]
    staged_xodr = next(
        (
            binding
            for binding in context["staged_catalog"]
            if Path(binding["path"]).name == "{}.xodr".format(town)
        ),
        None,
    )
    runtime_binding = {
        "sha256": _sha256_bytes(runtime_opendrive),
        "bytes": len(runtime_opendrive),
    }
    if not client_version or not server_version or client_version != server_version:
        raise ValueError("CARLA client/server versions are empty or mismatched")
    if Path(world_map_name).name != town:
        raise ValueError("CARLA runtime world map differs from the declared Town")
    if staged_xodr is None or runtime_binding != {
        "sha256": staged_xodr["sha256"],
        "bytes": staged_xodr["bytes"],
    }:
        raise ValueError("CARLA runtime OpenDRIVE differs from the staged static OpenDRIVE")
    return {
        "client_version": client_version,
        "server_version": server_version,
        "world_map_name": world_map_name,
        "declared_town": town,
        "runtime_opendrive": runtime_binding,
        "staged_opendrive": dict(staged_xodr),
    }


def _carla_realization(path, seed, max_iterations, request):
    random.seed(seed)
    scenario, _ = _carla_compile(path, request)
    scene, iterations = scenario.generate(maxIterations=max_iterations)
    if scene.egoObject is None or scene.objects.count(scene.egoObject) != 1:
        raise ValueError("CARLA realization requires exactly one ego object")
    objects = []
    for index, actor in enumerate(scene.objects):
        position = actor.position
        row = {
            "object_index": index,
            "kind": type(actor).__name__,
            "is_ego": actor is scene.egoObject,
            "position": [float(position[0]), float(position[1])],
            "heading_rad": float(actor.heading),
            "length_m": float(actor.length),
            "width_m": float(actor.width),
            "blueprint": actor.blueprint if isinstance(actor.blueprint, str) else "",
        }
        if not all(
            _finite_number(value)
            for value in row["position"]
            + [row["heading_rad"], row["length_m"], row["width_m"]]
        ):
            raise ValueError("CARLA realization contains non-finite geometry")
        if row["length_m"] <= 0 or row["width_m"] <= 0:
            raise ValueError("CARLA realization contains non-positive dimensions")
        objects.append(row)
    import scenic.simulators.carla.blueprints as blueprint_catalog

    known_blueprints = set()
    for name in dir(blueprint_catalog):
        value = getattr(blueprint_catalog, name)
        if name.endswith("Models") and isinstance(value, (list, tuple)):
            known_blueprints.update(value)
    legacy_names = getattr(blueprint_catalog, "oldBlueprintNames", {})
    if isinstance(legacy_names, dict):
        known_blueprints.update(legacy_names)
        for aliases in legacy_names.values():
            known_blueprints.update(aliases)
    if any(not row["blueprint"] or row["blueprint"] not in known_blueprints for row in objects):
        raise ValueError("CARLA realization contains an unresolvable blueprint")

    network = getattr(scene.workspace, "network", None)
    if network is None:
        raise ValueError("CARLA realization lacks a native driving network")
    regions = [
        getattr(network, name, None)
        for name in (
            "drivableRegion",
            "walkableRegion",
            "roadRegion",
            "intersectionRegion",
            "crossingRegion",
            "sidewalkRegion",
            "shoulderRegion",
        )
    ]
    regions = [region for region in regions if region is not None]
    if not regions:
        raise ValueError("CARLA realization has no map-bounded native regions")
    for actor in scene.objects:
        if any(not any(region.containsPoint(corner) for region in regions) for corner in actor.corners):
            raise ValueError("CARLA realization places an object outside the native map")
    for index, actor in enumerate(scene.objects):
        for other in scene.objects[:index]:
            if actor.intersects(other):
                raise ValueError("CARLA realization contains initial physical overlap")
    ego = next(row for row in objects if row["is_ego"])
    if ego["blueprint"] != "vehicle.chevrolet.impala":
        raise ValueError("CARLA ego does not use the approved Impala proxy")
    if abs(ego["length_m"] - 5.33) > 1e-9 or abs(ego["width_m"] - 2.10) > 1e-9:
        raise ValueError("CARLA ego does not use the approved 5.33 x 2.10 footprint")
    realization = {"seed": seed, "iterations": iterations, "objects": objects}
    return scenario, scene, realization


def _carla_sv(path, seed, max_iterations, request):
    _, _, realization = _carla_realization(path, seed, max_iterations, request)
    return {
        "ok": True,
        "mode": "sv",
        "seed": seed,
        "iterations": realization["iterations"],
        "object_count": len(realization["objects"]),
        "realization_sha256": _sha256_bytes(_canonical_bytes(realization)),
    }


def _carla_controller_classes(parameters):
    import carla
    from scenic.core.dynamics import Behavior
    from scenic.domains.driving.actions import SetBrakeAction, SetSteerAction, SetThrottleAction

    class FixedIDMPIDBehavior(Behavior):
        """Query-blind lane follower with IDM longitudinal control and PID steering."""

        def makeGenerator(self, agent):
            integral = 0.0
            previous_error = 0.0
            previous_steer = 0.0
            previous_acceleration = 0.0
            dt = float(parameters["dt_seconds"])
            while True:
                actor = agent.carlaActor
                transform = actor.get_transform()
                location = transform.location
                velocity = actor.get_velocity()
                speed = math.sqrt(velocity.x ** 2 + velocity.y ** 2 + velocity.z ** 2)
                world = actor.get_world()
                road_map = world.get_map()
                waypoint = road_map.get_waypoint(
                    location,
                    project_to_road=True,
                    lane_type=carla.LaneType.Driving,
                )
                if waypoint is None:
                    yield (SetThrottleAction(0.0), SetBrakeAction(1.0), SetSteerAction(0.0))
                    continue
                candidates = waypoint.next(max(2.0, speed * parameters["lookahead_seconds"]))
                target = candidates[0].transform.location if candidates else waypoint.transform.location
                forward = transform.get_forward_vector()
                target_x = target.x - location.x
                target_y = target.y - location.y
                target_norm = max(math.hypot(target_x, target_y), 1e-9)
                heading_error = math.atan2(
                    forward.x * target_y - forward.y * target_x,
                    forward.x * target_x + forward.y * target_y,
                )
                integral += heading_error * dt
                derivative = (heading_error - previous_error) / dt
                raw_steer = (
                    parameters["lateral_kp"] * heading_error
                    + parameters["lateral_ki"] * integral
                    + parameters["lateral_kd"] * derivative
                )
                steer = max(-1.0, min(1.0, raw_steer))
                max_steer_step = (
                    parameters["max_front_wheel_rate_rad_s"]
                    / parameters["max_front_wheel_angle_rad"]
                    * dt
                )
                steer = max(
                    previous_steer - max_steer_step,
                    min(previous_steer + max_steer_step, steer),
                )
                previous_error = heading_error
                previous_steer = steer

                nearest = None
                nearest_distance = float("inf")
                right = transform.get_right_vector()
                for other in world.get_actors().filter("vehicle.*"):
                    if other.id == actor.id:
                        continue
                    delta = other.get_location() - location
                    longitudinal = delta.x * forward.x + delta.y * forward.y
                    lateral = abs(delta.x * right.x + delta.y * right.y)
                    if 0 < longitudinal < nearest_distance and lateral <= parameters["lane_half_width_m"]:
                        nearest = other
                        nearest_distance = longitudinal

                target_speed = parameters["target_speed_m_s"]
                acceleration = parameters["max_acceleration_m_s2"] * (
                    1.0 - (max(speed, 0.0) / max(target_speed, 1e-6)) ** parameters["idm_delta"]
                )
                if nearest is not None:
                    other_velocity = nearest.get_velocity()
                    other_speed = other_velocity.x * forward.x + other_velocity.y * forward.y
                    closing_speed = speed - other_speed
                    desired_gap = parameters["minimum_gap_m"] + max(
                        0.0,
                        speed * parameters["time_headway_seconds"]
                        + speed
                        * closing_speed
                        / (
                            2.0
                            * math.sqrt(
                                parameters["max_acceleration_m_s2"]
                                * parameters["comfortable_deceleration_m_s2"]
                            )
                        ),
                    )
                    acceleration -= parameters["max_acceleration_m_s2"] * (
                        desired_gap / max(nearest_distance, 0.1)
                    ) ** 2
                acceleration = max(
                    -parameters["max_deceleration_m_s2"],
                    min(parameters["max_acceleration_m_s2"], acceleration),
                )
                jerk_step = parameters["max_longitudinal_jerk_m_s3"] * dt
                acceleration = max(
                    previous_acceleration - jerk_step,
                    min(previous_acceleration + jerk_step, acceleration),
                )
                previous_acceleration = acceleration
                throttle = max(acceleration, 0.0) / parameters["max_acceleration_m_s2"]
                brake = max(-acceleration, 0.0) / parameters["max_deceleration_m_s2"]
                yield (
                    SetThrottleAction(float(max(0.0, min(1.0, throttle)))),
                    SetBrakeAction(float(max(0.0, min(1.0, brake)))),
                    SetSteerAction(float(steer)),
                )

    return FixedIDMPIDBehavior


def _write_trace(path, rows):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(_canonical_bytes(row).decode("utf-8"))
            stream.write("\n")


def _carla_ne(path, request):
    seed = int(request["seed"])
    scenario, scene, _ = _carla_realization(path, seed, 2000, request)
    controller_document = request["controller"]
    behavior_class = _carla_controller_classes(controller_document["parameters"])
    scene.egoObject.behavior = behavior_class()
    scene.params["address"] = request["carla_endpoint"]["host"]
    scene.params["port"] = request["carla_endpoint"]["port"]
    scene.params["timeout"] = request["carla_endpoint"]["timeout_seconds"]
    scene.params["render"] = 0
    scene.params["timestep"] = float(controller_document["parameters"]["dt_seconds"])
    simulator = scenario.getSimulator()
    try:
        native_runtime = _carla_runtime_map_evidence(simulator, request)
        steps_budget = int(
            round(float(request["rollout_seconds"]) / float(scene.params["timestep"]))
        )
        simulation = simulator.simulate(
            scene,
            maxSteps=steps_budget,
            maxIterations=1,
            raiseGuardViolations=False,
        )
        if simulation is None:
            raise RuntimeError("CARLA simulation was rejected")
        rows = []
        previous = None
        for step, state in enumerate(simulation.trajectory):
            objects = []
            for index, position in enumerate(state):
                xy = [float(position[0]), float(position[1])]
                speed = 0.0
                if previous is not None:
                    before = previous[index]
                    speed = math.hypot(xy[0] - before[0], xy[1] - before[1]) / float(
                        scene.params["timestep"]
                    )
                objects.append(
                    {
                        "object_index": index,
                        "is_ego": index == 0,
                        "kind": type(scene.objects[index]).__name__,
                        "position": xy,
                        "speed_m_s": speed,
                    }
                )
            ego_action = None
            if step < len(simulation.result.actions):
                actions = simulation.result.actions[step].get(scene.egoObject, ())
                control = {"throttle": 0.0, "brake": 0.0, "steering": 0.0}
                observed = False
                for action in actions:
                    if hasattr(action, "throttle"):
                        control["throttle"] = float(action.throttle)
                        observed = True
                    if hasattr(action, "brake"):
                        control["brake"] = float(action.brake)
                        observed = True
                    if hasattr(action, "steer"):
                        control["steering"] = float(action.steer)
                        observed = True
                ego_action = control if observed else None
            rows.append(
                {
                    "step": step,
                    "simulated_time_seconds": step * float(scene.params["timestep"]),
                    "objects": objects,
                    "ego_action": ego_action,
                }
            )
            previous = [entry["position"] for entry in objects]
        _write_trace(request["trace_path"], rows)
        simulated_seconds = min(
            float(request["rollout_seconds"]),
            max(0, len(rows) - 1) * float(scene.params["timestep"]),
        )
        return {
            "ok": True,
            "mode": "ne",
            "steps": max(0, len(rows) - 1),
            "simulated_seconds": simulated_seconds,
            "termination_reason": str(simulation.result.terminationReason),
            "runtime_controller_class": behavior_class.__name__,
            "native_runtime": native_runtime,
        }
    finally:
        simulator.destroy()


def _metadrive_native(path, request):
    import metadrive_pg

    registry_binding = request["metadrive_token_registry"]
    registry, registry_sha256 = metadrive_pg.load_token_registry(
        Path(registry_binding["path"]),
        expected_sha256=registry_binding["sha256"],
        formal=False,
        verify_files=True,
    )
    document = metadrive_pg.parse_artifact(
        path.read_bytes(), registry, registry_sha256
    )
    return document, registry


def _metadrive_compile(path, request):
    from metadrive.component.algorithm.blocks_prob_dist import PGBlockDistConfig

    import metadrive_pg

    document, registry = _metadrive_native(path, request)
    token_classes = metadrive_pg.validate_token_registry(
        registry, formal=False, verify_files=True
    )
    for token, expected_class_name in token_classes.items():
        actual_class = PGBlockDistConfig.get_block(token)
        if actual_class.__name__ != expected_class_name:
            raise ValueError(
                "MetaDrive token registry differs from the live PG block registry"
            )
    return document, {
        "ok": True,
        "mode": "compile",
        "native_type": "MetaDriveEnv+PGMap:block_sequence",
        "block_sequence": document["map"]["block_sequence"],
        "expected_block_ids": ["I"] + list(document["map"]["block_sequence"]),
        "actor_count": 1 + len(document["actors"]),
        "semantic_region_count": len(document.get("semantic_regions", [])),
        "token_registry_id": registry["registry_id"],
    }


def _oriented_box_corners(position, heading, length, width):
    cosine = math.cos(float(heading))
    sine = math.sin(float(heading))
    forward = (cosine, sine)
    left = (-sine, cosine)
    half_length = float(length) / 2.0
    half_width = float(width) / 2.0
    return [
        (
            float(position[0]) + sign_length * half_length * forward[0]
            + sign_width * half_width * left[0],
            float(position[1]) + sign_length * half_length * forward[1]
            + sign_width * half_width * left[1],
        )
        for sign_length, sign_width in ((1, 1), (1, -1), (-1, -1), (-1, 1))
    ]


def _boxes_overlap(first, second):
    for corners in (first, second):
        for index in range(2):
            edge = (
                corners[index + 1][0] - corners[index][0],
                corners[index + 1][1] - corners[index][1],
            )
            axis = (-edge[1], edge[0])
            first_projection = [point[0] * axis[0] + point[1] * axis[1] for point in first]
            second_projection = [point[0] * axis[0] + point[1] * axis[1] for point in second]
            if max(first_projection) <= min(second_projection) or max(second_projection) <= min(first_projection):
                return False
    return True


def _point_segment_distance(point, start, end):
    dx = float(end[0]) - float(start[0])
    dy = float(end[1]) - float(start[1])
    denominator = dx * dx + dy * dy
    if denominator <= 1e-18:
        return math.hypot(float(point[0]) - float(start[0]), float(point[1]) - float(start[1]))
    projection = (
        (float(point[0]) - float(start[0])) * dx
        + (float(point[1]) - float(start[1])) * dy
    ) / denominator
    projection = max(0.0, min(1.0, projection))
    closest = (float(start[0]) + projection * dx, float(start[1]) + projection * dy)
    return math.hypot(float(point[0]) - closest[0], float(point[1]) - closest[1])


def _point_in_polygon(point, polygon):
    inside = False
    x, y = float(point[0]), float(point[1])
    for index, current in enumerate(polygon):
        previous = polygon[index - 1]
        x1, y1 = float(previous[0]), float(previous[1])
        x2, y2 = float(current[0]), float(current[1])
        if _point_segment_distance((x, y), (x1, y1), (x2, y2)) <= 1e-9:
            return True
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
    return inside


def _point_near_native_map(point, features, tolerance=4.5):
    for feature in features:
        polygon = feature.get("polygon", [])
        if len(polygon) >= 3 and _point_in_polygon(point, polygon):
            return True
        for geometry_name, closed in (("polyline", False), ("polygon", True)):
            geometry = feature.get(geometry_name, [])
            if len(geometry) < 2:
                continue
            segment_count = len(geometry) if closed else len(geometry) - 1
            for index in range(segment_count):
                if (
                    _point_segment_distance(
                        point, geometry[index], geometry[(index + 1) % len(geometry)]
                    )
                    <= tolerance
                ):
                    return True
    return False


def _polygon_area(polygon):
    return abs(
        sum(
            float(point[0]) * float(polygon[(index + 1) % len(polygon)][1])
            - float(polygon[(index + 1) % len(polygon)][0]) * float(point[1])
            for index, point in enumerate(polygon)
        )
    ) / 2.0


def _metadrive_static_sanity(document, environment):
    map_features = list(environment.current_map.get_map_features().values())
    map_points = []
    for feature in map_features:
        for geometry_name in ("polyline", "polygon"):
            for point in feature.get(geometry_name, []):
                if len(point) >= 2 and _finite_number(point[0]) and _finite_number(point[1]):
                    map_points.append((float(point[0]), float(point[1])))
    if not map_points:
        raise ValueError("MetaDrive realization lacks finite native map geometry")
    if not map_points:
        raise ValueError("MetaDrive realization lacks finite PGMap geometry")

    expected_blocks = ["I"] + list(document["map"]["block_sequence"])
    actual_blocks = [block.ID for block in environment.current_map.blocks]
    if actual_blocks != expected_blocks:
        raise ValueError(
            "MetaDrive generated block IDs differ from the submitted token sequence"
        )

    actor_specs = [
        {
            "actor_id": "ego",
            "actor_type": "vehicle",
            "length_m": document["ego"]["length_m"],
            "width_m": document["ego"]["width_m"],
            "trajectory": document["ego"]["reference_trajectory"],
        }
    ] + list(document["actors"])
    initial_boxes = []
    checked_actor_count = 0
    for actor in actor_specs:
        valid_states = [state for state in actor["trajectory"] if state["valid"]]
        if not valid_states:
            raise ValueError("MetaDrive actor lacks a valid trajectory state")
        state = valid_states[0]
        position = state["position_m"]
        heading = state["heading_rad"]
        length = actor["length_m"]
        width = actor["width_m"]
        corners = _oriented_box_corners(position, heading, length, width)
        geometry_points = [(float(position[0]), float(position[1]))]
        if actor["actor_type"] in {"vehicle", "motorcycle"}:
            geometry_points += corners
        if not all(_point_near_native_map(point, map_features) for point in geometry_points):
            raise ValueError(
                "MetaDrive realization places an actor outside generated PGMap geometry"
            )
        checked_actor_count += 1
        if actor["trajectory"][0]["valid"]:
            initial_boxes.append((actor["actor_id"], corners))
    for index, (actor_id, corners) in enumerate(initial_boxes):
        for other_id, other_corners in initial_boxes[:index]:
            if _boxes_overlap(corners, other_corners):
                raise ValueError(
                    "MetaDrive realization contains initial physical overlap: {} and {}".format(
                        other_id, actor_id
                    )
                )
    for region in document.get("semantic_regions", []):
        polygon = region["polygon_m"]
        if _polygon_area(polygon) <= 1e-6:
            raise ValueError("MetaDrive semantic region polygon has zero area")
        if not all(
            _point_near_native_map(point, map_features, tolerance=6.0)
            for point in polygon
        ):
            raise ValueError(
                "MetaDrive semantic region is not grounded near generated PGMap geometry"
            )
    return {
        "actor_count": checked_actor_count,
        "map_point_count": len(map_points),
        "semantic_region_count": len(document.get("semantic_regions", [])),
        "actual_block_ids": actual_blocks,
    }


def _metadrive_environment(document, policy_class, horizon):
    from metadrive.envs.metadrive_env import MetaDriveEnv

    map_spec = document["map"]
    ego_state = document["ego"]["reference_trajectory"][0]
    environment = MetaDriveEnv(
        {
            "agent_policy": policy_class,
            "map_config": {
                "type": "block_sequence",
                "config": map_spec["block_sequence"],
                "lane_num": map_spec["lane_num"],
                "lane_width": map_spec["lane_width_m"],
                "exit_length": map_spec["exit_length_m"],
            },
            "agent_configs": {
                "default_agent": {
                    "vehicle_model": "xl",
                    "top_down_length": 5.33,
                    "top_down_width": 2.10,
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
            "horizon": horizon,
            "truncate_as_terminate": False,
        }
    )
    environment.reset(seed=int(map_spec["map_seed"]))
    vehicle = environment.agent
    if abs(float(vehicle.top_down_length) - 5.33) > 1e-9 or abs(
        float(vehicle.top_down_width) - 2.10
    ) > 1e-9:
        environment.close()
        raise ValueError("MetaDrive ego proxy footprint differs from 5.33 x 2.10")
    actual_blocks = [block.ID for block in environment.current_map.blocks]
    if actual_blocks != ["I"] + list(map_spec["block_sequence"]):
        environment.close()
        raise ValueError("MetaDrive PGMap did not preserve the submitted block tokens")
    return environment


def _metadrive_sv(path, request, seed):
    from metadrive.policy.idm_policy import IDMPolicy

    document, _ = _metadrive_native(path, request)
    environment = None
    try:
        environment = _metadrive_environment(document, IDMPolicy, 1)
        static_report = _metadrive_static_sanity(document, environment)
        return {
            "ok": True,
            "mode": "sv",
            "seed": seed,
            "validation_repetition": seed,
            "sampling_semantics": "deterministic_artifact_confirmation",
            "iterations": 1,
            "vehicle_class": type(environment.agent).__name__,
            "object_count": len(environment.engine.get_objects()),
            "static_actor_count": static_report["actor_count"],
            "semantic_region_count": static_report["semantic_region_count"],
            "actual_block_ids": static_report["actual_block_ids"],
            "map_seed": document["map"]["map_seed"],
        }
    finally:
        if environment is not None:
            environment.close()


def _metadrive_policy_class(parameters):
    import numpy as np
    from metadrive.policy.idm_policy import IDMPolicy

    class FrozenBusIDMPolicy(IDMPolicy):
        NORMAL_SPEED = float(parameters["target_speed_km_h"])
        ACC_FACTOR = float(parameters["acceleration_factor"])
        DEACC_FACTOR = float(parameters["deceleration_factor"])

        def act(self, *args, **kwargs):
            action = super().act(*args, **kwargs)
            steering = float(np.clip(action[0], -1.0, 1.0))
            longitudinal = float(np.clip(action[1], -1.0, 1.0))
            self.last_action = [steering, longitudinal]
            self.action_info["action"] = self.last_action
            return self.last_action

    return FrozenBusIDMPolicy


def _metadrive_spawn_actor(environment, actor, state, validation_seed):
    actor_type = actor["actor_type"]
    common = {
        "position": list(state["position_m"]),
        "heading_theta": float(state["heading_rad"]),
        "random_seed": int(validation_seed),
        "name": actor["actor_id"],
        "force_spawn": True,
    }
    if actor_type == "pedestrian":
        from metadrive.component.traffic_participants.pedestrian import (
            PedestrianBoundingBox,
        )

        obj = environment.engine.spawn_object(
            PedestrianBoundingBox,
            width=float(actor["width_m"]),
            length=float(actor["length_m"]),
            height=1.75,
            **common,
        )
    elif actor_type in {"e_bike", "bicycle"}:
        from metadrive.component.traffic_participants.cyclist import CyclistBoundingBox

        obj = environment.engine.spawn_object(
            CyclistBoundingBox,
            width=float(actor["width_m"]),
            length=float(actor["length_m"]),
            height=1.75,
            **common,
        )
    else:
        from metadrive.component.vehicle.vehicle_type import (
            VaryingDynamicsBoundingBoxVehicle,
        )

        vehicle_config = environment.config["vehicle_config"].copy()
        vehicle_config.update(
            {
                "vehicle_model": "varying_dynamics_bounding_box",
                "length": float(actor["length_m"]),
                "width": float(actor["width_m"]),
                "height": 1.5 if actor_type == "vehicle" else 1.4,
                "spawn_position_heading": [
                    list(state["position_m"]),
                    float(state["heading_rad"]),
                ],
                "no_wheel_friction": True,
                "top_down_length": float(actor["length_m"]),
                "top_down_width": float(actor["width_m"]),
            }
        )
        vehicle_common = dict(common)
        vehicle_common["heading"] = vehicle_common.pop("heading_theta")
        obj = environment.engine.spawn_object(
            VaryingDynamicsBoundingBoxVehicle,
            vehicle_config=vehicle_config,
            **vehicle_common,
        )
    return obj


def _metadrive_apply_actor_states(
    environment, document, step, actor_objects, validation_seed
):
    for offset, actor in enumerate(document["actors"]):
        state = actor["trajectory"][step] if step < document["horizon_steps"] else None
        valid = state is not None and state["valid"] is True
        actor_id = actor["actor_id"]
        obj = actor_objects.get(actor_id)
        if not valid:
            if obj is not None:
                environment.engine.clear_objects([obj.id], force_destroy=True)
                actor_objects.pop(actor_id, None)
            continue
        if obj is None:
            obj = _metadrive_spawn_actor(
                environment, actor, state, validation_seed + offset + 1
            )
            actor_objects[actor_id] = obj
        obj.set_position(list(state["position_m"]))
        obj.set_heading_theta(float(state["heading_rad"]))
        obj.set_velocity(
            [math.cos(float(state["heading_rad"])), math.sin(float(state["heading_rad"]))],
            value=float(state["speed_m_s"]),
        )
        navigation = getattr(obj, "navigation", None)
        if navigation is not None:
            navigation.update_localization(obj)


def _metadrive_trace_row(environment, step, dt, actor_types):
    objects = []
    ego_id = environment.agent.id
    for object_id, actor in environment.engine.get_objects().items():
        if not hasattr(actor, "position"):
            continue
        position = actor.position
        if len(position) < 2 or not all(_finite_number(value) for value in position[:2]):
            continue
        speed_km_h = float(getattr(actor, "speed_km_h", 0.0))
        objects.append(
            {
                "object_id": str(object_id),
                "is_ego": str(object_id) == str(ego_id),
                "kind": type(actor).__name__,
                "artifact_actor_type": actor_types.get(str(object_id)),
                "position": [float(position[0]), float(position[1])],
                "heading_rad": float(getattr(actor, "heading_theta", 0.0)),
                "speed_m_s": speed_km_h / 3.6,
            }
        )
    policy = environment.agent_manager.get_policy(ego_id)
    action = (policy.get_action_info() or {}).get("action") if policy is not None else None
    ego_action = None
    if isinstance(action, (list, tuple)) and len(action) == 2:
        ego_action = {
            "steering": float(action[0]),
            "normalized_throttle_brake": float(action[1]),
        }
    return {
        "step": step,
        "simulated_time_seconds": step * dt,
        "objects": sorted(objects, key=lambda item: item["object_id"]),
        "ego_action": ego_action,
    }


def _metadrive_ne(path, request):
    document, _ = _metadrive_native(path, request)
    parameters = request["controller"]["parameters"]
    policy_class = _metadrive_policy_class(parameters)
    dt = float(parameters["dt_seconds"])
    steps_budget = int(round(float(request["rollout_seconds"]) / dt))
    environment = None
    rows = []
    actor_objects = {}
    actor_types = {actor["actor_id"]: actor["actor_type"] for actor in document["actors"]}
    termination_reason = "time_limit"
    try:
        environment = _metadrive_environment(document, policy_class, steps_budget)
        policy = environment.agent_manager.get_policy(environment.agent.id)
        if not isinstance(policy, policy_class):
            raise RuntimeError("MetaDrive did not install the frozen IDM-PID policy")
        _metadrive_apply_actor_states(
            environment, document, 0, actor_objects, int(request["seed"])
        )
        rows.append(_metadrive_trace_row(environment, 0, dt, actor_types))
        steps = 0
        for steps in range(1, steps_budget + 1):
            _metadrive_apply_actor_states(
                environment, document, steps, actor_objects, int(request["seed"])
            )
            _, _, terminated, truncated, info = environment.step([0.0, 0.0])
            rows.append(_metadrive_trace_row(environment, steps, dt, actor_types))
            if terminated or truncated:
                reasons = sorted(
                    key
                    for key, value in info.items()
                    if isinstance(value, bool) and value
                )
                termination_reason = ",".join(reasons) or (
                    "terminated" if terminated else "truncated"
                )
                break
        _write_trace(request["trace_path"], rows)
        return {
            "ok": True,
            "mode": "ne",
            "steps": steps,
            "simulated_seconds": min(float(request["rollout_seconds"]), steps * dt),
            "termination_reason": termination_reason,
            "runtime_controller_class": policy_class.__name__,
            "actual_block_ids": [block.ID for block in environment.current_map.blocks],
            "map_seed": document["map"]["map_seed"],
        }
    finally:
        if environment is not None:
            environment.close()


def execute(request):
    path = _verify_request(request)
    platform = request["platform"]
    mode = request["mode"]
    if platform == "carla":
        if mode == "compile":
            return _carla_compile(path, request)[1]
        if mode == "sv":
            return _carla_sv(
                path,
                int(request["seed"]),
                int(request["max_iterations"]),
                request,
            )
        return _carla_ne(path, request)
    if mode == "compile":
        return _metadrive_compile(path, request)[1]
    if mode == "sv":
        return _metadrive_sv(path, request, int(request["seed"]))
    return _metadrive_ne(path, request)


def main():
    try:
        request = _strict_json_object_bytes(sys.stdin.buffer.read())
        result = execute(request)
    except Exception as exc:
        result = {
            "ok": False,
            "error": "{}: {}".format(type(exc).__name__, exc),
            "traceback": traceback.format_exc(limit=8),
        }
    sys.stdout.buffer.write(_canonical_bytes(result))


if __name__ == "__main__":
    main()
