---
status: accepted
---

# Preserve the generator except for the ego proxy

Version 0.1 keeps ChatScene's prompts, extraction format, retrieval content, generation stages, composition semantics, and Scenic output contract unchanged. The sole generated-content exception is a frozen, query-independent substitution of the ego blueprint with `vehicle.chevrolet.impala` and the ego Scenic footprint with length 5.33 m and width 2.10 m.

## Consequences

- Query ingestion, identifier tracking, artifact preservation, validation, and scoring may be implemented in an external benchmark harness.
- The ego substitution is identical for every query and cannot inspect query intent or evaluation metadata beyond the public invariant that this library uses an ego bus.
- No Event Plan, new semantic field, prompt instruction, retrieval example, or query-specific repair may be added to improve generated content.
- Any missing actors, road structures, relations, behaviors, or event semantics remain observable method limitations and are scored as generated.
