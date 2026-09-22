"""Atomic, immutable bundle helpers shared by evaluator-side pipelines."""

import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterable, Mapping, Sequence, Set

from .errors import ValidationError
from .jsonio import canonical_json_bytes, sha256_bytes


def path_has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def jsonl_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    payload = b"\n".join(canonical_json_bytes(dict(record)) for record in records)
    return payload + (b"\n" if records else b"")


def file_binding(path: Path, *, require_nonempty: bool = False) -> Mapping[str, Any]:
    path = Path(path)
    if path_has_symlink_component(path):
        raise ValidationError("bundle binding cannot traverse a symlink")
    path = path.resolve()
    if not path.is_file():
        raise ValidationError("bundle binding must reference a regular non-symlink file")
    payload = path.read_bytes()
    if require_nonempty and not payload:
        raise ValidationError("bundle binding file must be non-empty")
    return {"path": str(path), "sha256": sha256_bytes(payload), "bytes": len(payload)}


def bound_bytes(
    binding: Mapping[str, Any], label: str, *, require_nonempty: bool = False
) -> bytes:
    if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256", "bytes"}:
        raise ValidationError("{} binding is malformed".format(label))
    path = Path(str(binding.get("path", "")))
    if path_has_symlink_component(path) or not path.is_file():
        raise ValidationError("{} file is missing or is a symlink".format(label))
    payload = path.read_bytes()
    if (
        (require_nonempty and not payload)
        or len(payload) != binding.get("bytes")
        or sha256_bytes(payload) != binding.get("sha256")
    ):
        raise ValidationError("{} byte binding changed".format(label))
    return payload


def write_staged_bytes(staging_directory: Path, relative_path: Path, payload: bytes) -> None:
    relative_path = Path(relative_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValidationError("bundle output path must stay relative to its bundle")
    target = Path(staging_directory) / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def validate_exact_bundle_inventory(
    bundle_directory: Path, expected_relative_files: Iterable[Path]
) -> None:
    """Reject missing, extra, special, or symlinked entries in one bundle tree."""

    bundle_directory = Path(os.path.abspath(str(bundle_directory)))
    if path_has_symlink_component(bundle_directory) or not bundle_directory.is_dir():
        raise ValidationError("bundle root must be a regular non-symlink directory")
    expected_files: Set[Path] = set()
    for relative in expected_relative_files:
        relative = Path(relative)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValidationError("bundle inventory paths must be relative and contained")
        expected_files.add(relative)
    expected_directories = set()
    for relative in expected_files:
        parent = relative.parent
        while parent != Path("."):
            expected_directories.add(parent)
            parent = parent.parent

    actual_files = set()
    actual_directories = set()
    stack = [(bundle_directory, Path("."))]
    while stack:
        absolute_directory, relative_directory = stack.pop()
        try:
            entries = list(os.scandir(str(absolute_directory)))
        except OSError as exc:
            raise ValidationError("cannot inspect bundle inventory") from exc
        for entry in entries:
            relative = (
                Path(entry.name)
                if relative_directory == Path(".")
                else relative_directory / entry.name
            )
            if entry.is_symlink():
                raise ValidationError("bundle inventory contains a symlink")
            if entry.is_dir(follow_symlinks=False):
                actual_directories.add(relative)
                stack.append((Path(entry.path), relative))
            elif entry.is_file(follow_symlinks=False):
                actual_files.add(relative)
            else:
                raise ValidationError("bundle inventory contains a special file")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ValidationError("bundle inventory differs from its exact allowlist")


class AtomicBundle:
    """Stage a complete directory and publish it with one atomic rename."""

    def __init__(self, output_directory: Path, label: str) -> None:
        self.output_directory = Path(output_directory).resolve()
        self.label = label
        self.parent = self.output_directory.parent
        self.lock_path = self.parent / ".{}.lock".format(self.output_directory.name)
        self.lock_descriptor = None
        self.staging_directory = None

    def __enter__(self) -> Path:
        self.parent.mkdir(parents=True, exist_ok=True)
        if self.output_directory.exists() or self.output_directory.is_symlink():
            raise ValidationError("immutable {} bundle already exists".format(self.label))
        try:
            self.lock_descriptor = os.open(
                str(self.lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError as exc:
            raise ValidationError(
                "another {} bundle publication is in progress".format(self.label)
            ) from exc
        try:
            self.staging_directory = Path(
                tempfile.mkdtemp(
                    prefix=".{}-staging-".format(self.output_directory.name),
                    dir=str(self.parent),
                )
            )
        except Exception:
            os.close(self.lock_descriptor)
            self.lock_descriptor = None
            self.lock_path.unlink()
            raise
        return self.staging_directory

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            if exc_type is None:
                if self.output_directory.exists() or self.output_directory.is_symlink():
                    raise ValidationError(
                        "immutable {} bundle already exists".format(self.label)
                    )
                os.rename(str(self.staging_directory), str(self.output_directory))
                self.staging_directory = None
                descriptor = os.open(str(self.parent), os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if self.staging_directory is not None and self.staging_directory.exists():
                shutil.rmtree(str(self.staging_directory))
            if self.lock_descriptor is not None:
                os.close(self.lock_descriptor)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
        return False


__all__ = [
    "AtomicBundle",
    "bound_bytes",
    "file_binding",
    "jsonl_bytes",
    "path_has_symlink_component",
    "validate_exact_bundle_inventory",
    "write_staged_bytes",
]
