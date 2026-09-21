# Experiment Plan

**Problem**: Use one frozen bus-scene query library to benchmark whether query-to-scene generation methods produce semantically correct, statically valid, and natively executable top-down scenarios without conflating generator quality with ego-policy or simulator-native variation.
**Method Thesis**: A deterministic-first, query-oracle-based protocol can measure semantic conformance, specificity robustness, unsupported handling, and platform-invariant semantic diversity while preserving each method's native generation architecture.
**Date**: 2026-07-14
**Status**: Query-library v0.2, schema-aware consumers, v0.2 machine drafts, metric gates, immutable generation harness, and both external runtime-track implementations exist; real human gold, source-bound platform fixture approval, Judge calibration, real method runs, and formal freeze remain pending

## Claim Map

| Claim | Why It Matters | Minimum Convincing Evidence | Linked Blocks |
|---|---|---|---|
| C1: Semantic correctness is separable from compilation, native execution, and ego-policy outcomes. | Otherwise the benchmark ranks simulator/controller behavior rather than query-to-scene generation. | Human-calibrated SRS/ARC/RSC/IEC_spec; separate Compile/SV/NE/IEC_exec; failure cases showing disagreement between semantic and runtime axes. | B1, B3, B4, B5 |
| C2: The unified library exposes robustness, support-boundary, and constrained-diversity failures missed by a single precise-query score. | This is the main reason to use precise/partial/vague triplets, unsupported intents, and repeated generation. | RQS floor, UQH, CPD_common with frozen coverage, confidence intervals, and intent-level failure analysis. | B3, B4, B5 |
| A1: Gains are not artifacts of judge-only scoring, evaluator repair, raw map diversity, or metric-specific tuning. | These are the strongest alternative explanations for favorable results. | Deterministic-first evaluator validation, immutable artifacts, one frozen configuration, no external support router, and common-semantic CPD fixtures. | B1, B2, B4 |

## Paper Storyline

- Main paper must prove:
  - semantic conformance is the primary generation result and is not replaced by executability;
  - specificity robustness and unsupported handling expose distinct method behavior;
  - CPD_common rewards only platform-invariant, constraint-preserving semantic differences.
- Appendix can support:
  - complete platform capability manifests;
  - atom-level judge confusion matrices and human-audit details;
  - per-subset, per-risk, and per-event-temporal-structure breakdowns;
  - platform-specific SV, NE, IEC_exec, and controller diagnostics.
- Experiments intentionally cut:
  - full production-bus dynamics or visual-fidelity claims;
  - a method-by-platform causal comparison, because full crossover is unavailable;
  - CPD-specific decoding or temperature tuning;
  - evaluator-side syntax or semantic repair.

## Frozen Evaluation Contract

- Test library: all 252 records in `bus_ego_topdown_2d_query_library_v0_2.jsonl`, comprising 84 intent groups, 228 supported queries, and 24 unsupported queries.
- Development library: all 48 records in `bus_ego_topdown_2d_dev_query_library_v0_2.jsonl`, comprising 16 non-overlapping intent groups—12 supported and 4 unsupported—with three surface styles each.
- Method input: `query_text` only. Query IDs are harness metadata; canonical queries, expected support, gold atoms, and evaluation annotations are evaluation-only.
- Repetition: five independent method runs per query, including unsupported queries.
- Artifact: one immutable platform-native scene artifact per run; no best-of-N selection or post-finalization repair.
- ChatScene exception: uniformly replace the ego blueprint with CARLA Chevrolet Impala and explicitly set the top-down footprint to 5.33 m by 2.10 m. Prompts, extraction, retrieval, generation, composition, and Scenic output schema remain unchanged.
- MetaDrive: use an external registered track adapter and an existing fixed vehicle proxy with the same footprint; do not add MetaDrive fields to ChatScene.
- Primary semantic axes: SRS, RQS, UQH, and CPD_common with coverage.
- Specialized semantic diagnostics: ARC, RSC, and IEC_spec.
- Validity/runtime axes: Compile Success, SV, and NE, always labeled by platform track.
- Driving diagnostics: IEC_exec/Event Realization Rate and any safety, comfort, or dynamics measures; never part of generation ranking.
- No composite total score.

## Current Code Audit and Required Containment

The following are implementation facts, not proposed architecture changes:

