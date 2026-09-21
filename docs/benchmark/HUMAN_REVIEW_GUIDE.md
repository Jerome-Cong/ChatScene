# Human Annotation and Audit Guide

## Purpose

Human work supplies semantic gold and measures evaluator error. It must not edit,
repair, select, or rerun a method output. Generation, platform validation, metric
calculation, bootstrap, and audit sampling remain automatic after the protocol is
frozen.

Current status: the number of completed, admissible human-gold records is exactly
zero. All existing oracle and protocol assets remain machine drafts. Approval
records created inside formal contract tests are synthetic test data: they prove
only that the freeze gate accepts a complete record and rejects attacks. They are
never admissible as benchmark gold, completed annotation work, or evidence of
reviewer identity.

The 252-query suite is **design-visible**, not an untouched held-out test set:
its contents were available while the evaluator was designed. Freezing prevents
any further tuning after the declared snapshot, but version 0.1 makes no
held-out-generalization claim. Such a claim requires a new, independently held
unseen query library.

Current human-review execution remains **CARLA-first**. The MetaDrive token-only
platform implementation now exists, but its platform/cross-platform review,
concrete method adapter, calibration, and final 13 hash-bound protocol subjects
are deferred. A CARLA-stage asset always carries
`formal_freeze_eligible: false` and cannot be submitted as the eventual
dual-platform freeze asset.

## Human work by lifecycle

### 1. Pre-freeze oracle review

One named reviewer produces the final label for every query before seeing
frozen-suite method outputs. Each subject can create exactly one human-gold
record; pending or uncertain work is not gold.

- Test: 252 query surfaces from 84 intents.
- Development: 48 query surfaces from 16 intents.
- Total: 300 reviewer submissions and 300 eventual human-gold records.
- Machine draft: 3,601 atom instances, 1,800 required-check entries, and 300
  CPD policies, for 5,701 explicit verdict entries inside 300 subject records;
  none is gold until confirmed.

The assignment packet exposes one mechanical retention proposal for each of
those 5,701 entries. A separate source-bound Agent semantic review covers all
100 intents and expands its findings to all 300 subjects: 4,078 decision
entries are finding-linked, while 1,623 are explicitly marked as
source-projection defaults. Its recommendations include 1,923 proposals to
accept the current value and 3,778 items requiring human judgment. Both
artifacts are drafting aids only. They
never populate the editable human submission: every response starts unset, and
neither an Agent recommendation nor a pending/uncertain/rejected response is
human gold.

For every query, reviewers decide:

1. supported or unsupported and the allowed response dispositions;
2. actor type, role, cardinality, polarity, and relative position;
3. operational road and spatial atoms;
4. event/state/trigger/constraint classification, actor binding, completion, and
   temporal edges;
5. core-required, surface-required, permitted, and forbidden layering;
6. CPD eligibility, common dimensions, mutually exclusive allowed values, and
   cross-platform judgeability.

The workbench may batch exact repeated atom semantics and surface-scoped CPD
policies, but the reviewer must inspect every query text listed in that batch.
Batch actions populate only blank per-instance drafts and allow existing or
later per-query decisions to remain exceptions; they never complete a subject.
The current extractor finds only five numeric surface deltas and explicitly
does not claim exhaustive non-numeric deltas.

Each final review payload must explicitly accept, reject, or replace every
machine-drafted atom and close all six required checks. The freeze gate
recomputes the final oracle hash. A `revise` verdict must change the
corresponding projection in `proposed_oracle`; CPD revision must also exactly
match `replacement_policy`. An unimplemented revision, a rejected high-level
check, a rejected CPD policy, a status-only record, or silent deletion of draft
atoms is rejected before `confirmed` can be written.

The production path is `query-export` → reviewer submission → `query-finalize`.
The finalizer writes confirmed oracle records and one `human_query_gold` record
per query. New atoms carry `human_review` provenance.

The equivalent graphical path is `workbench.ipynb` → source-bound checkpoint
→ explicit per-subject completion → split finalization. The notebook exposes
the Agent semantic draft beside the form but never applies it as a human
decision. A support/acceptable-response disagreement cannot be finalized in the
UI because those fields belong to the frozen query source; correct the source
and regenerate the assignment instead.

