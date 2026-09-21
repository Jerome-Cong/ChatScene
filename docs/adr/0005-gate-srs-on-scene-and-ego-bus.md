---
status: accepted
---

# Gate SRS on a scene output with one ego bus

For a supported query, Scenario Requirement Satisfaction is zero unless the method outputs a scene containing exactly one semantic ego bus, including an approved proxy-bus representation. Once eligible, actor, road, spatial, event, temporal, and normative requirements receive weighted partial credit; Scene Validity and Native Executability remain separate metrics and do not gate semantic scoring.

## Consequences

- Correct secondary details cannot compensate for omitting or replacing the ego bus.
- Semantically informative but invalid or non-executable scene outputs retain an SRS diagnosis while failing SV or NE.
- Unsupported-query responses are evaluated by Unsupported-Query Handling rather than forced through the SRS gate.
