# Platform-native runtime runner

The runtime runner is external to ChatScene and does not change ChatScene's
architecture. It consumes only immutable response records and finalized
artifacts; neither query text nor oracle/evaluation metadata is passed to the
platform worker or fixed ego controller. CARLA and the method-independent
MetaDrive sequence-block-token platform track are implemented separately. The
concrete evaluated MetaDrive method adapter is not yet selected, so there is no
claim that a MetaDrive method has generated benchmark outputs.

For every supported response, the runner performs:

1. one native compile/load attempt;
2. five fresh SV attempts labelled `0..4` with the frozen 2,000-iteration
   budget: CARLA samples five seeded Scenic realizations, while MetaDrive repeats
   deterministic validation of the exact submitted artifact and its fixed
   `map_seed`;
3. one NE rollout at the lowest successful SV seed, for at most 30 simulated seconds at `0.1 s` steps.

Unsupported queries are outside SV/NE. A supported generation failure remains in
the denominator and contributes zero. SV is the fraction of all five attempts,
not an any-attempt pass flag. MetaDrive's five values are repeated deterministic
confirmations, not five independent road samples; barring transient
infrastructure failure their outcomes should be all-or-none. NE uses one
query-blind controller and does not implement a bus-event state machine.

## Development execution

```bash
./chatscene/bin/python-frozen -m bus_benchmark runtime-execute \
  --responses benchmark_artifacts/generation/chatscene-carla-dev/response.jsonl \
  --roster benchmark_artifacts/drafts/chatscene_carla_dev_roster_draft.json \
  --library query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl \
  --method-config benchmark_configs/methods/chatscene_carla_draft.json \
  --generation-dir benchmark_artifacts/generation/chatscene-carla-dev \
  --platform-config benchmark_configs/platforms/carla_runtime_draft.json \
  --controller-config benchmark_configs/platforms/carla_controller_draft.json \
  --output-dir benchmark_artifacts/runtime/chatscene-carla-dev \
  --output benchmark_artifacts/runtime/chatscene-carla-dev/runtime.jsonl \
  --development
```

The three generation-chain inputs are shown even in development so runtime is
bound to the committed generation manifest and implementation bundle. Omitting
all three is permitted only in development and records
`generation_chain_verified=false`; it is not score-admissible. Use `--resume`
only to verify and reuse immutable terminal run directories and an identical
JSONL index. Formal execution additionally requires `--freeze-manifest`; the
library, method config, roster, platform runtime config, and controller config
must all be registered in that verified freeze.

Aggregate with the matching response records and freeze inputs:

```bash
./chatscene/bin/python-frozen -m bus_benchmark runtime-aggregate \
  --records benchmark_artifacts/runtime/chatscene-carla-dev/runtime.jsonl \
  --responses benchmark_artifacts/generation/chatscene-carla-dev/response.jsonl \
  --roster benchmark_artifacts/drafts/chatscene_carla_dev_roster_draft.json \
  --library query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl \
  --method-config benchmark_configs/methods/chatscene_carla_draft.json \
  --generation-dir benchmark_artifacts/generation/chatscene-carla-dev \
  --platform-config benchmark_configs/platforms/carla_runtime_draft.json \
  --controller-config benchmark_configs/platforms/carla_controller_draft.json \
  --development
```

## Evidence and validity checks

Every request, stdout, stderr, and NE trace is stored and hashed. The normalized
runtime configuration retains the frozen SHA-256 and byte count for both the
interpreter and worker; each stage copies those expectations into its request
and verifies the exact files and their file identities before and after the
subprocess. A replacement after configuration validation, including a transient
replacement restored to the original bytes, fails closed and the stage is not
published. Captured JSON rejects duplicate keys and non-finite constants. The validator
recomputes SV iterations, NE steps/time/controller/termination, normalized control
actions, and one-ego geometry from those captures. It rejects mixed method grids,
duplicate runs, incomplete formal rosters, changed worker/interpreter/controller
bindings, initial physical overlap, unresolvable types/blueprints, and placement
outside native map bounds.

### CARLA artifact layout and provenance

