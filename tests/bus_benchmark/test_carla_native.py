import copy
from contextlib import ExitStack
import hashlib
import io
import itertools
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from bus_benchmark.carla_native import parse, validate, observe, ARTIFACT_TYPE
from bus_benchmark.generation import _finalize_ego_artifact
from bus_benchmark.runtime import _prepare_carla_capture_input
from bus_benchmark.jsonio import sha256_file
from bus_benchmark import carla_native_worker
from bus_benchmark.native_observer import _carla_observation
from bus_benchmark.adapters.chatscene import _run_process
from bus_benchmark.runtime import _validate_carla_runtime_dependencies
from bus_benchmark.runtime import _revalidate_normalized_metadrive_runtime_dependencies
from bus_benchmark.errors import ValidationError


def document():
    transform = {"location_m": [0.0, 0.0, 1.0], "rotation_deg": [0.0, 0.0, 0.0]}
    control = {
        "type": "vehicle",
        "throttle": 0.0,
        "brake": 0.0,
        "steer": 0.0,
        "hand_brake": False,
        "reverse": False,
        "manual_gear_shift": False,
        "gear": 0,
    }
    state = {
        "valid": True,
        "transform": copy.deepcopy(transform),
        "velocity_m_s": [0, 0, 0],
        "control": control,
    }
    road = "<OpenDRIVE></OpenDRIVE>"
    return {
        "artifact_type": ARTIFACT_TYPE,
        "coordinate_frame": "carla_left_handed_xyz_m",
        "time_step_s": 0.1,
        "carla_version": "0.9.15",
        "map": {
            "name": "Town05",
            "opendrive": road,
            "sha256": hashlib.sha256(road.encode()).hexdigest(),
        },
        "weather": {"cloudiness": 0.0},
        "actors": [
            {
                "id": "o0",
                "is_ego": True,
                "blueprint": "vehicle.lincoln.mkz_2017",
                "kind": "vehicle",
                "length_m": 4.9,
                "width_m": 1.8,
                "initial_transform": transform,
            }
        ],
        "frames": [
            {"step": 0, "elapsed_seconds": 0.0, "states": [copy.deepcopy(state)]},
            {"step": 1, "elapsed_seconds": 0.1, "states": [copy.deepcopy(state)]},
            {"step": 2, "elapsed_seconds": 0.2, "states": [{"valid": False}]},
        ],
        "termination": {"kind": "native_program_complete", "step": 2},
    }


