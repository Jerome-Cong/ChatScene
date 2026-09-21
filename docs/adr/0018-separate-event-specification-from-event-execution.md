---
status: accepted
---

# Separate event specification from event execution

Version 0.1 reports event evaluation in two uncombined layers. `IEC_spec` is the primary generation metric and checks whether the finalized artifact correctly specifies required actor bindings, events, grounded targets, triggers, completion conditions, and temporal order; `IEC_exec` is the Event Realization Rate observed under the fixed Reference Ego Controller and remains a Driving Diagnostic.

## Consequences

- An event that occurs accidentally without a correct generated specification cannot earn `IEC_spec` credit.
- A correct specification that fails to execute remains distinguishable from a generation error rather than being collapsed into one score.
- SRS event, temporal, and normative/risk atoms use specification-level evidence so ego-policy performance cannot alter the primary generation ranking.
- `IEC_spec` and `IEC_exec` are neither multiplied nor averaged into a composite IEC.
- A native trajectory alone is insufficient evidence for role-, lane-, region-, trigger-, and completion-sensitive events. Until a frozen query-blind event observer exists, `IEC_exec` is reported as `unavailable` without a numeric score; this does not block `IEC_spec`, SV, or NE.