ChatScene's Scenic output resolves its map through
`dynamic_scenario/../maps/<Town>.xodr`. The generation capture is stored outside
that original directory, so the runtime adapter recreates the relative layout
inside each immutable attempt:

- it copies the finalized Scenic artifact byte-for-byte to
  `native-input/dynamic_scenario/finalized_artifact.scenic`;
- it reads exactly one literal supported `Town` declaration and copies only that
  town's frozen `.xodr` and optional `.snet` into `native-input/maps`;
- it sends both source and staged file bindings to the worker, which verifies the
  bytes again. Compile reports this file as `static_opendrive`: it is the
  OpenDRIVE used by Scenic for static network compilation, not evidence about the
  live CARLA world.

`carla_map_context` binds the source and staged Scenic artifact as well as the
source and staged map catalog. The copies deliberately do not share an inode
with the generation artifact or map source. The worker and record-consistency
validator re-hash every file and re-check the independent-inode relationship.
This is layout reconstruction only: no generated Scenic byte or map byte is
rewritten, and no event semantics are added.

Before CARLA NE, the orchestrator scans the local Linux `/proc` TCP tables and
requires exactly one loopback-reachable `LISTEN` socket on the frozen RPC port.
It resolves that socket to one PID and records its PID, process start-time tick,
socket inode, and `/proc/<pid>/exe` file binding. That executable must be the
hash-bound `CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping` ELF inside the
frozen server tree; `CarlaUE4.sh` is only a launcher and is not an admissible
server identity. After the worker returns, the orchestrator repeats the
attestation and requires the same PID, start time, socket inode, and executable.
The pre/post evidence is stored in the hash-bound `ne.server_session` capture.
No listener, an ambiguous listener, a different executable, or a changed process
makes NE fail without changing Compile or SV.

A successful NE additionally records the CARLA client and server versions,
`world.get_map().name`, and the SHA-256/byte count of
`world.get_map().to_opendrive()`. The live world basename must equal the one
literal declared `Town`, and its OpenDRIVE bytes must exactly equal the staged
`.xodr`. Thus the benchmark keeps two explicit map evidence lines:

- Compile: staged `static_opendrive` used by Scenic's static network;
- NE: server-returned live-world identity and runtime OpenDRIVE bytes.

Development CARLA execution fail-closes on the bound regular Python executable,
its environment controls, CARLA Python egg, Scenic editable source tree,
individual map files, CARLA shipping server ELF, and complete server-distribution
tree. Python caches are the only excluded derived files. All other files and
relative names contribute to the existing tree digests; symlinks are rejected.
The normalized CARLA runtime dependencies are revalidated immediately before
and after every Compile, SV, and NE stage, so a stage cannot silently span two
Scenic, server, map, Python-entry, or manifest states.

This is still development evidence only. Formal CARLA execution additionally
requires a complete closure for the Python runtime tree, standard library,
site-packages, native extension/shared-library dependencies, and loader
identity. The current manifest binds the executable, controls, and distribution
inventory but does not provide that closure, so validation deliberately fails
with `formal CARLA Python runtime requires a complete runtime-tree closure` even
if its status is mechanically changed to `frozen`. `--development` records the
current dependencies as `draft_bound`; the status must not be promoted until the
closure is implemented and frozen under the single-reviewer human-gold protocol.

CARLA uses `FixedIDMPIDBehavior`, preserving the approved
`vehicle.chevrolet.impala` proxy, `5.33 × 2.10 m` footprint, and planning
acceleration limits. It is a query-blind lane-following IDM-PID reference policy;
it does not understand docking, dwell, departure, or merge intent. Those belong
to primary `IEC_spec`; any observed execution is reported separately as
diagnostic `IEC_exec`. The current native trace does not establish all event
roles, lanes, regions, triggers, and temporal completion conditions. Until one
frozen, query-blind event observer is registered, every runtime record therefore
reports `IEC_exec.status = unavailable` and no numeric event score, even when an
NE trace exists.

