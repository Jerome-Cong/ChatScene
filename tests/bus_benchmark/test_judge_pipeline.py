import fcntl
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bus_benchmark.atoms import make_atom
from bus_benchmark.errors import ValidationError
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    write_json,
    write_jsonl,
)
from bus_benchmark.judge import judge_config_sha256
from bus_benchmark.judge_pipeline import (
    _sealed_memfd,
    create_judge_request_bundle,
    run_judge_request_bundle,
    validate_judge_run_bundle,
)
from bus_benchmark.generation import probe_python_environment


ROOT = Path(__file__).resolve().parents[2]
CREATED_AT = "2026-07-14T00:00:00+00:00"
TEST_SECRET_NAME = "BUS_BENCHMARK_JUDGE_TEST_SECRET"
PYTHON_EXECUTABLE = Path(sys.executable).resolve()
PYTHON_ENVIRONMENT = probe_python_environment(PYTHON_EXECUTABLE)


def _binding(path):
    path = Path(path).resolve()
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _asset(role, path):
    return {"role": role, **_binding(path)}


class JudgePipelineTests(unittest.TestCase):
    def _bundle(
        self,
        directory,
        mode="success",
        deterministic=False,
        atom_count=None,
        copied_executable=False,
    ):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        artifact = root / "scene.scenic"
        artifact.write_text(
            "ego = new Car\n" + ("x" * 2_000_000 if mode == "blocking" else ""),
            encoding="utf-8",
        )
        method_stdout = root / "method.stdout"
        method_stderr = root / "method.stderr"
        method_stdout.write_text("generated\n", encoding="utf-8")
        method_stderr.write_bytes(b"")

        response = {
            "schema_version": "0.1",
            "run_id": "run-1",
            "method_id": "method-a",
            "platform": "carla",
            "query_id": "query-1",
            "intent_group_id": "intent-1",
            "statistical_intent_cluster_id": "cluster-1",
            "surface_style": "precise",
            "repetition": 0,
            "expected_support": "supported",
            "disposition": "generate",
            "request_sha256": "1" * 64,
            "config_sha256": "2" * 64,
            "implementation_bundle_sha256": "3" * 64,
            "terminal_status": "complete",
            "artifact": _binding(artifact),
            "raw_method_response": {
                "stdout": _binding(method_stdout),
                "stderr": _binding(method_stderr),
                "exit_code": 0,
                "timed_out": False,
                "envelope_sha256": "4" * 64,
            },
            "provenance": {
                "producer_id": "fixture",
                "producer_version": "0.1",
                "created_at_utc": CREATED_AT,
            },
        }
        if atom_count is None:
            atom_count = 2 if mode == "self_mutate" else 1
        atoms = [
            make_atom(
                "event",
                "event_spec",
                {"event": "bus_docking_{}".format(index)},
                provenance={"source": "human_review", "query_id": "query-1"},
                decision_status="confirmed",
            )
            for index in range(atom_count)
        ]
        evidence_atoms = []
        complete_categories = []
        if deterministic:
            evidence_atoms = [
                {
                    "atom_id": atom["atom_id"],
                    "category": atom["category"],
                    "predicate": atom["predicate"],
                    "arguments": atom["arguments"],
                    "polarity": atom["polarity"],
                    "evidence_source": "deterministic",
                    "confidence": 1.0,
                }
                for atom in atoms
            ]
            complete_categories = ["event"]
        evidence = {
            "schema_version": "0.1",
            "query_id": "query-1",
            "run_id": "run-1",
            "repetition": 0,
            "method_id": "method-a",
            "platform": "carla",
            "intent_group_id": "intent-1",
            "statistical_intent_cluster_id": "cluster-1",
            "surface_style": "precise",
            "expected_support": "supported",
            "terminal_status": "complete",
            "ego": {
                "count": 1,
                "semantic_role": "ego_bus",
                "approved_proxy": True,
                "deterministic": True,
                "length_m": 5.33,
                "width_m": 2.1,
                "blueprint": "vehicle.chevrolet.impala",
            },
            "atoms": evidence_atoms,
            "common_atoms": [],
            "complete_categories": complete_categories,
            "provenance": {
                "source_response_sha256": sha256_bytes(
                    canonical_json_bytes(response)
                ),
                "source_artifact_sha256": response["artifact"]["sha256"],
                "extractor_id": "fixture",
                "extractor_version": "0.1",
                "extractor_config_sha256": "5" * 64,
                "created_at_utc": CREATED_AT,
            },
        }

        library = root / "library.jsonl"
        oracle = root / "oracle.jsonl"
        write_jsonl(
            library,
            [{"query_id": "query-1", "query_text": "Generate a docking bus."}],
        )
        write_jsonl(oracle, [{"query_id": "query-1", "atoms": atoms}])
        prompt = root / "prompt.txt"
        prompt.write_text("Return one strict JSON verdict.\n", encoding="utf-8")
        calibration = root / "calibration.jsonl"
        calibration_roster = root / "calibration_roster.json"
        write_jsonl(calibration, [{"fixture": True}])
        write_json(calibration_roster, {"fixture": True})

        runner_source = root / "runner.py"
        if mode == "success":
            source = (
                "import json, sys\n"
                "json.load(sys.stdin)\n"
                "json.dump({'evidence':['fixture'],'rationale':'fixture',"
                "'verdict':'satisfied','confidence':1.0}, sys.stdout, "
                "sort_keys=True, separators=(',', ':'))\n"
            )
            credentials = []
        elif mode == "request_bound":
            source = (
                "import json, sys\n"
                "request = json.load(sys.stdin)\n"
                "atom_id = request['messages'][1]['content']['oracle_atom']['atom_id']\n"
                "json.dump({'evidence':['fixture-' + atom_id],"
                "'rationale':'fixture-' + atom_id,'verdict':'satisfied',"
                "'confidence':1.0}, sys.stdout, sort_keys=True, "
                "separators=(',', ':'))\n"
            )
            credentials = []
        elif mode == "exit":
            source = "import sys\nsys.stdin.buffer.read()\nsys.exit(7)\n"
            credentials = []
        elif mode == "leak":
            source = (
                "import json, os, sys\n"
                "json.load(sys.stdin)\n"
                "sys.stderr.write(os.environ['{}'])\n"
                "json.dump({{'evidence':['fixture'],'rationale':'fixture',"
                "'verdict':'satisfied','confidence':1.0}}, sys.stdout)\n"
            ).format(TEST_SECRET_NAME)
            credentials = [TEST_SECRET_NAME]
        elif mode == "home_sentinel":
            source = (
                "import json, os, pathlib, sys\n"
                "json.load(sys.stdin)\n"
                "pathlib.Path(os.environ['HOME'], 'home-sentinel').write_text('x')\n"
                "pathlib.Path(os.environ['TMPDIR'], 'tmp-sentinel').write_text('x')\n"
                "json.dump({'evidence':['fixture'],'rationale':'fixture',"
                "'verdict':'satisfied','confidence':1.0}, sys.stdout)\n"
            )
            credentials = []
        elif mode == "blocking":
            source = "import time\ntime.sleep(30)\n"
            credentials = []
        elif mode == "self_mutate":
            marker = root / "self-mutation-attempted"
            replacement = (
                "import json, sys\n"
                "json.load(sys.stdin)\n"
                "json.dump({'evidence':['mutated'],'rationale':'mutated',"
                "'verdict':'violated','confidence':1.0}, sys.stdout)\n"
            )
            source = (
                "import json, pathlib, sys\n"
                "json.load(sys.stdin)\n"
                "marker = pathlib.Path({marker!r})\n"
                "if not marker.exists():\n"
                "    try:\n"
                "        pathlib.Path(__file__).chmod(0o600)\n"
                "        pathlib.Path(__file__).write_text({replacement!r}, "
                "encoding='utf-8')\n"
                "    except OSError:\n"
                "        pass\n"
                "    marker.write_text('attempted', encoding='utf-8')\n"
                "json.dump({{'evidence':['fixture'],'rationale':'fixture',"
                "'verdict':'satisfied','confidence':1.0}}, sys.stdout, "
                "sort_keys=True, separators=(',', ':'))\n"
            ).format(marker=str(marker), replacement=replacement)
            credentials = []
        else:
            raise AssertionError("unknown mode")
        runner_source.write_text(source, encoding="utf-8")
        if copied_executable:
            executable = root / "runner-python"
            shutil.copyfile(PYTHON_EXECUTABLE, executable)
            executable.chmod(0o700)
            executable = executable.resolve()
        else:
            executable = PYTHON_EXECUTABLE
        environment = root / "runner_environment.json"
        write_json(
            environment,
            {
                "schema_version": "0.1",
                "decision_status": "frozen",
                "runtime_kind": "python",
                "executable": _binding(executable),
                "runtime_version": PYTHON_ENVIRONMENT["runtime_version"],
                "locked_dependencies": PYTHON_ENVIRONMENT["locked_dependencies"],
            },
        )
        runner_manifest = root / "runner_manifest.json"
        write_json(
            runner_manifest,
            {
                "schema_version": "0.1",
                "decision_status": "confirmed",
                "runner_id": "fixture-runner",
                "runner_version": "0.1",
                "input_contract": "canonical_judge_request_stdin_v0.1",
                "output_contract": "judge_output_stdout_v0.1",
                "execution_order": "item_id_ascending",
                "max_attempts": 1,
                "environment_manifest": _binding(environment),
                "source_files": [_binding(runner_source)],
                "command": {
                    "argv": [str(executable), str(runner_source.resolve())],
                    "timeout_seconds": 0.2 if mode == "blocking" else 5.0,
                    "max_request_bytes": 4194304,
                    "max_stdout_bytes": 1048576,
                    "max_stderr_bytes": 1048576,
                    "credential_env_names": credentials,
                },
            },
        )
        output_schema = (
            ROOT / "benchmark_configs" / "schemas" / "judge_output.schema.json"
        )
        config_sha256 = judge_config_sha256(
            model_id="fixture-model",
            prompt_sha256=sha256_file(prompt),
            inference_config={"temperature": 0.0},
            output_schema_sha256=sha256_file(output_schema),
            request_contract="judge_atom_v0.1",
            runner_manifest_sha256=sha256_file(runner_manifest),
        )
        rubric = root / "rubric.json"
        write_json(
            rubric,
            {
                "decision_status": "confirmed",
                "request_contract": "judge_atom_v0.1",
                "model_id": "fixture-model",
                "inference_config": {"temperature": 0.0},
                "prompt": _binding(prompt),
                "output_schema": _binding(output_schema),
                "runner_manifest_sha256": sha256_file(runner_manifest),
                "judge_config_sha256": config_sha256,
                "calibration": _binding(calibration),
                "calibration_roster": _binding(calibration_roster),
                "calibration_context": _binding(calibration_roster),
                "calibration_request_manifest": _binding(calibration_roster),
                "calibration_run_manifest": _binding(calibration_roster),
                "calibration_record_count": 180,
                "macro_f1": 1.0,
                "cohen_kappa": 1.0,
                "gold_support_by_verdict": {
                    "satisfied": 60,
                    "violated": 60,
                    "unknown": 60,
                },
                "platform_metrics": {
                    platform: {
                        "record_count": 90,
                        "macro_f1": 1.0,
                        "cohen_kappa": 1.0,
                        "gold_support_by_verdict": {
                            "satisfied": 30,
                            "violated": 30,
                            "unknown": 30,
                        },
                    }
                    for platform in ("carla", "metadrive")
                },
            },
        )
        manifest = {
            "manifest_sha256": "f" * 64,
            "assets": [
                _asset("query_library_test", library),
                _asset("requirement_oracle_test", oracle),
                _asset("judge_runner_manifest", runner_manifest),
                _asset("judge_rubric", rubric),
            ],
        }
        return {
            "manifest": manifest,
            "responses": [response],
            "evidence": [evidence],
            "runner_source": runner_source,
            "runner_executable": executable,
            "atoms": atoms,
        }

    def _request(self, bundle, path):
        return create_judge_request_bundle(
            bundle["responses"],
            bundle["evidence"],
            bundle["manifest"],
            path,
        )

    def test_happy_path_is_exact_and_replayable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            request_dir = root / "requests"
            run_dir = root / "run"
            request_manifest = self._request(bundle, request_dir)
            self.assertEqual(request_manifest["request_count"], 1)
            run_manifest = run_judge_request_bundle(
                request_dir,
                bundle["responses"],
                bundle["evidence"],
                bundle["manifest"],
                run_dir,
            )
            self.assertEqual(run_manifest["response_count"], 1)
            validated, responses = validate_judge_run_bundle(
                run_dir,
                bundle["responses"],
                bundle["evidence"],
                bundle["manifest"],
            )
            self.assertEqual(validated, run_manifest)
            self.assertEqual(len(responses), 1)
            self.assertEqual(
                json.loads(responses[0]["raw_response"])["verdict"], "satisfied"
            )

    def test_zero_item_bundle_never_invokes_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root, deterministic=True)
            request_dir = root / "requests"
            run_dir = root / "run"
            self.assertEqual(self._request(bundle, request_dir)["request_count"], 0)
            with mock.patch(
                "bus_benchmark.judge_pipeline._execute_runner",
                side_effect=AssertionError("empty request bundle invoked runner"),
            ):
                run_manifest = run_judge_request_bundle(
                    request_dir,
                    bundle["responses"],
                    bundle["evidence"],
                    bundle["manifest"],
                    run_dir,
                )
            self.assertEqual(run_manifest["execution_count"], 0)
            self.assertEqual(read_jsonl(run_dir / "judge_responses.jsonl"), [])

    def test_runner_source_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            request_dir = root / "requests"
            self._request(bundle, request_dir)
            bundle["runner_source"].write_text("raise SystemExit(0)\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "binding changed"):
                run_judge_request_bundle(
                    request_dir,
                    bundle["responses"],
                    bundle["evidence"],
                    bundle["manifest"],
                    root / "run",
                )

    def test_missing_run_response_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            request_dir = root / "requests"
            run_dir = root / "run"
            self._request(bundle, request_dir)
            run_judge_request_bundle(
                request_dir,
                bundle["responses"],
                bundle["evidence"],
                bundle["manifest"],
                run_dir,
            )
            write_jsonl(run_dir / "judge_responses.jsonl", [])
            with self.assertRaisesRegex(ValidationError, "exactly cover"):
                validate_judge_run_bundle(
                    run_dir,
                    bundle["responses"],
                    bundle["evidence"],
                    bundle["manifest"],
                )

    def test_process_failure_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root, mode="exit")
            request_dir = root / "requests"
            run_dir = root / "run"
            self._request(bundle, request_dir)
            with self.assertRaisesRegex(ValidationError, "exited unsuccessfully"):
                run_judge_request_bundle(
                    request_dir,
                    bundle["responses"],
                    bundle["evidence"],
                    bundle["manifest"],
                    run_dir,
                )
            self.assertFalse(run_dir.exists())

    def test_bundles_are_immutable_and_resume_only_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            request_dir = root / "requests"
            run_dir = root / "run"
            self._request(bundle, request_dir)
            with self.assertRaisesRegex(ValidationError, "already exists"):
                self._request(bundle, request_dir)
            run_judge_request_bundle(
                request_dir,
                bundle["responses"],
                bundle["evidence"],
                bundle["manifest"],
                run_dir,
            )
            with self.assertRaisesRegex(ValidationError, "already exists"):
                run_judge_request_bundle(
                    request_dir,
                    bundle["responses"],
                    bundle["evidence"],
                    bundle["manifest"],
                    run_dir,
                )
            resumed = run_judge_request_bundle(
                request_dir,
                bundle["responses"],
                bundle["evidence"],
                bundle["manifest"],
                run_dir,
                resume=True,
            )
            self.assertEqual(resumed["terminal_status"], "complete")

    def test_credential_value_is_never_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root, mode="leak")
            request_dir = root / "requests"
            run_dir = root / "run"
            self._request(bundle, request_dir)
            secret = "fixture-secret-value-never-persist"
            with mock.patch.dict(os.environ, {TEST_SECRET_NAME: secret}, clear=False):
                with self.assertRaisesRegex(ValidationError, "credential value"):
                    run_judge_request_bundle(
                        request_dir,
                        bundle["responses"],
                        bundle["evidence"],
                        bundle["manifest"],
                        run_dir,
                    )
            self.assertFalse(run_dir.exists())
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret.encode("utf-8"), path.read_bytes())

    def test_home_and_tmp_are_external_and_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root, mode="home_sentinel")
            request_dir = root / "requests"
            run_dir = root / "run"
            self._request(bundle, request_dir)
            real_mkdtemp = tempfile.mkdtemp
            workspaces = []

            def recording_mkdtemp(*args, **kwargs):
                path = real_mkdtemp(*args, **kwargs)
                if kwargs.get("prefix", "").startswith("bus-benchmark-judge-exec-"):
                    workspaces.append(Path(path))
                return path

            with mock.patch(
                "bus_benchmark.judge_pipeline.tempfile.mkdtemp",
                side_effect=recording_mkdtemp,
            ):
                run_judge_request_bundle(
                    request_dir,
                    bundle["responses"],
                    bundle["evidence"],
                    bundle["manifest"],
                    run_dir,
                )
            self.assertEqual(len(workspaces), 1)
            self.assertFalse(workspaces[0].exists())
            self.assertFalse(any("sentinel" in path.name for path in run_dir.rglob("*")))
            validate_judge_run_bundle(
                run_dir,
                bundle["responses"],
                bundle["evidence"],
                bundle["manifest"],
            )

    def test_blocking_stdin_obeys_the_single_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root, mode="blocking")
            request_dir = root / "requests"
            run_dir = root / "run"
            self._request(bundle, request_dir)
            started = time.monotonic()
            with self.assertRaisesRegex(ValidationError, "timed out"):
                run_judge_request_bundle(
                    request_dir,
                    bundle["responses"],
                    bundle["evidence"],
                    bundle["manifest"],
                    run_dir,
                )
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertFalse(run_dir.exists())

    def test_execution_uses_sealed_bytes_when_original_source_is_swapped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            request_dir = root / "requests"
            run_dir = root / "run"
            self._request(bundle, request_dir)
            source_path = bundle["runner_source"]
            original_source = source_path.read_bytes()
            real_popen = __import__("subprocess").Popen
            executed_argv = []

            def swapping_popen(argv, *args, **kwargs):
                if (
                    str(argv[0]).startswith("/proc/self/fd/")
                    and len(argv) > 1
                    and str(argv[1]).startswith("/proc/self/fd/")
                ):
                    executed_argv.append(list(argv))
                    source_path.write_text("raise SystemExit(91)\n", encoding="utf-8")
                return real_popen(argv, *args, **kwargs)

            try:
                with mock.patch(
                    "bus_benchmark.judge_pipeline.subprocess.Popen",
                    side_effect=swapping_popen,
                ):
                    run_judge_request_bundle(
                        request_dir,
                        bundle["responses"],
                        bundle["evidence"],
                        bundle["manifest"],
                        run_dir,
                    )
            finally:
                source_path.write_bytes(original_source)
            self.assertEqual(len(executed_argv), 1)
            self.assertNotIn(str(source_path.resolve()), executed_argv[0])
            responses = read_jsonl(run_dir / "judge_responses.jsonl")
            self.assertEqual(json.loads(responses[0]["raw_response"])["verdict"], "satisfied")
            validate_judge_run_bundle(
                run_dir,
                bundle["responses"],
                bundle["evidence"],
                bundle["manifest"],
            )

    def test_each_item_uses_fresh_sealed_runner_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root, mode="self_mutate")
            request_dir = root / "requests"
            run_dir = root / "run"
            self.assertEqual(self._request(bundle, request_dir)["request_count"], 2)
            run_judge_request_bundle(
                request_dir,
                bundle["responses"],
                bundle["evidence"],
                bundle["manifest"],
                run_dir,
            )
            _, responses = validate_judge_run_bundle(
                run_dir,
                bundle["responses"],
                bundle["evidence"],
                bundle["manifest"],
            )
            self.assertEqual(len(responses), 2)
            self.assertTrue((root / "self-mutation-attempted").is_file())
            self.assertEqual(
                [json.loads(record["raw_response"])["verdict"] for record in responses],
                ["satisfied", "satisfied"],
            )

    def test_request_and_run_inventory_reject_extra_and_symlink_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            request_dir = root / "requests"
            self._request(bundle, request_dir)
            (request_dir / "extra.txt").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "exact allowlist"):
                run_judge_request_bundle(
                    request_dir,
                    bundle["responses"],
                    bundle["evidence"],
                    bundle["manifest"],
                    root / "unused-run",
                )
            (request_dir / "extra.txt").unlink()
            run_dir = root / "run"
            run_judge_request_bundle(
                request_dir,
                bundle["responses"],
                bundle["evidence"],
                bundle["manifest"],
                run_dir,
            )
            (run_dir / "unexpected-link").symlink_to(run_dir / "judge_responses.jsonl")
            with self.assertRaisesRegex(ValidationError, "symlink"):
                validate_judge_run_bundle(
                    run_dir,
                    bundle["responses"],
                    bundle["evidence"],
                    bundle["manifest"],
                )

    def test_live_python_environment_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            drifted = {
                "runtime_version": PYTHON_ENVIRONMENT["runtime_version"] + "-drift",
                "locked_dependencies": PYTHON_ENVIRONMENT["locked_dependencies"],
            }
            with mock.patch(
                "bus_benchmark.generation.probe_python_environment",
                return_value=drifted,
            ):
                with self.assertRaisesRegex(ValidationError, "environment drifted"):
                    self._request(bundle, root / "requests")

    def test_environment_probe_uses_verified_sealed_executable_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root, copied_executable=True)
            executable = bundle["runner_executable"]
            real_probe = probe_python_environment
            observed_probe_paths = []

            def swap_original_then_probe(probe_path, **kwargs):
                observed_probe_paths.append(str(probe_path))
                executable.write_bytes(b"attacker-swapped-executable\n")
                return real_probe(probe_path, **kwargs)

            with mock.patch(
                "bus_benchmark.generation.probe_python_environment",
                side_effect=swap_original_then_probe,
            ):
                request_manifest = self._request(bundle, root / "requests")
            self.assertEqual(request_manifest["request_count"], 1)
            self.assertEqual(len(observed_probe_paths), 1)
            self.assertTrue(observed_probe_paths[0].startswith("/proc/self/fd/"))

    def test_executable_and_source_execute_bits_are_sealed(self):
        seal_exec = getattr(fcntl, "F_SEAL_EXEC", 0x0020)
        executable_fd = _sealed_memfd(
            "judge-test-executable",
            b"verified executable bytes",
            0o500,
            executable=True,
        )
        source_fd = _sealed_memfd(
            "judge-test-source",
            b"verified source bytes",
            0o400,
            executable=False,
        )
        try:
            self.assertEqual(
                fcntl.fcntl(executable_fd, fcntl.F_GET_SEALS) & seal_exec,
                seal_exec,
            )
            self.assertEqual(
                fcntl.fcntl(source_fd, fcntl.F_GET_SEALS) & seal_exec,
                seal_exec,
            )
            with self.assertRaises(OSError):
                os.fchmod(executable_fd, 0o400)
            with self.assertRaises(OSError):
                os.fchmod(source_fd, 0o500)
        finally:
            os.close(executable_fd)
            os.close(source_fd)


if __name__ == "__main__":
    unittest.main()
