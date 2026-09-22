import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bus_benchmark.errors import ValidationError
from bus_benchmark.human_workflow import export_query_review_bundle
from bus_benchmark.jsonio import read_json, read_jsonl, canonical_json_bytes
from bus_benchmark.review_draft_store import ReviewDraftStore, publish_proposal
from bus_benchmark.review_forms import response_from_form
from bus_benchmark.review_model import make_proposal, new_draft, edit_draft, confirmation_projection, confirm_scope, compile_confirmed

ROOT = Path(__file__).resolve().parents[2]


class ExplicitConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.reviewer = "synthetic-baseline-reviewer"
        bundle = export_query_review_bundle(read_jsonl(ROOT / "query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl")[:1], read_jsonl(ROOT / "benchmark_artifacts/drafts/dev_oracle_draft.jsonl")[:1], reviewer_id=self.reviewer)
        self.task = bundle["reviewer_packet"]["tasks"][0]
        self.proposal = make_proposal(self.task)
        self.draft = new_draft(self.task, self.proposal, self.reviewer)

    def confirm_all(self, draft):
        view = confirmation_projection(self.task, self.proposal, draft)
        return confirm_scope(self.task, self.proposal, draft, [u["id"] for u in view["units"]], view["content_sha256"], explicit=True)

    def compile(self, draft):
        return compile_confirmed(self.task, self.proposal, draft, reviewer_id=self.reviewer)

    def test_loading_projection_and_saving_never_confirm_or_create_gold(self):
        before = copy.deepcopy(self.draft)
        view = confirmation_projection(self.task, self.proposal, self.draft)
        with tempfile.TemporaryDirectory() as root:
            proposal_path = Path(root) / "proposal.json"
            publish_proposal(proposal_path, self.task, self.proposal)
            store = ReviewDraftStore(Path(root) / "draft.json")
            store.save(self.task, self.proposal, self.draft)
            loaded, _ = store.load(self.task, self.proposal, self.reviewer)
            other = make_proposal(self.task, provenance={"source": "another generator"})
            with self.assertRaisesRegex(ValidationError, "already exists"):
                publish_proposal(proposal_path, self.task, other)
            self.assertEqual(read_json(proposal_path), self.proposal)
        self.assertEqual(before, loaded)
        self.assertFalse(loaded["human_gold"])
        self.assertEqual(loaded["receipts"], [])
        with self.assertRaises(ValidationError):
            self.compile(loaded)
        with self.assertRaises(ValidationError):
            confirm_scope(self.task, self.proposal, loaded, ["support"], view["content_sha256"])

    def test_scoped_receipts_must_cover_every_visible_unit(self):
        view = confirmation_projection(self.task, self.proposal, self.draft)
        ids = [u["id"] for u in view["units"]]
        first = confirm_scope(self.task, self.proposal, self.draft, ids[:2], view["content_sha256"], explicit=True)
        with self.assertRaises(ValidationError):
            self.compile(first)
        complete = confirm_scope(self.task, self.proposal, first, ids[2:], view["content_sha256"], explicit=True)
        response = self.compile(complete)
        self.assertEqual(len(response["required_check_decisions"]), 6)
        self.assertEqual(len(response["atom_decisions"]), len(self.task["oracle_draft"]["atoms"]))
        self.assertFalse(complete["human_gold"])
        self.assertTrue(all(d["reason"].startswith("Human attestation:") for d in response["atom_decisions"]))

    def test_edit_inheritance_and_machine_reapplication_invalidate_confirmation(self):
        complete = self.confirm_all(self.draft)
        for origin in ("human", "inherited", "machine_proposal"):
            changed = edit_draft(self.task, self.proposal, complete, complete["form"], origin)
            self.assertEqual(changed["receipts"], [])
            self.assertEqual(changed["status"], "draft")
            self.assertFalse(changed["human_gold"])
            with self.assertRaises(ValidationError):
                self.compile(changed)

    def test_stale_display_foreign_reviewer_and_source_versions_are_rejected(self):
        view = confirmation_projection(self.task, self.proposal, self.draft)
        form = copy.deepcopy(self.draft["form"])
        form["notes"] = "new edit"
        changed = edit_draft(self.task, self.proposal, self.draft, form)
        with self.assertRaisesRegex(ValidationError, "displayed content"):
            confirm_scope(self.task, self.proposal, changed, ["notes"], view["content_sha256"], explicit=True)
        complete = self.confirm_all(self.draft)
        with self.assertRaisesRegex(ValidationError, "reviewer"):
            compile_confirmed(self.task, self.proposal, complete, reviewer_id="someone-else")
        with patch("bus_benchmark.review_model.GUIDE_VERSION", "changed"), self.assertRaises(ValidationError):
            self.compile(complete)
        forged_task = copy.deepcopy(self.task)
        forged_task["query_text"] += " altered"
        with self.assertRaises(ValidationError):
            compile_confirmed(forged_task, self.proposal, complete, reviewer_id=self.reviewer)

    def test_receipt_tampering_and_unknown_or_deferred_requirements_fail_closed(self):
        complete = self.confirm_all(self.draft)
        for key, value in (("covered_units", ["invented"]), ("reviewer_id", "other"), ("content_sha256", "0" * 64)):
            changed = copy.deepcopy(complete)
            changed["receipts"][0][key] = value
            with self.assertRaises(ValidationError):
                self.compile(changed)
        changed = copy.deepcopy(self.draft)
        changed["issues"] = [{"reason": "cannot determine semantics"}]
        with self.assertRaisesRegex(ValidationError, "unresolved"):
            self.confirm_all(changed)
        form = copy.deepcopy(self.draft["form"])
        replacement = copy.deepcopy(self.task["oracle_draft"]["atoms"][0])
        replacement["arguments"]["unknown"] = 1
        form["atom_decisions"][0].update(verdict="modify", reason="synthetic edit", replacement_atoms_json=json.dumps([replacement]))
        changed = edit_draft(self.task, self.proposal, self.draft, form)
        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            self.confirm_all(changed)

    def test_supported_old_forms_compile_to_identical_formal_payloads(self):
        fixture = read_json(ROOT / "tests/bus_benchmark/fixtures/workbench_compatibility_v1.json")
        for case in fixture["cases"]:
            # Old fixture's arbitrary synthetic merge predicate is intentionally
            # unknown to the presentation registry: do not shortcut-confirm it.
            if not case["valid"] or case["name"] == "merge":
                continue
            with self.subTest(case=case["name"]):
                changed = edit_draft(self.task, self.proposal, self.draft, case["form"])
                confirmed = self.confirm_all(changed)
                self.assertEqual(canonical_json_bytes(self.compile(confirmed)), canonical_json_bytes(response_from_form(self.task, case["form"])))

    def test_known_merge_and_source_target_mapping_match_old_compiler(self):
        form = copy.deepcopy(self.draft["form"])
        target = copy.deepcopy(self.task["oracle_draft"]["atoms"][2])
        target["arguments"]["value"] = "synthetic_merged_road"
        for index in (2, 3):
            form["atom_decisions"][index].update(verdict="merge", reason="Synthetic explicit merge", replacement_atoms_json=json.dumps([target]))
        draft = edit_draft(self.task, self.proposal, self.draft, form)
        compiled = self.compile(self.confirm_all(draft))
        self.assertEqual(compiled["atom_decisions"][2]["replacement_atoms"], compiled["atom_decisions"][3]["replacement_atoms"])
        self.assertEqual(compiled["proposed_oracle"], response_from_form(self.task, form)["proposed_oracle"])

    def test_display_projection_is_a_detached_snapshot(self):
        before = copy.deepcopy(self.draft)
        view = confirmation_projection(self.task, self.proposal, self.draft)
        view["form"]["notes"] = "changed display object"
        view["units"][0]["source"]["fields"][0]["value"] = "changed display field"
        self.assertEqual(self.draft, before)

    def test_store_rejects_stale_foreign_and_corrupt_submitted_state(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReviewDraftStore(Path(root) / "draft.json")
            original = store.save(self.task, self.proposal, self.draft)
            confirmed = self.confirm_all(self.draft)
            store.save(self.task, self.proposal, confirmed, original)
            with self.assertRaisesRegex(ValidationError, "another session"):
                store.save(self.task, self.proposal, self.draft, original)
            with self.assertRaisesRegex(ValidationError, "another reviewer"):
                store.load(self.task, self.proposal, "other")
            self.assertEqual(store.load(self.task, self.proposal, self.reviewer)[0], confirmed)
            confirmed["receipts"] = []
            with self.assertRaises(ValidationError):
                store.save(self.task, self.proposal, confirmed, original)
