"""Platform-neutral semantic ontology derived from confirmed benchmark oracles."""

import json
from typing import Any, Dict, Iterable, Mapping

from .constants import (
    CARLA_EGO_BLUEPRINT,
    EGO_PROXY_LENGTH_M,
    EGO_PROXY_WIDTH_M,
    SCHEMA_VERSION,
)
from .errors import ValidationError
from .jsonio import canonical_json_bytes, sha256_bytes


DETERMINISTIC_REQUIREMENT_PREDICATES = {
    "risk_level",
}


def deterministic_representative_arguments(
    category: str,
    predicate: str,
    polarity: str = "present",
    argument_domain: Iterable[Mapping[str, Any]] = (),
) -> list:
    """Choose one deterministic representative exclusively from the dev domain."""

    del category
    if polarity != "present" or predicate not in DETERMINISTIC_REQUIREMENT_PREDICATES:
        return []
    encoded = sorted(
        canonical_json_bytes(value).decode("utf-8") for value in argument_domain
    )
    return [json.loads(encoded[0])] if encoded else []


def _definition_hash(definition: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(definition))


def build_common_ontology(
    oracle_records: Iterable[Mapping[str, Any]], decision_status: str = "confirmed"
) -> Dict[str, Any]:
    records = list(oracle_records)
    atom_domains = {}
    cpd_variants = {}
    for oracle in records:
        for atom in oracle.get("atoms", []):
            key = (
                atom.get("category"),
                atom.get("predicate"),
                atom.get("polarity", "present"),
            )
            atom_domains.setdefault(key, set()).add(
                canonical_json_bytes(atom.get("arguments", {})).decode("utf-8")
            )
        for dimension in oracle.get("cpd_policy", {}).get("dimensions", []):
            definition = {
                field: dimension.get(field)
                for field in (
                    "name",
                    "common_semantic",
                    "cardinality",
                    "allowed_values",
                    "bin_definition_m",
                    "target_selector",
                )
                if field in dimension
            }
            name = definition.get("name")
            if not name:
                raise ValidationError("CPD dimension lacks a name")
            cpd_variants.setdefault(name, set()).add(
                canonical_json_bytes(definition).decode("utf-8")
            )

    concepts = []
    ego_definition = {
        "kind": "ego_proxy",
        "semantic_role": "ego_bus",
        "count": 1,
        "length_m": EGO_PROXY_LENGTH_M,
        "width_m": EGO_PROXY_WIDTH_M,
        "carla_blueprint": CARLA_EGO_BLUEPRINT,
        "metadrive_vehicle_model": "xl",
    }
    concepts.append(
        {
            "concept_id": "ego:bus_proxy",
            "kind": "ego_proxy",
            "definition": ego_definition,
            "definition_sha256": _definition_hash(ego_definition),
        }
    )
    for (category, predicate, polarity), encoded_arguments in sorted(atom_domains.items()):
        definition = {
            "kind": "requirement_atom_domain",
            "category": category,
            "predicate": predicate,
            "polarity": polarity,
            "argument_domain": [
                json.loads(value) for value in sorted(encoded_arguments)
            ],
        }
        concepts.append(
            {
                "concept_id": "atom:{}:{}:{}".format(
                    category, predicate, polarity
                ),
                "kind": "requirement_atom_domain",
                "definition": definition,
                "definition_sha256": _definition_hash(definition),
            }
        )
    for name, encoded_variants in sorted(cpd_variants.items()):
        definition = {
            "kind": "cpd_dimension",
            "name": name,
            "variants": [json.loads(value) for value in sorted(encoded_variants)],
        }
        concepts.append(
            {
                "concept_id": "cpd:{}".format(name),
                "kind": "cpd_dimension",
                "definition": definition,
                "definition_sha256": _definition_hash(definition),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_status": decision_status,
        "ontology_id": "bus-common-v0.2",
        "concepts": concepts,
    }