Finalization does not trust a self-consistent edited packet. It regenerates the
entire reviewer-visible bundle from the current library/oracle sources and the
assigned reviewer ID, then requires canonical equality. A modified query text or
record remains invalid even if every packet hash is recomputed.

### 2. One-time protocol decisions

Reviewers must eventually close exactly 13 hash-bound machine-proposed decision
subjects before the dual-platform freeze. Each subject will follow the same
one-reviewer/one-gold policy:

- suite visibility, development/test semantic overlap, and duplicate clustering;
- B08 supported-generation versus strict-rejection handling for UQH, including
  the frozen independent-assessor registry and zero credit for controlled
  degradation in version 0.1;
- actor cardinality, lane tri-state semantics, risk scoring, and event ontology;
- CPD eligibility/common dimensions and the fixed Judge calibration roster;
- CARLA and MetaDrive validation levels, plus the ego proxy and query-blind
  controller contract.

The current draft recommends statistical collapsing only for A07/E03 and B09/E06.
It keeps all original queries and repetitions in the benchmark.

The historical protocol files under `benchmark_artifacts/human_review/pending/`
are superseded, unbound previews and must not be assigned or counted. The
MetaDrive-adjusted protocol workflow will be generated only after all final
cross-platform assets exist.

For the CARLA-first stage these 13 subjects are deliberately **not exported or
counted**: several decisions bind MetaDrive and final cross-platform assets that
do not yet exist. Reviewing placeholders now would create a stale protocol
asset. They remain an eventual freeze obligation, not current CARLA work.

The CARLA stage has 14 capability entries and 38 CARLA raw-artifact fixture
cases, hence 52 platform subjects. Every subject binds its complete content hash
and ontology-definition hash to one final reviewer label.
Fixture inputs and expected values are frozen separately from extractor-produced
results; raw artifacts cannot contain benchmark-truth sidecars.

These fixture/capability counts are derived from the final confirmed development
ontology, not imposed as a quota. If human oracle review modifies, splits, or adds
an ontology domain, fixtures and review subjects must be regenerated and the
workload summary updated before freeze.

Reviewers must not overstate CARLA evidence: it currently has native Scenic
compile plus five-seed static-SV evidence, but no successful API-responsive
server rollout on the present host. Interpreter binaries, package versions,
source revisions, and tracked source-tree hashes are machine-verified and do not
create annotation subjects.

MetaDrive is not reviewed in this stage. Its external platform contract limits
road construction to built-in sequence-block tokens; the runtime/observer path
is implemented, source-tested, and independently reviewed as a platform-track
candidate. The
tested-method adapter and real tested-method outputs have not been provided;
method-dependent fixtures, capability decisions, calibration, and final protocol
binding will be regenerated after that method is integrated. No MetaDrive
subject is counted in the current CARLA workload, and native platform probes are
not human gold or method-run evidence.

The replayable
`benchmark_artifacts/evidence/metadrive_native_probe_evidence_v0_1.json` is a
Schema-bound machine gate over the bound source/runtime, 13 admitted tokens,
three expected failures, and one native reset-step fixture. It creates no review
packet and no additional human-gold subject. Its token whitelist maps to the
existing `registry_implementation:metadrive:pg_block_registry` platform-review
subject; the top-down proxy maps to the existing `EGO_PROXY_AND_CONTROLLERS`
subject; native reset-step admission maps to the existing
`RUNTIME_VALIDATION_METADRIVE` subject.

The six checked-in MetaDrive machine decisions are a crosswalk, not a seventh
review object. Each maps to an existing platform subject, one of the final 13
protocol subjects, or an automatic admission gate; therefore they add zero
standalone human-gold records.

### 3. Development calibration

CARLA-stage calibration uses one benchmark-owned CARLA reference corpus covering
all 48 development queries with five outputs per query: 240 finalized outputs and
240 matching semantic-evidence records. It is a calibration reference, not an
extra obligation for every evaluated method. Deterministic evidence is used
first; humans label only atoms routed as undecidable.

- Reference population: exactly 240 CARLA development outputs.
- Human unit: each judge-routed atom, not every deterministically decidable atom.
- Stage roster: exactly 90 precommitted CARLA atoms.
- Selection rule: the fixed seed `bus-judge-calibration-v0.1` and frozen
  `query_category_then_sha256_v0.1` algorithm are used once; labels, Judge
  predictions, or observed verdict balance cannot trigger reselection.
