"""Canonical JSON, JSONL, hashing, and atomic-write helpers."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Tuple

from .errors import ValidationError


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON value deterministically for hashing and manifests."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object key")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("non-JSON numeric constant {}".format(value))


def strict_json_value_bytes(payload: bytes, label: str) -> Any:
    """Parse UTF-8 JSON while rejecting duplicate keys and NaN/Infinity."""

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("{} is not strict UTF-8 JSON".format(label)) from exc


def strict_json_object_bytes(payload: bytes, label: str) -> Dict[str, Any]:
    """Parse one strict UTF-8 JSON object."""

    value = strict_json_value_bytes(payload, label)
    if not isinstance(value, dict):
        raise ValidationError("{} must be a JSON object".format(label))
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise ValidationError("cannot read JSON {}: {}".format(path, exc)) from exc
    return strict_json_value_bytes(payload, "JSON {}".format(path))


def iter_jsonl(path: Path) -> Iterator[Tuple[int, Dict[str, Any]]]:
    try:
        with Path(path).open("rb") as stream:
            for line_number, raw in enumerate(stream, 1):
                if not raw.strip():
                    continue
                try:
                    value = strict_json_object_bytes(
                        raw, "JSONL {}:{}".format(path, line_number)
                    )
                except ValidationError as exc:
                    raise ValidationError(
                        "invalid JSONL at {}:{}: {}".format(path, line_number, exc)
                    ) from exc
                yield line_number, value
    except OSError as exc:
        raise ValidationError("cannot read JSONL {}: {}".format(path, exc)) from exc


def read_jsonl(path: Path) -> list:
    return [record for _, record in iter_jsonl(path)]


def _atomic_replace(path: Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".{}-".format(path.name), dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_json(path: Path, value: Any) -> None:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ).encode("utf-8") + b"\n"
    _atomic_replace(Path(path), payload)


def write_jsonl(path: Path, values: Iterable[Dict[str, Any]]) -> None:
    lines = [canonical_json_bytes(value) for value in values]
    payload = b"\n".join(lines) + (b"\n" if lines else b"")
    _atomic_replace(Path(path), payload)
