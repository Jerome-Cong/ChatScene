"""Declared OpenSCENARIO carrier and byte-local ego proxy normalization."""

import json
import math
import re
import xml.etree.ElementTree as ET


ARTIFACT_TYPE = "carla_xosc_bundle_v0.1"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate native XOSC carrier key")
        result[key] = value
    return result


def xml_root(text):
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 8 * 1024 * 1024:
        raise ValueError("native XOSC text is empty or exceeds its bound")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise ValueError("native XOSC must not contain DTD or entity declarations")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("native XOSC XML is malformed") from exc
    if root.tag != "OpenSCENARIO":
        raise ValueError("native XOSC requires OpenSCENARIO root")
    return root


def ego(root):
    objects = root.findall("Entities/ScenarioObject")
    names = [obj.get("name") for obj in objects]
    if not objects or any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("native XOSC entity names are missing or duplicated")
    candidates = [obj for obj in objects if any(
        prop.get("name") == "type" and prop.get("value") == "ego_vehicle"
        for prop in obj.iter("Property")
    )]
    if len(candidates) != 1 or len(candidates[0].findall("Vehicle")) != 1:
        raise ValueError("native XOSC requires exactly one marked ego Vehicle")
    vehicle = candidates[0].find("Vehicle")
    dimensions = vehicle.find("BoundingBox/Dimensions")
    if dimensions is None or not vehicle.get("name") or any(dimensions.get(k) is None for k in ("length", "width")):
        raise ValueError("native XOSC ego lacks blueprint or dimensions")
    return candidates[0], vehicle, dimensions


def town(root):
    declarations = root.findall("RoadNetwork/LogicFile")
    if len(declarations) != 1:
        raise ValueError("native XOSC requires one LogicFile declaration")
    value = declarations[0].get("filepath")
    if not isinstance(value, str) or re.fullmatch(r"Town(?:0[1-7]|10HD)", value) is None:
        raise ValueError("native XOSC map must be a supported literal CARLA Town")
    return value


def parse(payload):
    document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique)
    if not isinstance(document, dict) or set(document) != {"artifact_type", "xosc", "ego_route"} or document["artifact_type"] != ARTIFACT_TYPE:
        raise ValueError("native XOSC carrier fields or type are invalid")
    root = xml_root(document["xosc"])
    ego(root)
    town(root)
    if any(node.tag.rsplit("}", 1)[-1] in {"UserDefinedAction", "CustomCommandAction"} for node in root.iter()):
        raise ValueError("native XOSC executable extensions are outside the bound demo profile")
    for controller in root.iter("Controller"):
        modules = [prop.get("value") for prop in controller.iter("Property") if prop.get("name") == "module"]
        if any(module not in {"external_control", "simple_vehicle_control"} for module in modules):
            raise ValueError("native XOSC external controller is outside the bound demo profile")
    if root.findall("CatalogLocations/*") or any(node.get("filepath") for node in root.findall("RoadNetwork/SceneGraphFile")):
        raise ValueError("external native XOSC catalogs require a separate bound profile")
    route = document["ego_route"]
    if not isinstance(route, dict) or set(route) != {"start_location", "target_location"}:
        raise ValueError("native XOSC carrier lacks the original ego route")
    for location in route.values():
        if not isinstance(location, dict) or set(location) != {"x", "y", "z", "h"}:
            raise ValueError("native ego route location is invalid")
        try:
            valid = all(not isinstance(v, bool) and math.isfinite(float(v)) for v in location.values())
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("native ego route contains non-finite values")
    return document


def _attribute(tag, name, value):
    pattern = re.compile(r"(?P<prefix>\b" + name + r"\s*=\s*)(?P<quote>['\"])(?P<value>.*?)(?P=quote)", re.S)
    matches = list(pattern.finditer(tag))
    if len(matches) != 1:
        raise ValueError("native ego {} attribute is ambiguous".format(name))
    match = matches[0]
    return tag[:match.start("value")] + value + tag[match.end("value"):]


