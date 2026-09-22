"""Intent-clustered bootstrap utilities implemented with the Python standard library."""

import random
from collections import defaultdict
from statistics import mean
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

from .errors import ValidationError


def percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValidationError("cannot take percentile of an empty sequence")
    if not 0.0 <= probability <= 1.0:
        raise ValidationError("probability must be in [0, 1]")
    position = (len(sorted_values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def clustered_bootstrap_mean(
    records: Iterable[Mapping[str, Any]],
    value_field: str,
    cluster_field: str = "statistical_intent_cluster_id",
    iterations: int = 10000,
    seed: int = 0,
) -> Dict[str, Any]:
    if iterations <= 0:
        raise ValidationError("bootstrap iterations must be positive")
    clusters = defaultdict(list)
    for record in records:
        value = record.get(value_field)
        if not isinstance(value, (int, float)):
            raise ValidationError("record missing numeric {}".format(value_field))
        cluster = record.get(cluster_field)
        if cluster is None and cluster_field == "statistical_intent_cluster_id":
            cluster = record.get("intent_group_id")
        if not cluster:
            raise ValidationError("record missing cluster {}".format(cluster_field))
        clusters[str(cluster)].append(float(value))
    cluster_ids = sorted(clusters)
    if not cluster_ids:
        raise ValidationError("clustered bootstrap requires records")
    cluster_means = {cluster_id: mean(values) for cluster_id, values in clusters.items()}
    observed = mean(cluster_means.values())
    generator = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        sampled_ids = [generator.choice(cluster_ids) for _ in cluster_ids]
        estimates.append(mean(cluster_means[cluster_id] for cluster_id in sampled_ids))
    estimates.sort()
    return {
        "estimate": observed,
        "ci_low": percentile(estimates, 0.025),
        "ci_high": percentile(estimates, 0.975),
        "confidence": 0.95,
        "iterations": iterations,
        "seed": seed,
        "cluster_count": len(cluster_ids),
        "record_count": sum(len(values) for values in clusters.values()),
    }


def paired_intent_difference(
    left: Iterable[Mapping[str, Any]],
    right: Iterable[Mapping[str, Any]],
    value_field: str,
) -> List[Dict[str, Any]]:
    def reduce(records: Iterable[Mapping[str, Any]]) -> Dict[str, float]:
        grouped = defaultdict(list)
        for record in records:
            cluster_id = record.get(
                "statistical_intent_cluster_id", record["intent_group_id"]
            )
            grouped[cluster_id].append(float(record[value_field]))
        return {group: mean(values) for group, values in grouped.items()}

    left_means = reduce(left)
    right_means = reduce(right)
    if set(left_means) != set(right_means):
        raise ValidationError("paired comparison requires identical intent groups")
    return [
        {
            "intent_group_id": group,
            "difference": left_means[group] - right_means[group],
        }
        for group in sorted(left_means)
    ]
