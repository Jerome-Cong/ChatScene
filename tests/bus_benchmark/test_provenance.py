import tempfile
import unittest
from pathlib import Path

from bus_benchmark.errors import ValidationError
from bus_benchmark.atoms import make_atom
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
    write_json,
    write_jsonl,
)
from bus_benchmark.provenance import (
    metric_source_hash,
    record_sha256,
    validate_aggregate_response_chain,
    validate_evidence_response_chain,
    validate_score_judge_run_provenance,
    validate_scores_against_evidence,
    validate_score_provenance,
)
from bus_benchmark.schema import supported_schema_paths


class ProvenanceTests(unittest.TestCase):
    def _base(self):
        return {
            "run_id": "run-0",
            "query_id": "q",
            "intent_group_id": "i",
            "statistical_intent_cluster_id": "i",
            "surface_style": "partial",
            "expected_support": "supported",
            "repetition": 0,
            "method_id": "method-a",
            "platform": "carla",
        }

    def test_evidence_is_bound_to_response_config_artifact_and_extractor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            method_config = root / "method.json"
            extractor = root / "extractor.json"
            library = root / "library.jsonl"
            oracle = root / "oracle.jsonl"
            artifact = root / "scene.scenic"
            token_registry = root / "token_registry.json"
            extractor_sources = {
                platform: root / "extractor_{}.py".format(platform)
                for platform in ("carla", "metadrive")
            }
            write_json(method_config, {"method_id": "method-a"})
            write_jsonl(library, [{"query_id": "q", "query_text": "make a scene"}])
            write_jsonl(oracle, [{"query_id": "q", "atoms": []}])
            projection = {
                "ego": {
                    "count": 0,
                    "semantic_role": "missing",
                    "approved_proxy": False,
                    "deterministic": True,
                    "length_m": 0.0,
                    "width_m": 0.0,
                },
                "atoms": [],
                "common_atoms": [],
                "complete_categories": [],
            }
            write_json(artifact, projection)
            write_json(token_registry, {"registry_id": "trusted"})
            for source in extractor_sources.values():
                source.write_text(
                    "import json\n"
                    "def extract(payload):\n"
                    "    return json.loads(payload['artifact']['text'])\n",
                    encoding="utf-8",
                )
            write_json(
                extractor,
                {
                    "decision_status": "confirmed",
                    "extractor_id": "fixture-extractor",
                    "extractor_version": "0.1",
                    "input_contract": "artifact_observation_v0.1",
                    "output_contract": "semantic_projection_v0.1",
                    "entrypoints": [
                        {
                            "platform": platform,
                            "path": str(source),
                            "sha256": sha256_file(source),
                            "callable": "extract",
                        }
                        for platform, source in extractor_sources.items()
                    ],
                },
            )
            manifest = {
                "assets": [
                    {
                        "role": "method_config",
                        "path": str(method_config),
                        "sha256": sha256_file(method_config),
                    },
                    {
                        "role": "semantic_extractor_manifest",
                        "path": str(extractor),
                        "sha256": sha256_file(extractor),
                    },
                    {
                        "role": "metadrive_pg_token_registry",
                        "path": str(token_registry),
                        "sha256": sha256_file(token_registry),
                        "bytes": token_registry.stat().st_size,
                    },
                    {
                        "role": "query_library_test",
                        "path": str(library),
                        "sha256": sha256_file(library),
                    },
                    {
                        "role": "requirement_oracle_test",
                        "path": str(oracle),
                        "sha256": sha256_file(oracle),
                    },
                ]
            }
            response = {
                **self._base(),
                "terminal_status": "complete",
                "disposition": "generate",
                "config_sha256": sha256_file(method_config),
                "request_sha256": sha256_bytes(
                    canonical_json_bytes({"query_text": "make a scene"})
                ),
                "artifact": {
                    "path": str(artifact),
                    "sha256": sha256_file(artifact),
                    "bytes": artifact.stat().st_size,
                },
            }
            evidence = {
                **self._base(),
                "terminal_status": "complete",
                **projection,
                "provenance": {
                    "source_response_sha256": record_sha256(response),
                    "source_artifact_sha256": sha256_file(artifact),
                    "extractor_id": "fixture-extractor",
                    "extractor_version": "0.1",
                    "extractor_config_sha256": sha256_file(extractor),
                },
            }
            validate_evidence_response_chain(
                [evidence], [response], manifest, "method-a"
            )
            evidence["terminal_status"] = "failed"
            with self.assertRaisesRegex(ValidationError, "terminal_status"):
                validate_evidence_response_chain(
                    [evidence], [response], manifest, "method-a"
                )
            evidence["terminal_status"] = "complete"
            evidence["ego"]["count"] = 1
            with self.assertRaises(ValidationError):
                validate_evidence_response_chain(
                    [evidence], [response], manifest, "method-a"
                )
            evidence["ego"]["count"] = 0
            evidence["provenance"]["extractor_config_sha256"] = "f" * 64
            with self.assertRaises(ValidationError):
                validate_evidence_response_chain(
                    [evidence], [response], manifest, "method-a"
                )

    def test_score_is_bound_to_frozen_evaluator_and_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluator_paths = sorted(Path("bus_benchmark").resolve().glob("*.py"))
            schema_paths = list(supported_schema_paths())
            extractor = root / "extractor.json"
            roster = root / "roster.json"
            oracle = root / "oracle.jsonl"
            write_json(
                extractor,
                {
                    "metric_files": [
                        {"path": str(path), "sha256": sha256_file(path)}
                        for path in evaluator_paths
                    ],
                    "schema_files": [
                        {"path": str(path), "sha256": sha256_file(path)}
                        for path in schema_paths
                    ],
                },
            )
            write_json(roster, {"method_id": "method-a"})
            oracle_record = {"query_id": "q"}
            write_jsonl(oracle, [oracle_record])
            manifest = {
                "manifest_sha256": "c" * 64,
                "assets": [
                    {
                        "role": "query_roster",
                        "path": str(roster),
                        "sha256": sha256_file(roster),
                    },
                    {
                        "role": "requirement_oracle_test",
                        "path": str(oracle),
                        "sha256": sha256_file(oracle),
                    },
                    {
                        "role": "semantic_extractor_manifest",
                        "path": str(extractor),
                        "sha256": sha256_file(extractor),
                    },
                ],
            }
            provenance = {
                "roster_sha256": sha256_file(roster),
                "oracle_sha256": sha256_file(oracle),
                "extractor_manifest_sha256": sha256_file(extractor),
                "evaluator_source_sha256": metric_source_hash(manifest),
                "freeze_manifest_sha256": "c" * 64,
                "response_record_sha256": "d" * 64,
                "evidence_record_sha256": "e" * 64,
                "oracle_record_sha256": record_sha256(oracle_record),
            }
            score = {**self._base(), "provenance": provenance}
            validate_score_provenance([score], manifest, roster, oracle)
            score["provenance"]["evaluator_source_sha256"] = "f" * 64
            with self.assertRaises(ValidationError):
                validate_score_provenance([score], manifest, roster, oracle)

            legacy_schema = (
                Path("benchmark_configs/schemas")
                / "human_adjudication_packet.schema.json"
            ).resolve()
            extractor_document = {
                "metric_files": [
                    {"path": str(path), "sha256": sha256_file(path)}
                    for path in evaluator_paths
                ],
                "schema_files": [
                    {"path": str(path), "sha256": sha256_file(path)}
                    for path in schema_paths
                ]
                + [
                    {
                        "path": str(legacy_schema),
                        "sha256": sha256_file(legacy_schema),
                    }
                ],
            }
            write_json(extractor, extractor_document)
            with self.assertRaisesRegex(
                ValidationError, "complete evaluator implementation is not frozen"
            ):
                metric_source_hash(manifest)

    def test_in_range_forged_score_is_rejected_by_live_recomputation(self):
        atom = make_atom("road", "road_topology", {"value": "midblock"})
        atom["decision_status"] = "confirmed"
        oracle = {
            "query_id": "q",
            "intent_group_id": "i",
            "surface_style": "partial",
            "expected_support": "supported",
            "decision_status": "confirmed",
            "atoms": [atom],
        }
        evidence = {
            **self._base(),
            "ego": {
                "count": 1,
                "semantic_role": "bus_proxy",
                "approved_proxy": True,
                "deterministic": True,
                "length_m": 5.33,
                "width_m": 2.1,
                "blueprint": "vehicle.chevrolet.impala",
            },
            "atoms": [],
            "complete_categories": ["road"],
        }
        from bus_benchmark.metrics import score_semantic_output

        score = score_semantic_output(oracle, evidence)
        score.update(self._base())
        score["provenance"] = {"evidence_record_sha256": record_sha256(evidence)}
        roster = {
            "method_id": "method-a",
            "platform": "carla",
            "query_count": 1,
            "expected_response_count": 5,
            "repetitions": [0, 1, 2, 3, 4],
            "queries": [
                {
                    "query_id": "q",
                    "intent_group_id": "i",
                    "statistical_intent_cluster_id": "i",
                    "surface_style": "partial",
                    "expected_support": "supported",
                }
            ],
        }
        validate_scores_against_evidence([score], [evidence], [oracle], roster)
        score["srs"] = 1.0
        with self.assertRaises(ValidationError):
            validate_scores_against_evidence([score], [evidence], [oracle], roster)

    def test_score_response_hash_must_match_the_actual_response(self):
        response = {**self._base(), "terminal_status": "generated"}
        score = {
            **self._base(),
            "provenance": {"response_record_sha256": record_sha256(response)},
        }
        validate_aggregate_response_chain([response], [score])
        score["provenance"]["response_record_sha256"] = "f" * 64
        with self.assertRaises(ValidationError):
            validate_aggregate_response_chain([response], [score])

    def test_score_judge_audit_must_match_the_current_validated_run(self):
        expected = {
            "judge_run_manifest_sha256": "a" * 64,
            "judge_responses_sha256": "b" * 64,
        }
        score = {
            "provenance": {
                "judge_run_manifest_sha256": "a" * 64,
                "judge_responses_sha256": "b" * 64,
            }
        }
        validate_score_judge_run_provenance([score], expected)
        score["provenance"]["judge_responses_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValidationError, "different Judge run"):
            validate_score_judge_run_provenance([score], expected)


if __name__ == "__main__":
    unittest.main()
