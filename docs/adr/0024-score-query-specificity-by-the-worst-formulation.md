---
status: accepted
---

# Score query specificity by the worst formulation

Every supported intent group in version 0.1 has exactly one precise, one partial, and one vague query, each with five independent generation runs. Therefore, macro-averaging

\[
\frac{SRS_{precise} + SRS_{partial} + SRS_{vague}}{3}
\]

across groups is mathematically identical to mean SRS over all supported-query outputs and does not provide a distinct robustness measurement.

Version 0.1 instead defines

\[
\bar S_{g,t} = \frac{1}{5}\sum_{k=1}^{5}SRS_{g,t,k}
\]

and

\[
Let \(G\) be the frozen set of statistical intent clusters. Then

\[
RQS = \frac{1}{|G|}\sum_{g \in G} \min_{t \in \{precise, partial, vague\}} \bar S_{g,t}.
\]

The current machine proposal keeps all 76 supported source intents but groups
the two registered duplicate pairs into 74 macro units. This denominator is a
draft until the duplicate-cluster human decision is confirmed; the evaluator
therefore derives \(|G|\) from the frozen roster and reports both the 76 source
intents and 74 statistical clusters instead of hard-coding either count.
\]

## Consequences

- Each intent contributes its weakest surface formulation, so performance on an easy formulation cannot compensate for failure on another formulation.
- The original three-style mean is retained as `RQS_mean` for compatibility but is explicitly identified as redundant with supported-query mean SRS.
- The diagnostic vague-specificity gap is reported as

\[
\Delta_{vague} = \frac{1}{76}\sum_g(\bar S_{g,precise} - \bar S_{g,vague}).
\]

- RQS uses only supported statistical intent clusters and the same five finalized artifacts already used by the other metrics; it receives no separate generation budget or configuration.
