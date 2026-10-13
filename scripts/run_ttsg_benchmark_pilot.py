#!/usr/bin/env python3
"""Run one complete declared development generation grid through the common harness."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark/src"))
from dotenv import dotenv_values
from bus_benchmark.generation import (
    GenerationRunner,
    validate_generation_response_chain,
)
from bus_benchmark.jsonio import read_json, write_json, write_jsonl


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--api-env", type=Path, default=ROOT / ".env")
    p.add_argument("--resume", action="store_true")
    a = p.parse_args()
    values = dotenv_values(a.api_env, interpolate=False)
    for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
        if not values.get(k):
            raise ValueError("required API configuration field missing: " + k)
        os.environ[k] = values[k]
    config = a.config_dir
    roster = read_json(config / "roster.json")
    method = read_json(config / "method.json")
    model = method["adapter"]["argv"][
        method["adapter"]["argv"].index("--model-name") + 1
    ]
    if model != values.get("OPENAI_MODEL"):
        raise ValueError("model differs from prepared profile")
    output = a.output.resolve()
    output.mkdir(parents=True, exist_ok=a.resume)
    runner = GenerationRunner(
        Path(roster["library_path"]),
        roster,
        config / "method.json",
        output / "generation",
        development=True,
        resume=a.resume,
        retain_workspaces=True,
    )
    try:
        responses = runner.run()
    finally:
        ledger = output / "retained-native-workspaces.json"
        previous = read_json(ledger)["paths"] if ledger.exists() else []
        write_json(
            ledger,
            {
                "development_only": True,
                "paths": sorted(set(previous + runner.retained_workspaces)),
            },
        )
    write_jsonl(output / "generation/response.jsonl", responses)
    write_jsonl(
        output / "generation/evidence.jsonl", runner.evidence_records(responses)
    )
    chain = validate_generation_response_chain(
        Path(roster["library_path"]),
        roster,
        config / "method.json",
        output / "generation",
        development=True,
    )
    result = {
        "development_only": True,
        "method_id": roster["method_id"],
        "model": model,
        "response_count": len(responses),
        "terminal_statuses": [r["terminal_status"] for r in responses],
        "dispositions": [r["disposition"] for r in responses],
        "generation_chain": chain,
    }
    write_json(output / "summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
