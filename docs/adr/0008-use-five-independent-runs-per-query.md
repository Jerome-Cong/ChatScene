---
status: accepted
---

# Use five independent generation runs per query

The formal benchmark performs five independent generation runs for every query-library record. SRS, ARC, RSC, IEC, SV, and NE are aggregated across those runs with dispersion retained; CPD is reported only for partial and vague queries, while repeated precise-query runs measure stability rather than rewarded diversity. Unsupported queries receive the same repetition budget for UQH consistency.

## Consequences

- The 252-record library produces 1,260 benchmark outputs in a complete formal run.
- Methods receive equal per-query generation budgets, and isolated successful or failed generations cannot determine a query's result.
- Small dry runs may validate the pipeline but cannot replace the five-run formal protocol.
