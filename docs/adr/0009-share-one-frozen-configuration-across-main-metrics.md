---
status: accepted
---

# Share one frozen configuration across main metrics

Each evaluated method uses one method-specific configuration frozen before test-set execution. SRS, ARC, RSC, IEC, SV, NE, RQS, UQH, and CPD are computed from outputs produced under that same configuration; a method may not change decoding, prompts, retrieval, retries, repairs, support handling, or generation budget for an individual metric.

## Consequences

- Different methods need not use identical internal parameters, but each method remains internally consistent across queries, repetitions, and metrics.
- A deterministic method may receive zero CPD; diversity-specific sampling settings cannot be substituted into its main results.
- Alternative configurations may be reported as separate ablations, never spliced into the main metric set.
