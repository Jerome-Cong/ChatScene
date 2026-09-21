import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from bus_benchmark.errors import ValidationError
from bus_benchmark.human_workflow import (
    QUERY_CHECK_PROJECTION_SPECS,
    build_method_blind_post_test_bundle,
    build_post_test_submission_template,
    export_carla_stage_judge_calibration_review_bundle,
    export_carla_stage_platform_review_bundle,
    export_query_review_bundle,
    finalize_carla_stage_judge_calibration_review,
    finalize_carla_stage_platform_review,
    finalize_post_test_audit,
    finalize_query_review_bundle,
    refresh_human_work_inventory,
    query_check_scope_description_zh,
    validate_carla_stage_calibration_report,
    validate_carla_stage_platform_report,
    validate_human_document,
    validate_post_test_audit_report,
)
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
)
from bus_benchmark.oracle import REQUIRED_REVIEW_CHECKS, build_review_tasks
from bus_benchmark.schema import validate_schema_records


ROOT = Path(__file__).resolve().parents[2]
DEV_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
DEV_ORACLE = ROOT / "benchmark_artifacts" / "drafts" / "dev_oracle_draft.jsonl"
TEST_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_query_library_v0_2.jsonl"
TEST_ORACLE = ROOT / "benchmark_artifacts" / "drafts" / "test_oracle_draft.jsonl"
INVENTORY = ROOT / "benchmark_artifacts" / "drafts" / "human_work_inventory_draft.json"


def _complete_query_submission(packet, reason="human review complete"):
    submission = copy.deepcopy(packet["submission_template"])
    submission["submission_status"] = "complete"
    tasks = {task["task_id"]: task for task in packet["tasks"]}
    for item in submission["responses"]:
        task = tasks[item["task_id"]]
        response = copy.deepcopy(task["response_template"])
        response["required_check_decisions"] = {
            check: {"verdict": "accept", "reason": reason}
            for check in response["required_check_decisions"]
        }
        for atom in response["atom_decisions"]:
            atom["verdict"] = "accept"
            atom["reason"] = reason
        response["cpd_decision"] = {
            "verdict": "accept",
            "reason": reason,
            "replacement_policy": None,
        }
        response["proposed_oracle"] = copy.deepcopy(task["oracle_draft"])
        response["notes"] = reason
        item["response"] = response
    return submission


def _complete_bound_submission(packet, response):
    submission = copy.deepcopy(packet["submission_template"])
    submission["submission_status"] = "complete"
    for item in submission["responses"]:
        item["response"] = copy.deepcopy(response)
    return submission


def _rehash_review_packet(packet):
    packet["packet_id"] = sha256_bytes(
        canonical_json_bytes(
            {
                key: packet[key]
                for key in (
                    "schema_version",
                    "workflow_type",
                    "reviewer_id",
                    "decision_status",
                    "source_binding",
                    "tasks",
                )
            }
        )
    )
    packet["submission_template"]["packet_id"] = packet["packet_id"]


def _fixture_input():
    text = "scenario Test = new Object\n"
    map_path = (
        ROOT
        / "Scenic"
        / "tests"
        / "formats"
        / "opendrive"
        / "maps"
        / "CARLA"
        / "Town01.xodr"
    )
    return {
        "schema_version": "0.1",
        "platform": "carla",
        "artifact_format": "scenic",
        "terminal_status": "complete",
        "disposition": "generate",
        "artifact": {
            "sha256": sha256_bytes(text.encode("utf-8")),
            "bytes": len(text),
            "text": text,
            "dependencies": [
                {
                    "kind": "opendrive",
                    "path": str(map_path),
                    "sha256": sha256_file(map_path),
                    "bytes": map_path.stat().st_size,
                }
            ],
        },
    }


def _projection():
    return {
        "ego": {
            "count": 1,
            "semantic_role": "ego_bus",
            "approved_proxy": True,
            "deterministic": True,
            "length_m": 5.33,
            "width_m": 2.1,
        },
        "atoms": [],
        "common_atoms": [],
        "complete_categories": ["event"],
    }


