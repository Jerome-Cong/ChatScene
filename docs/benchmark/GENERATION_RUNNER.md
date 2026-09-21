# Immutable CARLA generation runner

The generation runner is an external benchmark harness. It does not change
ChatScene's generation architecture. Each invocation receives only the exact
`query_text`, runs once in a fresh private directory, and consumes one of the
five pre-registered repetitions even when it fails or times out.

## Formal admission boundary

A formal run requires all of the following:

- a `frozen` method config and `confirmed` query roster;
- a freeze manifest which binds the query library, roster, method config, and
  any platform registry required by the platform;
- a `frozen` implementation bundle whose self-hash covers a sorted list of
  live implementation files plus distinct method and harness environment
  manifests;
- exact live hash/size matches for both interpreters, both environment
  manifests, every generation-harness Python source, every benchmark JSON
  schema, and every registered method source file;
- the adapter constructed from the method config. Supplying a Python adapter
  object is development-only and is rejected in formal mode.

The method environment manifest describes the interpreter which executes the
generated method. Its executable must be the adapter executable. The harness
environment manifest independently describes the interpreter running
`python -m bus_benchmark`; its executable must equal the live harness
interpreter. Both files must be distinct and `frozen` in formal mode, and both
executables must appear in the implementation file list.

For each Python environment, admission invokes the bound executable with a
fixed isolated probe. The declared Python version and the complete, canonical,
sorted installed-distribution inventory must exactly equal the live result.
An empty, partial, forged, or stale dependency list fails closed. Formal
command adapters currently require a probed Python method environment;
admission for a genuinely native command is deferred until it has an equally
strict platform-independent runtime probe.

The harness part of a formal implementation bundle includes all
`bus_benchmark/**/*.py` files and every schema in the explicit active allowlist
returned by `supported_schema_paths()`. It deliberately excludes legacy
A/B-review, adjudication, and mandatory-second-review schemas that remain on
disk only for historical traceability. This covers the actual
`python -m bus_benchmark` entry path, CLI dispatch, adapters, validators,
finalizers, and the active schemas they consume. Omitting any one of those
bindings rejects the bundle before generation; merely placing another schema
under `benchmark_configs/schemas/` does not make it formally admissible.

For the legacy ChatScene adapter, every regular source file copied into the
formal snapshot must be in the implementation bundle; there is no query-file
or pre-existing-output exception. Immediately after copying, and before the
query is written or the method process starts, the adapter enumerates the
private snapshot and requires the exact bundle-bound relative path, byte count,
and SHA-256 set. A missing, extra, changed, or symlinked file fails the
repetition before method execution. Only after that attestation may the private
query copy be overwritten and private generated-output paths be prepared. Its
Python interpreter and `bwrap` executable must also be absolute, regular,
non-symlink files present in that bundle, and the method environment must be
Python. Draft configs may register incomplete source or dependency lists for
development, but cannot be frozen or admitted as formal.

The active CARLA draft uses the repository-local `chatscene` uv environment
(Python 3.8.10). Because the existing `chatscene/bin/python` is a symlink and
formal executable identities must be regular files, the bundle binds the
regular `chatscene/bin/python-frozen` launcher. A same-filesystem hard link to
`/usr/bin/python3.8` was unavailable on this workspace mount, so the launcher is
an audited byte-identical regular copy; the existing uv symlinks are unchanged.
The environment manifest also binds 67 startup-control files: `pyvenv.cfg`,
all `.pth` files, local modules/packages imported or inspected by those hooks,
`sitecustomize.py`/`usercustomize.py` when present, the SafeBench editable
finder, and editable `direct_url.json` controls. Each legacy invocation copies
those controls and the exact CARLA egg through opened descriptors into a
private per-run snapshot, mounts only the copies, and revalidates both source
and copy before and after execution; drift invalidates the result.

The checked-in CARLA draft is
`benchmark_configs/methods/chatscene_carla_draft.json`. The runner contract,
ego-only finalizer, bounded adapter, implementation-bundle checks, response
chain, and replay validator are implemented and covered by directed tests.
The config deliberately remains `draft`: the 173-distribution dependency lock
and startup controls are complete, but the full Python runtime tree (all
third-party package code, standard-library files, and loaded shared libraries)
is not yet closed. Formal validation therefore fails closed even if the draft
status were edited. Complete snapshot bindings, credential injection,
retrieval-model cache, roster confirmation, and final status also remain open.
Those decisions must not be silently supplied by the benchmark harness.

