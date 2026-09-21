---
status: accepted
---

# Represent the ego bus with a fixed Impala proxy

Version 0.1 represents the ego bus as Scenic's existing Car class with CARLA blueprint `vehicle.chevrolet.impala` and explicit Scenic length 5.33 m and width 2.10 m. The evaluator recognizes only this designated ego representation as the approved bus proxy for the SRS eligibility gate.

## Consequences

- No Bus class or ChatScene/Scenic architecture change is required.
- Scenic spatial checks use the target top-down footprint, while CARLA executes the nearby Impala footprint of approximately 5.369 m by 2.053 m.
- Height, visual bus fidelity, passenger capacity, and production-bus dynamics are outside the primary benchmark claim.
- Version 0.1 does not override the Impala's mass, wheelbase, steering, suspension, or other low-level physics; supplied bus-dynamics limits remain Driving Diagnostics.
