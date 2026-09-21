---
status: accepted
---

# Require method-independent, platform-neutral CPD

The benchmark scope includes CARLA and MetaDrive. CPD rewardable dimensions are defined by a shared benchmark ontology and do not vary by evaluated method. Primary cross-platform CPD must not reward differences caused only by simulator-native map identifiers, asset catalogs, coordinate systems, or behavior APIs.

## Consequences

- A method cannot receive a reduced or custom opportunity set because it supports fewer diversity dimensions.
- Raw CARLA Town identifiers, MetaDrive procedural-map seeds, vehicle blueprints, rendering assets, and native controller primitives are excluded from the primary CPD fingerprint.
- Platform capability and platform-specific diversity are recorded separately from method CPD.
- A full method-by-platform crossover is not feasible for this benchmark. Method and platform effects therefore remain statistically confounded and cannot be claimed as causally separated.
- ADR-0026 supplies the shared-semantic projection that removes platform-native reward dimensions and supports descriptive cross-platform comparability without claiming complete platform-effect elimination.
