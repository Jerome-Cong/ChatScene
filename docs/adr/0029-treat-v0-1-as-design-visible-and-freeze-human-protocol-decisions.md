---
status: accepted
---

# Treat version 0.1 as design-visible and freeze human protocol decisions

The 252-query version 0.1 library was available while evaluator semantics,
ontology coverage, and fixtures were being developed. It is therefore a
design-visible benchmark suite, not an untouched held-out test set. The suite is
immutable after freeze, and no method may tune against frozen-suite outputs, but
version 0.1 does not support a held-out-generalization claim. A future claim of
that kind requires a separately authored query library hidden from evaluator and
method development until all configurations are frozen.

## Consequences

- The freeze protocol records `benchmark_suite_visibility = design_visible` and
  `held_out_generalization_claim = false`.
- The common deterministic ontology is derived only from the confirmed
  development oracle. Test-only CPD dimensions are ineligible and reduce
  reported coverage instead of being copied back into the evaluator.
- The frozen development-only ontology contains 16 concepts. Conformance uses
  exactly 76 routing/conformance fixtures and 15 cross-platform fixtures,
  producing 91 fixture-review subjects plus 32 platform-capability subjects.
  Complex roles, right-of-way, road, spatial, and temporal semantics route to the
  Judge instead of being inferred from proximity or one trajectory pattern.
- Exactly 13 one-time protocol decisions are hash-bound to their relevant assets.
  When the final cross-platform assets exist, each requires one complete human
  gold approval; a status-only or uncertain confirmation cannot pass freeze.
- The existing B08 decision also binds the UQH assessor registry and evaluator
  implementation: reject/clarification assessments must be independent and
  content-addressed, while controlled degradation remains zero in version 0.1.
- The method-independent MetaDrive runtime API, strict sequence-block-token
  artifact, executable token constraints, PGMap reset/step probe, and external
  observer are implemented. They remain draft and require the registered human
  decisions. The concrete evaluated MetaDrive method adapter and its generation
  outputs are still absent, so this platform evidence cannot be reported as a
  method result or final dual-platform readiness.
- Existing synthetic labels in formal contract tests validate gate behavior only.
  They remain inadmissible as human gold.