- One reviewer directly labels `satisfied`, `violated`, or `unknown` while blind
  to the Judge prediction. `unknown` is a semantic verdict, not review uncertainty.
- Stage diagnostic: recomputed atom-level macro-F1 and Cohen's kappa for CARLA;
  this cannot substitute for the eventual per-platform dual-platform gate.
- Stage support disclosure: report the observed gold count for each verdict both
  overall and for CARLA. The label-blind CARLA stage does not fail when one
  verdict has zero observed support and cannot be reselected from observed labels.
- Final support gate: `cross_platform_final` requires all three verdicts to be
  represented on each platform. Its precommitted rosters cannot be reselected,
  and there is no adaptive minimum-per-verdict quota or favorable-seed search.
- Coverage gate: the CARLA roster covers every category and supported
  development query that actually contains at least one Judge-eligible atom; a
  category or query with no Judge-eligible atom is not fabricated to fill a quota.

The eligible judge-routed population is intentionally not guessed before
extractor execution. From that population, the stage deterministically selects
the exact 90-item CARLA roster before labels or predictions exist. It
records every selected item, its one final human gold, prompt, model,
inference configuration, strict output schema, actual gold support, and recomputed
metrics. A pre-run context binds that gold and the complete 240-response CARLA
development roster with 240 corresponding semantic-evidence records. The
separate request and run manifests bind the exact process stdout for each of the
90 selected atoms. A calibration file cannot provide a prediction: the evaluator
rebuilds the full Judge request and strictly parses the prediction from validated
stdout bytes.
Every selected atom must both exist in the confirmed development oracle and
remain genuinely undecidable after live execution of the frozen deterministic
extractor.

The stage sequence is `carla-calibration-subjects` →
`carla-calibration-export` → reviewer submission →
`carla-calibration-finalize`. Human packets contain the query,
artifact, deterministic evidence, and oracle atom, but recursively reject Judge
predictions and Judge response bindings. Human gold contains no machine-response
fields; the separately validated run is joined by `item_id` only after the gold
is locked. The finalized wrapper remains marked
`stage_confirmed` and `formal_freeze_eligible: false`.

Platform and calibration finalizers likewise require their original ontology,
fixture/capability, roster, and blind-subject sources. They regenerate the full
bundle before any verdict becomes gold, so edited `audit_subject` or
`review_payload` content cannot be accepted by merely recomputing `packet_id`.

### 4. Post-test audit

The program selects a deterministic stratified sample after scoring:

- population per method: 1,260 frozen-suite responses;
- minimum sample per method: `ceil(1260 × 0.10) = 126`;
- total for `M` methods: `126 × M` outputs.

One blind reviewer may inspect all selected outputs. Every response must be
`complete`; `uncertain` is explicitly not gold and makes `post-test-finalize`
fail closed. There is no mandatory second-review path. The finalizer also
recomputes the exact deterministic sample from the full population, declared
rate, and seed. The audit records human SRS, ARC, RSC, IEC_spec,
error class, and notes. Audit findings estimate evaluator error and bias; they do
not alter one method's official scores. A protocol error requires a new benchmark
version and complete rescoring of every method.

Every method output choosing rejection or clarification requires one independent,
content-addressed UQH assessment before formal aggregation. The assessor may be a
frozen Judge or a named human-audit protocol, but its identity, source,
configuration, and independence from the method-output producer must be registered
before test execution. The assessment cites exact verified stdout/stderr byte
ranges and must cover every frozen oracle conflict reason; missing assessments stop
the formal pipeline rather than silently lowering the method score.

Raw assessor request/response hashes prove that the decision can be reproduced;
they do **not** authenticate who made it. Formal UQH therefore also requires an
RSA-PSS signature for every assessment. The trust root is the public key in the
frozen assessor registry plus the one-time human approval of independent key
custody. The private key must be mounted outside the repository and method runner,
with owner-only permissions (`0600` recommended; read-only `0400` is accepted),
and is never copied into an artifact, manifest, log, or result record. Group- or
world-accessible keys, keys inside the repository, and keys that do not match the
frozen public key are rejected. Provider
response IDs remain useful audit metadata but are not treated as cryptographic
proof.

