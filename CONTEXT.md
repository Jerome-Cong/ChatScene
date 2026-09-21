# ChatScene Unified Query Benchmark

This context defines the language used to evaluate ChatScene against the unified bus-scene query library. It separates scene-generation evidence from downstream driving-performance evidence.

## Language

**Scene-Generation Benchmark**:
An evaluation of whether a natural-language query produces an executable top-down scene whose actors, topology, and interactions match the requested semantics.
_Avoid_: Driving benchmark, policy benchmark

**Ego Bus**:
The scene actor assigned the bus role by the query, evaluated through its semantic role and top-down geometry. The term does not by itself claim production-bus dynamics or visual fidelity.
_Avoid_: Physical bus model, production bus

**Driving Diagnostic**:
A runtime measurement used to diagnose whether a generated scene executes plausibly, without contributing to the primary ranking of scene-generation methods.
_Avoid_: Primary score, generation score

**Semantic Conformance**:
Agreement between a generated scene and the query's required actors, topology, event order, constraints, and support boundary.
_Avoid_: Compilation success, runtime success

**Benchmark Query**:
The natural-language `query_text` presented to the scene-generation method. Its identity and evaluation annotations are not part of the method input.
_Avoid_: Canonical query, annotated query

**Evaluation Oracle**:
The query-library metadata used only after generation to determine expected support, acceptable responses, and semantic conformance.
_Avoid_: Generator context, retrieval metadata

**Scenario Requirement**:
An atomic, independently assessable condition stated or necessarily implied by a benchmark query, such as an actor role, road context, spatial relation, event, temporal order, or normative constraint.
_Avoid_: Prompt token, metadata field

**Core Intent**:
The invariant scenario meaning shared by the precise, partial, and vague formulations in one intent group.
_Avoid_: Surface wording, canonical phrasing

**Constraint-Preserving Diversity**:
Variation among generated scenes that retains the query's core intent and required constraints.
_Avoid_: Unconstrained variation, requirement drift

**Scenario Requirement Satisfaction**:
The weighted proportion of a query's scenario requirements satisfied by an output. It is the benchmark's aggregate semantic-conformance measure.
_Avoid_: Overall benchmark score, executability score

**Specialized Conformance Metric**:
A diagnostic view of one semantic dimension, such as actor roles, road and spatial structure, or interaction events. It explains conformance without being added back into Scenario Requirement Satisfaction.
_Avoid_: Independent total score, duplicate weight

**Requirement Oracle**:
A versioned evaluation artifact that classifies query requirements as core required, surface required, permitted, or forbidden before benchmark outputs are observed.
_Avoid_: Post-hoc rubric, canonical-query copy

**Human Review Subject**:
One source-bound unit assigned to a reviewer that can produce at most one complete Human Gold record. A query is one subject even when its oracle contains many requirement atoms.
_Avoid_: Atom review count, duplicated reviewer task

**Reviewer Assignment**:
The binding between a Human Review Subject and one named reviewer. The reviewer identifier labels the assignment but does not itself prove human identity.
_Avoid_: Identity proof, reviewer authentication

**Machine Review Draft**:
A source-bound machine or Agent recommendation used to prioritize human review. It has no human authorship or completion status and can never be Human Gold by itself.
_Avoid_: Prefilled human answer, automatic gold

**Human Review Submission**:
A complete source-bound set of reviewer decisions for every subject in one assigned packet. It remains a submission, not Human Gold, until the canonical finalizer revalidates its sources and decisions.
_Avoid_: Review checkpoint, confirmed oracle

**Human Gold**:
The unique complete human decision for a Human Review Subject accepted by the canonical finalizer under external human custody. Pending, uncertain, rejected, machine-generated, and synthetic contract-test records are not Human Gold.
_Avoid_: Machine draft, checkpoint, synthetic approval

**Permitted Completion**:
A reasonable detail left unspecified by a query that an output may choose without reward or penalty, provided it does not conflict with required or forbidden semantics.
_Avoid_: Required inference, hallucination penalty

**SRS Eligibility Gate**:
The minimum condition for a supported-query output to receive nonzero Scenario Requirement Satisfaction: it must be a scene with exactly one semantic ego bus, including an approved proxy representation.
_Avoid_: Executability gate, all-requirements gate

