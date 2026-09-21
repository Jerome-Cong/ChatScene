# Production Human-Review Workflow

## Boundary

`scripts/human_review_workflow.py` is the CARLA-first production entry point. It
does not call ChatScene, alter its architecture, repair generated scenes, or
change method outputs. Every subject has one source-bound reviewer packet and
can create exactly one final human-gold record.

The code authenticates content, not human identity. `reviewer_id` is an
assignment label, and the generated registry explicitly records
`identity_proof_provided: false` plus
`authorship_trust_boundary: external_human_custody_required`. A record is
admissible as real human gold only when the assigned human controls the editable
submission/finalization step (or an external signed/append-only review ledger is
added before freeze). Agent-generated complete submissions are structurally
valid test fixtures, not evidence of human authorship.

The active rule is:

- one named reviewer submission per query, platform, calibration, or post-test
  subject;
- only `review_status: complete` is gold;
- `uncertain`, pending, rejected, incomplete, or stale-bound work fails closed;
- no A/B packet, adjudication packet, or mandatory second-review path exists in
  the production CLI.

Current review scope is CARLA-first. CARLA platform/calibration results remain
stage assets with `formal_freeze_eligible: false`. The MetaDrive token-only
platform implementation passed independent review, but its human platform
review, concrete method adapter, and the final 13 cross-platform protocol
subjects are deferred.

## Machine drafts and assignment registry

Generate the two currently actionable, source-bound query bundles for one real
reviewer with:

```bash
./chatscene/bin/python scripts/export_human_review_drafts.py \
  --reviewer REAL_REVIEWER_ID \
  --output-dir HUMAN_REVIEW_OUTPUT
```

The command writes complete `*_bundle.json` inputs accepted by
`query-finalize`, separate editable `*_submission_template.json` files, and a
hash-bound `machine_draft_registry.json`. Every query task also contains one
explicit `machine_recommendation`: 5,701 machine-only mechanical form proposals
over the 300 subjects. The separate source-bound
`benchmark_artifacts/drafts/query_agent_semantic_review_v0_2.json` contains the
independent Agent semantic pass over all 100 intents, expanded to the same 300
subjects and 5,701 decision slots. Neither artifact populates the human
submission, has reviewer identity or completion status, or creates human gold.
Reviewer IDs are assignment labels, not identity proof.

The semantic artifact is intentionally conservative after the v0.2 event-
responsibility migration. Of its 5,701 draft entries, 4,078 are linked to
intent-level semantic findings and 1,623 are explicitly labelled
source-projection defaults. Its recommendations are 1,923 `accept_current` and
3,778 `human_judgment_required`; no machine entry claims a completed revision
or rejection. These are triage recommendations, not executable
oracle revisions: the human reviewer must inspect the stated finding and encode
any accepted revision in `proposed_oracle`.

The checked-in
`benchmark_artifacts/drafts/human_review_machine_draft_summary.json` is a
schema-validated pre-assignment summary. It is not a production registry and
does not bind assignment files. Only the registry written into the requested
output directory is authoritative for that reviewer assignment.

`HUMAN_REVIEW_OUTPUT` must be a new or empty, non-symlink directory. The batch
exporter never overwrites an existing assignment or foreign file. Resume an
existing assignment through `workbench.ipynb`, which verifies its registry,
bundles, live v0.2 Agent/oracle counts, and checkpoint before reuse.

The registry distinguishes subject records from embedded form decisions:

- 300 query subjects contain 3,601 atom verdicts, 1,800 required-check verdicts
  (six per query), and 300 CPD verdicts: 5,701 explicit verdict entries;
- 52 CARLA platform subjects add 52 approve/reject verdicts;
- 90 calibration subjects add 90 semantic labels;
- therefore the CARLA stage is 442 human-gold records containing 5,843 explicit
  verdict entries.

Without additional sources, only the 300 query packets are ready and the
registry reports 142 blocked subjects. Once all three platform inputs exist,
append `--ontology`, `--semantic-fixtures`, and `--carla-capability`; once both
calibration inputs exist, append `--calibration-roster` and
`--calibration-reviewer-subjects`. Each input group is all-or-none, preventing a
partial packet from being presented as review-ready.

## Graphical query-review workbench

`workbench.ipynb` is the graphical entry point for the currently actionable P0
query review. It covers the same 48 development and 252 test subjects as the
production exporter and finalizer; it does not cover the 52 CARLA platform or
90 calibration subjects whose source assets are not ready.

