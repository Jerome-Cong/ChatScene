"""Run a complete public-method development grid, loading credentials outside the method bundle."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark/src"))
from dotenv import dotenv_values
from bus_benchmark.generation import GenerationRunner, validate_generation_response_chain
from bus_benchmark.jsonio import read_json, write_json, write_jsonl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-env", type=Path, default=ROOT / ".env")
    parser.add_argument("--embedding-env", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    config = args.config_dir.resolve()
    method = read_json(config / "method.json")
    roster = read_json(config / "roster.json")
    values = dotenv_values(args.api_env, interpolate=False)
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
        if not values.get(key):
            raise ValueError("missing generation API configuration field: " + key)
        os.environ[key] = values[key]
    argv = method["adapter"]["argv"]
    if argv[argv.index("--model-name") + 1] != values.get("OPENAI_MODEL"):
        raise ValueError("generation model differs from the prepared profile")
    if "--embedding-model" in argv:
        if args.embedding_env is None:
            raise ValueError("Text2Scenario requires --embedding-env at the harness boundary")
        embedding = dotenv_values(args.embedding_env, interpolate=False)
        if argv[argv.index("--embedding-model") + 1] != embedding.get("OPENAI_MODEL"):
            raise ValueError("embedding model differs from the prepared profile")
        for source, target in (("OPENAI_API_KEY", "EMBEDDING_API_KEY"), ("OPENAI_BASE_URL", "EMBEDDING_BASE_URL")):
            if not embedding.get(source):
                raise ValueError("missing embedding API configuration field: " + source)
            os.environ[target] = embedding[source]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=args.resume)
    runner = GenerationRunner(Path(roster["library_path"]), roster, config / "method.json", output / "generation", development=True,
                              resume=args.resume, retain_workspaces=True)
    try:
        responses = runner.run()
    finally:
        ledger = output / "retained-native-workspaces.json"
        previous = read_json(ledger)["paths"] if ledger.exists() else []
        write_json(ledger, {"development_only": True, "paths": sorted(set(previous + runner.retained_workspaces))})
    write_jsonl(output / "generation/response.jsonl", responses)
    write_jsonl(output / "generation/evidence.jsonl", runner.evidence_records(responses))
    chain = validate_generation_response_chain(Path(roster["library_path"]), roster, config / "method.json", output / "generation", development=True)
    report = {"development_only": True, "method_id": method["method_id"], "response_count": len(responses),
              "terminal_statuses": [r["terminal_status"] for r in responses], "generation_chain": chain}
    write_json(output / "summary.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
