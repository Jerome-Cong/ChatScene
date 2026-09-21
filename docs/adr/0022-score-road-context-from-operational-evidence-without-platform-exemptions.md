---
status: accepted
---

# Score road context from operational evidence without platform exemptions

Version 0.1 credits road-context and spatial requirements only when the finalized Scenic artifact operationally instantiates them and the evidence is grounded in native map facts, explicit constructed geometry, or sampled spatial relations. Comments, labels, and variable names alone do not establish a road atom.

The fixed map set natively exposes driving lanes, sidewalks, shoulders or parking lanes, and crosswalk objects on some maps, but does not provide uniform first-class semantics for bus bays, non-motor lanes, bus-stop areas, no-parking zones, or queue areas. The evaluator does not synthesize these missing structures. A required atom that the output does not operationally express scores zero in RSC and its SRS category.

## Consequences

- All queries marked supported by the frozen library remain in primary scoring; environment limitations do not trigger post-hoc exclusion or relabeling.
- A separate Platform Expression Coverage preflight reports native, constructible, and currently undetectable concepts without changing method scores.
- Scene Validity remains query-independent. In addition to bounded Scenic sampling, it checks finite poses and positive dimensions, resolvable types and blueprints, map-bounded placement, and absence of initial physical overlap.
- Scene Validity does not require requested road semantics, legal placement, or event satisfaction, so intentionally illegal parking and other targeted violations are not rejected merely for being violations.
