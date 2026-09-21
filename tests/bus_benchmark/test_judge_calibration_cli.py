import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from bus_benchmark.cli import main
from bus_benchmark.errors import ValidationError
from bus_benchmark.human_workflow import (
    build_carla_stage_calibration_reviewer_subjects,
    export_carla_stage_judge_calibration_review_bundle,
    finalize_carla_stage_judge_calibration_review,
)
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    write_json,
    write_jsonl,
)
from bus_benchmark.judge_pipeline import (
    validate_judge_calibration_request_bundle,
    validate_judge_calibration_run_bundle,
)
from tests.bus_benchmark.formal_freeze_fixture import DEV_LIBRARY, build_formal_bundle


class JudgeCalibrationCliTests(unittest.TestCase):
    def test_carla_stage_finalizer_gold_runs_directly_through_90_item_cli_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_root = root / "fixture"
            fixture_root.mkdir()
            _, paths = build_formal_bundle(fixture_root)

            final_roster = read_json(paths["judge_calibration_roster"])
            stage_items = [
                copy.deepcopy(item)
                for item in final_roster["items"]
                if item["platform"] == "carla"
            ]
            self.assertEqual(len(stage_items), 90)
            stage_roster = {
                "schema_version": "0.1",
                "decision_status": "stage_confirmed",
                "stage_scope": "carla_first",
                "platform": "carla",
                "formal_freeze_eligible": False,
                "selection_algorithm": "query_category_then_sha256_v0.1",
                "seed": "bus-judge-calibration-v0.1",
                "eligible_population_count": final_roster[
                    "eligible_population_count_by_platform"
                ]["carla"],
                "eligible_population_sha256": final_roster[
                    "eligible_population_sha256"
                ],
                "items_sha256": sha256_bytes(canonical_json_bytes(stage_items)),
                "items": stage_items,
            }
            stage_roster_path = root / "carla_stage_roster.json"
            write_json(stage_roster_path, stage_roster)
            subjects = build_carla_stage_calibration_reviewer_subjects(
                stage_roster_path,
                DEV_LIBRARY,
                paths["requirement_oracle_dev"],
                paths["development_response_records"],
                paths["development_evidence_records"],
            )
            subjects_path = root / "carla_stage_reviewer_subjects.jsonl"
            write_jsonl(subjects_path, subjects)
            review_bundle = export_carla_stage_judge_calibration_review_bundle(
                stage_roster_path,
                subjects_path,
                reviewer_id="carla-stage-reviewer",
            )
            submission = copy.deepcopy(
                review_bundle["reviewer_packet"]["submission_template"]
            )
            submission["submission_status"] = "complete"
            for item in submission["responses"]:
                verdict = ("satisfied", "violated", "unknown")[
                    int(item["subject_id"][:8], 16) % 3
                ]
                item["response"] = {
                    "verdict": verdict,
                    "evidence": ["single human-observed CARLA evidence"],
                    "rationale": "single blinded human-gold fixture",
                }
            stage_report = finalize_carla_stage_judge_calibration_review(
                review_bundle,
                submission,
                calibration_roster_source=stage_roster_path,
                reviewer_subject_source=subjects_path,
            )
            stage_gold_path = root / "carla_stage_finalizer_gold.json"
            write_json(stage_gold_path, stage_report)

            final_context = read_json(paths["judge_calibration_context_manifest"])
            inference_path = root / "inference_config.json"
            write_json(inference_path, final_context["inference_config"])
            context_path = root / "carla_stage_context.json"
            context_stdout = io.StringIO()
            with contextlib.redirect_stdout(context_stdout):
                self.assertEqual(
                    main(
                        [
                            "judge-calibration-context",
                            "--profile",
                            "carla_stage",
                            "--query-library",
                            str(DEV_LIBRARY),
                            "--oracle",
                            str(paths["requirement_oracle_dev"]),
                            "--roster",
                            str(stage_roster_path),
                            "--human-gold",
                            str(stage_gold_path),
                            "--prompt",
                            final_context["prompt"]["path"],
                            "--output-schema",
                            final_context["output_schema"]["path"],
                            "--runner-manifest",
                            str(paths["judge_runner_manifest"]),
                            "--model-id",
                            final_context["model_id"],
                            "--inference-config",
                            str(inference_path),
                            "--carla-source-config",
                            str(paths["judge_calibration_source_config"]),
                            "--carla-roster",
                            str(paths["development_query_roster"]),
                            "--carla-responses",
                            str(paths["development_response_records"]),
                            "--carla-evidence",
                            str(paths["development_evidence_records"]),
                            "--output",
                            str(context_path),
                        ]
                    ),
                    0,
                )
            context = read_json(context_path)
            self.assertEqual(context["calibration_profile"], "carla_stage")
            self.assertFalse(context["formal_freeze_eligible"])
            self.assertEqual(
                Path(context["calibration_human_gold"]["path"]),
                stage_gold_path.resolve(),
            )
            self.assertEqual(
                context["calibration_human_gold"]["sha256"],
                sha256_file(stage_gold_path),
            )

            request_directory = root / "carla_stage_requests"
            run_directory = root / "carla_stage_run"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "judge-calibration-request",
                            "--context",
                            str(context_path),
                            "--output-dir",
                            str(request_directory),
                        ]
                    ),
                    0,
                )
            run_stdout = io.StringIO()
            with contextlib.redirect_stdout(run_stdout):
                self.assertEqual(
                    main(
                        [
                            "judge-calibration-run",
                            "--context",
                            str(context_path),
                            "--request-dir",
                            str(request_directory),
                            "--output-dir",
                            str(run_directory),
                        ]
                    ),
                    0,
                )
            run_result = json.loads(run_stdout.getvalue())
            self.assertEqual(run_result["response_count"], 90)
            self.assertEqual(run_result["calibration_profile"], "carla_stage")
            self.assertFalse(run_result["formal_freeze_eligible"])
            self.assertEqual(run_result["diagnostics"]["record_count"], 90)
            self.assertEqual(run_result["diagnostics"]["macro_f1"], 1.0)
            self.assertEqual(run_result["diagnostics"]["cohen_kappa"], 1.0)
            manifest, responses = validate_judge_calibration_run_bundle(
                run_directory,
                context_path,
                request_directory=request_directory,
            )
            self.assertEqual(manifest["response_count"], 90)
            self.assertEqual(len(responses), 90)

    def test_confirmed_180_item_cli_chain_and_raw_response_attack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_root = root / "fixture"
            fixture_root.mkdir()
            _, paths = build_formal_bundle(fixture_root)
            context_path = paths["judge_calibration_context_manifest"]
            request_directory = root / "cli_requests"
            run_directory = root / "cli_run"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "judge-calibration-request",
                            "--context",
                            str(context_path),
                            "--output-dir",
                            str(request_directory),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "judge-calibration-run",
                            "--context",
                            str(context_path),
                            "--request-dir",
                            str(request_directory),
                            "--output-dir",
                            str(run_directory),
                        ]
                    ),
                    0,
                )
            manifest, responses = validate_judge_calibration_run_bundle(
                run_directory,
                context_path,
                request_directory=request_directory,
            )
            self.assertEqual(manifest["response_count"], 180)
            self.assertEqual(len(responses), 180)

            response_path = run_directory / "judge_responses.jsonl"
            forged = read_jsonl(response_path)
            forged[0]["raw_response"] = forged[1]["raw_response"]
            forged[0]["raw_response_sha256"] = forged[1]["raw_response_sha256"]
            write_jsonl(response_path, forged)
            with self.assertRaisesRegex(ValidationError, "raw execution"):
                validate_judge_calibration_run_bundle(
                    run_directory,
                    context_path,
                    request_directory=request_directory,
                )

            (request_directory / "unexpected.txt").write_text(
                "not allowlisted\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "exact allowlist"):
                validate_judge_calibration_request_bundle(
                    request_directory, context_path
                )

            context = read_json(context_path)
            gold_path = Path(context["calibration_human_gold"]["path"])
            gold = read_jsonl(gold_path)
            gold[0]["predicted_verdict"] = "satisfied"
            write_jsonl(gold_path, gold)
            with self.assertRaises(ValidationError):
                validate_judge_calibration_run_bundle(
                    run_directory,
                    context_path,
                    request_directory=request_directory,
                )


if __name__ == "__main__":
    unittest.main()