The checked-in config and both CARLA rosters now share method ID
`chatscene_carla_v0_1`, and a smoke test constructs the complete 48-query and
252-query grids without entering the method process. That is a harness preflight,
not evidence that ChatScene generated a scene. On the current host, the unchanged
legacy entrypoint is still blocked before generation by unavailable NVML/GPU
access. The repository-local environment now imports `sentence_transformers`,
and the exact CARLA 0.9.13 Python egg is hash-bound and importable. Its existing
LLM wrapper still overwrites environment-based API credential injection with a
placeholder.

Inside `bwrap`, the mutable checkout is hidden before the uv environment is
mounted read-only at `/run/chatscene-benchmark/python-env`; it is never exposed
again at its host checkout path. Each bound Python path entry is mounted by
stable index under `/run/chatscene-benchmark/python-path/`, preserving the egg
filename required by CARLA, and extraction uses the private writable
`/run/chatscene-benchmark/python-eggs` cache. `PYTHONPATH` contains only these
fixed mounts plus the immutable per-run repository snapshot.

The private HOME/cache deliberately prevents use of an unregistered host
SentenceTransformer cache. The current adapter/config has no bundle-bound
read-only model-cache mount, and this host has no local
`sentence-transformers/sentence-t5-large` cache to register. Consequently the
current CARLA generation configuration is execution-NO-GO even if the earlier
NVML, runtime-library, CARLA-import, and credential blockers are resolved.
Before a formal run, the external adapter must gain a read-only mount for an
audited cache whose every file is in the implementation bundle (or the method
environment must provide an equivalently frozen local model installation). An
implicit per-run download is not a reproducible freeze.

Generation itself also requires an API-responsive CARLA server; this is not
only an NE concern. The unchanged `save_scenic_code` path constructs
`ScenicSimulator`, whose CARLA simulator constructor immediately creates a
client and loads or generates the configured world. Installing the Python
client without a reachable server, bound endpoint, and available map therefore
still cannot produce a terminal Scenic artifact on this host.
Resolving these method-environment issues would change ChatScene behavior or its
environment and therefore remains an explicit method-owner task under the
no-architecture-change boundary.

There is no checked-in MetaDrive method config because the concrete method,
entrypoint, and environment have not been selected. Creating a placeholder
would make provenance misleading.

## Process isolation and bounds

Adapter commands are shell-free. `HOME`, cache paths, and bytecode behavior are
redirected into the private work directory. Only this fixed passthrough set is
configurable:

`PATH`, `LANG`, `LC_ALL`, `LD_LIBRARY_PATH`, `CUDA_VISIBLE_DEVICES`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `HTTP_PROXY`, `HTTPS_PROXY`, and
`NO_PROXY`.

`HOME`, `PYTHONPATH`, arbitrary variables, source/config symlinks, output-root
symlinks, and artifact symlinks are rejected. Stdout/stderr are drained through
bounded readers; each method config freezes stdout, stderr, artifact, and time
limits. Limit overflow is a terminal failed repetition, never a retry.

Host environment passthrough is development-only. Formal admission rejects any
non-empty `environment_passthrough`, because recording only a variable name does
not bind its value: a changed model endpoint, runtime-library path, CUDA device,
locale, or proxy could otherwise change the method under the same config hash.
The checked ChatScene draft still uses host passthrough and is therefore
execution-NO-GO for formal runs. A future frozen config must put every
non-secret value directly under a hash-bound environment contract and obtain
credentials through an externally attested secret-store identity. Secret values
or reversible identifiers must never be written to configs, logs, or evidence.

The legacy adapter runs through the bundle-bound `bwrap` binary in a new mount
namespace and an explicitly unshared PID namespace. The host root is
read-only, the per-run snapshot is the only writable repository mount, the
live ChatScene source root is masked, `/tmp` is private, and `/proc` is mounted
for the private PID namespace. `--die-with-parent`, a new session, bounded
process-group cleanup, and the PID namespace prevent escaped descendants from
surviving a terminal run. Network is intentionally not unshared because the
unchanged method may need its configured model endpoint.

Inside that sandbox, inherited `PYTHONPATH` is discarded. It is replaced with
only the per-run repository snapshot and its `Scenic/src` directory. This is
required because the legacy environment contains editable SafeBench/Scenic
links back to the live checkout; the benchmark must import the hash-bound
snapshot or fail, never silently execute mutable live source.

## CARLA artifact finalization

The CARLA artifact remains Scenic text. The sole post-generation policy may
set the ego proxy to:

- blueprint `vehicle.chevrolet.impala`;
- length `5.33 m`;
- width `2.10 m`.