- `retrieve/retrieve.py` consumes plain lines from `scenario_descriptions.txt`; it does not understand JSONL IDs or preserve benchmark metadata.
- It writes `dynamic_{q}.scenic` and recreates `dynamic_log.csv`, so repeated benchmark runs overwrite earlier outputs unless the external harness snapshots each run.
- It logs `Success=1` before compilation and comments out the intended per-query exception path.
- `retrieve/utils.py::save_scenic_code` only constructs `ScenicSimulator`; this is Compile Success, not SV or NE. It moves failed `.scenic` files to `.txt`, so the harness must preserve the original artifact identity and failure stage.
- `retrieve/architecture.py` sets a placeholder API key in source. The benchmark does not edit that method source; the external adapter must use an approved secret-injection procedure and never record secrets. Until the method owner resolves the wrapper behavior, real ChatScene execution remains blocked.
- The benchmark package now has an external MetaDrive PG block-sequence artifact contract, token registry, runtime worker, native observer, and semantic extractor. The token/runtime implementation was source-validated against `/home/shijie20/CodeSpace/mdsn/metadrive` and passed independent review; no MetaDrive method adapter is inferred from the simulator source itself.
- The current generated ego uses `EGO_MODEL` but does not explicitly declare the accepted 5.33 m by 2.10 m Scenic footprint; the approved proxy change must cover both blueprint and dimensions.
- The current ChatScene path is effectively one-adversarial-actor oriented. Missing multi-actor, road, and event semantics must remain measurable failures rather than evaluator completions.

## Implementation Work Packages

### W0: Protocol assets and provenance

- Create a benchmark-only package outside ChatScene generation internals.
- Define schemas for query records, response disposition, artifact manifests, requirement atoms, evidence, metric outputs, and freeze manifests.
- Hash every input, configuration, model identifier, prompt, artifact, log, and evaluator version.
- Acceptance: a dry run can be reconstructed from a manifest without consulting mutable working files.

### W1: Development library and requirement oracle

- Author 16 new non-overlapping development intents and their three surfaces.
- Convert each test and development query into versioned core-required, surface-required, permitted, forbidden, and CPD-rewardable annotations.
- Use one accountable reviewer and exactly one complete human-gold record per subject; record atom provenance rather than copying canonical text as gold. Machine/agent recommendations remain drafts, leave human submissions unset, and cannot self-promote to gold.
- Acceptance: schema validation passes; all query IDs and triplets are unique and complete; no development intent is a semantic duplicate of a test intent.

### W2: Immutable generation harness

- Feed only `query_text` to the evaluated method while retaining IDs in the harness.
- Run ChatScene in an isolated staged workspace so its fixed filenames cannot mutate the source checkout or overwrite prior repetitions.
- Snapshot `.scenic` or failure `.txt`, request/response logs, retrieved examples, config, timestamps, and actual stage result after every run.
- Record response disposition for generated, rejected, clarification, and controlled-degradation outcomes.
- Acceptance: 252 queries times five runs produce exactly 1,260 immutable run manifests per method, including failures, with no filename collision.

### W3: Generator-preserving ChatScene adapter

- Change only the query-independent ego blueprint and top-down dimensions in generated content.
- Keep the source checkout unchanged. Bind any credential-injection or wrapper correction as a method-owner-controlled execution prerequisite; never write credentials into source, logs, or manifests.
- Do not edit extraction, behavior, geometry, or spawn prompts; retrieval database; snippet selection; generation stages; composition order; or output fields.
- Add a regression snapshot proving that, except for the approved ego substitution and non-semantic run metadata, identical inputs produce the existing ChatScene composition.
- Acceptance: a source and artifact diff contains no semantic generator change beyond the ego proxy.

### W4: Deterministic-first semantic evaluator

- Parse native artifacts into platform-specific evidence records.
- Implement ego gate, actor-role tuples, road/spatial atoms, reachable event specification, temporal order, and normative/risk atoms. Runtime ego reactions cannot certify `event_spec`; v0.2 event specifications are derived from the artifact/Judge evidence path because they describe counterpart or environment events.
- Use sampled geometry only for relations requiring a valid realization; use the frozen judge only when deterministic evidence is undecidable.
- Prevent comments, variable names, or natural-language headers from independently satisfying atoms.
- Acceptance: deterministic fixtures pass exactly; judge reaches macro-F1 at least 0.85 and kappa at least 0.8 on development data.

### W5: RQS, UQH, and CPD_common evaluators

