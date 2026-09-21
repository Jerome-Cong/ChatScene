---
status: superseded by ADR-0019
---

# Execute artifact-conditioned plans with a layered controller

The Reference Ego Controller is upgraded from plain IDM-PID to a query-blind executor of a standardized Ego Event Plan contained in each finalized Scenic artifact. A layered state machine combines a reusable bus-service flow, a safety supervisor, and small door and merge substates with IDM-PID low-level control; it never reads query text or evaluation-only metadata and never supplies a missing plan.

## Consequences

- The 76 supported intents and 115 supported event labels do not produce intent-specific controllers. Version 0.1 targets roughly eight reusable ego maneuver primitives, 12–15 layered state nodes, and 20–30 guarded transitions.
- Other-actor actions remain Scenic behaviors, while queues, near-misses, violations, and conflicts remain event-detector outcomes rather than ego states.
- Plans must ground targets and triggers in entities and regions present in the generated artifact. Missing or invalid plans fall back to safe route following without receiving semantic completion from the controller.
- The controller remains an implementation of the existing Agent Policy extension point and does not change the ChatScene retrieval-generation-composition architecture.
