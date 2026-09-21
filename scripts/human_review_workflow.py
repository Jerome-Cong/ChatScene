#!/usr/bin/env python3
"""CARLA-first production CLI for one-reviewer human-gold finalization."""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bus_benchmark.human_workflow import (
    build_carla_stage_calibration_reviewer_subjects,
    build_method_blind_post_test_bundle,
    build_post_test_submission_template,
    export_carla_stage_judge_calibration_review_bundle,
    export_carla_stage_platform_review_bundle,
    export_query_review_bundle,
    finalize_carla_stage_judge_calibration_review,
    finalize_carla_stage_platform_review,
    finalize_post_test_audit,
    finalize_query_review_bundle,
    refresh_human_work_inventory,
)
from bus_benchmark.jsonio import read_json, write_json, write_jsonl


def _load(path):
    return read_json(Path(path))


def _write_bundle(path, value):
    target = Path(path).resolve()
    write_json(target, value)
    print(json.dumps({"output": str(target)}, ensure_ascii=False, sort_keys=True))


def _query_export(args):
    _write_bundle(
        args.output,
        export_query_review_bundle(
            Path(args.library),
            Path(args.oracle_draft),
            reviewer_id=args.reviewer,
        ),
    )


def _query_finalize(args):
    result = finalize_query_review_bundle(
        _load(args.bundle),
        _load(args.submission),
        library_source=Path(args.library),
        oracle_source=Path(args.oracle_draft),
    )
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "confirmed_oracle.jsonl", result["confirmed_oracles"])
    write_jsonl(
        output / "human_query_gold.jsonl", result["human_gold_records"]
    )
    summary = {
        key: value
        for key, value in result.items()
        if key not in ("confirmed_oracles", "human_gold_records")
    }
    summary["record_count"] = len(result["confirmed_oracles"])
    write_json(output / "finalization_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def _carla_platform_export(args):
    bundle = export_carla_stage_platform_review_bundle(
        Path(args.ontology),
        Path(args.semantic_fixtures),
        Path(args.carla_capability),
        reviewer_id=args.reviewer,
    )
    _write_bundle(args.output, bundle)


def _carla_platform_finalize(args):
    _write_bundle(
        args.output,
        finalize_carla_stage_platform_review(
            _load(args.bundle),
            _load(args.submission),
            ontology_source=Path(args.ontology),
            semantic_fixture_source=Path(args.semantic_fixtures),
            carla_capability_source=Path(args.carla_capability),
        ),
    )


def _carla_calibration_subjects(args):
    records = build_carla_stage_calibration_reviewer_subjects(
        Path(args.roster),
        Path(args.library),
        Path(args.oracle),
        Path(args.responses),
        Path(args.evidence),
    )
    write_jsonl(Path(args.output).resolve(), records)
    print(json.dumps({"output": str(Path(args.output).resolve()), "records": len(records)}))


def _carla_calibration_export(args):
    _write_bundle(
        args.output,
        export_carla_stage_judge_calibration_review_bundle(
            Path(args.roster),
            Path(args.reviewer_subjects),
            reviewer_id=args.reviewer,
        ),
    )


def _carla_calibration_finalize(args):
    _write_bundle(
        args.output,
        finalize_carla_stage_judge_calibration_review(
            _load(args.bundle),
            _load(args.submission),
            calibration_roster_source=Path(args.roster),
            reviewer_subject_source=Path(args.reviewer_subjects),
        ),
    )


def _inventory_refresh(args):
    value = refresh_human_work_inventory(
        _load(args.inventory),
        query_bundles=[_load(path) for path in args.query_bundle],
        platform_bundle=_load(args.platform_bundle),
        calibration_bundle=_load(args.calibration_bundle),
        protocol_bundle=None,
        profile=args.profile,
        platform_sources={
            "ontology": Path(args.ontology) if args.ontology else None,
            "semantic_fixtures": (
                Path(args.semantic_fixtures) if args.semantic_fixtures else None
            ),
            "carla_capability": (
                Path(args.carla_capability) if args.carla_capability else None
            ),
        },
        calibration_sources={
            "roster": Path(args.roster) if args.roster else None,
            "reviewer_subjects": (
                Path(args.reviewer_subjects) if args.reviewer_subjects else None
            ),
        },
    )
    _write_bundle(args.output, value)


def _post_test_export(args):
    bundle = build_method_blind_post_test_bundle(
        Path(args.population), rate=args.rate, seed=args.seed
    )
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "public_tasks.json", bundle["public_tasks"])
    write_json(output / "private_linkage.json", bundle["private_linkage"])
    write_json(
        output / "submission_template.json",
        build_post_test_submission_template(bundle["public_tasks"], args.reviewer),
    )
    print(
        json.dumps(
            {"output_dir": str(output), "selected": bundle["public_tasks"]["selected"]},
            sort_keys=True,
        )
    )


