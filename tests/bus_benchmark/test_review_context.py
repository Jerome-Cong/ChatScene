from bus_benchmark.paths import PACKAGE_ROOT
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bus_benchmark.errors import ValidationError
from bus_benchmark.jsonio import read_jsonl, write_jsonl
from bus_benchmark.review_context import ReviewContext
from bus_benchmark.review_forms import response_from_form


ROOT = Path(__file__).resolve().parents[2]


class ExplicitContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.library = self.workspace / "library.jsonl"
        self.oracle = self.workspace / "oracle.jsonl"
        write_jsonl(self.library, read_jsonl(ROOT / "query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl")[:1])
        write_jsonl(self.oracle, read_jsonl(ROOT / "benchmark_artifacts/drafts/dev_oracle_draft.jsonl")[:1])

    def context(self, **overrides):
        args = dict(library_source=self.library, oracle_source=self.oracle, workspace=self.workspace, reviewer_id="synthetic-test-reviewer", split="custom-development", expected_subjects=1)
        args.update(overrides)
        return ReviewContext(**args)

    def test_explicit_context_from_foreign_cwd_without_widget_imports(self):
        code = """
import importlib.abc,sys
class NoWidgets(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in ('ipywidgets','IPython'):
   raise AssertionError('core attempted a notebook import')
sys.meta_path.insert(0, NoWidgets())
from bus_benchmark.review_context import ReviewContext
s=ReviewContext(sys.argv[1],sys.argv[2],sys.argv[3],'synthetic-test-reviewer','custom-development',1).open_session()
assert len(s.ordered_query_ids)==1
assert 'bus_benchmark.query_review_workbench' not in sys.modules
assert 'bus_benchmark.review_field_widgets' not in sys.modules
assert not s._state['human_gold']
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.library), str(self.oracle), str(self.workspace)], cwd=self.workspace, env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT.parent)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_contexts_fail_before_creating_state(self):
        cases = (
            {"workspace": self.workspace / "missing"},
            {"workspace": PACKAGE_ROOT},
            {"library_source": self.workspace / "absent"},
            {"oracle_source": None},
            {"expected_subjects": 48},
            {"expected_subjects": True},
            {"reviewer_id": ""},
            {"split": ""},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValidationError):
                self.context(**case).open_session()
        self.assertFalse((self.workspace / "review_checkpoint.json").exists())
        with self.assertRaises(ValidationError):
            self.context().open_session("../escape.json")

    def test_notebook_reexports_the_same_compiler(self):
        from bus_benchmark.query_review_workbench import response_from_form as old
        self.assertIs(old, response_from_form)

    def test_legacy_entry_rejects_installation_state_before_export(self):
        from bus_benchmark.review_legacy import ensure_review_assignment
        with patch("bus_benchmark.review_legacy.subprocess.run") as run:
            with self.assertRaisesRegex(ValidationError, "outside package"):
                ensure_review_assignment("synthetic-test-reviewer", PACKAGE_ROOT / "unsafe-review")
            run.assert_not_called()
        self.assertFalse((PACKAGE_ROOT / "unsafe-review").exists())

    def test_manifest_count_and_sources_are_checked_on_resume(self):
        first = self.context().open_session()
        qid = first.ordered_query_ids[0]
        form = first.form(qid)
        form["notes"] = "retained draft"
        first.save_form(qid, form)
        self.assertEqual(self.context().open_session().form(qid)["notes"], "retained draft")
        records = read_jsonl(self.library)
        records[0]["query_text"] += " changed source"
        write_jsonl(self.library, records)
        with self.assertRaises(ValidationError):
            self.context().open_session()
