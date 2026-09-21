---
status: accepted
---

# Score unsupported handling without an external router

Version 0.1 defines Unsupported-Query Handling from two response-routing rates over all five independent runs:

\[
A_s = \frac{\text{supported-query responses choosing generation}}{\text{all supported-query responses}}
\]

Here, "choosing generation" is counted only when the immutable response reaches
`complete` with a non-empty artifact satisfying the response artifact contract.
Failed, timed-out, missing, or empty artifacts are not accepted generations.

\[
H_u = \frac{\text{unsupported-query responses with valid safe handling}}{\text{all unsupported-query responses}}
\]

\[
UQH = \begin{cases}
\frac{2 A_s H_u}{A_s + H_u}, & A_s + H_u > 0 \\
0, & A_s + H_u = 0
\end{cases}
\]

## Consequences

- Support labels, allowed dispositions, and conflict reasons come from the frozen
  requirement oracle. Method-reported `expected_support` must match it. The method
  response contains only a locator for its raw stdout or stderr; evaluator verdicts
  and normalized reasons are forbidden from that payload.
- A rejection must identify a relevant scope conflict or contradiction and submit no Scenic artifact.
- A clarification request must ask an actionable question whose answer could make the request supported, and it submits no artifact before clarification.
- Controlled degradation must disclose the removed or replaced requirements, resolve every hard conflict, preserve nonconflicting core intent, and may then submit the degraded artifact.
- Rejection and clarification receive credit only after a `complete` terminal
  decision, successful non-timed-out process exit, non-empty selected response,
  no artifact, an oracle-allowed disposition, and an independent assessment that
  covers every frozen oracle reason code exactly.
- The assessor identity, version, evaluation source, configuration, and explicit
  independence from the response producer are frozen in the UQH assessor
  registry. Assessments cite verified raw-response byte ranges. Their content hash
  and assessment ID bind the verdict, all reason codes, evidence, rationale,
  provenance, response record, raw-response envelope, and the exact raw assessor
  response reconstructed into the assessment.
- Hashes provide replayability and tamper detection, not assessor authentication.
  Every formal assessment therefore carries a detached RSA-PSS/SHA-256 signature
  verified against the public key in the frozen registry. The trust root is that
  public key plus the independently approved custody of its private key outside
  both the repository and method runner. Private-key files are owner-only (`0600`
  recommended, `0400` allowed). Provider response IDs are audit metadata, not
  cryptographic identity proof.
- Formal UQH follows `generate` → verified generation chain → `uqh-request` →
  independent execution → transactional `uqh-attest` → `aggregate`. Request and
  assessment bundles are immutable directory publications; validation or signing
  failure leaves no partially published bundle.
- Every rejection or clarification output requires exactly one assessment. A
  missing assessment is an incomplete formal evaluation pipeline and aborts
  aggregation; it is not silently converted into a method score of zero.
- Controlled degradation cannot be validated from a method-supplied flag,
  claimed decision source, or assessment. Version 0.1 assigns controlled
  degradation zero credit unconditionally; enabling it requires a later protocol
  version.
- Raw stdout, stderr, and artifact files are read during aggregation and their byte
  counts and SHA-256 values are recomputed. A missing or changed bound file is an
  integrity error. Empty output, non-zero exit, timeout, generic failure, compile
  failure, or silent generation after ignoring conflicts receives no handling
  credit.
- The aggregate reports hashes of the exact response, oracle, assessment, roster,
  assessor-registry, and formal freeze inputs used to compute UQH.
- The benchmark harness records the method's disposition, raw streams, and optional
  artifact but does not add a support classifier. A support router is a distinct
  method variant or ablation.
- ChatScene's current pipeline has no unsupported-query branch. If it chooses generation for every unsupported query, then `H_u = 0` and `UQH = 0`; that result is retained as a baseline capability gap.
