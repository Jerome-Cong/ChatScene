# Bus-Ego Query Library v0.2

## Active release files

- `bus_ego_topdown_2d_dev_query_library_v0_2.jsonl`: 48 development queries, 16 intents.
- `bus_ego_topdown_2d_query_library_v0_2.jsonl`: 252 locked test queries, 84 intents.
- `query_id_mapping_v0_1_to_v0_2.json`: one-to-one intent and query migration map.
- `policy_diagnostic_manifest_v0_2.jsonl`: generator-invisible migration audit.
- `v0_2_validation_report.json`: structural, source, mapping, and semantic-scope checks.
- `dev_vs_test_overlap_report_v0_2.json`: reproducible overlap audit.
- `build_query_library_v0_2.py`: project-relative deterministic builder and checker.
- `SHA256SUMS.txt`: hashes for every active release file above.

The checked-in v0.1 libraries are immutable migration sources. Publishing v0.2 does not silently migrate a consumer: active workflows must explicitly bind the v0.2 filenames and hashes.

## Split usage

- Development rows use `tuning_allowed=true` and `locked_test_usage=design_visible_reference_no_post_freeze_tuning`.
- Test rows use `tuning_allowed=false` and `locked_test_usage=held_out_test_only_not_for_tuning`.

## Field responsibilities

- `bus_operation_phase` is the sole structured source of the ego bus initial intent.
- `query_text` and `canonical_query` contain only initial intent, context, counterpart actors, and external interaction events.
- `interaction_events` contains only counterpart-actor actions, environment/signal events, and policy-independent triggers.
- `event_temporal_structure` orders only those external events.
- `unsupported_constraints` stores scope, topology, type, kinematic, or logical conflicts.
- `rule_hooks` remains a structured object with `scene_rules`, `counterpart_actor_rules`, and `counterpart_actor_violations`.
- `risk_level` describes external exposure/criticality, not a realized ego-policy outcome.

## Meaning of `view_mode`

`"view_mode": "top_down_2d"` means native simulation with ground-plane semantic geometry; height features are excluded from method ranking.

## Reproduction

Run with the repository Python 3.8 environment:

```bash
./chatscene/bin/python query_lib/build_query_library_v0_2.py --check
```
