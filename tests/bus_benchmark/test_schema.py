import copy
import json
import unittest
from pathlib import Path

from bus_benchmark.calibration import (
    build_judge_calibration_roster,
    compute_judge_calibration,
)
from bus_benchmark.errors import ValidationError
from bus_benchmark.jsonio import canonical_json_bytes, sha256_bytes
from bus_benchmark.schema import (
    available_schemas,
    load_schema,
    validate_schema_instance,
    validate_schema_records,
)


SHA = "a" * 64
CREATED_AT = "2026-07-14T00:00:00+00:00"


def raw_method_response():
    envelope = {
        "stdout": {"path": "/tmp/method.stdout", "sha256": SHA, "bytes": 10},
        "stderr": {"path": "/tmp/method.stderr", "sha256": SHA, "bytes": 0},
        "exit_code": 0,
        "timed_out": False,
    }
    return dict(
        envelope,
        envelope_sha256=sha256_bytes(canonical_json_bytes(envelope)),
    )


def unsupported_handling(raw_response):
    return {
        "response_stream": "stdout",
        "response_sha256": raw_response["stdout"]["sha256"],
    }


def common_metadata():
    return {
        "schema_version": "0.1",
        "run_id": "run-001",
        "method_id": "method-a",
        "platform": "carla",
        "query_id": "query-001",
        "intent_group_id": "intent-001",
        "statistical_intent_cluster_id": "cluster-001",
        "surface_style": "partial",
        "repetition": 0,
        "expected_support": "supported",
    }


def response_record():
    value = common_metadata()
    value.update(
        {
            "disposition": "generate",
            "request_sha256": SHA,
            "config_sha256": SHA,
            "implementation_bundle_sha256": SHA,
            "terminal_status": "complete",
            "artifact": {"path": "/tmp/scene.scenic", "sha256": SHA, "bytes": 20},
            "raw_method_response": raw_method_response(),
            "provenance": {
                "producer_id": "immutable-harness",
                "producer_version": "0.1",
                "created_at_utc": CREATED_AT,
            },
        }
    )
    return value


def semantic_evidence():
    value = common_metadata()
    value.update(
        {
            "terminal_status": "complete",
            "ego": {
                "count": 1,
                "semantic_role": "bus_proxy",
                "approved_proxy": True,
                "deterministic": True,
                "length_m": 5.33,
                "width_m": 2.1,
                "blueprint": "vehicle.chevrolet.impala",
            },
            "atoms": [
                {
                    "category": "event",
                    "predicate": "event_spec",
                    "arguments": {"event": "bus_docking"},
                    "polarity": "present",
                    "evidence_source": "deterministic",
                    "confidence": 1.0,
                }
            ],
            "common_atoms": [
                {"dimension": "event_realization_mode", "value": "decelerate"}
            ],
            "complete_categories": ["event"],
            "provenance": {
                "source_response_sha256": SHA,
                "source_artifact_sha256": SHA,
                "extractor_id": "semantic-extractor",
                "extractor_version": "0.1",
                "extractor_config_sha256": SHA,
                "created_at_utc": CREATED_AT,
            },
        }
    )
    return value


def f1_diagnostic():
    return {
        "availability": "available",
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
        "true_positive": 1,
        "false_positive": 0,
        "false_negative": 0,
        "gold_count": 1,
        "predicted_count": 1,
        "judge_true_positive": 0,
    }


def semantic_score():
    value = common_metadata()
    value.update(
        {
            "ego_gate_passed": True,
            "srs": 1.0,
            "srs_common": 1.0,
            "srs_category_scores": {"event": 1.0},
            "srs_common_category_scores": {"event": 1.0},
            "common_core_preservation": {
                "all_common_core_satisfied": True,
                "no_common_forbidden": True,
                "evidence_complete": True,
            },
            "arc": f1_diagnostic(),
            "rsc": f1_diagnostic(),
            "iec_spec": f1_diagnostic(),
            "atom_results": [
                {
                    "atom_id": "a" * 16,
                    "category": "event",
                    "layer": "core_required",
                    "polarity": "present",
                    "verdict": "satisfied",
                    "verdict_source": "deterministic",
                    "satisfied": True,
                    "weight": 1.0,
                }
            ],
            "judge_bindings_sha256": SHA,
            "provenance": {
                "oracle_sha256": SHA,
                "oracle_record_sha256": SHA,
                "evidence_record_sha256": SHA,
                "response_record_sha256": SHA,
                "roster_sha256": SHA,
                "extractor_manifest_sha256": SHA,
                "evaluator_source_sha256": SHA,
                "freeze_manifest_sha256": SHA,
                "scorer_id": "semantic-scorer",
                "scorer_version": "0.1",
                "created_at_utc": CREATED_AT,
            },
        }
    )
    return value


