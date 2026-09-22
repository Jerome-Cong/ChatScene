from bus_benchmark.paths import PACKAGE_ROOT
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bus_benchmark.errors import ValidationError
from bus_benchmark.human_workflow import HUMAN_SCHEMA_FILES, validate_human_document
from bus_benchmark.jsonio import read_jsonl, sha256_file, write_json, write_jsonl
from bus_benchmark.oracle import REQUIRED_REVIEW_CHECKS
from bus_benchmark.schema import (
    SCHEMA_FILES,
    SUPPORTED_SCHEMA_FILENAMES,
    supported_schema_paths,
    validate_schema_instance,
)
from tests.bus_benchmark.test_human_workflow import (
    _carla_stage_calibration_sources,
    _carla_stage_platform_sources,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "human_review_workflow.py"
EXPORT_SCRIPT = ROOT / "scripts" / "export_human_review_drafts.py"
AGENT_REVIEW = (
    ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "query_agent_semantic_review_v0_2.json"
)
DEV_ORACLE = ROOT / "benchmark_artifacts" / "drafts" / "dev_oracle_draft.jsonl"
TEST_ORACLE = ROOT / "benchmark_artifacts" / "drafts" / "test_oracle_draft.jsonl"


def _query_explicit_decisions(oracle_path):
    records = read_jsonl(oracle_path)
    return sum(len(record["atoms"]) for record in records) + len(records) * (
        len(REQUIRED_REVIEW_CHECKS) + 1
    )


class HumanWorkflowCliTests(unittest.TestCase):
    def test_batch_exporter_rejects_legacy_v0_1_workbench_directory(self):
        legacy = (
            ROOT
            / "benchmark_artifacts"
            / "human_review"
            / "workbench_assignment"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(EXPORT_SCRIPT),
                "--reviewer",
                "reviewer-1",
                "--output-dir",
                str(legacy),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("read-only v0.1 historical directory", result.stderr)

    def test_checked_in_machine_draft_summary_binds_sources_and_zero_gold(self):
        agent_summary = json.loads(AGENT_REVIEW.read_text(encoding="utf-8"))[
            "summary"
        ]
        query_decisions = agent_summary["explicit_decision_entries"]
        summary = json.loads(
            (
                ROOT
                / "benchmark_artifacts"
                / "drafts"
                / "human_review_machine_draft_summary.json"
            ).read_text(encoding="utf-8")
        )
        validate_human_document(summary, "machine_draft_summary")
        self.assertEqual(
            summary["human_gold_state"]["completed_admissible_human_gold_records"],
            0,
        )
        self.assertFalse(summary["human_gold_state"]["machine_drafts_are_gold"])
        self.assertFalse(summary["human_gold_state"]["machine_recommendations_are_gold"])
        self.assertFalse(
            summary["human_gold_state"][
                "machine_recommendations_populate_human_submissions"
            ]
        )
        self.assertEqual(summary["production_registry_scope"], "assignment_time_only")
        self.assertNotIn("legacy_quarantine", summary)
        self.assertEqual(summary["workload"]["expected_subject_records"], 442)
        self.assertEqual(
            summary["workload"]["expected_explicit_verdict_entries"],
            query_decisions + 142,
        )
        self.assertEqual(
            summary["workload"]["query_machine_recommendation_entries"],
            query_decisions,
        )
        semantic_review = summary["query_agent_semantic_review"]
        self.assertEqual(semantic_review["reviewed_intent_groups"], 100)
        self.assertEqual(semantic_review["reviewed_subjects"], 300)
        self.assertEqual(semantic_review["draft_decision_entries"], query_decisions)
        self.assertFalse(semantic_review["human_submission_populated"])
        self.assertTrue(semantic_review["human_action_required"])
        self.assertEqual(
            sha256_file(ROOT / semantic_review["artifact"]["path"]),
            semantic_review["artifact"]["sha256"],
        )
        entries = {entry["draft_id"]: entry for entry in summary["entries"]}
        self.assertEqual(
            entries["dev_query_oracle"]["explicit_verdict_entries"],
            _query_explicit_decisions(DEV_ORACLE),
        )
        self.assertEqual(
            entries["test_query_oracle"]["explicit_verdict_entries"],
            _query_explicit_decisions(TEST_ORACLE),
        )
        for entry_id in (
            "dev_query_oracle",
            "test_query_oracle",
            "metadrive_token_only_platform_infrastructure",
        ):
            for binding in entries[entry_id]["source_bindings"]:
                self.assertEqual(sha256_file(ROOT / binding["path"]), binding["sha256"])
        self.assertEqual(
            entries["metadrive_token_only_platform_infrastructure"]["status"],
            "implemented_source_tested_independent_review_go_crosswalk_ready",
        )
        self.assertFalse(
            entries["metadrive_token_only_platform_infrastructure"][
                "standalone_human_gold_subject"
            ]
        )
        self.assertEqual(
            entries["metadrive_token_only_platform_infrastructure"][
                "formal_destination_count"
            ],
            5,
        )

        stale = copy.deepcopy(summary)
        stale["workload"]["query_atom_verdict_entries"] += 1
        with self.assertRaisesRegex(ValidationError, "not internally exact"):
            validate_human_document(stale, "machine_draft_summary")

        coherent_forgery = copy.deepcopy(summary)
        coherent_forgery["workload"]["query_atom_verdict_entries"] += 1
        coherent_forgery["workload"][
            "query_machine_recommendation_entries"
        ] += 1
        coherent_forgery["workload"][
            "currently_packet_generatable_explicit_verdict_entries"
        ] += 1
        coherent_forgery["workload"]["expected_explicit_verdict_entries"] += 1
        coherent_forgery["query_agent_semantic_review"][
            "draft_decision_entries"
        ] += 1
        coherent_forgery["query_agent_semantic_review"][
            "intent_semantic_finding_entries"
        ] += 1
        coherent_forgery["query_agent_semantic_review"]["recommendation_counts"][
            "human_judgment_required"
        ] += 1
        next(
            entry
            for entry in coherent_forgery["entries"]
            if entry["draft_id"] == "dev_query_oracle"
        )["explicit_verdict_entries"] += 1
        with self.assertRaisesRegex(ValidationError, "live Agent/oracle contract"):
            validate_human_document(coherent_forgery, "machine_draft_summary")

    def test_batch_exporter_refuses_every_nonempty_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "assignment"
            output.mkdir()
            marker = output / "foreign-marker.txt"
            registry = output / "machine_draft_registry.json"
            marker.write_text("retain me", encoding="utf-8")
            registry.write_text('{"legacy":true}\n', encoding="utf-8")
            before = {
                marker: marker.read_bytes(),
                registry: registry.read_bytes(),
            }
            result = subprocess.run(
                [
                    sys.executable,
                    str(EXPORT_SCRIPT),
                    "--reviewer",
                    "reviewer-1",
                    "--output-dir",
                    str(output),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be new or empty", result.stderr)
            self.assertEqual(
                {path: path.read_bytes() for path in before},
                before,
            )

    def test_metadrive_machine_decisions_crosswalk_without_extra_gold(self):
        draft = json.loads(
            (
                ROOT
                / "benchmark_artifacts"
                / "drafts"
                / "metadrive_track_draft_decisions.json"
            ).read_text(encoding="utf-8")
        )
        validate_schema_instance(draft, "metadrive_track_draft_decisions")
        self.assertFalse(draft["human_gold"])
        self.assertNotIn("human_review_subject", draft)
        self.assertFalse(draft["formal_alignment"]["standalone_human_gold_subject"])
        self.assertEqual(
            draft["formal_alignment"]["standalone_human_gold_records_required"], 0
        )
        expected = {
            "MD_TOKEN_EXECUTABLE_WHITELIST": (
                "platform_review_subject",
                "registry_implementation:metadrive:pg_block_registry",
            ),
            "MD_EGO_XL_PROXY": (
                "protocol_decision_subject",
                "EGO_PROXY_AND_CONTROLLERS",
            ),
            "MD_QUERY_BLIND_POLICY": (
                "protocol_decision_subject",
                "EGO_PROXY_AND_CONTROLLERS",
            ),
            "MD_SV_REPETITION_SEMANTICS": (
                "protocol_decision_subject",
                "RUNTIME_VALIDATION_METADRIVE",
            ),
            "MD_EVIDENCE_SEPARATION": (
                "automatic_gate",
                "semantic_extractor_and_runtime_evidence_contract",
            ),
            "MD_METHOD_ADAPTER_ADMISSION": (
                "automatic_gate",
                "method_config_and_generation_chain_admission",
            ),
        }
        actual = {
            item["decision_id"]: (
                item["formal_destination"]["destination_type"],
                item["formal_destination"]["destination_id"],
            )
            for item in draft["decisions"]
        }
        self.assertEqual(actual, expected)
        self.assertTrue(
            all(
                item["formal_destination"]["standalone_human_gold_records_added"] == 0
                for item in draft["decisions"]
            )
        )
        self.assertEqual(len(set(actual.values())), 5)

    def test_help_exposes_only_single_gold_production_commands(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=str(ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("query-finalize", result.stdout)
        self.assertIn("carla-platform-finalize", result.stdout)
        self.assertIn("carla-calibration-finalize", result.stdout)
        self.assertNotIn("build-adjudication", result.stdout)
        self.assertNotIn("post-test-second-export", result.stdout)
        self.assertNotIn("protocol-finalize", result.stdout)
        for legacy_command in (
            "build-adjudication",
            "post-test-second-export",
            "protocol-finalize",
            "platform-finalize",
        ):
            rejected = subprocess.run(
                [sys.executable, str(SCRIPT), legacy_command],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("invalid choice", rejected.stderr)

        platform_help = subprocess.run(
            [sys.executable, str(SCRIPT), "carla-platform-finalize", "--help"],
            cwd=str(ROOT),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for option in ("--ontology", "--semantic-fixtures", "--carla-capability"):
            self.assertIn(option, platform_help)
        calibration_help = subprocess.run(
            [sys.executable, str(SCRIPT), "carla-calibration-finalize", "--help"],
            cwd=str(ROOT),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for option in ("--roster", "--reviewer-subjects"):
            self.assertIn(option, calibration_help)

    def test_query_export_writes_one_reviewer_packet(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "query-export",
                    "--library",
                    str(
                        ROOT
                        / "query_lib"
                        / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
                    ),
                    "--oracle-draft",
                    str(
                        ROOT
                        / "benchmark_artifacts"
                        / "drafts"
                        / "dev_oracle_draft.jsonl"
                    ),
                    "--reviewer",
                    "reviewer-1",
                    "--output",
                    str(output),
                ],
                cwd=str(ROOT),
                check=True,
                capture_output=True,
                text=True,
            )
            bundle = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            bundle["review_policy"], "one_complete_human_gold_per_subject"
        )
        self.assertEqual(bundle["reviewer_packet"]["reviewer_id"], "reviewer-1")
        self.assertEqual(len(bundle["reviewer_packet"]["tasks"]), 48)
        self.assertNotIn("annotator_a_packet", bundle)
        self.assertNotIn("adjudication_template", bundle)

    def test_batch_exporter_writes_finalizable_query_bundles_and_registry(self):
        dev_decisions = _query_explicit_decisions(DEV_ORACLE)
        test_decisions = _query_explicit_decisions(TEST_ORACLE)
        query_decisions = dev_decisions + test_decisions
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(EXPORT_SCRIPT),
                    "--reviewer",
                    "reviewer-1",
                    "--output-dir",
                    directory,
                ],
                cwd=str(ROOT),
                check=True,
                capture_output=True,
                text=True,
            )
            summary = json.loads(result.stdout)
            registry = json.loads(
                (Path(directory) / "machine_draft_registry.json").read_text(
                    encoding="utf-8"
                )
            )
            for prefix, expected, expected_recommendations in (
                ("dev_query_review", 48, dev_decisions),
                ("test_query_review", 252, test_decisions),
            ):
                bundle = json.loads(
                    (Path(directory) / "{}_bundle.json".format(prefix)).read_text(
                        encoding="utf-8"
                    )
                )
                template = json.loads(
                    (
                        Path(directory)
                        / "{}_submission_template.json".format(prefix)
                    ).read_text(encoding="utf-8")
                )
                self.assertIn("reviewer_packet", bundle)
                self.assertEqual(
                    bundle["reviewer_packet"]["submission_template"], template
                )
                self.assertEqual(len(bundle["reviewer_packet"]["tasks"]), expected)
                self.assertTrue(
                    all(item["response"] is None for item in template["responses"])
                )
                recommendation_entries = 0
                for task in bundle["reviewer_packet"]["tasks"]:
                    recommendation = task["machine_recommendation"]
                    self.assertEqual(
                        recommendation["recommendation_status"],
                        "machine_only_non_gold",
                    )
                    self.assertTrue(recommendation["machine_generated"])
                    self.assertFalse(recommendation["human_gold"])
                    self.assertFalse(recommendation["human_submission_populated"])
                    self.assertTrue(recommendation["human_review_required"])
                    self.assertEqual(
                        recommendation["recommended_response"]["proposed_oracle"],
                        task["oracle_draft"],
                    )
                    self.assertIsNone(task["response_template"]["proposed_oracle"])
                    recommendation_entries += recommendation[
                        "explicit_verdict_entries"
                    ]
                self.assertEqual(recommendation_entries, expected_recommendations)

        validate_human_document(registry, "machine_draft_registry")
        self.assertEqual(summary["admissible_human_gold_created"], 0)
        self.assertEqual(
            summary["query_machine_recommendation_entries"], query_decisions
        )
        self.assertEqual(summary["query_agent_reviewed_intents"], 100)
        self.assertEqual(summary["query_agent_reviewed_subjects"], 300)
        self.assertEqual(
            summary["query_agent_draft_decision_entries"], query_decisions
        )
        self.assertFalse(summary["machine_recommendations_are_gold"])
        self.assertFalse(
            summary["machine_recommendations_populate_human_submissions"]
        )
        self.assertEqual(registry["workload"]["packet_ready_subjects"], 300)
        self.assertEqual(registry["workload"]["blocked_subjects"], 142)
        self.assertEqual(
            registry["workload"]["packet_ready_explicit_verdict_entries"],
            query_decisions,
        )
        self.assertEqual(
            registry["workload"]["expected_explicit_verdict_entries"],
            query_decisions + 142,
        )
        self.assertEqual(
            registry["workload"]["query_machine_recommendation_entries"],
            query_decisions,
        )
        self.assertEqual(
            registry["query_agent_semantic_review"]["reviewed_intent_groups"],
            100,
        )
        self.assertEqual(
            registry["query_agent_semantic_review"]["reviewed_subjects"], 300
        )
        self.assertEqual(
            registry["query_agent_semantic_review"]["draft_decision_entries"],
            query_decisions,
        )
        self.assertTrue(
            registry["query_agent_semantic_review"]["machine_only_non_gold"]
        )
        self.assertFalse(
            registry["query_agent_semantic_review"]["human_submission_populated"]
        )
        self.assertEqual(registry["human_gold"]["admissible_records_created_by_export"], 0)
        self.assertFalse(registry["human_gold"]["machine_recommendations_are_gold"])
        self.assertFalse(registry["reviewer_assignment"]["identity_proof_provided"])
        self.assertEqual(
            registry["reviewer_assignment"]["authorship_trust_boundary"],
            "external_human_custody_required",
        )
        self.assertFalse(
            registry["human_gold"]["machine_recommendations_populate_human_submissions"]
        )
        self.assertEqual(
            registry["metadrive_boundary"]["machine_decision_crosswalk_entries"], 6
        )
        self.assertEqual(
            registry["metadrive_boundary"]["standalone_human_gold_subjects"], 0
        )
        self.assertEqual(registry["readiness"]["query_human_review"], "GO")
        self.assertEqual(registry["readiness"]["carla_stage_freeze"], "NO_GO")
        self.assertNotIn("legacy_quarantine", registry)
        active_paths = json.dumps(registry["packets"], sort_keys=True)
        self.assertNotIn("annotator_a.json", active_paths)
        self.assertNotIn("adjudication_pending.json", active_paths)

        stale = copy.deepcopy(registry)
        stale["workload"]["query_machine_recommendation_entries"] += 1
        with self.assertRaisesRegex(ValidationError, "not internally exact"):
            validate_human_document(stale, "machine_draft_registry")

    def test_batch_exporter_can_materialize_all_442_when_sources_exist(self):
        query_decisions = _query_explicit_decisions(
            DEV_ORACLE
        ) + _query_explicit_decisions(TEST_ORACLE)
        ontology, semantic, capability = _carla_stage_platform_sources()
        roster, subjects = _carla_stage_calibration_sources()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ontology_path = root / "ontology.json"
            semantic_path = root / "semantic.json"
            capability_path = root / "capability.json"
            roster_path = root / "roster.json"
            subjects_path = root / "subjects.jsonl"
            write_json(ontology_path, ontology)
            write_json(semantic_path, semantic)
            capability["ontology_sha256"] = sha256_file(ontology_path)
            write_json(capability_path, capability)
            write_json(roster_path, roster)
            write_jsonl(subjects_path, subjects)
            subprocess.run(
                [
                    sys.executable,
                    str(EXPORT_SCRIPT),
                    "--reviewer",
                    "reviewer-1",
                    "--output-dir",
                    str(root / "assignment"),
                    "--ontology",
                    str(ontology_path),
                    "--semantic-fixtures",
                    str(semantic_path),
                    "--carla-capability",
                    str(capability_path),
                    "--calibration-roster",
                    str(roster_path),
                    "--calibration-reviewer-subjects",
                    str(subjects_path),
                ],
                cwd=str(ROOT),
                check=True,
                capture_output=True,
                text=True,
            )
            registry = json.loads(
                (root / "assignment" / "machine_draft_registry.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(registry["workload"]["packet_ready_subjects"], 442)
        self.assertEqual(registry["workload"]["blocked_subjects"], 0)
        self.assertEqual(
            registry["workload"]["packet_ready_explicit_verdict_entries"],
            query_decisions + 142,
        )
        self.assertEqual(registry["readiness"]["carla_platform_human_review"], "GO")
        self.assertEqual(registry["readiness"]["carla_calibration_human_review"], "GO")
        self.assertEqual(registry["readiness"]["carla_stage_freeze"], "NO_GO")

    def test_legacy_ab_assets_are_isolated_from_active_registries_and_freeze(self):
        legacy_schemas = {
            "human_adjudication_packet.schema.json",
            "human_adjudication_submission.schema.json",
            "human_post_test_second_review_packet.schema.json",
            "human_post_test_second_review_submission.schema.json",
            "human_protocol_review_packet.schema.json",
            "human_protocol_review_submission.schema.json",
            "review_adjudication.schema.json",
        }
        active_schemas = set(SUPPORTED_SCHEMA_FILENAMES)
        self.assertTrue(legacy_schemas.isdisjoint(active_schemas))
        self.assertTrue(set(HUMAN_SCHEMA_FILES.values()) <= active_schemas)
        disk_schemas = {
            path.name
            for path in (ROOT / "benchmark_configs" / "schemas").glob("*.json")
        }
        self.assertEqual(disk_schemas - active_schemas, legacy_schemas)
        self.assertEqual(len(disk_schemas), len(active_schemas) + len(legacy_schemas))
        self.assertEqual(
            active_schemas,
            {path.name for path in supported_schema_paths()},
        )
        freeze_source = (PACKAGE_ROOT / "freeze.py").read_text(
            encoding="utf-8"
        )
        for filename in (
            "dev_query_review_annotator_a.json",
            "dev_query_review_adjudication_pending.json",
            "protocol_review_annotator_b.json",
        ):
            self.assertNotIn(filename, freeze_source)
        formal_query_schema = json.loads(
            (
                ROOT
                / "benchmark_configs"
                / "schemas"
                / "human_query_gold_record.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            SCHEMA_FILES["human_query_gold"], "human_query_gold_record.schema.json"
        )
        self.assertNotIn("review_adjudication", SCHEMA_FILES)
        self.assertNotIn('"review_adjudication"', freeze_source)
        self.assertNotIn("annotator_a", json.dumps(formal_query_schema))
        self.assertNotIn("annotator_b", json.dumps(formal_query_schema))


if __name__ == "__main__":
    unittest.main()
