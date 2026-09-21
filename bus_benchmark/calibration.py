"""Select and score a platform-balanced, single-reviewer human-gold calibration."""

import math
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping

from .constants import ATOM_CATEGORIES, SCHEMA_VERSION
from .errors import ValidationError
from .jsonio import canonical_json_bytes, sha256_bytes


VERDICTS = ("satisfied", "violated", "unknown")
CALIBRATION_PLATFORMS = ("carla", "metadrive")
CALIBRATION_ITEMS_PER_PLATFORM = 90
MINIMUM_CALIBRATION_ITEMS = CALIBRATION_ITEMS_PER_PLATFORM * len(
    CALIBRATION_PLATFORMS
)
CALIBRATION_SELECTION_ALGORITHM = "query_category_then_sha256_v0.1"
CALIBRATION_SELECTION_SEED = "bus-judge-calibration-v0.1"
CALIBRATION_PROFILES = ("carla_stage", "cross_platform_final")


def _selection_rank(seed: str, item_id: str) -> str:
    return sha256_bytes(
        canonical_json_bytes({"seed": seed, "item_id": item_id})
    )


def build_judge_calibration_roster(
    candidates: Iterable[Mapping[str, Any]], seed: str
) -> Dict[str, Any]:
    """Precommit 90 judge-routed atoms per platform before labels/predictions exist."""

    if seed != CALIBRATION_SELECTION_SEED:
        raise ValidationError("judge calibration must use the version-frozen selection seed")
    required_fields = {
        "item_id",
        "platform",
        "source_id",
        "query_id",
        "run_id",
        "repetition",
        "atom_id",
        "category",
        "surface_style",
        "source_response_sha256",
        "source_evidence_sha256",
    }
    population: List[Dict[str, Any]] = []
    seen_item_ids = set()
    for candidate in candidates:
        item = {field: candidate.get(field) for field in required_fields}
        if (
            set(item) != required_fields
            or item["platform"] not in CALIBRATION_PLATFORMS
            or item["category"] not in ATOM_CATEGORIES
            or not all(item.get(field) is not None for field in required_fields)
        ):
            raise ValidationError("judge calibration candidate is malformed")
        if item["item_id"] in seen_item_ids:
            raise ValidationError("judge calibration population repeats an item ID")
        seen_item_ids.add(item["item_id"])
        population.append(item)
    if {item["platform"] for item in population} != set(CALIBRATION_PLATFORMS):
        raise ValidationError("judge calibration population requires both platforms")

    selected: List[Dict[str, Any]] = []
    population_counts = {}
    for platform in CALIBRATION_PLATFORMS:
        platform_items = [item for item in population if item["platform"] == platform]
        population_counts[platform] = len(platform_items)
        ranked = sorted(
            platform_items,
            key=lambda item: (_selection_rank(seed, item["item_id"]), item["item_id"]),
        )
        if len(ranked) < CALIBRATION_ITEMS_PER_PLATFORM:
            raise ValidationError(
                "{} judge population has fewer than {} items".format(
                    platform, CALIBRATION_ITEMS_PER_PLATFORM
                )
            )
        chosen = []
        chosen_ids = set()

        def choose(item: Mapping[str, Any]) -> None:
            if item["item_id"] not in chosen_ids:
                chosen.append(dict(item))
                chosen_ids.add(item["item_id"])

        query_ids = sorted({item["query_id"] for item in ranked})
        if len(query_ids) > CALIBRATION_ITEMS_PER_PLATFORM:
            raise ValidationError("query coverage exceeds the calibration budget")
        for query_id in query_ids:
            choose(next(item for item in ranked if item["query_id"] == query_id))
        covered_categories = {item["category"] for item in chosen}
        observed_categories = sorted({item["category"] for item in ranked})
        for category in observed_categories:
            if category not in covered_categories:
                choose(next(item for item in ranked if item["category"] == category))
        for item in ranked:
            if len(chosen) == CALIBRATION_ITEMS_PER_PLATFORM:
                break
            choose(item)
        if len(chosen) != CALIBRATION_ITEMS_PER_PLATFORM:
            raise ValidationError("judge calibration roster selection is incomplete")
        selected.extend(chosen)

    normalized_population = sorted(
        population, key=lambda item: (item["platform"], item["item_id"])
    )
    normalized_selected = sorted(
        selected, key=lambda item: (item["platform"], item["item_id"])
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_status": "confirmed",
        "selection_algorithm": CALIBRATION_SELECTION_ALGORITHM,
        "seed": seed,
        "items_per_platform": CALIBRATION_ITEMS_PER_PLATFORM,
        "eligible_population_count_by_platform": population_counts,
        "eligible_population_sha256": sha256_bytes(
            canonical_json_bytes(normalized_population)
        ),
        "items_sha256": sha256_bytes(canonical_json_bytes(normalized_selected)),
        "items": normalized_selected,
    }