class NativeCarlaTests(unittest.TestCase):
    def test_native_sdk_option_does_not_relax_metadrive_dependencies(self):
        deps = {
            name: {}
            for name in (
                "environment_manifest",
                "metadrive_module",
                "source_tree",
                "native_carla_files",
            )
        }
        deps["verification_status"] = "draft_bound"
        with self.assertRaisesRegex(ValidationError, "MetaDrive runtime lacks bound"):
            _revalidate_normalized_metadrive_runtime_dependencies(
                {"runtime_dependencies": deps}
            )

    def test_package_observer_reads_native_json(self):
        self.assertEqual(
            _carla_observation(json.dumps(document())), observe(document())
        )

    def test_deep_native_json_is_a_controlled_failure(self):
        with self.assertRaisesRegex(ValueError, "nesting"):
            parse("[" * 2000 + "0" + "]" * 2000)

    def test_process_credential_output_is_suppressed_before_recording(self):
        credentials = {"OPENAI_API_KEY": "test-only-generation-credential",
                       "EMBEDDING_API_KEY": "test-only-embedding-credential"}
        for name in credentials:
            for stream in ("stdout", "stderr"):
                with self.subTest(name=name, stream=stream), ExitStack() as stack:
                    directory = stack.enter_context(tempfile.TemporaryDirectory())
                    stack.enter_context(patch.dict(os.environ, credentials))
                    result = _run_process(
                        [sys.executable, "-c", 'import os,sys; print(os.environ[{!r}],file=sys.{})'.format(name, stream)],
                        Path(directory), 5, list(credentials), None, 4096, 4096,
                    )
                self.assertIsNotNone(result.error)
                for sentinel in credentials.values():
                    self.assertNotIn(sentinel.encode(), result.stdout + result.stderr)
                    self.assertNotIn(sentinel, result.error)

    def test_unclosed_stream_suppresses_partial_credential_bytes(self):
        released = threading.Event()
        fragment = b"test-only-partial-credential"

        class UnclosedStream:
            first = True

            def read(self, size):
                if self.first:
                    self.first = False
                    return fragment
                released.wait(10)
                return b""

            def close(self):
                pass

        process = types.SimpleNamespace(stdout=UnclosedStream(), stderr=io.BytesIO(),
                                        stdin=None, returncode=0, poll=lambda: 0)
        try:
            with tempfile.TemporaryDirectory() as directory, patch(
                "bus_benchmark.adapters.chatscene.subprocess.Popen", return_value=process
            ), patch("bus_benchmark.adapters.chatscene._terminate_process_group"):
                result = _run_process([sys.executable], Path(directory), 5, [], None, 4096, 4096)
            self.assertIn("streams did not close", result.error)
            self.assertNotIn(fragment, result.stdout + result.stderr)
        finally:
            released.set()

    def test_installed_sdk_requires_complete_files_inside_registered_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "env"
            package = env / "lib/python3.10/site-packages/carla"
            package.mkdir(parents=True)
            for name in (
                "__init__.py",
                "libcarla.cpython-310-x86_64-linux-gnu.so",
                "command.py",
            ):
                (package / name).write_bytes(b"fixture")

            def binding(path):
                return {
                    "path": str(path.resolve()),
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }

            manifest = root / "environment.json"
            manifest.write_text(json.dumps({"python_environment_root": str(env)}))
            road = root / "Town05.xodr"
            road.write_text("<OpenDRIVE/>")
            server_root = root / "server"
            server = server_root / "CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping"
            server.parent.mkdir(parents=True)
            server.write_bytes(b"fixture")
            deps = {
                "environment_manifest": binding(manifest),
                "python_path_entries": [],
                "source_trees": [
                    {"role": "carla_server_distribution", "path": str(server_root)},
                    {"role": "scenic_editable_source", "path": str(root / "scenic")},
                ],
                "map_catalog": [binding(road)],
                "native_carla_files": [binding(p) for p in sorted(package.iterdir())],
            }
            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "bus_benchmark.runtime._validate_runtime_environment_manifest",
                        return_value=binding(manifest),
                    )
                )
                stack.enter_context(
                    patch(
                        "bus_benchmark.runtime._validate_runtime_tree_binding",
                        side_effect=lambda value, *a, **k: value,
                    )
                )
                stack.enter_context(
                    patch(
                        "bus_benchmark.runtime._validate_carla_server_binary",
                        return_value=binding(server),
                    )
                )
                self.assertEqual(
                    _validate_carla_runtime_dependencies(
                        deps, {}, binding(server), formal=False
                    )["native_carla_files"],
                    deps["native_carla_files"],
                )
                incomplete = copy.deepcopy(deps)
                incomplete["native_carla_files"].pop()
                with self.assertRaisesRegex(ValidationError, "incomplete"):
                    _validate_carla_runtime_dependencies(
                        incomplete, {}, binding(server), formal=False
                    )
                (package / "unexpected.py").write_text("unregistered code")
                with self.assertRaisesRegex(ValidationError, "incomplete"):
                    _validate_carla_runtime_dependencies(
                        deps, {}, binding(server), formal=False
                    )
                manifest.write_text(
                    json.dumps(
                        {"python_environment_root": str(root / "another-environment")}
                    )
                )
                with self.assertRaisesRegex(ValidationError, "escapes"):
                    _validate_carla_runtime_dependencies(
                        deps, {}, binding(server), formal=False
                    )

    def test_native_finalization_only_changes_ego_geometry_and_blueprint(self):
        d = document()
        raw = json.dumps(d).encode()
        result = _finalize_ego_artifact("carla", raw, artifact_type=ARTIFACT_TYPE)
        expected = copy.deepcopy(d)
        expected["actors"][0].update(
            blueprint="vehicle.chevrolet.impala", length_m=5.33, width_m=2.1
        )
        self.assertEqual(parse(result.final_bytes), expected)
        self.assertEqual(parse(raw), d)
        self.assertEqual(
            _finalize_ego_artifact(
                "carla", result.final_bytes, artifact_type=ARTIFACT_TYPE
            ).changes,
            (),
        )
        self.assertEqual(len(observe(expected)["objects"][0]["trajectory"]), 2)

    def test_unknown_metadata_duplicate_keys_nonfinite_and_mask_reappearance_rejected(
        self,
    ):
        for mutate in [
            lambda d: d.update(query_text="forbidden input"),
            lambda d: d["actors"][0].update(length_m=float("nan")),
            lambda d: d["frames"][0]["states"][0]["transform"][
                "location_m"
            ].__setitem__(0, float("inf")),
            lambda d: d["frames"][0]["states"][0].update(valid=False),
            lambda d: d["map"].update(sha256="0" * 64),
        ]:
            d = document()
            mutate(d)
            with self.assertRaises((ValueError, OverflowError)):
                validate(d)
        with self.assertRaises(ValueError):
            parse('{"artifact_type":"x","artifact_type":"y"}')

    def test_first_frame_and_initial_transform_must_be_identical(self):
        d = document()
        d["actors"][0]["initial_transform"]["location_m"][0] = 10
        with self.assertRaisesRegex(ValueError, "authoritative first frame"):
            validate(d)

    def test_truncated_capture_or_forged_termination_cannot_run_as_complete(self):
        for change in ("missing", "valid_terminal", "wrong_step"):
            d = document()
            if change == "missing":
                d.pop("termination")
            elif change == "valid_terminal":
                d["frames"][-1]["states"] = [copy.deepcopy(d["frames"][0]["states"][0])]
            else:
                d["termination"]["step"] = 1
            with self.assertRaises(ValueError):
                validate(d)

    def test_budget_completion_requires_all_real_timestamps_and_cleanup(self):
        d = document()
        state = copy.deepcopy(d["frames"][0]["states"][0])
        d["frames"] = [
            {"step": i, "elapsed_seconds": i * 0.1, "states": [copy.deepcopy(state)]}
            for i in range(301)
        ]
        d["frames"].append(
            {"step": 301, "elapsed_seconds": 30.1, "states": [{"valid": False}]}
        )
        d["termination"] = {
            "kind": "native_observation_budget_complete",
            "step": 301,
            "budget_seconds": 30.0,
        }
        validate(d)
        longer_cleanup = copy.deepcopy(d)
        for i in (302, 303, 304):
            longer_cleanup["frames"].append(
                {"step": i, "elapsed_seconds": i * 0.1, "states": [{"valid": False}]}
            )
        longer_cleanup["termination"]["step"] = 304
        validate(longer_cleanup)
        for change in ("short", "missing_tick", "early_ego_loss", "budget"):
            bad = copy.deepcopy(d)
            if change == "short":
                bad["frames"] = bad["frames"][:20] + [
                    {"step": 20, "elapsed_seconds": 2.0, "states": [{"valid": False}]}
                ]
                bad["termination"]["step"] = 20
            elif change == "missing_tick":
                bad["frames"][-1]["elapsed_seconds"] = 30.0
            elif change == "early_ego_loss":
                bad["frames"][300]["states"] = [{"valid": False}]
            else:
                bad["termination"]["budget_seconds"] = 3.0
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(bad)

    def test_staging_uses_frozen_source_and_rejects_another_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "artifact.json"
            d = document()
            artifact.write_text(json.dumps(d))
            source = root / "Town05.xodr"
            source.write_text(d["map"]["opendrive"])
            b = {
                "path": str(source),
                "sha256": sha256_file(source),
                "bytes": source.stat().st_size,
            }
            attempt = root / "attempt"
            attempt.mkdir()
            staged, context = _prepare_carla_capture_input(
                artifact, attempt, {"map_catalog": [b]}
            )
            self.assertEqual(context["source_catalog"], [b])
            self.assertEqual(staged.read_bytes(), artifact.read_bytes())
            bad = root / "bad"
            bad.mkdir()
            with self.assertRaises(Exception):
                _prepare_carla_capture_input(
                    artifact, bad, {"map_catalog": [{**b, "sha256": "0" * 64}]}
                )

    def test_ne_short_closed_capture_does_not_shorten_fixed_budget(self):
        d = document()
        actor = types.SimpleNamespace(is_alive=True)
        actor.set_target_velocity = lambda v: None
        actor.get_location = lambda: types.SimpleNamespace(x=0.0, y=0.0, z=0.0)
        actor.get_velocity = lambda: types.SimpleNamespace(x=0.0, y=0.0, z=0.0)
        actor.apply_control = lambda v: None
        actor.destroy = lambda: None
        world = types.SimpleNamespace(
            get_settings=lambda: types.SimpleNamespace(
                synchronous_mode=False, fixed_delta_seconds=None
            ),
            apply_settings=lambda v: None,
            get_weather=lambda: types.SimpleNamespace(cloudiness=0.0),
            set_weather=lambda v: None,
            get_blueprint_library=lambda: types.SimpleNamespace(find=lambda v: v),
            try_spawn_actor=lambda *a: actor,
            tick=lambda: None,
        )
        client = types.SimpleNamespace(
            set_timeout=lambda v: None,
            load_world=lambda v: world,
            get_server_version=lambda: "0.9.15",
        )
        fake = types.SimpleNamespace(
            Client=lambda *a: client,
            Location=lambda *a: a,
            Vector3D=lambda *a: a,
            Rotation=lambda **k: k,
            Transform=lambda *a: a,
            VehicleControl=lambda **k: k,
        )
        rows = []
        request = {
            "carla_endpoint": {
                "host": "127.0.0.1",
                "port": 2200,
                "timeout_seconds": 10,
            },
            "controller": {"parameters": {}},
            "rollout_seconds": 30.0,
            "trace_path": "unused",
        }
        with ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"carla": fake}))
            stack.enter_context(
                patch.object(carla_native_worker, "load", return_value=(d, None, None))
            )
            result = carla_native_worker.ne(
                "unused",
                request,
                lambda a, p: itertools.repeat(
                    {"throttle": 0.1, "brake": 0.0, "steering": 0.0}
                ),
                lambda *a: {},
                lambda p, v: rows.extend(v),
            )
        self.assertEqual(result["steps"], 300)
        self.assertEqual(result["simulated_seconds"], 30.0)
        self.assertEqual(len(rows), 301)

    def test_ne_first_row_precedes_first_physics_tick(self):
        d = document()
        d["frames"][0]["states"][0]["velocity_m_s"] = [1.0, 0.0, 0.0]
        position = types.SimpleNamespace(x=0.0, y=0.0, z=1.0)
        velocity = types.SimpleNamespace(x=1.0, y=0.0, z=0.0)
        actor = types.SimpleNamespace(
            is_alive=True,
            set_target_velocity=lambda v: None,
            get_location=lambda: position,
            get_velocity=lambda: velocity,
            apply_control=lambda v: None,
            destroy=lambda: None,
        )
        ticks = []

        def tick():
            ticks.append(1)
            position.x += 0.1

        world = types.SimpleNamespace(
            get_settings=lambda: types.SimpleNamespace(
                synchronous_mode=False, fixed_delta_seconds=None
            ),
            apply_settings=lambda v: None,
            get_weather=lambda: types.SimpleNamespace(cloudiness=0.0),
            set_weather=lambda v: None,
            get_blueprint_library=lambda: types.SimpleNamespace(find=lambda v: v),
            try_spawn_actor=lambda *a: actor,
            tick=tick,
        )
        client = types.SimpleNamespace(
            set_timeout=lambda v: None,
            load_world=lambda v: world,
            get_server_version=lambda: "0.9.15",
        )
        fake = types.SimpleNamespace(
            Client=lambda *a: client,
            Location=lambda *a: a,
            Vector3D=lambda *a: a,
            Rotation=lambda **k: k,
            Transform=lambda *a: a,
            VehicleControl=lambda **k: k,
        )
        rows = []
        request = {
            "carla_endpoint": {
                "host": "127.0.0.1",
                "port": 2200,
                "timeout_seconds": 10,
            },
            "controller": {"parameters": {}},
            "rollout_seconds": 30.0,
            "trace_path": "unused",
        }
        with ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"carla": fake}))
            stack.enter_context(
                patch.object(carla_native_worker, "load", return_value=(d, None, None))
            )
            carla_native_worker.ne(
                "unused",
                request,
                lambda a, p: itertools.repeat(
                    {"throttle": 0.1, "brake": 0.0, "steering": 0.0}
                ),
                lambda *a: {},
                lambda p, v: rows.extend(v),
            )
        self.assertEqual(rows[0]["objects"][0]["position"], [0.0, 0.0])
        self.assertEqual(len(ticks), 300)
        self.assertAlmostEqual(rows[-1]["objects"][0]["position"][0], 30.0)