**Active Requirement Category**:
A requirement category that contains at least one scored atom for the presented query. Only active categories participate in the macro-average for Scenario Requirement Satisfaction.
_Avoid_: Empty full-score category, fixed denominator category

**Benchmark Output**:
One immutable platform-native scene artifact produced by an independent generation run for a benchmark query. ChatScene's CARLA track uses a Scenic program; another registered method may use a MetaDrive-native artifact. It is the unit scored for generation quality and compared for diversity after semantic projection.
_Avoid_: Runtime realization, simulator seed

**Runtime Realization**:
One simulator execution sampled from an already generated platform-native artifact. Multiple realizations may diagnose validity or executability, but their variation is not generation diversity.
_Avoid_: Independent generation, benchmark output

**Generation Repetition Budget**:
The fixed number of independent generation runs allocated to each benchmark query. Version 0.1 assigns five runs to every query, including unsupported queries.
_Avoid_: Runtime sample count, retry-until-success budget

**Frozen Main Configuration**:
One method-specific configuration fixed before test-set execution and shared by all primary metrics. It includes the model, prompts, retrieval and decoding settings, generation budget, retries, repairs, and support-handling behavior.
_Avoid_: Metric-specific tuning, post-hoc configuration selection

**Compile Success**:
A platform-track diagnostic showing that the native artifact parser or loader can construct the scene specification and load its simulator interface. For ChatScene/CARLA this is Scenic compilation. It does not establish scene validity or native executability.
_Avoid_: Scene Validity, Native Executability

**Scene Validity**:
The ability of a loaded native artifact to produce a statically valid realization within a bounded platform-track sampling budget and satisfy benchmark-wide geometric sanity checks without a full simulator rollout.
_Avoid_: Compilation alone, query satisfaction, driving outcome

**Native Executability**:
The ability of a valid realization to create its actors and advance through its registered native simulator track until the common simulated-time horizon or a normal termination, without setup or runtime failure and without manual edits.
_Avoid_: Event correctness, collision avoidance, policy quality

**SV Sampling Trial**:
One bounded attempt to obtain and statically validate a realization from a loaded native artifact under a predetermined platform seed. Version 0.1 uses seeds zero through four and at most 2,000 native sampling or rejection iterations per seed.
_Avoid_: Unbounded retry, CARLA rollout, generation run

**NE Rollout**:
The single native simulator execution allocated to a supported-query benchmark output. It uses the lowest-numbered realization that passed SV and runs for 30 seconds of simulated time or until a normal earlier termination, without retrying another seed. CARLA may implement this as 300 steps at 0.1 seconds.
_Avoid_: Rollout selection, driving-performance trial, retry budget

**Ego Bus Proxy**:
The benchmark-approved track-native vehicle representation carrying the ego-bus role and an explicit top-down footprint of 5.33 m by 2.10 m. ChatScene/CARLA uses Scenic's Car class with the Chevrolet Impala blueprint; other tracks register a fixed existing proxy without claiming bus appearance or dynamics.
_Avoid_: Native bus class, physical bus model, unmarked passenger car

**Benchmark Adapter**:
A declared, frozen method-side layer that converts the public benchmark input and output contract into a method's existing interface without using evaluation-only metadata or rewriting completed outputs.
_Avoid_: Evaluation-time correction, hidden oracle access, architecture replacement

**Post-output Semantic Repair**:
An evaluator or harness change that inserts, replaces, or corrects required scene meaning after the evaluated method has completed its output. It is prohibited in the benchmark.
_Avoid_: Pre-registered adapter, syntax validation, scoring

**Finalized Artifact**:
The single immutable platform-native artifact submitted by one generation run before benchmark validation begins. Failures remain evidence and cannot be replaced by retrying or selecting another artifact.
_Avoid_: Best-of-N selection, evaluator repair, overwritten failure

**Method-native Repair**:
A bounded self-correction step that belongs to an evaluated method before artifact finalization, is declared in its Frozen Main Configuration, and has all model calls recorded. ChatScene version 0.1 adds no such step.
_Avoid_: Benchmark-provided repair, unlimited retry, post-finalization edit