The formal order is fixed:

1. Run `generate` to produce the immutable response, generation-evidence, and run-manifest bundle.
2. Run `uqh-request` against that verified generation chain. It atomically publishes `request_index.jsonl` and every exact assessor request.
3. The independent Judge or human assessor consumes only that request bundle and writes one `uqh_assessor_execution` row plus raw JSON response per required run.
4. Run `uqh-attest`; it prevalidates every input, signs only in staging, and atomically publishes `assessment_index.jsonl` plus signatures.
5. Pass the published assessment index to formal `aggregate`.

Request export:

```bash
./chatscene/bin/python -m bus_benchmark.cli uqh-request \
  --responses generation/response.jsonl --oracle test_oracle.jsonl \
  --roster query_roster.json --uqh-assessor-registry uqh_assessor_registry.json \
  --generation-library test_query_library.jsonl \
  --generation-method-config frozen_method_config.json \
  --generation-output-root generation \
  --generation-evidence generation/evidence.jsonl \
  --generation-run-manifest generation/generation_run_manifest.json \
  --assessor-id frozen-uqh-assessor --freeze-manifest freeze.json \
  --output-dir uqh_requests
```

After independent execution, create the signed bundle with:

```bash
./chatscene/bin/python -m bus_benchmark.cli uqh-attest \
  --responses generation/response.jsonl --oracle test_oracle.jsonl \
  --roster query_roster.json --assessor-executions uqh_executions.jsonl \
  --uqh-assessor-registry uqh_assessor_registry.json \
  --generation-library test_query_library.jsonl \
  --generation-method-config frozen_method_config.json \
  --generation-output-root generation \
  --generation-evidence generation/evidence.jsonl \
  --generation-run-manifest generation/generation_run_manifest.json \
  --assessor-id frozen-uqh-assessor --private-key "$UQH_SIGNING_KEY" \
  --freeze-manifest freeze.json --output-dir uqh_attested
```

The command validates exact execution coverage, reconstructs the assessment from
the raw response, binds the frozen request/config/source, checks that the external
private key matches the frozen public key, signs each record, and runs the same UQH
validator used by aggregation.

The checked-in registry remains deliberately non-runnable: its public key is
`null`, and the draft config still contains `TBD_BEFORE_FREEZE` for both model and
inference configuration. Choosing the independent assessor/model, approving the
public key and custodian, and replacing those placeholders are explicit human
freeze blockers; synthetic test keys and labels cannot satisfy them.

The test suite has 24 unsupported queries and five repetitions, so a method can
require at most 120 UQH assessments. This is not necessarily human work: the draft
recommendation is an independently frozen Judge, with human audit reserved for
calibration and disputes. If a human assessor is frozen instead, add one human
decision for each rejection/clarification output. Controlled degradation remains
zero in version 0.1 regardless of any submitted assessment.

## Human workload summary

The current CARLA-first workload forecast has 442 review subjects: 300 query-oracle
subjects, 52 CARLA fixture/capability subjects, 90 CARLA Judge-calibration atoms,
and zero protocol subjects at this stage. That means 442 reviewer submissions
and 442 eventual human-gold records. The 13 final protocol subjects are deferred
until every cross-platform asset exists. Atom-level decisions remain embedded in
the 300 query reviews and are not additional subjects. Only the 300 query packets
are currently generatable; the 52/90 counts are unbound forecasts, not completed
formal gold or assignable packets.

Those 442 records contain 5,843 explicit reviewer verdict entries: 3,601 query
atoms, 1,800 query checks, 300 CPD policies, 52 platform approvals, and 90
calibration labels. This second count estimates form-level work; it must not be
misreported as 5,843 independent gold records.

The 442 value is not trusted merely because it appears in a draft. The production
`inventory-refresh` command accepts the exact query bundle(s), CARLA platform
bundle, CARLA calibration bundle, and their current source files in one call. It
requires the exact checked-in 48/252 query-library and oracle-draft bytes and
recomputes counts from bound tasks. Only the complete profile can say
`stage_bound`; incomplete work must explicitly use `partial_dev`.

