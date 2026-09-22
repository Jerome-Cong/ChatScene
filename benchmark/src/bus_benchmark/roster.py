"""Frozen query roster construction and exact five-run completeness checks."""

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

from .constants import REPETITIONS, SCHEMA_VERSION
from .decisions import apply_statistical_cluster, statistical_cluster_map
from .errors import ValidationError
from .jsonio import sha256_file


def build_roster(
    records: Iterable[Mapping[str, Any]],
    library_path: Path,
    method_id: str,
    platform: str,
    cluster_document: Mapping[str, Any],
    allow_draft: bool = False,
) -> Dict[str, Any]:
    records = list(records)
    if platform not in ("carla", "metadrive"):
        raise ValidationError("roster platform must be carla or metadrive")
    if not method_id:
        raise ValidationError("roster method_id is required")
    mapping = statistical_cluster_map(cluster_document, allow_draft=allow_draft)
    entries = []
    seen = set()
    for record in records:
        query_id = record["query_id"]
        if query_id in seen:
            raise ValidationError("roster contains duplicate query_id {}".format(query_id))
        seen.add(query_id)
        intent_group_id = record["intent_group_id"]
        entries.append(
            {
                "query_id": query_id,
                "intent_group_id": intent_group_id,
                "statistical_intent_cluster_id": apply_statistical_cluster(
                    intent_group_id, mapping
                ),
                "surface_style": record["surface_style"],
                "expected_support": record["expected_support"],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_status": "draft"
        if cluster_document.get("decision_status") != "confirmed"
        else "confirmed",
        "method_id": method_id,
        "platform": platform,
        "library_path": str(Path(library_path).resolve()),
        "library_sha256": sha256_file(Path(library_path)),
        "query_count": len(entries),
        "expected_response_count": len(entries) * REPETITIONS,
        "repetitions": list(range(REPETITIONS)),
        "queries": sorted(entries, key=lambda value: value["query_id"]),
    }


def roster_entries(roster: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    entries = {}
    for entry in roster.get("queries", []):
        query_id = entry.get("query_id")
        if not query_id or query_id in entries:
            raise ValidationError("roster query IDs must be unique and non-empty")
        entries[query_id] = entry
    if len(entries) != roster.get("query_count"):
        raise ValidationError("roster query_count does not match entries")
    if roster.get("expected_response_count") != len(entries) * REPETITIONS:
        raise ValidationError("roster expected_response_count is inconsistent")
    if roster.get("repetitions") != list(range(REPETITIONS)):
        raise ValidationError("roster repetitions must be [0,1,2,3,4]")
    return entries


def validate_records_against_roster(
    records: Iterable[Mapping[str, Any]],
    roster: Mapping[str, Any],
    require_confirmed: bool = True,
) -> Dict[str, int]:
    records = list(records)
    if require_confirmed and roster.get("decision_status") != "confirmed":
        raise ValidationError("formal execution requires a confirmed roster")
    entries = roster_entries(roster)
    repetitions = defaultdict(set)
    run_ids = set()
    for record in records:
        query_id = record.get("query_id")
        if query_id not in entries:
            raise ValidationError("record references query outside frozen roster: {}".format(query_id))
        repetition = record.get("repetition")
        if (
            isinstance(repetition, bool)
            or not isinstance(repetition, int)
            or not 0 <= repetition < REPETITIONS
        ):
            raise ValidationError("record repetition must be 0..4")
        if repetition in repetitions[query_id]:
            raise ValidationError("duplicate repetition {} for {}".format(repetition, query_id))
        repetitions[query_id].add(repetition)
        run_id = record.get("run_id")
        if not run_id or run_id in run_ids:
            raise ValidationError("records require unique non-empty run_id values")
        run_ids.add(run_id)
        if record.get("method_id") != roster.get("method_id"):
            raise ValidationError("record method differs from frozen roster")
        if record.get("platform") != roster.get("platform"):
            raise ValidationError("record platform differs from frozen roster")
        entry = entries[query_id]
        for field in (
            "intent_group_id",
            "statistical_intent_cluster_id",
            "surface_style",
            "expected_support",
        ):
            if record.get(field) != entry.get(field):
                raise ValidationError(
                    "record {} differs from roster for {}".format(field, query_id)
                )
    if set(repetitions) != set(entries):
        missing = sorted(set(entries) - set(repetitions))
        raise ValidationError("records omit frozen roster queries: {}".format(missing[:5]))
    expected = set(range(REPETITIONS))
    incomplete = [query_id for query_id, values in repetitions.items() if values != expected]
    if incomplete:
        raise ValidationError("records omit repetitions for queries: {}".format(incomplete[:5]))
    if len(records) != roster.get("expected_response_count"):
        raise ValidationError("record count differs from frozen roster")
    return {
        "query_count": len(entries),
        "record_count": len(records),
        "method_count": 1,
    }


def validate_cpd_records_against_roster(
    records: Iterable[Mapping[str, Any]],
    roster: Mapping[str, Any],
    eligible_query_ids: Sequence[str],
    require_confirmed: bool = True,
) -> Dict[str, int]:
    records = list(records)
    if require_confirmed and roster.get("decision_status") != "confirmed":
        raise ValidationError("formal CPD requires a confirmed roster")
    entries = roster_entries(roster)
    expected_ids = set(eligible_query_ids)
    if not expected_ids <= set(entries):
        raise ValidationError("eligible CPD query is outside the frozen roster")
    repetitions = defaultdict(set)
    run_ids = set()
    for record in records:
        query_id = record.get("query_id")
        if query_id not in expected_ids:
            raise ValidationError("CPD record is not in the frozen eligible set")
        if record.get("method_id") != roster.get("method_id") or record.get("platform") != roster.get(
            "platform"
        ):
            raise ValidationError("CPD method/platform differs from frozen roster")
        repetition = record.get("repetition")
        if (
            isinstance(repetition, bool)
            or not isinstance(repetition, int)
            or not 0 <= repetition < REPETITIONS
        ):
            raise ValidationError("CPD repetition must be 0..4")
        run_id = record.get("run_id")
        if not run_id or run_id in run_ids:
            raise ValidationError("CPD records require unique non-empty run_id values")
        run_ids.add(run_id)
        if repetition in repetitions[query_id]:
            raise ValidationError("duplicate CPD repetition")
        repetitions[query_id].add(repetition)
        entry = entries[query_id]
        for field in (
            "intent_group_id",
            "statistical_intent_cluster_id",
            "surface_style",
            "expected_support",
        ):
            if record.get(field) != entry.get(field):
                raise ValidationError("CPD record metadata differs from frozen roster")
    if set(repetitions) != expected_ids:
        raise ValidationError("CPD records do not cover the complete eligible roster")
    if any(values != set(range(REPETITIONS)) for values in repetitions.values()):
        raise ValidationError("each eligible CPD query requires five records")
    return {"eligible_query_count": len(expected_ids), "record_count": len(records)}
