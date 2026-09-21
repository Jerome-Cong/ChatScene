---
status: accepted
---

# Use a deterministic-first semantic evidence hierarchy

Version 0.1 scores semantic atoms from the strongest available evidence. Platform-native source or schema, AST where available, loaded objects, native map facts, valid sampled geometry, and NE traces take precedence; a frozen judge rubric handles only semantics that cannot be decided deterministically and is calibrated and audited against human review. Scenic source and OpenDRIVE are the ChatScene/CARLA instances of this hierarchy.

## Consequences

- ARC actor identity and cardinality come from artifact and compiled-object facts, with role inference using geometry and attached behavior.
- RSC road facts come from the program and map, while sampled spatial atoms are averaged over successful SV realizations and score zero when no valid realization exists.
- `IEC_spec` requires actual reachable behavior, trigger, and action evidence; comments, variable names, and natural-language descriptions alone cannot satisfy an event atom. `IEC_exec` uses rollout traces only.
- Deterministic evidence cannot be overturned by a judge. Identical artifact hashes are reviewed once, and ambiguous judgments use a frozen rubric with human calibration and audit.
- Artifact parsing is structurally oracle-blind: the frozen extractor receives no oracle and returns only observed ego, atoms, common atoms, and category completeness. The evaluator derives deterministic verdicts; a separately hash-bound judge result may fill only verdicts that remain `unknown` and can never override deterministic evidence.
- This guarantee is payload-blindness under a trusted, frozen benchmark-owned extractor. The extractor subprocess is not treated as a general filesystem sandbox, so claims must not imply isolation from every host file.
- Judge calibration predictions are derived only by strict parsing of stdout from a validated request/runner/run chain. A pre-run context binds the single human gold for each item without machine-response fields. The request hash covers the exact query text, artifact text/hash, live deterministic evidence, full oracle atom, prompt, model, inference configuration, and output schema.