`EGO_MODEL` must have exactly two code references: its single assignment and
the single ego blueprint clause. A non-ego reference is rejected before any
rewrite. The semantic skeleton verifies that no other Scenic content changes.

## MetaDrive platform contract versus method adapter

The method-independent MetaDrive platform contract is implemented. It defines a
strict `metadrive_pg_block_scene_v0.1` artifact, an ego-only finalizer, a
source-bound executable token registry, native PGMap materialization, fixed
query-blind runtime policy, and semantic observation. Roads are created only
from MetaDrive's built-in sequence-block-token generator; no real-map input is
accepted.

This does not provide a MetaDrive generation method. There is intentionally no
checked-in MetaDrive method config, query roster, entrypoint adapter, or method
environment bundle until the evaluated method is selected. That future adapter
must receive only `query_text` and return the strict PG artifact directly; the
benchmark may change only its three ego proxy leaves and may not translate,
repair, or infer road/event semantics after generation.

The platform token registry is still a draft pending human review. Its native
probe excludes `F`/`f` because this MetaDrive revision raises from both fork
constructors, constrains `P` to `lane_num = 1`, and limits lane count to `1..5`.
Therefore “class can be imported” is never used as evidence that a generator may
emit the corresponding token. No real method generation result is claimed.

## Outputs and complete-chain validation

The output directory contains:

- `response.jsonl`: complete roster-aligned terminal responses (written by the CLI);
- `evidence.jsonl`: per-run raw artifact and finalization evidence (written by the CLI);
- `generation_run_manifest.json`: hashes every response/evidence pair and binds
  the library, canonical roster, method config, and implementation bundle;
- `runs/<run_id>/`: immutable stdout, stderr, raw/final artifacts, minimal diff,
  response, and generation evidence.

Empty streams are still persisted and hashed. `--resume` verifies all hashes,
replays the finalizer, and reuses exact bytes; it never overwrites a terminal
run.

Downstream scoring and UQH must call the only admission API:

```python
validate_generation_response_chain(
    library_path,
    roster,
    method_config_path,
    output_root,
    responses=responses,
    evidence=evidence,
    manifest=manifest,
    development=False,
)
```

The validator re-hashes the implementation and registry, reconstructs the
exact query-by-five grid, verifies every stored descriptor and envelope,
replays ego finalization, checks optional JSONL indexes, and validates the run
manifest self-hash before returning a score-safe summary.

### Score and CPD admission

Formal `score` and `cpd` commands require the same three generation-chain
arguments:

- `--library`: the exact frozen test query library;
- `--method-config`: the one frozen main method configuration shared by all
  metrics;
- `--generation-dir`: the committed generation output directory.

Their `--responses` path must be exactly
`<generation-dir>/response.jsonl`. The library and method config must be
registered in the same verified freeze as the roster and oracle. Both commands
load `<generation-dir>/evidence.jsonl` and
`<generation-dir>/generation_run_manifest.json` and pass all three indexes to
`validate_generation_response_chain` before reading semantic evidence or
computing a score. A copied or forged response descriptor, changed artifact,
missing repetition, implementation drift, or manifest mismatch therefore
fails before metric evaluation.

Every formal semantic metric command also accepts Judge evidence only through
`--judge-run-dir`. Bare `--judge-responses` remains a development convenience
and is rejected in formal mode. The run directory binds the frozen runner,
environment, source files, canonical request bundle, per-item request/stdout/
stderr bytes, execution index, and exact deterministic-unknown atom coverage.

Development mode may omit all three arguments only with the explicit
`--allow-unverified-generation-chain` acknowledgement. Partial chains are
always rejected, and this acknowledgement is incompatible with formal mode or
with supplied chain arguments. Unverified score records retain a null formal
freeze binding and the CLI summary reports
`generation_chain_verified=false`. CPD output is an envelope with
`generation_chain`, including the verification flag and committed generation
manifest/implementation hashes. Formal CPD additionally requires the frozen
`--coverage` asset. The schema-validated envelope reports partial, vague, and
joint `CPD_common` macros and coverage, precise-query stability, per-query
results under `queries`, and hashes of every scoring input.

### Formal semantic-evidence extraction

`extract-evidence` is the only formal path from committed generation responses
to `semantic_evidence.jsonl`. It verifies the frozen manifest and assets, the
exact roster, and the committed generation chain before running the frozen
benchmark-owned extractor. It then re-executes that extractor through the
provenance validator and writes one immutable JSONL file. Empty, partial,
off-roster, tampered, or already-different outputs fail closed.

