---
status: accepted
---

# Finalize one artifact and preserve failures

Each ChatScene generation run finalizes exactly one Scenic artifact before validation. The evaluator compiles and records the artifact but does not return compiler feedback for regeneration, repair it, or replace it with another attempt. ChatScene version 0.1 does not implement the currently absent compiler-feedback repair loop.

## Consequences

- A compile failure is terminal for that generation run, and its artifact, error, and stage status remain immutable experimental evidence.
- Status must be recorded after the actual validation stage rather than writing success before compilation; artifacts must not be moved or overwritten in a way that loses their original identity.
- Other methods may retain bounded Method-native Repair only when it is part of their declared frozen configuration and every model call is recorded; the benchmark harness never supplies repair.
