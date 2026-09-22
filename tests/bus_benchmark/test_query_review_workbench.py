import gc
import json
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from bus_benchmark.errors import ValidationError
from bus_benchmark.human_workflow import (
    export_query_review_bundle,
    query_check_projection,
    validate_query_review_task_response,
)
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    write_json,
)
from bus_benchmark.query_review_workbench import (
    ATOM_ACCEPT_REASON,
    CHECK_ACCEPT_REASON,
    CHECK_REVISE_REASON,
    CPD_ACCEPT_REASON,
    CPD_REVISION_REASON_TEXT,
    DERIVED_REQUIRED_CHECKS,
    DEFAULT_ASSIGNMENT_DIR,
    LEGACY_ASSIGNMENT_DIR,
    QueryReviewSession,
    QueryReviewWorkbench,
    _human_atom_statement,
    ensure_review_assignment,
    launch_workbench,
    response_from_form,
)


ROOT = Path(__file__).resolve().parents[2]
DEV_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
DEV_ORACLE = ROOT / "benchmark_artifacts" / "drafts" / "dev_oracle_draft.jsonl"
TEST_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_query_library_v0_2.jsonl"
TEST_ORACLE = ROOT / "benchmark_artifacts" / "drafts" / "test_oracle_draft.jsonl"


