"""Materialize semantic evidence with the frozen, query-blind extractor."""

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

from .errors import ValidationError
from .freeze import execute_frozen_extractor, load_frozen_extractor_runtime
from .provenance import frozen_asset_hash, record_sha256
from .schema import validate_schema_records


_RESPONSE_METADATA_FIELDS = (
    "query_id",
    "run_id",
    "repetition",
    "method_id",
    "platform",
    "intent_group_id",
    "statistical_intent_cluster_id",
    "surface_style",
    "expected_support",
    "terminal_status",
)


def utc_now() -> str:
    """Return one schema-compatible UTC timestamp."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_frozen_semantic_evidence(
    response_records: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    *,
    created_at_utc: Optional[str] = None,
) -> list:
    """Extract one immutable evidence record for every response.

    Only the response and benchmark-owned frozen extractor context reach the
    extractor.  In particular, no query text, oracle atom, or Judge verdict is
    accepted by this API.
    """

    responses = list(response_records)
    if not responses:
        raise ValidationError("semantic extraction requires at least one response")
    validate_schema_records(
        responses, "response_record", context="semantic extraction response"
    )
    run_ids = [record.get("run_id") for record in responses]
    if any(not run_id for run_id in run_ids) or len(set(run_ids)) != len(run_ids):
        raise ValidationError(
            "semantic extraction requires unique non-empty response run IDs"
        )

    extractor_document, extractor_runtime = load_frozen_extractor_runtime(manifest)
    extractor_sha256 = frozen_asset_hash(manifest, "semantic_extractor_manifest")
    timestamp = created_at_utc or utc_now()
    records = []
    for response in responses:
        projection = execute_frozen_extractor(extractor_runtime, response)
        artifact = response.get("artifact")
        evidence: Dict[str, Any] = {
            "schema_version": "0.1",
            **{field: response.get(field) for field in _RESPONSE_METADATA_FIELDS},
            **projection,
            "provenance": {
                "source_response_sha256": record_sha256(response),
                "source_artifact_sha256": (
                    artifact.get("sha256") if isinstance(artifact, Mapping) else None
                ),
                "extractor_id": extractor_document["extractor_id"],
                "extractor_version": extractor_document["extractor_version"],
                "extractor_config_sha256": extractor_sha256,
                "created_at_utc": timestamp,
            },
        }
        records.append(evidence)

    validate_schema_records(records, "semantic_evidence", context="semantic evidence")
    return records
