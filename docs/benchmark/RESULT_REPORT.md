# Formal method-result report

`method-result` publishes exactly one tamper-evident, exclusive-create result
envelope for one
registered `method_id × platform` cell.  It does not change ChatScene, Scenic,
SafeBench, method adapters, generation semantics, or controller semantics.

## Admission and recomputation

The formal command accepts only complete, already-produced inputs:

- verified frozen manifest, test library/oracle, confirmed roster, and frozen
  main method config;
- committed generation directory and its exact `response.jsonl`;
- semantic evidence, semantic scores, a validated Judge run directory, and the
  semantic/RQS/UQH child aggregate plus signed UQH assessments/registry;
- frozen CPD coverage and the CPD child aggregate;
- native runtime records/aggregate and the frozen platform/controller config.

The publisher never treats a child aggregate as authoritative.  It revalidates
the generation, Judge, semantic-score, UQH-signature, CPD, and native-runtime
chains; recomputes every reported metric from leaf records; and requires the
supplied child aggregates to be canonical-exact copies of those recomputations.
Bare Judge responses are not accepted.

Before publication, stable file-descriptor reads opened with `O_NOFOLLOW`
capture the exact 23 public inputs and the transitive generation, Judge,
runtime, freeze, implementation, and dependency leaves. File identity/content,
parent-directory identity/change time, and bundle directory inventories form a
private closure guard; the closure digest/counts are recorded in provenance.
Transitive discovery parses only those 23 direct documents and JSON reached by
an exact hash/size file binding; directory enumeration is capture-only. Runtime
attempts must be strict descendants of the bound runtime-record directory, and
all directory closures have fixed raw-scan, retained-inventory, file, byte, and
depth limits. The raw-scan budget is enforced before per-directory sorting and
also counts entries later excluded by cache policy. CARLA trees use their
declared cache-exclusion policy; MetaDrive source closure is exactly the
Git-tracked worktree inventory accepted by the bound source snapshot. Untracked
and ignored files do not enter that closure; a tracked bytecode file, if one
exists, remains part of it.
The complete reconstruction is run twice, then guards are checked immediately
before and after the exclusive atomic link. Publication retains a
component-by-component `O_DIRECTORY|O_NOFOLLOW` parent FD for staging, linking,
`fsync`, verification, and cleanup. A successful publication intentionally
leaves its transaction-unique hidden stage name as a hard link to the published
inode. Failures may leave a stage and/or quarantine name. All are retained for
explicit operator cleanup because POSIX name-based unlink cannot atomically
guarantee that a same-name foreign replacement did not arrive after identity
verification. A foreign object stays under the isolated name, while any object
that appeared concurrently at the public name is left untouched.
Existing output paths are always rejected, even when their bytes are identical.
The output parent must already exist and must not be a guarded input parent or
sit inside a guarded input bundle, so publication cannot mask parent-directory
drift.

## Frozen primary scalar definitions

The envelope reports a metric vector, never a composite total:

- `SRS`, `ARC`, `RSC`, and `IEC_spec` are the numeric fields returned by
  `aggregate_semantic_metrics`; incomplete diagnostic coverage rejects formal
  publication instead of producing null or filling a value.
- `RQS` is exactly `compute_rqs(...).rqs`, the specificity-floor macro score;
  `rqs_mean` is diagnostic only and is never substituted for it.
- `UQH` is exactly `compute_uqh(...).uqh` after raw-response, assessment,
  registry, signature, roster, and main-config validation.
- `CPD_common` is exactly `cpd_common.joint.macro_mean`.  Partial and vague
  macro means remain diagnostics.
- `coverage` is exactly frozen `cpd_coverage.coverage`; it is reported beside
  CPD and is not multiplied into it a second time.
- `SV` and `NE` are `scene_validity` and `native_executability` from the single
  platform returned by a fresh `aggregate_runtime` call.
- `IEC_exec` is diagnostic only and is fixed to `status=unavailable`,
  `coverage=none`, with no score field.  A usable-value claim is invalid.

The closed schema has no `total`, `overall`, ranking, interpolation, or platform
correction field.  Adding one invalidates the result even if the self-hash is
recomputed.

## Cell and comparison policy