class WorkbenchMigrationBoundaryTests(unittest.TestCase):
    def test_all_v02_atoms_have_readable_cardinality_and_optional_labels(self):
        rows = read_jsonl(DEV_ORACLE) + read_jsonl(TEST_ORACLE)
        statements = [
            _human_atom_statement(atom) for row in rows for atom in row["atoms"]
        ]
        self.assertEqual(len(statements), 3601)
        self.assertFalse(any("multiple 个" in value for value in statements))
        self.assertFalse(any("optional 个" in value for value in statements))
        self.assertFalse(any("应为 optional" in value for value in statements))
        self.assertTrue(any("未限定精确数量" in value for value in statements))
        self.assertTrue(any("不是必需参与者" in value for value in statements))
        self.assertTrue(any("可选配置" in value for value in statements))

    def test_legacy_v0_1_assignment_directory_is_read_only(self):
        with self.assertRaisesRegex(ValidationError, "v0.1 历史目录"):
            ensure_review_assignment("human-reviewer", LEGACY_ASSIGNMENT_DIR)

    def test_launcher_defaults_to_v0_2_assignment_directory(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        launcher = launch_workbench()
        self.assertEqual(Path(launcher.children[2].value), DEFAULT_ASSIGNMENT_DIR)


class QueryReviewWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.library = read_jsonl(DEV_LIBRARY)[:2]
        self.oracle = read_jsonl(DEV_ORACLE)[:2]
        self.bundle = export_query_review_bundle(
            self.library, self.oracle, reviewer_id="human-reviewer"
        )

    def _session(self):
        return QueryReviewSession(
            self.bundle,
            library_source=self.library,
            oracle_source=self.oracle,
            split="development",
            checkpoint_path=self.workspace / "checkpoint.json",
        )

    def _workbench(self, session):
        workbench = QueryReviewWorkbench(session)
        self.addCleanup(workbench.close)
        return workbench

    def _triplet_session(self, intent_group_id="BSG_DEV_v0_2_S01"):
        library = [
            row
            for row in read_jsonl(DEV_LIBRARY)
            if row["intent_group_id"] == intent_group_id
        ]
        oracle = [
            row
            for row in read_jsonl(DEV_ORACLE)
            if row["intent_group_id"] == intent_group_id
        ]
        bundle = export_query_review_bundle(
            library, oracle, reviewer_id="human-reviewer"
        )
        return QueryReviewSession(
            bundle,
            library_source=library,
            oracle_source=oracle,
            split="development",
            checkpoint_path=self.workspace / "triplet-checkpoint.json",
        )

    @staticmethod
    def _align_required_checks(task, form):
        proposal = response_from_form(task, form)["proposed_oracle"]
        for check in form["required_check_decisions"]:
            if check in (
                "support_and_response_disposition",
                "cpd_common_eligibility",
            ):
                continue
            changed = query_check_projection(
                check, task["oracle_draft"]
            ) != query_check_projection(check, proposal)
            form["required_check_decisions"][check] = {
                "verdict": "revise" if changed else "accept",
                "reason": CHECK_REVISE_REASON if changed else CHECK_ACCEPT_REASON,
            }

    def test_ui_presents_plain_language_five_step_review_before_technical_fields(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        self.assertEqual(
            [workbench.review_tabs.get_title(index).split(" · ")[0] for index in range(5)],
            [
                "1 参与者与外部事件",
                "2 道路与空间",
                "3 规则与风险",
                "4 最终一致性确认",
                "5 CPD 与备注",
            ],
        )
        atom = session.task(workbench.current_query_id)["oracle_draft"]["atoms"][0]
        atom_widgets = workbench._atom_widgets[atom["atom_id"]]
        card_text = workbench.atom_step_panels["actors_events"].children[0].children[
            0
        ].value
        self.assertIn("机器草稿的计分严格程度", card_text)
        self.assertIn("候选依据", card_text)
        self.assertNotIn(atom["atom_id"], card_text)
        self.assertIsNone(atom_widgets["advanced"].selected_index)
        self.assertIn(
            ("不应作为评分要求", "reject"), atom_widgets["verdict"].options
        )
        self.assertIn(
            ("当前要求内容和计分方式都正确", "accept"),
            atom_widgets["verdict"].options,
        )
        gaps = workbench._completion_gaps(workbench._collect_form())
        self.assertTrue(any("要求卡未判断" in gap for gap in gaps))
        self.assertTrue(any("CPD 结论尚未判断" in gap for gap in gaps))
        self.assertEqual(workbench.cpd_required_summary_row.layout.display, "none")
        workbench.cpd_verdict.value = "accept"
        workbench.cpd_reason.value = "当前 query 不包含可奖励的跨平台变化维度"
        collected = workbench._collect_form()
        self.assertEqual(
            collected["required_check_decisions"]["cpd_common_eligibility"],
            {
                "verdict": collected["cpd_decision"]["verdict"],
                "reason": collected["cpd_decision"]["reason"],
            },
        )

    def test_quick_decisions_update_progress_and_keep_disagreement_drafts(self):
        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        query_id = workbench.current_query_id
        controls = next(iter(workbench._atom_widgets.values()))
        self.assertEqual(controls["verdict"].value, "")
        self.assertIn("待判断", controls["status"].value)
        self.assertIsNone(controls["decision_details"].selected_index)
        title_before = workbench.review_tabs.get_title(0)

        controls["quick_buttons"]["accept"].click()
        self.assertEqual(controls["verdict"].value, "accept")
        self.assertEqual(controls["reason"].layout.display, "none")
        self.assertIn("已选", controls["status"].value)
        self.assertNotEqual(workbench.review_tabs.get_title(0), title_before)
        controls["quick_buttons"]["reject"].click()
        self.assertEqual(controls["reason"].layout.display, "")
        controls["reason"].value = "本条文本没有要求这一条件"
        controls["quick_buttons"]["accept"].click()
        controls["quick_buttons"]["reject"].click()
        self.assertEqual(controls["reason"].value, "本条文本没有要求这一条件")
        self.assertTrue(workbench._save_current(False))
        saved = session.form(query_id)["atom_decisions"][0]
        self.assertEqual(saved["verdict"], "reject")
        self.assertEqual(saved["reason"], "本条文本没有要求这一条件")
        self.assertNotEqual(session.status(query_id), "complete")
        self.assertEqual(session.progress()["human_gold_records"], 0)
        # Restored card state comes from the existing checkpoint, not a UI-only answer.
        restored = next(iter(workbench._atom_widgets.values()))
        self.assertIn("不应作为评分要求", restored["status"].value)

    def test_guide_and_batch_entry_do_not_change_answers(self):
        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        before = workbench._collect_form()
        checkpoint = session.checkpoint_path.read_bytes()
        self.assertIsNone(workbench.batch_accordion.selected_index)
        for _ in range(2):
            workbench.guide_button.click()
            workbench.batch_button.click()
            for panel in (workbench.guide_accordion, workbench.batch_accordion):
                self.assertEqual(panel.layout.display, "" if panel.selected_index == 0 else "none")
        self.assertIsNone(workbench.guide_accordion.selected_index)
        self.assertIsNone(workbench.batch_accordion.selected_index)
        self.assertEqual(workbench._collect_form(), before)
        self.assertFalse(workbench.dirty)
        self.assertEqual(session.checkpoint_path.read_bytes(), checkpoint)

    def test_complete_and_continue_skips_completed_and_preserves_search(self):
        session = self._triplet_session()
        first, second, third = session.ordered_query_ids
        session.mark_complete(second, session.mechanical_form(second))
        session.save_form(first, session.mechanical_form(first))
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        search = session.task(first)["query_record"]["intent_group_id"]
        workbench.search.value = search
        workbench.query_select.value = first
        with patch.object(workbench.query_select, "focus") as focus:
            with patch.object(workbench, "_render_current", wraps=workbench._render_current) as render:
                self.assertTrue(workbench._complete_and_continue())
        focus.assert_called_once_with()
        render.assert_called_once_with()
        self.assertEqual(session.status(first), "complete")
        self.assertEqual(workbench.current_query_id, third)
        self.assertEqual(workbench.search.value, search)
        self.assertEqual(workbench.filter.value, "all")
        self.assertNotEqual(session.status(third), "complete")

    def test_complete_and_continue_uses_original_position_in_dynamic_filter(self):
        session = self._triplet_session()
        first, second, third = session.ordered_query_ids
        session.save_form(second, session.mechanical_form(second))
        with patch.object(session, "has_attention", return_value=True):
            workbench = self._workbench(session)
            workbench.query_select.value = second
            self.assertTrue(workbench._complete_and_continue())
            self.assertEqual(workbench.current_query_id, third)
            self.assertNotEqual(session.status(first), "complete")

    def test_complete_and_continue_stays_on_failure_and_does_not_escape_search(self):
        session = self._session()
        first, second = session.ordered_query_ids
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        workbench.query_select.value = first
        self.assertFalse(workbench._complete_and_continue())
        self.assertEqual(workbench.current_query_id, first)
        session.save_form(first, session.mechanical_form(first))
        workbench._render_current()
        # A formal validation failure must not navigate either.
        with patch.object(session, "mark_complete_and_seed_siblings", side_effect=ValidationError("validation failed")):
            self.assertFalse(workbench._complete_and_continue())
        self.assertEqual(workbench.current_query_id, first)
        workbench.search.value = first
        self.assertTrue(workbench._complete_and_continue())
        self.assertEqual(workbench.current_query_id, first)
        self.assertNotEqual(session.status(second), "complete")

    def test_complete_and_continue_wraps_and_disables_actions_for_empty_queue(self):
        session = self._triplet_session()
        first, second, third = session.ordered_query_ids
        session.mark_complete(second, session.mechanical_form(second))
        session.save_form(third, session.mechanical_form(third))
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        workbench.query_select.value = third
        self.assertTrue(workbench._complete_and_continue())
        self.assertEqual(workbench.current_query_id, first)
        workbench.search.value = "no-such-request"
        self.assertIsNone(workbench.current_query_id)
        self.assertTrue(workbench.complete_next_button.disabled)
        self.assertFalse(workbench._complete_and_continue())

    def test_final_check_mapping_is_explicit_for_lane_count_and_risk(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._triplet_session("BSG_DEV_v0_2_S02")
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        task = session.task(workbench.current_query_id)
        lane_count = next(
            atom
            for atom in task["oracle_draft"]["atoms"]
            if atom["predicate"] == "lane_configuration"
            and atom["arguments"].get("feature") == "motor_lanes_same_direction"
        )
        risk = next(
            atom
            for atom in task["oracle_draft"]["atoms"]
            if atom["predicate"] == "risk_level"
        )

        def mapped_checks(atom_id):
            return {
                check
                for check in (
                    "cardinality_and_polarity",
                    "road_and_spatial_decomposition",
                    "event_graph_and_actor_binding",
                    "surface_layering",
                )
                if atom_id in workbench._mapped_source_atom_ids(task, check)
            }

        self.assertEqual(
            mapped_checks(lane_count["atom_id"]),
            {
                "cardinality_and_polarity",
                "road_and_spatial_decomposition",
                "surface_layering",
            },
        )
        self.assertEqual(
            mapped_checks(risk["atom_id"]),
            {
                "cardinality_and_polarity",
                "event_graph_and_actor_binding",
                "surface_layering",
            },
        )

        for atom, expected_labels in (
            (
                lane_count,
                ("要求内容、数量与必须/禁止", "道路与空间", "计分严格程度"),
            ),
            (
                risk,
                (
                    "要求内容、数量与必须/禁止",
                    "参与者、事件、规则与风险语义",
                    "计分严格程度",
                ),
            ),
        ):
            location = workbench._atom_locations[atom["atom_id"]]
            card = workbench.atom_step_panels[location["step"]].children[
                location["index"]
            ]
            rendered = card.children[0].value
            self.assertIn("第4步自动关联", rendered)
            for label in expected_labels:
                self.assertIn(label, rendered)
            self.assertNotIn("cardinality_and_polarity", rendered)

        road_mapping = workbench._required_widgets[
            "road_and_spatial_decomposition"
        ]["mapping_info"].value
        event_mapping = workbench._required_widgets[
            "event_graph_and_actor_binding"
        ]["mapping_info"].value
        self.assertIn(_human_atom_statement(lane_count), road_mapping)
        self.assertNotIn(_human_atom_statement(risk), road_mapping)
        self.assertIn(_human_atom_statement(risk), event_mapping)
        self.assertNotIn(_human_atom_statement(lane_count), event_mapping)

        mapping_guide = workbench.review_tabs.children[3].children[1].value
        self.assertIn("不是让你重复审核", mapping_guide)
        self.assertIn("不是让你手工给评分指标分配字段", mapping_guide)
        self.assertIn(_human_atom_statement(lane_count), mapping_guide)
        self.assertIn(_human_atom_statement(risk), mapping_guide)

    def test_completing_precise_seeds_incomplete_partial_and_vague_drafts(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._triplet_session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        precise_id, partial_id, vague_id = session.ordered_query_ids
        self.assertEqual(workbench.current_query_id, precise_id)
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)
        self.assertTrue(workbench._save_current(True))

        self.assertEqual(session.status(precise_id), "complete")
        for target_id in (partial_id, vague_id):
            self.assertNotEqual(session.status(target_id), "complete")
            self.assertFalse(session._entry(target_id)["human_confirmed"])
            target_form = session.form(target_id)
            self.assertTrue(
                all(item["verdict"] for item in target_form["atom_decisions"])
            )
            self.assertTrue(
                all(
                    item["verdict"]
                    for item in target_form["required_check_decisions"].values()
                )
            )
            self.assertTrue(target_form["cpd_decision"]["verdict"])

            precise_decisions = {
                item["atom_id"]: item for item in session.form(precise_id)["atom_decisions"]
            }
            for target_decision in target_form["atom_decisions"]:
                if target_decision["atom_id"] in precise_decisions:
                    self.assertEqual(
                        target_decision, precise_decisions[target_decision["atom_id"]]
                    )
            self.assertNotIn(
                "surface_numeric_constraint",
                {
                    atom["predicate"]
                    for atom in session.task(target_id)["oracle_draft"]["atoms"]
                },
            )
        self.assertEqual(session.progress()["human_gold_records"], 0)

    def test_precise_inheritance_never_overwrites_existing_surface_work(self):
        session = self._triplet_session()
        precise_id, partial_id, vague_id = session.ordered_query_ids
        partial_form = session.form(partial_id)
        partial_form["notes"] = "reviewer already started this surface"
        session.save_form(partial_id, partial_form)

        seeded = session.mark_complete_and_seed_siblings(
            precise_id, session.mechanical_form(precise_id)
        )

        self.assertEqual(seeded, [partial_id, vague_id])
        self.assertEqual(
            session.form(partial_id)["notes"], "reviewer already started this surface"
        )
        self.assertTrue(
            all(item["verdict"] for item in session.form(partial_id)["atom_decisions"])
        )
        self.assertFalse(session._entry(partial_id)["human_confirmed"])
        self.assertTrue(session._form_has_content(session.form(vague_id)))
        self.assertFalse(session._entry(vague_id)["human_confirmed"])

    def test_existing_completed_precise_can_explicitly_seed_one_blank_surface(self):
        session = self._triplet_session()
        precise_id, partial_id, vague_id = session.ordered_query_ids
        session.mark_complete(precise_id, session.mechanical_form(precise_id))
        self.assertTrue(session.can_seed_from_precise(partial_id))

        source = session.seed_from_completed_precise(partial_id)

        self.assertEqual(source, precise_id)
        self.assertFalse(session.can_seed_from_precise(partial_id))
        self.assertFalse(session._entry(partial_id)["human_confirmed"])
        self.assertTrue(session._form_has_content(session.form(partial_id)))
        self.assertTrue(session.can_seed_from_precise(vague_id))

    def test_atom_batch_fills_only_blank_unconfirmed_instances(self):
        session = self._triplet_session()
        batch = next(
            item
            for item in session.atom_review_batches()
            if len(item["instances"]) == 3
        )
        by_style = {item["surface_style"]: item for item in batch["instances"]}

        partial = by_style["partial"]
        partial_form = session.form(partial["subject_id"])
        partial_decision = next(
            item
            for item in partial_form["atom_decisions"]
            if item["atom_id"] == partial["atom_id"]
        )
        partial_decision.update(
            {
                "verdict": "reject",
                "reason": "当前 partial 原文没有要求这一细节",
            }
        )
        session.save_form(partial["subject_id"], partial_form)

        vague = by_style["vague"]
        session.mark_complete(
            vague["subject_id"], session.mechanical_form(vague["subject_id"])
        )
        result = session.apply_atom_batch_decision(batch["batch_id"], "accept")

        self.assertEqual(
            result,
            {
                "instances_total": 3,
                "applied": 1,
                "skipped_complete": 1,
                "skipped_existing": 1,
            },
        )
        precise = by_style["precise"]
        precise_decision = next(
            item
            for item in session.form(precise["subject_id"])["atom_decisions"]
            if item["atom_id"] == precise["atom_id"]
        )
        self.assertEqual(precise_decision["verdict"], "accept")
        self.assertFalse(session._entry(precise["subject_id"])["human_confirmed"])
        preserved = next(
            item
            for item in session.form(partial["subject_id"])["atom_decisions"]
            if item["atom_id"] == partial["atom_id"]
        )
        self.assertEqual(preserved["verdict"], "reject")

    def test_permitted_atom_batch_materializes_query_bound_targets(self):
        session = self._triplet_session()
        batch = next(
            item
            for item in session.atom_review_batches()
            if item["atom"]["polarity"] == "present"
            and item["atom"]["layer"] != "permitted"
            and len(item["instances"]) == 3
        )

        result = session.apply_atom_batch_decision(batch["batch_id"], "permitted")

        self.assertEqual(result["applied"], 3)
        for instance in batch["instances"]:
            decision = next(
                item
                for item in session.form(instance["subject_id"])["atom_decisions"]
                if item["atom_id"] == instance["atom_id"]
            )
            self.assertEqual(decision["verdict"], "modify")
            targets = json.loads(decision["replacement_atoms_json"])
            self.assertEqual(targets[0]["layer"], "permitted")
            self.assertFalse(session._entry(instance["subject_id"])["human_confirmed"])

    def test_cpd_batches_are_surface_scoped_and_keep_subjects_incomplete(self):
        session = self._triplet_session()
        batches = session.cpd_review_batches()
        self.assertEqual({item["surface_style"] for item in batches}, {
            "precise", "partial", "vague"
        })
        precise = next(item for item in batches if item["surface_style"] == "precise")

        result = session.apply_cpd_batch_decision(precise["batch_id"], "accept")

        self.assertEqual(result["applied"], 1)
        precise_id = precise["instances"][0]["subject_id"]
        self.assertEqual(session.form(precise_id)["cpd_decision"]["verdict"], "accept")
        self.assertFalse(session._entry(precise_id)["human_confirmed"])
        for style in ("partial", "vague"):
            query_id = next(
                item["instances"][0]["subject_id"]
                for item in batches
                if item["surface_style"] == style
            )
            self.assertEqual(session.form(query_id)["cpd_decision"]["verdict"], "")

    def test_batch_panel_displays_every_covered_query_text(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._triplet_session()
        batch = next(
            item
            for item in session.atom_review_batches()
            if len(item["instances"]) == 3
        )
        workbench = self._workbench(session)
        workbench.atom_batch_select.value = batch["batch_id"]

        rendered = workbench.atom_batch_detail.value
        for instance in batch["instances"]:
            self.assertIn(instance["query_text"], rendered)
            self.assertIn(instance["subject_id"], rendered)

    def test_response_derives_four_checks_even_when_form_slots_are_blank(self):
        session = self._session()
        query_id = session.ordered_query_ids[0]
        task = session.task(query_id)
        form = session.mechanical_form(query_id)
        for check in DERIVED_REQUIRED_CHECKS:
            form["required_check_decisions"][check] = {"verdict": "", "reason": ""}

        response = response_from_form(task, form)

        self.assertTrue(
            all(
                response["required_check_decisions"][check]["verdict"] == "accept"
                for check in DERIVED_REQUIRED_CHECKS
            )
        )
        first = form["atom_decisions"][0]
        first.update(
            {
                "verdict": "reject",
                "reason": "当前 query 不要求这一候选内容",
                "replacement_atoms_json": "[]",
            }
        )
        revised = response_from_form(task, form)
        for check in DERIVED_REQUIRED_CHECKS:
            expected = (
                "revise"
                if query_check_projection(check, task["oracle_draft"])
                != query_check_projection(check, revised["proposed_oracle"])
                else "accept"
            )
            self.assertEqual(
                revised["required_check_decisions"][check]["verdict"], expected
            )

    def test_derived_check_exception_remains_a_source_blocker(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        workbench = self._workbench(self._session())
        workbench.filter.value = "all"
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)
        widgets = workbench._required_widgets["cardinality_and_polarity"]

        widgets["exception"].value = True
        self.assertEqual(widgets["verdict"].value, "reject")
        self.assertEqual(widgets["reason"].layout.display, "")
        self.assertTrue(
            any("只能保持阻塞" in gap for gap in workbench._completion_gaps(
                workbench._collect_form()
            ))
        )
        widgets["reason"].value = "冻结的 ego gate 与当前 source 不一致"
        form = workbench._collect_form()
        self.assertEqual(
            form["required_check_decisions"]["cardinality_and_polarity"]["verdict"],
            "reject",
        )
        with self.assertRaisesRegex(ValidationError, "rejected required check"):
            response = response_from_form(
                workbench.session.task(workbench.current_query_id), form
            )
            validate_query_review_task_response(
                workbench.session.task(workbench.current_query_id),
                response,
                reviewer_id=workbench.session.reviewer_id,
                require_confirmation=True,
            )

    def test_inheritance_drops_merge_group_if_a_precise_only_source_is_missing(self):
        session = self._triplet_session()
        precise_id, partial_id, vague_id = session.ordered_query_ids
        task = session.task(precise_id)
        form = session.mechanical_form(precise_id)
        atoms = task["oracle_draft"]["atoms"]
        precise_only = next(
            atom for atom in atoms if atom["predicate"] == "surface_numeric_constraint"
        )
        common = next(atom for atom in atoms if atom["category"] == "actor")
        target = {
            key: common[key]
            for key in (
                "category",
                "predicate",
                "arguments",
                "layer",
                "polarity",
                "notes",
            )
        }
        target["layer"] = "permitted"
        replacement_json = json.dumps([target])
        for decision in form["atom_decisions"]:
            if decision["atom_id"] in (precise_only["atom_id"], common["atom_id"]):
                decision.update(
                    {
                        "verdict": "merge",
                        "reason": "这两张 precise source 卡由同一个最终要求表达",
                        "replacement_atoms_json": replacement_json,
                    }
                )
        self._align_required_checks(task, form)

        seeded = session.mark_complete_and_seed_siblings(precise_id, form)

        self.assertEqual(seeded, [partial_id, vague_id])
        for target_id in seeded:
            target_decision = next(
                item
                for item in session.form(target_id)["atom_decisions"]
                if item["atom_id"] == common["atom_id"]
            )
            self.assertEqual(target_decision["verdict"], "accept")
            self.assertNotEqual(session.status(target_id), "draft")

    def test_inherited_cpd_revision_becomes_accept_when_target_already_matches(self):
        session = self._triplet_session()
        precise_id, partial_id, vague_id = session.ordered_query_ids
        precise_task = session.task(precise_id)
        partial_policy = session.task(partial_id)["oracle_draft"]["cpd_policy"]
        form = session.mechanical_form(precise_id)
        form["cpd_decision"] = {
            "verdict": "revise",
            "reason": CPD_REVISION_REASON_TEXT["candidate_classification"],
            "replacement_policy_json": json.dumps(partial_policy),
        }
        form["required_check_decisions"]["cpd_common_eligibility"] = {
            "verdict": "revise",
            "reason": CPD_REVISION_REASON_TEXT["candidate_classification"],
        }
        self._align_required_checks(precise_task, form)

        seeded = session.mark_complete_and_seed_siblings(precise_id, form)

        self.assertEqual(seeded, [partial_id, vague_id])
        for target_id in seeded:
            inherited = session.form(target_id)
            self.assertEqual(inherited["cpd_decision"]["verdict"], "accept")
            self.assertEqual(
                inherited["required_check_decisions"]["cpd_common_eligibility"][
                    "verdict"
                ],
                "accept",
            )
            self.assertNotEqual(session.status(target_id), "draft")

    def test_allowed_not_required_shortcut_builds_auditable_target(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        task = session.task(workbench.current_query_id)
        source = next(
            atom for atom in task["oracle_draft"]["atoms"] if atom["layer"] != "permitted"
        )
        atom_widgets = workbench._atom_widgets[source["atom_id"]]

        atom_widgets["permit_button"].click()

        self.assertEqual(atom_widgets["verdict"].value, "modify")
        self.assertEqual(atom_widgets["advanced"].selected_index, 0)
        targets = atom_widgets["replacement_editor"].value()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["layer"], "permitted")
        self.assertTrue(atom_widgets["reason"].value)
        atom_widgets["replacement_editor"].atom_editors[0].predicate.value = (
            "unfinished_human_target"
        )
        atom_widgets["permit_button"].click()
        self.assertEqual(
            atom_widgets["replacement_editor"].value()[0]["predicate"],
            "unfinished_human_target",
        )
        form = workbench._collect_form()
        decision = next(
            item for item in form["atom_decisions"] if item["atom_id"] == source["atom_id"]
        )
        self.assertEqual(
            json.loads(decision["replacement_atoms_json"])[0]["layer"], "permitted"
        )

    def test_permitted_shortcut_reopens_existing_or_invalid_edit_without_overwrite(self):
        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        atom = next(atom for atom in session.task(workbench.current_query_id)["oracle_draft"]["atoms"]
                    if atom["layer"] != "permitted")
        controls = workbench._atom_widgets[atom["atom_id"]]
        controls["clone_source"].click()
        before = controls["replacement_editor"].value()
        controls["decision_details"].selected_index = None
        controls["permit_button"].click()
        self.assertEqual(controls["decision_details"].selected_index, 0)
        self.assertEqual(controls["advanced"].selected_index, 0)
        self.assertEqual(controls["replacement_editor"].value(), before)
        self.assertEqual(controls["verdict"].value, "")
        controls["decision_details"].selected_index = None
        with patch.object(controls["replacement_editor"], "value", side_effect=ValidationError("unfinished edit")):
            controls["permit_button"].click()
        self.assertEqual(controls["decision_details"].selected_index, 0)
        self.assertEqual(controls["replacement_editor"].value(), before)

        controls["replacement_editor"].set_value([])
        controls["verdict"].value = "modify"
        controls["decision_details"].selected_index = None
        controls["advanced"].selected_index = None
        controls["permit_button"].click()
        self.assertEqual(controls["decision_details"].selected_index, 0)
        self.assertEqual(controls["advanced"].selected_index, 0)
        self.assertEqual(controls["replacement_editor"].value()[0]["layer"], "permitted")

    def test_conditional_reasons_only_show_free_text_for_disagreement(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        atom = session.task(workbench.current_query_id)["oracle_draft"]["atoms"][0]
        atom_widgets = workbench._atom_widgets[atom["atom_id"]]

        self.assertEqual(atom_widgets["reason"].layout.display, "none")
        atom_widgets["verdict"].value = "accept"
        self.assertEqual(atom_widgets["reason"].layout.display, "none")
        decision = next(
            item
            for item in workbench._collect_form()["atom_decisions"]
            if item["atom_id"] == atom["atom_id"]
        )
        self.assertEqual(decision["reason"], ATOM_ACCEPT_REASON)

        atom_widgets["verdict"].value = "reject"
        self.assertEqual(atom_widgets["reason"].layout.display, "")
        self.assertEqual(atom_widgets["reason"].value, "")
        atom_widgets["reason"].value = "当前 query 没有要求这一元数据细节"
        atom_widgets["verdict"].value = "accept"
        atom_widgets["verdict"].value = "reject"
        self.assertEqual(
            atom_widgets["reason"].value,
            "当前 query 没有要求这一元数据细节",
        )

        check_widgets = workbench._required_widgets["surface_layering"]
        self.assertTrue(check_widgets["verdict"].disabled)
        self.assertEqual(check_widgets["verdict"].value, "revise")
        self.assertEqual(check_widgets["reason"].layout.display, "none")
        self.assertEqual(
            workbench._collect_form()["required_check_decisions"]["surface_layering"][
                "reason"
            ],
            CHECK_REVISE_REASON,
        )
        check_widgets["verdict"].value = "revise"
        self.assertEqual(check_widgets["reason"].layout.display, "none")
        self.assertEqual(
            workbench._collect_form()["required_check_decisions"]["surface_layering"][
                "reason"
            ],
            CHECK_REVISE_REASON,
        )
        check_widgets["verdict"].value = "reject"
        self.assertEqual(check_widgets["reason"].layout.display, "none")

        source = session.task(workbench.current_query_id)["oracle_draft"]["atoms"][0]
        added = {
            key: source[key]
            for key in (
                "category",
                "predicate",
                "arguments",
                "layer",
                "polarity",
                "notes",
            )
        }
        added.update(
            {
                "predicate": "human_added_review_requirement",
                "arguments": {"value": "missing_from_machine_draft"},
            }
        )
        workbench.added_atoms_editor.add_atom(added)
        gaps = workbench._completion_gaps(workbench._collect_form())
        self.assertTrue(any("新增 requirement" in gap for gap in gaps))
        workbench.notes.value = "机器草稿遗漏了当前 query 明确要求的独立条件"
        gaps = workbench._completion_gaps(workbench._collect_form())
        self.assertFalse(any("新增 requirement" in gap for gap in gaps))

    def test_rejected_source_reintroduced_by_another_target_is_localized(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        task = session.task(workbench.current_query_id)
        form = session.mechanical_form(workbench.current_query_id)
        excluded, editing = task["oracle_draft"]["atoms"][:2]
        decisions = {item["atom_id"]: item for item in form["atom_decisions"]}
        decisions[excluded["atom_id"]].update(
            {
                "verdict": "reject",
                "reason": "当前 query 不要求这张 source 卡",
            }
        )
        replacement = {
            key: copy_value
            for key, copy_value in excluded.items()
            if key
            in (
                "category",
                "predicate",
                "arguments",
                "layer",
                "polarity",
                "notes",
            )
        }
        decisions[editing["atom_id"]].update(
            {
                "verdict": "modify",
                "reason": "需要修改为另一个最终要求",
                "replacement_atoms_json": json.dumps([replacement]),
            }
        )
        self._align_required_checks(task, form)

        issues = workbench._atom_completion_issues(form)
        editing_issue = next(
            issue for issue in issues if issue["atom_id"] == editing["atom_id"]
        )
        excluded_statement = _human_atom_statement(excluded)
        self.assertIn("target_invariant", editing_issue["codes"])
        self.assertIn(excluded_statement, "；".join(editing_issue["problems"]))
        gaps = workbench._completion_gaps(form)
        self.assertTrue(any("高级修改内容不一致" in gap for gap in gaps))
        workbench._show_completion_issues(form, gaps)
        self.assertEqual(
            workbench.review_tabs.selected_index,
            {"actors_events": 0, "road_space": 1, "rules_risk": 2}[
                workbench._atom_locations[editing["atom_id"]]["step"]
            ],
        )
        rendered = " ".join(
            child.children[0].value
            for child in workbench.completion_issues.children[1:]
            if hasattr(child, "children") and child.children
        )
        self.assertIn(excluded_statement, rendered)
        self.assertNotIn(excluded["atom_id"], rendered)
        self.assertNotIn("rejected atom remains in proposed_oracle", rendered)

    def test_added_requirement_identity_conflicts_are_localized_before_finalize(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        task = session.task(workbench.current_query_id)
        source = task["oracle_draft"]["atoms"][0]
        raw_source = {
            key: source[key]
            for key in (
                "category",
                "predicate",
                "arguments",
                "layer",
                "polarity",
                "notes",
            )
        }

        form = session.mechanical_form(workbench.current_query_id)
        form["added_atoms_json"] = json.dumps([raw_source])
        form["notes"] = "检查重复 requirement 的本地反馈"
        issues = workbench._added_atom_completion_issues(form)
        self.assertEqual(len(issues), 1)
        self.assertIn("duplicates_source", issues[0]["codes"])
        self.assertIn("原卡判断", "；".join(issues[0]["problems"]))
        gaps = workbench._completion_gaps(form)
        workbench._show_completion_issues(form, gaps)
        self.assertEqual(workbench.review_tabs.selected_index, 2)
        self.assertEqual(workbench.advanced_operations.selected_index, 0)
        detail = workbench.completion_issues.children[1].children[0].value
        self.assertIn(_human_atom_statement(source), detail)
        self.assertNotIn(source["atom_id"], detail)

        target = dict(raw_source)
        target["predicate"] = "human_reviewed_target_identity"
        decision = form["atom_decisions"][0]
        decision.update(
            {
                "verdict": "modify",
                "reason": "需要改成一个新 target",
                "replacement_atoms_json": json.dumps([target]),
            }
        )
        form["added_atoms_json"] = json.dumps([target, target])
        issues = workbench._added_atom_completion_issues(form)
        self.assertEqual(len(issues), 2)
        self.assertTrue(
            all("duplicates_target" in issue["codes"] for issue in issues)
        )
        self.assertTrue(
            all("duplicates_added" in issue["codes"] for issue in issues)
        )

    def test_cpd_reason_is_automatic_structured_or_manual_by_verdict(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        workbench = self._workbench(self._session())
        workbench.filter.value = "all"
        self.assertEqual(workbench.cpd_reason.layout.display, "none")
        self.assertEqual(workbench.cpd_reason_code.layout.display, "none")

        workbench.cpd_verdict.value = "accept"
        collected = workbench._collect_form()
        self.assertEqual(collected["cpd_decision"]["reason"], CPD_ACCEPT_REASON)
        self.assertEqual(
            collected["required_check_decisions"]["cpd_common_eligibility"][
                "reason"
            ],
            CPD_ACCEPT_REASON,
        )

        workbench.cpd_verdict.value = "revise"
        self.assertEqual(workbench.cpd_reason_code.layout.display, "")
        self.assertEqual(workbench.cpd_reason.layout.display, "none")
        workbench.cpd_reason_code.value = "dimension_definition"
        self.assertEqual(workbench.cpd_reason.layout.display, "none")
        self.assertEqual(
            workbench._collect_form()["cpd_decision"]["reason"],
            CPD_REVISION_REASON_TEXT["dimension_definition"],
        )
        workbench.cpd_reason_code.value = "other"
        self.assertEqual(workbench.cpd_reason.layout.display, "")
        workbench.cpd_reason.value = "允许值应按当前 query 的参与者重新定义"
        self.assertEqual(
            workbench._collect_form()["cpd_decision"]["reason"],
            "允许值应按当前 query 的参与者重新定义",
        )

        workbench.cpd_verdict.value = "reject"
        self.assertEqual(workbench.cpd_reason_code.layout.display, "none")
        self.assertEqual(workbench.cpd_reason.layout.display, "")
        self.assertEqual(workbench.cpd_reason.value, "")

    def test_high_level_checks_are_derived_from_structural_change(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"

        layering = workbench._required_widgets["surface_layering"]
        self.assertTrue(layering["verdict"].disabled)
        self.assertEqual(layering["verdict"].value, "accept")
        self.assertIn("自动派生", layering["reason_status"].value)
        form = workbench._collect_form()
        self.assertEqual(
            form["required_check_decisions"]["surface_layering"]["reason"],
            CHECK_ACCEPT_REASON,
        )
        self.assertFalse(
            any("尚未反映为对应结构变化" in gap for gap in workbench._completion_gaps(form))
        )

        support = workbench._required_widgets["support_and_response_disposition"]
        support["verdict"].value = "revise"
        self.assertEqual(support["reason"].layout.display, "")
        self.assertIn("需要人工说明", support["reason_status"].value)
        self.assertEqual(
            workbench._collect_form()["required_check_decisions"]
            ["support_and_response_disposition"]["reason"],
            "",
        )

        source = session.task(workbench.current_query_id)["oracle_draft"]["atoms"][0]
        atom_widgets = workbench._atom_widgets[source["atom_id"]]
        replacement = {
            key: source[key]
            for key in (
                "category",
                "predicate",
                "arguments",
                "layer",
                "polarity",
                "notes",
            )
        }
        replacement["layer"] = (
            "permitted" if source["layer"] != "permitted" else "surface_required"
        )
        atom_widgets["verdict"].value = "modify"
        atom_widgets["reason"].value = "当前 query 对这一细节的计分严格程度不同"
        atom_widgets["replacement_editor"].set_value([replacement])

        self.assertIn("自动派生", layering["reason_status"].value)
        self.assertEqual(layering["verdict"].value, "revise")
        self.assertEqual(
            workbench._collect_form()["required_check_decisions"]
            ["surface_layering"]["reason"],
            CHECK_REVISE_REASON,
        )

        query_id = workbench.current_query_id
        session.save_form(query_id, workbench._collect_form())
        workbench.close()
        restored = self._workbench(self._session())
        restored.filter.value = "all"
        restored_layering = restored._required_widgets["surface_layering"]
        self.assertEqual(restored_layering["verdict"].value, "revise")
        self.assertIn("自动派生", restored_layering["reason_status"].value)
        self.assertEqual(
            restored._collect_form()["required_check_decisions"]
            ["surface_layering"]["reason"],
            CHECK_REVISE_REASON,
        )

    def test_completion_failure_lists_and_focuses_each_problem_card(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)

        task = session.task(workbench.current_query_id)
        source = task["oracle_draft"]["atoms"][0]
        widgets = workbench._atom_widgets[source["atom_id"]]
        widgets["verdict"].value = "modify"
        widgets["reason"].value = "这条要求需要修改，但 target 尚未完成"
        widgets["replacement_editor"].set_value([], notify=False)
        widgets["advanced"].selected_index = None
        widgets["decision_details"].selected_index = None
        workbench.review_tabs.selected_index = 4

        with patch.object(widgets["verdict"], "focus") as focus:
            self.assertFalse(workbench._save_current(True))
        focus.assert_called_once_with()
        self.assertEqual(workbench.completion_issues.layout.display, "")
        self.assertGreaterEqual(len(workbench.completion_issues.children), 2)
        header = workbench.completion_issues.children[0].value
        issue_row = workbench.completion_issues.children[1]
        detail, locate = issue_row.children
        statement = _human_atom_statement(source)
        self.assertIn("完成检查发现：1 张问题要求卡", header)
        self.assertIn(statement, detail.value)
        self.assertIn("修改必须恰好提供 1 个最终 target", detail.value)
        self.assertEqual(locate.description, "定位这张要求卡")

        location = workbench._atom_locations[source["atom_id"]]
        expected_tab = {
            "actors_events": 0,
            "road_space": 1,
            "rules_risk": 2,
        }[location["step"]]
        self.assertEqual(workbench.review_tabs.selected_index, expected_tab)
        self.assertEqual(widgets["decision_details"].selected_index, 0)
        self.assertEqual(widgets["advanced"].selected_index, 0)

        workbench.review_tabs.selected_index = 4
        widgets["decision_details"].selected_index = None
        with patch.object(widgets["verdict"], "focus") as focus:
            locate.click()
        focus.assert_called_once_with()
        self.assertEqual(workbench.review_tabs.selected_index, expected_tab)
        self.assertEqual(widgets["decision_details"].selected_index, 0)

        workbench.search.value = "query-that-does-not-exist-anywhere"
        self.assertEqual(workbench.completion_issues.layout.display, "none")
        self.assertEqual(workbench.completion_issues.children, ())
        self.assertIn("没有符合筛选条件", workbench.content.children[0].value)

    def test_completion_issues_cover_formal_target_invariants(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)
        atoms = session.task(workbench.current_query_id)["oracle_draft"]["atoms"][:4]

        def replacement(atom, layer):
            value = {
                key: atom[key]
                for key in (
                    "category",
                    "predicate",
                    "arguments",
                    "polarity",
                    "notes",
                )
            }
            value["layer"] = layer
            return value

        alternate_layers = {
            atom["atom_id"]: next(
                layer
                for layer in (
                    "core_required",
                    "surface_required",
                    "permitted",
                    "forbidden",
                )
                if layer != atom["layer"]
            )
            for atom in atoms
        }

        modify, split, merge, unchanged = atoms
        modify_widgets = workbench._atom_widgets[modify["atom_id"]]
        modify_widgets["verdict"].value = "modify"
        modify_widgets["reason"].value = "测试修改 target 数量"
        modify_layers = [
            layer
            for layer in (
                "core_required",
                "surface_required",
                "permitted",
                "forbidden",
            )
            if layer != modify["layer"]
        ][:2]
        modify_widgets["replacement_editor"].set_value(
            [replacement(modify, layer) for layer in modify_layers], notify=False
        )

        split_widgets = workbench._atom_widgets[split["atom_id"]]
        split_widgets["verdict"].value = "split"
        split_widgets["reason"].value = "测试拆分 target 数量"
        split_widgets["replacement_editor"].set_value(
            [replacement(split, alternate_layers[split["atom_id"]])], notify=False
        )

        merge_widgets = workbench._atom_widgets[merge["atom_id"]]
        merge_widgets["verdict"].value = "merge"
        merge_widgets["reason"].value = "测试单成员 merge"
        merge_widgets["replacement_editor"].set_value(
            [replacement(merge, alternate_layers[merge["atom_id"]])], notify=False
        )

        unchanged_widgets = workbench._atom_widgets[unchanged["atom_id"]]
        unchanged_widgets["verdict"].value = "modify"
        unchanged_widgets["reason"].value = "测试无语义变化"
        unchanged_widgets["replacement_editor"].set_value(
            [replacement(unchanged, unchanged["layer"])], notify=False
        )

        issues = {
            issue["atom_id"]: "；".join(issue["problems"])
            for issue in workbench._atom_completion_issues(workbench._collect_form())
        }
        self.assertIn("修改必须恰好提供 1 个最终 target", issues[modify["atom_id"]])
        self.assertIn("拆分必须至少提供 2 个最终 target", issues[split["atom_id"]])
        self.assertIn("合并至少需要 2 张 source 卡", issues[merge["atom_id"]])
        self.assertIn("没有语义变化", issues[unchanged["atom_id"]])

        gaps = workbench._completion_gaps(workbench._collect_form())
        self.assertTrue(any("4 张要求卡的判断与高级修改内容不一致" in gap for gap in gaps))

    def test_manual_override_cannot_create_unrealized_high_level_revision(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        workbench = self._workbench(self._session())
        workbench.filter.value = "all"
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)
        workbench._required_widgets["surface_layering"]["verdict"].value = "revise"
        form = workbench._collect_form()
        gaps = workbench._completion_gaps(form)
        self.assertEqual(
            form["required_check_decisions"]["surface_layering"],
            {"verdict": "accept", "reason": CHECK_ACCEPT_REASON},
        )
        self.assertFalse(any("尚未反映" in gap for gap in gaps))

    def test_s02_atom_change_automatically_updates_high_level_checks(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        library = [
            row
            for row in read_jsonl(DEV_LIBRARY)
            if row["query_id"] == "BSG_DEV_v0_2_S02_precise"
        ]
        oracle = [
            row
            for row in read_jsonl(DEV_ORACLE)
            if row["query_id"] == "BSG_DEV_v0_2_S02_precise"
        ]
        bundle = export_query_review_bundle(
            library, oracle, reviewer_id="human-reviewer"
        )
        session = QueryReviewSession(
            bundle,
            library_source=library,
            oracle_source=oracle,
            split="development",
            checkpoint_path=self.workspace / "s02-checkpoint.json",
        )
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)

        task = session.task(workbench.current_query_id)
        lane_count = next(
            atom
            for atom in task["oracle_draft"]["atoms"]
            if atom["predicate"] == "lane_configuration"
            and atom["arguments"].get("feature") == "motor_lanes_same_direction"
        )
        lane_widgets = workbench._atom_widgets[lane_count["atom_id"]]
        lane_widgets["verdict"].value = "reject"
        lane_widgets["reason"].value = "当前 query 没有要求固定同方向机动车道数量"
        form = workbench._collect_form()
        issues = workbench._required_check_completion_issues(form)
        self.assertEqual(issues, [])
        self.assertEqual(
            form["required_check_decisions"]["cardinality_and_polarity"]["verdict"],
            "revise",
        )
        self.assertEqual(
            form["required_check_decisions"]["surface_layering"]["verdict"],
            "revise",
        )
        self.assertEqual(
            form["required_check_decisions"]["road_and_spatial_decomposition"][
                "verdict"
            ],
            "revise",
        )
        self.assertEqual(workbench._completion_gaps(workbench._collect_form()), [])
        self.assertTrue(workbench._save_current(True))

    def test_cpd_inconsistencies_are_localized_before_formal_validation(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        workbench = self._workbench(self._session())
        workbench.filter.value = "all"
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)

        workbench.cpd_verdict.value = "revise"
        workbench.cpd_reason_code.value = "other"
        form = workbench._collect_form()
        issues = workbench._cpd_completion_issues(form)
        self.assertEqual(len(issues), 1)
        self.assertIn("unrealized_revision", issues[0]["codes"])
        self.assertIn("missing_reason", issues[0]["codes"])
        self.assertTrue(
            any("policy 尚未实际变化" in gap for gap in workbench._completion_gaps(form))
        )
        self.assertFalse(workbench._save_current(True))
        self.assertEqual(workbench.review_tabs.selected_index, 4)
        self.assertEqual(workbench.cpd_advanced.selected_index, 0)
        header = workbench.completion_issues.children[0].value
        detail = workbench.completion_issues.children[1].children[0].value
        self.assertIn("1 项 CPD 结论", header)
        self.assertIn("只选择修改原因不能代替修改 policy", detail)
        self.assertIn("缺少必须的判断依据", detail)
        self.assertNotIn("cpd_common_eligibility", detail)
        apply_suggestion = workbench.completion_issues.children[1].children[1].children[1]
        self.assertIn("当前 CPD 结论正确", apply_suggestion.description)
        apply_suggestion.click()
        self.assertEqual(workbench.cpd_verdict.value, "accept")
        self.assertEqual(workbench._completion_gaps(workbench._collect_form()), [])

        workbench.cpd_verdict.value = "reject"
        workbench.cpd_reason.value = "CPD source 将目标参与者绑定错了"
        self.assertFalse(workbench._save_current(True))
        detail = workbench.completion_issues.children[1].children[0].value
        self.assertIn("只能保存为阻塞草稿", detail)
        self.assertNotIn("cpd_common_eligibility", detail)

        workbench.cpd_verdict.value = "accept"
        self.assertTrue(workbench._save_current(True))

    def test_cpd_accept_with_stale_advanced_edit_is_blocked_after_reload(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        workbench = self._workbench(self._session())
        workbench.filter.value = "all"
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)
        original_candidate = workbench.cpd_policy_editor.candidate.value
        workbench.cpd_policy_editor.candidate.value = not original_candidate
        workbench.cpd_verdict.value = "accept"
        self.assertTrue(workbench._save_current(False))

        resumed = self._workbench(self._session())
        resumed.filter.value = "all"
        self.assertEqual(resumed.cpd_verdict.value, "accept")
        self.assertEqual(
            resumed.cpd_policy_editor.candidate.value, not original_candidate
        )
        form = resumed._collect_form()
        issues = resumed._cpd_completion_issues(form)
        self.assertEqual(len(issues), 1)
        self.assertIn("accepted_drift", issues[0]["codes"])
        self.assertFalse(resumed._save_current(True))
        self.assertEqual(resumed.review_tabs.selected_index, 4)
        detail = resumed.completion_issues.children[1].children[0].value
        self.assertIn("policy 已发生变化", detail)
        self.assertNotIn("cpd_common_eligibility", detail)

        apply_suggestion = resumed.completion_issues.children[1].children[1].children[1]
        self.assertIn("我需要修改 CPD", apply_suggestion.description)
        apply_suggestion.click()
        resumed.cpd_reason_code.value = "candidate_classification"
        self.assertEqual(resumed._completion_gaps(resumed._collect_form()), [])
        self.assertTrue(resumed._save_current(True))

    def test_added_requirement_sources_are_attributed_individually(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        workbench = self._workbench(self._session())
        workbench.filter.value = "all"
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)
        road = {
            "category": "road",
            "predicate": "human_added_road_feature",
            "arguments": {"feature": "review_only_road", "value": True},
            "layer": "surface_required",
            "polarity": "present",
            "notes": "",
        }
        actor = {
            "category": "actor",
            "predicate": "human_added_actor_role",
            "arguments": {"actor_type": "pedestrian", "role": "review_only_actor"},
            "layer": "surface_required",
            "polarity": "present",
            "notes": "",
        }
        form = workbench._collect_form()
        form["added_atoms_json"] = json.dumps([road, actor])

        sources = workbench._required_check_change_sources(
            "road_and_spatial_decomposition", form
        )
        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0].startswith("人工新增："))
        self.assertIn("review only road", sources[0])
        self.assertNotIn("review only actor", sources[0])

    def test_machine_prefill_accept_reasons_become_human_attestations_on_complete(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        query_id = workbench.current_query_id
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)
        first_atom = session.task(query_id)["oracle_draft"]["atoms"][0]
        atom_widgets = workbench._atom_widgets[first_atom["atom_id"]]
        self.assertTrue(
            atom_widgets["reason"].value.startswith("Mechanical retention proposal:")
        )
        self.assertEqual(atom_widgets["reason"].layout.display, "none")

        self.assertTrue(workbench._save_current(True))
        saved = session.form(query_id)
        self.assertTrue(
            all(
                item["reason"] == ATOM_ACCEPT_REASON
                for item in saved["atom_decisions"]
            )
        )
        self.assertTrue(
            all(
                item["reason"] == CHECK_ACCEPT_REASON
                for check, item in saved["required_check_decisions"].items()
                if check != "cpd_common_eligibility"
            )
        )
        self.assertEqual(saved["cpd_decision"]["reason"], CPD_ACCEPT_REASON)

    def test_structured_cpd_revision_reason_passes_production_validation(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        query_id = workbench.current_query_id
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)

        workbench.cpd_verdict.value = "revise"
        workbench.cpd_reason_code.value = "candidate_classification"
        workbench.cpd_policy_editor.candidate.value = True

        self.assertTrue(workbench._save_current(True))
        saved = session.form(query_id)
        self.assertEqual(
            saved["cpd_decision"]["reason"],
            CPD_REVISION_REASON_TEXT["candidate_classification"],
        )
        self.assertEqual(
            saved["required_check_decisions"]["cpd_common_eligibility"][
                "reason"
            ],
            CPD_REVISION_REASON_TEXT["candidate_classification"],
        )

    def test_reason_drafts_saved_before_verdict_remain_visible_and_are_reused(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        query_id = session.ordered_query_ids[0]
        atom_id = session.task(query_id)["oracle_draft"]["atoms"][0]["atom_id"]
        form = session.form(query_id)
        next(
            item for item in form["atom_decisions"] if item["atom_id"] == atom_id
        )["reason"] = "尚未决定删除还是修改，但这条元数据不是当前文本的明确要求"
        form["required_check_decisions"]["surface_layering"]["reason"] = (
            "尚未决定是否属于 source blocker"
        )
        form["cpd_decision"]["reason"] = "CPD 维度可能需要调整"
        session.save_form(query_id, form)

        workbench = self._workbench(session)
        workbench.filter.value = "all"
        workbench.query_select.value = query_id
        atom_widgets = workbench._atom_widgets[atom_id]
        self.assertEqual(atom_widgets["reason"].layout.display, "")
        atom_widgets["verdict"].value = "reject"
        self.assertEqual(
            atom_widgets["reason"].value,
            "尚未决定删除还是修改，但这条元数据不是当前文本的明确要求",
        )

        check_widgets = workbench._required_widgets["surface_layering"]
        self.assertEqual(check_widgets["reason"].layout.display, "none")
        self.assertTrue(check_widgets["verdict"].disabled)
        check_widgets["verdict"].value = "reject"
        self.assertEqual(
            workbench._collect_form()["required_check_decisions"][
                "surface_layering"
            ]["verdict"],
            "revise",
        )

        self.assertEqual(workbench.cpd_reason.layout.display, "")
        workbench.cpd_verdict.value = "revise"
        self.assertEqual(workbench.cpd_reason_code.value, "other")
        self.assertEqual(workbench.cpd_reason.value, "CPD 维度可能需要调整")

    def test_checkpoint_and_explicit_completion_never_claim_gold(self):
        session = self._session()
        query_id = session.ordered_query_ids[0]
        form = session.mechanical_form(query_id)

        session.save_form(query_id, form)
        self.assertEqual(session.status(query_id), "locally_valid")
        checkpoint = read_json(session.checkpoint_path)
        self.assertFalse(checkpoint["human_gold"])
        self.assertFalse(checkpoint["formal_submission"])
        self.assertFalse(
            next(
                entry
                for entry in checkpoint["entries"]
                if entry["subject_id"] == query_id
            )["human_confirmed"]
        )

        resumed = self._session()
        self.assertEqual(resumed.status(query_id), "locally_valid")
        resumed.mark_complete(query_id, form)
        self.assertEqual(resumed.status(query_id), "complete")
        self.assertEqual(resumed.progress()["human_gold_records"], 0)

    def test_finalizer_is_the_only_gold_transition_and_does_not_overwrite(self):
        session = self._session()
        for query_id in session.ordered_query_ids:
            session.mark_complete(query_id, session.mechanical_form(query_id))

        output = self.workspace / "finalized"
        result = session.finalize(output)
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(len(read_jsonl(output / "human_query_gold.jsonl")), 2)
        self.assertEqual(len(read_jsonl(output / "confirmed_oracle.jsonl")), 2)
        with self.assertRaisesRegex(ValidationError, "\u5df2\u5b58\u5728"):
            session.finalize(output)

        external = self.workspace / "external-output"
        external.mkdir()
        marker = external / "foreign.txt"
        marker.write_text("do not replace", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "\u5df2\u5b58\u5728"):
            session.finalize(external)
        self.assertEqual(marker.read_text(encoding="utf-8"), "do not replace")

    def test_failed_finalization_retains_exclusive_directory_without_receipt(self):
        session = self._session()
        for query_id in session.ordered_query_ids:
            session.mark_complete(query_id, session.mechanical_form(query_id))

        output = self.workspace / "failed-finalization"

        def fail_after_foreign_file(path, _records):
            (Path(path).parent / "foreign-diagnostic.txt").write_text(
                "retain me", encoding="utf-8"
            )
            raise OSError("simulated write failure")

        with patch(
            "bus_benchmark.review_session.write_jsonl",
            side_effect=fail_after_foreign_file,
        ):
            with self.assertRaisesRegex(ValidationError, "\u5df2\u4fdd\u7559\u672a\u5b8c\u6210\u76ee\u5f55"):
                session.finalize(output)

        self.assertTrue(output.is_dir())
        self.assertTrue((output / "submission.json").is_file())
        self.assertEqual(
            (output / "foreign-diagnostic.txt").read_text(encoding="utf-8"),
            "retain me",
        )
        self.assertFalse((output / "finalization_summary.json").exists())
        with self.assertRaisesRegex(ValidationError, "\u5df2\u5b58\u5728"):
            session.finalize(output)
        self.assertEqual(
            (output / "foreign-diagnostic.txt").read_text(encoding="utf-8"),
            "retain me",
        )

    def test_ui_finalization_saves_dirty_form_and_requires_reconfirmation(self):
        session = self._session()
        for query_id in session.ordered_query_ids:
            session.mark_complete(query_id, session.mechanical_form(query_id))

        workbench = self._workbench(session)
        workbench.filter.value = "all"
        query_id = session.ordered_query_ids[0]
        workbench.query_select.value = query_id
        workbench.notes.value = "edited after the subject was marked complete"
        self.assertTrue(workbench.dirty)

        output = self.workspace / "must-not-finalize-dirty-state"
        workbench.finalize_output.value = str(output)
        workbench.finalize_confirmation.value = "FINALIZE"
        workbench._finalize(None)

        self.assertFalse(workbench.dirty)
        self.assertEqual(session.status(query_id), "locally_valid")
        self.assertFalse(output.exists())
        self.assertEqual(session.progress()["human_gold_records"], 0)

    def test_stale_rehashed_checkpoint_is_rejected(self):
        session = self._session()
        state = read_json(session.checkpoint_path)
        state["reviewer_id"] = "another-reviewer"
        core = {key: value for key, value in state.items() if key != "checkpoint_id"}
        state["checkpoint_id"] = sha256_bytes(canonical_json_bytes(core))
        write_json(session.checkpoint_path, state)

        with self.assertRaisesRegex(ValidationError, "reviewer_id binding is stale"):
            self._session()

    def test_v0_1_source_binding_cannot_resume_in_v0_2_session(self):
        session = self._session()
        state = read_json(session.checkpoint_path)
        state["source_binding"]["library_sha256"] = sha256_file(
            ROOT / "query_lib" / "bus_ego_topdown_2d_dev_query_library_v0_1.jsonl"
        )
        core = {key: value for key, value in state.items() if key != "checkpoint_id"}
        state["checkpoint_id"] = sha256_bytes(canonical_json_bytes(core))
        write_json(session.checkpoint_path, state)

        with self.assertRaisesRegex(ValidationError, "source_binding binding is stale"):
            self._session()

    def test_v0_1_checkpoint_schema_cannot_resume_in_v0_2_session(self):
        session = self._session()
        state = read_json(session.checkpoint_path)
        state["schema_version"] = "0.1"
        core = {key: value for key, value in state.items() if key != "checkpoint_id"}
        state["checkpoint_id"] = sha256_bytes(canonical_json_bytes(core))
        write_json(session.checkpoint_path, state)

        with self.assertRaisesRegex(ValidationError, "schema_version binding is stale"):
            self._session()

    def test_concurrent_session_cannot_silently_overwrite_newer_checkpoint(self):
        first = self._session()
        second = self._session()
        first_query, second_query = first.ordered_query_ids
        first_form = first.form(first_query)
        first_form["notes"] = "first session"
        first.save_form(first_query, first_form)

        second_form = second.form(second_query)
        second_form["notes"] = "stale second session"
        with self.assertRaisesRegex(ValidationError, "\u53e6\u4e00\u4f1a\u8bdd"):
            second.save_form(second_query, second_form)

        reloaded = self._session()
        self.assertEqual(reloaded.form(first_query)["notes"], "first session")
        self.assertEqual(reloaded.form(second_query)["notes"], "")

    def test_stale_complete_session_cannot_finalize_over_newer_human_decision(self):
        initial = self._session()
        for query_id in initial.ordered_query_ids:
            initial.mark_complete(query_id, initial.mechanical_form(query_id))
        stale = self._session()
        newer = self._session()
        query_id = newer.ordered_query_ids[0]
        form = newer.form(query_id)
        form["notes"] = "newer human decision"
        newer.mark_complete(query_id, form)

        with self.assertRaisesRegex(ValidationError, "\u9648\u65e7 workbench"):
            stale.finalize(self.workspace / "stale-finalization")
        self.assertFalse((self.workspace / "stale-finalization").exists())

    def test_source_correction_decision_cannot_be_marked_complete(self):
        session = self._session()
        query_id = session.ordered_query_ids[0]
        form = session.mechanical_form(query_id)
        form["required_check_decisions"]["support_and_response_disposition"] = {
            "verdict": "revise",
            "reason": "frozen source metadata needs correction",
        }
        with self.assertRaises(ValidationError):
            session.mark_complete(query_id, form)
        self.assertNotEqual(session.status(query_id), "complete")

    def test_widget_renders_one_subject_and_saves_before_direct_switch(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        first, second = session.ordered_query_ids
        workbench.query_select.value = first
        self.assertTrue(workbench.reset_button.disabled)
        workbench.reset_ack.value = True
        self.assertFalse(workbench.reset_button.disabled)
        workbench.notes.value = "unsaved reviewer note"
        self.assertTrue(workbench.dirty)

        workbench.query_select.value = second
        self.assertEqual(
            session.form(first)["notes"], "unsaved reviewer note"
        )
        self.assertEqual(workbench.current_query_id, second)
        self.assertEqual(
            len(workbench._atom_widgets),
            len(session.task(second)["oracle_draft"]["atoms"]),
        )

    def test_next_does_not_skip_after_save_removes_subject_from_filter(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        first, second = session.ordered_query_ids
        workbench.filter.value = "pending"
        workbench.query_select.value = first
        workbench.notes.value = "save before navigating"

        workbench.next_button.click()

        self.assertEqual(session.form(first)["notes"], "save before navigating")
        self.assertEqual(workbench.current_query_id, second)

    def test_graphical_atom_revision_passes_production_subject_validation(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        query_id = session.ordered_query_ids[0]
        workbench.filter.value = "all"
        workbench.query_select.value = query_id
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)

        source_atom = next(
            atom
            for atom in session.task(query_id)["oracle_draft"]["atoms"]
            if atom["category"] == "actor"
        )
        widgets = workbench._atom_widgets[source_atom["atom_id"]]
        widgets["verdict"].value = "modify"
        widgets["reason"].value = "把未在当前 surface 明示的角色改为 permitted"
        widgets["clone_source"].click()
        replacement = widgets["replacement_editor"].atom_editors[0]
        replacement.layer.value = "permitted"

        for check in ("event_graph_and_actor_binding", "surface_layering"):
            workbench._required_widgets[check]["verdict"].value = "revise"
            workbench._required_widgets[check]["reason"].value = (
                "该 actor 的 requirement layer 已由人工修订"
            )

        self.assertTrue(workbench._save_current(True))
        self.assertEqual(session.status(query_id), "complete")
        response = response_from_form(session.task(query_id), session.form(query_id))
        revised = next(
            atom
            for atom in response["proposed_oracle"]["atoms"]
            if atom["atom_id"] == source_atom["atom_id"]
        )
        self.assertEqual(revised["layer"], "permitted")
        self.assertEqual(revised["provenance"]["source"], "human_review")

    def test_structured_draft_survives_save_before_verdict_and_reload(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        query_id = session.ordered_query_ids[0]
        workbench.query_select.value = query_id
        source_atom = session.task(query_id)["oracle_draft"]["atoms"][0]
        atom_widgets = workbench._atom_widgets[source_atom["atom_id"]]
        atom_widgets["clone_source"].click()
        atom_widgets["replacement_editor"].atom_editors[0].layer.value = "permitted"
        workbench.cpd_policy_editor.candidate.value = True

        self.assertEqual(atom_widgets["verdict"].value, "")
        self.assertEqual(workbench.cpd_verdict.value, "")
        self.assertTrue(workbench._save_current(False))

        resumed = self._session()
        resumed_workbench = self._workbench(resumed)
        resumed_workbench.filter.value = "all"
        resumed_workbench.query_select.value = query_id
        resumed_atom = resumed_workbench._atom_widgets[source_atom["atom_id"]][
            "replacement_editor"
        ].value()
        self.assertEqual(len(resumed_atom), 1)
        self.assertEqual(resumed_atom[0]["layer"], "permitted")
        self.assertTrue(resumed_workbench.cpd_policy_editor.value()["candidate"])
        self.assertNotEqual(resumed.status(query_id), "complete")

    def test_cross_source_merge_wizard_builds_one_production_merge_group(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        session = self._session()
        workbench = self._workbench(session)
        workbench.filter.value = "all"
        query_id = session.ordered_query_ids[0]
        workbench.query_select.value = query_id
        workbench.mechanical_ack.value = True
        workbench._load_mechanical(None)
        road_atoms = [
            atom
            for atom in session.task(query_id)["oracle_draft"]["atoms"]
            if atom["category"] == "road"
        ][:2]
        source_ids = tuple(atom["atom_id"] for atom in road_atoms)

        workbench.merge_source_atoms.value = source_ids
        workbench.merge_template_button.click()
        workbench.merge_target_editor.atom_editors[0].predicate.value = (
            "human_merged_road_requirement"
        )
        workbench.merge_reason.value = "两个道路原子表达同一个统一道路要求"
        workbench.merge_apply_ack.value = True
        workbench.merge_apply_button.click()

        for atom_id in source_ids:
            self.assertEqual(workbench._atom_widgets[atom_id]["verdict"].value, "merge")
        for check in (
            "cardinality_and_polarity",
            "road_and_spatial_decomposition",
            "surface_layering",
        ):
            workbench._required_widgets[check]["verdict"].value = "revise"
            workbench._required_widgets[check]["reason"].value = (
                "跨源道路 atoms 已合并为一个共享 target"
            )

        self.assertTrue(workbench._save_current(True))
        response = response_from_form(session.task(query_id), session.form(query_id))
        merge_decisions = [
            item
            for item in response["atom_decisions"]
            if item["atom_id"] in source_ids
        ]
        self.assertEqual(len(merge_decisions), 2)
        replacement_ids = {
            item["replacement_atoms"][0]["atom_id"] for item in merge_decisions
        }
        self.assertEqual(len(replacement_ids), 1)
        self.assertEqual(session.status(query_id), "complete")
        for other_query_id in session.ordered_query_ids:
            if other_query_id != query_id:
                session.mark_complete(
                    other_query_id, session.mechanical_form(other_query_id)
                )
        output = self.workspace / "merge-finalized"
        session.finalize(output)
        gold = next(
            record
            for record in read_jsonl(output / "human_query_gold.jsonl")
            if record["query_id"] == query_id
        )
        merge_reviews = [
            item
            for item in gold["review_payload"]["draft_atom_reviews"]
            if item["draft_atom_id"] in source_ids
        ]
        self.assertEqual(len(merge_reviews), 2)
        self.assertEqual(
            len({item["operation_group_id"] for item in merge_reviews}), 1
        )
        self.assertTrue(merge_reviews[0]["operation_group_id"].startswith("merge:"))

    def test_repeated_navigation_keeps_widget_registry_bounded(self):
        try:
            import ipywidgets as ipywidgets
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            registry_size = lambda: len(ipywidgets.Widget.widgets)
            baseline = registry_size()
            library = read_jsonl(TEST_LIBRARY)
            oracle = read_jsonl(TEST_ORACLE)
            bundle = export_query_review_bundle(
                library, oracle, reviewer_id="human-reviewer"
            )
            session = QueryReviewSession(
                bundle,
                library_source=library,
                oracle_source=oracle,
                split="test",
                checkpoint_path=self.workspace / "test-navigation-checkpoint.json",
            )
            workbench = self._workbench(session)
            workbench.filter.value = "all"
            query_ids = session.ordered_query_ids[:125]
            counts_by_pass = []
            for _pass_index in range(2):
                counts = {}
                for query_id in query_ids:
                    workbench.query_select.value = query_id
                    counts[query_id] = registry_size()
                counts_by_pass.append(counts)
            # Different subjects legitimately render different numbers of
            # controls. A leak would make the second visit to the same subject
            # larger, so compare like-for-like rather than simple vs. complex.
            self.assertEqual(counts_by_pass[0], counts_by_pass[1])
            workbench.close()
            gc.collect()
            # ipywidgets keeps a small process-global set of style/output
            # models, but the closed workbench must not retain one model set
            # per visited subject.
            self.assertLessEqual(registry_size(), baseline + 200)

    def test_canonical_assignment_contains_300_query_subjects_and_zero_gold(self):
        assignment = self.workspace / "assignment"
        registry = ensure_review_assignment("human-reviewer", assignment)
        packets = {item["packet_name"]: item for item in registry["packets"]}
        self.assertEqual(packets["dev_query_review"]["packet_ready_subjects"], 48)
        self.assertEqual(packets["test_query_review"]["packet_ready_subjects"], 252)
        self.assertEqual(
            registry["human_gold"]["admissible_records_created_by_export"], 0
        )
        self.assertEqual(
            QueryReviewSession.from_assignment(assignment, "development").progress()[
                "subjects_total"
            ],
            48,
        )

    def test_coherent_registry_count_forgery_cannot_resume_assignment(self):
        assignment = self.workspace / "forged-assignment"
        registry = ensure_review_assignment("human-reviewer", assignment)
        forged = json.loads(canonical_json_bytes(registry).decode("utf-8"))
        dev_packet = next(
            packet
            for packet in forged["packets"]
            if packet["packet_name"] == "dev_query_review"
        )
        dev_packet["expected_explicit_verdict_entries"] += 1
        dev_packet["packet_ready_explicit_verdict_entries"] += 1
        forged["workload"]["query_atom_verdict_entries"] += 1
        forged["workload"]["query_machine_recommendation_entries"] += 1
        forged["workload"]["expected_explicit_verdict_entries"] += 1
        forged["workload"]["packet_ready_explicit_verdict_entries"] += 1
        forged["query_agent_semantic_review"]["draft_decision_entries"] += 1
        write_json(assignment / "machine_draft_registry.json", forged)
        with self.assertRaisesRegex(ValidationError, "live Agent/oracle contract"):
            ensure_review_assignment("human-reviewer", assignment)

    def test_launcher_saves_dirty_subject_before_switching_split(self):
        try:
            import ipywidgets  # noqa: F401
        except ImportError:
            self.skipTest("ipywidgets is not installed")

        development = self._session()
        test_session = QueryReviewSession(
            self.bundle,
            library_source=self.library,
            oracle_source=self.oracle,
            split="test",
            checkpoint_path=self.workspace / "test-checkpoint.json",
        )
        with patch(
            "bus_benchmark.query_review_workbench.ensure_review_assignment"
        ), patch(
            "bus_benchmark.query_review_workbench.QueryReviewSession.from_assignment",
            side_effect=[development, test_session],
        ):
            launcher = launch_workbench(self.workspace / "assignment")
            launcher.children[1].value = "human-reviewer"
            launcher.children[4].click()
            active = launcher._query_review_state["workbench"]
            active.filter.value = "all"
            query_id = active.current_query_id
            active.notes.value = "save before launcher replacement"
            self.assertTrue(active.dirty)

            launcher.children[3].value = "test"
            launcher.children[4].click()

        self.addCleanup(launcher._query_review_state["workbench"].close)
        self.assertEqual(
            development.form(query_id)["notes"],
            "save before launcher replacement",
        )
        self.assertTrue(active._closed)
        self.assertIs(
            launcher._query_review_state["workbench"].session, test_session
        )


class WorkbenchNotebookContractTests(unittest.TestCase):
    def test_notebook_is_thin_and_has_no_saved_outputs(self):
        notebook = ROOT / "workbench.ipynb"
        self.assertTrue(notebook.is_file())
        value = json.loads(notebook.read_text(encoding="utf-8"))
        code_cells = [cell for cell in value["cells"] if cell["cell_type"] == "code"]
        self.assertEqual(len(code_cells), 1)
        self.assertEqual(code_cells[0]["outputs"], [])
        self.assertIsNone(code_cells[0]["execution_count"])
        source = "".join(code_cells[0]["source"])
        compile(source, str(notebook), "exec")
        self.assertIn("launch_workbench", source)
        self.assertNotIn("human_gold", source)


if __name__ == "__main__":
    unittest.main()