def _calibration_summary(
    records: List[Mapping[str, Any]],
    predictions_by_item: Mapping[str, str],
    *,
    require_all_verdicts: bool,
) -> Dict[str, Any]:
    gold = [record["gold_verdict"] for record in records]
    predicted = [predictions_by_item[record["item_id"]] for record in records]
    if require_all_verdicts and set(gold) != set(VERDICTS):
        raise ValidationError("judge calibration gold must cover all verdict classes")
    gold_counts = Counter(gold)
    per_class = {}
    for label in VERDICTS:
        true_positive = sum(g == label and p == label for g, p in zip(gold, predicted))
        false_positive = sum(g != label and p == label for g, p in zip(gold, predicted))
        false_negative = sum(g == label and p != label for g, p in zip(gold, predicted))
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": gold_counts[label],
        }
    observed = sum(g == p for g, p in zip(gold, predicted)) / len(gold)
    predicted_counts = Counter(predicted)
    expected = sum(
        (gold_counts[label] / len(gold)) * (predicted_counts[label] / len(gold))
        for label in VERDICTS
    )
    if require_all_verdicts and math.isclose(expected, 1.0):
        raise ValidationError("judge calibration kappa is undefined for a degenerate sample")
    return {
        "record_count": len(records),
        "macro_f1": sum(value["f1"] for value in per_class.values()) / len(VERDICTS),
        "cohen_kappa": (
            None
            if math.isclose(expected, 1.0)
            else (observed - expected) / (1.0 - expected)
        ),
        "accuracy": observed,
        "gold_support_by_verdict": {
            label: gold_counts[label] for label in VERDICTS
        },
        "predicted_support_by_verdict": {
            label: predicted_counts[label] for label in VERDICTS
        },
        "per_class": per_class,
    }


def compute_judge_calibration(
    records: Iterable[Mapping[str, Any]],
    predictions_by_item: Mapping[str, str],
    *,
    calibration_profile: str = "cross_platform_final",
) -> Dict[str, Any]:
    records = list(records)
    if calibration_profile not in CALIBRATION_PROFILES:
        raise ValidationError("unknown Judge calibration profile")
    expected_platforms = (
        ("carla",)
        if calibration_profile == "carla_stage"
        else CALIBRATION_PLATFORMS
    )
    expected_record_count = CALIBRATION_ITEMS_PER_PLATFORM * len(
        expected_platforms
    )
    if len(records) != expected_record_count:
        raise ValidationError(
            "judge calibration requires exactly {} labeled items".format(
                expected_record_count
            )
        )
    item_ids = set()
    for record in records:
        item_id = record.get("item_id")
        if not item_id or item_id in item_ids:
            raise ValidationError("judge calibration item IDs must be unique")
        item_ids.add(item_id)
        if not record.get("query_id") or not record.get("atom_id"):
            raise ValidationError("judge calibration item lacks query_id or atom_id")
        if not record.get("run_id"):
            raise ValidationError("judge calibration item lacks run_id")
        gold_record_id = record.get("gold_record_id")
        expected_gold_record_id = sha256_bytes(
            canonical_json_bytes(
                {
                    key: value
                    for key, value in record.items()
                    if key != "gold_record_id"
                }
            )
        )
        if gold_record_id != expected_gold_record_id:
            raise ValidationError("judge calibration human-gold hash mismatch")
        gold = record.get("gold_verdict")
        prediction = predictions_by_item.get(item_id)
        if any(value not in VERDICTS for value in (gold, prediction)):
            raise ValidationError("judge calibration contains an invalid verdict")
        if (
            record.get("workflow_type") != "judge_calibration_human_gold"
            or record.get("review_status") != "complete"
            or record.get("decision_status") != "confirmed"
            or record.get("canonical_source_revalidated") is not True
            or not record.get("reviewer_id")
        ):
            raise ValidationError(
                "judge calibration contains unfinished or unvalidated human gold"
            )
        if record.get("reviewer_blind_to_judge_prediction") is not True:
            raise ValidationError("judge calibration reviewer was not blinded to predictions")
        evidence = record.get("gold_evidence")
        rationale = record.get("gold_rationale")
        if (
            not isinstance(evidence, list)
            or not evidence
            or any(not isinstance(value, str) or not value for value in evidence)
            or not isinstance(rationale, str)
            or not rationale
        ):
            raise ValidationError("judge calibration human gold lacks evidence/rationale")

    if set(predictions_by_item) != item_ids:
        raise ValidationError("judge predictions must exactly cover calibration items")

    by_platform = {}
    require_all_verdicts = calibration_profile == "cross_platform_final"
    for platform in expected_platforms:
        platform_records = [
            record for record in records if record.get("platform") == platform
        ]
        if len(platform_records) != CALIBRATION_ITEMS_PER_PLATFORM:
            raise ValidationError(
                "{} judge calibration requires exactly {} items".format(
                    platform, CALIBRATION_ITEMS_PER_PLATFORM
                )
            )
        by_platform[platform] = _calibration_summary(
            platform_records,
            predictions_by_item,
            require_all_verdicts=require_all_verdicts,
        )
    if any(record.get("platform") not in expected_platforms for record in records):
        raise ValidationError("judge calibration contains an unknown platform")
    result = _calibration_summary(
        records,
        predictions_by_item,
        require_all_verdicts=require_all_verdicts,
    )
    result["by_platform"] = by_platform
    result["calibration_profile"] = calibration_profile
    result["formal_freeze_eligible"] = calibration_profile == "cross_platform_final"
    return result
