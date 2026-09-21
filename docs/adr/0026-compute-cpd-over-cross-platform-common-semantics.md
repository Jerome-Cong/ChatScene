---
status: accepted
---

# Compute CPD over cross-platform common semantics

Because a full method-by-platform crossover is unavailable, version 0.1 does not claim to causally eliminate simulator effects. It instead reports `CPD_common`, a descriptively comparable diversity measure over a shared semantic projection of CARLA and MetaDrive artifacts.

The common projection retains normalized actor types, roles, and semantic actions; ego-centric lane-topological relations; shared road-context categories; and event types, trigger relations, and temporal partial orders. It excludes native map identifiers and seeds, blueprints and visual assets, raw coordinates and map scale, simulator behavior APIs, dynamics and controller parameters, time steps, rollout trajectories, and sampling variation.

## Eligibility and coverage

For candidate query `q`, let `E_q = 1` only when:

1. at least one oracle-permitted diversity dimension can be expressed, deterministically extracted, and validated on both CARLA and MetaDrive; and
2. the core constraints needed to establish preservation can be judged consistently on both platforms.

Eligibility is frozen before method outputs are observed. Ineligible queries receive `CPD_common = N/A`, not zero. Coverage is reported separately for the 76 partial and 76 vague queries and jointly over all 152 candidates:

\[
Coverage_t = \frac{\sum_{q \in t}E_q}{76}, \qquad
Coverage_{all} = \frac{\sum_q E_q}{152}.
\]

Every actor-bound dimension freezes one platform-neutral target signature. The
extractor receives only the native artifact and emits all anonymous observable
candidates with an actor class; it never receives the query, oracle, role label,
or simulator-local track ID. The evaluator then binds the policy signature only
when exactly one candidate matches. Missing or multiple matches fail closed and
make that output ineligible for diversity credit. A static pre-freeze check
requires every eligible policy to route through a matching anonymous candidate
emitter registered for both CARLA and MetaDrive, and requires the oracle's
minimum target-class cardinality to be exactly one for every actor-bound
dimension. The unique `ego_bus` target is established by the ego gate; a
missing or intrinsically multi-actor target is excluded before coverage is
frozen.

## Score

For an eligible query and its five finalized outputs, extract each output's set `P_i` of oracle-approved common semantic diversity atoms. Let `I_i = 1` only when output `i` passes the ego gate, preserves every common core requirement, violates no common forbidden atom, and every core verdict is resolved either by deterministic evidence or by an exact hash-bound response from the frozen calibrated Judge. Define

\[
d_{ij} = 1 - \frac{|P_i \cap P_j|}{|P_i \cup P_j|},
\]

with `d_{ij} = 0` when both sets are empty, and

\[
D_{common,q} = \frac{1}{10}\sum_{i<j}I_i I_j d_{ij}.
\]

The denominator remains all ten output pairs. The final query score is

\[
CPD_{common,q} = D_{common,q} \times \overline{SRS}_{common,q},
\]

where `SRS_common` is the CPD-only preservation score over requirements consistently judgeable on both platforms; it does not replace the benchmark's full SRS.

## Consequences

- Partial and vague scores are macro-averaged separately over frozen statistical
  intent clusters, so duplicate query clusters do not receive extra weight.
  Precise queries report stability rather than rewarded diversity.
- The result envelope reports partial, vague, and joint macro means together
  with partial, vague, and joint coverage, per-query results, and precise-query
  mean pairwise fingerprint distance. It binds the oracle, coverage asset,
  roster, semantic evidence, semantic scores, responses, Judge responses,
  evaluator source, freeze, and generation chain by SHA-256.
- Method or generation failures reduce `CPD_common` and never reduce coverage or remove pairs from the denominator.
- Raw CARLA Town IDs and MetaDrive procedural-map seeds do not count; only a permitted change in their projected common road semantics can count.
- CARLA and MetaDrive extractors are frozen and validated with paired fixtures in which equivalent scenes produce identical fingerprints and controlled semantic perturbations produce the expected distance.
- A legitimately Judge-routed core atom is not excluded merely because it was not deterministically decidable; unresolved atoms still fail preservation.
- Results are described as platform-invariant semantic CPD, not proof that platform effects have been completely eliminated.
