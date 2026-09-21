---
status: accepted
---

# Limit the primary benchmark claim to scene generation

The unified query benchmark ranks methods by whether they produce executable, semantically conformant top-down scenes. Ego driving safety, comfort, and vehicle dynamics remain runtime diagnostics and do not affect the primary method ranking, because the benchmark may use a size-compatible proxy for the ego bus and the existing driving policy was trained for a different vehicle.

## Consequences

- Results must not be presented as evidence of production-bus dynamics or ego-policy quality.
- Proxy-vehicle mismatch is reported as a limitation rather than folded into the generation score.
- Driving metrics may detect broken executions, but cannot compensate for missing query semantics.
