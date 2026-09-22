from bus_benchmark.paths import PACKAGE_ROOT
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from jsonschema import Draft7Validator

from bus_benchmark.adapters import AdapterOutcome
from bus_benchmark.adapters.chatscene import ChatSceneLegacyAdapter, CommandAdapter
from bus_benchmark.cli import main as benchmark_main
from bus_benchmark.errors import ValidationError
from bus_benchmark.generation import (
    GenerationRunner,
    build_generation_grid,
    finalize_carla_ego_artifact,
    finalize_metadrive_ego_artifact,
    ensure_immutable_jsonl,
    generation_harness_paths,
    implementation_bundle_sha256,
    python_environment_control_paths,
    probe_python_environment,
    running_python_environment_executable,
    run_generation,
    validate_generation_response_chain,
    validate_implementation_bundle,
    verify_carla_ego_finalization,
    verify_metadrive_ego_finalization,
)
from bus_benchmark.generation_preflight import generation_preflight
from bus_benchmark import metadrive_pg
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    write_json,
    write_jsonl,
)
from bus_benchmark.schema import supported_schema_paths
from bus_benchmark.roster import validate_records_against_roster
from bus_benchmark.schema import validate_schema_instance


SCENIC_SOURCE = """param map = localPath('../maps/Town05.xodr')
model scenic.simulators.carla.model
EGO_MODEL = "vehicle.lincoln.mkz_2017"
ego = Car at 0@0,
    with regionContainedIn None,
    with blueprint EGO_MODEL
other = Pedestrian at 8@1, with regionContainedIn None
"""

_PYTHON_ENVIRONMENT_CACHE = {}


def _metadrive_source(registry_sha256):
    return {
        "schema_version": "0.1",
        "artifact_type": "metadrive_pg_block_scene_v0.1",
        "time_step_s": 0.1,
        "horizon_steps": 2,
        "map": {
            "generation_type": "block_sequence",
            "block_sequence": "SXS",
            "map_seed": 7,
            "lane_num": 2,
            "lane_width_m": 3.5,
            "exit_length_m": 50.0,
            "token_registry_id": "metadrive-pg-token-registry-v0.1",
            "token_registry_sha256": registry_sha256,
        },
        "ego": {
            "actor_id": "ego",
            "actor_type": "vehicle",
            "vehicle_model": "s",
            "length_m": 4.5,
            "width_m": 1.8,
            "reference_trajectory": [
                {"step": 0, "position_m": [0.0, 0.0], "heading_rad": 0.0, "speed_m_s": 5.0, "valid": True},
                {"step": 1, "position_m": [0.5, 0.0], "heading_rad": 0.0, "speed_m_s": 5.0, "valid": True},
            ],
        },
        "actors": [
            {
                "actor_id": "pedestrian-1",
                "actor_type": "pedestrian",
                "length_m": 0.5,
                "width_m": 0.5,
                "motion_mode": "replay_trajectory",
                "trajectory": [
                    {"step": 0, "position_m": [8.0, 1.0], "heading_rad": -1.57, "speed_m_s": 1.0, "valid": True},
                    {"step": 1, "position_m": [8.0, 0.9], "heading_rad": -1.57, "speed_m_s": 1.0, "valid": True},
                ],
            }
        ],
        "semantic_regions": [
            {
                "region_id": "bus-stop-1",
                "region_type": "bus_stop_area",
                "polygon_m": [[5.0, -2.0], [10.0, -2.0], [10.0, -1.0], [5.0, -1.0]],
            }
        ],
    }


class _CountingAdapter:
    def __init__(self):
        self.calls = []

    def generate(self, query_text, workdir):
        self.calls.append(query_text)
        workdir.mkdir(parents=True, exist_ok=False)
        artifact = workdir / "scene.scenic"
        artifact.write_text(SCENIC_SOURCE + "# query: {}\n".format(query_text), encoding="utf-8")
        return AdapterOutcome(
            stdout=query_text.encode("utf-8"),
            stderr=b"",
            exit_code=0,
            timed_out=False,
            disposition="generate",
            artifact_path=artifact,
        )


class _RaisingAdapter:
    def generate(self, query_text, workdir):
        raise RuntimeError("intentional adapter failure")


