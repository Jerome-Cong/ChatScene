---
status: accepted
---

# Report benchmark metrics without a composite total

Scenario Requirement Satisfaction is the aggregate semantic-conformance metric. Actor and Role Correctness, Road-Context and Spatial-Relation Correctness, and Interaction-Event Correctness are specialized diagnostic views of overlapping requirements and are not added back into Scenario Requirement Satisfaction; validity, executability, query-specificity robustness, unsupported-query handling, and constraint-preserving diversity are reported on separate axes rather than collapsed into one total score.

## Consequences

- No method can compensate for poor semantic conformance with executability or diversity.
- Specialized metrics explain failure modes without double-counting actor, road, spatial, or event requirements.
- Benchmark comparisons must present a metric vector rather than claim a universal scalar ranking.
