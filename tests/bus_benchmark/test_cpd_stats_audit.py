import unittest

from bus_benchmark.audit import select_stratified_audit_sample
from bus_benchmark.cli import build_parser
from bus_benchmark.cpd import (
    aggregate_cpd_results,
    compute_coverage,
    jaccard_distance,
    project_common_atoms,
    query_blind_target_selector,
    score_query_cpd,
)
from bus_benchmark.errors import ValidationError
from bus_benchmark.schema import validate_schema_instance
from bus_benchmark.stats import clustered_bootstrap_mean


class CPDTests(unittest.TestCase):
    def policy(self):
        return {
            "query_id": "q",
            "expected_support": "supported",
            "surface_style": "partial",
            "cpd_policy": {
                "eligible": True,
                "decision_status": "confirmed",
                "cross_platform_judgeable": True,
                "dimensions": [
                    {
                        "name": "actor_longitudinal_distance_bin",
                        "common_semantic": True,
                        "cardinality": "exactly_one",
                        "allowed_values": ["near", "medium", "far"],
                        "target_selector": query_blind_target_selector(
                            "actor_longitudinal_distance_bin", "motor_vehicle"
                        ),
                    }
                ],
            },
        }

    def output(self, repetition, value, preserves=True, srs=1.0):
        return {
            "query_id": "q",
            "run_id": "q-{}".format(repetition),
            "repetition": repetition,
            "common_atoms": [
                {
                    "dimension": "actor_longitudinal_distance_bin",
                    "value": value,
                    "target_signature": {"actor_class": "motor_vehicle"},
                }
            ],
            "srs_common": srs,
            "preservation": {
                "ego_gate_passed": preserves,
                "all_common_core_satisfied": preserves,
                "no_common_forbidden": preserves,
                "evidence_complete": preserves,
            },
        }

    def test_native_id_dimension_is_rejected(self):
        with self.assertRaises(ValidationError):
            project_common_atoms(
                [{"dimension": "map_id", "value": "Town01"}],
                {
                    "actor_longitudinal_distance_bin": {
                        "allowed_values": ["near"],
                        "cardinality": "exactly_one",
                    }
                },
            )

    def test_native_map_id_cannot_hide_as_common_value(self):
        with self.assertRaises(ValidationError):
            project_common_atoms(
                [{"dimension": "actor_longitudinal_distance_bin", "value": "Town01"}],
                {
                    "actor_longitudinal_distance_bin": {
                        "allowed_values": ["near", "medium", "far"],
                        "cardinality": "exactly_one",
                    }
                },
            )

    def test_mutually_exclusive_values_cannot_coexist(self):
        with self.assertRaises(ValidationError):
            project_common_atoms(
                [
                    {"dimension": "actor_longitudinal_distance_bin", "value": "near"},
                    {"dimension": "actor_longitudinal_distance_bin", "value": "far"},
                ],
                {
                    "actor_longitudinal_distance_bin": {
                        "allowed_values": ["near", "medium", "far"],
                        "cardinality": "exactly_one",
                    }
                },
            )

    def test_known_but_unrewarded_dimension_is_projected_out(self):
        projected = project_common_atoms(
            [
                {"dimension": "actor_longitudinal_distance_bin", "value": "near"},
                {"dimension": "yield_realization_mode", "value": "hold"},
            ],
            {
                "actor_longitudinal_distance_bin": {
                    "allowed_values": ["near", "medium", "far"],
                    "cardinality": "exactly_one",
                }
            },
            known_dimensions={
                "actor_longitudinal_distance_bin",
                "yield_realization_mode",
            },
        )
        self.assertEqual(len(projected), 1)

    def test_invalid_eligible_policy_is_not_coverage(self):
        from bus_benchmark.cpd import compute_coverage

        record = {
            "query_id": "q",
            "expected_support": "supported",
            "surface_style": "partial",
            "cpd_policy": {
                "candidate": True,
                "eligible": True,
                "dimensions": [],
                "cross_platform_judgeable": False,
                "decision_status": "confirmed",
            },
        }
        with self.assertRaises(ValidationError):
            compute_coverage([record], expected_per_style=None)

    def test_fixed_ten_pair_denominator_and_failure_penalty(self):
        outputs = [
            self.output(
                repetition,
                ("near", "medium", "far", "near", "medium")[repetition],
                repetition != 4,
                1.0 if repetition != 4 else 0.0,
            )
            for repetition in range(5)
        ]
        result = score_query_cpd(outputs, self.policy())
        self.assertEqual(result["pair_denominator"], 10)
        self.assertEqual(len(result["pair_terms"]), 10)
        self.assertLess(result["cpd_common"], result["common_diversity"])

    def test_missing_output_is_not_removed_from_denominator(self):
        with self.assertRaises(ValidationError):
            score_query_cpd(
                [self.output(0, "near"), self.output(1, "medium")], self.policy()
            )

    def test_duplicate_target_signature_fails_closed_without_aborting_query(self):
        outputs = [self.output(repetition, "near") for repetition in range(5)]
        outputs[0]["common_atoms"].append(
            {
                "dimension": "actor_longitudinal_distance_bin",
                "value": "far",
                "target_signature": {"actor_class": "motor_vehicle"},
            }
        )

        result = score_query_cpd(outputs, self.policy())

        affected = [
            term
            for term in result["pair_terms"]
            if term["left"] == 0 or term["right"] == 0
        ]
        self.assertEqual(len(affected), 4)
        self.assertTrue(all(term["both_core_preserving"] is False for term in affected))
        self.assertTrue(all(term["term"] == 0.0 for term in affected))


