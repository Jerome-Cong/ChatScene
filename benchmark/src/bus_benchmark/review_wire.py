"""Cross-language hashing for browser receipts, independent of formal JSON hashes.

JSON numbers are represented by finite IEEE-754 binary64 bytes. Typed trees
avoid collisions with user objects. Python-only canonical formal IDs are never
recomputed in the browser. Integers outside JavaScript's exact range fail closed.
"""
import math
import struct
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