Launch it from the repository root with the project's `chatscene` uv
environment (Python 3.8.10), which already contains JupyterLab and
`ipywidgets`:

```bash
source chatscene/bin/activate
jupyter lab workbench.ipynb
```

The launcher asks for a real reviewer's assignment label, a work directory, and
the split. On first use it invokes `scripts/export_human_review_drafts.py` to
create the canonical 300-subject assignment. On later use it verifies every
source and artifact binding before reopening the checkpoint. The interface
renders only the current query and organizes the audit into five human-facing
steps: participants/external events, road/space, rules/risk, final consistency,
and CPD/notes. Each draft requirement is shown first as a plain-language scoring
statement with its evidence and strictness. Atom IDs, raw metadata, replacement
targets, split/merge tools, and CPD dimension internals stay in collapsed
advanced sections. The precise/partial/vague triplet and Agent semantic findings
are also collapsed reference material, not additional answers.

On wide screens the current request stays in a sticky left column while the
review steps occupy the right column; narrow screens use one column. Requirement
cards are directly visible within each step. `正确`, `不应计分`, and
`允许但不强制` are single-click actions; `其他判断` retains the existing structured
modification/split/merge controls. Mapping summaries and machine hints are grouped
under each card's expandable reference. Each card shows its current selection and each
step reports how many decisions remain unselected. `已选` / `已选齐` describe
selection progress only, not successful validation or human confirmation.

The `标注指南` entry brings together scoring strictness, shared check criteria,
and the draft/completion rules. Opening it never changes answers. Task-binding
IDs and checkpoint paths remain available in a collapsed technical reference.
`完成本条审核` retains its existing behavior; `完成并继续` additionally advances
from the original item to the next unfinished query in the current filter and
search scope, wrapping at the end. A validation/save failure stays on the current
query. Successful navigation also focuses the persistent query selector so the
next request starts in view. If that scope has no other unfinished item, the UI
explains this instead of changing the filter or search. Neither completion action
finalizes the split.

For the reviewer, a requirement card has only four common meanings:

- `正确`: the machine-drafted content and strictness
  are both correct;
- `不应计分`: remove the candidate from scoring, not from the scene;
- `允许但不强制`: the detail may appear, but its absence must not
  reduce the score;
- `内容或计分严格程度需要修改`: open the advanced editor only when the final
  requirement differs from the draft.

Before reviewing subjects one by one, open `同内容批审` in the toolbar. The panel
is collapsed initially so it does not displace the current request. It
groups atoms by the exact formal semantic projection (`category`, `predicate`,
`arguments`, `layer`, `polarity`, and `weight`) and groups CPD policies by exact
policy content within one surface style. Every covered query text is rendered
in the batch; reading all of them is required before applying a batch draft.
The action fills only blank, unfinished instances. Existing drafts and complete
subjects remain instance-level exceptions and are never overwritten. Complex
atom modifications, splits, merges, and any context-specific disagreement stay
in the per-query editor.

Batching is deliberately split-local so development review never exposes test
text early and one batch write touches only one crash-safe checkpoint. The
checked-in sources contain 490 atom semantic groups and 40 surface-scoped CPD
groups globally; keeping the development/test boundary yields 127 + 380 atom
batches and 11 + 38 CPD batches in the two workbenches. This small duplication
is the cost of preserving split isolation and atomic checkpoint writes.

Rationales are conditional rather than repeated free text:

- accepting a requirement writes a standard human attestation when the draft is
  saved or completed; the mechanical proposal's boilerplate is not carried into
  the human response;
- rejecting, modifying, splitting, or merging a requirement requires a human
  explanation because the disagreement cannot be reconstructed from the source
  alone;
- four atom-based high-level summaries are derived automatically from the same
  source-bound before/after projection used by the production validator. The
  reviewer sees the derived result and attests it when completing the subject,
  rather than selecting a duplicate verdict. Support/response remains an
  independent human decision; revising or rejecting that source always requires
  prose because the workbench has no structured editor for it;
- accepting CPD receives a standard attestation; CPD revision first uses a
  structured reason category, with prose required only for `other`; rejecting
  the CPD source requires a written explanation;
- adding a requirement requires one subject-level note explaining what the
  machine draft omitted; no prose is required when no requirement is added.

The active Schema still receives a non-empty `reason` for every decision. This
UI change reduces typing without weakening the source-bound submission, gold,
or finalizer contract. Existing custom human rationales are preserved, and
unfinished manual text is retained per verdict while switching choices in the
current session.

