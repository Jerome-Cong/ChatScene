import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bus_benchmark.cli import _cmd_extract_evidence, build_parser
from bus_benchmark.errors import ValidationError
from bus_benchmark.jsonio import sha256_file, write_json, write_jsonl
from bus_benchmark.provenance import record_sha256
from bus_benchmark.semantic_pipeline import build_frozen_semantic_evidence
from tests.bus_benchmark.test_schema import semantic_evidence, response_record


class FrozenSemanticPipelineTests(unittest.TestCase):
    def setUp(self):
        self.response = response_record()
        expected = semantic_evidence()
        self.projection = {
            field: copy.deepcopy(expected[field])
            for field in ("ego", "atoms", "common_atoms", "complete_categories")
        }
        self.manifest = {
            "assets": [
                {
                    "role": "semantic_extractor_manifest",
                    "sha256": "a" * 64,
                }
            ]
        }

    @mock.patch("bus_benchmark.semantic_pipeline.execute_frozen_extractor")
    @mock.patch("bus_benchmark.semantic_pipeline.load_frozen_extractor_runtime")
    def test_builds_one_source_bound_record_per_response(self, load_runtime, execute):
        runtime = {"carla": {"entrypoint": {}}}
        load_runtime.return_value = (
            {"extractor_id": "frozen-extractor", "extractor_version": "0.2"},
            runtime,
        )
        execute.return_value = self.projection

        records = build_frozen_semantic_evidence(
            [self.response],
            self.manifest,
            created_at_utc="2026-07-14T00:00:00Z",
        )

        self.assertEqual(len(records), 1)
        record = records[0]
        for field in (
            "query_id",
            "run_id",
            "repetition",
            "method_id",
            "platform",
            "intent_group_id",
            "statistical_intent_cluster_id",
            "surface_style",
            "expected_support",
            "terminal_status",
        ):
            self.assertEqual(record[field], self.response[field])
        self.assertEqual(record["provenance"]["source_response_sha256"], record_sha256(self.response))
        self.assertEqual(
            record["provenance"]["source_artifact_sha256"],
            self.response["artifact"]["sha256"],
        )
        self.assertEqual(record["provenance"]["extractor_config_sha256"], "a" * 64)
        execute.assert_called_once_with(runtime, self.response)

    def test_duplicate_run_ids_fail_before_extractor_execution(self):
        with self.assertRaisesRegex(ValidationError, "unique non-empty"):
            build_frozen_semantic_evidence(
                [self.response, copy.deepcopy(self.response)], self.manifest
            )

    def test_empty_response_set_fails_closed(self):
        with self.assertRaisesRegex(ValidationError, "at least one response"):
            build_frozen_semantic_evidence([], self.manifest)

    @mock.patch("bus_benchmark.semantic_pipeline.execute_frozen_extractor")
    @mock.patch("bus_benchmark.semantic_pipeline.load_frozen_extractor_runtime")
    def test_malformed_projection_cannot_be_persisted(self, load_runtime, execute):
        load_runtime.return_value = (
            {"extractor_id": "frozen-extractor", "extractor_version": "0.2"},
            {},
        )
        execute.return_value = {
            "ego": self.projection["ego"],
            "atoms": [],
            "common_atoms": [],
        }
        with self.assertRaisesRegex(ValidationError, "complete_categories"):
            build_frozen_semantic_evidence([self.response], self.manifest)

    @mock.patch("bus_benchmark.cli.validate_evidence_response_chain")
    @mock.patch("bus_benchmark.cli.build_frozen_semantic_evidence")
    @mock.patch("bus_benchmark.cli.validate_generation_response_chain")
    @mock.patch("bus_benchmark.cli._authorize_frozen_assets")
    def test_formal_cli_verifies_chain_then_writes_immutable_index(
        self, authorize, validate_chain, build_evidence, validate_evidence
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generation = root / "generation"
            generation.mkdir()
            library = root / "library.jsonl"
            write_jsonl(
                library,
                [
                    {
                        "query_id": "query-001",
                        "intent_group_id": "intent-001",
                        "surface_style": "partial",
                        "expected_support": "supported",
                        "query_text": "private query sentinel",
                    }
                ],
            )
            responses = []
            evidence = []
            for repetition in range(5):
                response = response_record()
                response.update(
                    {
                        "run_id": "run-{}".format(repetition),
                        "repetition": repetition,
                        "statistical_intent_cluster_id": "cluster-001",
                    }
                )
                responses.append(response)
                item = semantic_evidence()
                item.update(
                    {
                        "run_id": response["run_id"],
                        "repetition": repetition,
                        "statistical_intent_cluster_id": "cluster-001",
                    }
                )
                evidence.append(item)
            responses_path = generation / "response.jsonl"
            write_jsonl(responses_path, responses)
            write_jsonl(generation / "evidence.jsonl", [])
            write_json(generation / "generation_run_manifest.json", {"fixture": True})
            roster = {
                "schema_version": "0.1",
                "decision_status": "confirmed",
                "method_id": "method-a",
                "platform": "carla",
                "library_path": str(library.resolve()),
                "library_sha256": sha256_file(library),
                "query_count": 1,
                "expected_response_count": 5,
                "repetitions": list(range(5)),
                "queries": [
                    {
                        "query_id": "query-001",
                        "intent_group_id": "intent-001",
                        "statistical_intent_cluster_id": "cluster-001",
                        "surface_style": "partial",
                        "expected_support": "supported",
                    }
                ],
            }
            roster_path = root / "roster.json"
            write_json(roster_path, roster)
            method_config = root / "method.json"
            write_json(method_config, {"fixture": True})
            output = root / "semantic_evidence.jsonl"
            manifest = {
                "assets": [
                    {
                        "role": "semantic_extractor_manifest",
                        "sha256": "a" * 64,
                    }
                ]
            }
            call_order = []

            def authorize_assets(*_args, **_kwargs):
                call_order.append("authorize")
                return manifest

            def validate_generation(*_args, **_kwargs):
                call_order.append("generation_chain")
                return {"manifest_sha256": "b" * 64}

            def materialize(*_args, **_kwargs):
                call_order.append("build_evidence")
                return evidence

            def validate_materialized(*_args, **_kwargs):
                call_order.append("validate_evidence")

            authorize.side_effect = authorize_assets
            validate_chain.side_effect = validate_generation
            build_evidence.side_effect = materialize
            validate_evidence.side_effect = validate_materialized
            args = build_parser().parse_args(
                [
                    "extract-evidence",
                    "--responses",
                    str(responses_path),
                    "--output",
                    str(output),
                    "--roster",
                    str(roster_path),
                    "--freeze-manifest",
                    str(root / "freeze.json"),
                    "--library",
                    str(library),
                    "--method-config",
                    str(method_config),
                    "--generation-dir",
                    str(generation),
                ]
            )
            self.assertFalse(hasattr(args, "development"))

            _cmd_extract_evidence(args)

            self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 5)
            authorize.assert_called_once()
            validate_chain.assert_called_once()
            build_evidence.assert_called_once_with(responses, manifest)
            validate_evidence.assert_called_once()
            self.assertEqual(
                call_order,
                [
                    "authorize",
                    "generation_chain",
                    "build_evidence",
                    "validate_evidence",
                ],
            )


if __name__ == "__main__":
    unittest.main()