def _post_test_finalize(args):
    _write_bundle(
        args.output,
        finalize_post_test_audit(
            _load(args.public_tasks),
            _load(args.private_linkage),
            _load(args.submission),
            Path(args.population),
        ),
    )


def _finalization_inputs(parser):
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--submission", required=True)


def main():
    parser = argparse.ArgumentParser(
        description="Source-bound production workflow for benchmark human decisions"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("query-export")
    command.add_argument("--library", required=True)
    command.add_argument("--oracle-draft", required=True)
    command.add_argument("--reviewer", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(function=_query_export)

    command = commands.add_parser("query-finalize")
    _finalization_inputs(command)
    command.add_argument("--library", required=True)
    command.add_argument("--oracle-draft", required=True)
    command.add_argument("--output-dir", required=True)
    command.set_defaults(function=_query_finalize)

    command = commands.add_parser("carla-platform-export")
    command.add_argument("--ontology", required=True)
    command.add_argument("--semantic-fixtures", required=True)
    command.add_argument("--carla-capability", required=True)
    command.add_argument("--reviewer", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(function=_carla_platform_export)

    command = commands.add_parser("carla-platform-finalize")
    _finalization_inputs(command)
    command.add_argument("--ontology", required=True)
    command.add_argument("--semantic-fixtures", required=True)
    command.add_argument("--carla-capability", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(function=_carla_platform_finalize)

    command = commands.add_parser("carla-calibration-subjects")
    command.add_argument("--roster", required=True)
    command.add_argument("--library", required=True)
    command.add_argument("--oracle", required=True)
    command.add_argument("--responses", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(function=_carla_calibration_subjects)

    command = commands.add_parser("carla-calibration-export")
    command.add_argument("--roster", required=True)
    command.add_argument("--reviewer-subjects", required=True)
    command.add_argument("--reviewer", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(function=_carla_calibration_export)

    command = commands.add_parser("carla-calibration-finalize")
    _finalization_inputs(command)
    command.add_argument("--roster", required=True)
    command.add_argument("--reviewer-subjects", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(function=_carla_calibration_finalize)

    command = commands.add_parser("inventory-refresh")
    command.add_argument("--inventory", required=True)
    command.add_argument("--query-bundle", action="append", required=True)
    command.add_argument("--platform-bundle", required=True)
    command.add_argument("--calibration-bundle", required=True)
    command.add_argument("--ontology")
    command.add_argument("--semantic-fixtures")
    command.add_argument("--carla-capability")
    command.add_argument("--roster")
    command.add_argument("--reviewer-subjects")
    command.add_argument(
        "--profile", choices=("carla_stage", "partial_dev"), default="carla_stage"
    )
    command.add_argument("--output", required=True)
    command.set_defaults(function=_inventory_refresh)

    command = commands.add_parser("post-test-export")
    command.add_argument("--population", required=True)
    command.add_argument("--reviewer", required=True)
    command.add_argument("--rate", type=float, default=0.10)
    command.add_argument("--seed", type=int, default=0)
    command.add_argument("--output-dir", required=True)
    command.set_defaults(function=_post_test_export)

    command = commands.add_parser("post-test-finalize")
    command.add_argument("--public-tasks", required=True)
    command.add_argument("--private-linkage", required=True)
    command.add_argument("--submission", required=True)
    command.add_argument("--population", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(function=_post_test_finalize)

    args = parser.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