def cpd_input_record():
    value = common_metadata()
    value.update(
        {
            "terminal_status": "complete",
            "common_atoms": [
                {"dimension": "actor_longitudinal_distance_bin", "value": "near"}
            ],
            "srs_common": 1.0,
            "preservation": {
                "ego_gate_passed": True,
                "all_common_core_satisfied": True,
                "no_common_forbidden": True,
                "evidence_complete": True,
            },
            "provenance": {
                "semantic_score_sha256": SHA,
                "cpd_policy_sha256": SHA,
                "evidence_record_sha256": SHA,
                "extractor_manifest_sha256": SHA,
                "extractor_id": "cpd-projector",
                "extractor_version": "0.1",
                "created_at_utc": CREATED_AT,
            },
        }
    )
    return value


def judge_output():
    return {
        "evidence": ["The generated artifact explicitly contains the event."],
        "rationale": "The deterministic evidence and artifact agree.",
        "verdict": "satisfied",
        "confidence": 1.0,
    }


def judge_response_record():
    raw_response = json.dumps(judge_output(), sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": "0.1",
        "judge_response_id": SHA,
        "item_id": SHA,
        "query_id": "query-001",
        "run_id": "run-001",
        "atom_id": "a" * 16,
        "attempt": 0,
        "model_id": "judge-model",
        "judge_config_sha256": SHA,
        "judge_request_sha256": SHA,
        "terminal_status": "complete",
        "raw_response": raw_response,
        "raw_response_sha256": SHA,
        "provenance": {
            "runner_id": "judge-runner",
            "runner_version": "0.1",
            "provider_request_id": None,
            "provider_response_id": None,
            "created_at_utc": CREATED_AT,
        },
    }


def judge_calibration_record():
    value = {
        "schema_version": "0.1",
        "workflow_type": "judge_calibration_human_gold",
        "item_id": SHA,
        "platform": "carla",
        "source_id": "benchmark-reference-carla-v0.1",
        "query_id": "query-001",
        "run_id": "run-001",
        "repetition": 0,
        "atom_id": "a" * 16,
        "category": "event",
        "surface_style": "partial",
        "source_response_sha256": SHA,
        "source_evidence_sha256": SHA,
        "reviewer_id": "reviewer-a",
        "gold_verdict": "satisfied",
        "gold_evidence": ["artifact evidence"],
        "gold_rationale": "reviewer rationale",
        "review_status": "complete",
        "canonical_source_revalidated": True,
        "reviewer_blind_to_judge_prediction": True,
        "decision_status": "confirmed",
    }
    value["gold_record_id"] = sha256_bytes(canonical_json_bytes(value))
    return value


def rehash_human_gold(record):
    record.pop("gold_record_id", None)
    record["gold_record_id"] = sha256_bytes(canonical_json_bytes(record))


def judge_calibration_roster_record():
    item = {
        "item_id": SHA,
        "platform": "carla",
        "source_id": "benchmark-reference-carla-v0.1",
        "query_id": "query-001",
        "run_id": "run-001",
        "repetition": 0,
        "atom_id": "a" * 16,
        "category": "event",
        "surface_style": "partial",
        "source_response_sha256": SHA,
        "source_evidence_sha256": SHA,
    }
    items = []
    for platform in ("carla", "metadrive"):
        for index in range(90):
            selected = copy.deepcopy(item)
            selected["item_id"] = "{:064x}".format(index + (0 if platform == "carla" else 90))
            selected["platform"] = platform
            selected["source_id"] = "benchmark-reference-{}-v0.1".format(platform)
            selected["run_id"] = "{}-run-{:03d}".format(platform, index)
            items.append(selected)
    return {
        "schema_version": "0.1",
        "decision_status": "confirmed",
        "selection_algorithm": "query_category_then_sha256_v0.1",
        "seed": "bus-judge-calibration-v0.1",
        "items_per_platform": 90,
        "eligible_population_count_by_platform": {"carla": 90, "metadrive": 90},
        "eligible_population_sha256": SHA,
        "items_sha256": SHA,
        "items": items,
    }