The current host has demonstrated native Scenic compilation and five-seed static
SV checks. It currently has no unique loopback `LISTEN` process that can be
bound to the frozen shipping ELF, so no live-world evidence or CARLA NE success
is claimed. The checked-in runtime and controller configs remain `draft` until a
server-responsive, fully attested run and the corresponding single complete
human-gold protocol decision are available.

## MetaDrive token-only platform track

MetaDrive road construction uses only `MetaDriveEnv` with
`map_config.type = block_sequence` and a string token sequence. It never accepts
a real-map path, `ScenarioDescription`, native map-feature payload, or a Python
block-class list as method output. The implicit first block `I` is inserted by
MetaDrive and cannot be submitted by a method.

The executable whitelist is source-backed against MetaDrive commit
`85e5dadc6c7436d324348f6e3d8f8e680c06b4db`. Native probes show that tokens
`F` and `f` resolve to classes but their constructors deliberately raise an
error in this source revision, so they are excluded. `P` is admitted only when
`lane_num = 1`; the general lane-count range is `1..5`, matching
`PGBlockDistConfig`. Importable class identity alone is not treated as runtime
support.

Compile validates the strict PG artifact and live token/class registry. SV
materializes a native `PGMap`, checks exact `I + submitted tokens`, finite map
geometry, actor placement, initial overlap, semantic-region grounding, and the
approved ego proxy. Every SV attempt preserves the artifact's `map_seed`; the
protocol labels `0..4` are validation-repetition indices only. NE runs one fixed
query-blind `FrozenBusIDMPolicy`, replays non-ego trajectories, and hashes the
native trace. The observer converts native NumPy map geometry to strict JSON
before semantic extraction.

MetaDrive's `IDMPolicy.ACC_FACTOR` and `DEACC_FACTOR` are dimensionless factors
inside its IDM calculation. The resulting longitudinal action is normalized
throttle/brake in `[-1,1]`, not acceleration in `m/s²`; matching numerical values
to the CARLA planning bounds does not make the physical dynamics equivalent.

The ego uses MetaDrive's existing `XLVehicle`: its physical simulator body is
`5.74 × 2.30 × 2.80 m`, while the approved benchmark top-down projection is
`5.33 × 2.10 m`. The top-down override does not change physical dynamics. This
is an explicit proxy limitation, not a claim that MetaDrive simulates the exact
bus mass, wheelbase, steering system, or footprint.

The checked-in MetaDrive runtime/controller/token-registry files remain drafts.
The runtime draft now independently binds the `scenarionet` interpreter and its
complete 285-distribution inventory, the actual imported `metadrive.__file__`,
and the clean 1,371-file git-tracked source subtree at commit
`85e5dadc6c7436d324348f6e3d8f8e680c06b4db`. The validator repeats those
checks before each execution and when validating its terminal record; the CLI
also rechecks the platform configuration after a grid. In addition, every
Compile/SV/NE request carries the bound module and source summary, the parent
re-runs the full provenance check immediately before and after each stage, and
the worker verifies its own imported `metadrive.__file__` bytes. A changed dependency,
alternate installed MetaDrive package, dirty/untracked source, revision drift,
or source-byte drift fails closed. MetaDrive v0.1 permits no worker environment
overrides, so no unbound `PYTHONPATH` can become an alternate import source.

The stage-level provenance gate repeatedly scans 1,371 files (72.6 MB) and the
285-distribution inventory. This is a known I/O cost, not simulator or method
time; the future MetaDrive development pilot must measure it before scheduling
the full test grid.

This is draft platform provenance, not tested-method evidence. Formal admission
still requires freezing these exact bindings, human platform/protocol gold, and
a concrete method adapter whose finalized output is
`metadrive_pg_block_scene_v0.1`; no real-map input is introduced by this gate.

The separately replayable development gate is:

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

Its strict Schema is
`benchmark_configs/schemas/metadrive_native_probe_evidence.schema.json`. The
evidence covers exactly 13 admitted single-token maps, three normalized expected
native failures, and one `reset + step([0.0, 0.0])` fixture. It is not a
`runtime_record`, does not consume a method/query/repetition cell, and cannot be
used as 30-second NE or IEC evidence.
