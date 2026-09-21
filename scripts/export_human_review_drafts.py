#!/usr/bin/env python3
"""Materialize source-bound CARLA-first packets for one final human reviewer.

The exporter never supplies a verdict and never creates human gold.  Query
packets are always available from the checked-in sources.  CARLA platform and
calibration packets are emitted only when their complete source groups are
provided; otherwise the generated registry records their exact blockers.
"""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bus_benchmark.errors import ValidationError
from bus_benchmark.agent_query_review import (
    DEFAULT_AGENT_REVIEW_FRAGMENT_PATHS,
    validate_agent_query_review_draft,
)
from bus_benchmark.human_workflow import (
    export_carla_stage_judge_calibration_review_bundle,
    export_carla_stage_platform_review_bundle,
    export_query_review_bundle,
    validate_human_document,
)
from bus_benchmark.jsonio import read_json, sha256_file, write_json


EXPECTED_QUERY_SUBJECTS = 300
EXPECTED_PLATFORM_SUBJECTS = 52
EXPECTED_CALIBRATION_SUBJECTS = 90
EXPECTED_CARLA_STAGE_SUBJECTS = 442
AGENT_QUERY_REVIEW = (
    ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "query_agent_semantic_review_v0_2.json"
)
LEGACY_V0_1_WORKBENCH_ASSIGNMENT = (
    ROOT / "benchmark_artifacts" / "human_review" / "workbench_assignment"
)

def _artifact_binding(path):
    path = path.resolve()
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _write_review_bundle(output, prefix, bundle):
    bundle_path = output / "{}_bundle.json".format(prefix)
    submission_path = output / "{}_submission_template.json".format(prefix)
    write_json(bundle_path, bundle)
    write_json(
        submission_path,
        bundle["reviewer_packet"]["submission_template"],
    )
    return {
        "bundle": _artifact_binding(bundle_path),
        "submission_template": _artifact_binding(submission_path),
    }


def _query_explicit_verdict_count(bundle):
    total = 0
    for task in bundle["reviewer_packet"]["tasks"]:
        template = task["response_template"]
        total += len(template["required_check_decisions"])
        total += len(template["atom_decisions"])
        total += 1  # one CPD verdict
    return total


def _ready_packet_entry(name, bundle, artifact_bindings, expected_subjects, verdicts):
    tasks = bundle["reviewer_packet"]["tasks"]
    if len(tasks) != expected_subjects:
        raise ValidationError(
            "{} expected {} subjects, got {}".format(
                name, expected_subjects, len(tasks)
            )
        )
    return {
        "packet_name": name,
        "status": "packet_ready",
        "expected_subjects": expected_subjects,
        "packet_ready_subjects": len(tasks),
        "expected_explicit_verdict_entries": verdicts,
        "packet_ready_explicit_verdict_entries": verdicts,
        "source_binding": bundle["source_binding"],
        "bundle": artifact_bindings["bundle"],
        "submission_template": artifact_bindings["submission_template"],
        "blockers": [],
    }


def _blocked_packet_entry(name, expected_subjects, verdicts, blockers):
    return {
        "packet_name": name,
        "status": "blocked_missing_inputs",
        "expected_subjects": expected_subjects,
        "packet_ready_subjects": 0,
        "expected_explicit_verdict_entries": verdicts,
        "packet_ready_explicit_verdict_entries": 0,
        "source_binding": None,
        "bundle": None,
        "submission_template": None,
        "blockers": blockers,
    }