def judge_rubric_record():
    file_binding = {"path": "/tmp/frozen-asset", "sha256": SHA, "bytes": 1}
    return {
        "decision_status": "confirmed",
        "request_contract": "judge_atom_v0.1",
        "model_id": "judge-model",
        "inference_config": {"temperature": 0.0},
        "prompt": copy.deepcopy(file_binding),
        "output_schema": copy.deepcopy(file_binding),
        "runner_manifest_sha256": SHA,
        "judge_config_sha256": SHA,
        "calibration": copy.deepcopy(file_binding),
        "calibration_roster": copy.deepcopy(file_binding),
        "calibration_context": copy.deepcopy(file_binding),
        "calibration_request_manifest": copy.deepcopy(file_binding),
        "calibration_run_manifest": copy.deepcopy(file_binding),
        "calibration_record_count": 180,
        "macro_f1": 1.0,
        "cohen_kappa": 1.0,
        "gold_support_by_verdict": {
            "satisfied": 176,
            "violated": 2,
            "unknown": 2,
        },
        "platform_metrics": {
            platform: {
                "record_count": 90,
                "macro_f1": 1.0,
                "cohen_kappa": 1.0,
                "gold_support_by_verdict": {
                    "satisfied": 88,
                    "violated": 1,
                    "unknown": 1,
                },
            }
            for platform in ("carla", "metadrive")
        },
    }