The extractor is **payload-blind under the trusted frozen benchmark-owned
extractor assumption**: its worker receives only platform, terminal status,
disposition, artifact bytes, and explicitly frozen platform registries, but no
query ID or text, oracle atom, or Judge verdict. Its subprocess is not a general
filesystem sandbox, so the benchmark does not claim stronger isolation than
this frozen-code and payload boundary.

## Commands

Read-only readiness report for the checked-in CARLA test roster:

```bash
./chatscene/bin/python-frozen -m bus_benchmark generation-preflight \
  --library query_lib/bus_ego_topdown_2d_query_library_v0_2.jsonl \
  --roster benchmark_artifacts/drafts/chatscene_carla_test_roster_draft.json \
  --method-config benchmark_configs/methods/chatscene_carla_draft.json
```

After a formal generation chain has committed, materialize semantic evidence
with the same frozen library, roster, and method configuration:

```bash
./chatscene/bin/python -m bus_benchmark extract-evidence \
  --responses <generation-dir>/response.jsonl \
  --output <results-dir>/semantic_evidence.jsonl \
  --roster <frozen-roster.json> \
  --freeze-manifest <freeze-manifest.json> \
  --library <frozen-test-library.jsonl> \
  --method-config <frozen-method-config.json> \
  --generation-dir <generation-dir>
```

Materialize the complete Judge request set, then execute the provider-neutral
frozen runner. The runner contract is one canonical request on stdin and one
strict `judge_output` JSON object on stdout:

```bash
./chatscene/bin/python -m bus_benchmark judge-request \
  --responses <generation-dir>/response.jsonl \
  --evidence <results-dir>/semantic_evidence.jsonl \
  --roster <frozen-roster.json> \
  --freeze-manifest <freeze-manifest.json> \
  --library <frozen-test-library.jsonl> \
  --method-config <frozen-method-config.json> \
  --generation-dir <generation-dir> \
  --output-dir <results-dir>/judge-requests

./chatscene/bin/python -m bus_benchmark judge-run \
  --request-dir <results-dir>/judge-requests \
  --responses <generation-dir>/response.jsonl \
  --evidence <results-dir>/semantic_evidence.jsonl \
  --roster <frozen-roster.json> \
  --freeze-manifest <freeze-manifest.json> \
  --library <frozen-test-library.jsonl> \
  --method-config <frozen-method-config.json> \
  --generation-dir <generation-dir> \
  --output-dir <results-dir>/judge-run
```

Both directories are atomically published and immutable. A zero-request run is
valid and starts no subprocess. A timeout, non-zero exit, invalid JSON, byte
limit violation, source drift, or credential value in captured output leaves
no partial run directory.

### Judge calibration profiles

Calibration uses the same runner, canonical request, raw stdout, validation,
and diagnostics path as benchmark judging. Create the pre-run context first;
the CARLA-stage command accepts the one 90-item finalizer report directly as
`--human-gold`:

```bash
./chatscene/bin/python -m bus_benchmark judge-calibration-context \
  --profile carla_stage \
  --query-library <dev-library.jsonl> --oracle <confirmed-dev-oracle.jsonl> \
  --roster <carla-stage-roster.json> \
  --human-gold <carla-stage-finalizer-gold.json> \
  --prompt <judge-prompt.txt> --output-schema <judge-output-schema.json> \
  --runner-manifest <judge-runner-manifest.json> \
  --model-id <model-id> --inference-config <inference-config.json> \
  --carla-source-config <carla-source-config.json> \
  --carla-roster <carla-dev-roster.json> \
  --carla-responses <carla-dev-responses.jsonl> \
  --carla-evidence <carla-dev-evidence.jsonl> \
  --output <carla-stage-context.json>

./chatscene/bin/python -m bus_benchmark judge-calibration-request \
  --context <carla-stage-context.json> \
  --output-dir <carla-stage-requests>

./chatscene/bin/python -m bus_benchmark judge-calibration-run \
  --context <carla-stage-context.json> \
  --request-dir <carla-stage-requests> \
  --output-dir <carla-stage-run>
```

`carla_stage` is exactly 90 CARLA items and is diagnostic only, with
`formal_freeze_eligible: false`. `cross_platform_final` uses the same three
commands but requires exactly 90 CARLA plus 90 MetaDrive items and all four
`--metadrive-*` source inputs; only that 180-item profile can bind the formal
freeze assets for context, request manifest, and run manifest.

