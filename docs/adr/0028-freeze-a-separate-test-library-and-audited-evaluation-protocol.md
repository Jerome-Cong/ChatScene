---
status: superseded
superseded_by: 0029-treat-v0-1-as-design-visible-and-freeze-human-protocol-decisions.md
---

# Freeze a separate test library and audited evaluation protocol

> Historical note: this ADR is superseded by ADR-0029. In particular, its
> `ScenarioOnlineEnv` and dual-platform calibration statements are not current
> readiness claims. A method-independent MetaDrive token-only platform track now
> exists, but the concrete MetaDrive generation method and final calibration
> remain deferred until that tested method is selected.

The complete 252-query version 0.1 library is the locked test set. It is not used to tune prompts, retrieval, thresholds, extractors, judges, method configurations, or stopping rules. A separate development and calibration library must be created with 16 non-overlapping intent groups—12 supported and 4 unsupported—with precise, partial, and vague surfaces for 48 total queries.

## Consequences

- The development intents cannot be paraphrases or semantic duplicates of locked test intents.
- At present, exactly zero admissible human-gold records have been completed. Synthetic approvals and labels in formal contract tests exercise validator acceptance and attack rejection only; they never count as benchmark gold or completed human review.
- Before test generation, a hashed freeze manifest records both libraries, the human-reviewed requirement oracle, CPD eligibility and coverage, platform capability manifests, semantic extractors, judge rubric and model, all method configurations, five-run budgets and seeds, ego proxies, controllers, runtime limits, exact Python interpreters and package versions, and git revision plus tracked-source-tree hashes for Scenic and MetaDrive.
- One human reviewer produces one hash-bound final gold record for each
  requirement-oracle subject. The reviewer explicitly accepts, rejects, or
  replaces each machine-drafted atom; silent omission or an `uncertain` result
  is not a completed gold label.
- The evaluator ontology is pre-registered and may be extended from the development oracle only; locked-test argument values never shape extractor or Judge capability claims. Benchmark-owned raw-artifact fixture corpora are frozen separately from extractor results. Their exact sizes are 94 deterministic cases and 23 cross-platform cases, covering one registered conformance representative per atom predicate domain with platform-specific positive/negative controls, all 11 CPD values on both platforms, four ego controls per platform, six category-equivalence pairs, six controlled differences, and 11 CPD-equivalence pairs. Capability records enumerate the exact argument values covered deterministically; every other argument value routes to the frozen Judge and is never inferred to be supported from predicate-level fixture success. Formal scoring re-executes the frozen extractor in a bounded isolated worker, verifies oracle invariance, and requires its live projection to equal stored evidence.
- The current CARLA fixture environment proves native Scenic parse/compile only
  because the CARLA server is not API-responsive on this host. The MetaDrive
  platform probe can natively materialize and step strict PGMap token artifacts,
  but remains draft and does not establish that any future tested method can
  generate them correctly.
- Every fixture and capability status has one complete hash-bound human gold
  review. `confirmed` without that exact review coverage cannot freeze.
- Final Judge calibration will use two benchmark-owned platform reference
  corpora and precommit 90 judge-routed atoms per platform before labels or
  predictions exist. The current non-formal stage contains only the 90 CARLA
  items; the MetaDrive corpus and the final 180-item dual-platform gate remain
  deferred. One human reviewer labels each item while blind to Judge
  predictions. Exactly one human gold is stored per item and contains no machine
  response binding. Predictions are parsed from a separately validated raw
  execution bundle rather than supplied by calibration rows; the final judge must reach atom-level macro-F1
  of at least 0.85 and Cohen's kappa of at least 0.8 overall and per platform.
- Each method produces 1,260 test responses. Supported queries permit at most 5,700 SV sampling trials and 1,140 30-second NE rollouts; unsupported queries remain outside SV and NE.
- Confidence intervals use intent-clustered bootstrap resampling, keeping all three surfaces and their repeated outputs together.
- At least ten percent of each method's test outputs receive stratified post-test human audit. A resulting protocol change requires a benchmark version increment and complete rescoring for every method; method-specific corrections are prohibited.
