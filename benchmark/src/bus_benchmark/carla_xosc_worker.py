"""Execute declared XOSC carriers with upstream ScenarioRunner and shared ego control."""

import contextlib
import json
import math
from pathlib import Path
import random
import sys
from types import SimpleNamespace

if __package__:
    from . import carla_xosc
else:
    import carla_xosc


def _sources(request):
    context = request.get("openscenario_context")
    if not isinstance(context, dict) or set(context) != {"source_tree", "server_tree"}:
        raise ValueError("native XOSC execution requires registered ScenarioRunner sources")
    source = context["source_tree"]
    server = context["server_tree"]
    if source.get("role") != "openscenario_source" or server.get("role") != "carla_server_distribution":
        raise ValueError("native XOSC source roles differ from the registered backend")
    root = Path(source["path"]).resolve()
    server_root = Path(server["path"]).resolve()
    if Path(request["carla_server_binary"]["path"]).resolve().relative_to(server_root) != Path("CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping"):
        raise ValueError("native XOSC server differs from its source tree")
    sys.path[:0] = [str(root), str(server_root / "PythonAPI/carla")]
    from srunner.scenarios import open_scenario
    from agents.navigation import basic_agent
    Path(open_scenario.__file__).resolve().relative_to(root)
    Path(basic_agent.__file__).resolve().relative_to(server_root / "PythonAPI/carla")
    return root


def _xml_file(path):
    document = carla_xosc.parse(path.read_bytes())
    raw = document["xosc"].encode("utf-8")
    target = path.with_name("native-scenario.xosc")
    if target.exists():
        if target.is_symlink() or target.read_bytes() != raw:
            raise ValueError("staged native XOSC differs from the frozen carrier")
    else:
        with target.open("xb") as stream:
            stream.write(raw)
    return document, target


def _require_native_carla_version(client):
    client_version = str(client.get_client_version())
    server_version = str(client.get_server_version())
    if client_version.split("-", 1)[0] != "0.9.13" or server_version != client_version:
        raise ValueError("public XOSC demo requires matching native CARLA 0.9.13 client and server builds")


def _load(path, request, common):
    import carla
    from srunner.scenarioconfigs.openscenario_configuration import OpenScenarioConfiguration
    from srunner.scenariomanager.carla_data_provider import CarlaDataProvider
    from srunner.scenariomanager.timer import GameTime
    from srunner.scenarios.open_scenario import OpenScenario
    document, xml = _xml_file(path)
    endpoint = request["carla_endpoint"]
    client = carla.Client(endpoint["host"], endpoint["port"])
    client.set_timeout(endpoint["timeout_seconds"])
    _require_native_carla_version(client)
    CarlaDataProvider.set_client(client)
    GameTime.restart()
    config = OpenScenarioConfiguration(str(xml), client, {})
    world = client.get_world()
    original_settings = world.get_settings()
    synchronous = world.get_settings()
    synchronous.synchronous_mode = True
    synchronous.fixed_delta_seconds = 0.1
    world.apply_settings(synchronous)
    CarlaDataProvider.set_world(world)
    scenario = None
    try:
        if len(config.ego_vehicles) != 1:
            raise ValueError("native ScenarioRunner requires one marked ego actor")
        egos = CarlaDataProvider.request_new_actors(config.ego_vehicles)
        if not egos or len(egos) != 1:
            raise ValueError("native XOSC ego spawn was rejected")
        scenario = OpenScenario(world, egos, config, str(xml), criteria_enable=False, timeout=30)
        actors = [egos[0]] + list(scenario.other_actors)
        entities = carla_xosc.xml_root(document["xosc"]).findall("Entities/ScenarioObject")
        expected_roles = {entity.get("name") for entity in entities}
        roles = [actor.attributes.get("role_name") for actor in actors]
        if len(actors) != len(entities) or len(set(roles)) != len(roles) or set(roles) != expected_roles or any(not actor.is_alive for actor in actors):
            raise ValueError("native XOSC realization is incomplete or contains duplicate actors")
        by_role = {entity.get("name"): entity for entity in entities}
        for actor in actors:
            entity = by_role[actor.attributes["role_name"]]
            definition = entity.find("Vehicle")
            if definition is None:
                definition = entity.find("Pedestrian")
            if definition is None:
                definition = entity.find("MiscObject")
            expected_model = definition.get("model") or definition.get("name")
            if not expected_model or actor.type_id != expected_model:
                raise ValueError("native XOSC actor blueprint differs from the declared entity")
        native = common._carla_runtime_map_evidence(SimpleNamespace(client=client, world=world), request)
        return document, scenario, actors, world, original_settings, native
    except BaseException:
        if scenario is not None:
            scenario.scenario.terminate()
        CarlaDataProvider.cleanup()
        world.apply_settings(original_settings)
        raise


def _objects(document, actors):
    root = carla_xosc.xml_root(document["xosc"])
    configured = {obj.get("name"): obj for obj in root.findall("Entities/ScenarioObject")}
    result = []
    for index, actor in enumerate(actors):
        transform = actor.get_transform()
        pose = transform.location
        role = actor.attributes.get("role_name")
        entity = configured.get(role)
        if entity is None:
            raise ValueError("native actor has no declared XOSC entity")
        dims = entity.find("Vehicle/BoundingBox/Dimensions")
        length = 2 * actor.bounding_box.extent.x
        width = 2 * actor.bounding_box.extent.y
        if index == 0 and dims is not None:
            length = float(dims.get("length"))
            width = float(dims.get("width"))
        result.append({
            "object_index": index, "kind": "Bicycle" if actor.type_id.startswith("vehicle.diamondback") else "Pedestrian" if actor.type_id.startswith("walker.") else "Car" if actor.type_id.startswith("vehicle.") else "Object",
            "is_ego": index == 0, "position": [pose.x, pose.y],
            "heading_rad": math.radians(transform.rotation.yaw),
            "length_m": length, "width_m": width, "blueprint": actor.type_id,
        })
    return result


