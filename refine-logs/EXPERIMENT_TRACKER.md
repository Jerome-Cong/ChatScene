# Experiment Tracker

Use one row per immutable run shard or validation batch. Expand `{METHOD}` and `{PLATFORM}` only after the method registry is frozen.

| Run ID | Milestone | Purpose | System / Variant | Split | Metrics / Artifacts | Priority | Status | Notes |
|---|---|---|---|---|---|---|---|---|
| R001 | M0 | Validate v0.2 query-library schema and frozen counts. | Benchmark harness | dev + test metadata only | dev 48/16/36/12; test 252/84/228/24; complete triplets | MUST | COMPLETE | Structured `rule_hooks`, split policy, immutable v0.1 source binding, v0.2 target hashes, triplets, counts, and source hashes verified; no method calls. |
| R002 | M0 | Create and duplicate-check development intents. | Human + schema validator | dev | 16 intents; 48 records; semantic-overlap review | MUST | BLOCKED | Structure and machine overlap audit complete; novelty and duplicate-candidate decisions require one human gold each where registered. |
| R003 | M0 | Build and review layered requirement oracle. | One accountable reviewer | dev + test | core/surface/permitted/forbidden atoms; provenance | MUST | BLOCKED | v0.2 machine drafts and 300 single-reviewer tasks are ready with 5,701 machine-only recommendations over 3,601 atoms; all human responses remain unset and 300 real complete human-gold records remain. |
| R004 | M0 | Freeze platform capability manifests. | CARLA + MetaDrive adapters | platform fixtures | representable/extractable road, actor, event atoms | MUST | RUNNING | CARLA provenance and the MetaDrive token-only platform implementation passed independent review; its six machine decisions crosswalk to existing formal destinations and add zero standalone gold. Human capability gold and a concrete MetaDrive method adapter remain absent. |
| R005 | M1 | Validate deterministic semantic extractors. | Evaluator | fixtures | exact fixture accuracy | MUST | COMPLETE | Isolated, trusted-context, metamorphic, native, and full cross-referenced formal gates pass; both platform implementation milestones received independent review. Gate: 100%. |
| R006 | M1 | Validate cross-platform common fingerprints. | CARLA + MetaDrive | paired fixtures | fingerprint equality and controlled perturbation distance | MUST | COMPLETE | Paired contracts and controlled perturbations pass in the complete cross-referenced formal fixture. Gate: all expected results exact. |
| R007 | M1 | Unit-test SRS, RQS, UQH, and CPD_common formulas. | Metric engine | synthetic | edge cases and invariants | MUST | COMPLETE | Includes range attacks, generate-all/reject-all, ten-pair denominator, roster completeness, and provenance-chain tests. |
| R008 | M2 | Smoke-test query_text-only boundary. | ChatScene adapter | 6 dev intents | leakage checks; request manifests | MUST | RUNNING | Harness leakage/immutability attacks pass; real method smoke remains blocked by the method environment. IDs stay harness-side. |
| R009 | M2 | Smoke-test artifact immutability and terminal stages. | ChatScene + CARLA | 6 dev intents | hashes; Compile/SV/NE stage records | MUST | BLOCKED | Capture and fail-closed contracts pass; real generation and CARLA API-responsive rollout evidence are absent. |
| R010 | M2 | Verify approved ego-proxy substitution. | ChatScene + CARLA | dev fixtures | blueprint; 5.33 m by 2.10 m footprint; regression diff | MUST | RUNNING | Static/finalizer attacks pass; real terminal ChatScene artifact smoke is still required. No other generator-content change. |
| R011 | M3 | Run full development generation. | `{METHOD}` / frozen candidate config | dev | 240 response manifests per method | MUST | TODO | Five runs per query. |
| R012 | M3 | Calibrate frozen judge against human labels. | Deterministic-first evaluator | dev outputs | macro-F1; precision/recall; kappa | MUST | TODO | Gate: F1 ≥ 0.85, kappa ≥ 0.8. |
| R013 | M3 | Measure pilot compute and simulator wall time. | `{METHOD}` + `{PLATFORM}` | dev | calls, GPU/API time, SV time, NE time | MUST | TODO | Used for scheduling, not test tuning. |
| R014 | M3 | Create benchmark freeze manifest. | Benchmark harness | frozen protocol | hashes for data, oracle, configs, models, seeds, adapters | MUST | BLOCKED | Freeze implementation and adversarial fixtures pass; real human gold, capabilities, extractor fixtures, judge calibration, and method configs are not yet frozen. |
| R100-{METHOD} | M4 | Generate locked test artifacts. | `{METHOD}` / one frozen main config | test | 1,260 immutable response manifests | MUST | TODO | No metric-specific configuration. |
| R200-{METHOD} | M5 | Run native compile/load and SV. | `{METHOD}` + `{PLATFORM}` | supported test | Compile; up to 5,700 SV trials | MUST | TODO | Unsupported excluded. |
| R300-{METHOD} | M5 | Run native executability rollouts. | `{METHOD}` + `{PLATFORM}` | supported test | up to 1,140 NE and IEC_exec traces | MUST | TODO | 30 simulated seconds, no retry. |
| R400-{METHOD} | M6 | Extract semantic evidence and score outputs. | Frozen evaluator | test | SRS; ARC; RSC; IEC_spec; response dispositions | MUST | TODO | Deterministic evidence cannot be overridden. |
| R410-{METHOD} | M6 | Aggregate specificity and support metrics. | Metric engine | test intents | RQS; RQS_mean; vague gap; UQH | MUST | TODO | Intent-group aggregation. |
| R420-{METHOD} | M6 | Compute common semantic diversity. | Common CPD evaluator | eligible partial/vague | CPD_common; partial/vague/all coverage | MUST | TODO | Precise reports stability only. |
| R430-{METHOD} | M6 | Run clustered bootstrap. | Statistics pipeline | test intents | means; 95% CIs; method-difference CIs | MUST | TODO | Resample whole intent groups. |
| R440-{METHOD} | M6 | Conduct stratified human audit. | Human reviewers | ≥10% test outputs | agreement; error taxonomy; bias checks | MUST | TODO | At least 126 outputs per method. |
| R450 | M6 | Run metric-necessity diagnostics. | Reporting pipeline | frozen outputs | semantic/runtime disagreement; RQS and CPD comparisons | MUST | TODO | No new generation. |
| R460 | M6 | Produce final machine-readable and paper reports. | Reporting pipeline | all | tables; figures; manifests; limitations | MUST | TODO | CPD_common always shown with coverage. |
| R900 | M6 | Optional extended subset visualizations. | Reporting pipeline | test | additional risk/event/platform plots | NICE | TODO | Must not delay core results. |

## Status Values

- `TODO`: not started.
- `RUNNING`: active and writing only to its immutable shard.
- `BLOCKED`: cannot proceed until the stated gate is satisfied.
- `FAILED`: terminal experimental result; do not silently rerun.
- `COMPLETE`: outputs, hashes, and terminal status verified.

## Test-Start Gate

- [ ] R001–R014 are `COMPLETE`.
- [ ] No development/test semantic overlap remains unresolved.
- [ ] No credential is stored in source, logs, or manifests.
- [ ] Frozen configurations and all artifact schemas are hash-addressed.
- [ ] The 48-query development suite is the only design-visible query set; the 252-query test suite remains held out and no test output has been used for tuning.