def _all_or_none(parser, args, names, label):
    present = [getattr(args, name) is not None for name in names]
    if any(present) and not all(present):
        parser.error(
            "{} inputs must be supplied together: {}".format(
                label, ", ".join(names)
            )
        )
    return all(present)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ontology")
    parser.add_argument("--semantic-fixtures")
    parser.add_argument("--carla-capability")
    parser.add_argument("--calibration-roster")
    parser.add_argument("--calibration-reviewer-subjects")
    args = parser.parse_args()
    platform_inputs_present = _all_or_none(
        parser,
        args,
        ("ontology", "semantic_fixtures", "carla_capability"),
        "CARLA platform",
    )
    calibration_inputs_present = _all_or_none(
        parser,
        args,
        ("calibration_roster", "calibration_reviewer_subjects"),
        "CARLA calibration",
    )
    requested_output = Path(args.output_dir)
    if requested_output.is_symlink():
        raise ValidationError("review assignment output directory must not be a symlink")
    output = requested_output.resolve()
    if output == LEGACY_V0_1_WORKBENCH_ASSIGNMENT.resolve():
        raise ValidationError(
            "workbench_assignment is a read-only v0.1 historical directory; "
            "use workbench_assignment_v0_2 or another new directory"
        )
    if output.exists():
        if not output.is_dir():
            raise ValidationError("review assignment output path is not a directory")
        if any(output.iterdir()):
            raise ValidationError(
                "review assignment output directory must be new or empty; "
                "existing assignments are verified in place and never overwritten"
            )
    else:
        output.mkdir(parents=True, exist_ok=False)

    query_sources = {
        "test_query_review": (
            ROOT / "query_lib" / "bus_ego_topdown_2d_query_library_v0_2.jsonl",
            ROOT / "benchmark_artifacts" / "drafts" / "test_oracle_draft.jsonl",
        ),
        "dev_query_review": (
            ROOT / "query_lib" / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl",
            ROOT / "benchmark_artifacts" / "drafts" / "dev_oracle_draft.jsonl",
        ),
    }
    agent_query_review = read_json(AGENT_QUERY_REVIEW)
    agent_query_source_pairs = (
        (
            "development",
            query_sources["dev_query_review"][0],
            query_sources["dev_query_review"][1],
        ),
        (
            "test",
            query_sources["test_query_review"][0],
            query_sources["test_query_review"][1],
        ),
    )
    validate_agent_query_review_draft(
        agent_query_review,
        query_sources=agent_query_source_pairs,
        fragment_paths=DEFAULT_AGENT_REVIEW_FRAGMENT_PATHS,
    )
    agent_summary = agent_query_review["summary"]
    expected_query_atom_verdicts = agent_summary["atom_decisions"]
    expected_query_required_check_verdicts = agent_summary[
        "required_check_decisions"
    ]
    expected_query_cpd_verdicts = agent_summary["cpd_decisions"]
    expected_query_explicit_verdicts = agent_summary["explicit_decision_entries"]
    expected_carla_stage_explicit_verdicts = (
        expected_query_explicit_verdicts
        + EXPECTED_PLATFORM_SUBJECTS
        + EXPECTED_CALIBRATION_SUBJECTS
    )
    packets = []
    written = {}
    query_subjects = 0
    for prefix, (library, oracle) in query_sources.items():
        bundle = export_query_review_bundle(
            library,
            oracle,
            reviewer_id=args.reviewer,
        )
        bindings = _write_review_bundle(output, prefix, bundle)
        subject_count = len(bundle["reviewer_packet"]["tasks"])
        verdict_count = _query_explicit_verdict_count(bundle)
        packets.append(
            _ready_packet_entry(
                prefix,
                bundle,
                bindings,
                252 if prefix.startswith("test_") else 48,
                verdict_count,
            )
        )
        written[prefix] = bindings
        query_subjects += subject_count

    query_verdicts = sum(
        packet["packet_ready_explicit_verdict_entries"] for packet in packets
    )
    if (
        query_subjects != EXPECTED_QUERY_SUBJECTS
        or query_verdicts != expected_query_explicit_verdicts
    ):
        raise ValidationError(
            "checked-in query drafts must match the 300 source-bound Agent-review subjects and decisions"
        )

    if platform_inputs_present:
        platform_bundle = export_carla_stage_platform_review_bundle(
            Path(args.ontology),
            Path(args.semantic_fixtures),
            Path(args.carla_capability),
            reviewer_id=args.reviewer,
        )
        bindings = _write_review_bundle(
            output, "carla_platform_review", platform_bundle
        )
        packets.append(
            _ready_packet_entry(
                "carla_platform_review",
                platform_bundle,
                bindings,
                EXPECTED_PLATFORM_SUBJECTS,
                EXPECTED_PLATFORM_SUBJECTS,
            )
        )
        written["carla_platform_review"] = bindings
    else:
        packets.append(
            _blocked_packet_entry(
                "carla_platform_review",
                EXPECTED_PLATFORM_SUBJECTS,
                EXPECTED_PLATFORM_SUBJECTS,
                [
                    "48 complete development-query human gold records and the confirmed development oracle",
                    "source-bound common ontology derived from the confirmed development oracle",
                    "38 CARLA raw-artifact semantic fixtures",
                    "14-entry CARLA capability manifest",
                ],
            )
        )

    if calibration_inputs_present:
        calibration_bundle = export_carla_stage_judge_calibration_review_bundle(
            Path(args.calibration_roster),
            Path(args.calibration_reviewer_subjects),
            reviewer_id=args.reviewer,
        )
        bindings = _write_review_bundle(
            output, "carla_calibration_review", calibration_bundle
        )
        packets.append(
            _ready_packet_entry(
                "carla_calibration_review",
                calibration_bundle,
                bindings,
                EXPECTED_CALIBRATION_SUBJECTS,
                EXPECTED_CALIBRATION_SUBJECTS,
            )
        )
        written["carla_calibration_review"] = bindings
    else:
        packets.append(
            _blocked_packet_entry(
                "carla_calibration_review",
                EXPECTED_CALIBRATION_SUBJECTS,
                EXPECTED_CALIBRATION_SUBJECTS,
                [
                    "48 complete development-query human gold records and the confirmed development oracle",
                    "240 immutable CARLA development response records and 240 matching semantic-evidence records",
                    "source-bound 90-item CARLA calibration roster",
                    "90 blind reviewer subjects built before Judge predictions are exposed",
                ],
            )
        )

    ready_subjects = sum(packet["packet_ready_subjects"] for packet in packets)
    ready_verdicts = sum(
        packet["packet_ready_explicit_verdict_entries"] for packet in packets
    )
    registry = {
        "schema_version": "0.1",
        "workflow_type": "carla_stage_machine_draft_registry",
        "decision_status": "draft",
        "stage_scope": "carla_first",
        "review_policy": "one_complete_human_gold_per_subject",
        "reviewer_assignment": {
            "reviewer_id": args.reviewer,
            "identity_proof_provided": False,
            "authorship_trust_boundary": "external_human_custody_required",
        },
        "human_gold": {
            "admissible_records_created_by_export": 0,
            "completed_real_human_reviews_imported": 0,
            "submission_templates_are_gold": False,
            "machine_recommendations_are_gold": False,
            "machine_recommendations_populate_human_submissions": False,
        },
        "workload": {
            "expected_subjects": EXPECTED_CARLA_STAGE_SUBJECTS,
            "packet_ready_subjects": ready_subjects,
            "blocked_subjects": EXPECTED_CARLA_STAGE_SUBJECTS - ready_subjects,
            "query_subjects": EXPECTED_QUERY_SUBJECTS,
            "carla_platform_subjects": EXPECTED_PLATFORM_SUBJECTS,
            "carla_calibration_subjects": EXPECTED_CALIBRATION_SUBJECTS,
            "current_protocol_subjects": 0,
            "eventual_protocol_subjects": 13,
            "query_atom_verdict_entries": expected_query_atom_verdicts,
            "query_required_check_verdict_entries": (
                expected_query_required_check_verdicts
            ),
            "query_cpd_verdict_entries": expected_query_cpd_verdicts,
            "query_machine_recommendation_entries": (
                expected_query_explicit_verdicts
            ),
            "expected_explicit_verdict_entries": expected_carla_stage_explicit_verdicts,
            "packet_ready_explicit_verdict_entries": ready_verdicts,
            "blocked_explicit_verdict_entries": (
                expected_carla_stage_explicit_verdicts - ready_verdicts
            ),
            "minimum_post_test_subjects_per_method": 126,
            "optional_human_uqh_subjects_per_method_min": 0,
            "optional_human_uqh_subjects_per_method_max": 120,
        },
        "query_agent_semantic_review": {
            "artifact": _artifact_binding(AGENT_QUERY_REVIEW),
            "reviewed_intent_groups": agent_query_review["summary"]["intent_groups"],
            "reviewed_subjects": agent_query_review["summary"]["subjects"],
            "draft_decision_entries": agent_query_review["summary"][
                "explicit_decision_entries"
            ],
            "machine_only_non_gold": True,
            "human_submission_populated": False,
            "human_action_required": True,
        },
        "packets": packets,
        "dependency_order": [
            "Assign a real reviewer ID, consult the source-bound Agent semantic-review draft, and complete the 48 development-query packet.",
            "Finalize the confirmed development oracle, then derive the common ontology and CARLA fixture/capability sources.",
            "Export and complete the 52-subject CARLA platform packet; rejected candidates must be corrected and re-exported.",
            "Produce 240 immutable CARLA development outputs/evidence records, freeze the 90-item blind roster, and export the calibration packet.",
            "Complete the 252 test-query packet and all 90 blind calibration labels, then run inventory-refresh over all 442 source-bound subjects.",
        ],
        "metadrive_boundary": {
            "token_only_runtime_observer": "implemented_source_tested_independent_review_go",
            "tested_method_adapter": "not_provided",
            "real_tested_method_outputs": "not_provided",
            "included_in_current_human_packets": False,
            "machine_decision_crosswalk_entries": 6,
            "standalone_human_gold_subjects": 0,
        },
        "readiness": {
            "query_human_review": "GO",
            "carla_platform_human_review": (
                "GO" if platform_inputs_present else "NO_GO"
            ),
            "carla_calibration_human_review": (
                "GO" if calibration_inputs_present else "NO_GO"
            ),
            "carla_stage_freeze": "NO_GO",
            "reason": "packet export creates zero human gold; all 442 complete source-bound human decisions remain required",
        },
    }
    validate_human_document(registry, "machine_draft_registry")
    registry_path = output / "machine_draft_registry.json"
    write_json(registry_path, registry)

    summary = {
        "status": "pending_real_human_review",
        "reviewer_id_is_assignment_not_identity_proof": True,
        "review_policy": "one_complete_human_gold_per_subject",
        "query_subjects": query_subjects,
        "packet_ready_subjects": ready_subjects,
        "blocked_subjects": EXPECTED_CARLA_STAGE_SUBJECTS - ready_subjects,
        "explicit_verdict_entries_ready": ready_verdicts,
        "explicit_verdict_entries_expected": expected_carla_stage_explicit_verdicts,
        "query_machine_recommendation_entries": expected_query_explicit_verdicts,
        "query_agent_semantic_review": _artifact_binding(AGENT_QUERY_REVIEW),
        "query_agent_reviewed_intents": agent_query_review["summary"][
            "intent_groups"
        ],
        "query_agent_reviewed_subjects": agent_query_review["summary"]["subjects"],
        "query_agent_draft_decision_entries": agent_query_review["summary"][
            "explicit_decision_entries"
        ],
        "machine_recommendations_are_gold": False,
        "machine_recommendations_populate_human_submissions": False,
        "protocol_subjects": 0,
        "protocol_packet_scope": "deferred_until_metadrive_and_final_assets",
        "formal_protocol_review_created": False,
        "admissible_human_gold_created": 0,
        "machine_draft_registry": _artifact_binding(registry_path),
        "written": written,
    }
    write_json(output / "export_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
