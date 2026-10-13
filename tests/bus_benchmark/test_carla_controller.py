"""Reference actions recorded from the Scenic controller before extraction."""

import json
import math
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from bus_benchmark.runtime_worker import fixed_carla_controls, _carla_controller_classes


class Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z

    def __sub__(self, other):
        return Vector(self.x - other.x, self.y - other.y, self.z - other.z)


def fixture():
    rows = [
        (0.0, 0.0, True, None),
        (1.0, 0.6, True, None),
        (7.0, -0.3, True, None),
        (9.0, 0.8, True, None),
        (8.0, 0.0, False, None),
        (6.0, 0.0, True, (5.0, 0.0, 2.0)),
        (6.0, -0.8, True, (1.0, 4.0, 0.0)),
        (0.0, 0.0, True, None),
    ]
    current = {"row": rows[0]}
    position = Vector()
    transform = types.SimpleNamespace(
        location=position,
        get_forward_vector=lambda: Vector(1.0),
        get_right_vector=lambda: Vector(0.0, 1.0),
    )

    def waypoint(*a, **k):
        speed, angle, on_map, lead = current["row"]
        if not on_map:
            return None
        target = types.SimpleNamespace(
            transform=types.SimpleNamespace(location=Vector(4.0, 4.0 * math.tan(angle)))
        )
        return types.SimpleNamespace(
            next=lambda distance: [target], transform=target.transform
        )

    def others():
        lead = current["row"][3]
        if lead is None:
            return []
        return [
            types.SimpleNamespace(
                id=2,
                get_location=lambda: Vector(lead[0], lead[1]),
                get_velocity=lambda: Vector(lead[2]),
            )
        ]

    world = types.SimpleNamespace(
        get_map=lambda: types.SimpleNamespace(get_waypoint=waypoint),
        get_actors=lambda: types.SimpleNamespace(filter=lambda pattern: others()),
    )
    actor = types.SimpleNamespace(
        id=1,
        get_transform=lambda: transform,
        get_velocity=lambda: Vector(current["row"][0]),
        get_world=lambda: world,
    )
    return rows, current, actor


class ControllerTests(unittest.TestCase):
    def test_native_and_scenic_actions_preserve_reference_sequence(self):
        parameters = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "benchmark_configs/platforms/carla_controller_draft.json"
            ).read_text()
        )["parameters"]
        # Filled from the pre-extraction controller, including steering rate,
        # jerk limits, a missing waypoint, following and ignored lateral traffic.
        expected = [
            {"throttle": 0.5714285714285715, "brake": 0.0, "steering": 0.0},
            {
                "throttle": 0.9997926399999967,
                "brake": 0.0,
                "steering": 0.06948590381426202,
            },
            {"throttle": 0.5021286399920342, "brake": 0.0, "steering": 0.0},
            {
                "throttle": 0.0,
                "brake": 0.01212748800139403,
                "steering": 0.06948590381426202,
            },
            {"throttle": 0.0, "brake": 1.0, "steering": 0.0},
            {"throttle": 0.0, "brake": 0.11212748800139404, "steering": 0.0},
            {
                "throttle": 0.0,
                "brake": 0.01212748800139403,
                "steering": -0.06948590381426202,
            },
            {"throttle": 0.5021286399920342, "brake": 0.0, "steering": 0.0},
        ]
        modules = {
            "carla": types.ModuleType("carla"),
            "scenic.core.dynamics": types.ModuleType("scenic.core.dynamics"),
            "scenic.domains.driving.actions": types.ModuleType(
                "scenic.domains.driving.actions"
            ),
        }
        modules["carla"].LaneType = types.SimpleNamespace(Driving=1)
        modules["scenic.core.dynamics"].Behavior = object
        actions = modules["scenic.domains.driving.actions"]
        for name, key in [
            ("SetThrottleAction", "throttle"),
            ("SetBrakeAction", "brake"),
            ("SetSteerAction", "steering"),
        ]:
            setattr(actions, name, lambda value, key=key: (key, value))
        with patch.dict("sys.modules", modules):
            rows, current, actor = fixture()
            native = fixed_carla_controls(actor, parameters)
            scenic = _carla_controller_classes(parameters)().makeGenerator(
                types.SimpleNamespace(carlaActor=actor)
            )
            observed = []
            for row in rows:
                current["row"] = row
                action = next(native)
                observed.append(action)
                wrapped = dict(next(scenic))
                for key, value in action.items():
                    self.assertAlmostEqual(wrapped[key], value, places=12)
        self.assertEqual(len(observed), len(expected))
        for action, reference in zip(observed, expected):
            for key, value in reference.items():
                self.assertAlmostEqual(action[key], value, places=12)