def _carla_stage_platform_sources(fixture_count=38, capability_count=14):
    concepts = []
    for index in range(capability_count):
        definition = {
            "kind": "requirement_atom_domain",
            "category": "event",
            "predicate": "event_spec",
            "polarity": "present",
            "argument_domain": [{"event": "bus_stop_{}".format(index)}],
        }
        concepts.append(
            {
                "concept_id": "atom:event:event_spec:present:{}".format(index),
                "kind": "requirement_atom_domain",
                "definition": definition,
                "definition_sha256": sha256_bytes(canonical_json_bytes(definition)),
            }
        )
    ontology = {
        "schema_version": "0.1",
        "decision_status": "confirmed",
        "ontology_id": "carla-stage-test-ontology",
        "concepts": concepts,
    }
    cases = []
    for index in range(fixture_count):
        cases.append(
            {
                "case_id": "carla-stage-case-{:02d}".format(index),
                "platform": "carla",
                "concept_ids": [concepts[index % capability_count]["concept_id"]],
                "test_kind": "positive",
                "input": _fixture_input(),
                "expected": _projection(),
            }
        )
    semantic = {
        "decision_status": "confirmed",
        "benchmark_owned": True,
        "input_contract": "raw_platform_artifact_v0.1",
        "cases": cases,
    }
    capability = {
        "platform": "carla",
        "decision_status": "confirmed",
        "method_independent": True,
        "ontology_id": ontology["ontology_id"],
        "ontology_sha256": sha256_bytes(canonical_json_bytes(ontology)),
        "concepts": [
            {
                "concept_id": concept["concept_id"],
                "definition_sha256": concept["definition_sha256"],
                "status": "constructible",
                "evidence_mode": "constructed_native",
                "rationale": "CARLA fixture reviewed",
                "fixture_case_ids": [
                    case["case_id"]
                    for case in cases
                    if case["concept_ids"] == [concept["concept_id"]]
                ],
                "coverage_scope": "registered_values_only",
                "uncovered_route": "judge",
                "deterministic_argument_values": copy.deepcopy(
                    concept["definition"]["argument_domain"]
                ),
            }
            for concept in concepts
        ],
    }
    return ontology, semantic, capability


def _carla_stage_calibration_sources():
    categories = ("actor", "road", "spatial", "event", "temporal", "normative")
    styles = ("precise", "partial", "vague")
    items = []
    subjects = []
    for index in range(90):
        item = {
            "item_id": "{:064x}".format(index + 1),
            "platform": "carla",
            "source_id": "carla-stage-reference",
            "query_id": "query-{:02d}".format(index % 48),
            "run_id": "carla-stage-run-{:03d}".format(index),
            "repetition": index % 5,
            "atom_id": "{:016x}".format(index + 1),
            "category": categories[index % len(categories)],
            "surface_style": styles[index % len(styles)],
            "source_response_sha256": "a" * 64,
            "source_evidence_sha256": "b" * 64,
        }
        items.append(item)
        subjects.append(
            {
                "item_id": item["item_id"],
                "review_payload": {
                    "query_text": "CARLA-stage blind calibration {}".format(index),
                    "artifact_evidence": ["observable evidence {}".format(index)],
                    "oracle_atom": {
                        "atom_id": item["atom_id"],
                        "category": item["category"],
                    },
                },
            }
        )
    roster = {
        "schema_version": "0.1",
        "decision_status": "draft",
        "stage_scope": "carla_first",
        "platform": "carla",
        "formal_freeze_eligible": False,
        "selection_algorithm": "query_category_then_sha256_v0.1",
        "seed": "bus-judge-calibration-v0.1",
        "eligible_population_count": 90,
        "eligible_population_sha256": "c" * 64,
        "items_sha256": sha256_bytes(canonical_json_bytes(items)),
        "items": items,
    }
    return roster, subjects


def _population():
    records = []
    for method_index, method_id in enumerate(("method-a", "method-b")):
        for index in range(10):
            records.append(
                {
                    "run_id": "{}-run-{}".format(method_id, index),
                    "method_id": method_id,
                    "query_id": "query-{}".format(index),
                    "platform": "carla" if method_index == 0 else "metadrive",
                    "surface_style": ("precise", "partial", "vague")[index % 3],
                    "terminal_status": "complete" if index % 2 else "runtime_error",
                    "srs": index / 10.0,
                    "arc": 0.5,
                    "rsc": 0.6,
                    "iec_spec": 0.7,
                    "review_payload": {
                        "query_text": "Generate bus scene {}".format(index),
                        "artifact": {
                            "sha256": "{:064x}".format(index + 1),
                            "content": "normalized scene evidence {}".format(index),
                        },
                    },
                }
            )
    return records


def _complete_post_submission(public_packet, reviewer="reviewer"):
    submission = build_post_test_submission_template(public_packet, reviewer)
    submission["submission_status"] = "complete"
    for response in submission["responses"]:
        response.update(
            {
                "review_status": "complete",
                "human_srs": 0.4,
                "human_arc": 0.5,
                "human_rsc": 0.6,
                "human_iec_spec": 0.7,
                "error_taxonomy": [],
                "notes": "blind review complete",
            }
        )
    return submission


