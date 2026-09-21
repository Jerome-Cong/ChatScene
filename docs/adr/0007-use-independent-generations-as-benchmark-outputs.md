---
status: accepted
---

# Use independent generations as benchmark outputs

A benchmark output is one immutable platform-native scene artifact produced by an independent generation run for a benchmark query. Constraint-Preserving Diversity compares semantic projections of multiple independently generated artifacts for the same query; it does not compare multiple simulator realizations sampled from one artifact under different runtime seeds. ChatScene's CARLA-track artifact is a Scenic program.

## Consequences

- Native simulator sampling variability cannot inflate the measured diversity of the query-to-scene method.
- Runtime realizations remain available for Scene Validity, Native Executability, and runtime-stability diagnostics.
- The number and selection of independent generation runs, and the number of runtime realizations per output, remain separate protocol decisions.