class StatisticsAndAuditTests(unittest.TestCase):
    def test_legacy_audit_sample_is_explicitly_sampling_only(self):
        help_text = " ".join(build_parser().format_help().split())
        self.assertIn(
            "diagnostic sampling-only; not a human assignment or gold workflow",
            help_text,
        )

    def test_bootstrap_resamples_intents(self):
        records = [
            {"intent_group_id": "a", "value": 0.0},
            {"intent_group_id": "a", "value": 1.0},
            {"intent_group_id": "b", "value": 1.0},
        ]
        result = clustered_bootstrap_mean(records, "value", iterations=100, seed=7)
        self.assertEqual(result["cluster_count"], 2)
        self.assertEqual(result["record_count"], 3)

    def test_bootstrap_weights_statistical_clusters_equally(self):
        records = []
        for index in range(30):
            records.append(
                {
                    "intent_group_id": "duplicate_member_{}".format(index // 15),
                    "statistical_intent_cluster_id": "duplicate_cluster",
                    "value": 0.0,
                }
            )
        for index in range(15):
            records.append(
                {
                    "intent_group_id": "independent",
                    "statistical_intent_cluster_id": "independent",
                    "value": 1.0,
                }
            )
        result = clustered_bootstrap_mean(records, "value", iterations=100, seed=7)
        self.assertAlmostEqual(result["estimate"], 0.5)
        self.assertEqual(result["cluster_count"], 2)

    def test_ten_percent_of_1260_is_126(self):
        records = []
        for index in range(1260):
            records.append(
                {
                    "run_id": "r{}".format(index),
                    "method_id": "method_a",
                    "query_id": "q{}".format(index // 5),
                    "platform": "carla",
                    "surface_style": ("precise", "partial", "vague")[index % 3],
                    "stage_status": "complete",
                    "srs": (index % 10) / 10,
                }
            )
        sample = select_stratified_audit_sample(records, rate=0.1, seed=0)
        self.assertEqual(sample["target"], 126)
        self.assertEqual(sample["selected"], 126)

    def test_ten_percent_is_enforced_per_method(self):
        records = []
        for method, count in (("a", 11), ("b", 21)):
            for index in range(count):
                records.append(
                    {
                        "run_id": "{}-{}".format(method, index),
                        "query_id": "q{}".format(index),
                        "method_id": method,
                        "platform": "carla",
                        "surface_style": "partial",
                        "stage_status": "complete",
                        "srs": 0.5,
                    }
                )
        sample = select_stratified_audit_sample(records, rate=0.1, seed=0)
        self.assertEqual(sample["method_targets"], {"a": 2, "b": 3})
        self.assertEqual(sample["selected"], 5)


class CPDAggregateTests(unittest.TestCase):
    def _oracle(self, query_id, style):
        candidate = style in ("partial", "vague")
        return {
            "query_id": query_id,
            "intent_group_id": query_id.split("-")[0],
            "expected_support": "supported",
            "surface_style": style,
            "cpd_policy": {
                "candidate": candidate,
                "eligible": candidate,
                "decision_status": "confirmed",
                "cross_platform_judgeable": True if candidate else False,
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
            },
        }

    def _provenance(self):
        value = "a" * 64
        return {
            "roster_sha256": value,
            "oracle_sha256": value,
            "coverage_sha256": value,
            "semantic_evidence_sha256": value,
            "semantic_scores_sha256": value,
            "responses_sha256": None,
            "judge_responses_sha256": None,
            "evaluator_source_sha256": value,
            "freeze_manifest_sha256": None,
            "generation_chain_verified": False,
            "generation_run_manifest_sha256": None,
            "implementation_bundle_sha256": None,
            "created_at_utc": "2026-07-14T00:00:00+00:00",
        }

    def test_partial_vague_joint_coverage_and_precise_stability_are_bound(self):
        oracles = [
            self._oracle("i1-partial", "partial"),
            self._oracle("i1-vague", "vague"),
            self._oracle("i1-precise", "precise"),
        ]
        coverage = compute_coverage(oracles, expected_per_style=None)
        query_results = {
            "i1-partial": {
                **score_query_cpd(
                    [self._output("i1-partial", repetition, "near") for repetition in range(5)],
                    oracles[0],
                ),
                "statistical_intent_cluster_id": "i1",
            },
            "i1-vague": {
                **score_query_cpd(
                    [self._output("i1-vague", repetition, "far") for repetition in range(5)],
                    oracles[1],
                ),
                "statistical_intent_cluster_id": "i1",
            },
        }
        precise = {
            "i1-precise": [
                {
                    "repetition": repetition,
                    "fingerprint": ["near"] if repetition < 4 else ["far"],
                }
                for repetition in range(5)
            ]
        }

        result = aggregate_cpd_results(
            query_results,
            oracles,
            coverage,
            precise,
            method_id="method-a",
            platform="carla",
            provenance=self._provenance(),
        )

        validate_schema_instance(result, "cpd_aggregate")
        self.assertEqual(result["coverage"]["joint"]["candidate"], 2)
        self.assertEqual(result["cpd_common"]["partial"]["eligible_query_count"], 1)
        self.assertEqual(result["cpd_common"]["vague"]["eligible_query_count"], 1)
        self.assertEqual(result["cpd_common"]["joint"]["eligible_query_count"], 2)
        self.assertEqual(result["precise_stability"]["pair_count"], 10)

    def _output(self, query_id, repetition, value):
        return {
            "query_id": query_id,
            "repetition": repetition,
            "common_atoms": [
                {
                    "dimension": "actor_longitudinal_distance_bin",
                    "value": value,
                    "target_signature": {"actor_class": "motor_vehicle"},
                }
            ],
            "srs_common": 1.0,
            "preservation": {
                "ego_gate_passed": True,
                "all_common_core_satisfied": True,
                "no_common_forbidden": True,
                "evidence_complete": True,
            },
        }

    def test_missing_eligible_query_is_rejected(self):
        oracles = [
            self._oracle("i1-partial", "partial"),
            self._oracle("i1-vague", "vague"),
            self._oracle("i1-precise", "precise"),
        ]
        with self.assertRaisesRegex(ValidationError, "eligible set"):
            aggregate_cpd_results(
                {},
                oracles,
                compute_coverage(oracles, expected_per_style=None),
                {
                    "i1-precise": [
                        {"repetition": repetition, "fingerprint": []}
                        for repetition in range(5)
                    ]
                },
                method_id="method-a",
                platform="carla",
                provenance=self._provenance(),
            )

    def test_duplicate_statistical_clusters_receive_one_macro_weight(self):
        oracles = [
            self._oracle("duplicate-a", "partial"),
            self._oracle("duplicate-b", "partial"),
            self._oracle("independent", "partial"),
        ]
        patterns = {
            "duplicate-a": ["near"] * 5,
            "duplicate-b": ["near", "far", "near", "far", "near"],
            "independent": ["near", "far", "near", "far", "near"],
        }
        clusters = {
            "duplicate-a": "duplicate-cluster",
            "duplicate-b": "duplicate-cluster",
            "independent": "independent-cluster",
        }
        query_results = {}
        for oracle in oracles:
            query_id = oracle["query_id"]
            query_results[query_id] = {
                **score_query_cpd(
                    [
                        self._output(query_id, repetition, value)
                        for repetition, value in enumerate(patterns[query_id])
                    ],
                    oracle,
                ),
                "statistical_intent_cluster_id": clusters[query_id],
            }

        result = aggregate_cpd_results(
            query_results,
            oracles,
            compute_coverage(oracles, expected_per_style=None),
            {},
            method_id="method-a",
            platform="carla",
            provenance=self._provenance(),
        )

        self.assertEqual(result["cpd_common"]["partial"]["eligible_query_count"], 3)
        self.assertEqual(result["cpd_common"]["partial"]["statistical_cluster_count"], 2)
        self.assertAlmostEqual(result["cpd_common"]["partial"]["macro_mean"], 0.45)


if __name__ == "__main__":
    unittest.main()
