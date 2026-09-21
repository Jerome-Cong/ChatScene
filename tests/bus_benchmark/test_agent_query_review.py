import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from bus_benchmark.agent_query_review import (
    build_agent_query_review_draft,
    validate_agent_query_review_draft,
)
from bus_benchmark.errors import ValidationError
from bus_benchmark.jsonio import canonical_json_bytes, read_json, read_jsonl
from bus_benchmark.oracle import REQUIRED_REVIEW_CHECKS
from bus_benchmark.schema import validate_schema_instance


ROOT = Path(__file__).resolve().parents[2]
QUERY_SOURCES = (
    (
        "development",
        ROOT / "query_lib" / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl",
        ROOT / "benchmark_artifacts" / "drafts" / "dev_oracle_draft.jsonl",
    ),
    (
        "test",
        ROOT / "query_lib" / "bus_ego_topdown_2d_query_library_v0_2.jsonl",
        ROOT / "benchmark_artifacts" / "drafts" / "test_oracle_draft.jsonl",
    ),
)
FRAGMENTS = (
    ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "agent_review_fragments"
    / "dev_intents.json",
    ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "agent_review_fragments"
    / "test_A_C_intents.json",
    ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "agent_review_fragments"
    / "test_D_G_intents.json",
)
ARTIFACT = (
    ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "query_agent_semantic_review_v0_2.json"
)
LEGACY_V0_1_ARTIFACT = (
    ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "query_agent_semantic_review_v0_1.json"
)


class AgentQueryReviewMigrationBoundaryTests(unittest.TestCase):
    def test_legacy_v0_1_artifact_cannot_silently_enter_v0_2_workflow(self):
        legacy = read_json(LEGACY_V0_1_ARTIFACT)
        with self.assertRaises(ValidationError):
            validate_agent_query_review_draft(legacy)


class AgentQueryReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checked_in = read_json(ARTIFACT)

    def test_fragments_are_strict_machine_only_and_cover_100_intents(self):
        intent_ids = []
        for path in FRAGMENTS:
            fragment = read_json(path)
            validate_schema_instance(fragment, "agent_semantic_review_fragment")
            self.assertTrue(fragment["agent_generated"])
            self.assertFalse(fragment["human_gold"])
            intent_ids.extend(
                review["intent_group_id"] for review in fragment["intent_reviews"]
            )
        self.assertEqual(len(intent_ids), 100)
        self.assertEqual(len(set(intent_ids)), 100)

    def test_checked_in_artifact_is_exact_reproducible_and_source_bound(self):
        rebuilt = build_agent_query_review_draft(QUERY_SOURCES, FRAGMENTS)
        self.assertEqual(
            canonical_json_bytes(rebuilt), canonical_json_bytes(self.checked_in)
        )
        validate_agent_query_review_draft(
            self.checked_in,
            query_sources=QUERY_SOURCES,
            fragment_paths=FRAGMENTS,
        )
        summary = self.checked_in["summary"]
        expected_atom_decisions = sum(
            len(oracle["atoms"])
            for _split, _library, oracle_path in QUERY_SOURCES
            for oracle in read_jsonl(oracle_path)
        )
        expected_explicit_decisions = expected_atom_decisions + 300 * (
            len(REQUIRED_REVIEW_CHECKS) + 1
        )
        self.assertEqual(summary["intent_groups"], 100)
        self.assertEqual(summary["subjects"], 300)
        self.assertEqual(summary["atom_decisions"], expected_atom_decisions)
        self.assertEqual(
            summary["explicit_decision_entries"], expected_explicit_decisions
        )
        self.assertEqual(summary["admissible_human_gold_records_created"], 0)
        self.assertFalse(self.checked_in["human_gold"])
        self.assertFalse(self.checked_in["human_submission_populated"])

    def test_semantic_review_is_not_blanket_accept(self):
        summary = self.checked_in["summary"]
        self.assertGreater(summary["basis_counts"]["intent_semantic_finding"], 0)
        self.assertGreater(
            summary["recommendation_counts"]["revise_current"]
            + summary["recommendation_counts"]["reject_current"]
            + summary["recommendation_counts"]["human_judgment_required"],
            0,
        )
        self.assertGreater(
            summary["overall_disposition_counts"]["accept_with_attention"]
            + summary["overall_disposition_counts"]["revise"],
            0,
        )

    def test_self_hash_summary_and_atom_binding_tampering_are_rejected(self):
        stale_hash = copy.deepcopy(self.checked_in)
        stale_hash["reviews"][0]["confidence"] = "low"
        with self.assertRaisesRegex(ValidationError, "artifact ID"):
            validate_agent_query_review_draft(stale_hash)

        stale_summary = copy.deepcopy(self.checked_in)
        stale_summary["summary"]["recommendation_counts"]["accept_current"] -= 1
        core = {key: value for key, value in stale_summary.items() if key != "artifact_id"}
        from bus_benchmark.jsonio import sha256_bytes

        stale_summary["artifact_id"] = sha256_bytes(canonical_json_bytes(core))
        with self.assertRaisesRegex(ValidationError, "stale recommendation counts"):
            validate_agent_query_review_draft(stale_summary)

        stale_atom = copy.deepcopy(self.checked_in)
        stale_atom["reviews"][0]["atom_decisions"][0]["predicate"] = "forged"
        core = {key: value for key, value in stale_atom.items() if key != "artifact_id"}
        stale_atom["artifact_id"] = sha256_bytes(canonical_json_bytes(core))
        with self.assertRaisesRegex(ValidationError, "decisions are stale"):
            validate_agent_query_review_draft(
                stale_atom,
                query_sources=QUERY_SOURCES,
                fragment_paths=FRAGMENTS,
            )

    def test_self_consistent_atom_omission_is_rejected_by_bound_oracle_count(self):
        from bus_benchmark.jsonio import sha256_bytes

        omitted = copy.deepcopy(self.checked_in)
        review = omitted["reviews"][0]
        removed = review["atom_decisions"].pop()
        review["explicit_decision_entries"] -= 1
        summary = omitted["summary"]
        summary["atom_decisions"] -= 1
        summary["explicit_decision_entries"] -= 1
        summary["recommendation_counts"][removed["recommendation"]] -= 1
        summary["basis_counts"][removed["basis"]] -= 1
        core = {key: value for key, value in omitted.items() if key != "artifact_id"}
        omitted["artifact_id"] = sha256_bytes(canonical_json_bytes(core))

        # Internal counts and the self-hash are consistent.  Only the bound
        # oracle source proves that one required atom decision is missing.
        validate_agent_query_review_draft(omitted)
        with self.assertRaisesRegex(ValidationError, "bound oracle sources"):
            validate_agent_query_review_draft(
                omitted,
                query_sources=QUERY_SOURCES,
                fragment_paths=FRAGMENTS,
            )

    def test_fragment_path_role_and_order_substitution_are_rejected(self):
        from bus_benchmark.jsonio import sha256_bytes

        def rehash(value):
            core = {key: child for key, child in value.items() if key != "artifact_id"}
            value["artifact_id"] = sha256_bytes(canonical_json_bytes(core))

        wrong_role = copy.deepcopy(self.checked_in)
        wrong_role["source_bindings"][4]["role"] = "test_query_library"
        rehash(wrong_role)
        with self.assertRaises(ValidationError):
            validate_agent_query_review_draft(
                wrong_role,
                query_sources=QUERY_SOURCES,
                fragment_paths=FRAGMENTS,
            )

        wrong_order = copy.deepcopy(self.checked_in)
        wrong_order["source_bindings"][4], wrong_order["source_bindings"][5] = (
            wrong_order["source_bindings"][5],
            wrong_order["source_bindings"][4],
        )
        rehash(wrong_order)
        with self.assertRaises(ValidationError):
            validate_agent_query_review_draft(
                wrong_order,
                query_sources=QUERY_SOURCES,
                fragment_paths=FRAGMENTS,
            )

        with tempfile.TemporaryDirectory() as directory:
            substitute = Path(directory) / "dev_intents.json"
            shutil.copyfile(FRAGMENTS[0], substitute)
            wrong_path = copy.deepcopy(self.checked_in)
            wrong_path["source_bindings"][4]["path"] = str(substitute)
            rehash(wrong_path)
            with self.assertRaises(ValidationError):
                validate_agent_query_review_draft(
                    wrong_path,
                    query_sources=QUERY_SOURCES,
                    fragment_paths=FRAGMENTS,
                )


if __name__ == "__main__":
    unittest.main()