def finalize(payload):
    document = parse(payload)
    root = xml_root(document["xosc"])
    obj, _, _ = ego(root)
    pattern = re.compile(r"<ScenarioObject\b[^>]*>.*?</ScenarioObject\s*>", re.S)
    try:
        blocks = [(match, ET.fromstring(match.group())) for match in pattern.finditer(document["xosc"])]
    except ET.ParseError as exc:
        raise ValueError("native XOSC entity source span is malformed") from exc
    selected = [(match, node) for match, node in blocks if node.get("name") == obj.get("name")]
    if len(selected) != 1:
        raise ValueError("native XOSC ego source span is ambiguous")
    match, _ = selected[0]
    block = match.group()
    tags = list(re.finditer(r"<Vehicle\b[^>]*>", block))
    if len(tags) != 1:
        raise ValueError("native XOSC ego Vehicle source span is ambiguous")
    vehicle = tags[0]
    normalized = _attribute(vehicle.group(), "name", "vehicle.chevrolet.impala")
    block = block[:vehicle.start()] + normalized + block[vehicle.end():]
    dimensions = list(re.finditer(r"<Dimensions\b[^>]*?/?>", block))
    if len(dimensions) != 1:
        raise ValueError("native XOSC ego Dimensions source span is ambiguous")
    dim = dimensions[0]
    normalized = _attribute(_attribute(dim.group(), "length", "5.33"), "width", "2.1")
    block = block[:dim.start()] + normalized + block[dim.end():]
    document["xosc"] = document["xosc"][:match.start()] + block + document["xosc"][match.end():]
    parse(json.dumps(document).encode())
    return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def verify(raw, final):
    if finalize(raw) != final:
        raise ValueError("native XOSC final artifact differs from ego-only normalization")


def observe(document):
    """Read initial states only; planned actions are not observed trajectories."""
    document = parse(json.dumps(document).encode())
    root = xml_root(document["xosc"])
    ego_entity, _, _ = ego(root)
    header = root.find("FileHeader")
    native_coordinates = header is not None and header.get("description", "").startswith("CARLA:")
    objects = []
    for index, entity in enumerate(root.findall("Entities/ScenarioObject")):
        name = entity.get("name")
        privates = [node for node in root.findall("Storyboard/Init/Actions/Private") if node.get("entityRef") == name]
        positions = [position for node in privates for position in node.findall("PrivateAction/TeleportAction/Position/WorldPosition")]
        if len(positions) != 1:
            raise ValueError("native XOSC initial observation requires one explicit WorldPosition per entity")
        position = positions[0]
        xy = [float(position.get("x", "0")), float(position.get("y", "0"))]
        heading = float(position.get("h", "0"))
        if native_coordinates:
            xy[1] *= -1
            heading *= -1
        actor = entity.find("Vehicle")
        if actor is None:
            actor = entity.find("Pedestrian")
        if actor is None:
            actor = entity.find("MiscObject")
        if actor is None:
            raise ValueError("native XOSC initial entity type is unsupported")
        dimensions = actor.find("BoundingBox/Dimensions")
        if dimensions is None:
            raise ValueError("native XOSC initial entity has no declared dimensions")
        objects.append({
            "object_index": index, "kind": "Bicycle" if actor.get("vehicleCategory") == "bicycle" else "Car" if actor.tag == "Vehicle" else "Pedestrian" if actor.tag == "Pedestrian" else "Object",
            "is_ego": entity is ego_entity, "initial_position": xy,
            "trajectory": [xy], "heading_rad": heading,
            "length_m": float(dimensions.get("length")), "width_m": float(dimensions.get("width")),
            "blueprint": actor.get("name", ""), "motion_observed": False,
        })
    return {"platform": "carla", "objects": objects, "observation_scope": "native_initial_states_only"}