def _sanity(document, actors, world, common):
    import carla
    objects = _objects(document, actors)
    for row, actor in zip(objects, actors):
        if row["length_m"] <= 0 or row["width_m"] <= 0 or not all(math.isfinite(v) for v in row["position"] + [row["heading_rad"], row["length_m"], row["width_m"]]):
            raise ValueError("native XOSC actor geometry is invalid")
        if world.get_map().get_waypoint(actor.get_location(), project_to_road=False, lane_type=carla.LaneType.Any) is None:
            raise ValueError("native XOSC actor is outside the CARLA map")
    boxes = [common._oriented_box_corners(row["position"], row["heading_rad"], row["length_m"], row["width_m"]) for row in objects]
    for index, box in enumerate(boxes):
        height = actors[index].get_location().z
        if any(world.get_map().get_waypoint(carla.Location(x=corner[0], y=corner[1], z=height), project_to_road=False, lane_type=carla.LaneType.Any) is None for corner in box):
            raise ValueError("native XOSC actor footprint is outside the CARLA map")
        if any(common._boxes_overlap(box, other) for other in boxes[:index]):
            raise ValueError("native XOSC actors have initial physical overlap")
    ego = objects[0]
    if ego["blueprint"] != "vehicle.chevrolet.impala" or abs(ego["length_m"] - 5.33) > 1e-9 or abs(ego["width_m"] - 2.1) > 1e-9:
        raise ValueError("native XOSC ego does not use the approved proxy")
    return objects


def _execute(path, request, common):
    source = _sources(request)
    document, xml = _xml_file(path)
    if request["mode"] == "compile":
        import xmlschema
        xsd = source / "srunner/openscenario/OpenSCENARIO.xsd"
        xmlschema.XMLSchema(str(xsd)).validate(str(xml))
        # Compilation validates the native XSD; dynamic action construction is
        # checked by the native ScenarioRunner during realization and execution.
        static = next(b for b in request["carla_map_context"]["staged_catalog"] if b["path"].endswith(".xodr"))
        return {"ok": True, "mode": "compile", "native_type": "OpenSCENARIO1.0", "object_spec_count": len(carla_xosc.xml_root(document["xosc"]).findall("Entities/ScenarioObject")), "static_opendrive": static}
    random.seed(int(request["seed"]))
    document, scenario, actors, world, original_settings, native = _load(path, request, common)
    from srunner.scenariomanager.carla_data_provider import CarlaDataProvider
    from srunner.scenariomanager.timer import GameTime
    try:
        objects = _sanity(document, actors, world, common)
        if request["mode"] == "sv":
            return {"ok": True, "mode": "sv", "seed": request["seed"], "iterations": 1, "sampling_semantics": "native_xosc_realization", "realization": {"objects": objects}, "static_opendrive": next(b for b in request["carla_map_context"]["staged_catalog"] if b["path"].endswith(".xodr"))}
        import carla
        import py_trees
        parameters = request["controller"]["parameters"]
        dt = float(parameters["dt_seconds"])
        if dt != 0.1 or float(request["rollout_seconds"]) != 30:
            raise ValueError("native XOSC observation budget differs from the shared controller")

        class FixedIDMPIDBehavior:
            def __init__(self, actor):
                self.controls = common.fixed_carla_controls(actor, parameters)

            def next(self):
                return next(self.controls)

        policy = FixedIDMPIDBehavior(actors[0])
        rows = [{"step": 0, "simulated_time_seconds": 0.0, "objects": objects, "ego_action": None}]
        reason = "time_limit"
        steps = 0
        for step in range(1, 301):
            GameTime.on_carla_tick(world.get_snapshot().timestamp)
            CarlaDataProvider.on_carla_tick()
            scenario.scenario.scenario_tree.tick_once()
            action = policy.next()
            # The same query-blind policy controls ego after native NPC updates.
            actors[0].apply_control(carla.VehicleControl(throttle=action["throttle"], brake=action["brake"], steer=action["steering"]))
            world.tick()
            steps = step
            states = _objects(document, actors)
            for state, actor in zip(states, actors):
                velocity = actor.get_velocity()
                state["speed_m_s"] = math.hypot(velocity.x, velocity.y)
            rows.append({"step": step, "simulated_time_seconds": step * dt, "objects": states, "ego_action": dict(action)})
            status = scenario.scenario.scenario_tree.status
            if status != py_trees.common.Status.RUNNING:
                reason = "native_scenario_success" if status == py_trees.common.Status.SUCCESS else "native_scenario_failure"
                break
        common._write_trace(request["trace_path"], rows)
        result = {"ok": reason != "native_scenario_failure", "mode": "ne", "steps": steps, "simulated_seconds": steps * dt, "termination_reason": reason, "runtime_controller_class": type(policy).__name__, "native_runtime": native, "native_status": str(scenario.scenario.scenario_tree.status)}
        if not result["ok"]:
            result["error"] = "native ScenarioRunner ended in FAILURE or invalid status"
        return result
    finally:
        scenario.scenario.terminate()
        CarlaDataProvider.cleanup()
        world.apply_settings(original_settings)


def execute(path, request, common):
    with contextlib.redirect_stdout(sys.stderr):
        return _execute(path, request, common)
