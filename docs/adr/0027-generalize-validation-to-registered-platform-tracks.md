---
status: accepted
---

# Generalize validation to registered platform tracks

The benchmark contains CARLA and MetaDrive methods without requiring every method to run on both platforms. A benchmark output is therefore a finalized platform-native scene artifact. Compile Success, SV, and NE retain the same conceptual stages but use a frozen track adapter and native evidence for the artifact's registered platform.

## Consequences

- ChatScene remains a CARLA/Scenic method. Its prompts, extraction, retrieval, composition, and Scenic output contract are unchanged except for the already approved uniform ego proxy substitution.
- CARLA support and the method-independent MetaDrive token-only platform track
  are implemented in external benchmark adapters. The concrete evaluated
  MetaDrive method adapter remains deferred; no MetaDrive fields or compiler are
  inserted into ChatScene.
- Each admitted track registers an existing ego vehicle proxy with the common
  5.33 m by 2.10 m top-down footprint. CARLA uses Scenic Car plus
  `vehicle.chevrolet.impala`; MetaDrive uses its existing `XLVehicle` with a
  5.33 m by 2.10 m top-down projection. Its native physical body remains 5.74 m
  by 2.30 m and is recorded as a proxy limitation.
- Compile Success means native parse or load; SV uses seeds zero through four and at most 2,000 native sampling iterations; NE advances one lowest-valid-seed realization for 30 seconds of simulated time.
- SV, NE, and `IEC_exec` are reported with platform labels. They cannot be used to claim causal platform-adjusted method superiority because method and platform are not fully crossed.
- `CPD_common` alone projects diversity into the accepted cross-platform common semantic space. Platform-native maps, assets, dynamics, and controller behavior remain outside that score.

The platform-level MetaDrive runtime is executable on the current host, but its
draft status and the absence of a tested-method adapter mean that no formal
MetaDrive method run or cross-platform result is claimed.