The request-directory allowlist is `request_manifest.json`,
`request_index.jsonl`, and `requests/<request-sha256>.json`. The run-directory
allowlist is `judge_run_manifest.json`, `execution_index.jsonl`,
`judge_responses.jsonl`, and, per item,
`items/<item-id>/{request.json,stdout.bin,stderr.bin,execution.json}`. Extra or
symlink entries fail validation.

The runner executes only the explicitly bound executable and source-file bytes
from fresh sealed anonymous files per item. For Python, standard-library and
shared-library contents are not claimed immutable by this mechanism; they are
checked only through the current frozen environment manifest and live isolated
environment probe.

The `generation-preflight` command validates the complete query-by-five grid and
reports formal and execution blockers as JSON on stdout, or writes the same
canonical JSON object when `--output` is supplied. It does not construct or call
the method adapter, create a generation output directory, expose query text or
secret values, or consume a repetition. It reports only credential presence and
contract state, never a credential value.

The checked-in CARLA readiness diagnostics can be regenerated without consuming
benchmark work:

```bash
./chatscene/bin/python-frozen -m bus_benchmark generation-preflight \
  --library query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl \
  --roster benchmark_artifacts/drafts/chatscene_carla_dev_roster_draft.json \
  --method-config benchmark_configs/methods/chatscene_carla_draft.json \
  --output benchmark_artifacts/evidence/chatscene_carla_dev_generation_preflight_v3.json

./chatscene/bin/python-frozen -m bus_benchmark generation-preflight \
  --library query_lib/bus_ego_topdown_2d_query_library_v0_2.jsonl \
  --roster benchmark_artifacts/drafts/chatscene_carla_test_roster_draft.json \
  --method-config benchmark_configs/methods/chatscene_carla_draft.json \
  --output benchmark_artifacts/evidence/chatscene_carla_test_generation_preflight_v3.json
```

These are readiness diagnostics, not method-generation evidence or successful
CARLA execution records.

The latest checked-in 2026-07-16 v3 diagnostics cover both 48 development
queries / 240 jobs and 252 test queries / 1,260 jobs. Each reports the same 13
blockers (nine formal and four execution), `formal_ready: false`, and
`execution_ready: false`, while recording that no
adapter was invoked, no repetition was consumed, and no query text or secret
value was disclosed.
The canonical artifact SHA-256 values are
`fc0ba09f12a34ae211b8a017601076332e5c6237a8abd0d3d2d57179bbc2f9bf`
for development and
`1024f0ca34124cc3ba9e296d49df81f36be20b1f49bbb64fcbd6f8de01285d0b`
for test. Both record `harness_running_executable_identity=pass`.
They are run with the exact repository-local Python 3.8 harness interpreter
registered by the draft method bundle. Historical v1/v2 reports used stale
interpreter/config identities and are retained only as historical evidence.
The mechanically regenerable but still non-frozen items are the draft config,
draft roster, incomplete 1/127 snapshot registration, and 10 unbound
environment passthrough names. Both method and harness inventories match all
173 observed distributions, and the single CARLA egg path entry is exact-bound.
The remaining external or method-owned blockers are NVML/CUDA unavailability,
no bundle-bound retrieval-model cache, source-level credential override, and no
CARLA listener on port 2000. `bwrap`, `sentence_transformers`, and `carla`
imports pass.
These observations are diagnostics, not generation evidence.

Development run with the checked-in CARLA draft:

```bash
./chatscene/bin/python-frozen -m bus_benchmark generate \
  --library query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl \
  --roster benchmark_artifacts/drafts/chatscene_carla_dev_roster_draft.json \
  --method-config benchmark_configs/methods/chatscene_carla_draft.json \
  --output-dir benchmark_artifacts/generation/chatscene-carla-dev \
  --development --resume
```

Development scoring bound to those committed outputs uses the same method
config rather than a metric-specific variant:

```bash
./chatscene/bin/python -m bus_benchmark score \
  --oracle <reviewed-dev-oracle.jsonl> \
  --evidence <semantic-evidence.jsonl> \
  --responses benchmark_artifacts/generation/chatscene-carla-dev/response.jsonl \
  --roster benchmark_artifacts/drafts/chatscene_carla_dev_roster_draft.json \
  --library query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl \
  --method-config benchmark_configs/methods/chatscene_carla_draft.json \
  --generation-dir benchmark_artifacts/generation/chatscene-carla-dev \
  --output <semantic-scores.jsonl> \
  --development --allow-draft-oracle
```

For a formal run, remove `--development`, provide `--freeze-manifest`, and use
only fully frozen assets. The runner rejects the current draft config.
