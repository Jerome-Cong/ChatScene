#!/usr/bin/env python3
"""Verify a complete TTSG development grid without running Judge or scoring."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark/src"))

from bus_benchmark import fixture_extractor_common
from bus_benchmark.generation import validate_generation_response_chain
from bus_benchmark.jsonio import read_json, read_jsonl, write_json, write_jsonl
from bus_benchmark.runtime import (
    execute_runtime_record,
    validate_platform_runtime_config,
    validate_runtime_record_consistency,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--generation-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    config = args.config_dir.resolve()
    generation = args.generation_output.resolve() / "generation"
    roster = read_json(config / "roster.json")
    responses = read_jsonl(generation / "response.jsonl")
    chain = validate_generation_response_chain(
        Path(roster["library_path"]),
        roster,
        config / "method.json",
        generation,
        development=True,
    )
    # Match the public runtime CLI: this marks the replayed generation chain,
    # while method/runtime configs and every output remain development-only.
    chain["verified"] = True
    worker = validate_platform_runtime_config(
        read_json(config / "platform.json"),
        "carla",
        allow_draft=True,
    )
    controller = read_json(config / "controller.json")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=args.resume)
    records, observations = [], []
    for response in responses:
        print(
            "Runtime starting repetition {}".format(response["repetition"]), flush=True
        )
        record = execute_runtime_record(
            response,
            output / "runtime",
            worker,
            controller,
            chain,
            resume=args.resume,
            allow_draft_controller=True,
            allow_draft_runtime=True,
        )
        records.append(record)
        diagnostic = {
            "development_only": True,
            "run_id": response["run_id"],
            "repetition": response["repetition"],
            "terminal_status": response["terminal_status"],
            "source_artifact_sha256": None,
            "semantic_projection": None,
        }
        artifact = response.get("artifact")
        if response["terminal_status"] == "complete" and artifact is not None:
            payload = {
                "schema_version": "0.1",
                "platform": "carla",
                "terminal_status": response["terminal_status"],
                "disposition": response["disposition"],
                "artifact": {
                    "text": Path(artifact["path"]).read_text(),
                    "dependencies": [],
                },
            }
            diagnostic["source_artifact_sha256"] = artifact["sha256"]
            diagnostic["semantic_projection"] = fixture_extractor_common.extract(
                payload, "carla"
            )
        observations.append(diagnostic)
        write_jsonl(output / "runtime-records.jsonl", records)
        write_jsonl(output / "semantic-diagnostics.jsonl", observations)
        print(
            "Runtime finished repetition {}: compile={}, SV={}, NE={}".format(
                response["repetition"],
                record["compile"]["status"],
                [trial["status"] for trial in record["sv_trials"]],
                record["ne"]["status"],
            ),
            flush=True,
        )

    # Reload the persisted records through the common validator, rather than
    # treating successful worker stdout as sufficient integration evidence.
    for record in read_jsonl(output / "runtime-records.jsonl"):
        validate_runtime_record_consistency(record, allow_draft_controller=True)
    if len(records) != roster["expected_response_count"]:
        raise ValueError("development runtime grid is incomplete")
    summary = {
        "development_only": True,
        "formal_scores_produced": False,
        "judge_kernel_gate_excluded_from_this_task": True,
        "record_count": len(records),
        "generation_chain": chain,
        "compile_statuses": [r["compile"]["status"] for r in records],
        "sv_statuses": [[s["status"] for s in r["sv_trials"]] for r in records],
        "ne_statuses": [r["ne"]["status"] for r in records],
        "record_reload_validation": "passed",
    }
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
