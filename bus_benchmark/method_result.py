"""Tamper-evident formal results for one frozen method/platform cell."""

import copy
import datetime as _datetime
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .cpd import recompute_cpd_aggregate
from .errors import ValidationError
from .freeze import verify_freeze_manifest
from .generation import validate_generation_response_chain
from .jsonio import (
    canonical_json_bytes,
    read_json,
    sha256_bytes,
    strict_json_object_bytes,
    strict_json_value_bytes,
)
from .judge_pipeline import validate_judge_run_bundle
from .metrics import aggregate_semantic_metrics, compute_rqs, compute_uqh
from .provenance import (
    metric_source_hash,
    record_sha256,
    validate_aggregate_response_chain,
    validate_evidence_response_chain,
    validate_frozen_judge_response_chain,
    validate_score_judge_run_provenance,
    validate_score_provenance,
    validate_scores_against_evidence,
)
from .roster import validate_records_against_roster
from .runtime import (
    _metadrive_tracked_source_snapshot,
    aggregate_runtime,
    validate_platform_runtime_config,
)
from .schema import validate_schema_instance, validate_schema_records


METHOD_RESULT_INPUT_ROLES = (
    "controller_config",
    "cpd_aggregate",
    "cpd_coverage",
    "freeze_manifest",
    "generation_evidence_index",
    "generation_responses",
    "generation_run_manifest",
    "judge_execution_index",
    "judge_request_manifest",
    "judge_responses",
    "judge_run_manifest",
    "method_config",
    "platform_config",
    "query_library_test",
    "query_roster",
    "requirement_oracle_test",
    "runtime_aggregate",
    "runtime_records",
    "semantic_aggregate",
    "semantic_evidence",
    "semantic_scores",
    "uqh_assessments",
    "uqh_assessor_registry",
)


_MAX_CLOSURE_FILES = 250000
_MAX_DIRECTORY_SCANNED_ENTRIES = 250000
_MAX_DIRECTORY_INVENTORY_ENTRIES = 250000
_MAX_CLOSURE_BYTES = 128 * 1024 * 1024 * 1024
_MAX_JSON_PAYLOAD_BYTES = 64 * 1024 * 1024
_MAX_DIRECTORY_DEPTH = 64
_RUNTIME_TREE_POLICY = "exclude_python_cache_v0.1"
_METADRIVE_TREE_POLICY = "git_tracked_worktree_bytes_v0.1"
_UNSAFE_DIRECTORY_ROOTS = {"/", "/home", "/tmp"}


@dataclass(frozen=True)
class MethodResultPaths:
    freeze_manifest: Path
    library: Path
    oracle: Path
    roster: Path
    method_config: Path
    generation_dir: Path
    responses: Path
    semantic_evidence: Path
    semantic_scores: Path
    judge_run_dir: Path
    semantic_aggregate: Path
    uqh_assessments: Path
    uqh_assessor_registry: Path
    cpd_aggregate: Path
    cpd_coverage: Path
    runtime_records: Path
    runtime_aggregate: Path
    platform_config: Path
    controller_config: Path


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).relative_to(Path(root))
    except ValueError:
        return False
    return True


def _canonical_absolute_path_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\x00" in value
        or value == "/"
        or os.path.normpath(value) != value
    ):
        raise ValidationError("{} must be a canonical absolute path".format(label))
    return value


