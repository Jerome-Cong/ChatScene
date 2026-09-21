import copy
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from bus_benchmark.adapters import AdapterOutcome
from bus_benchmark.cli import build_parser, main
from bus_benchmark.generation import (
    ensure_immutable_jsonl,
    implementation_bundle_sha256,
    run_generation,
)
from bus_benchmark.jsonio import (
    read_json,
    sha256_file,
    write_json,
    write_jsonl,
)


SCENIC_SOURCE = """param map = localPath('../maps/Town05.xodr')
model scenic.simulators.carla.model
EGO_MODEL = "vehicle.lincoln.mkz_2017"
ego = Car at 0@0,
    with regionContainedIn None,
    with blueprint EGO_MODEL
other = Pedestrian at 8@1, with regionContainedIn None
"""


class _FixtureAdapter:
    def generate(self, query_text, workdir):
        workdir.mkdir(parents=True, exist_ok=False)
        artifact = workdir / "scene.scenic"
        artifact.write_text(SCENIC_SOURCE, encoding="utf-8")
        return AdapterOutcome(
            stdout=query_text.encode("utf-8"),
            stderr=b"",
            exit_code=0,
            timed_out=False,
            disposition="generate",
            artifact_path=artifact,
        )


class ScoreCpdGenerationChainTests(unittest.TestCase):
    def test_cpd_cli_exposes_frozen_coverage_input(self):
        args = build_parser().parse_args(
            [
                "cpd",
                "--input",
                "evidence.jsonl",
                "--oracle",
                "oracle.jsonl",
                "--scores",
                "scores.jsonl",
                "--coverage",
                "coverage.json",
                "--roster",
                "roster.json",
                "--development",
                "--allow-unverified-generation-chain",
            ]
        )
        self.assertEqual(args.coverage, "coverage.json")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._build_committed_generation_chain()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _binding(path):
        path = Path(path).resolve()
        return {
            "path": str(path),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }

    def _build_committed_generation_chain(self):
        self.library = self.root / "library.jsonl"
        write_jsonl(
            self.library,
            [
                {
                    "query_id": "query-partial",
                    "query_text": "A bus scene with one pedestrian.",
                    "intent_group_id": "intent-1",
                    "surface_style": "partial",
                    "expected_support": "supported",
                }
            ],
        )
        roster = {
            "schema_version": "0.1",
            "decision_status": "confirmed",
            "method_id": "method-a",
            "platform": "carla",
            "library_path": str(self.library),
            "library_sha256": sha256_file(self.library),
            "query_count": 1,
            "expected_response_count": 5,
            "repetitions": [0, 1, 2, 3, 4],
            "queries": [
                {
                    "query_id": "query-partial",
                    "intent_group_id": "intent-1",
                    "statistical_intent_cluster_id": "cluster-1",
                    "surface_style": "partial",
                    "expected_support": "supported",
                }
            ],
        }
        self.roster = self.root / "roster.json"
        write_json(self.roster, roster)

        executable = Path(sys.executable).resolve()
        executable_binding = self._binding(executable)
        environment = {
            "schema_version": "0.1",
            "decision_status": "draft",
            "runtime_kind": "python",
            "executable": executable_binding,
            "runtime_version": sys.version.split()[0],
            "locked_dependencies": [],
        }
        environment_path = self.root / "environment.json"
        write_json(environment_path, environment)
        harness_environment_path = self.root / "harness-environment.json"
        write_json(harness_environment_path, environment)
        bundle = {
            "status": "draft",
            "bundle_id": "score-chain-test-v0.1",
            "bundle_sha256": "0" * 64,
            "environment_manifest": self._binding(environment_path),
            "harness_environment_manifest": self._binding(
                harness_environment_path
            ),
            "files": [executable_binding],
        }
        bundle["bundle_sha256"] = implementation_bundle_sha256(bundle)
        config = {
            "schema_version": "0.1",
            "status": "draft",
            "method_id": "method-a",
            "platform": "carla",
            "query_input_fields": ["query_text"],
            "post_output_semantic_repair": False,
            "implementation_bundle": bundle,
            "adapter": {
                "type": "command",
                "argv": [str(executable), "-c", "pass"],
                "artifact_path": "scene.scenic",
                "timeout_seconds": 2,
                "output_protocol": "artifact_on_zero",
                "environment_passthrough": [],
                "max_stdout_bytes": 1024,
                "max_stderr_bytes": 1024,
                "max_artifact_bytes": 65536,
            },
            "finalization": {
                "policy_id": "carla_ego_proxy_v0.1",
                "ego_blueprint": "vehicle.chevrolet.impala",
                "ego_length_m": 5.33,
                "ego_width_m": 2.1,
            },
        }
        self.method_config = self.root / "method.json"
        write_json(self.method_config, config)
        self.generation_dir = self.root / "generation"
        self.responses = run_generation(
            self.library,
            roster,
            self.method_config,
            self.generation_dir,
            adapter=_FixtureAdapter(),
            development=True,
        )
        generation_evidence = [
            read_json(
                Path(response["raw_method_response"]["stdout"]["path"]).parent
                / "generation_evidence.json"
            )
            for response in self.responses
        ]
        ensure_immutable_jsonl(
            self.generation_dir / "response.jsonl", self.responses
        )
        ensure_immutable_jsonl(
            self.generation_dir / "evidence.jsonl", generation_evidence
        )

    def _chain_arguments(self, command, responses):
        arguments = [
            command,
            "--responses",
            str(responses),
            "--roster",
            str(self.roster),
            "--library",
            str(self.library),
            "--method-config",
            str(self.method_config),
            "--generation-dir",
            str(self.generation_dir),
            "--development",
        ]
        if command == "score":
            arguments.extend(
                [
                    "--oracle",
                    str(self.root / "unused-oracle.jsonl"),
                    "--evidence",
                    str(self.root / "unused-semantic-evidence.jsonl"),
                    "--output",
                    str(self.root / "unused-scores.jsonl"),
                ]
            )
        else:
            arguments.extend(
                [
                    "--input",
                    str(self.root / "unused-semantic-evidence.jsonl"),
                    "--oracle",
                    str(self.root / "unused-oracle.jsonl"),
                    "--scores",
                    str(self.root / "unused-scores.jsonl"),
                    "--output",
                    str(self.root / "unused-cpd.json"),
                ]
            )
        return arguments

    def test_score_rejects_forged_response_before_semantic_scoring(self):
        forged = copy.deepcopy(self.responses)
        forged[0]["artifact"]["sha256"] = "0" * 64
        forged_path = self.root / "forged-responses.jsonl"
        write_jsonl(forged_path, forged)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = main(self._chain_arguments("score", forged_path))
        self.assertEqual(result, 2)
        self.assertIn("differs from committed generation records", stderr.getvalue())

    def test_cpd_rejects_forged_artifact_before_semantic_scoring(self):
        artifact = Path(self.responses[0]["artifact"]["path"])
        artifact.write_bytes(artifact.read_bytes() + b"\n# forged\n")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = main(
                self._chain_arguments(
                    "cpd", self.generation_dir / "response.jsonl"
                )
            )
        self.assertEqual(result, 2)
        self.assertIn("generation evidence hash mismatch", stderr.getvalue().lower())

    def test_formal_score_and_cpd_require_generation_chain_inputs(self):
        commands = {
            "score": [
                "--oracle",
                "missing-oracle.jsonl",
                "--evidence",
                "missing-evidence.jsonl",
                "--responses",
                "missing-responses.jsonl",
                "--output",
                "missing-scores.jsonl",
                "--roster",
                "missing-roster.json",
            ],
            "cpd": [
                "--input",
                "missing-evidence.jsonl",
                "--oracle",
                "missing-oracle.jsonl",
                "--scores",
                "missing-scores.jsonl",
                "--responses",
                "missing-responses.jsonl",
                "--judge-responses",
                "missing-judge.jsonl",
                "--roster",
                "missing-roster.json",
            ],
        }
        for command, arguments in commands.items():
            with self.subTest(command=command):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    result = main([command, *arguments])
                self.assertEqual(result, 2)
                self.assertIn(
                    "requires --library, --method-config, and --generation-dir",
                    stderr.getvalue(),
                )

    def test_formal_score_requires_the_committed_response_index_path(self):
        copied_responses = self.root / "copied-responses.jsonl"
        write_jsonl(copied_responses, self.responses)
        arguments = self._chain_arguments("score", copied_responses)
        arguments.remove("--development")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = main(arguments)
        self.assertEqual(result, 2)
        self.assertIn(
            "must be the committed response.jsonl under --generation-dir",
            stderr.getvalue(),
        )

    def test_development_omission_requires_explicit_unverified_flag(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = main(
                [
                    "score",
                    "--oracle",
                    "missing-oracle.jsonl",
                    "--evidence",
                    "missing-evidence.jsonl",
                    "--output",
                    "missing-scores.jsonl",
                    "--roster",
                    "missing-roster.json",
                    "--development",
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("--allow-unverified-generation-chain", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