- Implement RQS as the intent-level minimum across the three five-run style means; retain redundant `RQS_mean` only for compatibility.
- Implement UQH as the harmonic mean of supported acceptance and valid unsupported handling; verify generate-all and reject-all both score zero.
- Build the common semantic ontology, per-query eligibility, partial/vague coverage, common fingerprint extraction, fixed ten-pair denominator, and SRS_common multiplier.
- Acceptance: raw map-ID/seed-only changes yield zero CPD_common distance; one approved semantic perturbation yields the expected positive distance; output failure never reduces coverage.

### W6: Track-native Compile/SV/NE adapters

- CARLA: separate Scenic compile, five seeded SV trials, and one lowest-valid-seed 30-second rollout. Compile binds Scenic's staged static OpenDRIVE; NE additionally requires `/proc` pre/post identity of the frozen shipping server ELF and exact live-world Town/OpenDRIVE evidence. A missing listener fails NE only.
- MetaDrive: implement equivalent native load, five fresh deterministic static confirmations of the exact artifact/map seed (protocol indices `0..4`), and one 30-second rollout through an external track adapter; do not describe those confirmations as independent map samples.
- Freeze a query-blind reference ego policy per track; preserve braking in the CARLA action interface.
- Report SV, NE, and IEC_exec with the platform label and never use them to repair or rescore semantic artifacts.
- Acceptance: supported outputs permit at most 5,700 SV trials and 1,140 rollouts per method; unsupported queries never enter SV or NE.

### W7: Statistics, audit, and reporting

- Aggregate at output, query, intent, style, subset, risk, temporal-structure, and platform levels without double-counting overlapping semantic metrics.
- Use intent-clustered bootstrap confidence intervals and paired intent differences when methods share the same query set.
- Draw a pre-specified, stratified ten-percent output sample per method for post-test human audit.
- Emit machine-readable results plus paper tables; show CPD_common and coverage together.
- Acceptance: every reported number traces to artifact hashes and a frozen metric version; test-time audit cannot selectively change one method.

### W8: Verification and release gate

- Add unit tests for library counts, triplets, gates, formula edge cases, artifact immutability, no-oracle input leakage, stage separation, and bootstrap clustering.
- Add paired CARLA/MetaDrive equivalence fixtures for common fingerprints.
- Run a small development smoke test, then the complete development protocol, before creating the freeze manifest.
- Acceptance: all mandatory checks pass before any locked-test method call is issued.

## Experiment Blocks

### B1: Oracle and evaluator validity

- Claim tested: C1, A1.
- Why this block exists: benchmark conclusions are invalid if semantic atoms or cross-platform projections are unreliable.
- Dataset / split / task: 48-query development library, handcrafted deterministic fixtures, paired CARLA/MetaDrive semantic-equivalence fixtures.
- Compared systems: deterministic-only extraction; judge-only interpretation; deterministic-first hybrid; reference labels from one complete single-reviewer human-gold record per subject.
- Metrics: exact fixture accuracy, atom-level macro-F1, precision/recall, Cohen's kappa, fingerprint equality, disagreement categories.
- Setup details: freeze ontology and evidence precedence; identical judge model/rubric for all methods; hash-deduplicate identical artifacts.
- Success criterion: deterministic and equivalence fixtures at 100%; judge-assisted evaluation macro-F1 at least 0.85 and kappa at least 0.8.
- Failure interpretation: evaluator is not ready; expand development fixtures or simplify ambiguous atoms before test freeze.
- Table / figure target: main evaluator-validity table; appendix atom confusion matrix.
- Priority: MUST-RUN.

### B2: End-to-end harness and ChatScene preservation pilot

- Claim tested: A1.
- Why this block exists: current fixed filenames, early success logging, and compile-only checking can silently corrupt benchmark evidence.
- Dataset / split / task: first six development intent groups for one smoke run, then all 48 development queries for five runs.
- Compared systems: current ChatScene output path captured by the adapter; approved ego-proxy variant; no generator-content ablation.
- Metrics: manifest completeness, artifact hash uniqueness, stage-status correctness, oracle-leakage checks, compile/SV/NE separation, wall time.
- Setup details: isolated staged workspace, one frozen ChatScene config, query_text-only input, immutable artifact store.
- Success criterion: no overwritten or missing artifact; every failure preserved; generated content differs only by the approved ego proxy.
- Failure interpretation: stop before test and fix harness isolation or provenance; do not patch generated semantics.
- Table / figure target: pipeline integrity checklist and stage-flow figure.
- Priority: MUST-RUN.

