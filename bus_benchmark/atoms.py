"""Canonical requirement-atom representation and set operations."""

import json
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from .constants import ATOM_CATEGORIES
from .errors import ValidationError
from .jsonio import canonical_json_bytes, sha256_bytes


def normalize_count(value: Any) -> str:
    """Normalize actor cardinality without pretending open counts are exact."""

    if isinstance(value, bool):
        raise ValidationError("actor count cannot be boolean")
    if isinstance(value, int):
        if value < 0:
            raise ValidationError("actor count cannot be negative")
        return str(value)
    if isinstance(value, str):
        if value in ("multiple", "optional"):
            return value
        if value.isdigit():
            return str(int(value))
    raise ValidationError("unsupported actor count {!r}".format(value))


def make_atom(
    category: str,
    predicate: str,
    arguments: Mapping[str, Any],
    layer: str = "core_required",
    polarity: str = "present",
    weight: float = 1.0,
    provenance: Mapping[str, Any] = None,
    decision_status: str = "draft",
    notes: str = "",
) -> Dict[str, Any]:
    if category not in ATOM_CATEGORIES:
        raise ValidationError("unknown atom category {!r}".format(category))
    if polarity not in ("present", "absent"):
        raise ValidationError("atom polarity must be present or absent")
    if float(weight) != 1.0:
        raise ValidationError("benchmark v0.1 requires every requirement atom weight to be 1.0")
    base = {
        "category": category,
        "predicate": str(predicate),
        "arguments": dict(arguments),
        "layer": layer,
        "polarity": polarity,
        "weight": 1.0,
    }
    identity = {
        "category": base["category"],
        "predicate": base["predicate"],
        "arguments": base["arguments"],
        "polarity": base["polarity"],
    }
    base.update(
        {
            "atom_id": sha256_bytes(canonical_json_bytes(identity))[:16],
            "provenance": dict(provenance or {}),
            "decision_status": decision_status,
            "notes": notes,
        }
    )
    return base


def atom_key(atom: Mapping[str, Any], include_polarity: bool = False) -> str:
    value = {
        "category": atom.get("category"),
        "predicate": atom.get("predicate"),
        "arguments": atom.get("arguments", {}),
    }
    if include_polarity:
        value["polarity"] = atom.get("polarity", "present")
    return canonical_json_bytes(value).decode("utf-8")


def canonical_atom_set(atoms: Iterable[Mapping[str, Any]]) -> set:
    return {atom_key(atom) for atom in atoms}


def atoms_by_category(atoms: Iterable[Mapping[str, Any]]) -> Dict[str, List[Mapping[str, Any]]]:
    result = {category: [] for category in ATOM_CATEGORIES}
    for atom in atoms:
        category = atom.get("category")
        if category in result:
            result[category].append(atom)
    return result


def parse_atom_key(key: str) -> Dict[str, Any]:
    value = json.loads(key)
    if not isinstance(value, dict):
        raise ValidationError("invalid atom key")
    return value
