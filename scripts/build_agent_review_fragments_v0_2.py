#!/usr/bin/env python3
"""Build conservative, source-bound v0.2 machine review fragments.

The fragments are drafting aids only.  They deliberately route semantics that
changed in v0.2 (external events, scoped rule hooks, temporal structure, and
risk metadata) to the single human reviewer instead of copying v0.1 reaction
assumptions into the new oracle.
"""

from __future__ import print_function

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRAFT_DIR = ROOT / "benchmark_artifacts" / "drafts"
FRAGMENT_DIR = DRAFT_DIR / "agent_review_fragments"
DEV_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
TEST_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_query_library_v0_2.jsonl"
DEV_ORACLE = DRAFT_DIR / "dev_oracle_draft.jsonl"
TEST_ORACLE = DRAFT_DIR / "test_oracle_draft.jsonl"

OUTPUTS = (
    ("dev_intents.json", "development", "dev"),
    ("test_A_C_intents.json", "test", "test_A_C"),
    ("test_D_G_intents.json", "test", "test_D_G"),
)

CAUTION_PREDICATES = {
    "actor_role_count": (
        "Counterpart cardinality is a scored requirement only when the surface text supports it.",
        "Confirm the count and polarity independently for this surface.",
    ),
    "actor_relative_region": (
        "The relative region is a heuristic projection from the counterpart role.",
        "Confirm or revise the region using the current v0.2 surface text.",
    ),
    "event_spec": (
        "In v0.2 interaction events may describe only counterpart, environment, signal, or policy-independent trigger behavior; ego reaction is excluded.",
        "Confirm the actor binding and reject any inferred ego reaction or terminal policy outcome.",
    ),
    "temporal_structure": (
        "The v0.2 temporal structure orders external interaction events, not ego-policy reactions.",
        "Confirm that every ordered stage is external and expressed by the surface.",
    ),
    "before": (
        "Temporal edges in v0.2 must connect external events only.",
        "Confirm both endpoints and remove any edge that implicitly specifies ego reaction.",
    ),
    "parallel_group": (
        "Parallel groups in v0.2 cover simultaneous external events only.",
        "Confirm the group membership without adding ego-policy behavior.",
    ),
    "rule_hook": (
        "v0.2 rule hooks have explicit scene, counterpart-rule, and counterpart-violation scopes.",
        "Confirm the preserved scope; do not reinterpret a counterpart rule as an ego reaction requirement.",
    ),
    "risk_level": (
        "Risk is external exposure metadata in v0.2, not a realized ego-policy outcome.",
        "Confirm whether the surface makes it scoreable or whether it should remain permitted metadata.",
    ),
    "surface_numeric_constraint": (
        "Numeric constraints are surface-specific and must not leak from the precise form into partial or vague forms.",
        "Confirm the value and layer for this surface.",
    ),
}


def _read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _relative(path):
    return str(path.resolve().relative_to(ROOT))


def _finding(scope, target_type, target, recommendation, reason, suggested_change):
    return {
        "scope": scope,
        "target_type": target_type,
        "target": target,
        "recommendation": recommendation,
        "reason": reason,
        "suggested_change": suggested_change,
    }


def _required_findings(expected_support):
    return [
        _finding(
            "all_surfaces",
            "required_check",
            "support_and_response_disposition",
            "accept",
            "The v0.2 source explicitly declares {} support and its acceptable response; this is a machine projection, not human gold.".format(expected_support),
            "Verify the declared support boundary and response disposition before finalizing the single human gold.",
        ),
        _finding(
            "all_surfaces",
            "required_check",
            "cardinality_and_polarity",
            "human_judgment",
            "Cardinality and polarity must be justified by each current surface rather than inherited from v0.1.",
            "Review the counterpart actors and any explicit absence or minimum-count language for all three surfaces.",
        ),
        _finding(
            "all_surfaces",
            "required_check",
            "road_and_spatial_decomposition",
            "human_judgment",
            "Road and spatial metadata can contain implementation detail not stated by partial or vague text.",
            "Separate required, permitted, and forbidden road or spatial detail per surface.",
        ),
        _finding(
            "all_surfaces",
            "required_check",
            "event_graph_and_actor_binding",
            "human_judgment",
            "v0.2 intentionally removes ego reaction from query and event requirements; only external interaction events remain.",
            "Confirm every event actor and temporal edge, and leave reactive ego behavior to the ego policy.",
        ),
        _finding(
            "all_surfaces",
            "required_check",
            "surface_layering",
            "human_judgment",
            "The precise, partial, and vague surfaces share an intent but do not necessarily state the same implementation detail.",
            "Confirm core-required, surface-required, permitted, and forbidden layers without canonical-query leakage.",
        ),
        _finding(
            "all_surfaces",
            "cpd_policy",
            "cpd_policy",
            "human_judgment",
            "CPD eligibility must use only frozen platform-invariant dimensions and an unambiguous target.",
            "Confirm eligibility and selector uniqueness; keep precise surfaces ineligible for rewarded diversity.",
        ),
    ]


