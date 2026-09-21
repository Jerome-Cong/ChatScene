import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bus_benchmark.atoms import make_atom
from bus_benchmark.cpd import (
    compute_coverage,
    query_blind_target_selector,
    recompute_cpd_aggregate,
)
from bus_benchmark.jsonio import canonical_json_bytes, sha256_bytes, sha256_file
from bus_benchmark.method_result import MethodResultPaths, build_method_result
from bus_benchmark.metrics import (
    aggregate_semantic_metrics,
    compute_rqs,
    score_semantic_output,
)
from bus_benchmark.roster import validate_records_against_roster
from bus_benchmark.provenance import record_sha256
from bus_benchmark.runtime import aggregate_runtime, runtime_tree_binding
from bus_benchmark.schema import validate_schema_instance, validate_schema_records


SHA = "a" * 64
CREATED_AT = "2026-07-15T00:00:00+00:00"


class MethodResultRealAggregationSmokeTests(unittest.TestCase):
    """Exercise the smallest statistically meaningful real metric chain.

    RQS requires a precise/partial/vague intent triplet, and every query requires
    repetitions 0..4.  This smoke therefore has 15 supported outputs.  It keeps
    the formal publisher's fixed 76-per-style admission test outside the tiny
    fixture while exercising the real schemas, roster, semantic scorer,
    semantic/RQS aggregation, CPD recomputation, and runtime aggregation math.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _oracle(self, style):
        query_id = "intent-{}".format(style)
        actor = make_atom(
            "actor",
            "actor_role_count",
            {"type": "car", "role": "rear_vehicle", "count": "1"},
            provenance={"source": "human_review", "query_id": query_id},
            decision_status="confirmed",
        )
        road = make_atom(
            "road",
            "road_topology",
            {"value": "midblock"},
            provenance={"source": "human_review", "query_id": query_id},
            decision_status="confirmed",
        )
        event = make_atom(
            "event",
            "event_spec",
            {"event": "bus_stop"},
            provenance={"source": "human_review", "query_id": query_id},
            decision_status="confirmed",
        )
        candidate = style in {"partial", "vague"}
        return {
            "schema_version": "0.1",
            "query_id": query_id,
            "intent_group_id": "intent",
            "statistical_intent_cluster_id": "intent",
            "surface_style": style,
            "expected_support": "supported",
            "acceptable_response": [],
            "unsupported_reasons": [],
            "ego_gate": {
                "semantic_role": "ego_bus",
                "count": 1,
                "approved_proxy_allowed": True,
                "length_m": 5.33,
                "width_m": 2.1,
            },
            "atoms": [actor, road, event],
            "cpd_policy": {
                "candidate": candidate,
                "eligible": candidate,
                "dimensions": (
                    [
                        {
                            "name": "actor_longitudinal_distance_bin",
                            "common_semantic": True,
                            "cardinality": "exactly_one",
                            "allowed_values": ["near", "medium", "far"],
                            "target_selector": query_blind_target_selector(
                                "actor_longitudinal_distance_bin", "motor_vehicle"
                            ),
                        }
                    ]
                    if candidate
                    else []
                ),
                "decision_status": "confirmed",
                "cross_platform_judgeable": True if candidate else False,
            },
            "decision_status": "confirmed",
        }

    @staticmethod
    def _observed_atom(atom):
        return {
            "atom_id": atom["atom_id"],
            "category": atom["category"],
            "predicate": atom["predicate"],
            "arguments": copy.deepcopy(atom["arguments"]),
            "polarity": atom["polarity"],
            "evidence_source": "deterministic",
            "confidence": 1.0,
        }

    def _evidence(self, oracle, repetition):
        query_id = oracle["query_id"]
        value = ("near", "medium", "far", "near", "medium")[repetition]
        return {
            "schema_version": "0.1",
            "query_id": query_id,
            "run_id": "{}-{}".format(query_id, repetition),
            "repetition": repetition,
            "method_id": "method-a",
            "platform": "carla",
            "intent_group_id": "intent",
            "statistical_intent_cluster_id": "intent",
            "surface_style": oracle["surface_style"],
            "expected_support": "supported",
            "terminal_status": "complete",
            "ego": {
                "count": 1,
                "semantic_role": "bus_proxy",
                "approved_proxy": True,
                "deterministic": True,
                "length_m": 5.33,
                "width_m": 2.1,
                "blueprint": "vehicle.chevrolet.impala",
                "proxy_id": "smoke-proxy",
            },
            "atoms": [self._observed_atom(atom) for atom in oracle["atoms"]],
            "common_atoms": [
                {
                    "dimension": "actor_longitudinal_distance_bin",
                    "value": value,
                    "target_signature": {"actor_class": "motor_vehicle"},
                }
            ],
            "complete_categories": [
                "actor",
                "road",
                "spatial",
                "event",
                "temporal",
            ],
            "provenance": {
                "source_response_sha256": SHA,
                "source_artifact_sha256": SHA,
                "extractor_id": "smoke-extractor",
                "extractor_version": "0.1",
                "extractor_config_sha256": SHA,
                "created_at_utc": CREATED_AT,
            },
        }

    @staticmethod
    def _score(oracle, evidence):
        score = score_semantic_output(oracle, evidence)
        score.update(
            {
                "schema_version": "0.1",
                "run_id": evidence["run_id"],
                "repetition": evidence["repetition"],
                "method_id": evidence["method_id"],
                "platform": evidence["platform"],
                "provenance": {
                    "oracle_sha256": SHA,
                    "oracle_record_sha256": sha256_bytes(
                        canonical_json_bytes(oracle)
                    ),
                    "evidence_record_sha256": sha256_bytes(
                        canonical_json_bytes(evidence)
                    ),
                    "response_record_sha256": SHA,
                    "roster_sha256": SHA,
                    "extractor_manifest_sha256": SHA,
                    "evaluator_source_sha256": SHA,
                    "freeze_manifest_sha256": SHA,
                    "scorer_id": "bus_benchmark.metrics",
                    "scorer_version": "0.1",
                    "created_at_utc": CREATED_AT,
                },
            }
        )
        return score

    @staticmethod
    def _write(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, list):
            payload = b"".join(canonical_json_bytes(item) + b"\n" for item in value)
        else:
            payload = canonical_json_bytes(value) + b"\n"
        path.write_bytes(payload)

    @staticmethod
    def _binding(path):
        path = Path(path).resolve()
        return {
            "path": str(path),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }

    @staticmethod
    def _skipped_stage():
        return {
            "status": "skipped",
            "started_at_utc": CREATED_AT,
            "finished_at_utc": CREATED_AT,
            "request": None,
            "stdout": None,
            "stderr": None,
            "exit_code": None,
            "timed_out": False,
            "result": None,
            "error": "no complete finalized artifact",
        }

    def _carla_controller(self, worker):
        return {
            "schema_version": "0.1",
            "status": "frozen",
            "controller_id": "carla-fixed-query-blind-v0.1",
            "platform": "carla",
            "query_blind": True,
            "reads_query_metadata": False,
            "reads_evaluation_metadata": False,
            "ego_proxy": {
                "length_m": 5.33,
                "width_m": 2.1,
                "proxy_id": "vehicle.chevrolet.impala",
            },
            "implementation": {
                "module": "bus_benchmark.runtime_worker",
                "class": "FixedIDMPIDBehavior",
                "source_path": str(worker.resolve()),
                "source_sha256": sha256_file(worker),
            },
            "parameters": {
                "dt_seconds": 0.1,
                "target_speed_m_s": 8.3333333333,
                "lookahead_seconds": 1.0,
                "lateral_kp": 1.2,
                "lateral_ki": 0.0,
                "lateral_kd": 0.2,
                "max_front_wheel_rate_rad_s": 0.419,
                "max_front_wheel_angle_rad": 0.603,
                "lane_half_width_m": 2.1,
                "idm_delta": 4.0,
                "max_acceleration_m_s2": 0.7,
                "comfortable_deceleration_m_s2": 2.0,
                "max_deceleration_m_s2": 4.0,
                "max_longitudinal_jerk_m_s3": 4.0,
                "minimum_gap_m": 2.0,
                "time_headway_seconds": 1.5,
            },
            "action_contract": {
                "lateral": "normalized steering [-1,1]",
                "longitudinal": "separate throttle/brake [0,1]",
                "braking_preserved": True,
            },
        }

    def _carla_runtime_record(
        self,
        response,
        worker_config,
        controller,
        attempt,
        generation_manifest_sha256,
        implementation_bundle_sha256,
    ):
        skipped = self._skipped_stage()
        ne = dict(
            copy.deepcopy(skipped),
            selected_seed=None,
            simulated_seconds=0.0,
            steps=0,
            trace=None,
            server_session=None,
            controller_id=controller["controller_id"],
            controller_config_sha256=record_sha256(controller),
            runtime_controller_class=None,
            termination_reason=None,
        )
        return {
            "schema_version": "0.1",
            "run_id": response["run_id"],
            "method_id": response["method_id"],
            "platform": "carla",
            "query_id": response["query_id"],
            "intent_group_id": response["intent_group_id"],
            "statistical_intent_cluster_id": response[
                "statistical_intent_cluster_id"
            ],
            "surface_style": response["surface_style"],
            "repetition": response["repetition"],
            "expected_support": "supported",
            "source_response_sha256": record_sha256(response),
            "source_config_sha256": response["config_sha256"],
            "source_generation_manifest_sha256": generation_manifest_sha256,
            "source_implementation_bundle_sha256": implementation_bundle_sha256,
            "generation_chain_verified": True,
            "source_artifact_sha256": None,
            "excluded_reason": "no_complete_finalized_artifact",
            "compile": copy.deepcopy(skipped),
            "sv_trials": [
                dict(copy.deepcopy(skipped), seed=seed, iterations=None)
                for seed in range(5)
            ],
            "selected_seed": None,
            "ne": ne,
            "iec_exec": {
                "status": "unavailable",
                "coverage": "none",
                "trace_available": False,
                "reason": "native_rollout_trace_unavailable",
            },
            "provenance": {
                "producer_id": "bus_benchmark.runtime",
                "producer_version": "0.1",
                "created_at_utc": CREATED_AT,
                "worker_path": worker_config["worker"],
                "worker_sha256": sha256_file(Path(worker_config["worker"])),
                "interpreter_path": worker_config["interpreter"],
                "interpreter_sha256": sha256_file(
                    Path(worker_config["interpreter"])
                ),
                "worker_config": copy.deepcopy(worker_config),
                "worker_config_sha256": record_sha256(worker_config),
                "controller_source_sha256": controller["implementation"][
                    "source_sha256"
                ],
                "controller_config": copy.deepcopy(controller),
                "attempt_path": str(attempt.resolve()),
            },
        }

    @staticmethod
    def _runtime_record():
        skipped = {
            "status": "skipped",
            "started_at_utc": CREATED_AT,
            "finished_at_utc": CREATED_AT,
            "request": None,
            "stdout": None,
            "stderr": None,
            "exit_code": None,
            "timed_out": False,
            "result": None,
            "error": None,
        }
        controller = {
            "controller_id": "smoke-controller",
            "implementation": {"source_sha256": SHA},
        }
        worker_config = {
            "interpreter": "/frozen/python",
            "interpreter_sha256": SHA,
            "interpreter_bytes": 1,
            "worker": "/frozen/worker.py",
            "worker_sha256": SHA,
            "worker_bytes": 1,
            "timeout_seconds": 1,
            "environment": {},
            "platform_context": {"metadrive_token_registry": {"path": "/frozen/registry", "sha256": SHA, "bytes": 1}},
            "runtime_dependencies": {
                "environment_manifest": {"path": "/frozen/env.json", "sha256": SHA, "bytes": 1},
                "metadrive_module": {"path": "/frozen/metadrive.py", "sha256": SHA, "bytes": 1},
                "source_tree": {
                    "repository_path": "/frozen/repo",
                    "revision": "85e5dadc6c7436d324348f6e3d8f8e680c06b4db",
                    "tracked_subpath": "metadrive",
                    "tracked_tree_sha256": SHA,
                    "tracked_file_count": 1,
                    "tracked_bytes": 1,
                    "policy": "git_tracked_worktree_bytes_v0.1",
                },
                "verification_status": "formal_verified",
            },
        }
        record = {
            "schema_version": "0.1",
            "run_id": "runtime-smoke",
            "method_id": "method-a",
            "platform": "metadrive",
            "query_id": "intent-precise",
            "intent_group_id": "intent",
            "statistical_intent_cluster_id": "intent",
            "surface_style": "precise",
            "repetition": 0,
            "expected_support": "supported",
            "source_response_sha256": SHA,
            "source_config_sha256": SHA,
            "source_generation_manifest_sha256": SHA,
            "source_implementation_bundle_sha256": SHA,
            "generation_chain_verified": True,
            "source_artifact_sha256": None,
            "excluded_reason": "no_complete_finalized_artifact",
            "compile": copy.deepcopy(skipped),
            "sv_trials": [dict(copy.deepcopy(skipped), seed=seed, iterations=None) for seed in range(5)],
            "selected_seed": None,
            "ne": dict(
                copy.deepcopy(skipped),
                selected_seed=None,
                simulated_seconds=0.0,
                steps=0,
                trace=None,
                controller_id="smoke-controller",
                controller_config_sha256=SHA,
                runtime_controller_class=None,
                termination_reason=None,
            ),
            "iec_exec": {
                "status": "unavailable",
                "coverage": "none",
                "trace_available": False,
                "reason": "native_rollout_trace_unavailable",
            },
            "provenance": {
                "producer_id": "bus_benchmark.runtime",
                "producer_version": "0.1",
                "created_at_utc": CREATED_AT,
                "worker_path": "/frozen/worker.py",
                "worker_sha256": SHA,
                "interpreter_path": "/frozen/python",
                "interpreter_sha256": SHA,
                "worker_config": worker_config,
                "worker_config_sha256": SHA,
                "controller_source_sha256": SHA,
                "controller_config": controller,
                "attempt_path": "/frozen/attempt",
            },
        }
        return record

    def test_one_intent_triplet_by_five_real_aggregation_chain(self):
        oracles = [self._oracle(style) for style in ("precise", "partial", "vague")]
        for oracle in oracles:
            validate_schema_instance(oracle, "oracle_record")
        roster = {
            "schema_version": "0.1",
            "decision_status": "confirmed",
            "method_id": "method-a",
            "platform": "carla",
            "library_path": str((self.root / "library.jsonl").resolve()),
            "library_sha256": SHA,
            "query_count": 3,
            "expected_response_count": 15,
            "repetitions": list(range(5)),
            "queries": [
                {
                    "query_id": oracle["query_id"],
                    "intent_group_id": "intent",
                    "statistical_intent_cluster_id": "intent",
                    "surface_style": oracle["surface_style"],
                    "expected_support": "supported",
                }
                for oracle in oracles
            ],
        }
        validate_schema_instance(roster, "query_roster")

        evidence = []
        scores = []
        for oracle in oracles:
            for repetition in range(5):
                item = self._evidence(oracle, repetition)
                evidence.append(item)
                scores.append(self._score(oracle, item))
        validate_schema_records(evidence, "semantic_evidence")
        validate_schema_records(scores, "semantic_score")
        validate_records_against_roster(evidence, roster, require_confirmed=True)
        validate_records_against_roster(scores, roster, require_confirmed=True)

        semantic = aggregate_semantic_metrics(scores)
        rqs = compute_rqs(scores)
        coverage = compute_coverage(oracles, expected_per_style=None)
        cpd = recompute_cpd_aggregate(
            evidence,
            scores,
            oracles,
            coverage,
            roster,
            provenance={
                "roster_sha256": SHA,
                "oracle_sha256": SHA,
                "coverage_sha256": SHA,
                "semantic_evidence_sha256": SHA,
                "semantic_scores_sha256": SHA,
                "responses_sha256": SHA,
                "judge_responses_sha256": SHA,
                "evaluator_source_sha256": SHA,
                "freeze_manifest_sha256": SHA,
                "generation_chain_verified": True,
                "generation_run_manifest_sha256": SHA,
                "implementation_bundle_sha256": SHA,
                "created_at_utc": CREATED_AT,
            },
            expected_per_style=None,
        )
        self.assertEqual(semantic["srs"], 1.0)
        self.assertEqual(rqs["rqs"], 1.0)
        self.assertGreater(cpd["cpd_common"]["joint"]["macro_mean"], 0.0)

        runtime_record = self._runtime_record()
        validate_schema_instance(runtime_record, "runtime_record")
        with mock.patch(
            "bus_benchmark.runtime.validate_runtime_record_consistency",
            return_value=runtime_record,
        ):
            runtime = aggregate_runtime([runtime_record], allow_partial=True)
        self.assertEqual(runtime["metadrive"]["eligible_outputs"], 1)
        self.assertEqual(runtime["metadrive"]["scene_validity"], 0.0)

    def test_carla_triplet_by_five_enters_full_method_result_build(self):
        method_id = "method-a"
        implementation_sha256 = "b" * 64
        generation_manifest_sha256 = "c" * 64
        environment_root = self.root / "runtime-venv"
        environment_bin = environment_root / "bin"
        environment_site = (
            environment_root / "lib" / "python3.8" / "site-packages"
        )
        environment_bin.mkdir(parents=True)
        environment_site.mkdir(parents=True)
        interpreter = environment_bin / "python"
        interpreter.write_bytes(Path(sys.executable).resolve().read_bytes())
        interpreter.chmod(0o755)
        pyvenv = environment_root / "pyvenv.cfg"
        pyvenv.write_text(
            "home = {}\ninclude-system-site-packages = false\nversion = {}\n".format(
                Path(sys.base_prefix) / "bin",
                "{}.{}.{}".format(*sys.version_info[:3]),
            ),
            encoding="utf-8",
        )

        generation_dir = self.root / "generation"
        judge_run_dir = self.root / "judge-run"
        judge_request_dir = self.root / "judge-request"
        for directory in (generation_dir, judge_run_dir, judge_request_dir):
            directory.mkdir()

        worker = self.root / "runtime_worker.py"
        worker.write_text("# frozen smoke worker\n", encoding="utf-8")
        harness_manifest = self.root / "harness-environment.txt"
        harness_manifest.write_text("frozen harness\n", encoding="utf-8")
        python_entry = self.root / "carla-python-entry.egg"
        python_entry.write_bytes(b"python-entry")
        map_path = self.root / "maps" / "TownSmoke.xodr"
        map_path.parent.mkdir()
        map_path.write_text("<OpenDRIVE/>\n", encoding="utf-8")
        server_root = self.root / "CARLA_0.9.13"
        server_binary = (
            server_root
            / "CarlaUE4"
            / "Binaries"
            / "Linux"
            / "CarlaUE4-Linux-Shipping"
        )
        server_binary.parent.mkdir(parents=True)
        server_binary.write_bytes(interpreter.read_bytes())
        server_binary.chmod(0o755)
        scenic_root = self.root / "Scenic" / "src"
        scenic_root.mkdir(parents=True)
        (scenic_root / "scenic.py").write_text("# scenic\n", encoding="utf-8")

        environment_manifest_path = self.root / "runtime-environment.json"
        environment_manifest = {
            "schema_version": "0.1",
            "decision_status": "frozen",
            "runtime_kind": "python",
            "executable": self._binding(interpreter),
            "python_environment_root": str(environment_root.resolve()),
            "python_environment_control_files": [self._binding(pyvenv)],
            "runtime_version": "smoke",
            "locked_dependencies": [],
        }
        self._write(environment_manifest_path, environment_manifest)
        runtime_dependencies = {
            "environment_manifest": self._binding(environment_manifest_path),
            "python_path_entries": [self._binding(python_entry)],
            "source_trees": [
                runtime_tree_binding(server_root, "carla_server_distribution"),
                runtime_tree_binding(scenic_root, "scenic_editable_source"),
            ],
            "map_catalog": [self._binding(map_path)],
        }
        platform_config = {
            "schema_version": "0.1",
            "platform": "carla",
            "status": "frozen",
            "sv_seeds": list(range(5)),
            "sv_max_iterations": 2000,
            "rollout_seconds": 30.0,
            "carla_endpoint": {
                "host": "127.0.0.1",
                "port": 2000,
                "timeout_seconds": 1.0,
            },
            "server_binary": self._binding(server_binary),
            "runtime_dependencies": runtime_dependencies,
            "worker": {
                "interpreter": str(interpreter),
                "interpreter_sha256": sha256_file(interpreter),
                "worker": str(worker.resolve()),
                "worker_sha256": sha256_file(worker),
                "timeout_seconds": 1,
                "environment": {"PYTHONPATH": str(python_entry.resolve())},
            },
        }
        platform_config_path = self.root / "platform.json"
        self._write(platform_config_path, platform_config)
        validate_schema_instance(platform_config, "platform_runtime_config")
        worker_config = {
            "interpreter": str(interpreter),
            "interpreter_sha256": sha256_file(interpreter),
            "interpreter_bytes": interpreter.stat().st_size,
            "worker": str(worker.resolve()),
            "worker_sha256": sha256_file(worker),
            "worker_bytes": worker.stat().st_size,
            "timeout_seconds": 1,
            "environment": {"PYTHONPATH": str(python_entry.resolve())},
            "platform_context": {
                "carla_endpoint": copy.deepcopy(platform_config["carla_endpoint"]),
                "server_binary": copy.deepcopy(platform_config["server_binary"]),
            },
            "runtime_dependencies": dict(
                copy.deepcopy(runtime_dependencies),
                verification_status="formal_verified",
            ),
        }

        controller = self._carla_controller(worker)
        controller_path = self.root / "controller.json"
        self._write(controller_path, controller)
        validate_schema_instance(controller, "controller_config")

        method_environment_binding = self._binding(environment_manifest_path)
        method_config = {
            "schema_version": "0.1",
            "status": "frozen",
            "method_id": method_id,
            "platform": "carla",
            "query_input_fields": ["query_text"],
            "post_output_semantic_repair": False,
            "implementation_bundle": {
                "status": "frozen",
                "bundle_id": "smoke-bundle",
                "bundle_sha256": implementation_sha256,
                "environment_manifest": method_environment_binding,
                "harness_environment_manifest": self._binding(harness_manifest),
                "files": [self._binding(worker)],
            },
            "adapter": {
                "type": "command",
                "argv": [str(interpreter), "-c", "print('{}')"],
                "artifact_path": None,
                "timeout_seconds": 1.0,
                "output_protocol": "stdout_json_v0.1",
                "environment_passthrough": [],
                "max_stdout_bytes": 1024,
                "max_stderr_bytes": 1024,
                "max_artifact_bytes": 1024,
            },
            "finalization": {
                "policy_id": "carla_ego_proxy_v0.1",
                "ego_blueprint": "vehicle.chevrolet.impala",
                "ego_length_m": 5.33,
                "ego_width_m": 2.1,
            },
        }
        method_config_path = self.root / "method.json"
        self._write(method_config_path, method_config)
        validate_schema_instance(method_config, "method_config")
        method_config_sha256 = sha256_file(method_config_path)

        oracles = [self._oracle(style) for style in ("precise", "partial", "vague")]
        oracle_path = self.root / "oracle.jsonl"
        self._write(oracle_path, oracles)
        validate_schema_records(oracles, "oracle_record")
        library_path = self.root / "library.jsonl"
        library_records = [
            {
                "query_id": oracle["query_id"],
                "query_text": "smoke {}".format(oracle["surface_style"]),
            }
            for oracle in oracles
        ]
        self._write(library_path, library_records)
        roster = {
            "schema_version": "0.1",
            "decision_status": "confirmed",
            "method_id": method_id,
            "platform": "carla",
            "library_path": str(library_path.resolve()),
            "library_sha256": sha256_file(library_path),
            "query_count": 3,
            "expected_response_count": 15,
            "repetitions": list(range(5)),
            "queries": [
                {
                    "query_id": oracle["query_id"],
                    "intent_group_id": "intent",
                    "statistical_intent_cluster_id": "intent",
                    "surface_style": oracle["surface_style"],
                    "expected_support": "supported",
                }
                for oracle in oracles
            ],
        }
        roster_path = self.root / "roster.json"
        self._write(roster_path, roster)
        validate_schema_instance(roster, "query_roster")

        stdout_path = self.root / "raw-stdout.txt"
        stderr_path = self.root / "raw-stderr.txt"
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"generation failed")
        stdout_binding = self._binding(stdout_path)
        stderr_binding = self._binding(stderr_path)
        raw_envelope = {
            "stdout": stdout_binding,
            "stderr": stderr_binding,
            "exit_code": 1,
            "timed_out": False,
        }
        raw_envelope["envelope_sha256"] = sha256_bytes(
            canonical_json_bytes(raw_envelope)
        )
        responses = []
        evidence = []
        scores = []
        for oracle in oracles:
            for repetition in range(5):
                run_id = "{}-{}".format(oracle["query_id"], repetition)
                response = {
                    "schema_version": "0.1",
                    "run_id": run_id,
                    "method_id": method_id,
                    "platform": "carla",
                    "query_id": oracle["query_id"],
                    "intent_group_id": "intent",
                    "statistical_intent_cluster_id": "intent",
                    "surface_style": oracle["surface_style"],
                    "repetition": repetition,
                    "expected_support": "supported",
                    "disposition": "failure",
                    "request_sha256": SHA,
                    "config_sha256": method_config_sha256,
                    "implementation_bundle_sha256": implementation_sha256,
                    "terminal_status": "failed",
                    "artifact": None,
                    "raw_method_response": copy.deepcopy(raw_envelope),
                    "provenance": {
                        "producer_id": "immutable-generation-runner",
                        "producer_version": "0.1",
                        "created_at_utc": CREATED_AT,
                    },
                }
                responses.append(response)
                item = self._evidence(oracle, repetition)
                evidence.append(item)
                scores.append(self._score(oracle, item))
        validate_schema_records(responses, "response_record")
        validate_schema_records(evidence, "semantic_evidence")
        validate_schema_records(scores, "semantic_score")
        validate_records_against_roster(responses, roster, require_confirmed=True)
        validate_records_against_roster(evidence, roster, require_confirmed=True)
        validate_records_against_roster(scores, roster, require_confirmed=True)

        response_path = generation_dir / "response.jsonl"
        evidence_index_path = generation_dir / "evidence.jsonl"
        generation_manifest_path = generation_dir / "generation_run_manifest.json"
        semantic_evidence_path = self.root / "semantic-evidence.jsonl"
        semantic_scores_path = self.root / "semantic-scores.jsonl"
        self._write(response_path, responses)
        self._write(evidence_index_path, [{}])
        self._write(generation_manifest_path, {})
        self._write(semantic_evidence_path, evidence)
        self._write(semantic_scores_path, scores)

        coverage = compute_coverage(oracles, expected_per_style=None)
        coverage_path = self.root / "coverage.json"
        self._write(coverage_path, coverage)
        validate_schema_instance(coverage, "cpd_coverage")

        request_manifest_path = judge_request_dir / "request_manifest.json"
        execution_index_path = judge_run_dir / "execution_index.jsonl"
        judge_responses_path = judge_run_dir / "judge_responses.jsonl"
        self._write(request_manifest_path, {})
        self._write(execution_index_path, [{}])
        self._write(judge_responses_path, [{}])
        judge_run_manifest = {
            "schema_version": "0.1",
            "result_kind": "judge_run_bundle",
            "terminal_status": "complete",
            "request_count": 15,
            "execution_count": 15,
            "response_count": 15,
            "request_manifest_sha256": sha256_file(request_manifest_path),
            "request_manifest": self._binding(request_manifest_path),
            "runner_manifest_sha256": SHA,
            "execution_index_sha256": sha256_file(execution_index_path),
            "judge_responses_sha256": sha256_file(judge_responses_path),
            "created_at_utc": CREATED_AT,
            "manifest_sha256": "d" * 64,
        }
        judge_run_manifest_path = judge_run_dir / "judge_run_manifest.json"
        self._write(judge_run_manifest_path, judge_run_manifest)
        validate_schema_instance(judge_run_manifest, "judge_run_manifest")

        uqh_assessments_path = self.root / "uqh-assessments.jsonl"
        uqh_registry_path = self.root / "uqh-registry.json"
        self._write(uqh_assessments_path, [])
        self._write(uqh_registry_path, {})
        uqh = {"uqh": 0.0, "provenance": {}}
        semantic_aggregate = {
            "repetition_grid": validate_records_against_roster(
                scores, roster, require_confirmed=True
            ),
            "semantic": aggregate_semantic_metrics(scores),
            "rqs": compute_rqs(scores),
            "uqh": dict(
                copy.deepcopy(uqh),
                provenance={
                    "freeze_manifest_sha256": SHA,
                    "generation_run_manifest_sha256": generation_manifest_sha256,
                },
            ),
        }
        semantic_aggregate_path = self.root / "semantic-aggregate.json"
        self._write(semantic_aggregate_path, semantic_aggregate)

        runtime_records = []
        for response in responses:
            attempt = self.root / "runtime-attempts" / response["run_id"]
            attempt.mkdir(parents=True)
            runtime_records.append(
                self._carla_runtime_record(
                    response,
                    worker_config,
                    controller,
                    attempt,
                    generation_manifest_sha256,
                    implementation_sha256,
                )
            )
        validate_schema_records(runtime_records, "runtime_record")
        generation_chain = {
            "method_id": method_id,
            "platform": "carla",
            "record_count": 15,
            "manifest_sha256": generation_manifest_sha256,
            "implementation_bundle_sha256": implementation_sha256,
            "verified": True,
        }
        runtime_aggregate = aggregate_runtime(
            runtime_records,
            roster=roster,
            responses=responses,
            generation_chain=generation_chain,
            allow_partial=False,
            require_confirmed=True,
        )
        runtime_records_path = self.root / "runtime-records.jsonl"
        runtime_aggregate_path = self.root / "runtime-aggregate.json"
        self._write(runtime_records_path, runtime_records)
        self._write(runtime_aggregate_path, runtime_aggregate)

        cpd_provenance = {
            "roster_sha256": sha256_file(roster_path),
            "oracle_sha256": sha256_file(oracle_path),
            "coverage_sha256": sha256_file(coverage_path),
            "semantic_evidence_sha256": sha256_file(semantic_evidence_path),
            "semantic_scores_sha256": sha256_file(semantic_scores_path),
            "responses_sha256": sha256_file(response_path),
            "judge_responses_sha256": sha256_file(judge_responses_path),
            "evaluator_source_sha256": SHA,
            "freeze_manifest_sha256": SHA,
            "generation_chain_verified": True,
            "generation_run_manifest_sha256": generation_manifest_sha256,
            "implementation_bundle_sha256": implementation_sha256,
            "created_at_utc": CREATED_AT,
        }
        cpd = recompute_cpd_aggregate(
            evidence,
            scores,
            oracles,
            coverage,
            roster,
            provenance=cpd_provenance,
            allow_draft_policy=False,
            expected_per_style=None,
        )
        cpd_path = self.root / "cpd.json"
        self._write(cpd_path, cpd)
        validate_schema_instance(cpd, "cpd_aggregate")

        human_gold_path = self.root / "human-gold.jsonl"
        self._write(human_gold_path, [{"subject_id": "single-human-gold"}])
        freeze_assets = [
            ("query_library_test", library_path),
            ("requirement_oracle_test", oracle_path),
            ("query_roster", roster_path),
            ("method_config", method_config_path),
            ("uqh_assessor_registry", uqh_registry_path),
            ("cpd_policy", coverage_path),
            ("platform_config_carla", platform_config_path),
            ("controller_config_carla", controller_path),
            ("human_query_gold", human_gold_path),
        ]
        freeze_manifest = {
            "manifest_sha256": SHA,
            "assets": [
                dict(self._binding(path), role=role) for role, path in freeze_assets
            ],
        }
        freeze_manifest_path = self.root / "freeze.json"
        self._write(freeze_manifest_path, freeze_manifest)

        paths = MethodResultPaths(
            freeze_manifest=freeze_manifest_path,
            library=library_path,
            oracle=oracle_path,
            roster=roster_path,
            method_config=method_config_path,
            generation_dir=generation_dir,
            responses=response_path,
            semantic_evidence=semantic_evidence_path,
            semantic_scores=semantic_scores_path,
            judge_run_dir=judge_run_dir,
            semantic_aggregate=semantic_aggregate_path,
            uqh_assessments=uqh_assessments_path,
            uqh_assessor_registry=uqh_registry_path,
            cpd_aggregate=cpd_path,
            cpd_coverage=coverage_path,
            runtime_records=runtime_records_path,
            runtime_aggregate=runtime_aggregate_path,
            platform_config=platform_config_path,
            controller_config=controller_path,
        )

        def smoke_cpd(*args, **kwargs):
            self.assertEqual(kwargs["expected_per_style"], 76)
            kwargs["expected_per_style"] = None
            return recompute_cpd_aggregate(*args, **kwargs)

        with mock.patch(
            "bus_benchmark.method_result.verify_freeze_manifest"
        ), mock.patch(
            "bus_benchmark.method_result.validate_generation_response_chain",
            return_value={key: value for key, value in generation_chain.items() if key != "verified"},
        ), mock.patch(
            "bus_benchmark.method_result.validate_score_provenance"
        ), mock.patch(
            "bus_benchmark.method_result.validate_evidence_response_chain"
        ), mock.patch(
            "bus_benchmark.method_result.validate_score_judge_run_provenance"
        ), mock.patch(
            "bus_benchmark.method_result.validate_scores_against_evidence"
        ), mock.patch(
            "bus_benchmark.method_result.validate_aggregate_response_chain"
        ), mock.patch(
            "bus_benchmark.method_result.validate_judge_run_bundle",
            return_value=(judge_run_manifest, []),
        ), mock.patch(
            "bus_benchmark.method_result.validate_frozen_judge_response_chain",
            return_value={},
        ), mock.patch(
            "bus_benchmark.method_result.compute_uqh", return_value=copy.deepcopy(uqh)
        ), mock.patch(
            "bus_benchmark.method_result.metric_source_hash", return_value=SHA
        ), mock.patch(
            "bus_benchmark.method_result.recompute_cpd_aggregate",
            side_effect=smoke_cpd,
        ), mock.patch(
            "bus_benchmark.method_result.validate_platform_runtime_config",
            return_value=worker_config,
        ):
            result, snapshots = build_method_result(paths, created_at_utc=CREATED_AT)

        self.assertEqual(result["method_id"], method_id)
        self.assertEqual(result["platform"], "carla")
        self.assertEqual(result["primary_metrics"]["srs"], 1.0)
        self.assertEqual(result["primary_metrics"]["rqs"], 1.0)
        self.assertEqual(result["primary_metrics"]["sv"], 0.0)
        self.assertEqual(result["primary_metrics"]["ne"], 0.0)
        self.assertEqual(
            result["primary_metrics"]["cpd_common"],
            cpd["cpd_common"]["joint"]["macro_mean"],
        )
        self.assertEqual(
            result["primary_metrics"]["sv"],
            runtime_aggregate["carla"]["scene_validity"],
        )
        self.assertEqual(
            result["primary_metrics"]["rqs"], semantic_aggregate["rqs"]["rqs"]
        )
        self.assertEqual(len(result["inputs"]), 23)
        self.assertIn(str(human_gold_path), snapshots.guards)


if __name__ == "__main__":
    unittest.main()
