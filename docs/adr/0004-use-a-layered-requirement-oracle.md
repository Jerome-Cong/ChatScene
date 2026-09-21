---
status: accepted
---

# Use a layered requirement oracle for each surface query

Before benchmark execution, a versioned and human-reviewed requirement oracle will classify atoms as core required for all formulations in an intent group, surface required by a particular `query_text`, permitted as an unspecified but reasonable completion, or forbidden by the intent, rules, or benchmark scope. Precise, partial, and vague queries will not inherit an undifferentiated copy of every detail in the canonical query.

## Consequences

- Scenario Requirement Satisfaction scores only required and forbidden conditions applicable to the presented query.
- Partial and vague outputs are not penalized for choosing reasonable unspecified details.
- Robustness to Query Specificity measures preservation of core intent rather than recovery of hidden canonical wording.
- Oracle revisions require a new version and cannot be motivated by observed model outputs.
