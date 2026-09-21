import copy
import gc
import json
import unittest
import warnings
from pathlib import Path
from unittest import mock

import ipywidgets as widgets

from bus_benchmark.atoms import make_atom
from bus_benchmark.errors import ValidationError
from bus_benchmark.review_field_widgets import AtomListEditor, CPDPolicyEditor


ROOT = Path(__file__).resolve().parents[2]


def _eligible_checked_in_record():
    path = ROOT / "benchmark_artifacts" / "drafts" / "dev_oracle_draft.jsonl"
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record["cpd_policy"]["eligible"]:
                return record
    raise AssertionError("checked-in development oracle has no eligible CPD policy")


def _ineligible_checked_in_policy_with_dimensions():
    path = ROOT / "benchmark_artifacts" / "drafts" / "test_oracle_draft.jsonl"
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            policy = json.loads(line)["cpd_policy"]
            if not policy["eligible"] and policy["dimensions"]:
                return policy
    raise AssertionError("checked-in test oracle has no ineligible CPD policy with dimensions")


def _semantic_atom(atom):
    return {
        "category": atom["category"],
        "predicate": atom["predicate"],
        "arguments": atom["arguments"],
        "layer": atom["layer"],
        "polarity": atom["polarity"],
        "notes": atom["notes"],
    }


def _visible_widget_descriptions(root):
    descriptions = []
    pending = [root]
    seen = set()
    while pending:
        widget = pending.pop()
        if id(widget) in seen:
            continue
        seen.add(id(widget))
        description = getattr(widget, "description", None)
        if description:
            descriptions.append(description)
        pending.extend(tuple(getattr(widget, "children", ())))
    return descriptions


def _widget_registry_size():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return len(widgets.Widget.widgets)


class AtomListEditorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = _eligible_checked_in_record()

    def test_checked_in_atoms_round_trip_without_identity_or_provenance(self):
        source = self.record["atoms"]
        editor = AtomListEditor(widgets, source)
        actual = editor.value()
        self.assertEqual(actual, [_semantic_atom(atom) for atom in source])
        for atom in actual:
            self.assertNotIn("atom_id", atom)
            self.assertNotIn("provenance", atom)
            make_atom(**atom)

    def test_clone_split_merge_add_and_remove_are_explicit_multi_atom_operations(self):
        changes = []
        atom = _semantic_atom(self.record["atoms"][0])
        editor = AtomListEditor(widgets, [atom], on_change=lambda: changes.append("changed"))

        editor.clone_current()
        self.assertEqual(editor.value(), [atom, atom])

        editor.current.value = 0
        editor.split_current(parts=3)
        self.assertEqual(len(editor.value()), 4)

        editor.merge_selected([0, 1])
        self.assertEqual(len(editor.value()), 3)

        editor.add_atom(atom)
        self.assertEqual(len(editor.value()), 4)
        editor.remove_current()
        self.assertEqual(len(editor.value()), 3)
        self.assertTrue(changes)

    def test_structural_merge_clears_stale_batch_selection(self):
        atom = _semantic_atom(self.record["atoms"][0])
        editor = AtomListEditor(widgets, [atom, atom, atom])
        editor.selected.value = (0, 1)
        editor.merge_selected()
        self.assertEqual(editor.selected.value, ())
        self.assertEqual(len(editor.value()), 2)
        with self.assertRaisesRegex(ValidationError, "at least two distinct"):
            editor.merge_selected()
        self.assertEqual(len(editor.value()), 2)

    def test_boolean_indices_fail_closed_without_mutation(self):
        atom = _semantic_atom(self.record["atoms"][0])
        editor = AtomListEditor(widgets, [atom, atom])
        before = editor.value()
        with self.assertRaisesRegex(ValidationError, "index out of range"):
            editor.merge_selected([0, True])
        self.assertEqual(editor.value(), before)

        atom_editor = editor.atom_editors[0]
        with self.assertRaises(IndexError):
            atom_editor.remove_argument(True)
        self.assertEqual(editor.value(), before)

    def test_argument_types_round_trip(self):
        atom = {
            "category": "event",
            "predicate": "typed_arguments",
            "arguments": {
                "text": "bus",
                "count": 2,
                "distance": 2.5,
                "enabled": True,
                "roles": ["bus", "pedestrian"],
                "metadata": {"source": "human"},
            },
            "layer": "surface_required",
            "polarity": "present",
            "notes": "typed values",
        }
        editor = AtomListEditor(widgets, [atom])
        self.assertEqual(editor.value(), [atom])

    def test_argument_delete_requires_one_shot_confirmation(self):
        changes = []
        atom = {
            "category": "event",
            "predicate": "three_arguments",
            "arguments": {"first": "a", "second": "b", "third": "c"},
            "layer": "core_required",
            "polarity": "present",
            "notes": "",
        }
        editor = AtomListEditor(widgets, [atom], on_change=lambda: changes.append(True))
        atom_editor = editor.atom_editors[0]
        remove_button = atom_editor.arguments_box.children[1].children[1]
        self.assertTrue(remove_button.disabled)

        # A disabled/stale programmatic click must be a semantic no-op too.
        remove_button.click()
        self.assertEqual(len(atom_editor._argument_rows), 3)
        self.assertEqual(changes, [])

        atom_editor.confirm_argument_delete.value = True
        self.assertEqual(changes, [])
        # The old button was detached by the refresh and cannot consume the
        # newly armed confirmation or delete a row.
        remove_button.click()
        self.assertEqual(len(atom_editor._argument_rows), 3)
        self.assertTrue(atom_editor.confirm_argument_delete.value)
        remove_button = atom_editor.arguments_box.children[1].children[1]
        self.assertFalse(remove_button.disabled)
        remove_button.click()
        self.assertEqual(len(atom_editor._argument_rows), 2)
        self.assertEqual(set(editor.value()[0]["arguments"]), {"first", "third"})
        self.assertFalse(atom_editor.confirm_argument_delete.value)
        self.assertTrue(atom_editor.arguments_box.children[0].children[1].disabled)
        self.assertEqual(changes, [True])

    def test_atom_delete_buttons_are_guarded_and_invalid_actions_do_not_escape(self):
        changes = []
        atom = _semantic_atom(self.record["atoms"][0])
        editor = AtomListEditor(widgets, [atom], on_change=lambda: changes.append(True))
        self.assertTrue(editor.remove_button.disabled)

        editor.remove_button.click()
        self.assertEqual(editor.value(), [atom])
        self.assertEqual(changes, [])

        editor.confirm_delete.value = True
        self.assertFalse(editor.remove_button.disabled)
        self.assertEqual(changes, [])
        editor.remove_button.click()
        self.assertEqual(editor.value(), [])
        self.assertEqual(changes, [True])
        self.assertFalse(editor.confirm_delete.value)
        self.assertTrue(editor.clone_button.disabled)
        self.assertTrue(editor.split_button.disabled)
        self.assertTrue(editor.remove_button.disabled)

        editor.new_button.click()
        before = len(editor.atom_editors)
        # The blank atom is intentionally invalid; the dispatcher must retain
        # control and surface the error in the component instead of raising.
        editor.clone_button.click()
        editor.split_button.click()
        self.assertEqual(len(editor.atom_editors), before)
        self.assertIn("atom predicate", editor.action_status.value)

    def test_atom_selection_change_clears_all_destructive_confirmations(self):
        atom = _semantic_atom(self.record["atoms"][0])
        editor = AtomListEditor(widgets, [atom, atom])
        editor.confirm_delete.value = True
        editor.atom_editors[0].confirm_argument_delete.value = True
        editor.current.value = 1
        self.assertFalse(editor.confirm_delete.value)
        self.assertFalse(editor.atom_editors[0].confirm_argument_delete.value)
        self.assertTrue(editor.remove_button.disabled)

    def test_merge_is_not_a_visible_atom_editor_action(self):
        atom = _semantic_atom(self.record["atoms"][0])
        editor = AtomListEditor(widgets, [atom, atom])
        self.assertNotIn("合并", _visible_widget_descriptions(editor.widget))
        self.assertFalse(hasattr(editor, "merge_button"))
        # The helper remains available only for structural/unit-test use; the
        # production workbench owns cross-source provenance-aware merging.
        editor.merge_selected([0, 1])
        self.assertEqual(len(editor.value()), 1)

    def test_invalid_direct_atom_container_types_fail_without_mutation(self):
        changes = []
        atom = _semantic_atom(self.record["atoms"][0])
        editor = AtomListEditor(widgets, [atom], on_change=lambda: changes.append(True))
        for invalid in (False, []):
            with self.assertRaisesRegex(ValidationError, "atom must be an object"):
                editor.add_atom(invalid)
        self.assertEqual(editor.value(), [atom])
        self.assertEqual(changes, [])

    def test_invalid_argument_key_and_value_fail_closed(self):
        atom = _semantic_atom(self.record["atoms"][0])
        editor = AtomListEditor(widgets, [atom])
        atom_editor = editor.atom_editors[0]
        first = atom_editor._argument_rows[0]

        original_key = first.key.value
        first.key.value = " "
        with self.assertRaisesRegex(ValidationError, "argument keys"):
            editor.value()

        first.key.value = original_key
        atom_editor.add_argument(original_key, "duplicate")
        with self.assertRaisesRegex(ValidationError, "duplicate atom argument"):
            editor.value()

        atom_editor.remove_argument(len(atom_editor._argument_rows) - 1)
        first.value_type.value = "integer"
        first.raw_value.value = "not-an-int"
        with self.assertRaisesRegex(ValidationError, "not an integer"):
            editor.value()

        first.value_type.value = "json"
        first.raw_value.value = "NaN"
        with self.assertRaisesRegex(ValidationError, "not valid JSON"):
            editor.value()

        first.raw_value.value = '{"duplicate": 1, "duplicate": 2}'
        with self.assertRaisesRegex(ValidationError, "not valid JSON"):
            editor.value()

        first.raw_value.value = "1e999"
        with self.assertRaisesRegex(ValidationError, "not valid JSON"):
            editor.value()

    def test_nested_atom_json_object_non_string_keys_are_rejected(self):
        atom = _semantic_atom(self.record["atoms"][0])
        atom["arguments"] = {"metadata": {1: "one"}}
        with self.assertRaisesRegex(ValidationError, "JSON object keys must be strings"):
            AtomListEditor(widgets, [atom])


class CPDPolicyEditorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = _eligible_checked_in_record()["cpd_policy"]

    def test_checked_in_policy_round_trips(self):
        callbacks = []
        editor = CPDPolicyEditor(widgets, self.policy, on_change=lambda: callbacks.append(True))
        self.assertEqual(editor.value(), self.policy)
        self.assertEqual(callbacks, [])

        editor.set_value(self.policy, notify=False)
        self.assertEqual(callbacks, [])

        policy = _ineligible_checked_in_policy_with_dimensions()
        self.assertEqual(CPDPolicyEditor(widgets, policy).value(), policy)

    def test_clone_add_and_remove_dimensions(self):
        changes = []
        editor = CPDPolicyEditor(
            widgets, self.policy, on_change=lambda: changes.append("changed")
        )
        original_count = len(self.policy["dimensions"])
        editor.clone_current()
        self.assertEqual(len(editor.dimension_editors), original_count + 1)
        # A clone is deliberately editable but duplicate names fail closed.
        with self.assertRaisesRegex(ValidationError, "names must be unique"):
            editor.value()
        editor.remove_current()
        self.assertEqual(editor.value(), self.policy)

        new_index = editor.add_dimension(copy.deepcopy(self.policy["dimensions"][0]))
        editor.dimension_editors[new_index].name.value = "review_only_dimension"
        editor.dimension_editors[new_index].selector_enabled.value = False
        actual = editor.value()
        self.assertEqual(len(actual["dimensions"]), original_count + 1)
        editor.remove_current()
        self.assertEqual(editor.value(), self.policy)
        self.assertTrue(changes)

    def test_dimension_delete_requires_one_shot_confirmation(self):
        changes = []
        editor = CPDPolicyEditor(
            widgets, self.policy, on_change=lambda: changes.append(True)
        )
        original_count = len(editor.dimension_editors)
        self.assertTrue(editor.remove_button.disabled)
        editor.remove_button.click()
        self.assertEqual(len(editor.dimension_editors), original_count)
        self.assertEqual(changes, [])

        editor.confirm_delete.value = True
        self.assertFalse(editor.remove_button.disabled)
        self.assertEqual(changes, [])
        editor.remove_button.click()
        self.assertEqual(len(editor.dimension_editors), original_count - 1)
        self.assertFalse(editor.confirm_delete.value)
        self.assertTrue(editor.remove_button.disabled)
        self.assertEqual(changes, [True])

        empty_policy = {
            "candidate": False,
            "eligible": False,
            "dimensions": [],
            "decision_status": "draft",
            "cross_platform_judgeable": None,
        }
        empty = CPDPolicyEditor(widgets, empty_policy)
        self.assertTrue(empty.clone_button.disabled)
        self.assertTrue(empty.remove_button.disabled)
        empty.clone_button.click()
        empty.remove_button.click()
        self.assertEqual(empty.value(), empty_policy)

    def test_allowed_value_and_bin_delete_share_one_shot_confirmation(self):
        changes = []
        policy = copy.deepcopy(self.policy)
        dimension = next(
            item
            for item in policy["dimensions"]
            if item["name"] == "actor_longitudinal_distance_bin"
        )
        editor = CPDPolicyEditor(
            widgets,
            {
                "candidate": True,
                "eligible": True,
                "dimensions": [dimension],
                "decision_status": "draft",
                "cross_platform_judgeable": None,
            },
            on_change=lambda: changes.append(True),
        )
        fields = editor.dimension_editors[0]
        allowed_count = len(fields.allowed_rows)
        bin_count = len(fields.bin_rows)

        allowed_remove = fields.allowed_values_box.children[0].children[1]
        self.assertTrue(allowed_remove.disabled)
        allowed_remove.click()
        self.assertEqual(len(fields.allowed_rows), allowed_count)
        self.assertEqual(changes, [])

        fields.confirm_field_delete.value = True
        self.assertEqual(changes, [])
        allowed_remove.click()
        self.assertEqual(len(fields.allowed_rows), allowed_count)
        self.assertTrue(fields.confirm_field_delete.value)
        allowed_remove = fields.allowed_values_box.children[0].children[1]
        self.assertFalse(allowed_remove.disabled)
        allowed_remove.click()
        self.assertEqual(len(fields.allowed_rows), allowed_count - 1)
        self.assertEqual(len(fields.bin_rows), bin_count)
        self.assertFalse(fields.confirm_field_delete.value)
        self.assertEqual(changes, [True])

        # The first confirmation is consumed; deleting a bin needs a new one.
        bin_remove = fields.bin_rows_box.children[0].children[1]
        self.assertTrue(bin_remove.disabled)
        bin_remove.click()
        self.assertEqual(len(fields.bin_rows), bin_count)
        fields.confirm_field_delete.value = True
        bin_remove = fields.bin_rows_box.children[0].children[1]
        bin_remove.click()
        self.assertEqual(len(fields.bin_rows), bin_count - 1)
        self.assertFalse(fields.confirm_field_delete.value)
        self.assertEqual(changes, [True, True])

    def test_dimension_selection_change_clears_all_destructive_confirmations(self):
        editor = CPDPolicyEditor(widgets, self.policy)
        editor.clone_current()
        editor.confirm_delete.value = True
        editor.dimension_editors[-1].confirm_field_delete.value = True
        editor.current.value = 0
        self.assertFalse(editor.confirm_delete.value)
        self.assertTrue(editor.remove_button.disabled)
        self.assertTrue(
            all(
                not dimension.confirm_field_delete.value
                for dimension in editor.dimension_editors
            )
        )

    def test_invalid_direct_dimension_container_types_fail_without_mutation(self):
        changes = []
        editor = CPDPolicyEditor(
            widgets, self.policy, on_change=lambda: changes.append(True)
        )
        before = copy.deepcopy(editor.value())
        for invalid in (False, []):
            with self.assertRaisesRegex(ValidationError, "CPD dimension must be an object"):
                editor.add_dimension(invalid)
        self.assertEqual(editor.value(), before)
        self.assertEqual(changes, [])

    def test_invalid_dimension_values_and_selector_fail_closed(self):
        editor = CPDPolicyEditor(widgets, self.policy)
        dimension = editor.dimension_editors[0]

        dimension.allowed_rows[1].raw_value.value = dimension.allowed_rows[0].raw_value.value
        with self.assertRaisesRegex(ValidationError, "must be unique"):
            editor.value()

        dimension.allowed_rows[1].raw_value.value = "medium"
        dimension.name.value = "unknown_actor_dimension"
        with self.assertRaisesRegex(ValidationError, "frozen query-blind selector"):
            editor.value()

    def test_cpd_common_fields_are_structured_and_selector_is_derived(self):
        policy = copy.deepcopy(self.policy)
        dimension = next(
            item
            for item in policy["dimensions"]
            if item["name"] == "actor_longitudinal_distance_bin"
        )
        editor = CPDPolicyEditor(
            widgets,
            {
                "candidate": True,
                "eligible": True,
                "dimensions": [dimension],
                "decision_status": "draft",
                "cross_platform_judgeable": None,
            },
        )
        fields = editor.dimension_editors[0]
        self.assertTrue(fields.allowed_rows)
        self.assertTrue(fields.bin_rows)
        fields.allowed_rows[0].raw_value.value = "close"
        fields.bin_rows[0].boundary.value = 12.0
        fields.target_actor_class.value = "pedestrian"
        actual = editor.value()["dimensions"][0]
        self.assertEqual(actual["allowed_values"][0], "close")
        self.assertEqual(actual["bin_definition_m"][fields.bin_rows[0].key.value], 12.0)
        self.assertEqual(
            actual["target_selector"]["target_signature"], {"actor_class": "pedestrian"}
        )
        self.assertNotIn("target_selector", fields.__dict__)

    def test_json_schema_numeric_equality_rejects_one_and_one_point_zero(self):
        policy = {
            "candidate": True,
            "eligible": True,
            "dimensions": [
                {
                    "name": "review_numeric_dimension",
                    "common_semantic": True,
                    "cardinality": "exactly_one",
                    "allowed_values": [1, 1.0],
                    "reason": "exercise JSON Schema uniqueItems semantics",
                }
            ],
            "decision_status": "draft",
            "cross_platform_judgeable": None,
        }
        with self.assertRaisesRegex(ValidationError, "must be unique"):
            CPDPolicyEditor(widgets, policy)

    def test_nested_cpd_json_object_non_string_keys_are_rejected(self):
        policy = copy.deepcopy(self.policy)
        policy["dimensions"][0]["allowed_values"] = [{1: "one"}]
        with self.assertRaisesRegex(ValidationError, "JSON object keys must be strings"):
            CPDPolicyEditor(widgets, policy)

    def test_schema_valid_large_integer_allowed_value_does_not_overflow(self):
        large = 10 ** 1000
        policy = {
            "candidate": True,
            "eligible": True,
            "dimensions": [
                {
                    "name": "review_large_integer_dimension",
                    "common_semantic": True,
                    "cardinality": "exactly_one",
                    "allowed_values": [large],
                }
            ],
            "decision_status": "draft",
            "cross_platform_judgeable": None,
        }
        self.assertEqual(CPDPolicyEditor(widgets, policy).value(), policy)

    def test_failed_set_value_is_transactional_and_silent(self):
        callbacks = []
        editor = CPDPolicyEditor(
            widgets, self.policy, on_change=lambda: callbacks.append("changed")
        )
        before = copy.deepcopy(editor.value())
        before_current = editor.current.value
        invalid = copy.deepcopy(self.policy)
        invalid["candidate"] = False
        invalid["eligible"] = False
        invalid["cross_platform_judgeable"] = False
        selector_dimension = next(
            dimension
            for dimension in invalid["dimensions"]
            if "target_selector" in dimension
        )
        selector_dimension["target_selector"]["target_signature"]["actor_class"] = "invalid"

        with self.assertRaises(ValidationError):
            editor.set_value(invalid, notify=False)
        self.assertEqual(editor.value(), before)
        self.assertEqual(editor.current.value, before_current)
        self.assertEqual(callbacks, [])

    def test_explicit_empty_policy_is_not_silently_replaced_with_defaults(self):
        with self.assertRaisesRegex(ValidationError, "missing required fields"):
            CPDPolicyEditor(widgets, {})

    def test_policy_consistency_fails_closed(self):
        editor = CPDPolicyEditor(widgets, self.policy)
        editor.candidate.value = False
        with self.assertRaisesRegex(ValidationError, "must be a candidate"):
            editor.value()

        editor.candidate.value = True
        editor.dimension_editors = []
        editor._refresh_navigation(0)
        with self.assertRaisesRegex(ValidationError, "requires at least one dimension"):
            editor.value()


class WidgetLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = _eligible_checked_in_record()

    def test_repeated_refresh_and_close_keep_widget_registry_bounded(self):
        gc.collect()
        baseline = _widget_registry_size()
        atom = _semantic_atom(self.record["atoms"][0])
        policy = self.record["cpd_policy"]
        atom_editor = AtomListEditor(widgets, [atom])
        cpd_editor = CPDPolicyEditor(widgets, policy)
        steady_sizes = []
        try:
            for _index in range(12):
                atom_editor.set_value([atom], notify=False)
                cpd_editor.set_value(policy, notify=False)
                steady_sizes.append(_widget_registry_size())
            self.assertEqual(min(steady_sizes), max(steady_sizes))
        finally:
            atom_editor.close()
            cpd_editor.close()
            # Component cleanup is deliberately idempotent because notebook
            # navigation and kernel teardown may both call it.
            atom_editor.close()
            cpd_editor.close()
        gc.collect()
        self.assertLessEqual(_widget_registry_size(), baseline)

    def test_failed_child_display_conversion_does_not_leak_partial_widgets(self):
        gc.collect()
        baseline = _widget_registry_size()
        atom = _semantic_atom(self.record["atoms"][0])
        atom["arguments"] = {"too_large_to_display": 1}
        policy = copy.deepcopy(self.record["cpd_policy"])
        policy["dimensions"][0]["allowed_values"] = [1]

        with mock.patch(
            "bus_benchmark.review_field_widgets._argument_text",
            side_effect=ValueError("display conversion failed"),
        ):
            with self.assertRaisesRegex(ValueError, "display conversion failed"):
                AtomListEditor(widgets, [atom])
            gc.collect()
            self.assertEqual(_widget_registry_size(), baseline)

            with self.assertRaisesRegex(ValueError, "display conversion failed"):
                CPDPolicyEditor(widgets, policy)
            gc.collect()
            self.assertEqual(_widget_registry_size(), baseline)

    def test_failed_direct_row_add_does_not_leak_partial_widgets(self):
        gc.collect()
        baseline = _widget_registry_size()
        atom = _semantic_atom(self.record["atoms"][0])
        atom_editor = AtomListEditor(widgets, [atom])
        cpd_editor = CPDPolicyEditor(widgets, self.record["cpd_policy"])
        live_size = _widget_registry_size()
        try:
            atom_before = copy.deepcopy(atom_editor.value())
            cpd_before = copy.deepcopy(cpd_editor.value())
            with self.assertRaisesRegex(
                ValidationError, "atom argument keys must be strings"
            ):
                atom_editor.atom_editors[0].add_argument(7, "x")
            with self.assertRaisesRegex(ValidationError, "CPD bin names must be strings"):
                cpd_editor.dimension_editors[0].add_bin(7, 1.0)
            with self.assertRaisesRegex(
                ValidationError, "JSON object keys must be strings"
            ):
                atom_editor.atom_editors[0].add_argument("bad_nested", {1: "one"})
            with self.assertRaisesRegex(
                ValidationError, "JSON object keys must be strings"
            ):
                cpd_editor.dimension_editors[0].add_allowed_value({1: "one"})
            gc.collect()
            self.assertEqual(_widget_registry_size(), live_size)
            self.assertEqual(atom_editor.value(), atom_before)
            self.assertEqual(cpd_editor.value(), cpd_before)

            with mock.patch(
                "bus_benchmark.review_field_widgets._argument_text",
                side_effect=ValueError("display conversion failed"),
            ):
                with self.assertRaisesRegex(ValueError, "display conversion failed"):
                    atom_editor.atom_editors[0].add_argument("huge", 1)
                gc.collect()
                self.assertEqual(_widget_registry_size(), live_size)

                with self.assertRaisesRegex(ValueError, "display conversion failed"):
                    cpd_editor.dimension_editors[0].add_allowed_value(1)
                gc.collect()
                self.assertEqual(_widget_registry_size(), live_size)
        finally:
            atom_editor.close()
            cpd_editor.close()
        gc.collect()
        self.assertLessEqual(_widget_registry_size(), baseline)


if __name__ == "__main__":
    unittest.main()