**Reference Ego Controller**:
The fixed ego policy used to advance scenes during NE and `IEC_exec` rollouts. It receives no query or evaluation-only metadata, does not alter finalized artifacts, and remains outside primary scene-generation scoring.
_Avoid_: Evaluated generation method, query-reading controller, artifact repair

**Event Realization Rate**:
A Driving Diagnostic reporting whether requested events are observed during the rollout under the Reference Ego Controller. It does not contribute to the primary IEC or generation-method ranking.
_Avoid_: Interaction-Event Correctness, executability score, policy benchmark

**Interaction-Event Specification Correctness**:
The primary IEC view of whether the finalized artifact correctly specifies the required actors, event primitives, grounded targets, triggers, completion conditions, and temporal order. It is attributable to scene generation rather than ego-policy execution.
_Avoid_: Observed event rate, controller performance, accidental event

**Interaction-Event Execution Correctness**:
The diagnostic IEC view of whether specified events are observed under the fixed Reference Ego Controller. It is reported as Event Realization Rate and is not combined with specification correctness.
_Avoid_: Primary IEC, generator-only score, composite event score

**Generator Preservation Boundary**:
The benchmark keeps ChatScene's prompts, extraction format, retrieval content, generation stages, composition semantics, and output contract unchanged. The only generated-content exception is the uniform Ego Bus Proxy blueprint and top-down footprint substitution applied to every query.
_Avoid_: Event Plan injection, query-specific template change, semantic post-processing

**Semantic Evidence Hierarchy**:
The precedence order used to score generated semantics: deterministic artifact, AST, compiled-object, map, sampled-geometry, and runtime-trace facts take priority; a frozen judge rubric and human review resolve only meanings not deterministically decidable.
_Avoid_: Judge-only scoring, comment-as-fact, evidence cherry-picking

**Judge-Assisted Evidence**:
A calibrated interpretation of ambiguous generated semantics under a frozen rubric. It may fill an undecidable semantic judgment but cannot contradict deterministic evidence.
_Avoid_: Sole ground truth, factual override, post-hoc rubric

**Operational Road-Context Evidence**:
Evidence that a required road or spatial atom is instantiated by the finalized Scenic artifact and grounded in native map facts, explicit constructed geometry, or sampled spatial relations. Labels, comments, and names without operational geometry are insufficient.
_Avoid_: Evaluator-created road structure, label-only road context

**Platform Expression Coverage**:
A per-track diagnostic preflight report of which benchmark road concepts can be represented and detected in the registered CARLA or MetaDrive environment. Missing platform support does not relabel a supported query, remove it from primary scoring, or authorize evaluator completion.
_Avoid_: Post-hoc exclusion, support relabeling, score exemption

**Query-Independent Geometric Sanity**:
The benchmark-wide static checks used by Scene Validity: finite poses and dimensions, resolvable object types and blueprints, placement within modeled map bounds, and absence of initial physical overlap. It does not test query satisfaction or traffic-law compliance.
_Avoid_: Semantic validity, legality filter, event correctness

**Supported-Query Acceptance**:
The proportion of supported-query responses in which an evaluated method chooses the generate disposition. Artifact quality and executability are scored elsewhere and do not change this routing decision.
_Avoid_: Successful compilation rate, semantic satisfaction rate

**Valid Unsupported Handling**:
An explicit rejection, actionable clarification request, or disclosed controlled degradation that correctly addresses an unsupported query's scope conflict or contradiction. Silent requirement removal, empty output, and generation failure are not valid handling.
_Avoid_: Generic error, failed generation, hidden degradation

**Unsupported-Query Handling**:
The harmonic mean of Supported-Query Acceptance and the rate of Valid Unsupported Handling. It assigns zero to both generate-all and reject-all systems and evaluates the method's own handling rather than a benchmark-provided router.
_Avoid_: Unsupported-only accuracy, external support classifier

**Specificity Robustness Floor**:
For one supported statistical intent cluster, the minimum of its five-run mean SRS values across precise, partial, and vague formulations. RQS macro-averages this floor across frozen clusters so a strong formulation cannot hide failure on another formulation; confirmed duplicate intents share one macro unit while all original outputs remain in the benchmark.
_Avoid_: Mean SRS duplicate, pooled-query average