class QueryReviewWorkflowTests(unittest.TestCase):
    def test_query_check_scope_descriptions_share_the_canonical_projection_spec(self):
        self.assertEqual(
            set(QUERY_CHECK_PROJECTION_SPECS), set(REQUIRED_REVIEW_CHECKS)
        )
        cardinality = query_check_scope_description_zh(
            "cardinality_and_polarity"
        )
        self.assertIn("卡片内容参数", cardinality)
        self.assertIn("要求出现/要求不存在", cardinality)
        self.assertIn("ego bus 身份门槛", cardinality)
        surface = query_check_scope_description_zh("surface_layering")
        self.assertIn("卡片内容参数", surface)
        self.assertIn("计分层级", surface)
        self.assertIn("precise/partial/vague", surface)

        for field_type, field_name in (
            ("atom_fields", "future_atom_field"),
            ("oracle_fields", "future_oracle_field"),
        ):
            mutated = copy.deepcopy(
                QUERY_CHECK_PROJECTION_SPECS["cardinality_and_polarity"]
            )
            mutated[field_type] = tuple(mutated[field_type]) + (field_name,)
            with self.subTest(field=field_name), patch.dict(
                QUERY_CHECK_PROJECTION_SPECS,
                {"cardinality_and_polarity": mutated},
            ), self.assertRaisesRegex(ValidationError, "undescribed"):
                query_check_scope_description_zh("cardinality_and_polarity")

    def setUp(self):
        self.library = read_jsonl(DEV_LIBRARY)[:1]
        self.oracle = read_jsonl(DEV_ORACLE)[:1]
        self.bundle = export_query_review_bundle(
            self.library, self.oracle, reviewer_id="human-reviewer"
        )

    def test_one_submission_directly_creates_one_complete_gold(self):
        self.assertEqual(
            self.bundle["review_policy"], "one_complete_human_gold_per_subject"
        )
        self.assertNotIn("annotator_a_packet", self.bundle)
        self.assertNotIn("adjudication_template", self.bundle)
        packet = self.bundle["reviewer_packet"]
        self.assertIsNone(packet["submission_template"]["responses"][0]["response"])
        recommendation = packet["tasks"][0]["machine_recommendation"]
        self.assertEqual(
            recommendation["recommendation_status"], "machine_only_non_gold"
        )
        self.assertFalse(recommendation["human_gold"])
        self.assertFalse(recommendation["human_submission_populated"])
        submission = _complete_query_submission(self.bundle["reviewer_packet"])
        result = finalize_query_review_bundle(
            self.bundle,
            submission,
            library_source=self.library,
            oracle_source=self.oracle,
        )
        self.assertEqual(len(result["human_gold_records"]), 1)
        self.assertEqual(result["human_gold_records"][0]["review_status"], "complete")
        self.assertEqual(result["confirmed_oracles"][0]["decision_status"], "confirmed")

    def test_v0_1_oracle_ids_cannot_silently_bind_to_v0_2_library(self):
        stale_oracle = copy.deepcopy(self.oracle)
        stale_oracle[0]["query_id"] = self.library[0]["supersedes_query_id"]
        stale_oracle[0]["intent_group_id"] = self.library[0][
            "supersedes_intent_group_id"
        ]
        with self.assertRaisesRegex(
            ValidationError, "query library and oracle draft query IDs differ"
        ):
            export_query_review_bundle(
                self.library,
                stale_oracle,
                reviewer_id="human-reviewer",
            )

    def test_checked_in_bundle_gold_is_formal_schema_compatible(self):
        bundle = export_query_review_bundle(
            DEV_LIBRARY, DEV_ORACLE, reviewer_id="human-reviewer"
        )
        submission = _complete_query_submission(bundle["reviewer_packet"])
        result = finalize_query_review_bundle(
            bundle,
            submission,
            library_source=DEV_LIBRARY,
            oracle_source=DEV_ORACLE,
        )
        self.assertEqual(len(result["human_gold_records"]), 48)
        validate_schema_records(
            result["human_gold_records"],
            "human_query_gold",
            context="production query gold admitted by the formal freeze schema",
        )
        incomplete = copy.deepcopy(result["human_gold_records"][:1])
        incomplete[0]["review_payload"] = {"status": "uncertain"}
        with self.assertRaises(ValidationError):
            validate_schema_records(incomplete, "human_query_gold")

    def test_checked_in_review_task_drafts_use_single_reviewer_policy(self):
        for oracle_path, tasks_path in (
            (
                DEV_ORACLE,
                ROOT / "benchmark_artifacts" / "drafts" / "dev_oracle_review_tasks.jsonl",
            ),
            (
                TEST_ORACLE,
                ROOT / "benchmark_artifacts" / "drafts" / "test_oracle_review_tasks.jsonl",
            ),
        ):
            expected = build_review_tasks(read_jsonl(oracle_path))
            self.assertEqual(read_jsonl(tasks_path), expected)
            self.assertTrue(
                all(
                    row["review_policy"]
                    == "one_complete_human_gold_per_subject"
                    for row in expected
                )
            )

    def test_accept_cannot_hide_projection_drift(self):
        submission = _complete_query_submission(self.bundle["reviewer_packet"])
        atom = submission["responses"][0]["response"]["proposed_oracle"]["atoms"][0]
        atom["arguments"] = {"forged": "semantic drift"}
        with self.assertRaisesRegex(ValidationError, "drifts"):
            finalize_query_review_bundle(
                self.bundle,
                submission,
                library_source=self.library,
                oracle_source=self.oracle,
            )

    def test_atom_accept_cannot_hide_a_declared_high_level_revision(self):
        submission = _complete_query_submission(self.bundle["reviewer_packet"])
        response = submission["responses"][0]["response"]
        atom = response["proposed_oracle"]["atoms"][0]
        atom["layer"] = (
            "permitted" if atom["layer"] != "permitted" else "core_required"
        )
        for check in ("event_graph_and_actor_binding", "surface_layering"):
            response["required_check_decisions"][check]["verdict"] = "revise"
        with self.assertRaisesRegex(ValidationError, "accepted atom has a semantic change"):
            finalize_query_review_bundle(
                self.bundle,
                submission,
                library_source=self.library,
                oracle_source=self.oracle,
            )

    def test_final_atom_reuse_requires_an_explicit_merge(self):
        submission = _complete_query_submission(self.bundle["reviewer_packet"])
        response = submission["responses"][0]["response"]
        first, second = response["proposed_oracle"]["atoms"][:2]
        response["proposed_oracle"]["atoms"] = [
            atom
            for atom in response["proposed_oracle"]["atoms"]
            if atom["atom_id"] != first["atom_id"]
        ]
        first_decision = next(
            decision
            for decision in response["atom_decisions"]
            if decision["atom_id"] == first["atom_id"]
        )
        first_decision["verdict"] = "modify"
        first_decision["replacement_atoms"] = [copy.deepcopy(second)]
        for check in (
            "cardinality_and_polarity",
            "event_graph_and_actor_binding",
            "surface_layering",
        ):
            response["required_check_decisions"][check]["verdict"] = "revise"
        with self.assertRaisesRegex(ValidationError, "reused without an explicit merge"):
            finalize_query_review_bundle(
                self.bundle,
                submission,
                library_source=self.library,
                oracle_source=self.oracle,
            )

    def test_source_change_invalidates_submission(self):
        submission = _complete_query_submission(self.bundle["reviewer_packet"])
        changed_library = copy.deepcopy(self.library)
        changed_library[0]["query_text"] += " changed"
        with self.assertRaisesRegex(ValidationError, "canonical current sources"):
            finalize_query_review_bundle(
                self.bundle,
                submission,
                library_source=changed_library,
                oracle_source=self.oracle,
            )

    def test_bundle_cannot_relabel_a_packet_source(self):
        submission = _complete_query_submission(self.bundle["reviewer_packet"])
        forged = copy.deepcopy(self.bundle)
        forged["source_binding"]["library_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "single reviewer packet"):
            finalize_query_review_bundle(
                forged,
                submission,
                library_source=self.library,
                oracle_source=self.oracle,
            )

    def test_rehashed_packet_cannot_change_reviewer_visible_query_source(self):
        forged = copy.deepcopy(self.bundle)
        packet = forged["reviewer_packet"]
        packet["tasks"][0]["query_text"] = "forged reviewer-visible text"
        packet["tasks"][0]["query_record"]["query_text"] = (
            "forged reviewer-visible record"
        )
        _rehash_review_packet(packet)
        submission = _complete_query_submission(packet)
        with self.assertRaisesRegex(ValidationError, "canonical current sources"):
            finalize_query_review_bundle(
                forged,
                submission,
                library_source=self.library,
                oracle_source=self.oracle,
            )


class CarlaStageWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ontology, cls.semantic, cls.capability = _carla_stage_platform_sources()
        cls.platform_bundle = export_carla_stage_platform_review_bundle(
            cls.ontology,
            cls.semantic,
            cls.capability,
            reviewer_id="platform-reviewer",
        )
        cls.roster, cls.subjects = _carla_stage_calibration_sources()
        cls.calibration_bundle = export_carla_stage_judge_calibration_review_bundle(
            cls.roster,
            cls.subjects,
            reviewer_id="calibration-reviewer",
        )

    def test_platform_direct_finalization_has_52_gold_records(self):
        packet = self.platform_bundle["reviewer_packet"]
        self.assertEqual(len(packet["tasks"]), 52)
        submission = _complete_bound_submission(
            packet, {"verdict": "approve", "reason": "source evidence approved"}
        )
        result = finalize_carla_stage_platform_review(
            self.platform_bundle,
            submission,
            ontology_source=self.ontology,
            semantic_fixture_source=self.semantic,
            carla_capability_source=self.capability,
        )
        self.assertEqual(len(result["human_gold_records"]), 52)
        self.assertTrue(
            all(row["review_status"] == "complete" for row in result["human_gold_records"])
        )
        self.assertFalse(result["formal_freeze_eligible"])

        mutated = copy.deepcopy(result)
        mutated["human_gold_records"][0]["reviewer"]["reason"] = "forged reason"
        with self.assertRaisesRegex(ValidationError, "mutates a human-gold"):
            validate_human_document(mutated, "carla_stage_platform_report")
        record = mutated["human_gold_records"][0]
        record["gold_record_id"] = sha256_bytes(
            canonical_json_bytes(
                {
                    key: value
                    for key, value in record.items()
                    if key != "gold_record_id"
                }
            )
        )
        with self.assertRaisesRegex(ValidationError, "source-bound reconstruction"):
            validate_carla_stage_platform_report(
                mutated,
                self.platform_bundle,
                submission,
                ontology_source=self.ontology,
                semantic_fixture_source=self.semantic,
                carla_capability_source=self.capability,
            )

        duplicate = copy.deepcopy(result)
        duplicate["human_gold_records"][1] = copy.deepcopy(
            duplicate["human_gold_records"][0]
        )
        with self.assertRaisesRegex(ValidationError, "repeats or mutates"):
            validate_human_document(duplicate, "carla_stage_platform_report")

    def test_platform_rejection_is_not_gold(self):
        submission = _complete_bound_submission(
            self.platform_bundle["reviewer_packet"],
            {"verdict": "reject", "reason": "fixture needs correction"},
        )
        with self.assertRaisesRegex(ValidationError, "was not approved"):
            finalize_carla_stage_platform_review(
                self.platform_bundle,
                submission,
                ontology_source=self.ontology,
                semantic_fixture_source=self.semantic,
                carla_capability_source=self.capability,
            )

    def test_rehashed_platform_packet_cannot_change_audit_subject(self):
        forged = copy.deepcopy(self.platform_bundle)
        packet = forged["reviewer_packet"]
        packet["tasks"][0]["subject"]["audit_subject"]["forged_visible"] = True
        _rehash_review_packet(packet)
        submission = _complete_bound_submission(
            packet, {"verdict": "approve", "reason": "forged content approved"}
        )
        with self.assertRaisesRegex(ValidationError, "canonical current sources"):
            finalize_carla_stage_platform_review(
                forged,
                submission,
                ontology_source=self.ontology,
                semantic_fixture_source=self.semantic,
                carla_capability_source=self.capability,
            )

    def test_calibration_direct_finalization_has_90_gold_records(self):
        self.assertNotIn("judge_response_id", str(self.calibration_bundle))
        label = {
            "verdict": "unknown",
            "evidence": ["human-observed evidence"],
            "rationale": "evidence does not resolve the atom",
        }
        submission = _complete_bound_submission(
            self.calibration_bundle["reviewer_packet"], label
        )
        result = finalize_carla_stage_judge_calibration_review(
            self.calibration_bundle,
            submission,
            calibration_roster_source=self.roster,
            reviewer_subject_source=self.subjects,
        )
        self.assertEqual(len(result["human_gold_records"]), 90)
        self.assertTrue(
            all(row["review_status"] == "complete" for row in result["human_gold_records"])
        )
        self.assertEqual(
            {row["gold_verdict"] for row in result["human_gold_records"]}, {"unknown"}
        )

        mutated = copy.deepcopy(result)
        mutated["human_gold_records"][0]["gold_rationale"] = "forged rationale"
        with self.assertRaisesRegex(ValidationError, "mutates a human-gold"):
            validate_human_document(mutated, "carla_stage_calibration_report")
        record = mutated["human_gold_records"][0]
        record["gold_record_id"] = sha256_bytes(
            canonical_json_bytes(
                {
                    key: value
                    for key, value in record.items()
                    if key != "gold_record_id"
                }
            )
        )
        with self.assertRaisesRegex(ValidationError, "source-bound reconstruction"):
            validate_carla_stage_calibration_report(
                mutated,
                self.calibration_bundle,
                submission,
                calibration_roster_source=self.roster,
                reviewer_subject_source=self.subjects,
            )

    def test_rehashed_calibration_packet_cannot_change_review_payload(self):
        forged = copy.deepcopy(self.calibration_bundle)
        packet = forged["reviewer_packet"]
        packet["tasks"][0]["subject"]["review_payload"]["query_text"] = (
            "forged reviewer-visible calibration text"
        )
        _rehash_review_packet(packet)
        submission = _complete_bound_submission(
            packet,
            {
                "verdict": "unknown",
                "evidence": ["forged evidence"],
                "rationale": "forged payload label",
            },
        )
        with self.assertRaisesRegex(ValidationError, "canonical current sources"):
            finalize_carla_stage_judge_calibration_review(
                forged,
                submission,
                calibration_roster_source=self.roster,
                reviewer_subject_source=self.subjects,
            )

    def test_complete_inventory_is_442_and_binds_current_sources(self):
        query_bundles = [
            export_query_review_bundle(DEV_LIBRARY, DEV_ORACLE, reviewer_id="query-reviewer"),
            export_query_review_bundle(TEST_LIBRARY, TEST_ORACLE, reviewer_id="query-reviewer"),
        ]
        inventory_draft = read_json(INVENTORY)
        inventory_draft["current_human_review_state"][
            "completed_real_human_reviews"
        ] = 999
        inventory_draft["current_human_review_state"][
            "completed_admissible_human_gold_records"
        ] = 999
        result = refresh_human_work_inventory(
            inventory_draft,
            query_bundles=query_bundles,
            platform_bundle=self.platform_bundle,
            calibration_bundle=self.calibration_bundle,
            platform_sources={
                "ontology": self.ontology,
                "semantic_fixtures": self.semantic,
                "carla_capability": self.capability,
            },
            calibration_sources={
                "roster": self.roster,
                "reviewer_subjects": self.subjects,
            },
        )
        workload = result["pre_freeze_human_workload"]
        expected_query_decisions = sum(
            task["machine_recommendation"]["explicit_verdict_entries"]
            for bundle in query_bundles
            for task in bundle["reviewer_packet"]["tasks"]
        )
        self.assertEqual(result["decision_status"], "stage_bound")
        self.assertEqual(workload["subjects"], 442)
        self.assertEqual(workload["reviewer_submissions"], 442)
        self.assertEqual(workload["human_gold_records_required"], 442)
        self.assertEqual(workload["total_human_decision_records"], 442)
        self.assertEqual(result["pre_freeze_oracle"]["required_check_decisions"], 1800)
        self.assertEqual(result["pre_freeze_oracle"]["cpd_decisions"], 300)
        self.assertEqual(
            result["pre_freeze_oracle"]["total_draft_decisions"],
            expected_query_decisions,
        )
        self.assertNotIn("adjudications", workload)
        self.assertEqual(
            result["current_human_review_state"]["completed_real_human_reviews"],
            0,
        )
        self.assertEqual(
            result["current_human_review_state"][
                "completed_admissible_human_gold_records"
            ],
            0,
        )

        changed = copy.deepcopy(self.semantic)
        changed["cases"][0]["case_id"] = "changed-current-source"
        with self.assertRaisesRegex(ValidationError, "differs from its review bundle"):
            refresh_human_work_inventory(
                read_json(INVENTORY),
                query_bundles=query_bundles,
                platform_bundle=self.platform_bundle,
                calibration_bundle=self.calibration_bundle,
                platform_sources={
                    "ontology": self.ontology,
                    "semantic_fixtures": changed,
                    "carla_capability": self.capability,
                },
                calibration_sources={
                    "roster": self.roster,
                    "reviewer_subjects": self.subjects,
                },
            )

    def test_fake_300_query_source_cannot_claim_stage_bound(self):
        fake_library = read_jsonl(DEV_LIBRARY) + read_jsonl(TEST_LIBRARY)
        fake_oracle = read_jsonl(DEV_ORACLE) + read_jsonl(TEST_ORACLE)
        fake_bundle = export_query_review_bundle(
            fake_library, fake_oracle, reviewer_id="query-reviewer"
        )
        with self.assertRaisesRegex(ValidationError, "checked-in file bytes"):
            refresh_human_work_inventory(
                read_json(INVENTORY),
                query_bundles=[fake_bundle],
                platform_bundle=self.platform_bundle,
                calibration_bundle=self.calibration_bundle,
                platform_sources={
                    "ontology": self.ontology,
                    "semantic_fixtures": self.semantic,
                    "carla_capability": self.capability,
                },
                calibration_sources={
                    "roster": self.roster,
                    "reviewer_subjects": self.subjects,
                },
            )

    def test_forged_query_tasks_cannot_reuse_checked_in_source_hashes(self):
        query_bundles = [
            export_query_review_bundle(DEV_LIBRARY, DEV_ORACLE, reviewer_id="query-reviewer"),
            export_query_review_bundle(TEST_LIBRARY, TEST_ORACLE, reviewer_id="query-reviewer"),
        ]
        forged = copy.deepcopy(query_bundles)
        packet = forged[0]["reviewer_packet"]
        packet["tasks"][0]["query_text"] = "forged while source hashes stay unchanged"
        packet["packet_id"] = sha256_bytes(
            canonical_json_bytes(
                {
                    key: packet[key]
                    for key in (
                        "schema_version",
                        "workflow_type",
                        "reviewer_id",
                        "decision_status",
                        "source_binding",
                        "tasks",
                    )
                }
            )
        )
        with self.assertRaisesRegex(ValidationError, "source materialization"):
            refresh_human_work_inventory(
                read_json(INVENTORY),
                query_bundles=forged,
                platform_bundle=self.platform_bundle,
                calibration_bundle=self.calibration_bundle,
                platform_sources={
                    "ontology": self.ontology,
                    "semantic_fixtures": self.semantic,
                    "carla_capability": self.capability,
                },
                calibration_sources={
                    "roster": self.roster,
                    "reviewer_subjects": self.subjects,
                },
            )

    def test_partial_dev_never_claims_stage_bound(self):
        result = refresh_human_work_inventory(
            read_json(INVENTORY),
            query_bundles=[
                export_query_review_bundle(
                    read_jsonl(DEV_LIBRARY)[:1],
                    read_jsonl(DEV_ORACLE)[:1],
                    reviewer_id="query-reviewer",
                )
            ],
            platform_bundle=self.platform_bundle,
            calibration_bundle=self.calibration_bundle,
            profile="partial_dev",
        )
        self.assertEqual(result["decision_status"], "partial_dev")
        self.assertFalse(
            result["pre_freeze_human_workload"]["formal_stage_profile_complete"]
        )


class PostTestBlindWorkflowTests(unittest.TestCase):
    def test_complete_single_review_finalizes(self):
        population = _population()
        bundle = build_method_blind_post_test_bundle(population, rate=0.2, seed=7)
        submission = _complete_post_submission(bundle["public_tasks"])
        self.assertEqual(
            bundle["public_tasks"]["review_policy"],
            "one_complete_human_gold_per_subject",
        )
        self.assertEqual(
            submission["review_assignment_policy"], "single_reviewer_final"
        )
        report = finalize_post_test_audit(
            bundle["public_tasks"], bundle["private_linkage"], submission, population
        )
        self.assertEqual(report["review_status"], "complete")
        self.assertEqual(report["selected"], 4)
        self.assertEqual(set(report["by_method"]), {"method-a", "method-b"})
        self.assertEqual(len(report["human_gold_records"]), report["selected"])
        gold_by_blind_id = {
            record["blind_review_id"]: record
            for record in report["human_gold_records"]
        }
        comparison_by_blind_id = {
            record["blind_review_id"]: record for record in report["records"]
        }
        self.assertEqual(set(gold_by_blind_id), set(comparison_by_blind_id))
        for blind_id, gold in gold_by_blind_id.items():
            self.assertEqual(gold["review_status"], "complete")
            self.assertEqual(gold["decision_status"], "confirmed")
            self.assertEqual(
                gold["gold_record_id"],
                sha256_bytes(
                    canonical_json_bytes(
                        {
                            key: value
                            for key, value in gold.items()
                            if key != "gold_record_id"
                        }
                    )
                ),
            )
            for forbidden in ("method_id", "platform", "run_id", "automatic"):
                self.assertNotIn(forbidden, gold)
            self.assertEqual(
                comparison_by_blind_id[blind_id]["human_gold_record_id"],
                gold["gold_record_id"],
            )

    def test_post_test_report_rejects_gold_integrity_attacks(self):
        population = _population()
        bundle = build_method_blind_post_test_bundle(population, rate=0.2, seed=7)
        submission = _complete_post_submission(bundle["public_tasks"])
        report = finalize_post_test_audit(
            bundle["public_tasks"], bundle["private_linkage"], submission, population
        )

        missing = copy.deepcopy(report)
        missing["human_gold_records"].pop()

        modified = copy.deepcopy(report)
        modified["human_gold_records"][0]["human_srs"] = 0.9

        duplicate_subject = copy.deepcopy(report)
        duplicate_subject["human_gold_records"][1] = copy.deepcopy(
            duplicate_subject["human_gold_records"][0]
        )

        cross_linked = copy.deepcopy(report)
        cross_linked["records"][0]["human_gold_record_id"] = cross_linked[
            "human_gold_records"
        ][1]["gold_record_id"]

        synchronized_deletion = copy.deepcopy(report)
        removed_gold = synchronized_deletion["human_gold_records"].pop()
        synchronized_deletion["records"] = [
            record
            for record in synchronized_deletion["records"]
            if record["blind_review_id"] != removed_gold["blind_review_id"]
        ]
        synchronized_deletion["selected"] -= 1

        adaptively_rehashed = copy.deepcopy(report)
        gold = adaptively_rehashed["human_gold_records"][0]
        old_gold_id = gold["gold_record_id"]
        gold["human_srs"] = 0.9
        gold["gold_record_id"] = sha256_bytes(
            canonical_json_bytes(
                {
                    key: value
                    for key, value in gold.items()
                    if key != "gold_record_id"
                }
            )
        )
        comparison = next(
            record
            for record in adaptively_rehashed["records"]
            if record["human_gold_record_id"] == old_gold_id
        )
        comparison["human_gold_record_id"] = gold["gold_record_id"]
        comparison["metric_errors"]["srs"]["human"] = 0.9

        rewritten_comparison = copy.deepcopy(report)
        rewritten_comparison["records"][0]["method_id"] = "forged-method"
        rewritten_comparison["records"][0]["run_id"] = "forged-run"

        forged_summary = copy.deepcopy(report)
        forged_summary["by_method"]["method-a"]["sample_count"] = 999

        for label, attacked in (
            ("missing", missing),
            ("modified", modified),
            ("duplicate", duplicate_subject),
            ("cross-linked", cross_linked),
            ("synchronized-deletion", synchronized_deletion),
            ("adaptively-rehashed", adaptively_rehashed),
            ("rewritten-comparison", rewritten_comparison),
            ("forged-summary", forged_summary),
        ):
            with self.subTest(label=label), self.assertRaises(ValidationError):
                validate_post_test_audit_report(
                    attacked,
                    bundle["public_tasks"],
                    bundle["private_linkage"],
                    submission,
                    population,
                )

    def test_uncertain_is_not_gold(self):
        population = _population()
        bundle = build_method_blind_post_test_bundle(population, rate=0.2, seed=7)
        submission = _complete_post_submission(bundle["public_tasks"])
        submission["responses"][0]["review_status"] = "uncertain"
        with self.assertRaisesRegex(ValidationError, "not human gold"):
            finalize_post_test_audit(
                bundle["public_tasks"],
                bundle["private_linkage"],
                submission,
                population,
            )

    def test_single_pass_population_iterable_is_stabilized_for_exact_rebuild(self):
        population = _population()
        bundle = build_method_blind_post_test_bundle(population, rate=0.2, seed=7)
        submission = _complete_post_submission(bundle["public_tasks"])
        report = finalize_post_test_audit(
            bundle["public_tasks"],
            bundle["private_linkage"],
            submission,
            (record for record in population),
        )
        self.assertEqual(report["selected"], 4)

    def test_declared_rate_is_recomputed_against_full_population(self):
        population = _population()
        bundle = build_method_blind_post_test_bundle(population, rate=0.2, seed=7)
        public = copy.deepcopy(bundle["public_tasks"])
        private = copy.deepcopy(bundle["private_linkage"])
        removed = public["tasks"].pop()["blind_review_id"]
        public["selected"] = len(public["tasks"])
        public["packet_id"] = sha256_bytes(
            canonical_json_bytes(
                {key: value for key, value in public.items() if key != "packet_id"}
            )
        )
        private["links"] = [
            link for link in private["links"] if link["blind_review_id"] != removed
        ]
        private["selected"] = len(private["links"])
        private["public_packet_id"] = public["packet_id"]
        submission = _complete_post_submission(public)
        with self.assertRaisesRegex(ValidationError, "deterministic rate/seed recomputation"):
            finalize_post_test_audit(public, private, submission, population)


if __name__ == "__main__":
    unittest.main()
