# MetaDrive sequence-block-token platform track

## Scope and status

This is a benchmark-owned platform adapter outside ChatScene. It uses no real
map and does not modify `/home/shijie20/CodeSpace/mdsn/metadrive`. The platform
track can parse, materialize, statically validate, and natively step a strict
`metadrive_pg_block_scene_v0.1` artifact. It is not a scene-generation method:
the evaluated MetaDrive method, adapter entrypoint, environment bundle, rosters,
and generated responses are still absent.

Current split conclusion:

- platform token/runtime track: implementation `GO` after independent review;
- concrete evaluated-method adapter: `NO-GO` until a method is selected and
  returns the strict artifact directly;
- formal benchmark admission: `NO-GO` until the checked draft
  source/environment bindings and required human gold are frozen.

## Source-backed contract

The inspected source is repository
`/home/shijie20/CodeSpace/mdsn/metadrive`, revision
`85e5dadc6c7436d324348f6e3d8f8e680c06b4db`, described as
`MetaDrive-0.4.3-32-g85e5dadc`.

The native runtime has its own provenance gate; it does not rely on the
semantic-extractor environment manifest. The checked-in draft binds the
`scenarionet` Python 3.9.23 executable, its complete 285-distribution inventory,
the actual imported
`/home/shijie20/CodeSpace/mdsn/metadrive/metadrive/__init__.py`, and the clean
git-tracked `metadrive/` subtree at that revision. The subtree binding covers
1,371 tracked files, 72,607,504 bytes, and digest
`2f3feca22ca46c33221c0fa943b76b232d1e3814ae3250b83716dca0fda942c6`.
Untracked or modified content under the subtree fails closed. The runtime
configuration is checked before execution, and every runtime record repeats the
environment, import-path, revision, clean-worktree, and source-byte checks after
execution. Each Compile/SV/NE request also carries the exact module and source
summary; the parent re-probes the full dependency set immediately before and
after every stage, while the worker imports MetaDrive itself and checks
`metadrive.__file__`, its hash, and byte count against the request. The
MetaDrive worker environment is deliberately empty in v0.1, so an unbound
`PYTHONPATH` cannot redirect the import.

This fail-closed stage boundary repeatedly scans 1,371 files (72.6 MB) and the
285-distribution environment inventory. It is intentionally conservative and
adds substantial I/O at full scale; wall time must be measured during the future
MetaDrive development pilot before scheduling 1,260 test outputs.

- `BIG.generate` treats a string as `block_sequence`, prepends
  `FirstPGBlock.ID`, and resolves each subsequent character through the live
  block distribution (`metadrive/component/algorithm/BIG.py`, lines 68–90 and
  105–129).
- `PGBlockDistConfig` limits lane count to `1..5` and maps a one-character block
  ID to the registered class (`component/algorithm/blocks_prob_dist.py`, lines
  1–3 and 44–59).
- `PGMap._big_generate` passes lane count, width, exit length, source seed, and
  token sequence to `BIG` (`component/map/pg_map.py`, lines 47–79).
- both fork implementations raise `ValueError("Bug exists in this block...")`
  from their construction path (`component/pgblock/fork.py`, lines 22–28 and
  156–178). Therefore `F` and `f` are not executable tokens even though their
  classes are importable.
- `ParkingLot` asserts that the previous block has exactly one positive lane
  (`component/pgblock/parking_lot.py`, lines 24–31). Therefore token `P` is valid
  only with `lane_num = 1`.
- `agent_policy` is instantiated by `VehicleAgentManager` for the native ego
  (`manager/agent_manager.py`, lines 37–52). The built-in `IDMPolicy` produces
  steering plus one signed longitudinal action (`policy/idm_policy.py`, lines
  225–268); the vehicle interprets those as normalized steering and
  throttle/brake, not direct SI acceleration commands
  (`component/vehicle/base_vehicle.py`, lines 472–520).
- `top_down_length` and `top_down_width` affect only the top-down representation
  (`envs/base_env.py`, lines 156–166; `component/vehicle/base_vehicle.py`, lines
  1003–1009). `XLVehicle` remains physically `5.74 × 2.30 × 2.80 m`
  (`component/vehicle/vehicle_type.py`, lines 53–84).

The admitted executable tokens are `$`, `B`, `C`, `O`, `P`, `R`, `S`, `T`,
`U`, `X`, `Y`, `r`, and `y`. Native probes materialize each as exact block IDs
`["I", token]`; the same probe records native failure for `F`, `f`, and `P` at
lane count two. Longer compositions are still required to pass native SV: token
membership is necessary, not sufficient, for a geometrically constructible
sequence.

