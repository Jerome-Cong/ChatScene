"""Pre-refactor, source-bound compiler vectors; never actual human gold."""

import unittest
import tempfile
from pathlib import Path

from bus_benchmark.errors import ValidationError
from bus_benchmark.human_workflow import (
    export_query_review_bundle,
    validate_query_review_task_response,
)
from bus_benchmark.jsonio import canonical_json_bytes, read_json, read_jsonl, sha256_bytes, sha256_file
from bus_benchmark.query_review_workbench import QueryReviewSession, _inherited_surface_form, response_from_form


ROOT = Path(__file__).resolve().parents[2]


class CompilerCompatibilityTests(unittest.TestCase):
    def test_fixed_forms_preserve_canonical_payload_and_formal_validity(self):
        fixture = read_json(Path(__file__).parent / "fixtures/workbench_compatibility_v1.json")
        sources = []
        for relative, digest in fixture["source_bindings"].items():
            self.assertEqual(sha256_file(ROOT / relative), digest)
            if relative.endswith(".jsonl"):
                sources.append([r for r in read_jsonl(ROOT / relative) if r["query_id"] == fixture["query_id"]])
        bundle = export_query_review_bundle(*sources, reviewer_id=fixture["reviewer_id"])
        self.assertEqual(sha256_bytes(canonical_json_bytes(bundle)), fixture["bundle_sha256"])
        task = bundle["reviewer_packet"]["tasks"][0]
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                response = response_from_form(task, case["form"])
                self.assertEqual(sha256_bytes(canonical_json_bytes(response)), case["expected_payload_sha256"])
                if case["valid"]:
                    validate_query_review_task_response(task, response, reviewer_id=fixture["reviewer_id"], require_confirmation=True)
                else:
                    with self.assertRaises(ValidationError) as caught:
                        validate_query_review_task_response(task, response, reviewer_id=fixture["reviewer_id"], require_confirmation=True)
                    self.assertEqual(str(caught.exception), case["error"])

    def test_fixed_inheritance_and_stale_write_recovery(self):
        fixture = read_json(Path(__file__).parent / "fixtures/workbench_compatibility_v1.json")
        sources = [
            [row for row in read_jsonl(ROOT / path) if row["query_id"] in fixture["inheritance"]["query_ids"]]
            for path in fixture["source_bindings"] if path.endswith(".jsonl")
        ]
        bundle = export_query_review_bundle(*sources, reviewer_id=fixture["reviewer_id"])
        tasks = {t["query_record"]["surface_style"]: t for t in bundle["reviewer_packet"]["tasks"]}
        case = fixture["inheritance"]
        inherited = _inherited_surface_form(tasks["precise"], case["source_form"], tasks["partial"], case["target_form"])
        self.assertEqual(sha256_bytes(canonical_json_bytes(inherited)), case["expected_form_sha256"])
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            def session():
                return QueryReviewSession(bundle, library_source=sources[0], oracle_source=sources[1], split="development", checkpoint_path=checkpoint)
            first, stale = session(), session()
            case = fixture["conflict"]
            first.save_form(case["query_id"], case["form"])
            with self.assertRaises(ValidationError) as caught:
                stale.save_form(case["query_id"], stale.form(case["query_id"]))
            self.assertEqual(str(caught.exception), case["error"])
            state = read_json(checkpoint)
            self.assertEqual(sha256_bytes(canonical_json_bytes(state)), case["expected_checkpoint_sha256"])
            self.assertFalse(state["human_gold"])
            self.assertFalse(any(e["human_confirmed"] for e in state["entries"]))
            self.assertEqual(session().form(case["query_id"]), case["form"])
