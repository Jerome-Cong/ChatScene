from bus_benchmark.paths import PACKAGE_ROOT
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bus_benchmark.runtime as runtime_module
from bus_benchmark.cli import main as benchmark_main
from bus_benchmark.errors import ValidationError
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    write_json,
    write_jsonl,
)
from bus_benchmark.provenance import record_sha256
from bus_benchmark.generation import python_environment_control_paths
from bus_benchmark.runtime import (
    _carla_server_preflight,
    _prepare_carla_native_input,
    _run_carla_ne_stage,
    _validate_carla_server_binary,
    _validate_carla_server_session,
    aggregate_runtime,
    execute_runtime_record,
    metadrive_tracked_source_binding,
    runtime_tree_binding,
    validate_platform_runtime_config,
    validate_runtime_record_consistency,
)
from bus_benchmark.runtime_worker import (
    _carla_runtime_map_evidence,
    _metadrive_static_sanity,
    _strict_json_object_bytes,
)


class RuntimePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.artifact = self.root / "scene.json"
        write_json(self.artifact, {"scene": "fixture"})
        self.worker = self.root / "fake_worker.py"
        self.worker.write_text(
            "import json, pathlib, sys\n"
            "request=json.loads(sys.stdin.read())\n"
            "mode=request['mode']\n"
            "if mode == 'ne':\n"
            "    rows=[]\n"
            "    for step in range(301):\n"
            "        action=None if step == 0 else {'steering': 0.0, 'normalized_throttle_brake': -0.2}\n"
            "        rows.append({'step': step, 'simulated_time_seconds': step * 0.1, 'objects': [{'object_id': 'ego', 'is_ego': True, 'position': [step * 0.1, 0.0]}], 'ego_action': action})\n"
            "    pathlib.Path(request['trace_path']).write_text(''.join(json.dumps(row, sort_keys=True) + '\\n' for row in rows), encoding='utf-8')\n"
            "result={'ok': True, 'mode': mode}\n"
            "if mode == 'sv': result.update(seed=request['seed'], validation_repetition=request['seed'], sampling_semantics='deterministic_artifact_confirmation', map_seed=7, iterations=request['seed'] + 1)\n"
            "if mode == 'ne': result.update(steps=300, simulated_seconds=30.0, termination_reason='time_limit', runtime_controller_class=request['controller']['implementation']['class'])\n"
            "sys.stdout.write(json.dumps(result, sort_keys=True))\n",
            encoding="utf-8",
        )
        metadrive_source = Path(
            "/home/shijie20/CodeSpace/mdsn/metadrive/metadrive/component/algorithm/BIG.py"
        ).resolve()
        self.token_registry = self.root / "metadrive-token-registry.json"
        write_json(
            self.token_registry,
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
                        "path": str(metadrive_source),
                        "sha256": sha256_file(metadrive_source),
                        "bytes": metadrive_source.stat().st_size,
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
        self.registry_descriptor = {
            "path": str(self.token_registry.resolve()),
            "sha256": sha256_file(self.token_registry),
            "bytes": self.token_registry.stat().st_size,
        }
        checked_runtime_config = read_json(
            Path(
                "benchmark_configs/platforms/metadrive_runtime_draft.json"
            ).resolve()
        )
        runtime_dependencies = copy.deepcopy(
            checked_runtime_config["runtime_dependencies"]
        )
        environment_manifest = read_json(
            Path(runtime_dependencies["environment_manifest"]["path"])
        )
        environment_manifest["decision_status"] = "frozen"
        frozen_environment_path = self.root / "metadrive-runtime-environment.json"
        write_json(frozen_environment_path, environment_manifest)
        runtime_dependencies["environment_manifest"] = {
            "path": str(frozen_environment_path.resolve()),
            "sha256": sha256_file(frozen_environment_path),
            "bytes": frozen_environment_path.stat().st_size,
        }
        self.raw_runtime_dependencies = runtime_dependencies
        self.runtime_dependencies = {
            **copy.deepcopy(runtime_dependencies),
            "verification_status": "formal_verified",
        }
        self.metadrive_interpreter = Path(
            checked_runtime_config["worker"]["interpreter"]
        ).resolve()
        source = PACKAGE_ROOT / "runtime_worker.py"
        self.controller = {
            "schema_version": "0.1",
            "status": "frozen",
            "controller_id": "fixture-controller",
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
                "source_path": str(source),
                "source_sha256": sha256_file(source),
            },
            "parameters": {
                "target_speed_km_h": 30.0,
                "acceleration_factor": 0.7,
                "deceleration_factor": -4.0,
                "dt_seconds": 0.1,
            },
            "action_contract": {
                "lateral": "normalized steering",
                "longitudinal": (
                    "normalized throttle/brake [-1,1]; IDM factors are dimensionless "
                    "and not SI acceleration bounds"
                ),
                "braking_preserved": True,
            },
        }
        self.worker_config = {
            "interpreter": str(self.metadrive_interpreter),
            "interpreter_sha256": sha256_file(self.metadrive_interpreter),
            "interpreter_bytes": self.metadrive_interpreter.stat().st_size,
            "worker": str(self.worker),
            "worker_sha256": sha256_file(self.worker),
            "worker_bytes": self.worker.stat().st_size,
            "timeout_seconds": 10,
            "environment": {},
            "platform_context": {
                "metadrive_token_registry": self.registry_descriptor,
            },
            "runtime_dependencies": copy.deepcopy(self.runtime_dependencies),
        }
        self.generation_chain = {
            "method_id": "fixture-method",
            "platform": "metadrive",
            "record_count": 5,
            "manifest_sha256": "4" * 64,
            "implementation_bundle_sha256": "3" * 64,
            "verified": True,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def response(self, **overrides):
        value = {
            "run_id": "fixture-run",
            "method_id": "fixture-method",
            "platform": "metadrive",
            "query_id": "fixture-query",
            "intent_group_id": "fixture-intent",
            "statistical_intent_cluster_id": "fixture-intent",
            "surface_style": "precise",
            "repetition": 0,
            "expected_support": "supported",
            "config_sha256": "2" * 64,
            "implementation_bundle_sha256": "3" * 64,
            "disposition": "generate",
            "terminal_status": "complete",
            "artifact": {
                "path": str(self.artifact.resolve()),
                "sha256": sha256_file(self.artifact),
                "bytes": self.artifact.stat().st_size,
            },
        }
        value.update(overrides)
        return value

    def test_complete_budget_is_immutable_and_selects_lowest_sv_seed(self):
        output_root = self.root / "runtime"
        with patch(
            "bus_benchmark.runtime._revalidate_normalized_metadrive_runtime_dependencies",
            wraps=runtime_module._revalidate_normalized_metadrive_runtime_dependencies,
        ) as revalidate:
            first = execute_runtime_record(
                self.response(), output_root, self.worker_config, self.controller,
                self.generation_chain,
            )
        self.assertEqual(revalidate.call_count, 16)
        self.assertEqual(first["compile"]["status"], "passed")
        self.assertEqual([row["seed"] for row in first["sv_trials"]], list(range(5)))
        self.assertTrue(all(row["status"] == "passed" for row in first["sv_trials"]))
        self.assertEqual(first["selected_seed"], 0)
        self.assertEqual(first["ne"]["status"], "passed")
        self.assertEqual(first["ne"]["simulated_seconds"], 30.0)
        self.assertIsNotNone(first["ne"]["trace"])
        expected_context = {
            "metadrive_module": self.runtime_dependencies["metadrive_module"],
            "source_tree": self.runtime_dependencies["source_tree"],
        }
        for stage in [first["compile"], *first["sv_trials"], first["ne"]]:
            request = read_json(Path(stage["request"]["path"]))
            self.assertEqual(request["metadrive_runtime_context"], expected_context)

        with self.assertRaisesRegex(ValidationError, "already terminal"):
            execute_runtime_record(
                self.response(), output_root, self.worker_config, self.controller,
                self.generation_chain,
            )
        resumed = execute_runtime_record(
            self.response(),
            output_root,
            self.worker_config,
            self.controller,
            self.generation_chain,
            resume=True,
        )
        self.assertEqual(resumed, first)

    def test_runtime_rejects_duplicate_key_and_nonfinite_worker_json(self):
        with self.assertRaisesRegex(ValueError, "duplicate object key"):
            _strict_json_object_bytes(b'{"mode":"compile","mode":"ne"}')
        with self.assertRaisesRegex(ValueError, "non-JSON numeric constant"):
            _strict_json_object_bytes(b'{"value":NaN}')

        self.worker.write_text(
            "import sys\n"
            "sys.stdin.buffer.read()\n"
            "sys.stdout.write('{\"ok\":false,\"ok\":true,\"mode\":\"compile\"}')\n",
            encoding="utf-8",
        )
        self.worker_config["worker_sha256"] = sha256_file(self.worker)
        self.worker_config["worker_bytes"] = self.worker.stat().st_size
        record = execute_runtime_record(
            self.response(),
            self.root / "duplicate-worker-json",
            self.worker_config,
            self.controller,
            self.generation_chain,
        )
        self.assertEqual(record["compile"]["status"], "failed")
        self.assertIn("strict UTF-8 JSON", record["compile"]["error"])
        self.assertTrue(all(trial["status"] == "skipped" for trial in record["sv_trials"]))
        self.assertEqual(record["ne"]["status"], "skipped")

    def test_unsupported_response_is_recorded_but_excluded(self):
        result = execute_runtime_record(
            self.response(
                expected_support="unsupported",
                disposition="reject",
                artifact=None,
            ),
            self.root / "unsupported-runtime",
            self.worker_config,
            self.controller,
            self.generation_chain,
        )
        self.assertEqual(
            result["excluded_reason"], "unsupported_query_outside_runtime_metrics"
        )
        self.assertEqual(result["compile"]["status"], "skipped")
        self.assertTrue(all(row["status"] == "skipped" for row in result["sv_trials"]))
        self.assertEqual(result["ne"]["status"], "skipped")

    def test_artifact_hash_change_fails_before_native_execution(self):
        response = self.response()
        self.artifact.write_text('{"changed":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "hash changed"):
            execute_runtime_record(
                response,
                self.root / "changed-runtime",
                self.worker_config,
                self.controller,
                self.generation_chain,
            )

    def test_controller_source_binding_and_platform_are_enforced(self):
        wrong_platform = dict(self.controller, platform="carla")
        with self.assertRaisesRegex(ValidationError, "platform differs"):
            execute_runtime_record(
                self.response(),
                self.root / "wrong-platform",
                self.worker_config,
                wrong_platform,
                self.generation_chain,
            )
        changed = dict(self.controller)
        changed["implementation"] = dict(
            changed["implementation"], source_sha256="0" * 64
        )
        with self.assertRaisesRegex(ValidationError, "source binding changed"):
            execute_runtime_record(
                self.response(),
                self.root / "wrong-source",
                self.worker_config,
                changed,
                self.generation_chain,
            )

        overstated = copy.deepcopy(self.controller)
        overstated["action_contract"]["longitudinal"] = "signed acceleration [-1,1]"
        with self.assertRaisesRegex(ValidationError, "overstates"):
            execute_runtime_record(
                self.response(),
                self.root / "overstated-metadrive-action",
                self.worker_config,
                overstated,
                self.generation_chain,
            )

    def test_runtime_aggregation_remains_platform_labeled(self):
        supported = execute_runtime_record(
            self.response(),
            self.root / "aggregate-supported",
            self.worker_config,
            self.controller,
            self.generation_chain,
        )
        unsupported = execute_runtime_record(
            self.response(
                run_id="unsupported-run",
                expected_support="unsupported",
                disposition="reject",
                artifact=None,
            ),
            self.root / "aggregate-unsupported",
            self.worker_config,
            self.controller,
            self.generation_chain,
        )
        result = aggregate_runtime([supported, unsupported], allow_partial=True)
        self.assertEqual(result["metadrive"]["eligible_outputs"], 1)
        self.assertEqual(result["metadrive"]["excluded_outputs"], 1)
        self.assertEqual(result["metadrive"]["compile_success"], 1.0)
        self.assertEqual(result["metadrive"]["scene_validity"], 1.0)
        self.assertEqual(result["metadrive"]["native_executability"], 1.0)
        self.assertEqual(result["metadrive"]["iec_exec"]["status"], "unavailable")
        self.assertEqual(result["metadrive"]["iec_exec"]["coverage"], "none")

    def test_carla_native_staging_preserves_artifact_and_copies_bound_map(self):
        artifact = self.root / "captured.scenic"
        artifact.write_text(
            "Town = 'Town05'\n"
            "param map = localPath(f'../maps/{Town}.xodr')\n"
            "param carla_map = Town\n"
            "model scenic.simulators.carla.model\n",
            encoding="utf-8",
        )
        map_source = self.root / "Town05.xodr"
        map_source.write_bytes(b"frozen-map")
        map_binding = {
            "path": str(map_source.resolve()),
            "sha256": sha256_file(map_source),
            "bytes": map_source.stat().st_size,
        }
        attempt = self.root / "carla-attempt"
        attempt.mkdir()
        staged_artifact, map_context = _prepare_carla_native_input(
            artifact,
            attempt,
            {"map_catalog": [map_binding]},
        )
        staged_map = Path(map_context["staged_catalog"][0]["path"])
        self.assertEqual(sha256_file(staged_artifact), sha256_file(artifact))
        self.assertEqual(sha256_file(staged_map), map_binding["sha256"])
        self.assertEqual(map_context["declared_town"], "Town05")
        self.assertEqual(
            map_context["source_artifact"]["sha256"],
            map_context["staged_artifact"]["sha256"],
        )
        self.assertNotEqual(staged_artifact.stat().st_ino, artifact.stat().st_ino)
        self.assertNotEqual(staged_map.stat().st_ino, map_source.stat().st_ino)
        map_source.write_bytes(b"changed-live-map")
        self.assertEqual(sha256_file(staged_map), map_binding["sha256"])

    def test_carla_server_binary_and_proc_session_bind_the_shipping_elf(self):
        server_root = self.root / "server-distribution"
        server = (
            server_root
            / "CarlaUE4"
            / "Binaries"
            / "Linux"
            / "CarlaUE4-Linux-Shipping"
        )
        server.parent.mkdir(parents=True)
        server.write_bytes(Path("/bin/true").read_bytes())
        server.chmod(0o755)
        binding = {
            "path": str(server.resolve()),
            "sha256": sha256_file(server),
            "bytes": server.stat().st_size,
        }
        self.assertEqual(_validate_carla_server_binary(binding), binding)

        proc = self.root / "proc"
        (proc / "net").mkdir(parents=True)
        (proc / "net" / "tcp").write_text(
            "sl local_address rem_address st tx_queue tr retr uid timeout inode\n"
            "0: 0100007F:07D0 00000000:0000 0A 00000000:00000000 "
            "00:00000000 00000000 1000 0 98765\n",
            encoding="ascii",
        )
        (proc / "net" / "tcp6").write_text(
            "sl local_address rem_address st tx_queue tr retr uid timeout inode\n",
            encoding="ascii",
        )
        process = proc / "123"
        (process / "fd").mkdir(parents=True)
        (process / "fd" / "5").symlink_to("socket:[98765]")
        (process / "exe").symlink_to(server)
        stat_fields = ["S"] + ["0"] * 18 + ["456"]
        (process / "stat").write_text(
            "123 (Carla UE4) {}\n".format(" ".join(stat_fields)),
            encoding="ascii",
        )
        endpoint = {"host": "127.0.0.1", "port": 2000, "timeout_seconds": 10.0}
        preflight = _carla_server_preflight(
            endpoint, binding, proc_root=proc
        )
        self.assertEqual(preflight["status"], "verified")
        self.assertEqual(preflight["attestation"]["pid"], 123)
        self.assertEqual(preflight["attestation"]["process_starttime_ticks"], 456)
        self.assertEqual(preflight["attestation"]["socket_inode"], "98765")

        (proc / "net" / "tcp").write_text(
            "sl local_address rem_address st tx_queue tr retr uid timeout inode\n",
            encoding="ascii",
        )
        missing = _carla_server_preflight(endpoint, binding, proc_root=proc)
        self.assertEqual(missing["status"], "failed")
        self.assertEqual(missing["failure_reason"], "no_loopback_listening_socket")

        server.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shell_binding = {
            "path": str(server.resolve()),
            "sha256": sha256_file(server),
            "bytes": server.stat().st_size,
        }
        with self.assertRaisesRegex(ValidationError, "Shipping ELF"):
            _validate_carla_server_binary(shell_binding)

    def test_carla_ne_without_listener_fails_with_bound_session_evidence(self):
        server = (
            self.root
            / "server"
            / "CarlaUE4"
            / "Binaries"
            / "Linux"
            / "CarlaUE4-Linux-Shipping"
        )
        server.parent.mkdir(parents=True)
        server.write_bytes(Path("/bin/true").read_bytes())
        server.chmod(0o755)
        server_binding = {
            "path": str(server.resolve()),
            "sha256": sha256_file(server),
            "bytes": server.stat().st_size,
        }
        worker = self.root / "always-ok-worker.py"
        worker.write_text(
            "import json,sys\n"
            "json.loads(sys.stdin.read())\n"
            "sys.stdout.write(json.dumps({'ok': True, 'mode': 'ne'}))\n",
            encoding="utf-8",
        )
        config = {
            "interpreter": sys.executable,
            "interpreter_sha256": sha256_file(Path(sys.executable)),
            "interpreter_bytes": Path(sys.executable).stat().st_size,
            "worker": str(worker.resolve()),
            "worker_sha256": sha256_file(worker),
            "worker_bytes": worker.stat().st_size,
            "timeout_seconds": 10,
            "environment": {},
            "platform_context": {
                "carla_endpoint": {
                    "host": "127.0.0.1",
                    "port": 65432,
                    "timeout_seconds": 1.0,
                },
                "server_binary": server_binding,
            },
        }
        with patch(
            "bus_benchmark.runtime._revalidate_normalized_carla_runtime_dependencies"
        ):
            stage = _run_carla_ne_stage(
                {"mode": "ne"}, self.root / "no-listener-ne", config
            )
        self.assertEqual(stage["status"], "failed")
        session = read_json(Path(stage["server_session"]["path"]))
        self.assertEqual(session["verification_status"], "failed")
        self.assertEqual(
            session["verification_failure"],
            "preflight:no_loopback_listening_socket",
        )

    def test_carla_live_map_evidence_is_distinct_and_matches_staged_opendrive(self):
        staged = self.root / "Town05.xodr"
        staged.write_text("<OpenDRIVE/>\n", encoding="utf-8")

        class FakeMap:
            name = "/Game/Carla/Maps/Town05"

            @staticmethod
            def to_opendrive():
                return "<OpenDRIVE/>\n"

        class FakeWorld:
            @staticmethod
            def get_map():
                return FakeMap()

        class FakeClient:
            @staticmethod
            def get_client_version():
                return "0.9.13"

            @staticmethod
            def get_server_version():
                return "0.9.13"

        class FakeSimulator:
            client = FakeClient()
            world = FakeWorld()

        binding = {
            "path": str(staged.resolve()),
            "sha256": sha256_file(staged),
            "bytes": staged.stat().st_size,
        }
        result = _carla_runtime_map_evidence(
            FakeSimulator(),
            {
                "carla_map_context": {
                    "declared_town": "Town05",
                    "staged_catalog": [binding],
                }
            },
        )
        self.assertEqual(result["client_version"], "0.9.13")
        self.assertEqual(result["runtime_opendrive"]["sha256"], binding["sha256"])
        self.assertEqual(result["staged_opendrive"], binding)

        FakeMap.name = "/Game/Carla/Maps/Town06"
        with self.assertRaisesRegex(ValueError, "declared Town"):
            _carla_runtime_map_evidence(
                FakeSimulator(),
                {
                    "carla_map_context": {
                        "declared_town": "Town05",
                        "staged_catalog": [binding],
                    }
                },
            )

    def test_carla_server_session_requires_identical_pre_and_post_identity(self):
        server = (
            self.root
            / "bound-server"
            / "CarlaUE4"
            / "Binaries"
            / "Linux"
            / "CarlaUE4-Linux-Shipping"
        )
        server.parent.mkdir(parents=True)
        server.write_bytes(Path("/bin/true").read_bytes())
        server.chmod(0o755)
        binding = {
            "path": str(server.resolve()),
            "sha256": sha256_file(server),
            "bytes": server.stat().st_size,
        }
        endpoint = {"host": "127.0.0.1", "port": 2000, "timeout_seconds": 10.0}
        preflight = {
            "status": "verified",
            "failure_reason": None,
            "attestation": {
                "pid": 123,
                "process_starttime_ticks": 456,
                "listener_table": "tcp",
                "listener_local_address_hex": "0100007F",
                "listener_port": 2000,
                "socket_inode": "98765",
                "process_exe": binding,
            },
        }
        session = {
            "schema_version": "0.1",
            "verification_status": "verified",
            "verification_failure": None,
            "endpoint": endpoint,
            "server_binary": binding,
            "preflight": preflight,
            "postflight": copy.deepcopy(preflight),
        }
        path = self.root / "server-session.json"
        write_json(path, session)
        descriptor = {
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        self.assertEqual(
            _validate_carla_server_session(descriptor, endpoint, binding), session
        )

        session["postflight"]["attestation"]["process_starttime_ticks"] += 1
        write_json(path, session)
        descriptor.update(sha256=sha256_file(path), bytes=path.stat().st_size)
        with self.assertRaisesRegex(ValidationError, "self-inconsistent"):
            _validate_carla_server_session(descriptor, endpoint, binding)

    def test_carla_native_staging_rejects_unbound_or_ambiguous_town(self):
        artifact = self.root / "captured.scenic"
        artifact.write_text("Town = 'Town05'\nTown = 'Town06'\n", encoding="utf-8")
        map_source = self.root / "Town05.xodr"
        map_source.write_bytes(b"map")
        attempt = self.root / "ambiguous-attempt"
        attempt.mkdir()
        with self.assertRaisesRegex(ValidationError, "one supported literal Town"):
            _prepare_carla_native_input(
                artifact,
                attempt,
                {
                    "map_catalog": [
                        {
                            "path": str(map_source.resolve()),
                            "sha256": sha256_file(map_source),
                            "bytes": map_source.stat().st_size,
                        }
                    ]
                },
            )

    def test_runtime_tree_binding_rejects_symlinked_subdirectory(self):
        tree = self.root / "runtime-tree"
        target = self.root / "outside-tree"
        tree.mkdir()
        target.mkdir()
        (target / "source.py").write_text("value = 1\n", encoding="utf-8")
        (tree / "linked").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValidationError, "symlink"):
            runtime_tree_binding(tree, "scenic_editable_source")

    def test_checked_in_carla_runtime_config_is_bound_but_not_formal(self):
        config = read_json(
            Path("benchmark_configs/platforms/carla_runtime_draft.json").resolve()
        )
        normalized = validate_platform_runtime_config(
            config, "carla", allow_draft=True
        )
        self.assertEqual(
            normalized["runtime_dependencies"]["verification_status"],
            "draft_bound",
        )
        self.assertEqual(len(normalized["runtime_dependencies"]["map_catalog"]), 14)
        formal = copy.deepcopy(config)
        formal["status"] = "frozen"
        with self.assertRaisesRegex(ValidationError, "frozen environment manifest"):
            validate_platform_runtime_config(formal, "carla", allow_draft=False)

        frozen_environment = read_json(
            Path(formal["runtime_dependencies"]["environment_manifest"]["path"])
        )
        frozen_environment["decision_status"] = "frozen"
        frozen_environment_path = self.root / "frozen-runtime-environment.json"
        write_json(frozen_environment_path, frozen_environment)
        formal["runtime_dependencies"]["environment_manifest"] = {
            "path": str(frozen_environment_path.resolve()),
            "sha256": sha256_file(frozen_environment_path),
            "bytes": frozen_environment_path.stat().st_size,
        }
        with self.assertRaisesRegex(ValidationError, "runtime-tree closure"):
            validate_platform_runtime_config(formal, "carla", allow_draft=False)

        outside_server = (
            self.root
            / "outside-server"
            / "CarlaUE4"
            / "Binaries"
            / "Linux"
            / "CarlaUE4-Linux-Shipping"
        )
        outside_server.parent.mkdir(parents=True)
        outside_server.write_bytes(Path("/bin/true").read_bytes())
        outside_server.chmod(0o755)
        outside = copy.deepcopy(config)
        outside["server_binary"] = {
            "path": str(outside_server.resolve()),
            "sha256": sha256_file(outside_server),
            "bytes": outside_server.stat().st_size,
        }
        with self.assertRaisesRegex(ValidationError, "outside its distribution tree"):
            validate_platform_runtime_config(outside, "carla", allow_draft=True)

        symlinked_worker = copy.deepcopy(config)
        interpreter_link = self.root / "python-link"
        interpreter_link.symlink_to(Path(config["worker"]["interpreter"]))
        symlinked_worker["worker"]["interpreter"] = str(interpreter_link)
        with self.assertRaisesRegex(ValidationError, "non-symlink"):
            validate_platform_runtime_config(
                symlinked_worker, "carla", allow_draft=True
            )

    def test_carla_stage_revalidation_rehashes_draft_source_trees(self):
        config = read_json(
            Path("benchmark_configs/platforms/carla_runtime_draft.json").resolve()
        )
        normalized = validate_platform_runtime_config(
            config, "carla", allow_draft=True
        )

        scenic_root = self.root / "scenic-source"
        scenic_root.mkdir()
        scenic_source = scenic_root / "model.py"
        scenic_source.write_text("VALUE = 1\n", encoding="utf-8")

        server_root = self.root / "carla-server"
        server_binary = (
            server_root
            / "CarlaUE4"
            / "Binaries"
            / "Linux"
            / "CarlaUE4-Linux-Shipping"
        )
        server_binary.parent.mkdir(parents=True)
        server_binary.write_bytes(Path("/bin/true").read_bytes())
        server_binary.chmod(0o755)

        normalized["platform_context"]["server_binary"] = {
            "path": str(server_binary.resolve()),
            "sha256": sha256_file(server_binary),
            "bytes": server_binary.stat().st_size,
        }
        normalized["runtime_dependencies"]["source_trees"] = [
            runtime_tree_binding(server_root, "carla_server_distribution"),
            runtime_tree_binding(scenic_root, "scenic_editable_source"),
        ]
        runtime_module._revalidate_normalized_carla_runtime_dependencies(
            normalized
        )

        scenic_source.write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "bound tree bytes"):
            runtime_module._revalidate_normalized_carla_runtime_dependencies(
                normalized
            )

    def test_validated_runtime_executor_rejects_later_worker_or_interpreter_change(self):
        config = read_json(
            Path("benchmark_configs/platforms/carla_runtime_draft.json").resolve()
        )
        config = copy.deepcopy(config)
        environment_root = self.root / "python-environment"
        interpreter = environment_root / "bin" / "python-frozen"
        site_packages = environment_root / "lib" / "python3.8" / "site-packages"
        site_packages.mkdir(parents=True)
        interpreter.parent.mkdir(parents=True)
        interpreter.write_bytes(Path(config["worker"]["interpreter"]).read_bytes())
        interpreter.chmod(0o755)
        (environment_root / "pyvenv.cfg").write_text(
            "home = /usr/bin\n", encoding="utf-8"
        )
        worker = self.root / "runtime-worker-copy.py"
        worker.write_bytes(Path(config["worker"]["worker"]).read_bytes())
        config["worker"].update(
            {
                "interpreter": str(interpreter.resolve()),
                "interpreter_sha256": sha256_file(interpreter),
                "worker": str(worker.resolve()),
                "worker_sha256": sha256_file(worker),
            }
        )
        source_manifest_path = Path(
            config["runtime_dependencies"]["environment_manifest"]["path"]
        )
        environment_manifest = read_json(source_manifest_path)
        environment_manifest.update(
            {
                "executable": {
                    "path": str(interpreter.resolve()),
                    "sha256": sha256_file(interpreter),
                    "bytes": interpreter.stat().st_size,
                },
                "python_environment_root": str(environment_root.resolve()),
                "python_environment_control_files": [
                    {
                        "path": str(path),
                        "sha256": sha256_file(path),
                        "bytes": path.stat().st_size,
                    }
                    for path in python_environment_control_paths(environment_root)
                ],
            }
        )
        environment_manifest_path = self.root / "runtime-environment.json"
        write_json(environment_manifest_path, environment_manifest)
        config["runtime_dependencies"]["environment_manifest"] = {
            "path": str(environment_manifest_path.resolve()),
            "sha256": sha256_file(environment_manifest_path),
            "bytes": environment_manifest_path.stat().st_size,
        }
        normalized = validate_platform_runtime_config(
            config, "carla", allow_draft=True
        )
        self.assertEqual(normalized["worker_sha256"], sha256_file(worker))
        self.assertEqual(normalized["worker_bytes"], worker.stat().st_size)
        self.assertEqual(
            normalized["interpreter_sha256"], sha256_file(interpreter)
        )
        self.assertEqual(
            normalized["interpreter_bytes"], interpreter.stat().st_size
        )

        worker_original = worker.read_bytes()
        worker.write_bytes(b"changed after runtime validation\n")
        with self.assertRaisesRegex(ValidationError, "frozen expected bytes"):
            runtime_module._run_worker_stage(
                {}, self.root / "changed-worker-stage", normalized
            )
        worker.write_bytes(worker_original)

        interpreter.write_bytes(b"changed after runtime validation\n")
        interpreter.chmod(0o755)
        with self.assertRaisesRegex(ValidationError, "frozen expected bytes"):
            runtime_module._run_worker_stage(
                {}, self.root / "changed-interpreter-stage", normalized
            )

    def test_runtime_executor_rejects_transient_worker_change_during_stage(self):
        worker = self.root / "transient-runtime-worker.py"
        original = b"print('frozen runtime worker')\n"
        worker.write_bytes(original)
        interpreter = Path(sys.executable).resolve()
        worker_config = {
            "interpreter": str(interpreter),
            "interpreter_sha256": sha256_file(interpreter),
            "interpreter_bytes": interpreter.stat().st_size,
            "worker": str(worker.resolve()),
            "worker_sha256": sha256_file(worker),
            "worker_bytes": worker.stat().st_size,
            "timeout_seconds": 10,
            "environment": {},
        }

        def modify_and_restore(*args, **kwargs):
            worker.write_bytes(b"print('transient replacement')\n")
            worker.write_bytes(original)
            return subprocess.CompletedProcess(
                args[0], 0, stdout=b'{"ok":true}', stderr=b""
            )

        with patch("bus_benchmark.runtime.subprocess.run", side_effect=modify_and_restore):
            with self.assertRaisesRegex(ValidationError, "changed during stage"):
                runtime_module._run_worker_stage(
                    {}, self.root / "transient-worker-stage", worker_config
                )

    def test_checked_in_metadrive_runtime_is_source_and_environment_bound(self):
        config = read_json(
            Path(
                "benchmark_configs/platforms/metadrive_runtime_draft.json"
            ).resolve()
        )
        normalized = validate_platform_runtime_config(
            config, "metadrive", allow_draft=True
        )
        dependencies = normalized["runtime_dependencies"]
        self.assertEqual(dependencies["verification_status"], "draft_verified")
        self.assertEqual(
            dependencies["source_tree"]["revision"],
            "85e5dadc6c7436d324348f6e3d8f8e680c06b4db",
        )
        self.assertEqual(dependencies["source_tree"]["tracked_file_count"], 1371)
        self.assertEqual(
            dependencies["metadrive_module"]["path"],
            "/home/shijie20/CodeSpace/mdsn/metadrive/metadrive/__init__.py",
        )

        wrong_import = copy.deepcopy(config)
        wrong_module = Path(
            "/home/shijie20/CodeSpace/mdsn/metadrive/metadrive/component/algorithm/BIG.py"
        )
        wrong_import["runtime_dependencies"]["metadrive_module"] = {
            "path": str(wrong_module.resolve()),
            "sha256": sha256_file(wrong_module),
            "bytes": wrong_module.stat().st_size,
        }
        with self.assertRaisesRegex(ValidationError, "imported an unbound package"):
            validate_platform_runtime_config(
                wrong_import, "metadrive", allow_draft=True
            )

        incomplete_inventory = copy.deepcopy(
            read_json(
                Path(
                    config["runtime_dependencies"]["environment_manifest"]["path"]
                )
            )
        )
        incomplete_inventory["locked_dependencies"] = incomplete_inventory[
            "locked_dependencies"
        ][1:]
        incomplete_path = self.root / "incomplete-environment.json"
        write_json(incomplete_path, incomplete_inventory)
        wrong_inventory = copy.deepcopy(config)
        wrong_inventory["runtime_dependencies"]["environment_manifest"] = {
            "path": str(incomplete_path.resolve()),
            "sha256": sha256_file(incomplete_path),
            "bytes": incomplete_path.stat().st_size,
        }
        with self.assertRaisesRegex(ValidationError, "package inventory changed"):
            validate_platform_runtime_config(
                wrong_inventory, "metadrive", allow_draft=True
            )

    def test_metadrive_source_binding_rejects_untracked_shadow_content(self):
        real_run = subprocess.run

        def shadowed_status(argv, *args, **kwargs):
            if "status" in argv and "--untracked-files=all" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=b"?? metadrive/shadow.py\n",
                    stderr=b"",
                )
            return real_run(argv, *args, **kwargs)

        with patch("bus_benchmark.runtime.subprocess.run", shadowed_status):
            with self.assertRaisesRegex(ValidationError, "not audited"):
                metadrive_tracked_source_binding(
                    Path("/home/shijie20/CodeSpace/mdsn/metadrive")
                )

    def test_supported_generation_failure_stays_in_runtime_denominator(self):
        failed = execute_runtime_record(
            self.response(
                disposition="failure",
                terminal_status="failed",
                artifact=None,
            ),
            self.root / "aggregate-failure",
            self.worker_config,
            self.controller,
            self.generation_chain,
        )
        result = aggregate_runtime([failed], allow_partial=True)["metadrive"]
        self.assertEqual(result["eligible_outputs"], 1)
        self.assertEqual(result["excluded_outputs"], 0)
        self.assertEqual(result["compile_success"], 0.0)
        self.assertEqual(result["scene_validity"], 0.0)
        self.assertEqual(result["native_executability"], 0.0)

    def test_supported_failure_may_preserve_a_partial_artifact_hash(self):
        failed = execute_runtime_record(
            self.response(disposition="failure", terminal_status="failed"),
            self.root / "partial-failure",
            self.worker_config,
            self.controller,
            self.generation_chain,
        )
        self.assertEqual(failed["source_artifact_sha256"], sha256_file(self.artifact))
        self.assertEqual(failed["compile"]["status"], "skipped")

    def test_cross_stage_tampering_cannot_self_certify(self):
        record = execute_runtime_record(
            self.response(),
            self.root / "tamper-runtime",
            self.worker_config,
            self.controller,
            self.generation_chain,
        )
        duplicate_seed = dict(record)
        duplicate_seed["sv_trials"] = [dict(row) for row in record["sv_trials"]]
        duplicate_seed["sv_trials"][4]["seed"] = 3
        with self.assertRaisesRegex(ValidationError, "ordered unique seeds"):
            validate_runtime_record_consistency(duplicate_seed)

        wrong_selection = dict(record, selected_seed=1)
        with self.assertRaisesRegex(ValidationError, "lowest passing"):
            validate_runtime_record_consistency(wrong_selection)

        no_trace = dict(record)
        no_trace["ne"] = dict(record["ne"], trace=None)
        with self.assertRaisesRegex(ValidationError, "prove advancement"):
            validate_runtime_record_consistency(no_trace)

        forged_derived = copy.deepcopy(record)
        forged_derived["sv_trials"][0]["iterations"] = 1999
        with self.assertRaisesRegex(ValidationError, "derived fields"):
            validate_runtime_record_consistency(forged_derived)

        forged_sampling_semantics = copy.deepcopy(record)
        forged_sampling_semantics["sv_trials"][0]["result"][
            "sampling_semantics"
        ] = "independent_road_sample"
        forged_stdout = self.root / "forged-sv-stdout.json"
        write_json(
            forged_stdout,
            forged_sampling_semantics["sv_trials"][0]["result"],
        )
        forged_sampling_semantics["sv_trials"][0]["stdout"] = {
            "path": str(forged_stdout.resolve()),
            "sha256": sha256_file(forged_stdout),
            "bytes": forged_stdout.stat().st_size,
        }
        with self.assertRaisesRegex(ValidationError, "deterministic artifact"):
            validate_runtime_record_consistency(forged_sampling_semantics)

        forged_state = copy.deepcopy(record)
        forged_state["compile"]["timed_out"] = True
        forged_state["compile"]["exit_code"] = 9
        with self.assertRaisesRegex(ValidationError, "impossible"):
            validate_runtime_record_consistency(forged_state)

        forged_interpreter = copy.deepcopy(record)
        forged_interpreter["provenance"]["interpreter_path"] = "/nonexistent"
        with self.assertRaisesRegex(ValidationError, "interpreter binding"):
            validate_runtime_record_consistency(forged_interpreter)

        forged_import = copy.deepcopy(record)
        wrong_module = Path(
            "/home/shijie20/CodeSpace/mdsn/metadrive/metadrive/component/algorithm/BIG.py"
        )
        forged_import["provenance"]["worker_config"]["runtime_dependencies"][
            "metadrive_module"
        ] = {
            "path": str(wrong_module.resolve()),
            "sha256": sha256_file(wrong_module),
            "bytes": wrong_module.stat().st_size,
        }
        forged_import["provenance"]["worker_config_sha256"] = record_sha256(
            forged_import["provenance"]["worker_config"]
        )
        with self.assertRaisesRegex(ValidationError, "imported an unbound package"):
            validate_runtime_record_consistency(forged_import)

        short_trace = copy.deepcopy(record)
        trace_path = Path(short_trace["ne"]["trace"]["path"])
        trace_path.write_text(
            json.dumps(
                {
                    "step": 0,
                    "simulated_time_seconds": 0.0,
                    "objects": [
                        {"object_id": "ego", "is_ego": True, "position": [0.0, 0.0]}
                    ],
                    "ego_action": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        short_trace["ne"]["trace"] = {
            "path": str(trace_path),
            "sha256": sha256_file(trace_path),
            "bytes": trace_path.stat().st_size,
        }
        with self.assertRaisesRegex(ValidationError, "row count"):
            validate_runtime_record_consistency(short_trace)

    def test_aggregation_rejects_duplicates_mixed_methods_and_missing_formal_join(self):
        record = execute_runtime_record(
            self.response(), self.root / "aggregate-attacks", self.worker_config,
            self.controller, self.generation_chain,
        )
        with self.assertRaisesRegex(ValidationError, "unique"):
            aggregate_runtime([record, record], allow_partial=True)
        other = copy.deepcopy(record)
        other["run_id"] = "other-run"
        other["method_id"] = "other-method"
        with self.assertRaisesRegex(ValidationError, "mix methods"):
            aggregate_runtime([record, other], allow_partial=True)
        with self.assertRaisesRegex(ValidationError, "requires a frozen roster"):
            aggregate_runtime([record])

    def test_unfrozen_controller_class_is_rejected(self):
        changed = copy.deepcopy(self.controller)
        changed["implementation"]["class"] = "ConfiguredButNeverInstalled"
        with self.assertRaisesRegex(ValidationError, "class is not frozen"):
            execute_runtime_record(
                self.response(), self.root / "wrong-class", self.worker_config, changed,
                self.generation_chain,
            )

    def test_metadrive_static_sanity_rejects_overlap_and_off_map_placement(self):
        def trajectory(x):
            return {
                "step": 0,
                "position_m": [x, 0.0],
                "heading_rad": 0.0,
                "speed_m_s": 0.0,
                "valid": True,
            }

        document = {
            "map": {"block_sequence": "S"},
            "ego": {
                "length_m": 5.33,
                "width_m": 2.10,
                "reference_trajectory": [trajectory(0.0)],
            },
            "actors": [
                {
                    "actor_id": "other",
                    "actor_type": "vehicle",
                    "length_m": 5.0,
                    "width_m": 2.0,
                    "trajectory": [trajectory(0.0)],
                }
            ],
        }
        fake_map = type(
            "FakeMap",
            (),
            {
                "blocks": [type("Block", (), {"ID": token})() for token in ("I", "S")],
                "get_map_features": lambda self: {
                    "lane": {"polyline": [[-20.0, 0.0], [20.0, 0.0]]}
                },
            },
        )()
        environment = type("FakeEnvironment", (), {"current_map": fake_map})()
        with self.assertRaisesRegex(ValueError, "overlap"):
            _metadrive_static_sanity(document, environment)
        document["actors"][0]["trajectory"] = [trajectory(1000.0)]
        with self.assertRaisesRegex(ValueError, "outside"):
            _metadrive_static_sanity(document, environment)

    def test_runtime_cli_executes_and_aggregates_complete_development_grid(self):
        controller = copy.deepcopy(self.controller)
        controller["implementation"]["source_path"] = str(self.worker.resolve())
        controller["implementation"]["source_sha256"] = sha256_file(self.worker)
        controller_path = self.root / "controller.json"
        write_json(controller_path, controller)
        platform_config = {
            "schema_version": "0.1",
            "platform": "metadrive",
            "status": "draft",
            "sv_seeds": [0, 1, 2, 3, 4],
            "sv_max_iterations": 2000,
            "rollout_seconds": 30.0,
            "token_registry": self.registry_descriptor,
            "runtime_dependencies": copy.deepcopy(self.raw_runtime_dependencies),
            "worker": {
                "interpreter": str(self.metadrive_interpreter),
                "interpreter_sha256": sha256_file(self.metadrive_interpreter),
                "worker": str(self.worker.resolve()),
                "worker_sha256": sha256_file(self.worker),
                "timeout_seconds": 10,
                "environment": {},
            },
        }
        platform_path = self.root / "platform.json"
        write_json(platform_path, platform_config)
        roster = {
            "schema_version": "0.1",
            "decision_status": "confirmed",
            "method_id": "fixture-method",
            "platform": "metadrive",
            "library_path": str((self.root / "library.jsonl").resolve()),
            "library_sha256": "0" * 64,
            "query_count": 1,
            "expected_response_count": 5,
            "repetitions": [0, 1, 2, 3, 4],
            "queries": [
                {
                    "query_id": "fixture-query",
                    "intent_group_id": "fixture-intent",
                    "statistical_intent_cluster_id": "fixture-intent",
                    "surface_style": "precise",
                    "expected_support": "supported",
                }
            ],
        }
        roster_path = self.root / "roster.json"
        write_json(roster_path, roster)
        stream_descriptor = {
            "path": str(self.artifact.resolve()),
            "sha256": sha256_file(self.artifact),
            "bytes": self.artifact.stat().st_size,
        }
        envelope = {
            "stdout": stream_descriptor,
            "stderr": stream_descriptor,
            "exit_code": 0,
            "timed_out": False,
        }
        responses = []
        for repetition in range(5):
            response = self.response(run_id="fixture-run-{}".format(repetition))
            response.update(
                {
                    "schema_version": "0.1",
                    "repetition": repetition,
                    "request_sha256": "1" * 64,
                    "config_sha256": "2" * 64,
                    "raw_method_response": dict(
                        envelope,
                        envelope_sha256=sha256_bytes(canonical_json_bytes(envelope)),
                    ),
                    "provenance": {
                        "producer_id": "fixture-producer",
                        "producer_version": "0.1",
                        "created_at_utc": "2026-07-14T00:00:00+00:00",
                    },
                }
            )
            responses.append(response)
        responses_path = self.root / "responses.jsonl"
        write_jsonl(responses_path, responses)
        runtime_index = self.root / "runtime.jsonl"
        exit_code = benchmark_main(
            [
                "runtime-execute",
                "--responses",
                str(responses_path),
                "--roster",
                str(roster_path),
                "--platform-config",
                str(platform_path),
                "--controller-config",
                str(controller_path),
                "--output-dir",
                str(self.root / "runtime-runs"),
                "--output",
                str(runtime_index),
                "--development",
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(read_jsonl(runtime_index)), 5)
        aggregate_path = self.root / "runtime-aggregate.json"
        exit_code = benchmark_main(
            [
                "runtime-aggregate",
                "--records",
                str(runtime_index),
                "--responses",
                str(responses_path),
                "--roster",
                str(roster_path),
                "--platform-config",
                str(platform_path),
                "--controller-config",
                str(controller_path),
                "--development",
                "--output",
                str(aggregate_path),
            ]
        )
        self.assertEqual(exit_code, 0)
        result = read_json(aggregate_path)["metadrive"]
        self.assertEqual(result["eligible_outputs"], 5)
        self.assertEqual(result["scene_validity"], 1.0)
        self.assertEqual(result["native_executability"], 1.0)


if __name__ == "__main__":
    unittest.main()
