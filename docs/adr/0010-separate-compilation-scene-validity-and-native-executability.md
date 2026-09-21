---
status: accepted
---

# Separate compilation, scene validity, and native executability

Compile Success is a track-native diagnostic indicating that the registered artifact parser or loader constructed the scene specification and loaded its simulator interface. For ChatScene/CARLA this is Scenic compilation. SV additionally requires a statically valid realization within a bounded sampling budget. NE additionally requires that a valid realization create its actors and advance in its unchanged registered runtime until the common simulated-time horizon or a normal termination without manual edits, setup errors, or runtime errors.

## Consequences

- The existing generation-time constructor check supports Compile Success only; it cannot by itself support SV or NE.
- IEC, not NE, determines whether a requested interaction event occurs. Intended collisions, violations, timeouts, and other scenario outcomes do not fail NE when the runtime itself remains healthy.
- Ego-policy safety, comfort, and dynamics remain Driving Diagnostics rather than conditions for NE.