### B3: Locked main benchmark

- Claim tested: C1, C2.
- Why this block exists: this is the primary result over the unified library.
- Dataset / split / task: all 252 locked test queries, five independent runs per method.
- Compared systems: unchanged ChatScene baseline plus each pre-registered competing method; at most three credible baseline families in the main paper.
- Metrics: decisive—SRS, RQS, UQH, CPD_common plus coverage; explanatory—ARC, RSC, IEC_spec; track-labeled—Compile, SV, NE, IEC_exec.
- Setup details: one frozen main configuration per method; no metric-specific generation; no post-output repair; platform assignment disclosed.
- Success criterion: complete preregistered run and confidence intervals, not a preselected favorable score threshold.
- Failure interpretation: low scores identify method or platform-track limitations; missing runs remain failures unless caused by a documented benchmark infrastructure outage affecting all methods equally.
- Table / figure target: main benchmark table and style-robustness plot.
- Priority: MUST-RUN.

### B4: Metric necessity and anti-claim checks

- Claim tested: C1, C2, A1.
- Why this block exists: demonstrate why compile/NE, mean-style SRS, judge-only scoring, or raw platform diversity are insufficient substitutes.
- Dataset / split / task: frozen development outputs for design validation; locked test outputs for preregistered diagnostic recomputation only.
- Compared systems: SRS versus Compile/SV/NE; IEC_spec versus IEC_exec; RQS floor versus RQS_mean; deterministic-first versus judge-only; CPD_common versus raw map/asset diversity.
- Metrics: rank correlations, disagreement rates, failure detection counts, human agreement, CPD coverage.
- Setup details: no additional generation calls and no change to primary metrics after freeze.
- Success criterion: each retained metric demonstrates a distinct interpretable failure mode or the paper removes the redundant claim.
- Failure interpretation: if an axis adds no information, demote it to appendix rather than inventing a stronger claim.
- Table / figure target: metric disagreement matrix and representative counterexamples.
- Priority: MUST-RUN for the central comparisons; NICE-TO-HAVE for extended subset plots.

### B5: Stratified failure and human-audit analysis

- Claim tested: C1, C2.
- Why this block exists: aggregate scores cannot reveal whether failures concentrate in road expression, multi-actor roles, temporal chains, vague wording, or unsupported handling.
- Dataset / split / task: at least ten percent of each method's locked-test outputs, stratified by platform, style, subset, stage result, and semantic score band.
- Compared systems: all registered methods; deterministic evaluator and frozen judge versus human review.
- Metrics: audit agreement, error taxonomy frequency, score correction estimate without changing official scores, per-stratum SRS/RQS/UQH/CPD_common.
- Setup details: blind human review where feasible; identical audit rubric across methods.
- Success criterion: evaluator error is quantified and no method-specific systematic bias is hidden.
- Failure interpretation: protocol change requires a new benchmark version and full rescoring, never selective repair.
- Table / figure target: failure taxonomy and qualitative scene panels.
- Priority: MUST-RUN.

## Run Order and Milestones

| Milestone | Goal | Runs | Decision Gate | Cost | Risk |
|---|---|---|---|---|---|
| M0 | Build development library, oracle, schemas, and freeze-manifest format. | No method test calls. | One complete source-bound human gold per subject; schema/count checks pass. | Human annotation dominates. | Test-intent leakage; mitigate with semantic duplicate review. |
| M1 | Validate extractors, formulas, and platform-common fingerprints. | Handcrafted and paired fixtures. | 100% deterministic/equivalence fixture pass. | Low simulator cost. | Ontology too broad; reduce to reliably extractable atoms. |
| M2 | Smoke-test immutable harness. | Six dev intents, one run each style. | No overwrite, leakage, secret, or stage-label error. | Low. | Current fixed paths; use isolated staged workspace. |
| M3 | Calibrate and freeze. | 48 dev queries times five runs per method. | Judge thresholds pass; configuration and hashes frozen. | 240 outputs per method plus validation. | Judge ambiguity; resolve only on development data. |
| M4 | Generate locked artifacts. | 252 test queries times five runs per method. | Exactly 1,260 immutable manifests per method. | 1,260 outputs; up to 5,040 LLM calls when ChatScene uses LLM generation for all three snippets. | API/runtime interruption; preserve terminal failure and resume only infrastructure-neutral shards. |
| M5 | Run native validation. | Up to 5,700 SV trials and 1,140 NE rollouts per method. | Every supported output has a terminal Compile/SV/NE record. | Up to 9.5 simulated rollout hours per method, plus startup overhead. | Simulator instability; preflight health checks and record infrastructure outages separately. |
| M6 | Score, audit, and report. | Semantic extraction, bootstrap, ten-percent audit. | All tables trace to frozen hashes; audit completed. | Human audit and judge inference dominate. | Post-hoc temptation; protocol changes require version bump and full rescore. |

