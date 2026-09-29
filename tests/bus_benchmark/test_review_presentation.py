from bus_benchmark.paths import PACKAGE_ROOT
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from bus_benchmark.atoms import make_atom
from bus_benchmark.jsonio import read_json, read_jsonl
from bus_benchmark.metrics import deterministic_atom_verdict
from bus_benchmark.review_presentation import _human_atom_statement, atom_evidence, project_atom, quick_confirmation_issues


ROOT = Path(__file__).resolve().parents[2]


class CompletePresentationTests(unittest.TestCase):
    def test_current_suite_fields_are_registered_and_each_value_is_visible(self):
        texts = {}
        for name in ("bus_ego_topdown_2d_dev_query_library_v0_2.jsonl", "bus_ego_topdown_2d_query_library_v0_2.jsonl"):
            texts.update({r["query_id"]: r["query_text"] for r in read_jsonl(ROOT / "query_lib" / name)})
        for name in ("dev", "test"):
            for row in read_jsonl(ROOT / ("benchmark_artifacts/drafts/" + name + "_oracle_draft.jsonl")):
                for atom in row["atoms"]:
                    with self.subTest(atom=atom["atom_id"]):
                        self.assertEqual(quick_confirmation_issues([atom], texts[row["query_id"]]), [])
                        projection = project_atom(atom, "")
                        self.assertEqual({f["key"] for f in projection["fields"]}, set(atom["arguments"]))
                        before = projection["statement"]
                        for key in atom["arguments"]:
                            changed = copy.deepcopy(atom)
                            changed["arguments"][key] = "changed-parameter-哨兵"
                            self.assertNotEqual(before, _human_atom_statement(changed))

    def test_optional_absent_road_feature_is_not_a_prohibition(self):
        atom = make_atom("road", "lane_configuration", {"feature":"has_crosswalk_zone", "value":False}, layer="permitted")
        atom['notes'] = 'Draft records absence metadata without scoring it as forbidden.'
        before = copy.deepcopy(atom)
        shown = _human_atom_statement(atom)
        self.assertIn('草稿描述：场景中没有人行横道区域。', shown)
        self.assertIn('本条是可选描述。场景是否符合这项描述，都不会因本条扣分。', shown)
        self.assertIn('机器把这项设施记录为不存在', shown)
        self.assertIn(atom['notes'], shown)
        self.assertNotIn('出现或成立', shown)
        self.assertNotIn('；', shown)
        self.assertEqual(atom, before)
        atom['polarity'] = 'absent'
        self.assertIn('不应出现以下情况：“场景中没有人行横道区域”', _human_atom_statement(atom))
        self.assertNotIn('场景中有人行横道区域', _human_atom_statement(atom))

    def test_general_forbidden_absence_does_not_repeat_negation(self):
        atom = make_atom('road', 'lane_configuration', {'feature':'has_crosswalk_zone','value':True}, layer='forbidden', polarity='absent')
        shown = _human_atom_statement(atom)
        self.assertIn('不应出现以下情况：“场景中有人行横道区域”', shown)
        self.assertIn('场景必须遵守上面的“不应出现”要求', shown)
        self.assertNotIn('不得出现上面指出的情况', shown)

    def test_conflicting_prohibition_is_not_silently_inverted(self):
        atom = make_atom('road', 'lane_configuration', {'feature':'has_crosswalk_zone','value':True}, layer='forbidden', polarity='present')
        shown = _human_atom_statement(atom)
        self.assertIn('场景中有人行横道区域', shown)
        self.assertIn('两种设置有冲突', shown)
        self.assertFalse(project_atom(atom, '')['quick_confirm_allowed'])

    def test_unknown_and_nested_fields_survive_and_disable_shortcuts(self):
        atom = make_atom("event", "event_spec", {"event": "crossing", "future_field": {"distance": 2, "unknown_unit": "custom"}})
        atom["future_top_level"] = "retain-this"
        projection = project_atom(atom, "query")
        self.assertFalse(projection["quick_confirm_allowed"])
        self.assertTrue(quick_confirmation_issues([atom]))
        for text in ("future_field", "unknown_unit", "retain-this", "custom", "2"):
            self.assertIn(text, projection["statement"])

    def test_notebook_whole_question_prefill_rejects_unknown_fields(self):
        from bus_benchmark.query_review_workbench import QueryReviewWorkbench
        atom = make_atom("event", "event_spec", {"event": "crossing", "future_field": 1})
        session = Mock()
        session.task.return_value = {"oracle_draft": {"atoms": [atom]}, "query_text": "query"}
        ui = SimpleNamespace(current_query_id="synthetic", session=session, _notify=Mock())
        QueryReviewWorkbench._load_mechanical(ui, None)
        session.save_form.assert_not_called()
        ui._notify.assert_called_once()

    def test_precise_text_evidence_does_not_promote_metadata_or_invalid_span(self):
        atom = make_atom("event", "event_spec", {"event": "crossing"}, provenance={"source": "query_text_regex", "span": [2, 5]})
        self.assertEqual(atom_evidence(atom, "a crossing")["text"], "cro")
        atom["provenance"]["span"] = [-1, 500]
        self.assertEqual(atom_evidence(atom, "a crossing")["kind"], "unresolved")
        self.assertFalse(project_atom(atom, "a crossing")["quick_confirm_allowed"])
        self.assertTrue(quick_confirmation_issues([atom], "a crossing"))
        atom["provenance"] = {"source": "metadata", "field": "interaction_events"}
        self.assertEqual(atom_evidence(atom, "a crossing")["kind"], "metadata")
        self.assertIn("草稿未明确", _human_atom_statement(atom))
        self.assertNotIn("actor", atom["arguments"])

    def test_forbidden_absence_matches_existing_scoring_direction(self):
        atom = make_atom("actor", "actor_role_count", {"count": "0", "role": "nearby_cyclist", "type": "bicycle"}, layer="forbidden", polarity="absent")
        evidence = {"atoms": [], "complete_categories": ["actor"]}
        self.assertEqual(deterministic_atom_verdict(atom, evidence), "satisfied")
        shown = _human_atom_statement(atom)
        self.assertIn("不应出现", shown)
        self.assertIn("场景必须遵守上面的“不应出现”要求", shown)
        self.assertNotIn("不得出现上面指出的情况", shown)
        self.assertNotIn("禁止满足", shown)
        self.assertFalse(quick_confirmation_issues([atom], "No cyclist."))
        present = make_atom("actor", "actor_role_count", {"count": "1", "role": "nearby_cyclist", "type": "bicycle"})
        self.assertEqual(deterministic_atom_verdict(atom, {"atoms": [present], "complete_categories": ["actor"]}), "violated")
        atom["polarity"] = "present"
        self.assertTrue(quick_confirmation_issues([atom], "No cyclist."))

    def test_null_parameters_and_empty_temporal_relations_are_not_confirmable(self):
        for predicate, args in (("event_spec", None), ("before", {"first": None, "second": "later"}), ("parallel_group", {"events": []})):
            atom = {"predicate": predicate, "arguments": args, "category": "event" if predicate == "event_spec" else "temporal", "layer": "core_required", "polarity": "present"}
            projection = project_atom(atom, "")
            self.assertFalse(projection["quick_confirm_allowed"])
            self.assertIn("⚠", projection["statement"])

    def test_practice_cases_cover_positive_negative_and_exclusion(self):
        packet = read_json(PACKAGE_ROOT / "assets/review/practice.json")
        self.assertFalse(packet["human_gold"])
        for case in packet["cases"]:
            with self.subTest(case=case["id"]):
                shown = _human_atom_statement(case["atom"])
                for fragment in case["expected_display"]:
                    self.assertIn(fragment, shown)
        self.assertEqual({c["answer"] for c in packet["cases"]}, {"required", "permitted", "forbidden", "exclude", "defer"})