**Vague-Specificity Gap**:
The supported-intent macro-average of precise-formulation mean SRS minus vague-formulation mean SRS. It diagnoses degradation as wording becomes less specific and is reported alongside RQS without being combined with it.
_Avoid_: Robustness score, absolute quality measure

**Cross-Platform Benchmark Scope**:
The benchmark includes CARLA and MetaDrive as explicit simulator factors. Platform-native assets, identifiers, maps, and APIs are implementation evidence rather than method-quality dimensions.
_Avoid_: Single-platform benchmark, hidden platform factor

**Method-Independent Diversity Opportunity**:
A CPD variation dimension defined once by the benchmark ontology and applied equally to every evaluated method. A method does not receive a custom whitelist based on what it can generate.
_Avoid_: Method-specific diversity rubric, capability-adjusted score

**Platform-Neutral Diversity Requirement**:
The requirement that primary CPD exclude variation caused only by simulator-native maps, assets, coordinate conventions, or behavior APIs. Platform-specific diversity may be reported diagnostically but cannot enter the cross-platform primary CPD.
_Avoid_: Raw map-ID diversity, blueprint diversity, simulator-API diversity

**Non-Crossed Platform Assignment**:
An experimental constraint in which every evaluated method cannot be run on both CARLA and MetaDrive. Method and platform effects are therefore not causally identifiable from scores, even after projecting outputs onto shared semantic dimensions.
_Avoid_: Full-factorial design, causal platform adjustment

**Common Semantic Diversity**:
Constraint-preserving diversity computed only from rewardable semantic dimensions that CARLA and MetaDrive can both express, deterministically extract, and validate through paired equivalence fixtures. Core preservation may use either deterministic evidence or an exact hash-bound verdict from the frozen calibrated Judge; unresolved atoms fail preservation. It excludes platform-native identifiers, assets, coordinates, dynamics, controllers, and runtime variation.
_Avoid_: Platform-specific diversity, causal platform neutrality

**Common CPD Eligibility**:
A query-level status frozen before method outputs are observed. A partial or vague query is eligible only when it has at least one permitted common semantic diversity dimension and its core preservation constraints can be judged consistently on both platforms.
_Avoid_: Output-dependent exclusion, failure filtering

**Common CPD Coverage**:
The proportion of candidate partial or vague queries satisfying Common CPD Eligibility. It reports the scope of `CPD_common`; generation failure affects a method's score rather than coverage.
_Avoid_: Success rate, Scene Validity, Native Executability

**Design-Visible Frozen Benchmark Suite**:
The complete 252-query version 0.1 library, whose contents were available during evaluator design. Its exact snapshot is immutable after freeze and frozen-suite outputs cannot drive later prompt, threshold, Judge, extractor, method-configuration, or stopping-rule changes. It does not support an untouched held-out-generalization claim.
_Avoid_: Untouched held-out test, post-freeze tuning set

**Development Calibration Library**:
The separate 48-query collection built from 16 semantically non-overlapping intent groups for pipeline debugging, evaluator calibration, and configuration selection before the benchmark freeze.
_Avoid_: Frozen-suite subset, paraphrased benchmark intents

**Benchmark Freeze Manifest**:
A versioned, hashed record of the query libraries, requirement oracle, CPD eligibility, platform manifests, extractors, judge, method configurations, seeds, proxies, controllers, runtime budgets, interpreter binaries, package versions, and tracked platform source trees fixed before test generation begins.
_Avoid_: Partial configuration log, post-hoc update

**Intent-Clustered Bootstrap**:
A confidence-interval procedure that resamples complete intent groups, keeping their surface formulations and repeated generations together so correlated records are not treated as independent samples.
_Avoid_: Per-output independent bootstrap, inflated sample size

**Post-Test Human Audit**:
A stratified human review of at least ten percent of each method's test outputs used to estimate evaluator error. It cannot trigger a method-specific rubric change; any protocol change requires a benchmark version increment and complete rescoring.
_Avoid_: Test-time calibration, selective correction
