---
status: superseded by ADR-0017
---

# Use an IDM-PID reference ego controller

Version 0.1 uses a fixed method-independent ego controller for NE rollouts: IDM supplies longitudinal car-following commands and a route-following PID supplies lateral commands. It is connected through the existing Agent Policy interface and is frozen across all evaluated generation methods.

## Consequences

- Adding the controller is an implementation of an existing extension point, not a change to the ChatScene generation or Scenic runtime architecture.
- The controller must emit signed longitudinal commands so braking is preserved; the existing BasicAgent and BehaviorAgent wrappers, which return throttle but omit brake, cannot be reused unchanged.
- Plain IDM-PID supports lane following and vehicle following but does not supply query-specific docking, dwell, departure, merge, or VRU interaction state machines.
- The controller is used for NE and Event Realization Rate only. Its safety, comfort, dynamics, and event realization do not affect primary IEC or generation-method ranking.
