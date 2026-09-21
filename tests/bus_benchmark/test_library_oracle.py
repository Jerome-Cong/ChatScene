import copy
import json
import unittest
from pathlib import Path

from bus_benchmark.atoms import normalize_count
from bus_benchmark.cpd import validate_cpd_policy_routing
from bus_benchmark.errors import ValidationError
from bus_benchmark.jsonio import canonical_json_bytes, read_jsonl, sha256_bytes
from bus_benchmark.library import (
    audit_dev_test_overlap,
    find_internal_duplicate_candidates,
    flatten_rule_hooks,
    validate_library,
    validate_record,
)
from bus_benchmark.oracle import (
    build_review_tasks,
    cpd_dimension_names,
    draft_oracle,
    draft_oracle_record,
    restrict_cpd_eligibility,
)


ROOT = Path(__file__).resolve().parents[2]
TEST_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_query_library_v0_2.jsonl"
DEV_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
LEGACY_TEST_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_query_library_v0_1.jsonl"
LEGACY_DEV_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_dev_query_library_v0_1.jsonl"


class LibraryValidationTests(unittest.TestCase):
    def test_locked_test_contract(self):
        report = validate_library(TEST_LIBRARY, 252, 84, 228)
        self.assertEqual(report["unsupported_records"], 24)
        self.assertEqual(report["supported_intents"], 76)
        self.assertEqual(report["metadata_schema_version"], "0.2")
        self.assertEqual(report["query_library_version"], "BSG_v0_2")
        self.assertEqual(report["dataset_split"], "test")
        self.assertTrue(report["triplets_complete"])

    def test_development_contract(self):
        report = validate_library(DEV_LIBRARY, 48, 16, 36)
        self.assertEqual(report["unsupported_intents"], 4)
        self.assertEqual(report["query_library_version"], "BSG_DEV_v0_2")
        self.assertEqual(report["dataset_split"], "development")

    def test_v02_rejects_unscoped_rule_hook_downgrade(self):
        record = copy.deepcopy(read_jsonl(TEST_LIBRARY)[0])
        record["rule_hooks"] = ["following_vehicle_lane_discipline"]
        with self.assertRaisesRegex(ValidationError, "scoped object"):
            validate_record(record)

    def test_v02_rejects_ego_reaction_event_even_when_signature_is_synced(self):
        for event in (
            "bus_hard_braking",
            "bus_collision_avoidance",
            "ego_bus_emergency_braking",
            "bus_speed_reduction",
            "ego_evasive_maneuver",
            "bus_stop_emergency_braking",
            "bus_stop_yield_response",
        ):
            record = copy.deepcopy(read_jsonl(TEST_LIBRARY)[0])
            record["interaction_events"] = [event]
            record["core_event_signature"]["events"] = [event]
            with self.assertRaisesRegex(ValidationError, "must not specify ego reaction"):
                validate_record(record)
            with self.assertRaisesRegex(ValidationError, "must not specify ego reaction"):
                draft_oracle_record(record)

    def test_v02_rejects_query_text_that_prescribes_ego_reaction(self):
        examples = (
            "This requires the ego bus to brake.",
            "The ego bus must brake.",
            "The bus will yield.",
            "Make the bus wait.",
            "The ego bus reacts by stopping.",
            "The bus brakes to avoid it.",
        )
        for text in examples:
            record = copy.deepcopy(read_jsonl(TEST_LIBRARY)[0])
            record["query_text"] += " " + text
            with self.subTest(text=text), self.assertRaisesRegex(
                ValidationError, "must not prescribe ego reaction"
            ):
                validate_record(record)

    def test_v02_rejects_ego_or_bus_reaction_rule_hook_subjects(self):
        attacks = (
            ("scene_rules", "ego_bus_yields_to_pedestrian"),
            ("counterpart_actor_rules", "ego_brakes_for_vehicle"),
            ("counterpart_actor_rules", "bus_yields_to_pedestrian"),
            ("counterpart_actor_violations", "bus_fails_to_yield"),
            ("scene_rules", "bus_yield_zone_exists"),
        )
        for scope, hook in attacks:
            record = copy.deepcopy(read_jsonl(TEST_LIBRARY)[0])
            record["rule_hooks"] = {
                "scene_rules": [],
                "counterpart_actor_rules": [],
                "counterpart_actor_violations": [],
            }
            record["rule_hooks"][scope] = [hook]
            with self.subTest(scope=scope, hook=hook), self.assertRaisesRegex(
                ValidationError, "must not assign"
            ):
                validate_record(record)

        record = copy.deepcopy(read_jsonl(TEST_LIBRARY)[0])
        record["rule_hooks"]["scene_rules"] = ["bus_stop_yield_zone_exists"]
        validate_record(record)

    def test_dev_test_overlap_has_no_exact_canonical(self):
        report = audit_dev_test_overlap(DEV_LIBRARY, TEST_LIBRARY)
        self.assertEqual(report["schema_version"], "0.2")
        self.assertEqual(report["exact_canonical_overlap_count"], 0)
        self.assertEqual(len(report["nearest_candidates"]), 16)

    def test_duplicate_a07_e03_is_detected(self):
        candidates = find_internal_duplicate_candidates(TEST_LIBRARY, threshold=0.99)
        pairs = {(item["left"], item["right"]) for item in candidates}
        self.assertIn(("BSG_v0_2_A07", "BSG_v0_2_E03"), pairs)

    def test_v02_rule_hooks_are_strict_and_flatten_in_scope_order(self):
        hooks = {
            "counterpart_actor_violations": ["violation_b"],
            "counterpart_actor_rules": ["actor_rule_a"],
            "scene_rules": ["scene_rule_a", "scene_rule_b"],
        }
        self.assertEqual(
            flatten_rule_hooks(hooks),
            (
                ("scene_rules", 0, "scene_rule_a"),
                ("scene_rules", 1, "scene_rule_b"),
                ("counterpart_actor_rules", 0, "actor_rule_a"),
                ("counterpart_actor_violations", 0, "violation_b"),
            ),
        )
        record = read_jsonl(TEST_LIBRARY)[0]
        for malformed in (
            {"scene_rules": [], "counterpart_actor_rules": []},
            dict(hooks, unexpected=[]),
            dict(hooks, scene_rules="not-a-list"),
            dict(hooks, scene_rules=["duplicate", "duplicate"]),
        ):
            candidate = copy.deepcopy(record)
            candidate["rule_hooks"] = malformed
            with self.subTest(malformed=malformed), self.assertRaises(ValidationError):
                validate_record(candidate, 1)

    def test_v02_version_and_provenance_contract_fail_closed(self):
        record = read_jsonl(TEST_LIBRARY)[0]
        mutations = {
            "query_library_version": "BSG_v0_1",
            "active_workflow_status": "draft",
            "tuning_allowed": True,
            "supersedes_query_id": "BSG_v0_1_wrong_precise",
            "supersedes_intent_group_id": "BSG_v0_1_wrong",
            "policy_diagnostic_ref": "PD_wrong",
            "surface_template_family": "wrong_family",
        }
        for field, value in mutations.items():
            candidate = copy.deepcopy(record)
            candidate[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_record(candidate, 1)
        for field in (
            "dataset_split",
            "supersedes_query_id",
            "unsupported_constraints",
            "core_event_signature",
        ):
            candidate = copy.deepcopy(record)
            del candidate[field]
            with self.subTest(missing=field), self.assertRaises(ValidationError):
                validate_record(candidate, 1)
        candidate = copy.deepcopy(record)
        candidate["core_event_signature"]["events"] = ["forged_event"]
        with self.assertRaisesRegex(ValidationError, "core_event_signature drift"):
            validate_record(candidate, 1)

    def test_supersedes_query_id_is_a_surface_variant(self):
        rows = read_jsonl(TEST_LIBRARY)[:3]
        self.assertEqual({row["intent_group_id"] for row in rows}, {"BSG_v0_2_A01"})
        self.assertEqual(len({row["supersedes_query_id"] for row in rows}), 3)
        self.assertEqual(
            {row["supersedes_intent_group_id"] for row in rows}, {"BSG_v0_1_A01"}
        )


class OracleDraftTests(unittest.TestCase):
    def _record(self, path, query_id):
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                if record["query_id"] == query_id:
                    return record
        self.fail("missing query {}".format(query_id))

    def test_open_cardinality_is_preserved(self):
        record = self._record(TEST_LIBRARY, "BSG_v0_2_A03_precise")
        oracle = draft_oracle_record(record)
        actor = next(atom for atom in oracle["atoms"] if atom["category"] == "actor")
        self.assertEqual(actor["arguments"]["count"], "multiple")
        self.assertEqual(actor["layer"], "core_required")

    def test_zero_cardinality_is_forbidden(self):
        record = self._record(DEV_LIBRARY, "BSG_DEV_v0_2_U03_precise")
        # Unsupported records are routed to UQH and intentionally have no SRS atoms.
        oracle = draft_oracle_record(record)
        self.assertEqual(oracle["expected_support"], "unsupported")
        self.assertEqual(oracle["atoms"], [])

    def test_optional_lane_is_permitted(self):
        record = self._record(TEST_LIBRARY, "BSG_v0_2_A01_partial")
        oracle = draft_oracle_record(record)
        optional = [
            atom
            for atom in oracle["atoms"]
            if atom["category"] == "road" and atom["arguments"].get("value") == "optional"
        ]
        self.assertTrue(optional)
        self.assertTrue(all(atom["layer"] == "permitted" for atom in optional))

    def test_count_normalization_rejects_boolean(self):
        with self.assertRaises(Exception):
            normalize_count(True)

    def test_test_only_cpd_dimension_is_marked_ineligible(self):
        with DEV_LIBRARY.open("r", encoding="utf-8") as stream:
            dev = draft_oracle(json.loads(line) for line in stream)
        record = self._record(TEST_LIBRARY, "BSG_v0_2_A01_partial")
        restricted = restrict_cpd_eligibility(
            [draft_oracle_record(record)], cpd_dimension_names(dev)
        )[0]
        self.assertFalse(restricted["cpd_policy"]["eligible"])
        self.assertFalse(restricted["cpd_policy"]["cross_platform_judgeable"])

    def test_all_v02_common_registry_policies_are_statically_routable(self):
        development = draft_oracle(read_jsonl(DEV_LIBRARY))
        test = draft_oracle(read_jsonl(TEST_LIBRARY))
        restrict_cpd_eligibility(test, cpd_dimension_names(development))

        report = validate_cpd_policy_routing(test, expected_eligible_queries=14)

        self.assertEqual(report["by_style"], {"partial": 7, "vague": 7})
        self.assertEqual(
            report["dimension_counts"]["actor_longitudinal_distance_bin"],
            14,
        )
        self.assertEqual(report["supported_platforms"], ["carla", "metadrive"])

    def test_intrinsically_ambiguous_actor_class_queries_are_not_cpd_eligible(self):
        development = draft_oracle(read_jsonl(DEV_LIBRARY))
        test = draft_oracle(read_jsonl(TEST_LIBRARY))
        restrict_cpd_eligibility(test, cpd_dimension_names(development))
        by_id = {record["query_id"]: record for record in test}
        ambiguous_ids = {
            "BSG_v0_2_{}_{}".format(intent, style)
            for intent in ("B05", "B09", "D15", "E06")
            for style in ("partial", "vague")
        }

        self.assertTrue(
            all(
                by_id[query_id]["cpd_policy"]["eligible"] is False
                for query_id in ambiguous_ids
            )
        )
        self.assertTrue(
            all(
                by_id[query_id]["cpd_policy"]["dimensions"] == []
                for query_id in ambiguous_ids
            )
        )

    def test_static_routing_rejects_ambiguous_core_actor_class(self):
        ambiguous = draft_oracle_record(
            self._record(TEST_LIBRARY, "BSG_v0_2_B05_partial")
        )
        unique = draft_oracle_record(
            self._record(TEST_LIBRARY, "BSG_v0_2_D14_partial")
        )
        distance = copy.deepcopy(
            next(
                dimension
                for dimension in unique["cpd_policy"]["dimensions"]
                if dimension["name"] == "actor_longitudinal_distance_bin"
            )
        )
        ambiguous["cpd_policy"].update(
            {
                "eligible": True,
                "dimensions": [distance],
                "decision_status": "confirmed",
                "cross_platform_judgeable": True,
            }
        )

        with self.assertRaisesRegex(
            ValidationError, "minimum actor-class cardinality 1"
        ):
            validate_cpd_policy_routing([ambiguous])

    def test_static_routing_rejects_invalid_yield_and_merge_target_cardinality(self):
        for dimension_name, query_id in (
            ("yield_realization_mode", "BSG_v0_2_A05_partial"),
            ("merge_gap_relation", "BSG_v0_2_A05_partial"),
        ):
            record = draft_oracle_record(self._record(TEST_LIBRARY, query_id))
            record["cpd_policy"].update(
                {
                    "decision_status": "confirmed",
                    "cross_platform_judgeable": True,
                }
            )
            dimension = next(
                item
                for item in record["cpd_policy"]["dimensions"]
                if item["name"] == dimension_name
            )
            dimension["target_selector"]["target_signature"] = {
                "actor_class": "pedestrian"
            }

            with self.subTest(dimension=dimension_name), self.assertRaisesRegex(
                ValidationError, "minimum actor-class cardinality 1"
            ):
                validate_cpd_policy_routing([record])

    def test_v02_yield_targets_the_counterpart_vehicle(self):
        record = draft_oracle_record(
            self._record(TEST_LIBRARY, "BSG_v0_2_A05_partial")
        )
        yield_dimension = next(
            dimension
            for dimension in record["cpd_policy"]["dimensions"]
            if dimension["name"] == "yield_realization_mode"
        )

        self.assertEqual(
            yield_dimension["target_selector"]["target_signature"],
            {"actor_class": "motor_vehicle"},
        )

    def test_rule_hook_scopes_do_not_create_ego_reaction_dimensions(self):
        record = self._record(TEST_LIBRARY, "BSG_v0_2_A01_partial")
        record["rule_hooks"] = {
            "scene_rules": ["bus_stop_yield_zone_exists"],
            "counterpart_actor_rules": ["following_vehicle_yields"],
            "counterpart_actor_violations": ["following_vehicle_non_yield"],
        }
        oracle = draft_oracle_record(record)
        hooks = [
            atom for atom in oracle["atoms"] if atom["predicate"] == "rule_hook"
        ]
        self.assertEqual(
            [atom["arguments"]["scope"] for atom in hooks],
            [
                "scene_rules",
                "counterpart_actor_rules",
                "counterpart_actor_violations",
            ],
        )
        self.assertNotIn(
            "yield_realization_mode",
            {dimension["name"] for dimension in oracle["cpd_policy"]["dimensions"]},
        )
        self.assertTrue(
            all("ego" not in atom["arguments"]["scope"] for atom in hooks)
        )

    def test_non_yield_and_yield_violation_are_not_positive_yield_dimensions(self):
        records = (
            self._record(TEST_LIBRARY, "BSG_v0_2_E04_partial"),
            self._record(DEV_LIBRARY, "BSG_DEV_v0_2_S08_partial"),
        )
        for record in records:
            dimensions = {
                value["name"]
                for value in draft_oracle_record(record)["cpd_policy"]["dimensions"]
            }
            self.assertNotIn("yield_realization_mode", dimensions)

    def test_all_v02_yield_subject_signatures_match_snapshot(self):
        test = draft_oracle(read_jsonl(TEST_LIBRARY))
        signatures = {
            record["query_id"]: next(
                dimension["target_selector"]["target_signature"]["actor_class"]
                for dimension in record["cpd_policy"]["dimensions"]
                if dimension["name"] == "yield_realization_mode"
            )
            for record in test
            if any(
                dimension["name"] == "yield_realization_mode"
                for dimension in record["cpd_policy"]["dimensions"]
            )
        }

        self.assertEqual(
            signatures,
            {
                "BSG_v0_2_A05_partial": "motor_vehicle",
                "BSG_v0_2_A05_vague": "motor_vehicle",
                "BSG_v0_2_A06_partial": "motor_vehicle",
                "BSG_v0_2_A06_vague": "motor_vehicle",
                "BSG_v0_2_A07_partial": "motor_vehicle",
                "BSG_v0_2_A07_vague": "motor_vehicle",
                "BSG_v0_2_E03_partial": "motor_vehicle",
                "BSG_v0_2_E03_vague": "motor_vehicle",
                "BSG_v0_2_E07_partial": "motor_vehicle",
                "BSG_v0_2_E07_vague": "motor_vehicle",
                "BSG_v0_2_E08_partial": "motor_vehicle",
                "BSG_v0_2_E08_vague": "motor_vehicle",
                "BSG_v0_2_F05_partial": "motor_vehicle",
                "BSG_v0_2_F05_vague": "motor_vehicle",
            },
        )
        self.assertEqual(
            sha256_bytes(canonical_json_bytes(signatures)),
            "10515b2a7fcb81340425bb6c235b070d3d4ab1946513f7e39b4f74192ac476d7",
        )

    def test_legacy_v01_oracle_exports_remain_backward_compatible(self):
        development = draft_oracle(read_jsonl(LEGACY_DEV_LIBRARY))
        test = draft_oracle(read_jsonl(LEGACY_TEST_LIBRARY))
        restrict_cpd_eligibility(test, cpd_dimension_names(development))
        development_again = draft_oracle(read_jsonl(LEGACY_DEV_LIBRARY))
        test_again = draft_oracle(read_jsonl(LEGACY_TEST_LIBRARY))
        restrict_cpd_eligibility(test_again, cpd_dimension_names(development_again))

        self.assertEqual(len(development), 48)
        self.assertEqual(len(test), 252)
        self.assertTrue(all("_v0_1_" in row["query_id"] for row in development + test))
        self.assertEqual(
            canonical_json_bytes(development), canonical_json_bytes(development_again)
        )
        self.assertEqual(canonical_json_bytes(test), canonical_json_bytes(test_again))
        self.assertEqual(
            canonical_json_bytes(build_review_tasks(development)),
            canonical_json_bytes(build_review_tasks(development_again)),
        )

    def test_merge_selector_is_only_registered_for_actual_merge_events(self):
        wait = draft_oracle_record(
            self._record(TEST_LIBRARY, "BSG_v0_2_A01_partial")
        )
        merge = draft_oracle_record(
            self._record(TEST_LIBRARY, "BSG_v0_2_A05_partial")
        )
        wait_dimensions = {
            value["name"] for value in wait["cpd_policy"]["dimensions"]
        }
        merge_dimensions = {
            value["name"] for value in merge["cpd_policy"]["dimensions"]
        }
        self.assertNotIn("merge_gap_relation", wait_dimensions)
        self.assertIn("merge_gap_relation", merge_dimensions)

    def test_merge_selector_query_set_matches_frozen_snapshot(self):
        test = draft_oracle(read_jsonl(TEST_LIBRARY))
        query_ids = sorted(
            record["query_id"]
            for record in test
            if any(
                dimension["name"] == "merge_gap_relation"
                for dimension in record["cpd_policy"]["dimensions"]
            )
        )

        self.assertEqual(
            query_ids,
            [
                "BSG_v0_2_A05_partial",
                "BSG_v0_2_A06_partial",
                "BSG_v0_2_A07_partial",
                "BSG_v0_2_A07_vague",
                "BSG_v0_2_B04_partial",
                "BSG_v0_2_B04_vague",
                "BSG_v0_2_C06_partial",
                "BSG_v0_2_C06_vague",
                "BSG_v0_2_C08_partial",
                "BSG_v0_2_C08_vague",
                "BSG_v0_2_D03_partial",
                "BSG_v0_2_D03_vague",
                "BSG_v0_2_D04_partial",
                "BSG_v0_2_D04_vague",
                "BSG_v0_2_D08_partial",
                "BSG_v0_2_D08_vague",
                "BSG_v0_2_D10_partial",
                "BSG_v0_2_D11_vague",
                "BSG_v0_2_D12_partial",
                "BSG_v0_2_D12_vague",
                "BSG_v0_2_D14_partial",
                "BSG_v0_2_D14_vague",
                "BSG_v0_2_D16_partial",
                "BSG_v0_2_D16_vague",
                "BSG_v0_2_E03_partial",
                "BSG_v0_2_E03_vague",
                "BSG_v0_2_E04_partial",
                "BSG_v0_2_E04_vague",
                "BSG_v0_2_F02_partial",
                "BSG_v0_2_F02_vague",
                "BSG_v0_2_F05_partial",
                "BSG_v0_2_F05_vague",
            ],
        )
        self.assertEqual(
            sha256_bytes(canonical_json_bytes(query_ids)),
            "42ebe1e024842c072ffd28b7451c839b47fa5c88f81fafeefcb625f32a7e7488",
        )


if __name__ == "__main__":
    unittest.main()