## Compute and Data Budget

- Generation per method:
  - development: 48 queries times five = 240 responses;
  - test: 252 queries times five = 1,260 responses;
  - ChatScene pure-retrieval mode still makes one extraction-model call per response;
  - ChatScene `--use_llm` makes up to four model calls per response: extraction plus behavior, geometry, and spawn generation.
- Static validation per method:
  - supported test outputs: 1,140;
  - at most five trials each: 5,700 platform-native sampling trials.
- Runtime per method:
  - at most 1,140 rollouts times 30 simulated seconds = 9.5 simulated hours;
  - actual wall time must be measured during M2/M3 and included in the freeze manifest.
- GPU/API budget:
  - no training is required by the benchmark harness;
  - exact model inference cost depends on each frozen method and must be estimated from the development pilot rather than invented here.
- Human evaluation:
  - one accountable reviewer and one complete gold record for each development/test oracle subject;
  - judge calibration on development outputs;
  - at least 126 audited test outputs per method, because ten percent of 1,260 is 126.
- Data preparation needs:
  - 48-query development library;
  - layered requirement oracle;
  - CARLA and MetaDrive capability manifests;
  - common ontology, eligibility, and paired equivalence fixtures.
- Biggest bottleneck: human-reviewed semantic evidence and stable native simulator execution, not model training.

## Risks and Mitigations

- Risk: method and platform remain confounded.
  - Mitigation: use common semantic CPD only, disclose platform assignment, keep runtime axes platform-labeled, and avoid causal platform-adjusted claims.
- Risk: current ChatScene runner overwrites artifacts or labels compile checks as success.
  - Mitigation: isolated external harness, immutable run manifests, and terminal stage records written after each actual check.
- Risk: query metadata leaks into generation.
  - Mitigation: adapter contract and tests proving that only `query_text` crosses the method boundary.
- Risk: judge interpretation dominates semantic scores.
  - Mitigation: deterministic evidence precedence, frozen judge, development calibration, artifact hash deduplication, and post-test audit.
- Risk: unsupported queries inflate validity statistics.
  - Mitigation: exclude all unsupported records from SV/NE by protocol and score their response behavior only through UQH.
- Risk: CPD rewards irrelevant clutter or native platform assets.
  - Mitigation: oracle-approved common dimensions only, fixed ten-pair denominator, common preservation gate, and simultaneous coverage reporting.
- Risk: ego proxy dimensions disagree with the CARLA physical blueprint.
  - Mitigation: use the 5.33 m by 2.10 m footprint for semantic/static checks, disclose native physical mismatch, and keep low-level dynamics outside primary claims.
- Risk: credentials or sensitive prompts enter artifacts.
  - Mitigation: external secret custody or an approved authentication gateway, redacted logs, and pre-release secret scanning; never write keys into ChatScene source or benchmark artifacts.

## Final Checklist

- [ ] Development library contains 16 non-overlapping intent triplets.
- [ ] Locked test library hash and 252/84/228/24 counts are recorded.
- [ ] Every requirement-oracle subject has one complete source-bound human gold.
- [ ] Common CPD eligibility and partial/vague coverage are frozen.
- [ ] CARLA and MetaDrive capability manifests are frozen.
- [ ] Deterministic and cross-platform equivalence fixtures pass exactly.
- [ ] Judge calibration reaches the accepted development thresholds.
- [ ] ChatScene diff is limited to the ego proxy and credential handling.
- [ ] Immutable harness preserves every output and failure.
- [ ] One main configuration per method is hashed before test.
- [ ] Main paper tables are covered.
- [ ] Semantic correctness is separated from validity and driving diagnostics.
- [ ] RQS, UQH, and CPD_common claims are directly supported.
- [ ] Platform-confounding and CPD coverage limitations are disclosed.
- [ ] Post-test human audit and intent-clustered confidence intervals are complete.
- [ ] Nice-to-have runs are separated from must-run runs.