If either completion action fails, the workbench renders a persistent card-level
issue list above the current query. Each affected requirement shows its review step,
plain-language statement, current verdict, and the exact missing action (for
example, a missing verdict, rationale, or replacement target). The first
affected card's decision selector receives focus automatically; every other card
has a locate button. The relevant step and any required advanced editor open
before focus moves to that selector. The production validator remains the
authority for completion.

Step 4 no longer asks for four duplicate atom-summary choices. It continuously
shows `accept` when the final atom projection equals the source and `revise`
when reviewed requirements change that projection. The generated response
still contains all six required-check decisions and non-empty reasons; only the
UI action is deduplicated. `support_and_response_disposition` remains visible
and manual, while `cpd_common_eligibility` remains synchronized from the single
visible CPD decision. If a reviewer believes an automatic projection or one of
its non-atom source fields (`ego_gate` or `surface_style`) is wrong, the row has
an explicit exception control requiring a written reason; it records `reject`
and keeps that subject blocked until the source is corrected.

Step 4 now exposes the production mapping instead of requiring the reviewer to
infer it. Every requirement card names the Chinese high-level summaries it
affects, and every summary contains a collapsed list of its concrete source
cards. These are overlapping consistency projections, not exclusive metric
buckets and not a second review:

- `cardinality_and_polarity` reads the content arguments and polarity of every
  requirement, so lane counts and risk levels both affect it;
- `road_and_spatial_decomposition` reads only road and spatial requirements;
- `event_graph_and_actor_binding` reads actor, spatial, event, temporal, and
  normative/risk requirements, so a risk card affects it but a lane-structure
  card does not;
- `surface_layering` reads each requirement's content parameters, layer,
  polarity, and weight.

Membership is calculated by counterfactual calls to the same
`query_check_projection` used by formal closure validation. The UI therefore
cannot silently maintain a second hand-written mapping table. Both that
projection and its Chinese scope explanation are generated from the shared
`QUERY_CHECK_PROJECTION_SPECS`; a future unknown field fails closed instead of
silently disappearing from the explanation. It also states
explicitly that this relationship does not ask the reviewer to assign fields
to benchmark metrics manually; downstream scoring consumes the finalized
oracle.

The visible CPD decision receives the same preflight treatment without showing
the duplicate internal closure check. A claimed CPD revision with no policy
change, a changed policy still marked correct, or a CPD source rejection is
reported in Chinese, opens step 5, and explains the exact next action. Where
the safe resolution is only to correct the derivative verdict, the report
offers an explicit synchronization button; it never edits the CPD policy on
the reviewer's behalf.

Within one intent triplet, complete the `precise` surface first. When that
subject passes formal per-query validation, the workbench atomically creates
drafts for blank `partial` and `vague` siblings:

- decisions for source requirement cards with the same stable atom ID are
  copied from the completed precise review;
- precise-only cards, such as a numeric distance stated only by the precise
  query, are not inserted into the other surfaces;
- a merge is inherited only when its complete source group exists on the
  target surface; a surviving fragment of a cross-surface merge falls back to
  that target's mechanical card decision;
- any target-only card keeps that target surface's mechanical default;
- high-level closure summaries are re-derived against each target oracle, so
  they cannot claim a structural revision which the target does not contain;
- CPD `accept` means accepting each target surface's own CPD source policy;
- a precise CPD revision which already equals the target surface's source
  policy is re-based to `accept`, rather than creating a false target revision;
- both sibling entries remain `human_confirmed: false` and create no gold.

Automatic inheritance never overwrites a complete sibling or a populated
decision slot. If batch review has already filled some partial/vague slots,
precise inheritance fills only the untouched atom, support, CPD, summary, note,
or added-atom slots and preserves every instance-level exception. For a precise
subject completed before this feature existed, a partial/vague surface with
safe untouched slots offers an explicit `从 precise 创建未完成草稿` button.
Inherited values are only a review starting point: the reviewer must compare
the current surface text, change any missing or weaker requirements, and
explicitly complete that subject.

In the advanced editor, **source** means the machine-drafted candidate and
**target** means the human-reviewed final requirement. Accepting or excluding a
candidate needs no target. Modify/split/merge decisions require one or more
targets because the finalizer must preserve an auditable mapping from every
draft candidate to the reviewed oracle.

An item in `人工新增要求` must have a genuinely new requirement identity. If it
duplicates an existing source, an advanced-edit target, or another added item,
the completion report opens the added-item editor and identifies the conflict
in Chinese. Likewise, an advanced target may not recreate a different source
card which the reviewer already excluded; that conflict is attached to the
editing source card and names the excluded requirement.