def _stat_fingerprint(
    value: os.stat_result,
) -> Tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@dataclass(frozen=True)
class _GuardedFile:
    role: str
    path: str
    sha256: str
    bytes: int
    file_fingerprint: Tuple[int, int, int, int, int, int, int]
    parent_fingerprints: Tuple[
        Tuple[str, Tuple[int, int, int, int, int, int, int]], ...
    ]

    def public_binding(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


def _open_no_follow(path: Path, role: str) -> Tuple[int, tuple]:
    """Open one absolute path component-by-component without following symlinks."""

    absolute = Path(os.path.abspath(str(path)))
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    parent_fingerprints = []
    directory_fd = None
    try:
        directory_fd = os.open(absolute.anchor, directory_flags)
        parent_fingerprints.append(
            (absolute.anchor, _stat_fingerprint(os.fstat(directory_fd)))
        )
        current = Path(absolute.anchor)
        for component in absolute.parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
            current = current / component
            parent_fingerprints.append(
                (str(current), _stat_fingerprint(os.fstat(directory_fd)))
            )
        descriptor = os.open(absolute.name, file_flags, dir_fd=directory_fd)
    except (OSError, ValueError) as exc:
        raise ValidationError(
            "{} must be reachable as a regular non-symlink file".format(role)
        ) from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    return descriptor, tuple(parent_fingerprints)


def _open_directory_no_follow(path: Path, role: str) -> Tuple[int, tuple]:
    """Open and retain one directory FD without following any path component."""

    absolute = Path(os.path.abspath(str(path)))
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    fingerprints = []
    descriptor = None
    try:
        descriptor = os.open(absolute.anchor, flags)
        fingerprints.append(
            (absolute.anchor, _stat_fingerprint(os.fstat(descriptor)))
        )
        current = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            current = current / component
            fingerprints.append(
                (str(current), _stat_fingerprint(os.fstat(descriptor)))
            )
    except (OSError, ValueError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ValidationError(
            "{} must be reachable as a non-symlink directory".format(role)
        ) from exc
    return descriptor, tuple(fingerprints)


def _capture_guarded_file(
    path: Path, role: str, *, keep_payload: bool = False
) -> Tuple[_GuardedFile, Optional[bytes]]:
    absolute = Path(os.path.abspath(str(path)))
    descriptor, parent_fingerprints = _open_no_follow(absolute, role)
    chunks = [] if keep_payload else None
    digest = hashlib.sha256()
    total_bytes = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValidationError("{} must be a regular file".format(role))
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total_bytes += len(chunk)
            if keep_payload and total_bytes > _MAX_JSON_PAYLOAD_BYTES:
                raise ValidationError(
                    "method-result JSON input exceeds its payload limit: {}".format(
                        role
                    )
                )
            if chunks is not None:
                chunks.append(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ValidationError("cannot capture method-result input {}".format(role)) from exc
    finally:
        os.close(descriptor)
    if _stat_fingerprint(before) != _stat_fingerprint(after):
        raise ValidationError(
            "method-result input changed while its stable FD was read: {}".format(role)
        )
    payload = b"".join(chunks) if chunks is not None else None
    guard = _GuardedFile(
        role=role,
        path=str(absolute),
        sha256=digest.hexdigest(),
        bytes=total_bytes,
        file_fingerprint=_stat_fingerprint(after),
        parent_fingerprints=parent_fingerprints,
    )
    return guard, payload


class _InputSnapshots(list):
    """Public 23 bindings plus private stable-FD guards for transitive leaves."""

    def __init__(self, values=()):
        super().__init__(values)
        self.guards: Dict[str, _GuardedFile] = {}
        self.payloads: Dict[str, bytes] = {}
        self.directory_inventories: Dict[str, Tuple[str, ...]] = {}
        self.directory_policies: Dict[str, Optional[str]] = {}
        self.directory_bindings: Dict[str, Mapping[str, Any]] = {}
        self.parsed_json_paths = set()
        self.total_bytes = 0
        self.sealed = False

    def capture(self, path: Path, role: str, *, keep_payload: bool = False) -> _GuardedFile:
        absolute = str(Path(os.path.abspath(str(path))))
        existing = self.guards.get(absolute)
        if existing is not None:
            if keep_payload and absolute not in self.payloads:
                guard, payload = _capture_guarded_file(
                    Path(absolute), role, keep_payload=True
                )
                if (
                    guard.sha256 != existing.sha256
                    or guard.bytes != existing.bytes
                    or guard.file_fingerprint != existing.file_fingerprint
                ):
                    raise ValidationError(
                        "method-result input changed during transitive discovery: {}".format(
                            role
                        )
                    )
                self.payloads[absolute] = payload or b""
            return existing
        if self.sealed:
            raise ValidationError(
                "method-result attempted to read an unsealed input: {}".format(role)
            )
        guard, payload = _capture_guarded_file(
            Path(absolute), role, keep_payload=keep_payload
        )
        if len(self.guards) + 1 > _MAX_CLOSURE_FILES:
            raise ValidationError("method-result input closure exceeds its file limit")
        if self.total_bytes + guard.bytes > _MAX_CLOSURE_BYTES:
            raise ValidationError("method-result input closure exceeds its byte limit")
        self.guards[absolute] = guard
        self.total_bytes += guard.bytes
        if payload is not None:
            self.payloads[absolute] = payload
        return guard

    def payload(self, path: Path) -> bytes:
        absolute = str(Path(os.path.abspath(str(path))))
        if absolute not in self.payloads:
            self.capture(Path(absolute), "transitive JSON", keep_payload=True)
        return self.payloads[absolute]

    def seal(self) -> None:
        self.sealed = True

    def closure_document(self) -> Dict[str, Any]:
        return {
            "files": [
                {
                    "path": guard.path,
                    "sha256": guard.sha256,
                    "bytes": guard.bytes,
                }
                for guard in sorted(self.guards.values(), key=lambda item: item.path)
            ],
            "directories": [
                {
                    "path": path,
                    "policy": self.directory_policies.get(path),
                    "inventory": list(inventory),
                }
                for path, inventory in sorted(self.directory_inventories.items())
            ],
        }


def _snapshot_file(path: Path, role: str) -> Dict[str, Any]:
    guard, _ = _capture_guarded_file(path, role, keep_payload=False)
    return guard.public_binding()


def _recheck_guarded_file(
    expected: _GuardedFile, *, check_parent_ctime: bool = True
) -> None:
    current, _ = _capture_guarded_file(
        Path(expected.path), expected.role, keep_payload=False
    )
    if (
        current.sha256 != expected.sha256
        or current.bytes != expected.bytes
        or current.file_fingerprint != expected.file_fingerprint
    ):
        raise ValidationError(
            "method-result input changed during validation: {}".format(expected.role)
        )
    old_parent_identities = [
        (path, fingerprint[:3]) for path, fingerprint in expected.parent_fingerprints
    ]
    new_parent_identities = [
        (path, fingerprint[:3]) for path, fingerprint in current.parent_fingerprints
    ]
    if old_parent_identities != new_parent_identities:
        raise ValidationError(
            "method-result input parent identity changed during validation: {}".format(
                expected.role
            )
        )
    if check_parent_ctime:
        volatile_roots = {"/", "/tmp", "/home"}
        old_guarded = [
            value
            for value in expected.parent_fingerprints
            if value[0] not in volatile_roots
        ]
        new_guarded = [
            value
            for value in current.parent_fingerprints
            if value[0] not in volatile_roots
        ]
        if old_guarded != new_guarded:
            raise ValidationError(
                "method-result input parent changed during validation: {}".format(
                    expected.role
                )
            )


def _recheck_snapshots(
    snapshots: Sequence[Mapping[str, Any]], *, check_parent_ctime: bool = True
) -> None:
    if isinstance(snapshots, _InputSnapshots):
        for directory, expected_inventory in snapshots.directory_inventories.items():
            policy = snapshots.directory_policies.get(directory)
            if policy == _METADRIVE_TREE_POLICY:
                expected_binding = snapshots.directory_bindings[directory]
                current_binding, tracked_paths = _metadrive_tracked_source_snapshot(
                    Path(expected_binding["repository_path"]),
                    expected_binding["tracked_subpath"],
                )
                if canonical_json_bytes(current_binding) != canonical_json_bytes(
                    expected_binding
                ):
                    raise ValidationError(
                        "method-result MetaDrive tracked source binding changed"
                    )
                prefix = expected_binding["tracked_subpath"] + "/"
                current_inventory = tuple(
                    "f:" + value[len(prefix) :]
                    for value in sorted(tracked_paths)
                    if value.startswith(prefix)
                )
            else:
                current_inventory, _ = _enumerate_directory(
                    Path(directory),
                    "transitive input",
                    policy=policy,
                )
            if current_inventory != expected_inventory:
                raise ValidationError(
                    "method-result transitive directory inventory changed: {}".format(
                        directory
                    )
                )
        for expected in snapshots.guards.values():
            _recheck_guarded_file(expected, check_parent_ctime=check_parent_ctime)
        return
    for snapshot in snapshots:
        current = _snapshot_file(Path(snapshot["path"]), snapshot["role"])
        if canonical_json_bytes(current) != canonical_json_bytes(dict(snapshot)):
            raise ValidationError(
                "method-result input changed during validation: {}".format(
                    snapshot["role"]
                )
            )


def _frozen_asset(
    manifest: Mapping[str, Any], role: str, path: Path
) -> Mapping[str, Any]:
    resolved = str(Path(path).resolve())
    matches = [
        asset
        for asset in manifest.get("assets", [])
        if asset.get("role") == role
        and str(Path(asset.get("path", "")).resolve()) == resolved
    ]
    if len(matches) != 1:
        raise ValidationError(
            "formal method-result requires one frozen {} asset at {}".format(
                role, resolved
            )
        )
    return matches[0]


def _require_unique_frozen_main_config(
    manifest: Mapping[str, Any], selected_path: Path, method_id: str, platform: str
) -> None:
    matches = []
    for asset in manifest.get("assets", []):
        if asset.get("role") != "method_config":
            continue
        candidate = read_json(Path(asset["path"]))
        if (
            candidate.get("method_id") == method_id
            and candidate.get("platform") == platform
        ):
            matches.append(asset)
    if len(matches) != 1 or Path(matches[0]["path"]).resolve() != Path(
        selected_path
    ).resolve():
        raise ValidationError(
            "formal method-result requires exactly one frozen main config per cell"
        )


def _require_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("formal method-result {} must be numeric".format(label))
    normalized = float(value)
    if not 0.0 <= normalized <= 1.0:
        raise ValidationError("formal method-result {} must be in [0,1]".format(label))
    return normalized


def _same_json(actual: Any, expected: Any, label: str) -> None:
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise ValidationError("{} differs from the recomputed formal value".format(label))


def _snapshot_json(path: Path, snapshots: _InputSnapshots) -> Any:
    return strict_json_value_bytes(
        snapshots.payload(path), "stable method-result JSON {}".format(path)
    )


def _snapshot_jsonl(path: Path, snapshots: _InputSnapshots) -> list:
    records = []
    payload = snapshots.payload(path)
    for line_number, raw in enumerate(payload.splitlines(), 1):
        if not raw.strip():
            continue
        records.append(
            strict_json_object_bytes(
                raw, "stable method-result JSONL {}:{}".format(path, line_number)
            )
        )
    return records


def _json_values_from_payload(path: Path, payload: bytes) -> list:
    if path.suffix == ".jsonl":
        values = []
        for line_number, raw in enumerate(payload.splitlines(), 1):
            if raw.strip():
                values.append(
                    strict_json_object_bytes(
                        raw,
                        "transitive method-result JSONL {}:{}".format(
                            path, line_number
                        ),
                    )
                )
        return values
    if path.suffix == ".json":
        return [
            strict_json_value_bytes(
                payload, "transitive method-result JSON {}".format(path)
            )
        ]
    return []


def _queue_snapshot_json_once(
    path: Path, snapshots: _InputSnapshots, pending_values: list
) -> None:
    absolute = str(Path(os.path.abspath(str(path))))
    if absolute in snapshots.parsed_json_paths:
        return
    snapshots.parsed_json_paths.add(absolute)
    pending_values.extend(
        _json_values_from_payload(Path(absolute), snapshots.payload(Path(absolute)))
    )


def _enumerate_directory(
    path: Path, role: str, policy: str = None
) -> Tuple[Tuple[str, ...], list]:
    path_text = _canonical_absolute_path_text(str(path), "{} directory".format(role))
    root = Path(path_text)
    if str(root) in _UNSAFE_DIRECTORY_ROOTS:
        raise ValidationError("{} directory root is too broad".format(role))
    if _has_symlink_component(root):
        raise ValidationError("{} directory must not contain symlinks".format(role))
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise ValidationError("{} directory is missing".format(role)) from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValidationError("{} must be a directory".format(role))
    inventory = []
    files = []

    file_bytes = 0
    scanned_entries = 0

    def visit(directory: Path, depth: int) -> None:
        nonlocal file_bytes, scanned_entries
        if depth > _MAX_DIRECTORY_DEPTH:
            raise ValidationError("{} directory exceeds its depth limit".format(role))
        try:
            entries = []
            with os.scandir(str(directory)) as iterator:
                for entry in iterator:
                    scanned_entries += 1
                    if scanned_entries > _MAX_DIRECTORY_SCANNED_ENTRIES:
                        raise ValidationError(
                            "{} directory exceeds its scanned-entry limit".format(role)
                        )
                    entries.append(entry)
        except OSError as exc:
            raise ValidationError("cannot enumerate {} directory".format(role)) from exc
        entries.sort(key=lambda item: item.name)
        for entry in entries:
            child = directory / entry.name
            relative = child.relative_to(root).as_posix()
            try:
                child_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValidationError("cannot stat {} transitive input".format(role)) from exc
            if stat.S_ISLNK(child_stat.st_mode):
                raise ValidationError(
                    "{} transitive tree contains a symlink: {}".format(role, relative)
                )
            if stat.S_ISDIR(child_stat.st_mode):
                if policy == _RUNTIME_TREE_POLICY and entry.name == "__pycache__":
                    continue
                inventory.append("d:" + relative)
                if len(inventory) > _MAX_DIRECTORY_INVENTORY_ENTRIES:
                    raise ValidationError(
                        "{} directory exceeds its inventory-entry limit".format(role)
                    )
                visit(child, depth + 1)
            elif stat.S_ISREG(child_stat.st_mode):
                if policy == _RUNTIME_TREE_POLICY and entry.name.endswith(
                    (".pyc", ".pyo")
                ):
                    continue
                inventory.append("f:" + relative)
                if len(inventory) > _MAX_DIRECTORY_INVENTORY_ENTRIES:
                    raise ValidationError(
                        "{} directory exceeds its inventory-entry limit".format(role)
                    )
                files.append(child)
                file_bytes += child_stat.st_size
                if len(files) > _MAX_CLOSURE_FILES:
                    raise ValidationError(
                        "{} directory exceeds its file limit".format(role)
                    )
                if file_bytes > _MAX_CLOSURE_BYTES:
                    raise ValidationError(
                        "{} directory exceeds its byte limit".format(role)
                    )
            else:
                raise ValidationError(
                    "{} transitive tree contains a special file: {}".format(
                        role, relative
                    )
                )

    visit(root, 0)
    return tuple(inventory), files


def _capture_directory(
    path: Path,
    role: str,
    snapshots: _InputSnapshots,
    policy: str = None,
) -> Tuple[Tuple[str, ...], list]:
    absolute = _canonical_absolute_path_text(
        str(path), "{} directory".format(role)
    )
    inventory, files = _enumerate_directory(Path(absolute), role, policy=policy)
    previous = snapshots.directory_inventories.get(absolute)
    if previous is not None:
        if previous != inventory:
            raise ValidationError(
                "method-result transitive directory changed during discovery: {}".format(
                    role
                )
            )
        return inventory, files
    snapshots.directory_inventories[absolute] = inventory
    snapshots.directory_policies[absolute] = policy
    for index, child in enumerate(files):
        snapshots.capture(child, "{} file[{}]".format(role, index))
    return inventory, files


def _walk_mappings(value: Any):
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            yield current
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)


def _file_binding(mapping: Mapping[str, Any]) -> Optional[Tuple[str, str, int]]:
    keys = set(mapping)
    if keys not in (
        {"path", "sha256", "bytes"},
        {"role", "path", "sha256", "bytes"},
        {"kind", "path", "sha256", "bytes"},
    ):
        return None
    path = mapping.get("path")
    digest = mapping.get("sha256")
    size = mapping.get("bytes")
    if (
        not isinstance(path, str)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or type(size) is not int
        or size < 0
    ):
        return None
    return path, digest, size


def _runtime_tree_binding(mapping: Mapping[str, Any]) -> bool:
    return (
        set(mapping)
        == {"role", "path", "sha256", "bytes", "file_count", "policy"}
        and mapping.get("policy") == _RUNTIME_TREE_POLICY
        and mapping.get("role")
        in {"carla_server_distribution", "scenic_editable_source"}
        and isinstance(mapping.get("path"), str)
        and isinstance(mapping.get("sha256"), str)
        and len(mapping["sha256"]) == 64
        and all(character in "0123456789abcdef" for character in mapping["sha256"])
        and type(mapping.get("file_count")) is int
        and 1 <= mapping["file_count"] <= _MAX_CLOSURE_FILES
        and type(mapping.get("bytes")) is int
        and 1 <= mapping["bytes"] <= _MAX_CLOSURE_BYTES
    )


def _metadrive_tree_binding(mapping: Mapping[str, Any]) -> bool:
    return (
        set(mapping)
        == {
            "repository_path",
            "revision",
            "tracked_subpath",
            "tracked_tree_sha256",
            "tracked_file_count",
            "tracked_bytes",
            "policy",
        }
        and mapping.get("policy") == _METADRIVE_TREE_POLICY
        and isinstance(mapping.get("repository_path"), str)
        and isinstance(mapping.get("revision"), str)
        and len(mapping["revision"]) == 40
        and all(character in "0123456789abcdef" for character in mapping["revision"])
        and mapping.get("tracked_subpath") == "metadrive"
        and isinstance(mapping.get("tracked_tree_sha256"), str)
        and len(mapping["tracked_tree_sha256"]) == 64
        and all(
            character in "0123456789abcdef"
            for character in mapping["tracked_tree_sha256"]
        )
        and type(mapping.get("tracked_file_count")) is int
        and 1 <= mapping["tracked_file_count"] <= _MAX_CLOSURE_FILES
        and type(mapping.get("tracked_bytes")) is int
        and 1 <= mapping["tracked_bytes"] <= _MAX_CLOSURE_BYTES
    )


def _capture_runtime_tree(
    binding: Mapping[str, Any], role: str, snapshots: _InputSnapshots
) -> None:
    if not _runtime_tree_binding(binding):
        raise ValidationError("{} must be an exact bounded runtime tree binding".format(role))
    root = Path(
        _canonical_absolute_path_text(binding["path"], "{} path".format(role))
    )
    _, files = _capture_directory(root, role, snapshots, policy=_RUNTIME_TREE_POLICY)
    records = []
    total_bytes = 0
    for child in files:
        guard = snapshots.guards[str(child)]
        records.append(
            {
                "path": child.relative_to(root).as_posix(),
                "sha256": guard.sha256,
                "bytes": guard.bytes,
            }
        )
        total_bytes += guard.bytes
    if (
        len(records) != binding["file_count"]
        or total_bytes != binding["bytes"]
        or sha256_bytes(canonical_json_bytes(records)) != binding["sha256"]
    ):
        raise ValidationError("{} differs from its bound tree bytes".format(role))


def _capture_metadrive_tree(
    binding: Mapping[str, Any], role: str, snapshots: _InputSnapshots
) -> None:
    if not _metadrive_tree_binding(binding):
        raise ValidationError("{} must be an exact bounded MetaDrive binding".format(role))
    repository = Path(
        _canonical_absolute_path_text(
            binding["repository_path"], "{} repository".format(role)
        )
    )
    before, tracked_paths = _metadrive_tracked_source_snapshot(
        repository, binding["tracked_subpath"]
    )
    if canonical_json_bytes(before) != canonical_json_bytes(binding):
        raise ValidationError("{} differs from its git-tracked binding".format(role))
    source_root = repository / binding["tracked_subpath"]
    source_root_text = _canonical_absolute_path_text(
        str(source_root), "{} source root".format(role)
    )
    prefix = binding["tracked_subpath"] + "/"
    relative_paths = []
    for index, relative_text in enumerate(sorted(tracked_paths)):
        if not relative_text.startswith(prefix):
            raise ValidationError("{} inventory escapes its tracked subtree".format(role))
        relative = relative_text[len(prefix) :]
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValidationError("{} inventory contains an unsafe path".format(role))
        snapshots.capture(
            repository / relative_text, "{} file[{}]".format(role, index)
        )
        relative_paths.append("f:" + relative)
    after, after_paths = _metadrive_tracked_source_snapshot(
        repository, binding["tracked_subpath"]
    )
    if (
        canonical_json_bytes(after) != canonical_json_bytes(binding)
        or after_paths != tracked_paths
    ):
        raise ValidationError("{} changed while its tracked files were captured".format(role))
    inventory = tuple(relative_paths)
    snapshots.directory_inventories[source_root_text] = inventory
    snapshots.directory_policies[source_root_text] = _METADRIVE_TREE_POLICY
    snapshots.directory_bindings[source_root_text] = dict(binding)


def _capture_bound_files(
    snapshots: _InputSnapshots, initial_values: Sequence[Any]
) -> None:
    pending_values = list(initial_values)
    pending_files = []
    while pending_values or pending_files:
        while pending_values:
            value = pending_values.pop()
            for mapping in _walk_mappings(value):
                binding = _file_binding(mapping)
                if binding is not None:
                    pending_files.append(binding)
        while pending_files:
            path_text, expected_sha256, expected_bytes = pending_files.pop()
            absolute = Path(
                _canonical_absolute_path_text(path_text, "transitive file binding")
            )
            guard = snapshots.capture(
                absolute,
                "transitive file binding",
                keep_payload=absolute.suffix in {".json", ".jsonl"},
            )
            if guard.sha256 != expected_sha256 or guard.bytes != expected_bytes:
                raise ValidationError("transitive file differs from its binding")
            if absolute.suffix in {".json", ".jsonl"}:
                _queue_snapshot_json_once(absolute, snapshots, pending_values)


def _runtime_stage_bindings(record: Mapping[str, Any]):
    stages = [record.get("compile"), record.get("ne")]
    stages.extend(record.get("sv_trials", []))
    for stage in stages:
        for mapping in _walk_mappings(stage):
            binding = _file_binding(mapping)
            if binding is not None:
                yield binding


def _discover_transitive_inputs(
    snapshots: _InputSnapshots,
    initial_values: Sequence[Any],
    directories: Sequence[Tuple[Path, str]],
    *,
    runtime_records: Sequence[Mapping[str, Any]],
    runtime_records_root: Path,
    platform_config: Mapping[str, Any],
    controller_config: Mapping[str, Any],
) -> None:
    # Directory enumeration is capture-only: its JSON files are never parsed.
    # Only the 23 public documents and JSON reached through an exact verified
    # file binding can introduce more file bindings.
    for directory, role in directories:
        _capture_directory(
            Path(_canonical_absolute_path_text(str(directory), role)), role, snapshots
        )

    runtime_records_root = Path(
        _canonical_absolute_path_text(
            str(runtime_records_root), "runtime records directory"
        )
    )
    for index, record in enumerate(runtime_records):
        provenance = record.get("provenance", {})
        attempt_text = provenance.get("attempt_path")
        if not isinstance(attempt_text, str):
            continue
        attempt = Path(
            _canonical_absolute_path_text(
                attempt_text, "runtime record[{}] attempt_path".format(index)
            )
        )
        if str(attempt) in _UNSAFE_DIRECTORY_ROOTS:
            raise ValidationError("runtime attempt directory root is too broad")
        if attempt == runtime_records_root or not _path_is_within(
            attempt, runtime_records_root
        ):
            raise ValidationError(
                "runtime attempt escapes the bound runtime-record directory"
            )
        for stage_path, _, _ in _runtime_stage_bindings(record):
            canonical = Path(
                _canonical_absolute_path_text(
                    stage_path, "runtime record[{}] stage artifact".format(index)
                )
            )
            if not _path_is_within(canonical, attempt):
                raise ValidationError("runtime stage artifact escapes its attempt directory")
        _capture_directory(attempt, "runtime attempt[{}]".format(index), snapshots)

    implementation = controller_config.get("implementation", {})
    if isinstance(implementation, Mapping):
        source_path = implementation.get("source_path")
        source_sha256 = implementation.get("source_sha256")
        if isinstance(source_path, str) and isinstance(source_sha256, str):
            source = Path(
                _canonical_absolute_path_text(
                    source_path, "controller implementation source"
                )
            )
            guard = snapshots.capture(source, "controller implementation source")
            if guard.sha256 != source_sha256:
                raise ValidationError("controller implementation source hash differs")

    path_documents = [
        (platform_config.get("worker", {}), "platform runtime config")
    ]
    path_documents.extend(
        (
            {
                "worker": record.get("provenance", {}).get("worker_path"),
                "worker_sha256": record.get("provenance", {}).get("worker_sha256"),
                "interpreter": record.get("provenance", {}).get("interpreter_path"),
                "interpreter_sha256": record.get("provenance", {}).get(
                    "interpreter_sha256"
                ),
            },
            "runtime provenance",
        )
        for record in runtime_records
    )
    for document, label in path_documents:
        if not isinstance(document, Mapping):
            continue
        for path_key, sha_key, path_label in (
            ("worker", "worker_sha256", "worker"),
            ("interpreter", "interpreter_sha256", "interpreter"),
        ):
            path_text = document.get(path_key)
            digest = document.get(sha_key)
            if isinstance(path_text, str) and isinstance(digest, str):
                path = Path(
                    _canonical_absolute_path_text(
                        path_text, "{} {}".format(label, path_label)
                    )
                )
                guard = snapshots.capture(path, "{} {}".format(label, path_label))
                if guard.sha256 != digest:
                    raise ValidationError("{} {} hash differs".format(label, path_label))

    special_values = [platform_config]
    special_values.extend(
        record.get("provenance", {}).get("worker_config", {})
        for record in runtime_records
    )
    seen_runtime_trees = set()
    seen_metadrive_trees = set()
    for value in special_values:
        for mapping in _walk_mappings(value):
            if _runtime_tree_binding(mapping):
                key = canonical_json_bytes(mapping)
                if key not in seen_runtime_trees:
                    seen_runtime_trees.add(key)
                    _capture_runtime_tree(mapping, "CARLA runtime dependency", snapshots)
            if _metadrive_tree_binding(mapping):
                key = canonical_json_bytes(mapping)
                if key not in seen_metadrive_trees:
                    seen_metadrive_trees.add(key)
                    _capture_metadrive_tree(mapping, "MetaDrive tracked source", snapshots)

    _capture_bound_files(snapshots, initial_values)


def _capture_inputs(paths: MethodResultPaths) -> list:
    generation_dir = Path(os.path.abspath(str(paths.generation_dir)))
    committed_response = generation_dir / "response.jsonl"
    if Path(os.path.abspath(str(paths.responses))) != committed_response:
        raise ValidationError(
            "formal method-result responses must be generation-dir/response.jsonl"
        )
    explicit = [
        ("freeze_manifest", paths.freeze_manifest),
        ("query_library_test", paths.library),
        ("requirement_oracle_test", paths.oracle),
        ("query_roster", paths.roster),
        ("method_config", paths.method_config),
        ("generation_responses", paths.responses),
        ("generation_evidence_index", generation_dir / "evidence.jsonl"),
        ("generation_run_manifest", generation_dir / "generation_run_manifest.json"),
        ("semantic_evidence", paths.semantic_evidence),
        ("semantic_scores", paths.semantic_scores),
        ("semantic_aggregate", paths.semantic_aggregate),
        ("uqh_assessments", paths.uqh_assessments),
        ("uqh_assessor_registry", paths.uqh_assessor_registry),
        ("cpd_aggregate", paths.cpd_aggregate),
        ("cpd_coverage", paths.cpd_coverage),
        ("runtime_records", paths.runtime_records),
        ("runtime_aggregate", paths.runtime_aggregate),
        ("platform_config", paths.platform_config),
        ("controller_config", paths.controller_config),
    ]
    run_dir = Path(os.path.abspath(str(paths.judge_run_dir)))
    run_manifest_path = run_dir / "judge_run_manifest.json"
    snapshots = _InputSnapshots()
    run_manifest_guard = snapshots.capture(
        run_manifest_path, "judge_run_manifest", keep_payload=True
    )
    run_manifest = _snapshot_json(run_manifest_path, snapshots)
    validate_schema_instance(
        run_manifest, "judge_run_manifest", context="method-result Judge run manifest"
    )
    request_manifest_path = Path(
        _canonical_absolute_path_text(
            run_manifest.get("request_manifest", {}).get("path"),
            "Judge request manifest path",
        )
    )
    if request_manifest_path.name != "request_manifest.json":
        raise ValidationError("Judge request manifest must be request_manifest.json")
    explicit.extend(
        [
            ("judge_execution_index", run_dir / "execution_index.jsonl"),
            ("judge_responses", run_dir / "judge_responses.jsonl"),
            ("judge_request_manifest", request_manifest_path),
        ]
    )
    public = []
    for role, path in explicit:
        guard = snapshots.capture(path, role, keep_payload=True)
        public.append(guard.public_binding())
    public.append(run_manifest_guard.public_binding())
    snapshots.extend(sorted(public, key=lambda value: value["role"]))
    roles = [value["role"] for value in snapshots]
    if tuple(roles) != METHOD_RESULT_INPUT_ROLES:
        raise AssertionError("method-result input roles must be the exact frozen set")
    public_identities = [
        snapshots.guards[binding["path"]].file_fingerprint[:2]
        for binding in snapshots
    ]
    if len(public_identities) != len(set(public_identities)):
        raise ValidationError(
            "method-result public inputs must have distinct file identities"
        )
    initial_values = []
    for binding in snapshots:
        path = Path(binding["path"])
        _queue_snapshot_json_once(path, snapshots, initial_values)
    runtime_records = _snapshot_jsonl(Path(paths.runtime_records), snapshots)
    platform_config = _snapshot_json(Path(paths.platform_config), snapshots)
    controller_config = _snapshot_json(Path(paths.controller_config), snapshots)
    validate_schema_records(
        runtime_records,
        "runtime_record",
        context="method-result discovery runtime records",
    )
    validate_schema_instance(
        platform_config,
        "platform_runtime_config",
        context="method-result discovery platform config",
    )
    validate_schema_instance(
        controller_config,
        "controller_config",
        context="method-result discovery controller config",
    )
    _discover_transitive_inputs(
        snapshots,
        initial_values,
        (
            (generation_dir, "generation bundle"),
            (run_dir, "Judge run bundle"),
            (request_manifest_path.parent, "Judge request bundle"),
        ),
        runtime_records=runtime_records,
        runtime_records_root=Path(os.path.abspath(str(paths.runtime_records))).parent,
        platform_config=platform_config,
        controller_config=controller_config,
    )
    snapshots.seal()
    return snapshots


def _validate_cell_identity(
    roster: Mapping[str, Any],
    method_config: Mapping[str, Any],
    method_config_file_sha256: str,
    record_groups: Iterable[Tuple[str, Sequence[Mapping[str, Any]]]],
) -> None:
    record_groups = list(record_groups)
    if roster.get("decision_status") != "confirmed":
        raise ValidationError("formal method-result requires a confirmed roster")
    if method_config.get("status") != "frozen":
        raise ValidationError("formal method-result requires a frozen method config")
    for field in ("method_id", "platform"):
        if roster.get(field) != method_config.get(field):
            raise ValidationError("roster and main method config {} differ".format(field))
    for label, records in record_groups:
        validate_records_against_roster(records, roster, require_confirmed=True)
        method_ids = {record.get("method_id") for record in records}
        platforms = {record.get("platform") for record in records}
        if method_ids != {method_config["method_id"]} or platforms != {
            method_config["platform"]
        }:
            raise ValidationError("{} mixes another method/platform cell".format(label))
    config_hashes = {
        record.get("config_sha256") for record in dict(record_groups)["responses"]
    }
    if config_hashes != {method_config_file_sha256}:
        raise ValidationError("responses do not share the frozen main method config")


def build_method_result(
    paths: MethodResultPaths, *, created_at_utc: str = None
) -> Tuple[Dict[str, Any], list]:
    """Validate and recompute one formal cell, returning it with TOCTOU snapshots."""

    snapshots = _capture_inputs(paths)
    manifest = _snapshot_json(Path(paths.freeze_manifest), snapshots)
    verify_freeze_manifest(manifest, require_frozen=True)
    frozen_roles = [
        ("query_library_test", paths.library),
        ("requirement_oracle_test", paths.oracle),
        ("query_roster", paths.roster),
        ("method_config", paths.method_config),
        ("uqh_assessor_registry", paths.uqh_assessor_registry),
        ("cpd_policy", paths.cpd_coverage),
    ]

    roster = _snapshot_json(Path(paths.roster), snapshots)
    method_config = _snapshot_json(Path(paths.method_config), snapshots)
    validate_schema_instance(roster, "query_roster", context="method-result roster")
    validate_schema_instance(
        method_config, "method_config", context="method-result method config"
    )
    platform = roster["platform"]
    _require_unique_frozen_main_config(
        manifest, Path(paths.method_config), roster["method_id"], platform
    )
    frozen_roles.extend(
        [
            ("platform_config_{}".format(platform), paths.platform_config),
            ("controller_config_{}".format(platform), paths.controller_config),
        ]
    )
    for role, path in frozen_roles:
        _frozen_asset(manifest, role, Path(path))

    responses = _snapshot_jsonl(Path(paths.responses), snapshots)
    evidence = _snapshot_jsonl(Path(paths.semantic_evidence), snapshots)
    scores = _snapshot_jsonl(Path(paths.semantic_scores), snapshots)
    runtime_records = _snapshot_jsonl(Path(paths.runtime_records), snapshots)
    oracle = _snapshot_jsonl(Path(paths.oracle), snapshots)
    validate_schema_records(responses, "response_record", context="method-result responses")
    validate_schema_records(
        evidence, "semantic_evidence", context="method-result semantic evidence"
    )
    validate_schema_records(scores, "semantic_score", context="method-result scores")
    validate_schema_records(
        runtime_records, "runtime_record", context="method-result runtime records"
    )
    validate_schema_records(oracle, "oracle_record", context="method-result oracle")
    groups = [
        ("responses", responses),
        ("semantic evidence", evidence),
        ("semantic scores", scores),
        ("runtime records", runtime_records),
    ]
    method_config_file_sha256 = snapshots.guards[
        str(Path(os.path.abspath(str(paths.method_config))))
    ].sha256
    _validate_cell_identity(roster, method_config, method_config_file_sha256, groups)

    generation_chain = dict(
        validate_generation_response_chain(
            Path(paths.library),
            roster,
            Path(paths.method_config),
            Path(paths.generation_dir),
            responses=responses,
            evidence=_snapshot_jsonl(
                Path(paths.generation_dir) / "evidence.jsonl", snapshots
            ),
            manifest=_snapshot_json(
                Path(paths.generation_dir) / "generation_run_manifest.json", snapshots
            ),
            development=False,
        ),
        verified=True,
    )
    if (
        generation_chain["method_id"] != roster["method_id"]
        or generation_chain["platform"] != platform
        or generation_chain["implementation_bundle_sha256"]
        != method_config["implementation_bundle"]["bundle_sha256"]
    ):
        raise ValidationError("generation chain differs from the main method config cell")

    validate_score_provenance(
        scores, manifest, Path(paths.roster), Path(paths.oracle)
    )
    validate_evidence_response_chain(evidence, responses, manifest, roster["method_id"])
    judge_run_manifest, raw_judge_records = validate_judge_run_bundle(
        Path(paths.judge_run_dir), responses, evidence, manifest
    )
    judge_bindings = validate_frozen_judge_response_chain(
        raw_judge_records, responses, evidence, manifest
    )
    judge_provenance = {
        "judge_run_manifest_sha256": judge_run_manifest["manifest_sha256"],
        "judge_responses_sha256": judge_run_manifest["judge_responses_sha256"],
    }
    validate_score_judge_run_provenance(scores, judge_provenance)
    validate_scores_against_evidence(
        scores, evidence, oracle, roster, judge_verdicts_by_run=judge_bindings
    )
    validate_aggregate_response_chain(responses, scores)

    grid = validate_records_against_roster(scores, roster, require_confirmed=True)
    semantic = aggregate_semantic_metrics(scores)
    rqs = compute_rqs(scores)
    assessments = _snapshot_jsonl(Path(paths.uqh_assessments), snapshots)
    assessor_registry = _snapshot_json(Path(paths.uqh_assessor_registry), snapshots)
    uqh = compute_uqh(
        responses,
        oracle,
        assessments,
        roster,
        assessor_registry,
        expected_config_sha256=method_config_file_sha256,
    )
    uqh["provenance"]["freeze_manifest_sha256"] = manifest["manifest_sha256"]
    uqh["provenance"]["generation_run_manifest_sha256"] = generation_chain[
        "manifest_sha256"
    ]
    recomputed_semantic_aggregate = {
        "repetition_grid": grid,
        "semantic": semantic,
        "rqs": rqs,
        "uqh": uqh,
    }
    _same_json(
        _snapshot_json(Path(paths.semantic_aggregate), snapshots),
        recomputed_semantic_aggregate,
        "semantic/RQS/UQH aggregate",
    )

    coverage = _snapshot_json(Path(paths.cpd_coverage), snapshots)
    validate_schema_instance(coverage, "cpd_coverage", context="method-result CPD coverage")
    supplied_cpd = _snapshot_json(Path(paths.cpd_aggregate), snapshots)
    validate_schema_instance(supplied_cpd, "cpd_aggregate", context="method-result CPD aggregate")
    cpd_provenance = {
        "roster_sha256": snapshots.guards[
            str(Path(os.path.abspath(str(paths.roster))))
        ].sha256,
        "oracle_sha256": snapshots.guards[
            str(Path(os.path.abspath(str(paths.oracle))))
        ].sha256,
        "coverage_sha256": snapshots.guards[
            str(Path(os.path.abspath(str(paths.cpd_coverage))))
        ].sha256,
        "semantic_evidence_sha256": snapshots.guards[
            str(Path(os.path.abspath(str(paths.semantic_evidence))))
        ].sha256,
        "semantic_scores_sha256": snapshots.guards[
            str(Path(os.path.abspath(str(paths.semantic_scores))))
        ].sha256,
        "responses_sha256": snapshots.guards[
            str(Path(os.path.abspath(str(paths.responses))))
        ].sha256,
        "judge_responses_sha256": snapshots.guards[
            str(
                Path(
                    os.path.abspath(
                        str(Path(paths.judge_run_dir) / "judge_responses.jsonl")
                    )
                )
            )
        ].sha256,
        "evaluator_source_sha256": metric_source_hash(manifest),
        "freeze_manifest_sha256": manifest["manifest_sha256"],
        "generation_chain_verified": True,
        "generation_run_manifest_sha256": generation_chain["manifest_sha256"],
        "implementation_bundle_sha256": generation_chain[
            "implementation_bundle_sha256"
        ],
        "created_at_utc": supplied_cpd.get("provenance", {}).get("created_at_utc"),
    }
    recomputed_cpd = recompute_cpd_aggregate(
        evidence,
        scores,
        oracle,
        coverage,
        roster,
        provenance=cpd_provenance,
        allow_draft_policy=False,
        expected_per_style=76,
    )
    validate_schema_instance(recomputed_cpd, "cpd_aggregate", context="recomputed CPD")
    _same_json(supplied_cpd, recomputed_cpd, "CPD aggregate")

    platform_config = _snapshot_json(Path(paths.platform_config), snapshots)
    worker_config = validate_platform_runtime_config(
        platform_config, platform, allow_draft=False
    )
    controller_config = _snapshot_json(Path(paths.controller_config), snapshots)
    validate_schema_instance(
        controller_config, "controller_config", context="method-result controller"
    )
    if (
        Path(controller_config["implementation"]["source_path"]).resolve()
        != Path(worker_config["worker"]).resolve()
        or controller_config["implementation"]["source_sha256"]
        != platform_config["worker"]["worker_sha256"]
    ):
        raise ValidationError("runtime controller and platform worker sources differ")
    worker_record_sha256 = record_sha256(worker_config)
    controller_record_sha256 = record_sha256(controller_config)
    for record in runtime_records:
        if (
            record.get("provenance", {}).get("worker_config_sha256")
            != worker_record_sha256
            or record.get("provenance", {}).get("worker_sha256")
            != platform_config["worker"]["worker_sha256"]
            or record.get("provenance", {}).get("interpreter_sha256")
            != platform_config["worker"]["interpreter_sha256"]
            or record.get("ne", {}).get("controller_config_sha256")
            != controller_record_sha256
            or record.get("source_config_sha256") != method_config_file_sha256
        ):
            raise ValidationError(
                "runtime record differs from the frozen cell/config/controller"
            )
    recomputed_runtime = aggregate_runtime(
        runtime_records,
        roster=roster,
        responses=responses,
        generation_chain=generation_chain,
        allow_partial=False,
        require_confirmed=True,
    )
    if set(recomputed_runtime) != {platform}:
        raise ValidationError("runtime aggregate is not exactly one platform cell")
    _same_json(
        _snapshot_json(Path(paths.runtime_aggregate), snapshots),
        recomputed_runtime,
        "runtime aggregate",
    )
    runtime = recomputed_runtime[platform]
    expected_iec_exec = {
        "status": "unavailable",
        "coverage": "none",
        "available_outputs": 0,
        "trace_outputs": runtime["iec_exec"]["trace_outputs"],
        "eligible_outputs": runtime["eligible_outputs"],
        "reason": "frozen_query_blind_event_observer_not_registered",
    }
    _same_json(runtime["iec_exec"], expected_iec_exec, "IEC_exec diagnostic")

    primary = {
        "srs": _require_number(semantic["srs"], "SRS"),
        "arc": _require_number(semantic["arc"], "ARC"),
        "rsc": _require_number(semantic["rsc"], "RSC"),
        "iec_spec": _require_number(semantic["iec_spec"], "IEC_spec"),
        "rqs": _require_number(rqs["rqs"], "RQS"),
        "uqh": _require_number(uqh["uqh"], "UQH"),
        "cpd_common": _require_number(
            recomputed_cpd["cpd_common"]["joint"]["macro_mean"], "CPD_common"
        ),
        "coverage": _require_number(coverage["coverage"], "coverage"),
        "sv": _require_number(runtime["scene_validity"], "SV"),
        "ne": _require_number(runtime["native_executability"], "NE"),
    }
    input_set_sha256 = sha256_bytes(canonical_json_bytes(snapshots))
    input_closure = snapshots.closure_document()
    cell_key = {
        "method_id": roster["method_id"],
        "platform": platform,
        "method_config_asset_file_sha256": method_config_file_sha256,
        "roster_asset_file_sha256": snapshots.guards[
            str(Path(os.path.abspath(str(paths.roster))))
        ].sha256,
        "freeze_manifest_self_sha256": manifest["manifest_sha256"],
    }
    result = {
        "schema_version": "0.1",
        "result_kind": "formal_method_platform_result",
        "cell_id": sha256_bytes(canonical_json_bytes(cell_key)),
        "method_id": roster["method_id"],
        "platform": platform,
        "cell_scope": {
            "unit": "method_platform",
            "cross_platform_completion_required": False,
            "missing_cell_policy": "omit_not_zero_or_impute",
            "platform_effect_adjustment": "none",
        },
        "primary_metrics": primary,
        "diagnostics": {
            "semantic_availability": copy.deepcopy(
                semantic["diagnostic_availability"]
            ),
            "rqs_mean": _require_number(rqs["rqs_mean"], "RQS mean diagnostic"),
            "rqs_vague_gap": float(rqs["vague_gap"]),
            "cpd_common_partial": _require_number(
                recomputed_cpd["cpd_common"]["partial"]["macro_mean"],
                "CPD partial diagnostic",
            ),
            "cpd_common_vague": _require_number(
                recomputed_cpd["cpd_common"]["vague"]["macro_mean"],
                "CPD vague diagnostic",
            ),
            "compile_success": _require_number(
                runtime["compile_success"], "compile success diagnostic"
            ),
            "iec_exec": copy.deepcopy(runtime["iec_exec"]),
        },
        "provenance": {
            "freeze_manifest_self_sha256": manifest["manifest_sha256"],
            "method_config_asset_file_sha256": method_config_file_sha256,
            "roster_asset_file_sha256": snapshots.guards[
                str(Path(os.path.abspath(str(paths.roster))))
            ].sha256,
            "generation_run_manifest_self_sha256": generation_chain[
                "manifest_sha256"
            ],
            "implementation_bundle_sha256": generation_chain[
                "implementation_bundle_sha256"
            ],
            "judge_run_manifest_self_sha256": judge_run_manifest["manifest_sha256"],
            "judge_responses_file_sha256": judge_run_manifest[
                "judge_responses_sha256"
            ],
            "evaluator_source_bundle_sha256": cpd_provenance[
                "evaluator_source_sha256"
            ],
            "input_set_sha256": input_set_sha256,
            "input_closure_sha256": sha256_bytes(
                canonical_json_bytes(input_closure)
            ),
            "input_closure_file_count": len(input_closure["files"]),
            "input_closure_directory_count": len(input_closure["directories"]),
        },
        "inputs": snapshots,
        "created_at_utc": created_at_utc or _utc_now(),
    }
    result["result_sha256"] = sha256_bytes(canonical_json_bytes(result))
    validate_method_result(result, verify_live_inputs=False)
    _recheck_snapshots(snapshots)
    return result, snapshots


def validate_method_result(
    result: Mapping[str, Any], *, verify_live_inputs: bool = False
) -> None:
    """Validate the envelope and optionally reconstruct it from all live evidence."""

    validate_schema_instance(result, "method_result", context="method result")
    unsigned = dict(result)
    claimed = unsigned.pop("result_sha256")
    if sha256_bytes(canonical_json_bytes(unsigned)) != claimed:
        raise ValidationError("method-result self-hash mismatch")
    if sha256_bytes(canonical_json_bytes(result["inputs"])) != result["provenance"][
        "input_set_sha256"
    ]:
        raise ValidationError("method-result input-set hash mismatch")
    roles = tuple(binding["role"] for binding in result["inputs"])
    if roles != METHOD_RESULT_INPUT_ROLES:
        raise ValidationError(
            "method-result inputs must use the exact ordered 23-role contract"
        )
    input_paths = [
        _canonical_absolute_path_text(
            binding["path"], "method-result input {}".format(binding["role"])
        )
        for binding in result["inputs"]
    ]
    normalized_paths = [os.path.normcase(path) for path in input_paths]
    if len(normalized_paths) != len(set(normalized_paths)):
        raise ValidationError("method-result input paths must be unique")
    by_role = {binding["role"]: binding for binding in result["inputs"]}
    provenance = result["provenance"]
    if (
        by_role["method_config"]["sha256"]
        != provenance["method_config_asset_file_sha256"]
        or by_role["query_roster"]["sha256"]
        != provenance["roster_asset_file_sha256"]
    ):
        raise ValidationError(
            "method-result identity provenance differs from its input bindings"
        )
    cell_key = {
        "method_id": result["method_id"],
        "platform": result["platform"],
        "method_config_asset_file_sha256": provenance[
            "method_config_asset_file_sha256"
        ],
        "roster_asset_file_sha256": provenance["roster_asset_file_sha256"],
        "freeze_manifest_self_sha256": provenance["freeze_manifest_self_sha256"],
    }
    if sha256_bytes(canonical_json_bytes(cell_key)) != result["cell_id"]:
        raise ValidationError("method-result cell_id does not match its frozen identity")
    if verify_live_inputs:
        def bound(role: str) -> Path:
            if role not in by_role:
                raise ValidationError(
                    "method-result live verification lacks {}".format(role)
                )
            return Path(by_role[role]["path"])

        live_paths = MethodResultPaths(
            freeze_manifest=bound("freeze_manifest"),
            library=bound("query_library_test"),
            oracle=bound("requirement_oracle_test"),
            roster=bound("query_roster"),
            method_config=bound("method_config"),
            generation_dir=bound("generation_run_manifest").parent,
            responses=bound("generation_responses"),
            semantic_evidence=bound("semantic_evidence"),
            semantic_scores=bound("semantic_scores"),
            judge_run_dir=bound("judge_run_manifest").parent,
            semantic_aggregate=bound("semantic_aggregate"),
            uqh_assessments=bound("uqh_assessments"),
            uqh_assessor_registry=bound("uqh_assessor_registry"),
            cpd_aggregate=bound("cpd_aggregate"),
            cpd_coverage=bound("cpd_coverage"),
            runtime_records=bound("runtime_records"),
            runtime_aggregate=bound("runtime_aggregate"),
            platform_config=bound("platform_config"),
            controller_config=bound("controller_config"),
        )
        rebuilt, _ = build_method_result(
            live_paths, created_at_utc=result["created_at_utc"]
        )
        _same_json(dict(result), rebuilt, "method-result envelope")


@dataclass
class _OutputTransaction:
    path: Path
    parent_fd: int
    parent_fingerprints: tuple
    output_name: str
    payload: bytes
    published_identity: Tuple[int, int]

    def close(self) -> None:
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write while staging method-result")
        offset += written


def _read_file_at(directory_fd: int, name: str) -> Tuple[bytes, os.stat_result]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValidationError("method-result output must remain a regular file")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _stat_fingerprint(before) != _stat_fingerprint(after):
        raise ValidationError("method-result output changed while being verified")
    return b"".join(chunks), after


def _transaction_name(kind: str) -> str:
    return ".method-result-{}-{}".format(kind, secrets.token_hex(16))


def _assert_output_parent_current(transaction: _OutputTransaction) -> None:
    current_fd, current_fingerprints = _open_directory_no_follow(
        transaction.path.parent, "method-result output parent"
    )
    try:
        held = os.fstat(transaction.parent_fd)
        current = os.fstat(current_fd)
    finally:
        os.close(current_fd)
    if (held.st_dev, held.st_ino, held.st_mode) != (
        current.st_dev,
        current.st_ino,
        current.st_mode,
    ):
        raise ValidationError("method-result output parent path changed")
    expected_ancestors = transaction.parent_fingerprints[:-1]
    current_ancestors = current_fingerprints[:-1]
    expected_identities = [
        (path, fingerprint[:3]) for path, fingerprint in expected_ancestors
    ]
    current_identities = [
        (path, fingerprint[:3]) for path, fingerprint in current_ancestors
    ]
    if current_identities != expected_identities:
        raise ValidationError("method-result output parent ancestry identity changed")
    # Shared roots can receive unrelated directory-entry mutations from other
    # processes.  Compare their identity above, but reserve full metadata
    # comparison for private ancestors.  The final output parent is checked by
    # its held/current identity because this transaction mutates its metadata.
    volatile_roots = {"/", "/tmp", "/home"}
    expected_guarded = [
        value for value in expected_ancestors if value[0] not in volatile_roots
    ]
    current_guarded = [
        value for value in current_ancestors if value[0] not in volatile_roots
    ]
    if current_guarded != expected_guarded:
        raise ValidationError("method-result output parent ancestry changed")


def _write_exclusive(path: Path, payload: bytes) -> _OutputTransaction:
    """Publish by exclusive hard link and retain the unique staging name."""

    path = Path(os.path.abspath(str(path)))
    parent_fd, parent_fingerprints = _open_directory_no_follow(
        path.parent, "method-result output parent"
    )
    stage_name = _transaction_name("stage")
    stage_created = False
    stage_identity = None
    linked = False
    try:
        try:
            descriptor = os.open(
                stage_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise ValidationError("method-result staging name collision") from exc
        stage_created = True
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            staged = os.fstat(descriptor)
            stage_identity = (staged.st_dev, staged.st_ino)
        finally:
            os.close(descriptor)
        try:
            os.link(
                stage_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            linked = True
        except FileExistsError as exc:
            raise ValidationError("method-result output already exists") from exc
        os.fsync(parent_fd)
        observed_payload, published = _read_file_at(parent_fd, path.name)
        if (
            (published.st_dev, published.st_ino) != stage_identity
            or observed_payload != payload
        ):
            raise ValidationError("method-result published output differs from staging")
        transaction = _OutputTransaction(
            path=path,
            parent_fd=parent_fd,
            parent_fingerprints=parent_fingerprints,
            output_name=path.name,
            payload=payload,
            published_identity=(published.st_dev, published.st_ino),
        )
        _assert_output_parent_current(transaction)
        return transaction
    except Exception:
        if stage_created:
            # The stage name is retained unconditionally; do not perform a
            # name-based read, delete, or restore after creation.
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
        if linked:
            temporary = _OutputTransaction(
                path=path,
                parent_fd=parent_fd,
                parent_fingerprints=parent_fingerprints,
                output_name=path.name,
                payload=payload,
                published_identity=stage_identity,
            )
            _cleanup_failed_publication(temporary)
        os.close(parent_fd)
        raise


def _cleanup_failed_publication(transaction: _OutputTransaction) -> None:
    """Isolate a failed target without name-based deletion after verification."""

    quarantine = _transaction_name("quarantine")
    try:
        os.rename(
            transaction.output_name,
            quarantine,
            src_dir_fd=transaction.parent_fd,
            dst_dir_fd=transaction.parent_fd,
        )
        os.fsync(transaction.parent_fd)
    except OSError:
        return
    try:
        isolated_payload, isolated = _read_file_at(transaction.parent_fd, quarantine)
        isolated_identity = (isolated.st_dev, isolated.st_ino)
        owned = (
            isolated_identity == transaction.published_identity
            and isolated_payload == transaction.payload
        )
    except (OSError, ValidationError):
        return
    if owned:
        # POSIX unlink is name-based: an object swapped into ``quarantine``
        # after the identity check could otherwise be deleted using stale
        # evidence.  Retain the isolated file for explicit operator cleanup.
        return
    if isolated_identity == transaction.published_identity:
        # The transaction inode was modified.  Keep it isolated instead of
        # restoring untrusted bytes to the public result name.
        return
    # A verified foreign inode is retained under the isolated name.  Any
    # name-based restore here would have the same stale-name race as unlink.


def publish_method_result(paths: MethodResultPaths, output: Path) -> Dict[str, Any]:
    """Publish one tamper-evident, exclusive-create formal result envelope."""

    result, snapshots = build_method_result(paths)
    # Re-run the complete generation, Judge, semantic, UQH, CPD, and runtime
    # validation immediately before publication.  These validators read the
    # per-item generation artifacts, Judge stdout/stderr/executions, and native
    # runtime captures which are transitively bound by the top-level indexes.
    rebuilt, rebuilt_snapshots = build_method_result(
        paths, created_at_utc=result["created_at_utc"]
    )
    _same_json(result, rebuilt, "pre-publication method-result reconstruction")
    verify_freeze_manifest(read_json(Path(paths.freeze_manifest)), require_frozen=True)
    _recheck_snapshots(snapshots)
    _recheck_snapshots(rebuilt_snapshots)
    output = Path(os.path.abspath(str(output)))
    output_parent = str(output.parent)
    guarded_parents = {
        parent
        for guard in snapshots.guards.values()
        for parent, _ in guard.parent_fingerprints
    }
    if output_parent in guarded_parents or any(
        output == Path(root) or _path_is_within(output, Path(root))
        for root in snapshots.directory_inventories
    ):
        raise ValidationError(
            "method-result output must use a pre-existing directory outside guarded inputs"
        )
    payload = canonical_json_bytes(result) + b"\n"
    transaction = _write_exclusive(output, payload)
    try:
        _assert_output_parent_current(transaction)
        # Output isolation means exclusive creation cannot legitimately change
        # an input parent.  Full content, identity, parent, and inventory guards
        # therefore remain mandatory after the link as well.
        _recheck_snapshots(snapshots, check_parent_ctime=True)
        _recheck_snapshots(rebuilt_snapshots, check_parent_ctime=True)
        _assert_output_parent_current(transaction)
    except Exception:
        _cleanup_failed_publication(transaction)
        raise
    finally:
        transaction.close()
    return result


__all__ = [
    "MethodResultPaths",
    "build_method_result",
    "publish_method_result",
    "validate_method_result",
]