One envelope is one observed registered cell.  A CARLA result does not require
the same method to have a MetaDrive result.  A missing cell is omitted: it is
never inserted as zero, interpolated, or imputed.  This cell rule does not
weaken the existing global formal-freeze gate: the shared final Judge freeze
still requires the currently frozen cross-platform infrastructure and
`cross_platform_final` calibration profile.

CPD rewards only the frozen platform-independent common-semantic dimensions.
The same coverage asset defines the eligible opportunity set for every cell;
native map IDs, blueprint IDs, MetaDrive block tokens, and other
platform-specific realization choices are not rewarded dimensions.  The
platform label is still retained for audit.  Results from different platforms
may be described on these common dimensions, but the benchmark applies no
platform causal correction and must not turn a platform-confounded comparison
into a method-superiority claim.

For a result collection, coverage means the fraction of pre-registered
platform-independent CPD candidate queries whose frozen policies are confirmed
and evaluable.  It is not the fraction of methods that happened to run on two
platforms.  Report the set of present cells alongside comparisons so omitted
cells remain visible.

## Commands

```bash
./chatscene/bin/python scripts/bus_benchmark.py method-result \
  --freeze-manifest FREEZE.json \
  --library TEST_LIBRARY.jsonl \
  --oracle TEST_ORACLE.jsonl \
  --roster METHOD_PLATFORM_ROSTER.json \
  --method-config FROZEN_METHOD_CONFIG.json \
  --generation-dir GENERATION_DIR \
  --responses GENERATION_DIR/response.jsonl \
  --semantic-evidence SEMANTIC_EVIDENCE.jsonl \
  --semantic-scores SEMANTIC_SCORES.jsonl \
  --judge-run-dir JUDGE_RUN_DIR \
  --semantic-aggregate SEMANTIC_AGGREGATE.json \
  --uqh-assessments UQH_ASSESSMENTS.jsonl \
  --uqh-assessor-registry UQH_REGISTRY.json \
  --cpd-aggregate CPD_AGGREGATE.json \
  --cpd-coverage CPD_COVERAGE.json \
  --runtime-records RUNTIME_RECORDS.jsonl \
  --runtime-aggregate RUNTIME_AGGREGATE.json \
  --platform-config PLATFORM_CONFIG.json \
  --controller-config CONTROLLER_CONFIG.json \
  --output METHOD_RESULT.json
```

Independent verification reconstructs the full live cell rather than merely
checking the envelope's self-hash:

```bash
./chatscene/bin/python scripts/bus_benchmark.py method-result-verify --input METHOD_RESULT.json
```

Frozen-manifest verification reads the bound human-gold asset as part of full
freeze validation, but human gold is not used in method-result metric
computation and is never modified. The upstream protocol continues to use
exactly one human gold per subject and introduces no second review or
adjudication.

## Current verification boundary

### Audit-safe metric implementation checkpoint (2026-07-29)

Metric implementation can proceed while human query review remains active
because scoring code consumes a finalized oracle only at execution time; it
does not write the Workbench assignment or checkpoint.  The active
`workbench_assignment_v0_2` directory is ignored as external-custody state and
was byte-checked before and after this implementation pass.  Its development
and test checkpoints remain non-gold and are not metric inputs.

This checkpoint completed the following work without changing query-library,
oracle-draft, Workbench, or human-finalization semantics:

- repaired cross-schema CPD aggregate validation after an external `coverage`
  reference, while preserving the existing aggregate contract;
- exercised the smallest complete CARLA method-result unit—one
  precise/partial/vague intent triplet × five repetitions—through the real
  Schema, semantic, RQS, CPD, runtime-aggregate, provenance, and publisher
  code, using synthetic leaf evidence only;
- made synthetic runtime fixtures bind the Python environment root and control
  files required by the hardened provenance validator; and
- made the formal Judge fixture bind a base, standard-library-only Python
  executable whose measured package inventory remains identical when the
  executable is sealed for validation.

The repository-local Python 3.8 environment passed 97 selected
metric/semantic/CPD/provenance/integration tests, 34 method-result tests, and
all 28 runtime/native-worker tests.  The 48-query mock runtime CLI test is
included in that runtime count and passed independently.  All 25 Schema tests
also passed.  These are contract and synthetic-integration checks, not a
method-performance result.

