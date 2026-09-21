import copy
import tempfile
import unittest
from pathlib import Path

from bus_benchmark.errors import FreezeError, ValidationError
from bus_benchmark.freeze import (
    _expected_protocol_decision_subjects,
    _load_role_documents,
    _require_formal_judge_calibration_profile,
    _validate_platform_fixture_reviews,
    _validate_protocol_decision_reviews,
    _validate_review_payload,
    create_freeze_manifest,
    verify_freeze_manifest,
)
from bus_benchmark.calibration import compute_judge_calibration
from bus_benchmark.judge import judge_config_sha256
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    write_json,
    write_jsonl,
)
from bus_benchmark.schema import validate_schema_instance, validate_schema_records
from tests.bus_benchmark.formal_freeze_fixture import (
    DEV_LIBRARY,
    DEV_ORACLE_DRAFT,
    TEST_LIBRARY,
    TEST_ORACLE_DRAFT,
    _review_atom_hash,
    build_formal_bundle,
)


class FormalFreezeContractTests(unittest.TestCase):
    def test_formal_freeze_rejects_carla_stage_calibration_profile(self):
        with self.assertRaisesRegex(FreezeError, "cross-platform final"):
            _require_formal_judge_calibration_profile(
                {
                    "calibration_profile": "carla_stage",
                    "formal_freeze_eligible": False,
                }
            )
        _require_formal_judge_calibration_profile(
            {
                "calibration_profile": "cross_platform_final",
                "formal_freeze_eligible": True,
            }
        )

    def _formal_asset_records(self, specification):
        return [
            {
                "role": asset["role"],
                "path": str(Path(asset["path"]).resolve()),
                "sha256": sha256_file(Path(asset["path"])),
                "bytes": Path(asset["path"]).stat().st_size,
            }
            for asset in specification["assets"]
        ]

    def _rehash_human_gold(self, record):
        record["gold_record_id"] = sha256_bytes(
            canonical_json_bytes(
                {
                    key: value
                    for key, value in record.items()
                    if key != "gold_record_id"
                }
            )
        )

    def _rewrite_calibration_binding(self, paths, rows):
        judge = read_json(paths["judge_rubric"])
        calibration_path = Path(judge["calibration"]["path"])
        write_jsonl(calibration_path, rows)
        judge["calibration"]["sha256"] = sha256_file(calibration_path)
        judge["calibration"]["bytes"] = calibration_path.stat().st_size
        write_json(paths["judge_rubric"], judge)

    def _refresh_synthetic_protocol_gold(self, specification, paths):
        """Rebind the fixture gold so a tamper test reaches its target check."""

        role_sha256 = {}
        for asset in specification["assets"]:
            role_sha256.setdefault(asset["role"], []).append(
                sha256_file(Path(asset["path"]))
            )
        subjects = _expected_protocol_decision_subjects(
            role_sha256,
            read_json(paths["semantic_extractor_manifest"]),
        )
        review = read_json(paths["protocol_decision_review"])
        for decision in review["decisions"]:
            subject = subjects[decision["decision_id"]]
            decision["subject"] = subject
            decision["subject_sha256"] = sha256_bytes(canonical_json_bytes(subject))
            self._rehash_human_gold(decision)
        write_json(paths["protocol_decision_review"], review)

    def test_complete_cross_referenced_bundle_freezes_and_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, _ = build_formal_bundle(directory)
            manifest = create_freeze_manifest(**specification)
            self.assertEqual(manifest["status"], "frozen")
            self.assertTrue(verify_freeze_manifest(manifest)["verified"])

    def test_development_responses_bind_the_real_synthetic_implementation_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            config = read_json(paths["judge_calibration_source_config"])
            bundle = config["implementation_bundle"]
            bundle_path = Path(bundle["path"])
            self.assertEqual(bundle["sha256"], sha256_file(bundle_path))
            self.assertEqual(bundle["bytes"], bundle_path.stat().st_size)
            responses = read_jsonl(paths["development_response_records"])
            self.assertTrue(
                all(
                    row["implementation_bundle_sha256"] == bundle["sha256"]
                    for row in responses
                )
            )
            original_responses = copy.deepcopy(responses)
            responses[0]["implementation_bundle_sha256"] = "0" * 64
            write_jsonl(paths["development_response_records"], responses)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
            write_jsonl(paths["development_response_records"], original_responses)
            bundle_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(FreezeError, "file hash mismatch"):
                create_freeze_manifest(**specification)

    def test_uqh_assessor_registry_and_nested_sources_are_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            registry = read_json(paths["uqh_assessor_registry"])
            config_path = Path(registry["assessors"][0]["assessment_config"]["path"])
            config_path.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(FreezeError, "file hash mismatch"):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            registry = read_json(paths["uqh_assessor_registry"])
            registry["assessors"][0]["independent_from_producer_ids"] = []
            write_json(paths["uqh_assessor_registry"], registry)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            registry = read_json(paths["uqh_assessor_registry"])
            registry["assessors"][0]["independent_from_producer_ids"] = [
                "immutable-generation-runner",
                "rogue-generation-runner",
            ]
            write_json(paths["uqh_assessor_registry"], registry)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            registry = read_json(paths["uqh_assessor_registry"])
            key_binding = registry["assessors"][0]["attestation_public_key"]
            key_path = Path(key_binding["path"])
            key_path.write_text("not a public key\n", encoding="utf-8")
            key_binding["sha256"] = sha256_file(key_path)
            key_binding["bytes"] = key_path.stat().st_size
            write_json(paths["uqh_assessor_registry"], registry)
            with self.assertRaisesRegex(FreezeError, "public key is invalid"):
                create_freeze_manifest(**specification)

    def test_wrong_oracle_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            records = read_jsonl(paths["requirement_oracle_test"])
            records[0]["surface_style"] = "vague"
            write_jsonl(paths["requirement_oracle_test"], records)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)

    def test_retired_schema_cannot_enter_formal_extractor_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            extractor = read_json(paths["semantic_extractor_manifest"])
            legacy_schema = (
                Path(__file__).resolve().parents[2]
                / "benchmark_configs"
                / "schemas"
                / "human_adjudication_packet.schema.json"
            )
            extractor["schema_files"].append(
                {
                    "path": str(legacy_schema),
                    "sha256": sha256_file(legacy_schema),
                }
            )
            write_json(paths["semantic_extractor_manifest"], extractor)
            with self.assertRaisesRegex(
                FreezeError, "complete evaluator schemas are not frozen"
            ):
                create_freeze_manifest(**specification)

    def test_unverifiable_extractor_and_forged_judge_score_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            extractor = read_json(paths["semantic_extractor_manifest"])
            extractor["extractor_files"][0]["path"] = "/missing/extractor.py"
            write_json(paths["semantic_extractor_manifest"], extractor)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            extractor = read_json(paths["semantic_extractor_manifest"])
            source_path = extractor["extractor_files"][0]["path"]
            with open(source_path, "w", encoding="utf-8"):
                pass
            from pathlib import Path
            from bus_benchmark.jsonio import sha256_file

            new_hash = sha256_file(Path(source_path))
            extractor["extractor_files"][0]["sha256"] = new_hash
            for entrypoint in extractor["entrypoints"]:
                if entrypoint["path"] == source_path:
                    entrypoint["sha256"] = new_hash
            write_json(paths["semantic_extractor_manifest"], extractor)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            judge = read_json(paths["judge_rubric"])
            judge["macro_f1"] = 999.0
            write_json(paths["judge_rubric"], judge)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)

    def test_query_review_requires_one_complete_human_gold(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            reviews = read_jsonl(paths["human_query_gold"])
            reviews[0]["review_status"] = "uncertain"
            self._rehash_human_gold(reviews[0])
            write_jsonl(paths["human_query_gold"], reviews)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)

    def test_platform_requires_one_complete_human_gold(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            reviews = read_jsonl(paths["platform_fixture_review"])
            self.assertIn("reviewer", reviews[0])
            self.assertNotIn("annotator_a", reviews[0])
            reviews[0]["review_status"] = "uncertain"
            self._rehash_human_gold(reviews[0])
            write_jsonl(paths["platform_fixture_review"], reviews)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)

    def test_single_human_gold_contracts_without_native_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            records = self._formal_asset_records(specification)
            documents = _load_role_documents(records)
            record_by_role = {}
            for record in records:
                record_by_role.setdefault(record["role"], []).append(record)
            ontology_by_id = {
                concept["concept_id"]: concept
                for concept in documents["common_ontology_registry"][0]["concepts"]
            }
            _validate_platform_fixture_reviews(
                documents, ontology_by_id, record_by_role
            )
            _validate_protocol_decision_reviews(documents, record_by_role)

            query_gold = read_jsonl(paths["human_query_gold"])
            validate_schema_records(query_gold, "human_query_gold")
            self.assertEqual(len(query_gold), 300)
            self.assertTrue(all(row["review_status"] == "complete" for row in query_gold))
            self.assertTrue(all("annotator_a" not in row for row in query_gold))

            source_rows = read_jsonl(TEST_LIBRARY) + read_jsonl(DEV_LIBRARY)
            source_by_id = {row["query_id"]: row for row in source_rows}
            draft_by_id = {
                row["query_id"]: row
                for row in read_jsonl(TEST_ORACLE_DRAFT)
                + read_jsonl(DEV_ORACLE_DRAFT)
            }
            first = query_gold[0]
            _validate_review_payload(
                first["review_payload"],
                draft_by_id[first["query_id"]],
                source_by_id[first["query_id"]],
                "single-gold accept",
            )
            broken_accept = copy.deepcopy(first["review_payload"])
            broken_accept["draft_atom_reviews"][0]["after_sha256"] = "0" * 64
            with self.assertRaisesRegex(FreezeError, "after hash mismatch"):
                _validate_review_payload(
                    broken_accept,
                    draft_by_id[first["query_id"]],
                    source_by_id[first["query_id"]],
                    "broken accept",
                )

            revised = copy.deepcopy(first["review_payload"])
            atom_review = revised["draft_atom_reviews"][0]
            atom_id = atom_review["draft_atom_id"]
            revised_atom = next(
                atom for atom in revised["decision"]["atoms"] if atom["atom_id"] == atom_id
            )
            revised_atom["layer"] = (
                "permitted"
                if revised_atom["layer"] != "permitted"
                else "core_required"
            )
            atom_review.update(
                {
                    "verdict": "modify",
                    "replacement_atom_ids": [atom_id],
                    "after_sha256": _review_atom_hash([revised_atom]),
                    "changed_fields": ["layer"],
                }
            )
            _validate_review_payload(
                revised,
                draft_by_id[first["query_id"]],
                source_by_id[first["query_id"]],
                "single-gold revision",
            )
            revised["draft_atom_reviews"][0]["before_sha256"] = "0" * 64
            with self.assertRaisesRegex(FreezeError, "before hash mismatch"):
                _validate_review_payload(
                    revised,
                    draft_by_id[first["query_id"]],
                    source_by_id[first["query_id"]],
                    "broken revision",
                )

            platform_gold = read_jsonl(paths["platform_fixture_review"])
            validate_schema_records(platform_gold, "platform_fixture_review")
            self.assertTrue(all("annotator_a" not in row for row in platform_gold))
            protocol_gold = read_json(paths["protocol_decision_review"])
            validate_schema_instance(protocol_gold, "protocol_decision_review")
            self.assertTrue(
                all("annotator_a" not in row for row in protocol_gold["decisions"])
            )

            judge = read_json(paths["judge_rubric"])
            calibration_gold = read_jsonl(Path(judge["calibration"]["path"]))
            validate_schema_records(calibration_gold, "judge_calibration")
            predictions = {
                row["item_id"]: row["gold_verdict"] for row in calibration_gold
            }
            metrics = compute_judge_calibration(
                calibration_gold, predictions
            )
            self.assertEqual(metrics["record_count"], 180)
            self.assertEqual(metrics["macro_f1"], 1.0)

            uncertain = copy.deepcopy(calibration_gold)
            uncertain[0]["review_status"] = "uncertain"
            self._rehash_human_gold(uncertain[0])
            with self.assertRaises(ValidationError):
                compute_judge_calibration(uncertain, predictions)

    def test_review_cannot_silently_delete_all_draft_atoms(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            reviews = read_jsonl(paths["human_query_gold"])
            for review in reviews:
                review["review_payload"]["decision"]["atoms"] = []
                self._rehash_human_gold(review)
            write_jsonl(paths["human_query_gold"], reviews)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)

    def test_review_atom_changes_additions_and_implicit_merges_are_traced(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            reviews = read_jsonl(paths["human_query_gold"])
            reviews[0]["review_payload"]["decision"]["atoms"][0]["arguments"] = {
                "forged": True
            }
            self._rehash_human_gold(reviews[0])
            write_jsonl(paths["human_query_gold"], reviews)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            reviews = read_jsonl(paths["human_query_gold"])
            from bus_benchmark.atoms import make_atom

            added = make_atom(
                "road",
                "human_added_fixture",
                {"value": "new"},
                provenance={
                    "source": "human_review",
                    "query_id": reviews[0]["query_id"],
                },
                decision_status="confirmed",
            )
            reviews[0]["review_payload"]["decision"]["atoms"].append(added)
            self._rehash_human_gold(reviews[0])
            write_jsonl(paths["human_query_gold"], reviews)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            reviews = read_jsonl(paths["human_query_gold"])
            payload = next(
                review["review_payload"]
                for review in reviews
                if len(review["review_payload"]["decision"]["atoms"]) >= 2
            )
            first, second = payload["decision"]["atoms"][:2]
            first_review = next(
                review
                for review in payload["draft_atom_reviews"]
                if review["draft_atom_id"] == first["atom_id"]
            )
            from tests.bus_benchmark.formal_freeze_fixture import (
                _review_atom_hash,
                _review_atom_projection,
            )

            first_review.update(
                {
                    "verdict": "modify",
                    "replacement_atom_ids": [second["atom_id"]],
                    "reason": "invalid implicit reuse",
                    "after_sha256": _review_atom_hash([second]),
                    "changed_fields": sorted(
                        field
                        for field in _review_atom_projection(first)
                        if first[field] != second[field]
                    ),
                }
            )
            payload["decision"]["atoms"] = [
                atom for atom in payload["decision"]["atoms"] if atom is not first
            ]
            owner = next(
                review for review in reviews if review["review_payload"] is payload
            )
            self._rehash_human_gold(owner)
            write_jsonl(paths["human_query_gold"], reviews)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)

    def test_empty_shell_labels_and_three_item_calibration_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            reviews = read_jsonl(paths["human_query_gold"])
            for review in reviews:
                review["review_payload"]["decision"] = {
                    "query_id": review["query_id"]
                }
                self._rehash_human_gold(review)
            write_jsonl(paths["human_query_gold"], reviews)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            judge = read_json(paths["judge_rubric"])
            calibration_path = judge["calibration"]["path"]
            rows = read_jsonl(calibration_path)[:3]
            write_jsonl(calibration_path, rows)
            from pathlib import Path
            from bus_benchmark.jsonio import sha256_file

            judge["calibration"]["sha256"] = sha256_file(Path(calibration_path))
            write_json(paths["judge_rubric"], judge)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)

    def test_wrong_runtime_or_ego_contract_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            platform = read_json(paths["platform_config_carla"])
            platform["rollout_seconds"] = 999.0
            write_json(paths["platform_config_carla"], platform)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            ego = read_json(paths["ego_proxy_config"])
            ego["carla_blueprint"] = "vehicle.wrong"
            write_json(paths["ego_proxy_config"], ego)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)

    def test_placeholder_capabilities_and_unbound_calibration_atom_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            capability = read_json(paths["platform_capability_carla"])
            capability["concepts"] = [
                {"concept_id": "unrelated_placeholder", "status": "native"}
            ]
            write_json(paths["platform_capability_carla"], capability)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            judge = read_json(paths["judge_rubric"])
            calibration_path = judge["calibration"]["path"]
            rows = read_jsonl(calibration_path)
            rows[0]["atom_id"] = "f" * 16
            write_jsonl(calibration_path, rows)
            from pathlib import Path
            from bus_benchmark.jsonio import sha256_file

            judge["calibration"]["sha256"] = sha256_file(Path(calibration_path))
            write_json(paths["judge_rubric"], judge)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)

    def test_identity_extractor_cannot_self_certify_against_raw_fixture_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            extractor = read_json(paths["semantic_extractor_manifest"])
            source_path = extractor["extractor_files"][0]["path"]
            from pathlib import Path
            from bus_benchmark.jsonio import sha256_file

            Path(source_path).write_text(
                "def extract_fixture(payload):\n    return payload\n",
                encoding="utf-8",
            )
            new_hash = sha256_file(Path(source_path))
            extractor["extractor_files"][0]["sha256"] = new_hash
            for entrypoint in extractor["entrypoints"]:
                if entrypoint["path"] == source_path:
                    entrypoint["sha256"] = new_hash
            write_json(paths["semantic_extractor_manifest"], extractor)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)

    def test_fixture_truth_sidecars_controls_and_byte_forgery_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            corpus = read_json(paths["semantic_fixture_corpus"])
            case = corpus["cases"][0]
            case["input"]["artifact"]["text"] += "param fixture_fact = '{}'\n"
            encoded = case["input"]["artifact"]["text"].encode("utf-8")
            case["input"]["artifact"]["sha256"] = sha256_bytes(encoded)
            case["input"]["artifact"]["bytes"] = len(encoded)
            write_json(paths["semantic_fixture_corpus"], corpus)
            with self.assertRaisesRegex(FreezeError, "truth sidecar"):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            corpus = read_json(paths["semantic_fixture_corpus"])
            case = next(
                value
                for value in corpus["cases"]
                if value["platform"] == "carla"
                and value["test_kind"] == "ego_wrong_proxy"
            )
            missing = next(
                value
                for value in corpus["cases"]
                if value["platform"] == "carla"
                and value["test_kind"] == "ego_missing"
            )
            case["expected"] = missing["expected"]
            write_json(paths["semantic_fixture_corpus"], corpus)
            with self.assertRaisesRegex(FreezeError, "control"):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            corpus = read_json(paths["semantic_fixture_corpus"])
            corpus["cases"][0]["input"]["artifact"]["sha256"] = "0" * 64
            write_json(paths["semantic_fixture_corpus"], corpus)
            with self.assertRaisesRegex(FreezeError, "byte binding"):
                create_freeze_manifest(**specification)

    def test_cpd_and_cross_fixtures_require_complete_controlled_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            corpus = read_json(paths["semantic_fixture_corpus"])
            cpd_cases = [
                value
                for value in corpus["cases"]
                if value["platform"] == "carla"
                and value["concept_ids"] == ["cpd:actor_longitudinal_distance_bin"]
                and value["test_kind"] == "positive"
            ]
            self.assertGreaterEqual(len(cpd_cases), 2)
            cpd_cases[1]["expected"] = cpd_cases[0]["expected"]
            cpd_cases[1]["input"] = cpd_cases[0]["input"]
            write_json(paths["semantic_fixture_corpus"], corpus)
            with self.assertRaisesRegex(FreezeError, "every allowed value"):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            corpus = read_json(paths["cross_platform_fixture_corpus"])
            case = next(
                value
                for value in corpus["cases"]
                if value["semantic_category"] == "event"
                and value["expected_equal"] is True
            )
            empty = next(
                value
                for value in read_json(paths["semantic_fixture_corpus"])["cases"]
                if value["platform"] == "carla"
                and value["concept_ids"] == [case["concept_id"]]
                and value["test_kind"] == "negative"
            )
            case["expected_left"] = empty["expected"]
            case["expected_right"] = empty["expected"]
            case["left_input"] = empty["input"]
            right_empty = next(
                value
                for value in read_json(paths["semantic_fixture_corpus"])["cases"]
                if value["platform"] == "metadrive"
                and value["concept_ids"] == [case["concept_id"]]
                and value["test_kind"] == "negative"
            )
            case["right_input"] = right_empty["input"]
            write_json(paths["cross_platform_fixture_corpus"], corpus)
            with self.assertRaisesRegex(FreezeError, "changed concept"):
                create_freeze_manifest(**specification)

    def test_cpd_joint_coverage_is_part_of_the_frozen_policy_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            coverage = read_json(paths["cpd_policy"])
            coverage["joint"]["eligible"] -= 1
            coverage["joint"]["coverage"] = (
                coverage["joint"]["eligible"]
                / coverage["joint"]["candidate"]
            )
            write_json(paths["cpd_policy"], coverage)

            with self.assertRaisesRegex(
                FreezeError, "CPD policy summary differs"
            ):
                create_freeze_manifest(**specification)

    def test_judge_predictions_only_come_from_bound_raw_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            judge = read_json(paths["judge_rubric"])
            rows = read_jsonl(Path(judge["calibration"]["path"]))
            rows[0]["predicted_verdict"] = "satisfied"
            self._rewrite_calibration_binding(paths, rows)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            run_directory = Path(paths["judge_calibration_run_manifest"]).parent
            response_rows = read_jsonl(run_directory / "judge_responses.jsonl")
            response_rows[0]["raw_response"] = "not-json"
            response_rows[0]["raw_response_sha256"] = sha256_bytes(b"not-json")
            write_jsonl(run_directory / "judge_responses.jsonl", response_rows)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)

    def test_judge_development_chain_is_live_complete_and_config_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            evidence = read_jsonl(paths["development_evidence_records"])
            evidence[-1]["ego"]["length_m"] = 5.0
            write_jsonl(paths["development_evidence_records"], evidence)
            self._refresh_synthetic_protocol_gold(specification, paths)
            with self.assertRaisesRegex(FreezeError, "live extractor"):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            responses = read_jsonl(paths["development_response_records"])
            write_jsonl(paths["development_response_records"], responses[:-1])
            self._refresh_synthetic_protocol_gold(specification, paths)
            with self.assertRaises(FreezeError):
                create_freeze_manifest(**specification)
        with tempfile.TemporaryDirectory() as directory:
            specification, paths = build_formal_bundle(directory)
            judge = read_json(paths["judge_rubric"])
            prompt = Path(judge["prompt"]["path"])
            prompt.write_text(prompt.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
            judge["prompt"]["sha256"] = sha256_file(prompt)
            judge["prompt"]["bytes"] = prompt.stat().st_size
            judge["judge_config_sha256"] = judge_config_sha256(
                model_id=judge["model_id"],
                prompt_sha256=judge["prompt"]["sha256"],
                inference_config=judge["inference_config"],
                output_schema_sha256=judge["output_schema"]["sha256"],
                request_contract=judge["request_contract"],
                runner_manifest_sha256=judge["runner_manifest_sha256"],
            )
            write_json(paths["judge_rubric"], judge)
            self._refresh_synthetic_protocol_gold(specification, paths)
            with self.assertRaisesRegex(
                FreezeError,
                "judge calibration context prompt differs from rubric",
            ):
                create_freeze_manifest(**specification)


if __name__ == "__main__":
    unittest.main()
