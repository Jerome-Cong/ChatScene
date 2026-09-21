#!/usr/bin/env python3
"""Build or verify the source-bound machine-only query semantic-review draft."""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bus_benchmark.agent_query_review import (
    DEFAULT_AGENT_REVIEW_FRAGMENT_PATHS,
    build_agent_query_review_draft,
    validate_agent_query_review_draft,
)
from bus_benchmark.jsonio import read_json, sha256_file, write_json


DEFAULT_OUTPUT = (
    ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "query_agent_semantic_review_v0_2.json"
)
QUERY_SOURCES = (
    (
        "development",
        ROOT / "query_lib" / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl",
        ROOT / "benchmark_artifacts" / "drafts" / "dev_oracle_draft.jsonl",
    ),
    (
        "test",
        ROOT / "query_lib" / "bus_ego_topdown_2d_query_library_v0_2.jsonl",
        ROOT / "benchmark_artifacts" / "drafts" / "test_oracle_draft.jsonl",
    ),
)
FRAGMENTS = DEFAULT_AGENT_REVIEW_FRAGMENT_PATHS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="validate the existing output against every current source",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if args.verify_only:
        value = read_json(output)
        validate_agent_query_review_draft(
            value,
            query_sources=QUERY_SOURCES,
            fragment_paths=FRAGMENTS,
        )
    else:
        value = build_agent_query_review_draft(QUERY_SOURCES, FRAGMENTS)
        write_json(output, value)
        # Re-read the exact bytes that will be handed to the human reviewer.
        value = read_json(output)
        validate_agent_query_review_draft(
            value,
            query_sources=QUERY_SOURCES,
            fragment_paths=FRAGMENTS,
        )
    print(
        json.dumps(
            {
                "status": "verified" if args.verify_only else "built_and_verified",
                "output": str(output),
                "sha256": sha256_file(output),
                "artifact_id": value["artifact_id"],
                "summary": value["summary"],
                "human_gold": value["human_gold"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