class RuntimeSchemaTests(unittest.TestCase):
    def assert_invalid(self, value, schema_name):
        with self.assertRaises(ValidationError):
            validate_schema_instance(value, schema_name)

    def test_all_registered_schemas_load_and_check(self):
        schema_names = available_schemas()
        self.assertIn("uqh_assessment", schema_names)
        for schema_name in schema_names:
            self.assertIsInstance(load_schema(schema_name), dict)

    def test_response_requires_identity_metadata_and_provenance(self):
        value = response_record()
        self.assertIs(validate_schema_instance(value, "response_record"), value)
        missing_run = copy.deepcopy(value)
        missing_run.pop("run_id")
        self.assert_invalid(missing_run, "response_record")
        missing_provenance = copy.deepcopy(value)
        missing_provenance.pop("provenance")
        self.assert_invalid(missing_provenance, "response_record")

    def test_response_requires_artifact_for_completed_generation(self):
        value = response_record()
        value.pop("artifact")
        self.assert_invalid(value, "response_record")
        value = response_record()
        value["artifact"] = None
        self.assert_invalid(value, "response_record")

    def test_integer_fields_explicitly_reject_bool_and_integral_float(self):
        for invalid in (True, 1.0):
            value = response_record()
            value["repetition"] = invalid
            self.assert_invalid(value, "response_record")
        value = response_record()
        value["artifact"]["bytes"] = False
        self.assert_invalid(value, "response_record")

    def test_unsupported_label_does_not_allow_harness_assessment_in_response(self):
        value = response_record()
        value["expected_support"] = "unsupported"
        validate_schema_instance(value, "response_record")
        value["unsupported_handling"] = {
            "valid": False,
            "decision_source": "deterministic",
            "reason_code": "generated_when_reject_expected",
        }
        self.assert_invalid(value, "response_record")

    def test_completed_handling_requires_only_a_raw_response_locator(self):
        for disposition in ("reject", "clarification", "controlled_degradation"):
            value = response_record()
            value["disposition"] = disposition
            if disposition in ("reject", "clarification"):
                value["artifact"] = None
            self.assert_invalid(value, "response_record")
            value["unsupported_handling"] = unsupported_handling(
                value["raw_method_response"]
            )
            validate_schema_instance(value, "response_record")

    def test_raw_method_response_is_required_and_strict(self):
        value = response_record()
        value.pop("raw_method_response")
        self.assert_invalid(value, "response_record")
        value = response_record()
        value["raw_method_response"]["stdout"]["bytes"] = False
        self.assert_invalid(value, "response_record")

    def test_non_generation_dispositions_have_fail_closed_artifact_shapes(self):
        for disposition in ("reject", "clarification"):
            value = response_record()
            value.update({"disposition": disposition, "artifact": None})
            value["unsupported_handling"] = unsupported_handling(
                value["raw_method_response"]
            )
            validate_schema_instance(value, "response_record")
            value["artifact"] = response_record()["artifact"]
            self.assert_invalid(value, "response_record")
            value.update({"artifact": None, "terminal_status": "failed"})
            self.assert_invalid(value, "response_record")

        degraded = response_record()
        degraded["disposition"] = "controlled_degradation"
        degraded["unsupported_handling"] = unsupported_handling(
            degraded["raw_method_response"]
        )
        validate_schema_instance(degraded, "response_record")
        degraded["artifact"] = None
        self.assert_invalid(degraded, "response_record")

    def test_uqh_assessment_schema_separates_verdict_from_method_response(self):
        response = response_record()
        response.update({"disposition": "reject", "artifact": None})
        response["unsupported_handling"] = unsupported_handling(
            response["raw_method_response"]
        )
        response_digest = sha256_bytes(canonical_json_bytes(response))
        envelope_digest = response["raw_method_response"]["envelope_sha256"]
        assessment = {
            "schema_version": "0.1",
            "assessment_id": SHA,
            "assessment_content_sha256": SHA,
            "run_id": response["run_id"],
            "query_id": response["query_id"],
            "disposition": "reject",
            "response_record_sha256": response_digest,
            "raw_response_envelope_sha256": envelope_digest,
            "oracle_record_sha256": SHA,
            "verdict": "valid",
            "reason_codes": ["rule_state_contradiction"],
            "assessment_source": "judge",
            "evidence": [
                {
                    "response_stream": "stdout",
                    "byte_start": 0,
                    "byte_end": 10,
                    "bytes_sha256": SHA,
                }
            ],
            "rationale": "Independent assessment of the immutable method output.",
            "provenance": {
                "assessor_id": "uqh-judge",
                "assessor_version": "0.1",
                "assessor_registry_sha256": SHA,
                "assessment_config_sha256": SHA,
                "assessor_source_sha256": SHA,
                "assessor_request_sha256": SHA,
                "raw_assessor_request": {
                    "path": "/tmp/uqh-assessor-request.json",
                    "sha256": SHA,
                    "bytes": 1,
                },
                "raw_assessor_response": {
                    "path": "/tmp/uqh-assessor-response.json",
                    "sha256": SHA,
                    "bytes": 1,
                },
                "external_execution_id": "provider-execution-1",
                "created_at_utc": CREATED_AT,
            },
            "attestation": {
                "algorithm": "rsa_pss_sha256",
                "signed_payload_sha256": SHA,
                "signature": {
                    "path": "/tmp/uqh-assessment.sig",
                    "sha256": SHA,
                    "bytes": 256,
                },
            },
        }
        validate_schema_instance(assessment, "uqh_assessment")
        assessment["reason_codes"] = []
        self.assert_invalid(assessment, "uqh_assessment")
        assessment["reason_codes"] = ["rule_state_contradiction"]
        assessment["disposition"] = "controlled_degradation"
        self.assert_invalid(assessment, "uqh_assessment")

    def test_uqh_assessor_registry_requires_frozen_file_and_independence_bindings(self):
        registry = {
            "schema_version": "0.1",
            "status": "frozen",
            "protocol_version": "0.1",
            "assessment_binding_contract": "uqh_assessment_binding_v0.1",
            "controlled_degradation_credit": False,
            "assessors": [
                {
                    "assessor_id": "uqh-judge",
                    "assessor_version": "0.1",
                    "assessment_source": "judge",
                    "independent_from_producer_ids": ["immutable-generation-runner"],
                    "assessment_config": {
                        "path": "/tmp/uqh-config.json",
                        "sha256": SHA,
                        "bytes": 1,
                    },
                    "assessor_source": {
                        "path": "/tmp/uqh-source.py",
                        "sha256": SHA,
                        "bytes": 1,
                    },
                    "attestation_algorithm": "rsa_pss_sha256",
                    "attestation_public_key": {
                        "path": "/tmp/uqh-public-key.pem",
                        "sha256": SHA,
                        "bytes": 1,
                    },
                    "key_custody": "external_to_method_runner",
                }
            ],
        }
        validate_schema_instance(registry, "uqh_assessor_registry")
        registry["controlled_degradation_credit"] = True
        self.assert_invalid(registry, "uqh_assessor_registry")
        registry["controlled_degradation_credit"] = False
        registry["assessors"][0]["independent_from_producer_ids"].append(
            "rogue-generation-runner"
        )
        self.assert_invalid(registry, "uqh_assessor_registry")

    def test_semantic_evidence_requires_extractor_provenance(self):
        value = semantic_evidence()
        validate_schema_instance(value, "semantic_evidence")
        value["provenance"].pop("source_artifact_sha256")
        self.assert_invalid(value, "semantic_evidence")

    def test_metric_fields_are_finite_numbers_in_closed_unit_interval(self):
        for invalid in (-0.01, 1.01, True, float("nan"), float("inf")):
            value = semantic_score()
            value["srs"] = invalid
            self.assert_invalid(value, "semantic_score")
        value = semantic_evidence()
        value["atoms"][0]["confidence"] = 1.1
        self.assert_invalid(value, "semantic_evidence")

    def test_supported_and_unsupported_semantic_score_contracts(self):
        validate_schema_instance(semantic_score(), "semantic_score")
        value = common_metadata()
        value.update(
            {
                "expected_support": "unsupported",
                "semantic_metrics": None,
                "reason": "scored through UQH only",
                "provenance": semantic_score()["provenance"],
            }
        )
        validate_schema_instance(value, "semantic_score")

    def test_cpd_input_requires_complete_metadata_and_provenance(self):
        value = cpd_input_record()
        validate_schema_instance(value, "cpd_input_record")
        value.pop("statistical_intent_cluster_id")
        self.assert_invalid(value, "cpd_input_record")
        value = cpd_input_record()
        value["srs_common"] = 2.0
        self.assert_invalid(value, "cpd_input_record")

    def test_freeze_core_records_and_local_references(self):
        asset = {
            "role": "query_library_test",
            "path": "/tmp/library.jsonl",
            "sha256": SHA,
            "bytes": 10,
        }
        protocol = {
            "repetitions": 5,
            "sv_seeds": [0, 1, 2, 3, 4],
            "sv_max_iterations": 2000,
            "rollout_seconds": 30.0,
            "query_input_fields": ["query_text"],
            "post_output_repair": False,
            "test_library_locked": True,
            "benchmark_suite_visibility": "design_visible",
            "held_out_generalization_claim": False,
        }
        roles = [
            "query_library_test",
            "query_library_dev",
            "requirement_oracle_test",
            "requirement_oracle_dev",
            "cpd_policy",
            "common_ontology_registry",
            "statistical_clusters",
            "platform_capability_carla",
            "platform_capability_metadrive",
            "semantic_extractor_manifest",
            "semantic_fixture_corpus",
            "cross_platform_fixture_corpus",
            "judge_rubric",
            "development_query_roster",
            "development_response_records",
            "development_evidence_records",
            "judge_calibration_context_manifest",
            "judge_calibration_request_bundle_manifest",
            "judge_calibration_run_manifest",
            "platform_fixture_review",
            "protocol_decision_review",
            "human_query_gold",
            "method_registry",
            "method_config",
            "query_roster",
            "platform_config_carla",
            "platform_config_metadrive",
            "controller_config_carla",
            "controller_config_metadrive",
            "ego_proxy_config",
        ]
        assets = [
            dict(asset, role=role, path="/tmp/{}.json".format(role)) for role in roles
        ]
        manifest = {
            "schema_version": "0.1",
            "benchmark_version": "bus-query-v0.1",
            "created_at_utc": CREATED_AT,
            "status": "frozen",
            "unfinished_decision_count": 0,
            "unfinished_decision_paths": [],
            "assets": assets,
            "protocol": protocol,
            "manifest_sha256": SHA,
        }
        validate_schema_instance(asset, "freeze_asset")
        validate_schema_instance(protocol, "freeze_protocol")
        validate_schema_instance(manifest, "freeze_manifest")
        invalid = copy.deepcopy(manifest)
        invalid["assets"][0]["bytes"] = True
        self.assert_invalid(invalid, "freeze_manifest")

    def test_existing_oracle_schema_resolves_requirement_atom_locally(self):
        path = Path("benchmark_artifacts/drafts/dev_oracle_draft.jsonl")
        with path.open("r", encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream]
        record = records[0]
        validate_schema_instance(record, "oracle_record")
        record.pop("unsupported_reasons")
        self.assert_invalid(record, "oracle_record")
        unsupported = next(
            value for value in records if value["expected_support"] == "unsupported"
        )
        unsupported["unsupported_reasons"] = []
        self.assert_invalid(unsupported, "oracle_record")

    def test_actor_bound_cpd_dimensions_require_only_the_frozen_selector(self):
        records = [
            json.loads(line)
            for line in Path(
                "benchmark_artifacts/drafts/test_oracle_draft.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        actor_bound = next(
            record
            for record in records
            if any(
                dimension["name"] == "actor_longitudinal_distance_bin"
                for dimension in record["cpd_policy"]["dimensions"]
            )
        )
        validate_schema_instance(actor_bound, "oracle_record")
        missing_selector = copy.deepcopy(actor_bound)
        next(
            dimension
            for dimension in missing_selector["cpd_policy"]["dimensions"]
            if dimension["name"] == "actor_longitudinal_distance_bin"
        ).pop("target_selector")
        self.assert_invalid(missing_selector, "oracle_record")

        non_actor = next(
            record
            for record in records
            if any(
                dimension["name"] == "optional_road_feature_presence"
                for dimension in record["cpd_policy"]["dimensions"]
            )
        )
        forged_selector = copy.deepcopy(non_actor)
        next(
            dimension
            for dimension in forged_selector["cpd_policy"]["dimensions"]
            if dimension["name"] == "optional_road_feature_presence"
        )["target_selector"] = copy.deepcopy(
            next(
                dimension
                for dimension in actor_bound["cpd_policy"]["dimensions"]
                if dimension["name"] == "actor_longitudinal_distance_bin"
            )["target_selector"]
        )
        self.assert_invalid(forged_selector, "oracle_record")

    def test_invalid_timestamp_and_unknown_schema_are_clear_failures(self):
        value = response_record()
        value["provenance"]["created_at_utc"] = "not-a-date"
        self.assert_invalid(value, "response_record")
        with self.assertRaisesRegex(ValidationError, "unknown benchmark schema"):
            validate_schema_instance({}, "missing")

    def test_record_iterable_reports_exact_count(self):
        result = validate_schema_records(
            [response_record(), response_record()], "response_record", context="responses"
        )
        self.assertEqual(result, {"schema": "response_record", "record_count": 2})

    def test_judge_output_is_strict_and_requires_explanation(self):
        value = judge_output()
        validate_schema_instance(value, "judge_output")
        for field in ("evidence", "rationale", "verdict", "confidence"):
            invalid = copy.deepcopy(value)
            invalid.pop(field)
            self.assert_invalid(invalid, "judge_output")
        invalid = copy.deepcopy(value)
        invalid["predicted_verdict"] = "satisfied"
        self.assert_invalid(invalid, "judge_output")

    def test_judge_response_keeps_only_raw_bound_output(self):
        value = judge_response_record()
        validate_schema_instance(value, "judge_response_record")
        for field in ("judge_request_sha256", "raw_response", "raw_response_sha256"):
            invalid = copy.deepcopy(value)
            invalid.pop(field)
            self.assert_invalid(invalid, "judge_response_record")
        invalid = copy.deepcopy(value)
        invalid["predicted_verdict"] = "satisfied"
        self.assert_invalid(invalid, "judge_response_record")

    def test_judge_calibration_is_bound_and_has_no_self_reported_prediction(self):
        value = judge_calibration_record()
        validate_schema_instance(value, "judge_calibration")
        for field in (
            "source_response_sha256",
            "source_evidence_sha256",
            "canonical_source_revalidated",
            "reviewer_blind_to_judge_prediction",
        ):
            invalid = copy.deepcopy(value)
            invalid.pop(field)
            self.assert_invalid(invalid, "judge_calibration")
        invalid = copy.deepcopy(value)
        invalid["reviewer_blind_to_judge_prediction"] = False
        self.assert_invalid(invalid, "judge_calibration")
        invalid = copy.deepcopy(value)
        invalid["predicted_verdict"] = "satisfied"
        self.assert_invalid(invalid, "judge_calibration")
        for field in ("judge_response_id", "judge_response_record_sha256"):
            invalid = copy.deepcopy(value)
            invalid[field] = SHA
            self.assert_invalid(invalid, "judge_calibration")

    def test_judge_calibration_roster_uses_the_frozen_seed(self):
        value = judge_calibration_roster_record()
        validate_schema_instance(value, "judge_calibration_roster")
        value["seed"] = "searched-for-balanced-verdicts"
        self.assert_invalid(value, "judge_calibration_roster")
        with self.assertRaisesRegex(ValidationError, "version-frozen selection seed"):
            build_judge_calibration_roster([], seed="searched-for-balanced-verdicts")

    def test_judge_calibration_covers_observed_not_fabricated_categories(self):
        candidates = []
        for platform_index, platform in enumerate(("carla", "metadrive")):
            for index in range(90):
                candidates.append(
                    {
                        "item_id": "{:064x}".format(platform_index * 90 + index),
                        "platform": platform,
                        "source_id": "{}-source".format(platform),
                        "query_id": "{}-query-{:03d}".format(platform, index),
                        "run_id": "{}-run-{:03d}".format(platform, index),
                        "repetition": 0,
                        "atom_id": "{:016x}".format(index),
                        "category": "event",
                        "surface_style": "precise",
                        "source_response_sha256": SHA,
                        "source_evidence_sha256": SHA,
                    }
                )
        roster = build_judge_calibration_roster(
            candidates, seed="bus-judge-calibration-v0.1"
        )
        self.assertEqual(len(roster["items"]), 180)
        self.assertEqual({item["category"] for item in roster["items"]}, {"event"})

    def test_judge_rubric_reports_actual_support_without_a_twenty_item_quota(self):
        value = judge_rubric_record()
        validate_schema_instance(value, "judge_rubric")
        invalid = copy.deepcopy(value)
        invalid.pop("gold_support_by_verdict")
        self.assert_invalid(invalid, "judge_rubric")
        invalid = copy.deepcopy(value)
        invalid["platform_metrics"]["carla"].pop("gold_support_by_verdict")
        self.assert_invalid(invalid, "judge_rubric")

        records = []
        predictions = {}
        for platform_offset, platform in enumerate(("carla", "metadrive")):
            verdicts = ["violated", "unknown"] + ["satisfied"] * 88
            for index, verdict in enumerate(verdicts):
                record = judge_calibration_record()
                record["platform"] = platform
                record["item_id"] = "{:064x}".format(platform_offset * 90 + index)
                record["run_id"] = "{}-run-{:03d}".format(platform, index)
                record["gold_verdict"] = verdict
                rehash_human_gold(record)
                records.append(record)
                predictions[record["item_id"]] = verdict
        metrics = compute_judge_calibration(records, predictions)
        self.assertEqual(
            metrics["gold_support_by_verdict"],
            {"satisfied": 176, "violated": 2, "unknown": 2},
        )
        for platform in ("carla", "metadrive"):
            self.assertEqual(
                metrics["by_platform"][platform]["gold_support_by_verdict"],
                {"satisfied": 88, "violated": 1, "unknown": 1},
            )
        unvalidated = copy.deepcopy(records)
        unvalidated[0]["canonical_source_revalidated"] = False
        rehash_human_gold(unvalidated[0])
        with self.assertRaisesRegex(ValidationError, "unvalidated human gold"):
            compute_judge_calibration(unvalidated, predictions)


if __name__ == "__main__":
    unittest.main()
