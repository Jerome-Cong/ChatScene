"""Helpers for reviewable protocol decisions that affect formal statistics."""

from typing import Any, Dict, Mapping

from .errors import ValidationError


def statistical_cluster_map(
    document: Mapping[str, Any], allow_draft: bool = False
) -> Dict[str, str]:
    if document.get("decision_status") != "confirmed" and not allow_draft:
        raise ValidationError("statistical-cluster decisions are not confirmed")
    mapping = {}
    for cluster in document.get("clusters", []):
        if cluster.get("decision_status") != "confirmed" and not allow_draft:
            raise ValidationError("cluster {} is not confirmed".format(cluster.get("cluster_id")))
        cluster_id = cluster.get("cluster_id")
        members = cluster.get("members", [])
        if not cluster_id or len(members) < 2:
            raise ValidationError("statistical cluster requires an id and at least two members")
        for member in members:
            if member in mapping:
                raise ValidationError("intent {} occurs in multiple clusters".format(member))
            mapping[member] = cluster_id
    return mapping


def apply_statistical_cluster(
    intent_group_id: str, mapping: Mapping[str, str]
) -> str:
    return mapping.get(intent_group_id, intent_group_id)
