---
status: accepted
---

# Run one fixed native rollout per supported output

Each supported-query benchmark output receives at most one NE rollout. The protocol selects the lowest-numbered seed among its successful SV trials, creates the actors through the unchanged registered platform path, and advances for 30 seconds of simulated time or until a normal earlier termination. CARLA may implement the horizon as 300 steps at 0.1 seconds. A setup or runtime failure scores NE as zero, and the output cannot retry with another seed.

## Consequences

- An output with no successful SV trial has NE zero without starting its native simulator.
- Collision, rule violation, timeout, or failure to exhibit the requested event does not fail NE when the runtime remains healthy; those outcomes belong to semantic metrics or Driving Diagnostics.
- Correctly handled unsupported queries are outside SV and NE. A complete per-method run therefore requires at most 1,140 native rollouts for the 228 supported records and five generation runs.