class GenerationTests(unittest.TestCase):
    def test_checked_in_carla_config_matches_both_registered_rosters(self):
        root = Path(__file__).resolve().parents[2]
        library_by_scope = {
            "dev": root / "query_lib" / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl",
            "test": root / "query_lib" / "bus_ego_topdown_2d_query_library_v0_2.jsonl",
        }
        config = root / "benchmark_configs" / "methods" / "chatscene_carla_draft.json"
        with tempfile.TemporaryDirectory() as directory:
            for scope, library in library_by_scope.items():
                roster = read_json(
                    root
                    / "benchmark_artifacts"
                    / "drafts"
                    / "chatscene_carla_{}_roster_draft.json".format(scope)
                )
                runner = GenerationRunner(
                    library,
                    roster,
                    config,
                    Path(directory) / scope,
                    development=True,
                )
                self.assertEqual(runner.method_config["method_id"], roster["method_id"])
                self.assertEqual(runner.method_config["platform"], "carla")
                self.assertEqual(len(runner.jobs), roster["expected_response_count"])

    @staticmethod
    def _evidence(response):
        run_directory = Path(response["raw_method_response"]["stdout"]["path"]).parent
        return read_json(run_directory / "generation_evidence.json")

    def _inputs(
        self, root, query_count=1, expected_support="supported", platform="carla"
    ):
        records = []
        for index in range(query_count):
            records.append(
                {
                    "query_id": "query-{}".format(index),
                    "query_text": "surface query {}".format(index),
                    "intent_group_id": "intent-{}".format(index),
                    "surface_style": ("precise", "partial", "vague")[index % 3],
                    "expected_support": expected_support,
                }
            )
        library = root / "library.jsonl"
        write_jsonl(library, records)
        roster = {
            "schema_version": "0.1",
            "decision_status": "confirmed",
            "method_id": "method-a",
            "platform": platform,
            "library_path": str(library),
            "library_sha256": sha256_file(library),
            "query_count": len(records),
            "expected_response_count": len(records) * 5,
            "repetitions": [0, 1, 2, 3, 4],
            "queries": [
                {
                    "query_id": record["query_id"],
                    "intent_group_id": record["intent_group_id"],
                    "statistical_intent_cluster_id": "cluster-{}".format(index),
                    "surface_style": record["surface_style"],
                    "expected_support": record["expected_support"],
                }
                for index, record in enumerate(records)
            ],
        }
        return records, library, roster

    @staticmethod
    def _file_binding(path):
        path = Path(path).resolve()
        return {
            "path": str(path),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }

    def _registry(self, root, status="frozen"):
        source = (
            Path(__file__).resolve().parents[2]
            / "benchmark_configs"
            / "methods"
            / "metadrive_pg_token_registry_draft.json"
        )
        registry = read_json(source)
        registry["decision_status"] = status
        path = root / "token-registry.json"
        write_json(path, registry)
        return registry, path, self._file_binding(path)

    def _implementation_bundle(self, root, executable, status="frozen"):
        executable = Path(executable).resolve()
        executable_binding = self._file_binding(executable)
        cache_key = str(executable)
        if cache_key not in _PYTHON_ENVIRONMENT_CACHE:
            _PYTHON_ENVIRONMENT_CACHE[cache_key] = probe_python_environment(executable)
        observed_environment = _PYTHON_ENVIRONMENT_CACHE[cache_key]
        environment = {
            "schema_version": "0.1",
            "decision_status": status,
            "runtime_kind": "python",
            "executable": executable_binding,
            "runtime_version": observed_environment["runtime_version"],
            "locked_dependencies": observed_environment["locked_dependencies"],
        }
        environment_path = root / "method-environment.json"
        write_json(environment_path, environment)
        harness_executable = running_python_environment_executable()
        harness_cache_key = str(harness_executable)
        if harness_cache_key not in _PYTHON_ENVIRONMENT_CACHE:
            _PYTHON_ENVIRONMENT_CACHE[harness_cache_key] = probe_python_environment(
                harness_executable
            )
        harness_observed = _PYTHON_ENVIRONMENT_CACHE[harness_cache_key]
        harness_environment = {
            "schema_version": "0.1",
            "decision_status": status,
            "runtime_kind": "python",
            "executable": self._file_binding(harness_executable),
            "python_environment_root": str(Path(sys.prefix).resolve()),
            "python_environment_control_files": [
                self._file_binding(path)
                for path in python_environment_control_paths(Path(sys.prefix).resolve())
            ],
            "runtime_version": harness_observed["runtime_version"],
            "locked_dependencies": harness_observed["locked_dependencies"],
        }
        harness_environment_path = root / "harness-environment.json"
        write_json(harness_environment_path, harness_environment)
        implementation_files = {
            executable,
            harness_executable,
            *python_environment_control_paths(Path(sys.prefix).resolve()),
            *generation_harness_paths(),
        }
        bundle = {
            "status": status,
            "bundle_id": "test-method-implementation-v0.1",
            "bundle_sha256": "0" * 64,
            "environment_manifest": self._file_binding(environment_path),
            "harness_environment_manifest": self._file_binding(
                harness_environment_path
            ),
            "files": [
                self._file_binding(path)
                for path in sorted(implementation_files, key=str)
            ],
        }
        bundle["bundle_sha256"] = implementation_bundle_sha256(bundle)
        return bundle

    def _config(self, root, adapter=None, platform="carla"):
        registry_binding = None
        if adapter is None:
            if platform == "carla":
                artifact_path = "scene.scenic"
                code = (
                    "import pathlib,sys; "
                    "q=sys.stdin.read(); "
                    "pathlib.Path('scene.scenic').write_text({!r} + '# query: ' + q + '\\n', encoding='utf-8'); "
                    "sys.stdout.write(q)"
                ).format(SCENIC_SOURCE)
            else:
                artifact_path = "scene.json"
                _, _, registry_binding = self._registry(root)
                document = json.dumps(
                    _metadrive_source(registry_binding["sha256"]), ensure_ascii=False
                )
                code = (
                    "import pathlib,sys; "
                    "q=sys.stdin.read(); "
                    "pathlib.Path('scene.json').write_text({!r}, encoding='utf-8'); "
                    "sys.stdout.write(q)"
                ).format(document)
            adapter = {
                "type": "command",
                "argv": [str(Path(sys.executable).resolve()), "-c", code],
                "artifact_path": artifact_path,
                "timeout_seconds": 2,
                "output_protocol": "artifact_on_zero",
                "environment_passthrough": [],
            }
        else:
            adapter = dict(adapter)
            adapter["argv"] = list(adapter["argv"])
            adapter["argv"][0] = str(Path(adapter["argv"][0]).resolve())
        adapter.setdefault("environment_passthrough", [])
        adapter.setdefault("max_stdout_bytes", 1048576)
        adapter.setdefault("max_stderr_bytes", 1048576)
        adapter.setdefault("max_artifact_bytes", 16777216)
        if platform == "metadrive" and registry_binding is None:
            _, _, registry_binding = self._registry(root)
        implementation_bundle = self._implementation_bundle(root, adapter["argv"][0])
        config = {
            "schema_version": "0.1",
            "status": "frozen",
            "method_id": "method-a",
            "platform": platform,
            "query_input_fields": ["query_text"],
            "post_output_semantic_repair": False,
            "implementation_bundle": implementation_bundle,
            "adapter": adapter,
            "finalization": (
                {
                    "policy_id": "carla_ego_proxy_v0.1",
                    "ego_blueprint": "vehicle.chevrolet.impala",
                    "ego_length_m": 5.33,
                    "ego_width_m": 2.1,
                }
                if platform == "carla"
                else {
                    "policy_id": "metadrive_pg_ego_proxy_v0.1",
                    "artifact_type": "metadrive_pg_block_scene_v0.1",
                    "ego_vehicle_model": "xl",
                    "ego_length_m": 5.33,
                    "ego_width_m": 2.1,
                }
            ),
        }
        if registry_binding is not None:
            config["token_registry"] = registry_binding
        path = root / "method-{}.json".format(platform)
        write_json(path, config)
        return config, path

    @staticmethod
    def _run_generate_cli(library, roster, config, output, *extra):
        repository = Path(__file__).resolve().parents[2]
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "bus_benchmark",
                "generate",
                "--library",
                str(library),
                "--roster",
                str(roster),
                "--method-config",
                str(config),
                "--output-dir",
                str(output),
                *extra,
            ],
            cwd=str(repository),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )

    def test_grid_is_complete_and_adapter_receives_only_surface_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records, library, roster = self._inputs(root, query_count=2)
            _, config = self._config(root)
            adapter = _CountingAdapter()
            responses = run_generation(
                library,
                roster,
                config,
                root / "output",
                adapter=adapter,
                development=True,
            )

            self.assertEqual(len(responses), 10)
            self.assertEqual(
                sorted(adapter.calls),
                sorted([record["query_text"] for record in records for _ in range(5)]),
            )
            self.assertEqual(
                validate_records_against_roster(responses, roster),
                {"query_count": 2, "record_count": 10, "method_count": 1},
            )
            for response in responses:
                validate_schema_instance(response, "response_record")
                self.assertEqual(response["terminal_status"], "complete")
                self.assertEqual(response["disposition"], "generate")
                self.assertNotIn("unsupported_handling", response)
                final_text = Path(response["artifact"]["path"]).read_text(encoding="utf-8")
                self.assertIn('EGO_MODEL = "vehicle.chevrolet.impala"', final_text)
                self.assertIn("with length 5.33,", final_text)
                self.assertIn("with width 2.10,", final_text)
                evidence = self._evidence(response)
                self.assertNotEqual(
                    evidence["raw_artifact"]["sha256"], response["artifact"]["sha256"]
                )
                self.assertEqual(
                    evidence["finalization"]["final_sha256"], response["artifact"]["sha256"]
                )

    def test_raw_streams_and_envelope_are_persisted_even_when_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            _, config = self._config(root)
            response = run_generation(
                library,
                roster,
                config,
                root / "output",
                adapter=_CountingAdapter(),
                development=True,
            )[0]
            raw = response["raw_method_response"]
            self.assertEqual(Path(raw["stdout"]["path"]).read_bytes(), b"surface query 0")
            self.assertEqual(Path(raw["stderr"]["path"]).read_bytes(), b"")
            self.assertEqual(raw["stderr"]["bytes"], 0)
            envelope = {
                "stdout": raw["stdout"],
                "stderr": raw["stderr"],
                "exit_code": 0,
                "timed_out": False,
            }
            self.assertEqual(
                raw["envelope_sha256"], sha256_bytes(canonical_json_bytes(envelope))
            )
            persisted = read_json(Path(response["artifact"]["path"]).parent / "response.json")
            self.assertEqual(persisted, response)

    def test_existing_terminal_runs_are_verified_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            _, config = self._config(root)
            first_adapter = _CountingAdapter()
            first = run_generation(
                library,
                roster,
                config,
                root / "output",
                adapter=first_adapter,
                development=True,
            )
            response_path = Path(first[0]["artifact"]["path"]).parent / "response.json"
            before = response_path.read_bytes()
            before_mtime = response_path.stat().st_mtime_ns
            blocked_adapter = _CountingAdapter()
            with self.assertRaises(ValidationError):
                run_generation(
                    library,
                    roster,
                    config,
                    root / "output",
                    adapter=blocked_adapter,
                    development=True,
                )
            self.assertEqual(blocked_adapter.calls, [])
            second_adapter = _CountingAdapter()
            second = run_generation(
                library,
                roster,
                config,
                root / "output",
                adapter=second_adapter,
                development=True,
                resume=True,
            )
            self.assertEqual(second, first)
            self.assertEqual(second_adapter.calls, [])
            self.assertEqual(response_path.read_bytes(), before)
            self.assertEqual(response_path.stat().st_mtime_ns, before_mtime)

            evidence_path = response_path.parent / "generation_evidence.json"
            evidence = read_json(evidence_path)
            evidence["finalization"]["final_sha256"] = "0" * 64
            write_json(evidence_path, evidence)
            with self.assertRaises(ValidationError):
                run_generation(
                    library,
                    roster,
                    config,
                    root / "output",
                    adapter=_CountingAdapter(),
                    development=True,
                    resume=True,
                )

    def test_adapter_exception_is_an_atomic_failure_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            _, config = self._config(root)
            responses = run_generation(
                library,
                roster,
                config,
                root / "output",
                adapter=_RaisingAdapter(),
                development=True,
            )
            self.assertEqual(len(responses), 5)
            for response in responses:
                self.assertEqual(response["terminal_status"], "failed")
                self.assertEqual(response["disposition"], "failure")
                self.assertIsNone(response["artifact"])
                self.assertIsNone(self._evidence(response)["raw_artifact"])
                self.assertIsNone(response["raw_method_response"]["exit_code"])
                self.assertFalse(response["raw_method_response"]["timed_out"])
                self.assertNotIn("unsupported_handling", response)
                self.assertTrue(Path(response["raw_method_response"]["stdout"]["path"]).is_file())
                self.assertTrue(Path(response["raw_method_response"]["stderr"]["path"]).is_file())

    def test_timeout_is_recorded_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            _, config = self._config(
                root,
                adapter={
                    "type": "command",
                    "argv": [sys.executable, "-c", "import time; time.sleep(2)"],
                    "artifact_path": "scene.scenic",
                    "timeout_seconds": 0.05,
                    "output_protocol": "artifact_on_zero",
                    "environment_passthrough": [],
                },
            )
            started = time.monotonic()
            # This test isolates the adapter timeout contract. Formal live
            # environment probes have their own tests and are intentionally
            # excluded from the wall-clock assertion below.
            responses = run_generation(
                library,
                roster,
                config,
                root / "output",
                development=True,
            )
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(len(responses), 5)
            for response in responses:
                self.assertEqual(response["terminal_status"], "timeout")
                self.assertEqual(response["disposition"], "failure")
                self.assertTrue(response["raw_method_response"]["timed_out"])
                self.assertIsNone(response["raw_method_response"]["exit_code"])
                self.assertNotIn("unsupported_handling", response)

    def test_resume_rejects_symlinked_per_run_response_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            _, config = self._config(root)
            responses = run_generation(
                library,
                roster,
                config,
                root / "output",
                development=True,
            )
            run_directory = Path(
                responses[0]["raw_method_response"]["stdout"]["path"]
            ).parent
            for filename in ("response.json", "generation_evidence.json"):
                with self.subTest(filename=filename):
                    committed = run_directory / filename
                    original = committed.read_bytes()
                    outside = root / "outside-{}".format(filename)
                    outside.write_bytes(original)
                    committed.unlink()
                    committed.symlink_to(outside)
                    with self.assertRaisesRegex(
                        ValidationError, "regular non-symlink file"
                    ):
                        run_generation(
                            library,
                            roster,
                            config,
                            root / "output",
                            development=True,
                            resume=True,
                        )
                    committed.unlink()
                    committed.write_bytes(original)

    def test_unsupported_disposition_is_bound_to_raw_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root, expected_support="unsupported")
            raw_response = json.dumps(
                {"disposition": "reject", "message": "unsupported road context"},
                sort_keys=True,
            )
            _, config = self._config(
                root,
                adapter={
                    "type": "command",
                    "argv": [sys.executable, "-c", "import sys; sys.stdout.write({!r})".format(raw_response)],
                    "artifact_path": None,
                    "timeout_seconds": 2,
                    "output_protocol": "stdout_json_v0.1",
                    "environment_passthrough": [],
                },
            )
            responses = run_generation(library, roster, config, root / "output")
            for response in responses:
                self.assertEqual(response["terminal_status"], "complete")
                self.assertEqual(response["disposition"], "reject")
                self.assertIsNone(response["artifact"])
                self.assertIsNone(self._evidence(response)["raw_artifact"])
                self.assertEqual(
                    response["unsupported_handling"],
                    {
                        "response_stream": "stdout",
                        "response_sha256": response["raw_method_response"]["stdout"]["sha256"],
                    },
                )

    def test_stdout_disposition_protocol_rejects_duplicate_keys_and_nonfinite_json(self):
        for name, raw_response in (
            (
                "duplicate",
                '{"disposition":"reject","disposition":"generate"}',
            ),
            ("nonfinite", '{"disposition":"reject","value":NaN}'),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, library, roster = self._inputs(
                    root, expected_support="unsupported"
                )
                _, config = self._config(
                    root,
                    adapter={
                        "type": "command",
                        "argv": [
                            sys.executable,
                            "-c",
                            "import sys; sys.stdout.write({!r})".format(raw_response),
                        ],
                        "artifact_path": None,
                        "timeout_seconds": 2,
                        "output_protocol": "stdout_json_v0.1",
                        "environment_passthrough": [],
                    },
                )
                responses = run_generation(
                    library, roster, config, root / "output"
                )
                self.assertTrue(
                    all(response["terminal_status"] == "failed" for response in responses)
                )
                self.assertTrue(
                    all(response["disposition"] == "failure" for response in responses)
                )
                self.assertTrue(
                    all(
                        "strict UTF-8 JSON" in self._evidence(response)["failure_reason"]
                        for response in responses
                    )
                )

    def test_finalizer_allows_only_blueprint_and_dimensions(self):
        result = finalize_carla_ego_artifact(SCENIC_SOURCE.encode("utf-8"))
        final_text = result.final_bytes.decode("utf-8")
        verify_carla_ego_finalization(SCENIC_SOURCE, final_text)
        self.assertEqual(
            set(result.changes), {"ego_blueprint", "ego_length_m", "ego_width_m"}
        )
        self.assertIn(b"vehicle.chevrolet.impala", result.diff_bytes)
        tampered = final_text.replace("other = Pedestrian", "other = Car")
        with self.assertRaises(ValidationError):
            verify_carla_ego_finalization(SCENIC_SOURCE, tampered)
        non_ego_reference = SCENIC_SOURCE.replace(
            "other = Pedestrian at 8@1, with regionContainedIn None",
            "other = Car at 8@1, with blueprint EGO_MODEL",
        )
        with self.assertRaisesRegex(ValidationError, "only by the ego"):
            finalize_carla_ego_artifact(non_ego_reference.encode("utf-8"))
        quoted_hash_non_ego_reference = SCENIC_SOURCE.replace(
            "other = Pedestrian at 8@1, with regionContainedIn None",
            'other = Car at 8@1, with rolename "#", with blueprint EGO_MODEL',
        )
        with self.assertRaisesRegex(ValidationError, "only by the ego"):
            finalize_carla_ego_artifact(
                quoted_hash_non_ego_reference.encode("utf-8")
            )
        f_string_non_ego_reference = SCENIC_SOURCE.replace(
            "other = Pedestrian at 8@1, with regionContainedIn None",
            'other = Car at 8@1, with blueprint f"{EGO_MODEL}"',
        )
        with self.assertRaisesRegex(ValidationError, "only by the ego"):
            finalize_carla_ego_artifact(f_string_non_ego_reference.encode("utf-8"))

    def test_metadrive_finalizer_allows_only_native_pg_ego_proxy_leaves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _, binding = self._registry(root)
            raw_document = _metadrive_source(binding["sha256"])
            raw_text = json.dumps(raw_document, ensure_ascii=False, indent=2)
            result = finalize_metadrive_ego_artifact(
                raw_text.encode("utf-8"), registry, binding["sha256"]
            )
            final_text = result.final_bytes.decode("utf-8")
            verify_metadrive_ego_finalization(
                raw_text, final_text, registry, binding["sha256"]
            )
            final_document = json.loads(final_text)
            ego = final_document["ego"]
            self.assertEqual(ego["length_m"], 5.33)
            self.assertEqual(ego["width_m"], 2.1)
            self.assertEqual(ego["vehicle_model"], "xl")
            self.assertEqual(
                set(result.changes),
                {"ego_length_m", "ego_width_m", "ego_vehicle_model"},
            )
            patch = json.loads(result.diff_bytes)
            self.assertEqual(patch["format"], "json_leaf_patch_v0.1")
            self.assertEqual(len(patch["operations"]), 3)
            self.assertEqual(final_document["actors"], raw_document["actors"])
            self.assertEqual(
                final_document["semantic_regions"], raw_document["semantic_regions"]
            )

            tampered = json.loads(final_text)
            tampered["actors"][0]["trajectory"][0]["position_m"][0] = 999.0
            with self.assertRaisesRegex(ValidationError, "outside approved"):
                verify_metadrive_ego_finalization(
                    raw_text,
                    json.dumps(tampered, ensure_ascii=False),
                    registry,
                    binding["sha256"],
                )

            forbidden = _metadrive_source(binding["sha256"])
            forbidden["road_graph"] = {}
            with self.assertRaisesRegex(ValidationError, "unknown fields"):
                finalize_metadrive_ego_artifact(
                    canonical_json_bytes(forbidden), registry, binding["sha256"]
                )
            implicit = _metadrive_source(binding["sha256"])
            implicit["map"]["block_sequence"] = "IS"
            with self.assertRaisesRegex(ValidationError, "implicit token I"):
                finalize_metadrive_ego_artifact(
                    canonical_json_bytes(implicit), registry, binding["sha256"]
                )

    def test_metadrive_command_generation_is_immutable_and_replayable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root, platform="metadrive")
            method_config, config = self._config(root, platform="metadrive")
            registry = read_json(Path(method_config["token_registry"]["path"]))
            registry_sha256 = method_config["token_registry"]["sha256"]
            first = run_generation(library, roster, config, root / "output")
            self.assertEqual(len(first), 5)
            for response in first:
                self.assertEqual(response["platform"], "metadrive")
                self.assertEqual(response["terminal_status"], "complete")
                self.assertTrue(response["artifact"]["path"].endswith("artifact.final.json"))
                evidence = self._evidence(response)
                self.assertEqual(
                    evidence["finalization"]["policy_id"],
                    "metadrive_pg_ego_proxy_v0.1",
                )
                raw_text = Path(evidence["raw_artifact"]["path"]).read_text(
                    encoding="utf-8"
                )
                final_text = Path(response["artifact"]["path"]).read_text(
                    encoding="utf-8"
                )
                verify_metadrive_ego_finalization(
                    raw_text, final_text, registry, registry_sha256
                )

            replayed = run_generation(
                library, roster, config, root / "output", resume=True
            )
            self.assertEqual(replayed, first)
            artifact = Path(first[0]["artifact"]["path"])
            tampered = json.loads(artifact.read_text(encoding="utf-8"))
            tampered["semantic_regions"][0]["polygon_m"][0][0] = 77.0
            artifact.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "hash mismatch"):
                run_generation(
                    library, roster, config, root / "output", resume=True
                )

    def test_malformed_or_ambiguous_ego_artifact_fails_closed(self):
        ambiguous = SCENIC_SOURCE + "ego = Car at 2@0, with blueprint EGO_MODEL\n"
        with self.assertRaises(ValidationError):
            finalize_carla_ego_artifact(ambiguous.encode("utf-8"))

    def test_legacy_adapter_mutates_only_its_isolated_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            retrieve = source / "retrieve"
            output = (
                source
                / "safebench"
                / "scenario"
                / "scenario_data"
                / "scenic_data"
                / "dynamic_scenario"
            )
            retrieve.mkdir(parents=True)
            output.mkdir(parents=True)
            (source / "safebench" / "probe.py").write_text(
                "ORIGIN = __file__\n", encoding="utf-8"
            )
            original_query = retrieve / "scenario_descriptions.txt"
            original_query.write_text("original source input", encoding="utf-8")
            entrypoint = retrieve / "retrieve.py"
            entrypoint.write_text(
                """import sys
from pathlib import Path
import carla
import safebench.probe
root = Path(__file__).resolve().parents[1]
assert Path(sys.prefix) == Path('/run/chatscene-benchmark/python-env')
assert str(Path(carla.__file__)).startswith('/run/chatscene-benchmark/python-path/0/')
Path(safebench.probe.ORIGIN).resolve().relative_to(root)
try:
    Path({original_query!r}).read_bytes()
except (FileNotFoundError, PermissionError):
    pass
else:
    raise RuntimeError('original repository remained visible inside sandbox')
query = (root / 'retrieve/scenario_descriptions.txt').read_bytes()
target = root / 'safebench/scenario/scenario_data/scenic_data/dynamic_scenario/dynamic_0.scenic'
target.write_bytes({scene!r} + b'# query: ' + query + b'\\n')
print(query.decode('utf-8'), end='')
""".format(
                    original_query=str(original_query.resolve()),
                    scene=SCENIC_SOURCE.encode("utf-8"),
                ),
                encoding="utf-8",
            )
            adapter = ChatSceneLegacyAdapter(
                source_root=source,
                snapshot_paths=["retrieve", "safebench"],
                entrypoint="retrieve/retrieve.py",
                python_executable=str(Path(sys.prefix).resolve() / "bin" / "python-frozen"),
                python_environment_root=str(Path(sys.prefix).resolve()),
                python_path_entries=[
                    self._file_binding(
                        Path(__file__).resolve().parents[2]
                        / "CARLA_0.9.13/PythonAPI/carla/dist/carla-0.9.13-py3.8-linux-x86_64.egg"
                    )
                ],
                sandbox_executable="/home/shijie20/.local/bin/bwrap",
                argv=[],
                artifact_path="safebench/scenario/scenario_data/scenic_data/dynamic_scenario/dynamic_0.scenic",
                failure_artifact_path="safebench/scenario/scenario_data/scenic_data/dynamic_scenario/dynamic_0.txt",
                timeout_seconds=2,
                environment_passthrough=[],
                snapshot_bindings=[
                    self._file_binding(path)
                    for path in sorted(
                        (entrypoint, original_query, source / "safebench" / "probe.py"),
                        key=str,
                    )
                ],
            )
            workdir = root / "isolated-work"
            outcome = adapter.generate("one physical query", workdir)
            self.assertEqual(
                outcome.disposition,
                "generate",
                outcome.stderr.decode("utf-8", errors="replace") or outcome.error,
            )
            self.assertEqual(outcome.stdout, b"one physical query")
            self.assertEqual(original_query.read_text(encoding="utf-8"), "original source input")
            isolated_query = (
                workdir / "repository" / "retrieve" / "scenario_descriptions.txt"
            )
            self.assertEqual(isolated_query.read_bytes(), b"one physical query")
            self.assertFalse((output / "dynamic_0.scenic").exists())
            self.assertTrue(outcome.artifact_path.is_file())

            (retrieve / "linked.py").symlink_to(entrypoint)
            rejected = adapter.generate("another query", root / "symlink-work")
            self.assertIsNotNone(rejected.error)
            self.assertIn("symlink", rejected.error)

    def test_legacy_adapter_revalidates_python_controls_and_path_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fixture(name):
                fixture_root = root / name
                environment = fixture_root / "environment"
                executable = environment / "bin" / "python-frozen"
                site_packages = environment / "lib" / "python3.8" / "site-packages"
                source = fixture_root / "source"
                (source / "retrieve").mkdir(parents=True)
                site_packages.mkdir(parents=True)
                executable.parent.mkdir(parents=True)
                executable.write_bytes(Path("/usr/bin/python3.8").read_bytes())
                executable.chmod(0o755)
                (environment / "pyvenv.cfg").write_text(
                    "home = /usr/bin\n", encoding="utf-8"
                )
                (site_packages / "startup.pth").write_text(
                    "import startup_hook\n", encoding="utf-8"
                )
                startup_hook = site_packages / "startup_hook.py"
                startup_hook.write_text("VALUE = 1\n", encoding="utf-8")
                python_path_entry = fixture_root / "carla-fixture.egg"
                python_path_entry.write_bytes(b"bound egg bytes\n")
                entrypoint = source / "retrieve" / "retrieve.py"
                entrypoint.write_text(
                    "raise RuntimeError('must not run')\n", encoding="utf-8"
                )
                (source / "retrieve" / "scenario_descriptions.txt").write_text(
                    "fixture\n", encoding="utf-8"
                )
                adapter = ChatSceneLegacyAdapter(
                    source_root=source,
                    snapshot_paths=["retrieve"],
                    entrypoint="retrieve/retrieve.py",
                    python_executable=str(executable),
                    python_environment_root=str(environment),
                    python_path_entries=[self._file_binding(python_path_entry)],
                    sandbox_executable="/home/shijie20/.local/bin/bwrap",
                    argv=[],
                    artifact_path="output/dynamic_0.scenic",
                    failure_artifact_path="output/dynamic_0.txt",
                    timeout_seconds=2,
                    environment_passthrough=[],
                )
                return adapter, python_path_entry, startup_hook

            egg_adapter, egg, _ = fixture("egg")
            egg.write_bytes(b"changed after construction\n")
            egg_outcome = egg_adapter.generate("query", root / "egg-run")
            self.assertIsNotNone(egg_outcome.error)
            self.assertIn("Python path entry", egg_outcome.error)

            control_adapter, _, startup_hook = fixture("control")
            self.assertIn(
                startup_hook.resolve(),
                {
                    Path(binding["path"]).resolve()
                    for binding in control_adapter.python_environment_control_bindings
                },
            )
            startup_hook.write_text("VALUE = 2\n", encoding="utf-8")
            control_outcome = control_adapter.generate(
                "query", root / "control-run"
            )
            self.assertIsNotNone(control_outcome.error)
            self.assertIn("environment control", control_outcome.error)

            snapshot_adapter, _, _ = fixture("snapshot")
            snapshot_workdir = root / "snapshot-run"

            def transient_snapshot_change(*args, **kwargs):
                snapshot = (
                    snapshot_workdir
                    / "runtime-dependencies"
                    / "python-path"
                    / "0"
                    / "carla-fixture.egg"
                )
                original = snapshot.read_bytes()
                snapshot.chmod(0o644)
                snapshot.write_bytes(b"transient replacement\n")
                snapshot.write_bytes(original)
                snapshot.chmod(0o444)
                return AdapterOutcome(b"", b"", 0, False)

            with mock.patch(
                "bus_benchmark.adapters.chatscene._run_process",
                side_effect=transient_snapshot_change,
            ):
                snapshot_outcome = snapshot_adapter.generate(
                    "query", snapshot_workdir
                )
            self.assertIsNotNone(snapshot_outcome.error)
            self.assertIn("changed during launch", snapshot_outcome.error)

            directory_adapter, _, _ = fixture("directory")
            directory_workdir = root / "directory-run"

            def transient_directory_change(*args, **kwargs):
                marker = (
                    directory_workdir
                    / "runtime-dependencies"
                    / "transient-marker"
                )
                marker.write_bytes(b"temporary\n")
                marker.unlink()
                return AdapterOutcome(b"", b"", 0, False)

            with mock.patch(
                "bus_benchmark.adapters.chatscene._run_process",
                side_effect=transient_directory_change,
            ):
                directory_outcome = directory_adapter.generate(
                    "query", directory_workdir
                )
            self.assertIsNotNone(directory_outcome.error)
            self.assertIn("snapshot directory changed", directory_outcome.error)

    def test_formal_legacy_snapshot_attestation_rejects_toctou_and_extra_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            retrieve = source / "retrieve"
            output = source / "output"
            retrieve.mkdir(parents=True)
            output.mkdir()
            query_file = retrieve / "scenario_descriptions.txt"
            query_file.write_text("source query", encoding="utf-8")
            entrypoint = retrieve / "retrieve.py"
            entrypoint.write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
            bindings = [
                self._file_binding(path)
                for path in sorted((entrypoint, query_file), key=str)
            ]

            def adapter_for(workdir):
                return ChatSceneLegacyAdapter(
                    source_root=source,
                    snapshot_paths=["retrieve"],
                    entrypoint="retrieve/retrieve.py",
                    python_executable=str(Path(sys.prefix).resolve() / "bin" / "python-frozen"),
                    python_environment_root=str(Path(sys.prefix).resolve()),
                    sandbox_executable="/home/shijie20/.local/bin/bwrap",
                    argv=[],
                    artifact_path="output/dynamic_0.scenic",
                    failure_artifact_path="output/dynamic_0.txt",
                    timeout_seconds=2,
                    environment_passthrough=[],
                    snapshot_bindings=bindings,
                ).generate("query", workdir)

            from bus_benchmark.adapters import chatscene as chatscene_adapter

            original_copy = chatscene_adapter._copy_snapshot

            def tampered_copy(source_root, destination, paths):
                original_copy(source_root, destination, paths)
                (destination / "retrieve" / "retrieve.py").write_text(
                    "# transiently changed after copy\n", encoding="utf-8"
                )

            with mock.patch(
                "bus_benchmark.adapters.chatscene._copy_snapshot",
                side_effect=tampered_copy,
            ):
                outcome = adapter_for(root / "toctou-work")
            self.assertIsNotNone(outcome.error)
            self.assertIn("attestation hash mismatch", outcome.error)

            def extra_copy(source_root, destination, paths):
                original_copy(source_root, destination, paths)
                (destination / "retrieve" / "unbound.py").write_text(
                    "EXTRA = True\n", encoding="utf-8"
                )

            with mock.patch(
                "bus_benchmark.adapters.chatscene._copy_snapshot",
                side_effect=extra_copy,
            ):
                outcome = adapter_for(root / "extra-work")
            self.assertIsNotNone(outcome.error)
            self.assertIn("unbound extra file", outcome.error)

            def missing_copy(source_root, destination, paths):
                original_copy(source_root, destination, paths)
                (destination / "retrieve" / "scenario_descriptions.txt").unlink()

            with mock.patch(
                "bus_benchmark.adapters.chatscene._copy_snapshot",
                side_effect=missing_copy,
            ):
                outcome = adapter_for(root / "missing-work")
            self.assertIsNotNone(outcome.error)
            self.assertIn("missing a bundle-bound file", outcome.error)

    def test_generation_preflight_is_read_only_and_redacts_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records, library, roster = self._inputs(root)
            _, config = self._config(root)
            sentinel = "TOP_SECRET_SENTINEL_VALUE"
            query_sentinel = records[0]["query_text"]
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": sentinel}):
                with mock.patch(
                    "bus_benchmark.adapters.chatscene.adapter_from_method_config",
                    side_effect=AssertionError("preflight constructed an adapter"),
                ) as adapter_factory, mock.patch.object(
                    CommandAdapter,
                    "generate",
                    side_effect=AssertionError("preflight invoked generation adapter"),
                ) as generate:
                    report = generation_preflight(library, roster, config)

            adapter_factory.assert_not_called()
            generate.assert_not_called()
            self.assertFalse(report["repetition_consumed"])
            self.assertFalse(report["adapter_invoked"])
            self.assertFalse(report["query_text_disclosed"])
            self.assertFalse(report["secret_values_disclosed"])
            self.assertEqual(report["query_count"], 1)
            self.assertEqual(report["job_count"], 5)
            self.assertNotIn(sentinel, json.dumps(report, sort_keys=True))
            self.assertNotIn(query_sentinel, json.dumps(report, sort_keys=True))
            self.assertFalse((root / "runs").exists())

    def test_generation_preflight_runs_the_formal_bundle_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            _, config_path = self._config(root)
            config = read_json(config_path)
            bundle = config["implementation_bundle"]
            harness_path = next(
                path
                for path in generation_harness_paths()
                if str(path) not in {
                    str(Path(sys.executable).resolve()),
                    bundle["environment_manifest"]["path"],
                    bundle["harness_environment_manifest"]["path"],
                }
            )
            bundle["files"] = [
                binding
                for binding in bundle["files"]
                if Path(binding["path"]).resolve() != harness_path
            ]
            bundle["bundle_sha256"] = implementation_bundle_sha256(bundle)
            write_json(config_path, config)

            report = generation_preflight(library, roster, config_path)

            self.assertFalse(report["formal_ready"])
            formal_check = next(
                check
                for check in report["checks"]
                if check["check_id"] == "implementation_bundle_formal_contract"
            )
            self.assertEqual(formal_check["status"], "blocker")
            self.assertEqual(
                formal_check["reason_code"],
                "implementation_bundle_formal_contract_invalid",
            )

    def test_symlinked_actual_harness_launcher_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            _, config_path = self._config(root)
            method = read_json(config_path)
            symlink_launcher = Path(sys.prefix) / "bin" / "python"
            self.assertTrue(symlink_launcher.is_symlink())
            with mock.patch.object(sys, "executable", str(symlink_launcher)):
                with self.assertRaisesRegex(ValidationError, "regular non-symlink"):
                    validate_implementation_bundle(method, formal=True)
                report = generation_preflight(library, roster, config_path)
            identity = next(
                check
                for check in report["checks"]
                if check["check_id"] == "harness_running_executable_identity"
            )
            self.assertEqual(identity["status"], "blocker")
            self.assertEqual(
                identity["reason_code"], "running_python_executable_not_regular"
            )
            self.assertFalse(report["formal_ready"])

    def test_generation_preflight_cli_can_persist_the_read_only_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            _, config = self._config(root)
            roster_path = root / "roster.json"
            write_json(roster_path, roster)
            output = root / "preflight.json"

            with mock.patch(
                "bus_benchmark.adapters.chatscene.adapter_from_method_config",
                side_effect=AssertionError("preflight constructed an adapter"),
            ), mock.patch.object(
                CommandAdapter,
                "generate",
                side_effect=AssertionError("preflight invoked generation adapter"),
            ):
                exit_code = benchmark_main(
                    [
                        "generation-preflight",
                        "--library",
                        str(library),
                        "--roster",
                        str(roster_path),
                        "--method-config",
                        str(config),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(exit_code, 0)
            report = read_json(output)
            self.assertEqual(report["preflight_type"], "generation_preflight_v0.1")
            self.assertFalse(report["adapter_invoked"])
            self.assertFalse(report["repetition_consumed"])
            self.assertEqual(report["query_count"], 1)
            self.assertEqual(report["job_count"], 5)

    def test_legacy_bwrap_sandbox_kills_escaped_session_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            retrieve = source / "retrieve"
            output = (
                source
                / "safebench"
                / "scenario"
                / "scenario_data"
                / "scenic_data"
                / "dynamic_scenario"
            )
            retrieve.mkdir(parents=True)
            output.mkdir(parents=True)
            (source / "safebench" / "util").mkdir(parents=True)
            (retrieve / "scenario_descriptions.txt").write_text(
                "source query", encoding="utf-8"
            )
            (retrieve / "retrieve.py").write_text(
                """import subprocess, sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
code = "import time; from pathlib import Path; time.sleep(0.5); Path({!r}).write_text('escaped', encoding='utf-8')"
subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
target = root / 'safebench/scenario/scenario_data/scenic_data/dynamic_scenario/dynamic_0.scenic'
target.write_bytes({!r})
""".format(
                    "/run/chatscene-benchmark/repository/escaped-child-marker",
                    SCENIC_SOURCE.encode("utf-8"),
                ),
                encoding="utf-8",
            )
            adapter = ChatSceneLegacyAdapter(
                source_root=source,
                snapshot_paths=["retrieve", "safebench"],
                entrypoint="retrieve/retrieve.py",
                python_executable=str(Path(sys.prefix).resolve() / "bin" / "python-frozen"),
                python_environment_root=str(Path(sys.prefix).resolve()),
                sandbox_executable="/home/shijie20/.local/bin/bwrap",
                argv=[],
                artifact_path="safebench/scenario/scenario_data/scenic_data/dynamic_scenario/dynamic_0.scenic",
                failure_artifact_path="safebench/scenario/scenario_data/scenic_data/dynamic_scenario/dynamic_0.txt",
                timeout_seconds=2,
                environment_passthrough=[],
            )
            workdir = root / "sandbox-work"
            outcome = adapter.generate("query", workdir)
            self.assertEqual(outcome.disposition, "generate", outcome.error)
            time.sleep(0.8)
            self.assertFalse(
                (workdir / "repository" / "escaped-child-marker").exists()
            )

    def test_method_config_schema_enforces_query_only_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _ = self._config(root)
            schema = json.loads(
                (
                    Path(__file__).resolve().parents[2]
                    / "benchmark_configs"
                    / "schemas"
                    / "method_config.schema.json"
                ).read_text(encoding="utf-8")
            )
            validator = Draft7Validator(schema)
            self.assertEqual(list(validator.iter_errors(config)), [])
            config["query_input_fields"] = ["query_text", "query_id"]
            self.assertTrue(list(validator.iter_errors(config)))

            legacy = dict(config, query_input_fields=["query_text"])
            legacy["adapter"] = {
                "type": "chatscene_legacy",
                "source_root": str(root),
                "snapshot_paths": ["."],
                "entrypoint": "retrieve/retrieve.py",
                "python_executable": sys.executable,
                "sandbox_executable": "/home/shijie20/.local/bin/bwrap",
                "argv": [],
                "query_file": "retrieve/scenario_descriptions.txt",
                "artifact_path": "output/dynamic_0.scenic",
                "failure_artifact_path": "output/dynamic_0.txt",
                "timeout_seconds": 2,
                "environment_passthrough": [],
                "max_stdout_bytes": 1048576,
                "max_stderr_bytes": 1048576,
                "max_artifact_bytes": 16777216,
            }
            self.assertTrue(list(validator.iter_errors(legacy)))
            legacy["adapter"]["snapshot_paths"] = ["query_lib"]
            self.assertTrue(list(validator.iter_errors(legacy)))

            metadrive, _ = self._config(root, platform="metadrive")
            self.assertEqual(list(validator.iter_errors(metadrive)), [])
            metadrive_legacy = dict(metadrive)
            metadrive_legacy["adapter"] = dict(
                legacy["adapter"], snapshot_paths=["retrieve"]
            )
            self.assertTrue(list(validator.iter_errors(metadrive_legacy)))
            mismatched_finalization = dict(metadrive)
            mismatched_finalization["finalization"] = config["finalization"]
            self.assertTrue(list(validator.iter_errors(mismatched_finalization)))

    def test_cli_generates_indexes_and_resume_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root, query_count=2)
            roster_path = root / "roster.json"
            write_json(roster_path, roster)
            _, config_path = self._config(root)
            output = root / "generation"

            first = self._run_generate_cli(
                library, roster_path, config_path, output, "--development"
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            summary = json.loads(first.stdout)
            self.assertEqual(summary["record_count"], 10)
            self.assertTrue(summary["response_index_created"])
            self.assertTrue(summary["evidence_index_created"])
            response_path = output / "response.jsonl"
            evidence_path = output / "evidence.jsonl"
            responses = read_jsonl(response_path)
            evidence = read_jsonl(evidence_path)
            self.assertEqual(len(responses), 10)
            self.assertEqual(len(evidence), 10)
            self.assertEqual(
                [record["run_id"] for record in responses],
                [record["run_id"] for record in evidence],
            )
            response_before = response_path.read_bytes()
            evidence_before = evidence_path.read_bytes()
            response_mtime = response_path.stat().st_mtime_ns
            evidence_mtime = evidence_path.stat().st_mtime_ns

            blocked = self._run_generate_cli(
                library, roster_path, config_path, output, "--development"
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("--resume", blocked.stderr)
            resumed = self._run_generate_cli(
                library,
                roster_path,
                config_path,
                output,
                "--development",
                "--resume",
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            resumed_summary = json.loads(resumed.stdout)
            self.assertFalse(resumed_summary["response_index_created"])
            self.assertFalse(resumed_summary["evidence_index_created"])
            self.assertEqual(response_path.read_bytes(), response_before)
            self.assertEqual(evidence_path.read_bytes(), evidence_before)
            self.assertEqual(response_path.stat().st_mtime_ns, response_mtime)
            self.assertEqual(evidence_path.stat().st_mtime_ns, evidence_mtime)

    def test_formal_requires_freeze_and_frozen_inputs_while_development_allows_drafts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            confirmed_roster = root / "confirmed-roster.json"
            write_json(confirmed_roster, roster)
            frozen_config, config_path = self._config(root)

            draft_config = dict(frozen_config, status="draft")
            draft_config_path = root / "draft-method.json"
            write_json(draft_config_path, draft_config)
            with self.assertRaisesRegex(ValidationError, "frozen method config"):
                run_generation(
                    library,
                    roster,
                    draft_config_path,
                    root / "formal-draft-config",
                )

            draft_roster = dict(roster, decision_status="draft")
            draft_roster_path = root / "draft-roster.json"
            write_json(draft_roster_path, draft_roster)
            with self.assertRaisesRegex(ValidationError, "confirmed roster"):
                run_generation(
                    library,
                    draft_roster,
                    config_path,
                    root / "formal-draft-roster",
                )

            missing_freeze = self._run_generate_cli(
                library,
                confirmed_roster,
                config_path,
                root / "formal-without-freeze",
            )
            self.assertEqual(missing_freeze.returncode, 2)
            self.assertIn("--freeze-manifest", missing_freeze.stderr)

            development = self._run_generate_cli(
                library,
                draft_roster_path,
                draft_config_path,
                root / "development",
                "--development",
            )
            self.assertEqual(development.returncode, 0, development.stderr)
            self.assertEqual(len(read_jsonl(root / "development" / "response.jsonl")), 5)
            self.assertEqual(len(read_jsonl(root / "development" / "evidence.jsonl")), 5)

    def test_library_roster_hash_mismatch_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            roster["library_sha256"] = "0" * 64
            with self.assertRaises(ValidationError):
                build_generation_grid(library, roster)

    def test_formal_generation_forbids_custom_adapter_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            _, config = self._config(root)
            adapter = _CountingAdapter()
            with self.assertRaisesRegex(ValidationError, "forbids a custom adapter"):
                run_generation(
                    library, roster, config, root / "output", adapter=adapter
                )
            self.assertEqual(adapter.calls, [])

    def test_formal_python_environment_requires_exact_live_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, config_path = self._config(root)
            method = read_json(config_path)
            self.assertEqual(
                validate_implementation_bundle(method, formal=True),
                method["implementation_bundle"]["bundle_sha256"],
            )

            attacks = {
                "runtime_version": lambda value: value.update(
                    runtime_version="0.0.0-forged"
                ),
                "locked_dependencies": lambda value: value.update(
                    locked_dependencies=[]
                ),
            }
            for manifest_field in (
                "environment_manifest",
                "harness_environment_manifest",
            ):
                environment_path = Path(
                    method["implementation_bundle"][manifest_field]["path"]
                )
                original_environment = read_json(environment_path)
                for name, mutate in attacks.items():
                    with self.subTest(manifest=manifest_field, attack=name):
                        environment = json.loads(json.dumps(original_environment))
                        mutate(environment)
                        write_json(environment_path, environment)
                        attacked = json.loads(json.dumps(method))
                        attacked["implementation_bundle"][manifest_field] = (
                            self._file_binding(environment_path)
                        )
                        attacked["implementation_bundle"]["bundle_sha256"] = (
                            implementation_bundle_sha256(
                                attacked["implementation_bundle"]
                            )
                        )
                        with self.assertRaisesRegex(
                            ValidationError,
                            "runtime_version differs"
                            if name == "runtime_version"
                            else "locked_dependencies differ",
                        ):
                            validate_implementation_bundle(attacked, formal=True)
                        write_json(environment_path, original_environment)

    def test_formal_bundle_requires_every_generation_harness_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, config_path = self._config(root)
            method = read_json(config_path)
            required_paths = generation_harness_paths()
            repository_root = Path(__file__).resolve().parents[2]
            expected_paths = {
                path.resolve()
                for path in (PACKAGE_ROOT).rglob("*.py")
                if "__pycache__" not in path.parts
            }
            expected_paths.update(supported_schema_paths())
            self.assertEqual(set(required_paths), expected_paths)
            self.assertNotIn(
                (
                    repository_root
                    / "benchmark_configs"
                    / "schemas"
                    / "human_adjudication_packet.schema.json"
                ).resolve(),
                expected_paths,
            )
            relative_paths = {
                str(path.relative_to(PACKAGE_ROOT))
                for path in required_paths
            }
            self.assertTrue(
                {
                    "__init__.py",
                    "__main__.py",
                    "cli.py",
                    "generation.py",
                    "adapters/__init__.py",
                    "adapters/chatscene.py",
                    "roster.py",
                    "decisions.py",
                    "assets/schemas/method_config.schema.json",
                    "assets/schemas/response_record.schema.json",
                    "assets/schemas/generation_evidence.schema.json",
                    "assets/schemas/generation_run_manifest.schema.json",
                }
                <= relative_paths
            )
            for omitted in required_paths:
                with self.subTest(omitted=str(omitted)):
                    attacked = json.loads(json.dumps(method))
                    attacked_bundle = attacked["implementation_bundle"]
                    attacked_bundle["files"] = [
                        binding
                        for binding in attacked_bundle["files"]
                        if Path(binding["path"]).resolve() != omitted
                    ]
                    attacked_bundle["bundle_sha256"] = (
                        implementation_bundle_sha256(attacked_bundle)
                    )
                    with self.assertRaisesRegex(
                        ValidationError,
                        "omits generation harness dependency",
                    ):
                        validate_implementation_bundle(attacked, formal=True)

    def test_formal_bundle_rejects_retired_schema_but_allows_extra_method_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, config_path = self._config(root)
            method = read_json(config_path)

            def with_extra(path):
                attacked = json.loads(json.dumps(method))
                bundle = attacked["implementation_bundle"]
                bundle["files"].append(self._file_binding(path))
                bundle["files"].sort(
                    key=lambda binding: str(Path(binding["path"]).resolve())
                )
                bundle["bundle_sha256"] = implementation_bundle_sha256(bundle)
                return attacked

            retired_schema = (
                Path(__file__).resolve().parents[2]
                / "benchmark_configs"
                / "schemas"
                / "human_adjudication_packet.schema.json"
            )
            with self.assertRaisesRegex(
                ValidationError, "unsupported benchmark schema"
            ):
                validate_implementation_bundle(
                    with_extra(retired_schema), formal=True
                )

            schema_namespace = root / "schema-namespace"
            nested_namespace = schema_namespace / "nested"
            nested_namespace.mkdir(parents=True)
            nested_files = (
                nested_namespace / "ordinary-data.json",
                nested_namespace / retired_schema.name,
            )
            nested_files[0].write_text("{}\n", encoding="utf-8")
            nested_files[1].write_bytes(retired_schema.read_bytes())
            for nested_file in nested_files:
                with self.subTest(nested_schema=str(nested_file)), mock.patch(
                    "bus_benchmark.generation.SCHEMA_DIRECTORY",
                    schema_namespace,
                ), self.assertRaisesRegex(
                    ValidationError, "unsupported benchmark schema"
                ):
                    validate_implementation_bundle(
                        with_extra(nested_file), formal=True
                    )

            extra_source = root / "extra-method-source.py"
            extra_source.write_text("# ordinary method source\n", encoding="utf-8")
            extended = with_extra(extra_source)
            self.assertEqual(
                validate_implementation_bundle(extended, formal=True),
                extended["implementation_bundle"]["bundle_sha256"],
            )

    def test_adapter_executable_must_match_method_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, config_path = self._config(root)
            method = read_json(config_path)
            second_python = Path("/usr/bin/python3.8")
            self.assertTrue(second_python.is_file())
            self.assertNotEqual(second_python, running_python_environment_executable())
            method["adapter"]["argv"][0] = str(second_python)
            bundle = method["implementation_bundle"]
            bindings = {
                Path(binding["path"]).resolve(): binding
                for binding in bundle["files"]
            }
            bindings[second_python] = self._file_binding(second_python)
            bundle["files"] = [
                bindings[path] for path in sorted(bindings, key=str)
            ]
            bundle["bundle_sha256"] = implementation_bundle_sha256(bundle)
            with self.assertRaisesRegex(
                ValidationError, "differs from the method environment executable"
            ):
                validate_implementation_bundle(method, formal=True)

    def test_formal_command_native_environment_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, config_path = self._config(root)
            method = read_json(config_path)
            bundle = method["implementation_bundle"]
            environment_path = Path(bundle["environment_manifest"]["path"])
            environment = read_json(environment_path)
            environment["runtime_kind"] = "native_command"
            write_json(environment_path, environment)
            bundle["environment_manifest"] = self._file_binding(environment_path)
            bundle["bundle_sha256"] = implementation_bundle_sha256(bundle)

            with self.assertRaisesRegex(
                ValidationError,
                "formal command adapters require a probed python method environment",
            ):
                validate_implementation_bundle(method, formal=True)

    def test_formal_generation_rejects_unbound_host_environment_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, config_path = self._config(root)
            method = read_json(config_path)
            self.assertEqual(method["adapter"]["environment_passthrough"], [])
            validate_implementation_bundle(method, formal=True)

            for name in ("OPENAI_BASE_URL", "LD_LIBRARY_PATH"):
                with self.subTest(name=name):
                    attacked = json.loads(json.dumps(method))
                    attacked["adapter"]["environment_passthrough"] = [name]
                    with self.assertRaisesRegex(
                        ValidationError, "unbound host environment passthrough"
                    ):
                        validate_implementation_bundle(attacked, formal=True)

    def test_formal_harness_environment_must_match_running_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, config_path = self._config(root)
            method = read_json(config_path)
            bundle = method["implementation_bundle"]
            second_python = Path("/usr/bin/python3.8").resolve()
            self.assertNotEqual(second_python, running_python_environment_executable())
            observed = probe_python_environment(second_python)
            harness_environment_path = root / "second-harness-environment.json"
            write_json(
                harness_environment_path,
                {
                    "schema_version": "0.1",
                    "decision_status": "frozen",
                    "runtime_kind": "python",
                    "executable": self._file_binding(second_python),
                    "runtime_version": observed["runtime_version"],
                    "locked_dependencies": observed["locked_dependencies"],
                },
            )
            bundle["harness_environment_manifest"] = self._file_binding(
                harness_environment_path
            )
            bound_files = {
                Path(binding["path"]).resolve(): binding
                for binding in bundle["files"]
            }
            bound_files[second_python] = self._file_binding(second_python)
            bundle["files"] = [
                bound_files[path] for path in sorted(bound_files, key=str)
            ]
            bundle["bundle_sha256"] = implementation_bundle_sha256(bundle)

            with self.assertRaisesRegex(
                ValidationError,
                "harness environment executable differs from the running interpreter",
            ):
                validate_implementation_bundle(method, formal=True)

    def test_legacy_sandbox_executable_is_an_explicit_bound_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, config_path = self._config(root)
            method = read_json(config_path)
            source = root / "legacy-source"
            retrieve = source / "retrieve"
            retrieve.mkdir(parents=True)
            entrypoint = retrieve / "retrieve.py"
            entrypoint.write_text("print('fixture')\n", encoding="utf-8")
            (retrieve / "scenario_descriptions.txt").write_text(
                "mutable query", encoding="utf-8"
            )
            sandbox = root / "bwrap-fixture"
            sandbox.write_bytes(b"fixture sandbox executable\n")
            frozen_python = running_python_environment_executable()
            environment_root = Path(sys.prefix).resolve()
            method["adapter"] = {
                "type": "chatscene_legacy",
                "source_root": str(source),
                "snapshot_paths": ["retrieve"],
                "entrypoint": "retrieve/retrieve.py",
                "python_executable": str(frozen_python),
                "python_environment_root": str(environment_root),
                "python_path_entries": [self._file_binding(entrypoint)],
                "sandbox_executable": str(sandbox.resolve()),
                "argv": [],
                "query_file": "retrieve/scenario_descriptions.txt",
                "artifact_path": "output/dynamic_0.scenic",
                "failure_artifact_path": "output/dynamic_0.txt",
                "timeout_seconds": 2,
                "environment_passthrough": [],
                "max_stdout_bytes": 1048576,
                "max_stderr_bytes": 1048576,
                "max_artifact_bytes": 16777216,
            }
            bundle = method["implementation_bundle"]
            environment_path = Path(bundle["environment_manifest"]["path"])
            method_environment = read_json(environment_path)
            method_environment.update(
                {
                    "executable": self._file_binding(frozen_python),
                    "python_environment_root": str(environment_root),
                    "python_environment_control_files": [
                        self._file_binding(path)
                        for path in python_environment_control_paths(environment_root)
                    ],
                    **probe_python_environment(frozen_python),
                }
            )
            write_json(environment_path, method_environment)
            bundle["environment_manifest"] = self._file_binding(environment_path)
            bound_paths = {
                Path(binding["path"]).resolve(): binding
                for binding in bundle["files"]
            }
            for path in (
                entrypoint,
                retrieve / "scenario_descriptions.txt",
                sandbox,
                frozen_python,
                *python_environment_control_paths(environment_root),
            ):
                bound_paths[path.resolve()] = self._file_binding(path)
            bundle["files"] = [
                bound_paths[path] for path in sorted(bound_paths, key=str)
            ]
            bundle["bundle_sha256"] = implementation_bundle_sha256(bundle)
            validate_implementation_bundle(method, formal=False)
            with self.assertRaisesRegex(ValidationError, "runtime-tree closure"):
                validate_implementation_bundle(method, formal=True)

            from bus_benchmark.generation import _legacy_snapshot_bindings

            query_path = retrieve / "scenario_descriptions.txt"
            query_path.unlink()
            with self.assertRaisesRegex(
                ValidationError, "frozen legacy snapshot source files are missing"
            ):
                _legacy_snapshot_bindings(method, formal=True)
            query_path.write_text("mutable query", encoding="utf-8")

            native_environment = read_json(
                Path(bundle["environment_manifest"]["path"])
            )
            original_environment = json.loads(json.dumps(native_environment))
            native_environment["runtime_kind"] = "native_command"
            write_json(environment_path, native_environment)
            native_kind = json.loads(json.dumps(method))
            native_kind["implementation_bundle"]["environment_manifest"] = (
                self._file_binding(environment_path)
            )
            native_kind["implementation_bundle"]["bundle_sha256"] = (
                implementation_bundle_sha256(native_kind["implementation_bundle"])
            )
            with self.assertRaisesRegex(
                ValidationError, "runtime_kind must be python"
            ):
                validate_implementation_bundle(native_kind, formal=False)
            write_json(environment_path, original_environment)

            missing_binding = json.loads(json.dumps(method))
            missing_bundle = missing_binding["implementation_bundle"]
            missing_bundle["files"] = [
                binding
                for binding in missing_bundle["files"]
                if Path(binding["path"]).resolve() != sandbox.resolve()
            ]
            missing_bundle["bundle_sha256"] = implementation_bundle_sha256(
                missing_bundle
            )
            with self.assertRaisesRegex(ValidationError, "sandbox_executable"):
                validate_implementation_bundle(missing_binding, formal=False)

            relative = json.loads(json.dumps(method))
            relative["adapter"]["sandbox_executable"] = "bwrap-fixture"
            with self.assertRaisesRegex(ValidationError, "sandbox_executable"):
                validate_implementation_bundle(relative, formal=False)

            sandbox_link = root / "bwrap-link"
            sandbox_link.symlink_to(sandbox)
            symlinked = json.loads(json.dumps(method))
            symlinked["adapter"]["sandbox_executable"] = str(sandbox_link)
            with self.assertRaisesRegex(ValidationError, "sandbox_executable"):
                validate_implementation_bundle(symlinked, formal=False)

            nonexistent = json.loads(json.dumps(method))
            nonexistent["adapter"]["sandbox_executable"] = str(
                root / "missing-bwrap"
            )
            with self.assertRaisesRegex(ValidationError, "sandbox_executable"):
                validate_implementation_bundle(nonexistent, formal=False)

    def test_generation_chain_manifest_replays_every_committed_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            _, config = self._config(root)
            output = root / "output"
            responses = run_generation(library, roster, config, output)
            evidence = [self._evidence(response) for response in responses]
            ensure_immutable_jsonl(output / "response.jsonl", responses)
            ensure_immutable_jsonl(output / "evidence.jsonl", evidence)
            manifest = read_json(output / "generation_run_manifest.json")
            schema_root = (
                Path(__file__).resolve().parents[2] / "benchmark_configs" / "schemas"
            )
            manifest_schema = read_json(
                schema_root / "generation_run_manifest.schema.json"
            )
            evidence_schema = read_json(
                schema_root / "generation_evidence.schema.json"
            )
            self.assertEqual(
                list(Draft7Validator(manifest_schema).iter_errors(manifest)), []
            )
            for evidence_record in evidence:
                self.assertEqual(
                    list(
                        Draft7Validator(evidence_schema).iter_errors(evidence_record)
                    ),
                    [],
                )
            result = validate_generation_response_chain(
                library,
                roster,
                config,
                output,
                responses=responses,
                evidence=evidence,
                manifest=manifest,
            )
            self.assertEqual(result["record_count"], 5)
            self.assertEqual(
                result["manifest_sha256"], manifest["manifest_sha256"]
            )
            forged = dict(manifest, record_count=4)
            with self.assertRaisesRegex(ValidationError, "differs from the committed"):
                validate_generation_response_chain(
                    library, roster, config, output, manifest=forged
                )

            environment = Path(
                read_json(config)["implementation_bundle"]["environment_manifest"][
                    "path"
                ]
            )
            environment.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "frozen live bytes"):
                validate_generation_response_chain(library, roster, config, output)

    def test_subprocess_capture_is_bounded_and_environment_is_allowlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, library, roster = self._inputs(root)
            _, config = self._config(
                root,
                adapter={
                    "type": "command",
                    "argv": [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.write('x' * 100000)",
                    ],
                    "artifact_path": None,
                    "timeout_seconds": 2,
                    "output_protocol": "artifact_on_zero",
                    "environment_passthrough": [],
                    "max_stdout_bytes": 32,
                    "max_stderr_bytes": 32,
                    "max_artifact_bytes": 1024,
                },
            )
            responses = run_generation(library, roster, config, root / "output")
            for response in responses:
                self.assertEqual(response["terminal_status"], "failed")
                self.assertEqual(response["raw_method_response"]["stdout"]["bytes"], 32)
                self.assertIn("byte limit", self._evidence(response)["failure_reason"])

            method = read_json(config)
            method["adapter"]["environment_passthrough"] = ["HOME"]
            invalid = root / "invalid-environment.json"
            write_json(invalid, method)
            with self.assertRaises(ValidationError):
                run_generation(library, roster, invalid, root / "invalid-output")

    def test_metadrive_contract_rejects_unexecutable_or_topology_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _, binding = self._registry(root)
            artifact = _metadrive_source(binding["sha256"])
            registry_schema = read_json(
                Path(__file__).resolve().parents[2]
                / "benchmark_configs"
                / "schemas"
                / "metadrive_pg_token_registry.schema.json"
            )
            artifact_schema = read_json(
                Path(__file__).resolve().parents[2]
                / "benchmark_configs"
                / "schemas"
                / "metadrive_pg_block_scene.schema.json"
            )
            self.assertEqual(
                list(Draft7Validator(registry_schema).iter_errors(registry)), []
            )
            self.assertEqual(
                list(Draft7Validator(artifact_schema).iter_errors(artifact)), []
            )
            subset_registry = json.loads(json.dumps(registry))
            subset_registry["tokens"] = subset_registry["tokens"][:1]
            metadrive_pg.validate_token_registry(
                subset_registry, formal=False, verify_files=False
            )
            with self.assertRaisesRegex(
                metadrive_pg.MetaDrivePGError, "complete audited"
            ):
                metadrive_pg.validate_token_registry(
                    subset_registry, formal=True, verify_files=False
                )
            artifact["time_step_s"] = 0.2
            with self.assertRaisesRegex(ValidationError, "exactly 0.1"):
                finalize_metadrive_ego_artifact(
                    canonical_json_bytes(artifact), registry, binding["sha256"]
                )

            artifact = _metadrive_source(binding["sha256"])
            for state in artifact["actors"][0]["trajectory"]:
                state["valid"] = False
            with self.assertRaisesRegex(ValidationError, "at least one valid"):
                finalize_metadrive_ego_artifact(
                    canonical_json_bytes(artifact), registry, binding["sha256"]
                )

            artifact = _metadrive_source(binding["sha256"])
            artifact["semantic_regions"][0]["region_type"] = "bus_bay"
            with self.assertRaisesRegex(ValidationError, "region_type"):
                finalize_metadrive_ego_artifact(
                    canonical_json_bytes(artifact), registry, binding["sha256"]
                )

            artifact = _metadrive_source(binding["sha256"])
            artifact["semantic_regions"][0]["polygon_m"] = [
                [0.0, 0.0],
                [1.0, 1.0],
                [0.0, 1.0],
                [1.0, 0.0],
            ]
            with self.assertRaisesRegex(ValidationError, "non-zero area|self-intersect"):
                finalize_metadrive_ego_artifact(
                    canonical_json_bytes(artifact), registry, binding["sha256"]
                )


if __name__ == "__main__":
    unittest.main()
