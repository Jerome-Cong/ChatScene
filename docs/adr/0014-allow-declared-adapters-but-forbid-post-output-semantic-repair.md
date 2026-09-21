---
status: accepted
---

# Allow declared adapters but forbid post-output semantic repair

The Ego Bus Proxy contract is public, and each method may use a declared Benchmark Adapter frozen as part of its main configuration. ChatScene may expose the proxy blueprint and dimensions through its existing fixed header, prompts, retrieval examples, and composition template. Once the platform-native artifact is complete, the evaluator may not insert, replace, resize, deduplicate, or otherwise correct its ego representation or other required semantics.

## Consequences

- Adapter logic is method-side and cannot access canonical queries, gold atoms, expected support, or other evaluation-only metadata.
- The completed artifact must contain exactly one conforming ego proxy; missing, duplicate, or nonconforming ego representations fail the SRS eligibility gate.
- Evaluation validates and scores artifacts but never completes the semantic task on a method's behalf.
