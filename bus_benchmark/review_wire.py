"""Cross-language hashing for browser receipts, independent of formal JSON hashes.

JSON numbers are represented by finite IEEE-754 binary64 bytes. Typed trees
avoid collisions with user objects. Python-only canonical formal IDs are never
recomputed in the browser. Integers outside JavaScript's exact range fail closed.
"""
import math
import struct
import json
from .errors import ValidationError
from .jsonio import canonical_json_bytes, sha256_bytes


def wire_tree(value):
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, (int, float)):
        if isinstance(value, int) and abs(value) > 9007199254740991:
            raise ValidationError("browser packet integer exceeds the exact JavaScript range")
        number = float(value)
        if not math.isfinite(number):
            raise ValidationError("browser packet contains a nonfinite number")
        return ["num", struct.pack(">d", 0.0 if number == 0 else number).hex()]
    if isinstance(value, list):
        return ["list", [wire_tree(x) for x in value]]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return ["dict", [[k, wire_tree(value[k])] for k in sorted(value)]]
    raise ValidationError("unsupported browser packet value")


def wire_hash(value):
    return sha256_bytes(canonical_json_bytes(wire_tree(value)))


def ensure_browser_form(form):
    """Check numbers hidden inside legacy JSON-string editor fields as well."""
    from .oracle import REQUIRED_REVIEW_CHECKS
    if not isinstance(form, dict) or set(form) != {"required_check_decisions", "atom_decisions", "cpd_decision", "added_atoms_json", "notes"} or not isinstance(form["notes"], str):
        raise ValidationError("browser form has unknown or missing fields")
    checks = form["required_check_decisions"]
    if not isinstance(checks, dict) or set(checks) != set(REQUIRED_REVIEW_CHECKS):
        raise ValidationError("browser consistency checks are incomplete")
    for decision in checks.values():
        if not isinstance(decision, dict) or set(decision) != {"verdict", "reason"} or decision["verdict"] not in ("", "accept", "revise", "reject") or not isinstance(decision["reason"], str):
            raise ValidationError("browser consistency decision is malformed")
    if not isinstance(form["atom_decisions"], list):
        raise ValidationError("browser atom decisions must be a list")
    for decision in form["atom_decisions"]:
        if not isinstance(decision, dict) or set(decision) != {"atom_id", "verdict", "reason", "replacement_atoms_json"} or not isinstance(decision["atom_id"], str) or not isinstance(decision["reason"], str) or decision["verdict"] not in ("", "accept", "reject", "modify", "split", "merge"):
            raise ValidationError("browser atom decision is malformed")
    policy = form["cpd_decision"]
    if not isinstance(policy, dict) or set(policy) != {"verdict", "reason", "replacement_policy_json"} or policy["verdict"] not in ("", "accept", "revise", "reject") or not isinstance(policy["reason"], str):
        raise ValidationError("browser CPD decision is malformed")
    def check(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value) or (isinstance(value, int) or value.is_integer()) and abs(value) > 9007199254740991:
                raise ValidationError("editor number exceeds browser exact integer range; use the expert workflow")
        elif isinstance(value, list):
            for item in value:check(item)
        elif isinstance(value, dict):
            for item in value.values():check(item)
    try:
        values = [(form["added_atoms_json"], list), (form["cpd_decision"]["replacement_policy_json"], dict)]
        values += [(d["replacement_atoms_json"], list) for d in form["atom_decisions"]]
        for raw, expected in values:
            parsed = json.loads(raw)
            if not isinstance(parsed, expected):
                raise ValidationError("browser editor field has the wrong shape")
            check(parsed)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValidationError("browser form contains malformed editor data") from exc