Use only one browser tab or kernel for a reviewer assignment. The checkpoint
uses an OS lock plus compare-and-swap; if another session saves first, the stale
session fails closed instead of overwriting newer work.

The workbench enforces the following transitions:

- `Save draft` atomically writes a source-bound local checkpoint with
  `human_gold: false` and `formal_submission: false`.
- Loading the mechanical proposal requires an explicit warning acknowledgement
  that it replaces the current draft and still creates only a draft. It never
  copies the proposal into a human submission automatically.
- `Validate and mark complete` runs the production per-subject schema, closure,
  and formal-payload checks before recording explicit human completion.
- Switching a query or changing filters first saves the current draft.
- Field changes are debounce-autosaved while the Jupyter kernel is alive, and a
  visible badge distinguishes saved from unsaved state. Click `Save draft`
  before closing the page or stopping the kernel.
- Structured atom/CPD edits are retained even before their verdict is chosen;
  verdict consistency is enforced only when the subject is validated complete,
  so an intermediate save never discards unfinished semantic edits.
- Clearing a subject requires a separate explicit checkpoint-deletion
  acknowledgement.
- Split finalization stays disabled until every subject is explicitly complete,
  requires typing `FINALIZE`, claims a new output directory with exclusive
  creation, and then calls the canonical production finalizer. A failed write
  leaves an incomplete directory for inspection rather than deleting or reusing
  it.

### Example: change one draft requirement to “allowed but not required”

For a concrete semantic correction, first load the mechanical proposal only if
you have acknowledged that it is a machine draft. Then:

1. Open the relevant step and read its visible requirement card: the current query,
   plain-language requirement, its evidence, and its current strictness.
2. If the detail is not required by the current surface but is valid when
   present, click `允许但不强制`. The UI creates the required target,
   sets its layer to `permitted`, and supplies an editable rationale. No JSON or
   atom ID is needed.
3. In `最终一致性确认`, inspect the automatically derived summaries. For
   example, changing an actor from `core_required` to `permitted` automatically
   changes both actor/event binding and scoring strictness to `revise`; no
   duplicate selection is needed.
4. Click `完成本条审核`. The UI serializes the structured fields
   internally and sends them through the same production per-subject validator;
   the reviewer never has to hand-edit atom or CPD JSON.

For less common semantic edits, select `内容或计分严格程度需要修改` and open
`高级修改`. Copy the source as a template, then edit the target with structured
fields. Splits create multiple targets. Cross-source merges and completely new
requirements live under `高级操作` and should be used only when ordinary card
editing cannot express the correction.

CPD revisions follow the same rule: choose `修订`, select the primary structured
reason, then edit candidate/eligibility, allowed values, distance-bin rows, and
the target actor class with structured controls. Free text appears only for an
`other` reason or a source-error rejection. The frozen query-blind selector is
derived automatically and is not free-form reviewer input. The production
schema records the CPD policy verdict and its high-level closure check
separately, but the workbench asks the reviewer only once and emits the two
matching fields automatically.

Disagreement with frozen support metadata is a source-correction blocker, not a
valid completed response. Fix the query/oracle source, export a new source-bound
assignment, and review it again. Do not edit the checkpoint hash or packet to
force completion. Protect the assignment/finalization directory as human-held
review material; do not commit it or treat the reviewer label as identity proof.

This P0 workbench needs no API key, GPU, CARLA server, model download, or network
connection.

## Query oracle

Export each query-library split for its assigned reviewer:

```bash
./chatscene/bin/python scripts/human_review_workflow.py query-export \
  --library query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl \
  --oracle-draft benchmark_artifacts/drafts/dev_oracle_draft.jsonl \
  --reviewer REVIEWER_ID \
  --output dev_query_bundle.json
```

The reviewer completes the packet's `submission_template`. Finalize directly:

```bash
./chatscene/bin/python scripts/human_review_workflow.py query-finalize \
  --bundle dev_query_bundle.json --submission dev_query_submission.json \
  --library query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl \
  --oracle-draft benchmark_artifacts/drafts/dev_oracle_draft.jsonl \
  --output-dir dev_query_gold
```

The finalizer writes `confirmed_oracle.jsonl` and `human_query_gold.jsonl`.
Every draft atom and required check must close. `accept` cannot silently change
the corresponding oracle projection; `revise` must change it; CPD revision must
equal its replacement policy. New human atoms require `human_review`
provenance.