## Replayable native probe evidence

The development artifact
`benchmark_artifacts/evidence/metadrive_native_probe_evidence_v0_1.json` binds
the draft platform runtime configuration, token registry, complete Python
environment manifest, probe runner, native observer, imported MetaDrive module,
and clean tracked source tree. It records all 13 admitted single-token
materializations, the three expected native failures (`F`, `f`, and `P` with
two lanes), and one native `XLVehicle` reset plus zero-action step on token `S`.

Recreate or exact-verify it under the bound interpreter with:

```bash
MPLCONFIGDIR=/tmp \
/home/shijie20/micromamba/envs/scenarionet/bin/python3.9 \
  scripts/metadrive_track_probe.py \
  --registry benchmark_configs/methods/metadrive_pg_token_registry_draft.json \
  --platform-config benchmark_configs/platforms/metadrive_runtime_draft.json \
  --source-repository /home/shijie20/CodeSpace/mdsn/metadrive \
  --development \
  --output benchmark_artifacts/evidence/metadrive_native_probe_evidence_v0_1.json
```

The output path is immutable: identical canonical bytes are reusable and
different bytes are rejected. This is platform development evidence only. It
does not prove a tested-method run, arbitrary long-sequence constructibility, a
30-second NE rollout, `IEC_exec`, use of real maps, or any human-gold decision.

## Stage semantics

- Compile: strict UTF-8 JSON parse, exact schema, registry hash, executable
  token/class equality, ego/actor/trajectory bounds, and map-free contract.
- SV: native `MetaDriveEnv.reset`, exact generated block IDs, finite PGMap
  geometry, map-grounded actors and semantic regions, and no initial actor
  overlap under the approved `5.33 × 2.10 m` benchmark projection. The native
  XLVehicle collision body is larger (`5.74 × 2.30 m`), so a boundary case can
  pass the proxy-box check without proving native-body non-overlap. The five
  common protocol labels `0..4` repeat this deterministic
  confirmation while preserving the submitted artifact's `map_seed`; they are
  not five independent road samples.
- NE: one 30-second rollout with a fixed query-blind `FrozenBusIDMPolicy` and
  replayed non-ego trajectories. The trace records native object states and
  normalized actions. `ACC_FACTOR` and `DEACC_FACTOR` are dimensionless IDM
  factors, and the signed longitudinal output is normalized throttle/brake,
  not an SI acceleration command.
- Semantic evidence: the native observer materializes PGMap geometry and emits
  strict JSON. Submitted trajectories are specification evidence; only the NE
  trace is execution evidence. Neither is allowed to read query/oracle metadata.

`IEC_spec` remains the primary generation metric. `IEC_exec` is diagnostic
because a fixed lane-following policy does not understand docking, dwelling, or
query-specific departure intent.

## Required future method interface

The eventual evaluated method must provide exactly one frozen main
configuration shared by every metric. Its adapter must:

1. receive only `query_text` for each of five pre-registered repetitions;
2. return a strict `metadrive_pg_block_scene_v0.1` document or an explicit
   unsupported/failure disposition;
3. emit only the executable token whitelist and satisfy per-token map
   constraints (`P` requires one lane);
4. express actors, replay trajectories, and annotation-only semantic regions in
   the returned artifact itself;
5. permit only the benchmark's three-leaf ego proxy finalizer; no road/event
   translation, semantic repair, Event Plan, or query-conditioned controller is
   allowed.

Before admission, the method owner must supply its command or callable,
interpreter and complete dependency lock, source snapshot/bindings, bounded
stdout/stderr/artifact limits, credential injection contract if applicable, and
the 48-query/252-query rosters. Until those exist, no MetaDrive SRS/ARC/RSC/IEC,
SV/NE, RQS, UQH, or CPD result can be reported for a tested method.

## Human review boundary

The machine proposals are listed in
`benchmark_artifacts/drafts/metadrive_track_draft_decisions.json`. Its six items
are machine-only crosswalk entries, not a standalone review subject and not human
gold. Each item has exactly one formal destination: an existing platform review
subject, one of the final protocol decision subjects, or an automatic gate.
Human gold is created only once at a human-reviewed destination; automatic gates
create none. The crosswalk therefore adds zero standalone gold records, and the
current completed admissible human-gold count remains zero.
