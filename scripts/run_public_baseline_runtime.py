#!/usr/bin/env python3
"""Run and reload a full public-method development runtime grid without scores."""

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "benchmark/src"), str(ROOT / "scripts")]
from public_baseline_extractor import PROFILE as OBSERVER_PROFILE
from public_baseline_carla_session import OwnedCarlaSession

from bus_benchmark.freeze import build_extractor_input
from bus_benchmark.adapters.chatscene import _run_process
from bus_benchmark.generation import validate_generation_response_chain
from bus_benchmark.jsonio import read_json, read_jsonl, write_json, write_jsonl, sha256_file
from bus_benchmark.runtime import (
    execute_runtime_record,
    validate_platform_runtime_config,
    validate_runtime_record_consistency,
    _safe_run_key,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--runtime-config-dir", type=Path, required=True)
    parser.add_argument("--generation-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--isolate-carla", action="store_true", help="start and stop a separate owned CARLA server for each response")
    parser.add_argument("--graphics-adapter", type=int, help="explicit Vulkan adapter index for the authorized GPU")
    args = parser.parse_args()
    if args.isolate_carla and (args.graphics_adapter is None or args.graphics_adapter < 0 or args.resume):
        parser.error("--isolate-carla requires --graphics-adapter and a fresh output; resume never starts services")
    configuration = args.config_dir.resolve()
    runtime_configuration = args.runtime_config_dir.resolve()
    generation = args.generation_output.resolve() / "generation"
    roster = read_json(configuration / "roster.json")
    responses = read_jsonl(generation / "response.jsonl")
    chain = validate_generation_response_chain(
        Path(roster["library_path"]), roster, configuration / "method.json",
        generation, development=True,
    )
    chain["verified"] = True
    platform = read_json(runtime_configuration / "platform.json")
    worker = validate_platform_runtime_config(platform, "carla", allow_draft=True)
    controller = read_json(runtime_configuration / "controller.json")
    dependencies = platform["runtime_dependencies"]
    context = {"interpreter": platform["worker"]["interpreter"],
               "scenic_compile_options": dependencies.get("scenic_compile_options")}
    maps = [{"kind": "opendrive", **binding} for binding in dependencies["map_catalog"]]
    entrypoint = ROOT / "scripts/public_baseline_extractor.py"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=args.resume)
    receipt_paths = list((output / "carla-sessions").glob("*/session.json"))
    receipts = []
    for path in receipt_paths:
        try:
            receipts.append(read_json(path))
        except Exception:
            # The per-response check below records corrupt receipts without
            # dropping later responses or inventing successful cleanup.
            pass
    if any(r.get("scope") != "one-process-per-response" for r in receipts):
        raise ValueError("unrecognized owned CARLA session receipt")
    previous = read_json(output / "summary.json") if args.resume and (output / "summary.json").exists() else {}
    isolated = previous.get("carla_process_isolation", args.isolate_carla or bool(receipts))
    if args.resume and isolated and not receipt_paths:
        raise ValueError("isolated resume is missing its owned session receipts")
    if args.resume and receipts and isolated is not True:
        raise ValueError("owned session receipts conflict with the original isolation declaration")
    prior_outcomes = read_jsonl(output / "grid-outcomes.jsonl") if args.resume and (output / "grid-outcomes.jsonl").exists() else previous.get("infrastructure_errors", [])
    prior_errors = {o["run_id"]: o for o in prior_outcomes if o.get("status") == "infrastructure_error"}
    records, diagnostics, outcomes = [], [], []
    for response in responses:
        print("Runtime starting repetition {}".format(response["repetition"]), flush=True)
        session = OwnedCarlaSession(platform, output / "carla-sessions" / str(response["repetition"]), args.graphics_adapter) if args.isolate_carla and response["terminal_status"] == "complete" else nullcontext()
        record = None
        outcome = {"run_id": response["run_id"], "repetition": response["repetition"],
                   "status": "runtime_record_saved", "lifecycle_error": None}
        try:
            if args.resume and not (output / "runtime" / _safe_run_key(response["run_id"]) / "runtime_record.json").is_file():
                raise ValueError("resume refused a missing canonical runtime record; no new worker or service was started")
            with session:
                record = execute_runtime_record(
                    response, output / "runtime", worker, controller, chain,
                    resume=args.resume, allow_draft_controller=True, allow_draft_runtime=True,
                )
        except Exception as exc:
            outcome.update(status="infrastructure_error", lifecycle_error=str(exc), error_type=type(exc).__name__)
        if args.resume and response["run_id"] in prior_errors:
            outcome.update(status="infrastructure_error", lifecycle_error=prior_errors[response["run_id"]].get("lifecycle_error"),
                           error_type=prior_errors[response["run_id"]].get("error_type"))
        receipt_path = output / "carla-sessions" / str(response["repetition"]) / "session.json"
        if isolated and response["terminal_status"] == "complete":
            try:
                receipt = read_json(receipt_path) if receipt_path.is_file() else {}
                if receipt.get("process_stopped") is not True or receipt.get("cleanup_error"):
                    outcome.update(status="infrastructure_error", lifecycle_error=receipt.get("cleanup_error") or "owned session has no verified stopped receipt")
            except Exception as exc:
                outcome.update(status="infrastructure_error", lifecycle_error="owned session receipt could not be validated: " + str(exc), error_type=type(exc).__name__)
        if record is not None:
            records.append(record)
        outcome["runtime_record_saved"] = record is not None
        outcomes.append(outcome)
        diagnostic = {
            "development_only": True, "run_id": response["run_id"],
            "repetition": response["repetition"], "terminal_status": response["terminal_status"],
            "observation_scope": "native_initial_states_only",
            "extractor_entrypoint_sha256": sha256_file(entrypoint),
            "runtime_config_sha256": sha256_file(runtime_configuration / "platform.json"),
            "observation_resource_profile": dict(OBSERVER_PROFILE),
            "status": "not_applicable", "semantic_projection": None,
        }
        if response["terminal_status"] == "complete" and response.get("artifact"):
            try:
                payload = build_extractor_input(response, artifact_format="scenic", trusted_dependencies=maps)
                argv = [sys.executable, "-I", str(entrypoint), "--scenic-python", context["interpreter"]]
                if context["scenic_compile_options"] == {"mode_2d": True, "use_2d_map": True}:
                    argv.append("--scenic-2d")
                work = output / "initial-observer-work" / str(response["repetition"])
                work.mkdir(parents=True, exist_ok=True)
                result = _run_process(argv, work, OBSERVER_PROFILE["wall_seconds"], ["PATH", "LANG", "LC_ALL"],
                                      json.dumps(payload, allow_nan=False).encode(),
                                      OBSERVER_PROFILE["max_stdout_bytes"], OBSERVER_PROFILE["max_stderr_bytes"])
                diagnostic["observer_exit_code"] = result.exit_code
                diagnostic["observer_timed_out"] = result.timed_out
                if result.error or result.exit_code != 0:
                    raise RuntimeError(result.error or result.stderr.decode("utf-8", errors="replace")[-1000:])
                observed = json.loads(result.stdout)
                if observed["resource_profile"] != OBSERVER_PROFILE:
                    raise ValueError("initial observer returned a different resource profile")
                diagnostic["semantic_projection"] = observed["projection"]
                diagnostic["status"] = "passed"
            except Exception as exc:
                diagnostic.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        diagnostics.append(diagnostic)
        write_jsonl(output / "runtime-records.jsonl", records)
        write_jsonl(output / "grid-outcomes.jsonl", outcomes)
        write_jsonl(output / "semantic-diagnostics.jsonl", diagnostics)
        print("Runtime finished repetition {}: compile={}, SV={}, NE={}, observation={}, infrastructure={}".format(
            response["repetition"], record["compile"]["status"] if record else "not_executed",
            [trial["status"] for trial in record["sv_trials"]] if record else [],
            record["ne"]["status"] if record else "not_executed", diagnostic["status"], outcome["status"],
        ), flush=True)
    for record in read_jsonl(output / "runtime-records.jsonl"):
        validate_runtime_record_consistency(record, allow_draft_controller=True)
    if len(outcomes) != roster["expected_response_count"]:
        raise ValueError("development response grid is incomplete")
    report = {
        "development_only": True, "formal_scores_produced": False,
        "judge_kernel_gate_retained": True, "record_count": len(records), "generation_chain": chain,
        "expected_response_count": roster["expected_response_count"], "grid_response_count": len(outcomes),
        "infrastructure_errors": [o for o in outcomes if o["status"] == "infrastructure_error"],
        "runtime_grid_complete": len(records) == len(outcomes) and all(o["status"] == "runtime_record_saved" for o in outcomes),
        "compile_statuses": [r["compile"]["status"] for r in records],
        "sv_statuses": [[s["status"] for s in r["sv_trials"]] for r in records],
        "ne_statuses": [r["ne"]["status"] for r in records],
        "initial_observation_statuses": [d["status"] for d in diagnostics],
        "initial_observation_resource_profile": dict(OBSERVER_PROFILE),
        "carla_process_isolation": isolated,
        "record_reload_validation": "passed" if records else "not_applicable",
    }
    write_json(output / "summary.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
