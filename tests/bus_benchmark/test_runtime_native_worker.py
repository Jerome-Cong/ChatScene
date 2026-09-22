from bus_benchmark.paths import PACKAGE_ROOT
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_file,
    write_json,
)


ROOT = Path(__file__).resolve().parents[2]
WORKER = PACKAGE_ROOT / "runtime_worker.py"
CARLA_PYTHON = ROOT / "chatscene" / "bin" / "python-frozen"
METADRIVE_PYTHON = Path(
    read_json(ROOT / "benchmark_configs/platforms/metadrive_runtime_draft.json")[
        "worker"
    ]["interpreter"]
)
CARLA_MAP = (
    ROOT
    / "safebench"
    / "scenario"
    / "scenario_data"
    / "scenic_data"
    / "maps"
    / "Town05.xodr"
)
CARLA_SERVER = (
    ROOT
    / "CARLA_0.9.13"
    / "CarlaUE4"
    / "Binaries"
    / "Linux"
    / "CarlaUE4-Linux-Shipping"
)


def _run_worker(interpreter, request, timeout=90):
    process = subprocess.run(
        [str(interpreter), str(WORKER)],
        input=canonical_json_bytes(request),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "MPLCONFIGDIR": "/tmp",
            "SDL_VIDEODRIVER": "dummy",
        },
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr.decode("utf-8", errors="replace")[-2000:])
    return json.loads(process.stdout.decode("utf-8"))


def _scenic_source(extra_lines=()):
    lines = [
        "Town = 'Town05'",
        "param map = localPath(f'../maps/{Town}.xodr')",
        "param carla_map = Town",
        "model scenic.simulators.carla.model",
        "ego = Car at -188@37,",
        "    with regionContainedIn None,",
        "    with blueprint 'vehicle.chevrolet.impala',",
        "    with length 5.33,",
        "    with width 2.10",
    ]
    lines.extend(extra_lines)
    return "\n".join(lines) + "\n"


class NativeRuntimeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.carla_stage_index = 0
        source = Path(
            "/home/shijie20/CodeSpace/mdsn/metadrive/metadrive/component/algorithm/BIG.py"
        ).resolve()
        self.registry = self.root / "token-registry.json"
        write_json(
            self.registry,
            {
                "schema_version": "0.1",
                "decision_status": "draft",
                "registry_id": "metadrive-pg-token-registry-v0.1",
                "artifact_type": "metadrive_pg_block_scene_v0.1",
                "metadrive_version": "0.4.3",
                "constructor_contract": {
                    "generation_type": "block_sequence",
                    "map_config_type": "block_sequence",
                    "config_value_type": "string",
                    "implicit_first_token": "I",
                    "class_list_allowed": False,
                    "path_input_allowed": False,
                },
                "source_files": [
                    {
                        "path": str(source),
                        "sha256": sha256_file(source),
                        "bytes": source.stat().st_size,
                    }
                ],
                "tokens": [
                    {
                        "token": "S",
                        "class_name": "Straight",
                        "map_parameter_constraints": {
                            "lane_num_allowed": [1, 2, 3, 4, 5]
                        },
                    }
                ],
            },
        )
        self.registry_binding = {
            "path": str(self.registry.resolve()),
            "sha256": sha256_file(self.registry),
            "bytes": self.registry.stat().st_size,
        }
        dependencies = read_json(
            ROOT / "benchmark_configs" / "platforms" / "metadrive_runtime_draft.json"
        )["runtime_dependencies"]
        self.metadrive_runtime_context = {
            "metadrive_module": dependencies["metadrive_module"],
            "source_tree": dependencies["source_tree"],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def _request(self, platform, artifact, mode="sv", **extra):
        interpreter = CARLA_PYTHON if platform == "carla" else METADRIVE_PYTHON
        map_context = None
        if platform == "carla":
            source_artifact_path = artifact.resolve()
            self.carla_stage_index += 1
            native_root = self.root / "carla-native-{:03d}".format(
                self.carla_stage_index
            )
            scenario_directory = native_root / "dynamic_scenario"
            map_directory = native_root / "maps"
            scenario_directory.mkdir(parents=True)
            map_directory.mkdir(parents=True)
            staged_artifact = scenario_directory / "finalized_artifact.scenic"
            shutil.copyfile(artifact, staged_artifact)
            source_catalog = []
            staged_catalog = []
            for source in (CARLA_MAP, CARLA_MAP.with_suffix(".snet")):
                if not source.is_file():
                    continue
                staged = map_directory / source.name
                shutil.copyfile(source, staged)
                source_catalog.append(
                    {
                        "path": str(source.resolve()),
                        "sha256": sha256_file(source),
                        "bytes": source.stat().st_size,
                    }
                )
                staged_catalog.append(
                    {
                        "path": str(staged.resolve()),
                        "sha256": sha256_file(staged),
                        "bytes": staged.stat().st_size,
                    }
                )
            artifact = staged_artifact
            map_context = {
                "declared_town": "Town05",
                "source_artifact": {
                    "path": str(source_artifact_path),
                    "sha256": sha256_file(source_artifact_path),
                    "bytes": source_artifact_path.stat().st_size,
                },
                "staged_artifact": {
                    "path": str(staged_artifact.resolve()),
                    "sha256": sha256_file(staged_artifact),
                    "bytes": staged_artifact.stat().st_size,
                },
                "source_catalog": source_catalog,
                "staged_catalog": staged_catalog,
            }
        request = {
            "schema_version": "0.1",
            "platform": platform,
            "artifact_path": str(artifact.resolve()),
            "artifact_sha256": sha256_file(artifact),
            "source_response_sha256": "1" * 64,
            "source_config_sha256": "2" * 64,
            "source_generation_manifest_sha256": "3" * 64,
            "source_implementation_bundle_sha256": "4" * 64,
            "generation_chain_verified": True,
            "runtime_environment": {"values": {}, "bindings": []},
            "runtime_executor": {
                "interpreter_path": str(interpreter.resolve()),
                "interpreter_sha256": sha256_file(interpreter.resolve()),
                "interpreter_bytes": interpreter.resolve().stat().st_size,
                "worker_path": str(WORKER.resolve()),
                "worker_sha256": sha256_file(WORKER.resolve()),
                "worker_bytes": WORKER.resolve().stat().st_size,
            },
            "mode": mode,
        }
        if platform == "carla":
            request["carla_endpoint"] = {
                "host": "127.0.0.1",
                "port": 2000,
                "timeout_seconds": 10.0,
            }
            request["carla_server_binary"] = {
                "path": str(CARLA_SERVER.resolve()),
                "sha256": sha256_file(CARLA_SERVER),
                "bytes": CARLA_SERVER.stat().st_size,
            }
            request["carla_map_context"] = map_context
        else:
            request["metadrive_token_registry"] = self.registry_binding
            request["metadrive_runtime_context"] = json.loads(
                json.dumps(self.metadrive_runtime_context)
            )
        request.update(extra)
        return request

    def _metadrive_artifact(self, *, overlap=False):
        def state(step, position):
            return {
                "step": step,
                "position_m": list(position),
                "heading_rad": 0.0,
                "speed_m_s": 0.0,
                "valid": True,
            }

        horizon = 301
        ego_positions = [[5.0, 0.0] for _ in range(horizon)]
        actor_position = [5.0, 0.0] if overlap else [12.0, 0.0]
        return {
            "schema_version": "0.1",
            "artifact_type": "metadrive_pg_block_scene_v0.1",
            "time_step_s": 0.1,
            "horizon_steps": horizon,
            "map": {
                "generation_type": "block_sequence",
                "block_sequence": "S",
                "map_seed": 0,
                "lane_num": 2,
                "lane_width_m": 3.5,
                "exit_length_m": 50.0,
                "token_registry_id": "metadrive-pg-token-registry-v0.1",
                "token_registry_sha256": self.registry_binding["sha256"],
            },
            "ego": {
                "actor_id": "ego",
                "actor_type": "vehicle",
                "vehicle_model": "xl",
                "length_m": 5.33,
                "width_m": 2.10,
                "reference_trajectory": [
                    state(step, position) for step, position in enumerate(ego_positions)
                ],
            },
            "actors": [
                {
                    "actor_id": "front-vehicle",
                    "actor_type": "vehicle",
                    "length_m": 4.5,
                    "width_m": 1.8,
                    "motion_mode": "replay_trajectory",
                    "trajectory": [
                        state(step, actor_position) for step in range(horizon)
                    ],
                }
            ],
        }

    @unittest.skipUnless(
        CARLA_PYTHON.is_file() and CARLA_MAP.is_file() and CARLA_SERVER.is_file(),
        "CARLA/Scenic fixture unavailable",
    )
    def test_carla_sv_enforces_blueprint_map_and_overlap_checks(self):
        valid = self.root / "valid.scenic"
        valid.write_text(_scenic_source(), encoding="utf-8")
        compiled = _run_worker(
            CARLA_PYTHON,
            self._request("carla", valid, mode="compile"),
        )
        self.assertTrue(compiled["ok"], compiled)
        self.assertIn("static_opendrive", compiled)
        self.assertNotIn("native_map", compiled)
        self.assertEqual(
            Path(compiled["static_opendrive"]["path"]).name,
            "Town05.xodr",
        )
        result = _run_worker(
            CARLA_PYTHON,
            self._request("carla", valid, seed=0, max_iterations=2000),
        )
        self.assertTrue(result["ok"], result)

        invalid_blueprint = self.root / "unknown.scenic"
        invalid_blueprint.write_text(
            _scenic_source().replace(
                "vehicle.chevrolet.impala", "vehicle.unknown.not_resolvable"
            ),
            encoding="utf-8",
        )
        result = _run_worker(
            CARLA_PYTHON,
            self._request("carla", invalid_blueprint, seed=0, max_iterations=2000),
        )
        self.assertFalse(result["ok"])
        self.assertIn("unresolvable blueprint", result["error"])

        overlap = self.root / "overlap.scenic"
        overlap.write_text(
            _scenic_source(
                (
                    "O = Car at -188@37,",
                    "    with regionContainedIn None,",
                    "    with allowCollisions True,",
                    "    with blueprint 'vehicle.chevrolet.impala'",
                )
            ).replace("    with width 2.10\n", "    with width 2.10,\n    with allowCollisions True\n"),
            encoding="utf-8",
        )
        result = _run_worker(
            CARLA_PYTHON,
            self._request("carla", overlap, seed=0, max_iterations=2000),
        )
        self.assertFalse(result["ok"])
        self.assertIn("initial physical overlap", result["error"])

        outside = self.root / "outside.scenic"
        outside.write_text(_scenic_source().replace("-188@37", "10000@10000"), encoding="utf-8")
        result = _run_worker(
            CARLA_PYTHON,
            self._request("carla", outside, seed=0, max_iterations=2000),
        )
        self.assertFalse(result["ok"])
        self.assertIn("outside the native map", result["error"])

    @unittest.skipUnless(METADRIVE_PYTHON.is_file(), "MetaDrive environment unavailable")
    def test_metadrive_sv_rejects_native_accepted_overlap_and_ne_uses_frozen_policy(self):
        document = self._metadrive_artifact()
        artifact = self.root / "metadrive.json"
        write_json(artifact, document)
        wrong_context_request = self._request(
            "metadrive", artifact, seed=0, max_iterations=2000
        )
        wrong_module = Path(
            "/home/shijie20/CodeSpace/mdsn/metadrive/metadrive/component/algorithm/BIG.py"
        )
        wrong_context_request["metadrive_runtime_context"]["metadrive_module"] = {
            "path": str(wrong_module.resolve()),
            "sha256": sha256_file(wrong_module),
            "bytes": wrong_module.stat().st_size,
        }
        wrong_context = _run_worker(METADRIVE_PYTHON, wrong_context_request)
        self.assertFalse(wrong_context["ok"])
        self.assertIn("outside the source tree", wrong_context["error"])

        result = _run_worker(
            METADRIVE_PYTHON,
            self._request("metadrive", artifact, seed=0, max_iterations=2000),
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["static_actor_count"], 2)
        self.assertEqual(result["actual_block_ids"], ["I", "S"])
        self.assertEqual(result["validation_repetition"], 0)
        self.assertEqual(result["sampling_semantics"], "deterministic_artifact_confirmation")
        self.assertEqual(result["map_seed"], 0)

        repeated = _run_worker(
            METADRIVE_PYTHON,
            self._request("metadrive", artifact, seed=4, max_iterations=2000),
        )
        self.assertTrue(repeated["ok"], repeated)
        self.assertEqual(repeated["validation_repetition"], 4)
        self.assertEqual(repeated["map_seed"], result["map_seed"])
        self.assertEqual(repeated["actual_block_ids"], result["actual_block_ids"])

        overlap_document = self._metadrive_artifact(overlap=True)
        overlap = self.root / "metadrive-overlap.json"
        write_json(overlap, overlap_document)
        result = _run_worker(
            METADRIVE_PYTHON,
            self._request("metadrive", overlap, seed=0, max_iterations=2000),
        )
        self.assertFalse(result["ok"])
        self.assertIn("initial physical overlap", result["error"])

        trace = self.root / "trace.jsonl"
        controller = {
            "schema_version": "0.1",
            "status": "frozen",
            "controller_id": "metadrive-fixed-query-blind-v0.1",
            "platform": "metadrive",
            "query_blind": True,
            "reads_query_metadata": False,
            "reads_evaluation_metadata": False,
            "ego_proxy": {
                "length_m": 5.33,
                "width_m": 2.10,
                "proxy_id": "xl-topdown-proxy",
            },
            "implementation": {
                "module": "bus_benchmark.runtime_worker",
                "class": "FrozenBusIDMPolicy",
                "source_path": str(WORKER.resolve()),
                "source_sha256": sha256_file(WORKER),
            },
            "parameters": {
                "target_speed_km_h": 30.0,
                "acceleration_factor": 0.7,
                "deceleration_factor": -4.0,
                "dt_seconds": 0.1,
            },
            "action_contract": {
                "lateral": "normalized steering [-1,1]",
                "longitudinal": (
                    "normalized throttle/brake [-1,1]; IDM factors are dimensionless "
                    "and not SI acceleration bounds"
                ),
                "braking_preserved": True,
            },
        }
        result = _run_worker(
            METADRIVE_PYTHON,
            self._request(
                "metadrive",
                artifact,
                mode="ne",
                seed=0,
                rollout_seconds=30.0,
                controller=controller,
                trace_path=str(trace.resolve()),
            ),
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["runtime_controller_class"], "FrozenBusIDMPolicy")
        rows = read_jsonl(trace)
        self.assertEqual(len(rows), result["steps"] + 1)
        actions = [row["ego_action"] for row in rows if row["ego_action"] is not None]
        self.assertEqual(len(actions), result["steps"])
        self.assertTrue(
            any(action["normalized_throttle_brake"] < 0 for action in actions)
        )


if __name__ == "__main__":
    unittest.main()
