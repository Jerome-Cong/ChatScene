---
status: accepted
---

# Score SV over five bounded sampling trials

Each compiled or loaded supported-query benchmark output receives five SV
attempts under protocol indices 0, 1, 2, 3, and 4. CARLA uses those values as
Scenic sampling seeds with at most 2,000 sampling or rejection iterations.
MetaDrive instead performs five fresh deterministic confirmations of the exact
submitted PG artifact and preserves its immutable `map_seed`, as clarified by
ADR-0030. An attempt passes only when native materialization succeeds and the
benchmark-wide static geometric checks pass. Artifact-level SV is the fraction
of the five attempts that pass.

## Consequences

- A compile failure assigns zero to all five trials without attempting sampling.
- Exhausted trials cannot be retried or repaired; rejection iterations and failure reasons are retained only as diagnostics.
- Unsupported-query responses are outside SV. A complete per-method benchmark
  therefore performs at most 5,700 bounded static validation attempts for 1,140
  supported-query outputs, distinct from full native simulator rollouts.