def _build_intent_review(query_rows, oracle_rows):
    by_style_query = {row["surface_style"]: row for row in query_rows}
    by_style_oracle = {row["surface_style"]: row for row in oracle_rows}
    if set(by_style_query) != {"precise", "partial", "vague"}:
        raise ValueError("query intent is not a complete surface triplet")
    if set(by_style_oracle) != set(by_style_query):
        raise ValueError("oracle intent does not match its query triplet")
    intent_id = query_rows[0]["intent_group_id"]
    if any(row["intent_group_id"] != intent_id for row in query_rows + oracle_rows):
        raise ValueError("intent group mismatch")
    findings = _required_findings(query_rows[0]["expected_support"])
    predicates_by_style = {
        style: {atom["predicate"] for atom in by_style_oracle[style]["atoms"]}
        for style in ("precise", "partial", "vague")
    }
    for predicate in sorted(CAUTION_PREDICATES):
        styles = [
            style
            for style in ("precise", "partial", "vague")
            if predicate in predicates_by_style[style]
        ]
        if not styles:
            continue
        reason, suggested_change = CAUTION_PREDICATES[predicate]
        scopes = ["all_surfaces"] if len(styles) == 3 else styles
        for scope in scopes:
            findings.append(
                _finding(
                    scope,
                    "atom_predicate",
                    predicate,
                    "human_judgment",
                    reason,
                    suggested_change,
                )
            )
    return {
        "intent_group_id": intent_id,
        "query_ids": sorted(row["query_id"] for row in query_rows),
        "overall_disposition": "accept_with_attention",
        "confidence": "medium",
        "semantic_findings": findings,
    }


def _load_reviews(library_path, oracle_path):
    queries = _read_jsonl(library_path)
    oracles = _read_jsonl(oracle_path)
    by_query = {row["query_id"]: row for row in queries}
    oracle_by_query = {row["query_id"]: row for row in oracles}
    if len(by_query) != len(queries) or set(by_query) != set(oracle_by_query):
        raise ValueError("library/oracle query IDs are not an exact one-to-one match")
    query_groups = defaultdict(list)
    oracle_groups = defaultdict(list)
    for row in queries:
        query_groups[row["intent_group_id"]].append(row)
    for row in oracles:
        oracle_groups[row["intent_group_id"]].append(row)
    if set(query_groups) != set(oracle_groups):
        raise ValueError("library/oracle intent IDs do not match")
    return {
        intent_id: _build_intent_review(query_groups[intent_id], oracle_groups[intent_id])
        for intent_id in sorted(query_groups)
    }


def _fragment(split, library_path, oracle_path, reviews, intent_range):
    counts = Counter(review["overall_disposition"] for review in reviews)
    return {
        "schema_version": "0.1",
        "artifact_type": "agent_semantic_review_fragment",
        "agent_generated": True,
        "human_gold": False,
        "scope": {
            "split": split,
            "library_path": _relative(library_path),
            "oracle_path": _relative(oracle_path),
            "intent_range": intent_range,
        },
        "intent_reviews": reviews,
        "summary": {
            "intent_count": len(reviews),
            "disposition_counts": {
                "accept": counts.get("accept", 0),
                "accept_with_attention": counts.get("accept_with_attention", 0),
                "revise": counts.get("revise", 0),
            },
        },
    }


def build_fragments():
    dev_reviews = _load_reviews(DEV_LIBRARY, DEV_ORACLE)
    test_reviews = _load_reviews(TEST_LIBRARY, TEST_ORACLE)
    dev = [dev_reviews[key] for key in sorted(dev_reviews)]
    test_a_c = [
        test_reviews[key]
        for key in sorted(test_reviews)
        if key.rsplit("_", 1)[-1][0] in "ABC"
    ]
    test_d_g = [
        test_reviews[key]
        for key in sorted(test_reviews)
        if key.rsplit("_", 1)[-1][0] in "DEFG"
    ]
    if (len(dev), len(test_a_c), len(test_d_g)) != (16, 40, 44):
        raise ValueError("unexpected v0.2 intent partition")
    return {
        "dev_intents.json": _fragment(
            "development", DEV_LIBRARY, DEV_ORACLE, dev, "all 16 BSG_DEV_v0_2 intents"
        ),
        "test_A_C_intents.json": _fragment(
            "test", TEST_LIBRARY, TEST_ORACLE, test_a_c, "BSG_v0_2 A01-C14"
        ),
        "test_D_G_intents.json": _fragment(
            "test", TEST_LIBRARY, TEST_ORACLE, test_d_g, "BSG_v0_2 D01-G08"
        ),
    }


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    fragments = build_fragments()
    mismatches = []
    if not args.check:
        FRAGMENT_DIR.mkdir(parents=True, exist_ok=True)
    for name, value in fragments.items():
        path = FRAGMENT_DIR / name
        expected = _bytes(value)
        if args.check:
            if not path.is_file() or path.read_bytes() != expected:
                mismatches.append(name)
        else:
            path.write_bytes(expected)
    if mismatches:
        raise SystemExit("stale v0.2 agent review fragments: {}".format(", ".join(mismatches)))
    print(json.dumps({"fragments": 3, "intents": 100, "status": "verified" if args.check else "built"}, sort_keys=True))


if __name__ == "__main__":
    main()
