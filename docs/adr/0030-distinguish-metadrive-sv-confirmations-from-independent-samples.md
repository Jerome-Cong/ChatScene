---
status: accepted
---

# Distinguish MetaDrive SV confirmations from independent samples

For a finalized MetaDrive PG artifact, the submitted block sequence and
`map_seed` remain immutable. The common five-attempt SV budget is retained, but
indices `0..4` mean fresh deterministic validation confirmations of that exact
artifact. They are not independent road samples and cannot contribute to CPD.

MetaDrive's `IDMPolicy.ACC_FACTOR` and `DEACC_FACTOR` are recorded as
dimensionless controller factors. Its second action component is normalized
throttle/brake in `[-1,1]`; neither the factors nor the action are reported as SI
acceleration bounds.

## Consequences

- MetaDrive SV should normally be all-pass or all-fail across the five fresh
  attempts, except for a separately visible transient infrastructure failure.
- Changing a validation-repetition index never changes the submitted map seed.
- Cross-platform runtime reports disclose the different CARLA and MetaDrive SV
  sampling semantics instead of implying identical randomness.
