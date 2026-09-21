---
status: accepted
---

# Evaluate unchanged generator artifacts without an Event Plan

Version 0.1 does not add an Event Plan or layered event executor because the benchmark must not change ChatScene's prompts, extraction format, retrieval content, composition template, or Scenic output contract. `IEC_spec` is inferred from the finalized Scenic artifact that the existing generator already produces, while `IEC_exec` remains a policy-dependent diagnostic under a fixed query-blind ego controller.

## Consequences

- The benchmark harness may feed `query_text`, preserve identifiers, validate artifacts, and extract evidence, but it cannot require or inject new generated event-plan fields.
- Missing or unreachable bus-event semantics receive low `IEC_spec` rather than being supplied by an adapter or controller.
- The artifact-conditioned layered controller accepted in ADR-0017 is not implemented.
- This decision concerns Event Plan instrumentation and does not by itself revisit the separately accepted Ego Bus Proxy substitution.
