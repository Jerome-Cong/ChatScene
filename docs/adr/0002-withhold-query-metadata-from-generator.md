---
status: accepted
---

# Withhold query metadata from the scene generator

The primary benchmark presents only `query_text` to ChatScene. The harness may retain `query_id` for artifact correlation, while `canonical_query`, support labels, acceptable responses, actor annotations, event annotations, and all other metadata remain an evaluation oracle unavailable to prompting, retrieval, and generation.

## Consequences

- Precise, partial, and vague formulations measure natural-language robustness instead of metadata conversion.
- Support-boundary decisions must come from the tested method rather than harness-side routing.
- Any metadata-assisted experiment must be reported as a separate upper-bound track.