Each task's `machine_recommendation.recommended_response` is only the exact
mechanical retention proposal needed to expose every form field. The reviewer
must look up the same `query_id` in
`query_agent_semantic_review_v0_2.json`, handle
`human_judgment_required` first, and then inspect the
remaining source-projection defaults. The human submission remains
`response: null` until the assigned reviewer supplies a complete response. The
exporter never copies either machine artifact into the submission and always
creates zero gold. If a named reviewer independently adopts the same decision,
the finalizer can create a content-valid gold record; the code cannot prove that
a human, rather than an agent, authored those bytes, so external human custody
is part of admissibility.

## CARLA platform review

After the development oracle fixes the common ontology, export 38 CARLA fixture
cases plus 14 capability statuses:

```bash
./chatscene/bin/python scripts/human_review_workflow.py carla-platform-export \
  --ontology COMMON_ONTOLOGY.json \
  --semantic-fixtures SEMANTIC_FIXTURES.json \
  --carla-capability CARLA_CAPABILITY.json \
  --reviewer REVIEWER_ID \
  --output carla_platform_bundle.json

./chatscene/bin/python scripts/human_review_workflow.py carla-platform-finalize \
  --bundle carla_platform_bundle.json \
  --submission carla_platform_submission.json \
  --ontology COMMON_ONTOLOGY.json \
  --semantic-fixtures SEMANTIC_FIXTURES.json \
  --carla-capability CARLA_CAPABILITY.json \
  --output carla_platform_gold.json
```

A rejected subject is not gold: fix the candidate asset, regenerate its packet,
and review the new content hash. Platform implementation choices, including
future MetaDrive sequence-block tokens, never become CPD_common dimensions.

The stage report validator checks every nested platform gold against its strict
record Schema, recomputes each content ID, rejects duplicate/missing subjects,
and requires the declared source count. Authoritative validation additionally
requires the original bundle, submission, ontology, fixture corpus, and
capability source and exact-rebuilds the whole report; Schema-only validation is
not a provenance claim.

## CARLA Judge calibration

The fixed CARLA stage has 90 Judge-routed atoms. First materialize blind reviewer
subjects from the roster and hash-matched evidence, then export one packet:

```bash
./chatscene/bin/python scripts/human_review_workflow.py carla-calibration-subjects \
  --roster CARLA_ROSTER.json --library DEV_LIBRARY.jsonl \
  --oracle CONFIRMED_DEV_ORACLE.jsonl --responses RESPONSES.jsonl \
  --evidence EVIDENCE.jsonl --output reviewer_subjects.jsonl

./chatscene/bin/python scripts/human_review_workflow.py carla-calibration-export \
  --roster CARLA_ROSTER.json --reviewer-subjects reviewer_subjects.jsonl \
  --reviewer REVIEWER_ID --output carla_calibration_bundle.json
```

The packet recursively excludes Judge predictions and Judge response IDs. The
reviewer directly supplies `satisfied`, `violated`, or `unknown`; the finalized
gold remains independent of machine responses:

```bash
./chatscene/bin/python scripts/human_review_workflow.py carla-calibration-finalize \
  --bundle carla_calibration_bundle.json \
  --submission carla_calibration_submission.json \
  --roster CARLA_ROSTER.json \
  --reviewer-subjects reviewer_subjects.jsonl \
  --output carla_calibration_gold.json
```

Each calibration subject has exactly one complete human-gold record. A separate
validated Judge run is joined later by `item_id`; no machine response ID or hash
is copied into human gold.

As with the platform stage, the finalized calibration wrapper validates every
nested gold and its content ID. Its authoritative validator also requires the
original blind bundle, submission, roster, and reviewer-subject source and
exact-rebuilds the complete report before it may be reused downstream.

The finalizer JSON is the sole CARLA-stage gold input; do not copy its 90
`human_gold_records` into a second JSONL file. Bind that exact file and its hash
into the supported calibration context command, then run the common
request/execution chain:

```bash
./chatscene/bin/python -m bus_benchmark judge-calibration-context \
  --profile carla_stage \
  --query-library DEV_LIBRARY.jsonl --oracle CONFIRMED_DEV_ORACLE.jsonl \
  --roster CARLA_ROSTER.json --human-gold carla_calibration_gold.json \
  --prompt JUDGE_PROMPT.txt --output-schema JUDGE_OUTPUT_SCHEMA.json \
  --runner-manifest JUDGE_RUNNER_MANIFEST.json \
  --model-id MODEL_ID --inference-config INFERENCE_CONFIG.json \
  --carla-source-config CARLA_SOURCE_CONFIG.json \
  --carla-roster CARLA_DEV_ROSTER.json \
  --carla-responses RESPONSES.jsonl --carla-evidence EVIDENCE.jsonl \
  --output carla_stage_context.json

./chatscene/bin/python -m bus_benchmark judge-calibration-request \
  --context carla_stage_context.json --output-dir carla_stage_requests
./chatscene/bin/python -m bus_benchmark judge-calibration-run \
  --context carla_stage_context.json --request-dir carla_stage_requests \
  --output-dir carla_stage_run
```

