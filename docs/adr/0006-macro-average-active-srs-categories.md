---
status: accepted
---

# Macro-average active SRS requirement categories

After the SRS eligibility gate, each active requirement category is scored as the unweighted mean of its atoms, and Scenario Requirement Satisfaction is the unweighted macro-average of active actor, road-context, spatial, event, temporal, and normative/risk categories. Empty categories are excluded rather than treated as perfect, and the ego requirement is not counted again after gating.

## Consequences

- Queries with more actors or more verbose event descriptions do not automatically give those categories greater influence.
- Version 0.1 requires no tuned numerical weights; each atom's effective weight follows from its category size and the number of active categories.
- Category-level scores remain directly interpretable and can be audited against the aggregate SRS.
