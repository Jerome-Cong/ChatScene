import copy
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import bus_benchmark.cli as cli_module
import bus_benchmark.method_result as method_result_module
from bus_benchmark.cli import build_parser
from bus_benchmark.errors import ValidationError
from bus_benchmark.jsonio import canonical_json_bytes, sha256_bytes, sha256_file
from bus_benchmark.method_result import (
    MethodResultPaths,
    _recheck_snapshots,
    _snapshot_file,
    build_method_result,
    publish_method_result,
    validate_method_result,
)
from bus_benchmark.provenance import record_sha256
from bus_benchmark.schema import validate_schema_instance as real_schema_validate


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


class MethodResultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.generation = self.root / "generation"
        self.judge_request = self.root / "judge-request"
        self.judge_run = self.root / "judge-run"
        for directory in (self.generation, self.judge_request, self.judge_run):
            directory.mkdir()
        self.output_directory = self.root / "published"
        self.output_directory.mkdir()
        self.worker_path = self.root / "worker.py"
        self.worker_path.write_text("# frozen test worker\n", encoding="utf-8")
        self.worker_sha256 = sha256_file(self.worker_path)
        self.expected_platform = "carla"
        self.files = {
            name: self.root / name
            for name in (
                "freeze.json",
                "library.jsonl",
                "oracle.jsonl",
                "roster.json",
                "method.json",
                "semantic_evidence.jsonl",
                "scores.jsonl",
                "semantic_aggregate.json",
                "uqh_assessments.jsonl",
                "uqh_registry.json",
                "cpd.json",
                "coverage.json",
                "runtime.jsonl",
                "runtime_aggregate.json",
                "platform.json",
                "controller.json",
            )
        }
        self._write(self.generation / "evidence.jsonl", [{}])
        self._write(self.generation / "generation_run_manifest.json", {})
        self._write(self.judge_request / "request_manifest.json", {})
        self._write(self.judge_run / "execution_index.jsonl", [{}])
        self._write(self.judge_run / "judge_responses.jsonl", [{}])
        self._write(
            self.judge_run / "judge_run_manifest.json",
            {"request_manifest": {"path": str(self.judge_request / "request_manifest.json")}},
        )
        self._write(self.files["library.jsonl"], [{}])
        self._write(self.files["oracle.jsonl"], [{}])
        self._write(
            self.files["roster.json"],
            {"decision_status": "confirmed", "method_id": "m", "platform": "carla"},
        )
        self._write(
            self.files["method.json"],
            {
                "status": "frozen",
                "method_id": "m",
                "platform": "carla",
                "implementation_bundle": {"bundle_sha256": SHA_B},
            },
        )
        config_sha = sha256_file(self.files["method.json"])
        self.response = {
            "run_id": "r",
            "query_id": "q",
            "method_id": "m",
            "platform": "carla",
            "config_sha256": config_sha,
        }
        self.evidence = dict(self.response)
        self.score = dict(self.response)
        self.runtime = dict(self.response)
        self.worker = {"worker": str(self.worker_path)}
        self.controller = {
            "implementation": {
                "source_path": str(self.worker_path),
                "source_sha256": self.worker_sha256,
            }
        }
        self.platform = {
            "worker": {
                "worker": str(self.worker_path),
                "worker_sha256": self.worker_sha256,
                "interpreter_sha256": SHA_C,
            }
        }
        self.runtime.update(
            {
                "source_config_sha256": config_sha,
                "provenance": {
                    "worker_config_sha256": record_sha256(self.worker),
                    "worker_sha256": self.worker_sha256,
                    "interpreter_sha256": SHA_C,
                },
                "ne": {"controller_config_sha256": record_sha256(self.controller)},
            }
        )
        self._write(self.generation / "response.jsonl", [self.response])
        self._write(self.files["semantic_evidence.jsonl"], [self.evidence])
        self._write(self.files["scores.jsonl"], [self.score])
        self._write(self.files["uqh_assessments.jsonl"], [{}])
        self._write(self.files["uqh_registry.json"], {})
        self._write(self.files["runtime.jsonl"], [self.runtime])
        self._write(self.files["platform.json"], self.platform)
        self._write(self.files["controller.json"], self.controller)
        self.coverage = {"coverage": 0.8}
        self._write(self.files["coverage.json"], self.coverage)
        self.semantic = {
            "srs": 0.9,
            "arc": 0.8,
            "rsc": 0.7,
            "iec_spec": 0.6,
            "diagnostic_availability": {
                key: {
                    "availability": "available",
                    "available_output_count": 1,
                    "output_count": 1,
                    "coverage": 1.0,
                }
                for key in ("arc", "rsc", "iec_spec")
            },
        }
        self.rqs = {"rqs": 0.5, "rqs_mean": 0.6, "vague_gap": 0.1}
        self.uqh = {"uqh": 0.4, "provenance": {}}
        self.semantic_aggregate = {
            "repetition_grid": {"query_count": 1, "record_count": 1, "method_count": 1},
            "semantic": self.semantic,
            "rqs": self.rqs,
            "uqh": {
                "uqh": 0.4,
                "provenance": {
                    "freeze_manifest_sha256": SHA_A,
                    "generation_run_manifest_sha256": SHA_C,
                },
            },
        }
        self._write(self.files["semantic_aggregate.json"], self.semantic_aggregate)
        self.cpd = {
            "cpd_common": {
                "partial": {"macro_mean": 0.2},
                "vague": {"macro_mean": 0.3},
                "joint": {"macro_mean": 0.25},
            },
            "coverage": self.coverage,
            "provenance": {"created_at_utc": "2026-01-01T00:00:00+00:00"},
        }
        self._write(self.files["cpd.json"], self.cpd)
        self.runtime_aggregate = {
            "carla": {
                "eligible_outputs": 1,
                "excluded_outputs": 0,
                "compile_success": 1.0,
                "scene_validity": 0.8,
                "native_executability": 0.7,
                "iec_exec": {
                    "status": "unavailable",
                    "coverage": "none",
                    "available_outputs": 0,
                    "trace_outputs": 0,
                    "eligible_outputs": 1,
                    "reason": "frozen_query_blind_event_observer_not_registered",
                },
            }
        }
        self._write(self.files["runtime_aggregate.json"], self.runtime_aggregate)
        roles = [
            ("query_library_test", self.files["library.jsonl"]),
            ("requirement_oracle_test", self.files["oracle.jsonl"]),
            ("query_roster", self.files["roster.json"]),
            ("method_config", self.files["method.json"]),
            ("uqh_assessor_registry", self.files["uqh_registry.json"]),
            ("cpd_policy", self.files["coverage.json"]),
            ("platform_config_carla", self.files["platform.json"]),
            ("controller_config_carla", self.files["controller.json"]),
        ]
        self.manifest = {
            "manifest_sha256": SHA_A,
            "assets": [
                {"role": role, "path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}
                for role, path in roles
            ],
        }
        self._write(self.files["freeze.json"], self.manifest)
        self.paths = MethodResultPaths(
            freeze_manifest=self.files["freeze.json"],
            library=self.files["library.jsonl"],
            oracle=self.files["oracle.jsonl"],
            roster=self.files["roster.json"],
            method_config=self.files["method.json"],
            generation_dir=self.generation,
            responses=self.generation / "response.jsonl",
            semantic_evidence=self.files["semantic_evidence.jsonl"],
            semantic_scores=self.files["scores.jsonl"],
            judge_run_dir=self.judge_run,
            semantic_aggregate=self.files["semantic_aggregate.json"],
            uqh_assessments=self.files["uqh_assessments.jsonl"],
            uqh_assessor_registry=self.files["uqh_registry.json"],
            cpd_aggregate=self.files["cpd.json"],
            cpd_coverage=self.files["coverage.json"],
            runtime_records=self.files["runtime.jsonl"],
            runtime_aggregate=self.files["runtime_aggregate.json"],
            platform_config=self.files["platform.json"],
            controller_config=self.files["controller.json"],
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, path, value):
        if isinstance(value, list):
            payload = b"".join(canonical_json_bytes(item) + b"\n" for item in value)
        else:
            payload = canonical_json_bytes(value) + b"\n"
        Path(path).write_bytes(payload)

    def _resign(self, result):
        unsigned = dict(result)
        unsigned.pop("result_sha256", None)
        result["result_sha256"] = sha256_bytes(canonical_json_bytes(unsigned))

    def patched_pipeline(self):
        stack = ExitStack()

        def schema_side_effect(value, name, context=None):
            if name == "method_result":
                return real_schema_validate(value, name, context=context)
            return None

        stack.enter_context(mock.patch("bus_benchmark.method_result.verify_freeze_manifest"))
        stack.enter_context(mock.patch("bus_benchmark.method_result.validate_schema_instance", side_effect=schema_side_effect))
        stack.enter_context(mock.patch("bus_benchmark.method_result.validate_schema_records"))
        stack.enter_context(
            mock.patch(
                "bus_benchmark.method_result.validate_records_against_roster",
                return_value={"query_count": 1, "record_count": 1, "method_count": 1},
            )
        )
        stack.enter_context(
            mock.patch(
                "bus_benchmark.method_result.validate_generation_response_chain",
                return_value={
                    "method_id": "m",
                    "platform": self.expected_platform,
                    "record_count": 1,
                    "manifest_sha256": SHA_C,
                    "implementation_bundle_sha256": SHA_B,
                },
            )
        )
        for name in (
            "validate_score_provenance",
            "validate_evidence_response_chain",
            "validate_score_judge_run_provenance",
            "validate_scores_against_evidence",
            "validate_aggregate_response_chain",
        ):
            stack.enter_context(mock.patch("bus_benchmark.method_result." + name))
        stack.enter_context(
            mock.patch(
                "bus_benchmark.method_result.validate_judge_run_bundle",
                return_value=(
                    {"manifest_sha256": SHA_B, "judge_responses_sha256": SHA_C},
                    [],
                ),
            )
        )
        stack.enter_context(
            mock.patch(
                "bus_benchmark.method_result.validate_frozen_judge_response_chain",
                return_value={},
            )
        )
        stack.enter_context(mock.patch("bus_benchmark.method_result.aggregate_semantic_metrics", return_value=self.semantic))
        stack.enter_context(mock.patch("bus_benchmark.method_result.compute_rqs", return_value=self.rqs))
        stack.enter_context(mock.patch("bus_benchmark.method_result.compute_uqh", return_value=copy.deepcopy(self.uqh)))
        stack.enter_context(mock.patch("bus_benchmark.method_result.metric_source_hash", return_value=SHA_A))
        stack.enter_context(mock.patch("bus_benchmark.method_result.recompute_cpd_aggregate", return_value=self.cpd))
        stack.enter_context(mock.patch("bus_benchmark.method_result.validate_platform_runtime_config", return_value=self.worker))
        stack.enter_context(mock.patch("bus_benchmark.method_result.aggregate_runtime", return_value=self.runtime_aggregate))
        return stack

    def _switch_platform(self, platform):
        self.expected_platform = platform
        roster = {"decision_status": "confirmed", "method_id": "m", "platform": platform}
        self._write(self.files["roster.json"], roster)
        method = {
            "status": "frozen",
            "method_id": "m",
            "platform": platform,
            "implementation_bundle": {"bundle_sha256": SHA_B},
        }
        self._write(self.files["method.json"], method)
        config_sha = sha256_file(self.files["method.json"])
        for path in (
            self.generation / "response.jsonl",
            self.files["semantic_evidence.jsonl"],
            self.files["scores.jsonl"],
            self.files["runtime.jsonl"],
        ):
            records = []
            for record in cli_module.read_jsonl(path):
                record["platform"] = platform
                record["config_sha256"] = config_sha
                if path == self.files["runtime.jsonl"]:
                    record["source_config_sha256"] = config_sha
                records.append(record)
            self._write(path, records)
        runtime = self.runtime_aggregate.pop("carla")
        self.runtime_aggregate = {platform: runtime}
        self._write(self.files["runtime_aggregate.json"], self.runtime_aggregate)
        roles = [
            ("query_library_test", self.files["library.jsonl"]),
            ("requirement_oracle_test", self.files["oracle.jsonl"]),
            ("query_roster", self.files["roster.json"]),
            ("method_config", self.files["method.json"]),
            ("uqh_assessor_registry", self.files["uqh_registry.json"]),
            ("cpd_policy", self.files["coverage.json"]),
            ("platform_config_{}".format(platform), self.files["platform.json"]),
            ("controller_config_{}".format(platform), self.files["controller.json"]),
        ]
        self.manifest["assets"] = [
            {
                "role": role,
                "path": str(path),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for role, path in roles
        ]
        self._write(self.files["freeze.json"], self.manifest)

    def test_happy_path_is_one_carla_cell_without_metadrive_result(self):
        with self.patched_pipeline():
            result, _ = build_method_result(self.paths, created_at_utc="2026-07-15T00:00:00+00:00")
        self.assertEqual(result["platform"], "carla")
        self.assertFalse(result["cell_scope"]["cross_platform_completion_required"])
        self.assertEqual(result["primary_metrics"]["rqs"], self.rqs["rqs"])
        self.assertEqual(result["primary_metrics"]["cpd_common"], 0.25)
        self.assertEqual(result["primary_metrics"]["coverage"], 0.8)
        validate_method_result(result)

    def test_formal_entry_keeps_frozen_76_candidates_per_cpd_style(self):
        with self.patched_pipeline(), mock.patch(
            "bus_benchmark.method_result.recompute_cpd_aggregate",
            return_value=self.cpd,
        ) as recompute:
            build_method_result(self.paths)
        self.assertEqual(recompute.call_args.kwargs["expected_per_style"], 76)

    def test_metadrive_happy_path_and_live_verification(self):
        self._switch_platform("metadrive")
        with self.patched_pipeline():
            result, _ = build_method_result(
                self.paths, created_at_utc="2026-07-15T00:00:00+00:00"
            )
            validate_method_result(result, verify_live_inputs=True)
        self.assertEqual(result["platform"], "metadrive")
        self.assertEqual(result["primary_metrics"]["sv"], 0.8)

    def test_static_contract_rejects_role_path_and_cell_identity_forgery(self):
        with self.patched_pipeline():
            result, _ = build_method_result(self.paths)
        mutations = []

        unknown_role = copy.deepcopy(result)
        unknown_role["inputs"][0]["role"] = "unknown_role"
        mutations.append(unknown_role)

        duplicate_role = copy.deepcopy(result)
        duplicate_role["inputs"][0]["role"] = duplicate_role["inputs"][1]["role"]
        mutations.append(duplicate_role)

        relative_path = copy.deepcopy(result)
        relative_path["inputs"][0]["path"] = "relative.json"
        mutations.append(relative_path)

        canonical = result["inputs"][0]["path"]
        noncanonical_paths = (
            str(Path(canonical).parent) + "/./" + Path(canonical).name,
            str(Path(canonical).parent / "unused") + "/../" + Path(canonical).name,
            "/" + canonical.lstrip("/").replace("/", "//", 1),
            "//" + canonical.lstrip("/"),
        )
        for path in noncanonical_paths:
            forged_path = copy.deepcopy(result)
            forged_path["inputs"][0]["path"] = path
            mutations.append(forged_path)

        duplicate_path = copy.deepcopy(result)
        duplicate_path["inputs"][0]["path"] = duplicate_path["inputs"][1]["path"]
        mutations.append(duplicate_path)

        forged_cell = copy.deepcopy(result)
        forged_cell["cell_id"] = SHA_C
        mutations.append(forged_cell)

        for forged in mutations:
            forged["provenance"]["input_set_sha256"] = sha256_bytes(
                canonical_json_bytes(forged["inputs"])
            )
            self._resign(forged)
            with self.assertRaises(ValidationError):
                validate_method_result(forged)

    def test_live_capture_rejects_public_hardlink_alias(self):
        source = self.files["coverage.json"]
        target = self.files["semantic_aggregate.json"]
        target.unlink()
        os.link(source, target)
        with self.patched_pipeline(), self.assertRaisesRegex(
            ValidationError, "distinct file identities"
        ):
            build_method_result(self.paths)

    def test_transient_rename_restore_of_transitive_leaf_is_rejected(self):
        run_directory = self.generation / "runs" / "r"
        run_directory.mkdir(parents=True)
        leaf = run_directory / "capture.bin"
        leaf.write_bytes(b"stable")

        def rename_restore(*args, **kwargs):
            backup = leaf.with_suffix(".backup")
            leaf.rename(backup)
            leaf.write_bytes(b"transient")
            leaf.unlink()
            backup.rename(leaf)

        with self.patched_pipeline() as stack:
            stack.enter_context(
                mock.patch(
                    "bus_benchmark.method_result.verify_freeze_manifest",
                    side_effect=rename_restore,
                )
            )
            with self.assertRaisesRegex(ValidationError, "changed"):
                build_method_result(self.paths)

    def test_transient_parent_symlink_swap_is_rejected(self):
        runs = self.generation / "runs"
        run_directory = runs / "r"
        run_directory.mkdir(parents=True)
        (run_directory / "capture.bin").write_bytes(b"stable")

        def swap_restore(*args, **kwargs):
            original = self.generation / "runs-original"
            runs.rename(original)
            runs.symlink_to(original, target_is_directory=True)
            runs.unlink()
            original.rename(runs)

        with self.patched_pipeline() as stack:
            stack.enter_context(
                mock.patch(
                    "bus_benchmark.method_result.verify_freeze_manifest",
                    side_effect=swap_restore,
                )
            )
            with self.assertRaisesRegex(ValidationError, "parent changed"):
                build_method_result(self.paths)

    def test_transient_deep_ancestor_symlink_swap_is_rejected(self):
        ancestor = self.generation / "runs" / "r"
        leaf = ancestor / "deep-a" / "deep-b" / "capture.bin"
        leaf.parent.mkdir(parents=True)
        leaf.write_bytes(b"stable")

        def swap_restore(*args, **kwargs):
            original = ancestor.with_name("r-original")
            ancestor.rename(original)
            ancestor.symlink_to(original, target_is_directory=True)
            ancestor.unlink()
            original.rename(ancestor)

        with self.patched_pipeline() as stack:
            stack.enter_context(
                mock.patch(
                    "bus_benchmark.method_result.verify_freeze_manifest",
                    side_effect=swap_restore,
                )
            )
            with self.assertRaisesRegex(ValidationError, "parent changed"):
                build_method_result(self.paths)

    def test_bound_raw_json_cannot_turn_attempt_path_into_directory_scan(self):
        artifact = self.root / "untrusted-artifact.json"
        self._write(artifact, {"attempt_path": "/"})
        response = dict(
            self.response,
            raw_artifact={
                "path": str(artifact),
                "sha256": sha256_file(artifact),
                "bytes": artifact.stat().st_size,
            },
        )
        self._write(self.generation / "response.jsonl", [response])
        with self.patched_pipeline():
            _, snapshots = build_method_result(self.paths)
        self.assertIn(str(artifact), snapshots.guards)
        self.assertNotIn("/", snapshots.directory_inventories)

    def test_runtime_attempt_cannot_scan_outside_runtime_record_directory(self):
        runtime = copy.deepcopy(self.runtime)
        runtime["provenance"]["attempt_path"] = "/home/shijie20"
        self._write(self.files["runtime.jsonl"], [runtime])
        with self.patched_pipeline(), self.assertRaisesRegex(
            ValidationError, "escapes the bound runtime-record directory"
        ):
            build_method_result(self.paths)

    def test_directory_inventory_limit_counts_empty_directories(self):
        wide = self.root / "wide-empty-tree"
        wide.mkdir()
        for index in range(6):
            (wide / "empty-{}".format(index)).mkdir()
        with mock.patch.object(
            method_result_module, "_MAX_DIRECTORY_INVENTORY_ENTRIES", 5
        ), self.assertRaisesRegex(ValidationError, "inventory-entry limit"):
            method_result_module._enumerate_directory(wide, "wide empty fixture")

    def test_wide_directory_hits_raw_scan_limit_before_sorting(self):
        wide = self.root / "raw-wide-tree"
        wide.mkdir()
        for index in range(20):
            (wide / "empty-{:02d}".format(index)).mkdir()
        real_scandir = os.scandir
        observed = {"yielded": 0}

        class CountingScandir:
            def __init__(self, path):
                self._iterator = real_scandir(path)

            def __enter__(self):
                self._iterator.__enter__()
                return self

            def __exit__(self, *args):
                return self._iterator.__exit__(*args)

            def __iter__(self):
                return self

            def __next__(self):
                entry = next(self._iterator)
                observed["yielded"] += 1
                return entry

        with mock.patch.object(
            method_result_module, "_MAX_DIRECTORY_SCANNED_ENTRIES", 5
        ), mock.patch(
            "bus_benchmark.method_result.os.scandir",
            side_effect=CountingScandir,
        ), self.assertRaisesRegex(ValidationError, "scanned-entry limit"):
            method_result_module._enumerate_directory(wide, "raw wide fixture")
        self.assertEqual(observed["yielded"], 6)

    def test_metadrive_closure_uses_only_git_tracked_inventory(self):
        repository = self.root / "metadrive-repository"
        source_root = repository / "metadrive"
        source_root.mkdir(parents=True)
        tracked = source_root / "tracked.py"
        tracked_bytecode = source_root / "tracked.pyc"
        ignored = source_root / "ignored.pyc"
        tracked.write_bytes(b"tracked")
        tracked_bytecode.write_bytes(b"tracked bytecode")
        ignored.write_bytes(b"ignored")
        binding = {
            "repository_path": str(repository),
            "revision": "8" * 40,
            "tracked_subpath": "metadrive",
            "tracked_tree_sha256": SHA_A,
            "tracked_file_count": 2,
            "tracked_bytes": (
                tracked.stat().st_size + tracked_bytecode.stat().st_size
            ),
            "policy": "git_tracked_worktree_bytes_v0.1",
        }
        snapshots = method_result_module._InputSnapshots()
        observed = (
            binding,
            {"metadrive/tracked.py", "metadrive/tracked.pyc"},
        )
        with mock.patch(
            "bus_benchmark.method_result._metadrive_tracked_source_snapshot",
            return_value=observed,
        ):
            method_result_module._capture_metadrive_tree(
                binding, "MetaDrive tracked source", snapshots
            )
            _recheck_snapshots(snapshots)
        self.assertIn(str(tracked), snapshots.guards)
        self.assertIn(str(tracked_bytecode), snapshots.guards)
        self.assertNotIn(str(ignored), snapshots.guards)
        self.assertEqual(
            snapshots.directory_inventories[str(source_root)],
            ("f:tracked.py", "f:tracked.pyc"),
        )

    def test_post_link_input_drift_removes_only_new_output(self):
        output = self.output_directory / "post-link-drift.json"
        original_write = cli_module.publish_method_result.__globals__["_write_exclusive"]

        def write_then_drift(path, payload):
            identity = original_write(path, payload)
            with self.files["semantic_evidence.jsonl"].open("ab") as stream:
                stream.write(b"\n")
            return identity

        with self.patched_pipeline(), mock.patch(
            "bus_benchmark.method_result._write_exclusive",
            side_effect=write_then_drift,
        ), self.assertRaisesRegex(ValidationError, "changed during validation"):
            publish_method_result(self.paths, output)
        self.assertFalse(output.exists())

    def test_post_check_parent_symlink_swap_leaves_no_redirected_result(self):
        output = self.output_directory / "parent-swap.json"
        original_parent = self.root / "published-original"
        redirected_parent = self.root / "redirected"
        redirected_parent.mkdir()
        original_write = method_result_module._write_exclusive

        def write_then_swap(path, payload):
            transaction = original_write(path, payload)
            self.output_directory.rename(original_parent)
            self.output_directory.symlink_to(redirected_parent, target_is_directory=True)
            return transaction

        try:
            with self.patched_pipeline(), mock.patch(
                "bus_benchmark.method_result._write_exclusive",
                side_effect=write_then_swap,
            ), self.assertRaisesRegex(
                ValidationError, "output parent (path changed|must be reachable)"
            ):
                publish_method_result(self.paths, output)
            self.assertFalse((original_parent / output.name).exists())
            self.assertFalse((redirected_parent / output.name).exists())
        finally:
            if self.output_directory.is_symlink():
                self.output_directory.unlink()
            if original_parent.exists():
                original_parent.rename(self.output_directory)

    def test_output_parent_volatile_ancestor_metadata_is_ignored_not_identity(self):
        directory_mode = 0o40700
        expected = (
            ("/", (1, 10, directory_mode, 2, 100, 1, 1)),
            ("/tmp", (1, 20, directory_mode, 2, 200, 2, 2)),
            ("/tmp/private", (1, 30, directory_mode, 2, 300, 3, 3)),
            ("/tmp/private/output", (1, 40, directory_mode, 2, 400, 4, 4)),
        )
        volatile_metadata_changed = (
            ("/", (1, 10, directory_mode, 9, 999, 9, 9)),
            ("/tmp", (1, 20, directory_mode, 8, 888, 8, 8)),
            expected[2],
            expected[3],
        )
        transaction = method_result_module._OutputTransaction(
            path=Path("/tmp/private/output/result.json"),
            parent_fd=101,
            parent_fingerprints=expected,
            output_name="result.json",
            payload=b"{}\n",
            published_identity=(50, 60),
        )
        same_parent = mock.Mock(st_dev=1, st_ino=40, st_mode=directory_mode)

        def assert_current(fingerprints):
            with mock.patch(
                "bus_benchmark.method_result._open_directory_no_follow",
                return_value=(202, fingerprints),
            ), mock.patch(
                "bus_benchmark.method_result.os.fstat",
                side_effect=(same_parent, same_parent),
            ), mock.patch("bus_benchmark.method_result.os.close"):
                method_result_module._assert_output_parent_current(transaction)

        assert_current(volatile_metadata_changed)
        identity_changed = list(volatile_metadata_changed)
        identity_changed[1] = (
            "/tmp",
            (1, 21, directory_mode, 8, 888, 8, 8),
        )
        with self.assertRaisesRegex(ValidationError, "ancestry identity changed"):
            assert_current(tuple(identity_changed))

    def test_successful_link_never_unlinks_replaced_stage_name(self):
        output = self.output_directory / "stage-success.json"
        payload = b'{"status":"complete"}\n'
        foreign_payload = b"foreign stage after link\n"
        original_link = os.link
        injected = {}

        def link_then_replace(source, destination, **kwargs):
            result = original_link(source, destination, **kwargs)
            if source.startswith(".method-result-stage-") and not injected:
                directory_fd = kwargs["src_dir_fd"]
                retained_name = ".method-result-owned-stage-success-test"
                os.rename(
                    source,
                    retained_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                descriptor = os.open(
                    source,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    os.write(descriptor, foreign_payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                injected.update(stage_name=source, retained_name=retained_name)
            return result

        with mock.patch(
            "bus_benchmark.method_result.os.link", side_effect=link_then_replace
        ):
            transaction = method_result_module._write_exclusive(output, payload)
        transaction.close()
        self.assertEqual(output.read_bytes(), payload)
        stage = self.output_directory / injected["stage_name"]
        retained = self.output_directory / injected["retained_name"]
        self.assertEqual(stage.read_bytes(), foreign_payload)
        self.assertEqual(retained.read_bytes(), payload)
        self.assertEqual(output.stat().st_ino, retained.stat().st_ino)

    def test_link_conflict_never_unlinks_replaced_stage_name(self):
        output = self.output_directory / "stage-conflict.json"
        output.write_bytes(b"existing output\n")
        payload = b'{"status":"new"}\n'
        foreign_payload = b"foreign stage after link conflict\n"
        original_link = os.link
        injected = {}

        def link_then_replace(source, destination, **kwargs):
            try:
                return original_link(source, destination, **kwargs)
            except FileExistsError:
                directory_fd = kwargs["src_dir_fd"]
                retained_name = ".method-result-owned-stage-conflict-test"
                os.rename(
                    source,
                    retained_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                descriptor = os.open(
                    source,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    os.write(descriptor, foreign_payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                injected.update(stage_name=source, retained_name=retained_name)
                raise

        with mock.patch(
            "bus_benchmark.method_result.os.link",
            side_effect=link_then_replace,
        ), self.assertRaisesRegex(ValidationError, "output already exists"):
            method_result_module._write_exclusive(output, payload)
        self.assertEqual(output.read_bytes(), b"existing output\n")
        self.assertEqual(
            (self.output_directory / injected["stage_name"]).read_bytes(),
            foreign_payload,
        )
        self.assertEqual(
            (self.output_directory / injected["retained_name"]).read_bytes(),
            payload,
        )

    def test_cleanup_never_deletes_foreign_public_replacement(self):
        output = self.output_directory / "foreign-replacement.json"
        foreign_payload = b"foreign\n"
        original_write = method_result_module._write_exclusive
        original_read = method_result_module._read_file_at
        injected = {"done": False}

        def write_then_drift(path, payload):
            transaction = original_write(path, payload)
            with self.files["semantic_evidence.jsonl"].open("ab") as stream:
                stream.write(b"\n")
            return transaction

        def read_then_replace(directory_fd, name):
            observed = original_read(directory_fd, name)
            if name.startswith(".method-result-quarantine-") and not injected["done"]:
                descriptor = os.open(
                    output.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    os.write(descriptor, foreign_payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                injected["done"] = True
            return observed

        with self.patched_pipeline(), mock.patch(
            "bus_benchmark.method_result._write_exclusive", side_effect=write_then_drift
        ), mock.patch(
            "bus_benchmark.method_result._read_file_at", side_effect=read_then_replace
        ), self.assertRaisesRegex(ValidationError, "changed during validation"):
            publish_method_result(self.paths, output)
        self.assertTrue(injected["done"])
        self.assertEqual(output.read_bytes(), foreign_payload)

    def test_cleanup_never_unlinks_quarantine_name_replaced_after_verification(self):
        output = self.output_directory / "quarantine-replacement.json"
        foreign_payload = b"foreign quarantine replacement\n"
        original_write = method_result_module._write_exclusive
        original_read = method_result_module._read_file_at
        injected = {}

        def write_then_drift(path, payload):
            transaction = original_write(path, payload)
            with self.files["semantic_evidence.jsonl"].open("ab") as stream:
                stream.write(b"\n")
            return transaction

        def read_then_replace(directory_fd, name):
            observed = original_read(directory_fd, name)
            if name.startswith(".method-result-quarantine-") and not injected:
                retained_name = ".method-result-owned-retained-test"
                os.rename(
                    name,
                    retained_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    os.write(descriptor, foreign_payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                injected.update(
                    quarantine_name=name,
                    retained_name=retained_name,
                )
            return observed

        with self.patched_pipeline(), mock.patch(
            "bus_benchmark.method_result._write_exclusive", side_effect=write_then_drift
        ), mock.patch(
            "bus_benchmark.method_result._read_file_at", side_effect=read_then_replace
        ), self.assertRaisesRegex(ValidationError, "changed during validation"):
            publish_method_result(self.paths, output)
        self.assertFalse(output.exists())
        self.assertEqual(
            (self.output_directory / injected["quarantine_name"]).read_bytes(),
            foreign_payload,
        )
        self.assertTrue((self.output_directory / injected["retained_name"]).is_file())

    def test_forged_child_aggregate_is_rejected(self):
        forged = copy.deepcopy(self.semantic_aggregate)
        forged["semantic"]["srs"] = 0.1
        self._write(self.files["semantic_aggregate.json"], forged)
        with self.patched_pipeline(), self.assertRaisesRegex(ValidationError, "recomputed"):
            build_method_result(self.paths)

    def test_other_platform_is_rejected(self):
        foreign = copy.deepcopy(self.response)
        foreign["platform"] = "metadrive"
        self._write(self.generation / "response.jsonl", [foreign])
        with self.patched_pipeline(), self.assertRaisesRegex(ValidationError, "mixes another"):
            build_method_result(self.paths)

    def test_other_method_is_rejected(self):
        foreign = copy.deepcopy(self.response)
        foreign["method_id"] = "other-method"
        self._write(self.generation / "response.jsonl", [foreign])
        with self.patched_pipeline(), self.assertRaisesRegex(ValidationError, "mixes another"):
            build_method_result(self.paths)

    def test_other_main_config_is_rejected(self):
        foreign = copy.deepcopy(self.response)
        foreign["config_sha256"] = SHA_A
        self._write(self.generation / "response.jsonl", [foreign])
        with self.patched_pipeline(), self.assertRaisesRegex(ValidationError, "main method config"):
            build_method_result(self.paths)

    def test_forged_cpd_aggregate_is_rejected(self):
        forged = copy.deepcopy(self.cpd)
        forged["cpd_common"]["joint"]["macro_mean"] = 0.99
        self._write(self.files["cpd.json"], forged)
        with self.patched_pipeline(), self.assertRaisesRegex(ValidationError, "CPD aggregate"):
            build_method_result(self.paths)

    def test_forged_runtime_aggregate_is_rejected(self):
        forged = copy.deepcopy(self.runtime_aggregate)
        forged["carla"]["scene_validity"] = 0.01
        self._write(self.files["runtime_aggregate.json"], forged)
        with self.patched_pipeline(), self.assertRaisesRegex(ValidationError, "runtime aggregate"):
            build_method_result(self.paths)

    def test_incomplete_primary_metric_is_rejected(self):
        incomplete = dict(self.semantic, arc=None)
        child = copy.deepcopy(self.semantic_aggregate)
        child["semantic"] = incomplete
        self._write(self.files["semantic_aggregate.json"], child)
        with self.patched_pipeline() as stack:
            stack.enter_context(mock.patch("bus_benchmark.method_result.aggregate_semantic_metrics", return_value=incomplete))
            with self.assertRaisesRegex(ValidationError, "ARC must be numeric"):
                build_method_result(self.paths)

    def test_changed_input_binding_is_rejected(self):
        snapshot = _snapshot_file(self.files["coverage.json"], "coverage")
        self.files["coverage.json"].write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "changed during validation"):
            _recheck_snapshots([snapshot])

    def test_duplicate_output_is_rejected(self):
        output = self.output_directory / "result.json"
        with self.patched_pipeline():
            publish_method_result(self.paths, output)
            with self.assertRaisesRegex(ValidationError, "already exists"):
                publish_method_result(self.paths, output)

    def test_drift_between_reconstructions_prevents_publication(self):
        output = self.output_directory / "drifted-result.json"
        calls = {"count": 0}

        def drift_on_second_validation(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                with self.files["semantic_evidence.jsonl"].open("ab") as stream:
                    stream.write(b"\n")

        with self.patched_pipeline(), mock.patch(
            "bus_benchmark.method_result.verify_freeze_manifest",
            side_effect=drift_on_second_validation,
        ), self.assertRaisesRegex(ValidationError, "changed during validation|pre-publication"):
            publish_method_result(self.paths, output)
        self.assertFalse(output.exists())

    def test_total_and_available_iec_exec_are_schema_rejected(self):
        with self.patched_pipeline():
            result, _ = build_method_result(self.paths)
        for mutate in (
            lambda value: value.update({"total": 0.9}),
            lambda value: value["diagnostics"]["iec_exec"].update({"status": "available"}),
        ):
            forged = copy.deepcopy(result)
            mutate(forged)
            unsigned = dict(forged)
            unsigned.pop("result_sha256")
            forged["result_sha256"] = sha256_bytes(canonical_json_bytes(unsigned))
            with self.assertRaises(ValidationError):
                validate_method_result(forged)

    def test_live_verifier_rejects_metric_forgery_even_with_new_self_hash(self):
        with self.patched_pipeline():
            result, _ = build_method_result(self.paths)
            forged = copy.deepcopy(result)
            forged["primary_metrics"]["srs"] = 0.1
            unsigned = dict(forged)
            unsigned.pop("result_sha256")
            forged["result_sha256"] = sha256_bytes(canonical_json_bytes(unsigned))
            with self.assertRaisesRegex(ValidationError, "envelope differs"):
                validate_method_result(forged, verify_live_inputs=True)

    def test_cli_has_no_bare_judge_response_input(self):
        choices = build_parser()._subparsers._group_actions[0].choices
        command = choices["method-result"]
        options = {option for action in command._actions for option in action.option_strings}
        self.assertNotIn("--judge-responses", options)
        self.assertIn("method-result-verify", choices)

    def test_cli_maps_every_method_result_path_and_live_verify_flag(self):
        arguments = ["method-result"]
        option_values = {
            "freeze-manifest": self.paths.freeze_manifest,
            "library": self.paths.library,
            "oracle": self.paths.oracle,
            "roster": self.paths.roster,
            "method-config": self.paths.method_config,
            "generation-dir": self.paths.generation_dir,
            "responses": self.paths.responses,
            "semantic-evidence": self.paths.semantic_evidence,
            "semantic-scores": self.paths.semantic_scores,
            "judge-run-dir": self.paths.judge_run_dir,
            "semantic-aggregate": self.paths.semantic_aggregate,
            "uqh-assessments": self.paths.uqh_assessments,
            "uqh-assessor-registry": self.paths.uqh_assessor_registry,
            "cpd-aggregate": self.paths.cpd_aggregate,
            "cpd-coverage": self.paths.cpd_coverage,
            "runtime-records": self.paths.runtime_records,
            "runtime-aggregate": self.paths.runtime_aggregate,
            "platform-config": self.paths.platform_config,
            "controller-config": self.paths.controller_config,
            "output": self.output_directory / "cli-result.json",
        }
        for option, value in option_values.items():
            arguments.extend(("--" + option, str(value)))
        namespace = build_parser().parse_args(arguments)
        published = {
            "cell_id": SHA_A,
            "method_id": "m",
            "platform": "carla",
            "result_sha256": SHA_B,
        }
        with mock.patch(
            "bus_benchmark.cli.publish_method_result", return_value=published
        ) as publish, mock.patch("bus_benchmark.cli._emit"):
            namespace.function(namespace)
        published_paths, published_output = publish.call_args.args
        self.assertEqual(published_paths, self.paths)
        self.assertEqual(published_output, option_values["output"])

        verify_input = self.output_directory / "verify.json"
        verify_namespace = build_parser().parse_args(
            ["method-result-verify", "--input", str(verify_input)]
        )
        with mock.patch(
            "bus_benchmark.cli.read_json", return_value=published
        ), mock.patch("bus_benchmark.cli.validate_method_result") as verify, mock.patch(
            "bus_benchmark.cli._emit"
        ):
            verify_namespace.function(verify_namespace)
        verify.assert_called_once_with(published, verify_live_inputs=True)


if __name__ == "__main__":
    unittest.main()