Two gates deliberately remain open until their source state is ready:

- the draft ChatScene/CARLA implementation bundle is internally self-hash
  consistent, but three pre-existing live bindings belong to the actively
  edited human-review UI/workflow.  They will be rebound once review tooling is
  stable rather than freezing moving audit code during review; and
- formal CARLA freeze still fails closed at
  `formal CARLA Python runtime requires a complete runtime-tree closure`.
  Real generation, a bound CARLA server session, CUDA/model/credential
  readiness, confirmed rosters, finalized human gold, and final extractor
  coverage are therefore not claimed by this checkpoint.

The current 2026-07-15 local verification artifact is
`benchmark_artifacts/evidence/carla_local_contract_verification_2026_07_15_v2.json`.
It binds the live 113-file generation/evaluator harness, 19 executed test files,
both readiness reports, and 288 passing selected tests: 35 generation, 95
semantic/metric/CPD/provenance, 60 Agent/human-workflow/Schema, 85
runtime/method-result/Judge, eight MetaDrive contract, and five formal-freeze
tests. Source inventories and declared test-count arithmetic are independently
recomputable. Raw unittest logs were not persisted, so the historical pass
states and elapsed times require rerunning the recorded commands; the report
does not claim otherwise. The benchmark worktree itself is still untracked and
not frozen. The prior 109-file/214-test artifact
`benchmark_artifacts/evidence/carla_local_contract_verification_2026_07_15.json`
is historical only.

This is contract and synthetic-integration evidence only. The report explicitly
records that real ChatScene generation, CARLA NE, a formal method result, and
method-performance evidence have not been produced. The v3 CARLA readiness
diagnostics remain NO-GO with 13 blockers (nine formal and four execution).
Their current SHA-256 values are `fc0ba09f…c2f9bf` for development and
`1024f0ca…85d0b` for test, and both explicitly record the regular running
launcher identity check as passed.
They use the regular launcher from the repository-local Python 3.8 uv
environment, whose 173-package inventory, 67 startup controls, and CARLA
0.9.13 egg import now pass. This is draft readiness only: a complete Python
runtime-tree closure has not been provided, so formal validation still fails
closed. CUDA, model-cache, credential, server, roster, snapshot, and
formal-freeze blockers remain. Real human gold
also remains exactly
zero: the active policy requires one complete human gold per subject, with no
A/B duplicate, adjudication, or mandatory second review. Machine/Agent drafts
cannot satisfy that gate.

The root `workbench.ipynb` now provides an `ipywidgets` interface for the 300
source-ready P0 query subjects. It uses the same production exporter,
per-subject validator, and finalizer, keeps partial work in non-gold checkpoints,
and requires explicit human completion. Its availability does not change the
current real-human-gold count of zero.

The CARLA-first human workload is 442 eventual gold records: 300 query subjects,
52 platform fixture/capability subjects, and 90 calibration subjects. Only the
300 query packets are currently source-ready; the other 142 require a confirmed
development oracle and generated CARLA source assets. Each evaluated method
later adds at least 126 blind post-test audit records. The 13 final protocol
subjects remain deferred until the cross-platform assets exist.

The focused method-result tests cover orchestration, identity isolation,
aggregate forgery rejection, live reconstruction, bounded schema-driven input
closure, and tamper-evident exclusive-create publication. A full publisher
integration smoke sends the smallest valid RQS unit—one CARLA
precise/partial/vague intent triplet × five repetitions (15 outputs)—through
`build_method_result` with real schemas, roster admission, semantic scoring,
SRS/ARC/RSC/IEC_spec aggregation, RQS, CPD recomputation, runtime-record
consistency, runtime aggregation, and exact child-aggregate comparison. Only
generation, Judge, independent UQH-assessor execution, native simulator setup,
and the smoke-only 76-per-style size gate are substituted. The formal publisher
itself remains fixed at 76 CPD candidates per partial/vague style; the reduced
smoke is not formal admission. The underlying
generation, Judge, semantic/UQH, CPD, provenance, and runtime validators retain
their own real test suites.  A single full real formal `method-result` run over
the complete 252-query cell remains part of the final full gate because it
requires produced generation/Judge/runtime artifacts and a freeze regenerated
after this evaluator/schema addition.
