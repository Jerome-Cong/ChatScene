# Pending human review packets

This directory contains machine-prepared assignments only. It contains exactly
zero completed real-human reviews and zero admissible human-gold records.

The historical `*_annotator_a.json`, `*_annotator_b.json`, and
`*_adjudication_pending.json` files were generated under a superseded workflow.
They are retained only as non-admissible previews and must not be assigned,
finalized, counted, or registered in a freeze.

The active policy is one named reviewer and one `review_status: complete` human
gold record per subject. Generate the 300 directly finalizable query bundles,
editable submission templates, and the machine-draft registry with:

```bash
python3 scripts/export_human_review_drafts.py \
  --reviewer REAL_REVIEWER_ID \
  --output-dir REVIEW_ASSIGNMENT_DIRECTORY
```

Do not run this into the historical preview directory. The exporter accepts
only a new or empty, non-symlink output directory and never overwrites an
existing assignment or foreign file. The generated registry is the
authoritative list of active assignment files; legacy files never appear as
active bundle bindings.

The CARLA-first workload is source-derived as 300 query subjects, 52 platform
subjects, and 90 Judge-calibration subjects: 442 reviewer submissions and 442
eventual human-gold records. `uncertain` is never gold. The inventory becomes
`stage_bound` only when `inventory-refresh` validates the exact complete CARLA
profile; incomplete development inventories require `--profile partial_dev`.

The 300 query records contain 5,701 explicit verdict entries (3,601 atoms +
1,800 required checks + 300 CPD policies). Platform and calibration add 142
entries, giving 5,843 explicit verdicts inside 442 eventual gold records. The
exporter includes 5,701 machine-only recommendations, but every editable human
response remains unset. Recommendations never populate a submission or become
gold; the exporter itself always creates zero gold.

The MetaDrive token-only platform runtime/observer is implemented,
source-tested, and independently reviewed as a platform-track candidate. Its
concrete tested-method adapter and real method outputs have not been provided,
so cross-platform review and the final 13 hash-bound protocol decisions are
deferred. No MetaDrive native probe or preview is counted as CARLA human work or
as a tested-method result. The six MetaDrive machine decisions map to existing
formal review subjects or automatic gates and add zero standalone human-gold
records.

Post-test blind audit uses one final reviewer per selected subject. A response
must be `complete`; uncertain work is excluded rather than converted by a
mandatory second-review workflow. The finalizer deterministically recomputes the
declared sampling rate and seed from the full population.