This 90-item CARLA-only result reports calibration diagnostics but remains
`formal_freeze_eligible: false`. The later `cross_platform_final` profile must
contain 90 CARLA and 90 MetaDrive items, requires both platforms' source inputs,
and is the only profile whose context, request manifest, and run manifest may
enter a formal freeze. Both profiles use one complete human gold per subject
and the same Judge execution/diagnostic core.

`unknown` is a valid semantic verdict. It is distinct from an uncertain review
status; only the latter is excluded from gold.

All three finalizers re-materialize the canonical bundle from the current source
files and the assigned reviewer ID, then exact-compare it before accepting the
submission. Recomputing `packet_id` cannot legitimize modified query text,
platform audit content, calibration evidence, duplicated subject fields, or
stale task hashes. Gold records and stage reports carry an explicit canonical
source/bundle revalidation flag.

## Source-bound inventory

The current CARLA workload is source-derived as 300 query + 52 platform + 90
calibration = 442 subjects, hence 442 reviewer submissions and 442 eventual gold
records. Completed real gold remains zero until humans submit it.

The default `carla_stage` profile requires exactly the checked-in 48/252 query
library and oracle-draft bytes, plus the current platform and calibration source
files used to create their bundles:

```bash
./chatscene/bin/python scripts/human_review_workflow.py inventory-refresh \
  --inventory benchmark_artifacts/drafts/human_work_inventory_draft.json \
  --query-bundle dev_query_bundle.json --query-bundle test_query_bundle.json \
  --platform-bundle carla_platform_bundle.json \
  --calibration-bundle carla_calibration_bundle.json \
  --ontology COMMON_ONTOLOGY.json \
  --semantic-fixtures SEMANTIC_FIXTURES.json \
  --carla-capability CARLA_CAPABILITY.json \
  --roster CARLA_ROSTER.json \
  --reviewer-subjects reviewer_subjects.jsonl \
  --output carla_human_work_inventory.json
```

The result may say `stage_bound` only after all hashes and exact counts pass.
Incomplete/synthetic development packets require explicit `--profile
partial_dev` and remain `partial_dev`. No protocol bundle is accepted in this
CARLA phase.

## Post-test audit

`post-test-export` deterministically selects a method-stratified sample at a
minimum rate of 10% and separates public method-blind tasks from private
linkage. The one reviewer must mark every selected task `complete`; uncertain
responses cannot be finalized as gold.

`post-test-finalize` rebuilds the sample from the full population with the
declared rate and seed and requires exact public/private equality. This prevents
a forged packet from claiming 10% while silently submitting fewer items. Audit
findings estimate evaluator error; they do not alter one method's official
score.

The final report stores exactly one pure `human_gold_record` per selected blind
subject and a separate unblinded comparison record that references that gold by
content hash. Method identity, automatic scores, and error values never enter
the human-gold record. Gold IDs bind the exact reviewer-visible subject,
reviewer response, private link, source output, population, and packet. Report
validation is never report-only: it requires the original public packet,
private linkage, submission, and full population, rebuilds the canonical report,
and rejects every byte-level difference, including synchronized deletion,
adaptive rehashing, cross-linking, comparison edits, and summary edits.

The supported production path is only `post-test-export → post-test-finalize`.
The general `python -m bus_benchmark audit-sample` command is a sampling-only
diagnostic that exposes method/platform fields; it is not a reviewer assignment,
cannot produce human gold, and cannot enter the formal post-test workflow.

## Deferred work

MetaDrive uses the implemented built-in sequence-block-token platform contract.
Its runtime/observer path is implemented and source-tested, with independent
review complete; the concrete tested-method adapter and real method outputs
have not been provided. Source-bound review packets, capability labels,
calibration, and the final 13 hash-bound protocol subjects are therefore not
created or counted by this CARLA-first workflow. Its six current machine
decisions are a crosswalk into existing platform/protocol subjects or automatic
gates and add no standalone human-gold subject.