After evaluation, each method adds at least 126 blind output audits. The total is
therefore `442 + 126 × M` complete reviewer records for `M` methods during the
CARLA stage. Current admissible
completion remains zero. If UQH uses a frozen human assessor rather than the
recommended independent Judge, add `N_reject_or_clarify` human assessments per
method, where `0 ≤ N_reject_or_clarify ≤ 120`.

## Machine-prepared review assets

- `benchmark_artifacts/drafts/test_oracle_draft.jsonl`
- `benchmark_artifacts/drafts/dev_oracle_draft.jsonl`
- `benchmark_artifacts/drafts/test_oracle_review_tasks.jsonl`
- `benchmark_artifacts/drafts/dev_oracle_review_tasks.jsonl`
- `benchmark_artifacts/drafts/query_agent_semantic_review_v0_2.json`
- `benchmark_artifacts/drafts/agent_review_fragments/dev_intents.json`
- `benchmark_artifacts/drafts/agent_review_fragments/test_A_C_intents.json`
- `benchmark_artifacts/drafts/agent_review_fragments/test_D_G_intents.json`
- `benchmark_artifacts/drafts/protocol_decisions_draft.json`
- `benchmark_artifacts/drafts/uqh_assessor_registry_draft.json`
- `benchmark_artifacts/drafts/uqh_assessment_config_draft.json`
- `benchmark_artifacts/drafts/uqh_assessor_source_draft.md`
- `benchmark_artifacts/drafts/dev_test_overlap_draft.json`
- `benchmark_artifacts/drafts/statistical_clusters_draft.json`
- `benchmark_artifacts/drafts/human_work_inventory_draft.json`
- checked-in static summary
  `benchmark_artifacts/drafts/human_review_machine_draft_summary.json`
- assignment-time `machine_draft_registry.json` produced by
  `scripts/export_human_review_drafts.py`

These files are intentionally marked `draft`; changing the marker alone is not
sufficient. The formal freeze validates their content and cross-references.

The batch exporter writes full `*_bundle.json` files plus editable
`*_submission_template.json` files. The current checked-in sources make all 300
query subjects actionable and attach 5,701 machine-only, non-gold mechanical
form proposals plus the independently source-bound Agent semantic artifact; all
human submission responses remain unset. The remaining 52 platform and 90
calibration subjects stay `NO_GO` until their source groups exist; the registry
records each missing dependency and still reports zero generated human gold.

## Work that is automatic after freeze

- JSONL schema, counts, triplets, IDs, hashes, and roster completeness;
- exactly five immutable responses for every registered query;
- Compile, SV, and NE execution plus the fixed `IEC_exec` diagnostic slot and
  terminal-stage recording; no event observer means the slot remains
  `unavailable` rather than becoming an execution claim;
- payload-blind execution of the trusted, frozen benchmark-owned semantic
  extractor from the immutable artifact, oracle-invariance checks, exact
  comparison with stored evidence, and judge invocation under the frozen
  rubric; this is a frozen-code and payload boundary, not a general filesystem
  sandbox;
- SRS, ARC, RSC, IEC_spec, RQS, UQH, CPD_common, coverage, and bootstrap;
- UQH raw stdout/stderr/artifact byte revalidation and exact assessment coverage;
- UQH raw assessor-response reconstruction and detached-signature verification;
- deterministic selection of an audit sample with a hard minimum rate of 10%;
- completed single-reviewer blind-audit validation, hash-checked private unblinding, and
  per-method evaluator-error reporting;
- response-to-evidence-to-score provenance and freeze-manifest verification.

## Minimum staffing

The protocol requires one accountable reviewer assignment per subject. Multiple
people may share the overall workload, but no subject requires A/B duplication,
joint adjudication, or a mandatory second review to become gold.

Command details and formal outputs are documented in
`docs/benchmark/HUMAN_REVIEW_WORKFLOW.md`.

Local hashes bind decisions to exact content and prevent silent replacement inside
the benchmark bundle. They do not authenticate that a reviewer or model provider
is who it claims to be. Therefore agent-completed submissions, even if they pass
the structural finalizer, remain synthetic fixtures and must not be counted as
real human gold. Production admissibility requires external human custody of the
submission/finalization step. If cryptographic identity is required, publish an
append-only review ledger or require reviewer/provider signatures before freeze.
