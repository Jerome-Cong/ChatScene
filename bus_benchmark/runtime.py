"""Immutable orchestration for platform-native Compile, SV, and NE stages.

The orchestrator never reads a query or an oracle.  It receives only a finalized
response record, invokes a frozen platform worker in a subprocess, and preserves
every request/stdout/stderr byte.  Platform-specific imports stay in
``runtime_worker.py`` so the benchmark control environment does not need CARLA or
MetaDrive installed.
"""

import datetime as _datetime
import ast
import io
import math
import os
import re
import shutil
import subprocess
import tokenize
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from .constants import (
    CARLA_TIMESTEP_SECONDS,
    ROLLOUT_SECONDS,
    SCHEMA_VERSION,
    SV_MAX_ITERATIONS,
    SV_SEEDS,
)
from .errors import ValidationError
from .jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    strict_json_object_bytes,
    write_json,
    write_jsonl,
)
from .provenance import record_sha256
from .roster import validate_records_against_roster
from .schema import validate_schema_instance


_CONTROLLER_CLASSES = {
    "carla": "FixedIDMPIDBehavior",
    "metadrive": "FrozenBusIDMPolicy",
}

_FORBIDDEN_RUNTIME_METADATA_KEYS = {
    "evaluation",
    "expected_support",
    "gold",
    "intent_group_id",
    "judge",
    "oracle",
    "query",
    "query_id",
    "query_text",
    "requirement",
    "requirements",
    "score",
    "statistical_intent_cluster_id",
    "surface_style",
}

_ALLOWED_WORKER_ENVIRONMENT_KEYS = {
    "CARLA_ROOT",
    "LD_LIBRARY_PATH",
    "PYTHON_EGG_CACHE",
    "PYTHONPATH",
}

_CONTENT_BEARING_ENVIRONMENT_KEYS = {
    "CARLA_ROOT",
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
}

_CARLA_RUNTIME_TREE_ROLES = {
    "carla_server_distribution",
    "scenic_editable_source",
}

_RUNTIME_TREE_POLICY = "exclude_python_cache_v0.1"

_CARLA_SERVER_RELATIVE_PATH = Path(
    "CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping"
)

_METADRIVE_AUDITED_REVISION = "85e5dadc6c7436d324348f6e3d8f8e680c06b4db"
_METADRIVE_TRACKED_SUBPATH = "metadrive"
_METADRIVE_TRACKED_TREE_POLICY = "git_tracked_worktree_bytes_v0.1"


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _safe_run_key(run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id:
        raise ValidationError("runtime input requires a non-empty run_id")
    return sha256_bytes(run_id.encode("utf-8"))[:24]


def _path_has_symlink_component(path: Path) -> bool:
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _regular_runtime_file(path_value: Any, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value:
        raise ValidationError("{} must be a non-empty absolute path".format(label))
    unresolved = Path(path_value)
    if (
        not unresolved.is_absolute()
        or _path_has_symlink_component(unresolved)
        or not unresolved.is_file()
    ):
        raise ValidationError(
            "{} must be an absolute regular non-symlink file".format(label)
        )
    return unresolved.resolve()


def _runtime_file_identity(path: Path, label: str) -> tuple:
    path = _regular_runtime_file(str(path), label)
    observed = path.stat()
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _validate_expected_runtime_file(
    path_value: Any,
    expected_sha256: Any,
    expected_bytes: Any,
    label: str,
) -> Path:
    path = _regular_runtime_file(path_value, label)
    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or type(expected_bytes) is not int
        or expected_bytes < 1
        or path.stat().st_size != expected_bytes
        or sha256_file(path) != expected_sha256
    ):
        raise ValidationError("{} differs from its frozen expected bytes".format(label))
    return path


def _exact_file_binding(binding: Any, label: str) -> Dict[str, Any]:
    if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256", "bytes"}:
        raise ValidationError("{} must be an exact file binding".format(label))
    path_value = binding.get("path")
    digest = binding.get("sha256")
    size = binding.get("bytes")
    if (
        not isinstance(path_value, str)
        or not Path(path_value).is_absolute()
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or type(size) is not int
        or size < 0
    ):
        raise ValidationError("{} contains an invalid path/hash/byte count".format(label))
    unresolved = Path(path_value)
    if _path_has_symlink_component(unresolved) or not unresolved.is_file():
        raise ValidationError("{} must be a regular non-symlink file".format(label))
    path = unresolved.resolve()
    if path.stat().st_size != size or sha256_file(path) != digest:
        raise ValidationError("{} differs from its bound bytes".format(label))
    return {"path": str(path), "sha256": digest, "bytes": size}


def _validate_carla_server_binary(binding: Any) -> Dict[str, Any]:
    """Validate the frozen CARLA server leaf, never its shell launcher."""

    normalized = _exact_file_binding(binding, "CARLA server binary")
    path = Path(normalized["path"])
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
    except OSError as exc:
        raise ValidationError("CARLA server binary cannot be read") from exc
    if (
        path.name != _CARLA_SERVER_RELATIVE_PATH.name
        or not os.access(str(path), os.X_OK)
        or magic != b"\x7fELF"
    ):
        raise ValidationError(
            "CARLA server_binary must be the executable CarlaUE4-Linux-Shipping ELF"
        )
    return normalized


def _failed_carla_server_preflight(reason: str) -> Dict[str, Any]:
    return {"status": "failed", "failure_reason": reason, "attestation": None}


def _carla_server_preflight(
    endpoint: Mapping[str, Any],
    server_binary: Mapping[str, Any],
    *,
    proc_root: Path = Path("/proc"),
) -> Dict[str, Any]:
    """Bind one loopback-reachable LISTEN socket to the frozen server process."""

    binary = _validate_carla_server_binary(server_binary)
    port = endpoint.get("port")
    if endpoint.get("host") != "127.0.0.1" or type(port) is not int:
        return _failed_carla_server_preflight("invalid_loopback_endpoint")
    listeners = []
    accepted_addresses = {
        "tcp": {"00000000", "0100007F"},
        "tcp6": {
            "00000000000000000000000000000000",
            "00000000000000000000000001000000",
        },
    }
    for table in ("tcp", "tcp6"):
        table_path = Path(proc_root) / "net" / table
        try:
            lines = table_path.read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeError):
            return _failed_carla_server_preflight("proc_network_table_unreadable")
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                address, port_hex = fields[1].split(":", 1)
                observed_port = int(port_hex, 16)
            except (ValueError, IndexError):
                return _failed_carla_server_preflight("proc_network_table_malformed")
            if observed_port != port or address not in accepted_addresses[table]:
                continue
            inode = fields[9]
            if not inode.isdigit():
                return _failed_carla_server_preflight("proc_socket_inode_malformed")
            listeners.append(
                {
                    "listener_table": table,
                    "listener_local_address_hex": address,
                    "listener_port": port,
                    "socket_inode": inode,
                }
            )
    if not listeners:
        return _failed_carla_server_preflight("no_loopback_listening_socket")
    if len(listeners) != 1:
        return _failed_carla_server_preflight("ambiguous_loopback_listening_sockets")

    listener = listeners[0]
    socket_target = "socket:[{}]".format(listener["socket_inode"])
    owning_pids = set()
    try:
        process_directories = sorted(
            path
            for path in Path(proc_root).iterdir()
            if path.name.isdigit() and path.is_dir()
        )
    except OSError:
        return _failed_carla_server_preflight("proc_process_table_unreadable")
    for process_directory in process_directories:
        fd_directory = process_directory / "fd"
        try:
            descriptors = list(fd_directory.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                if os.readlink(str(descriptor)) == socket_target:
                    owning_pids.add(int(process_directory.name))
                    break
            except OSError:
                continue
    if not owning_pids:
        return _failed_carla_server_preflight("listener_pid_unresolved")
    if len(owning_pids) != 1:
        return _failed_carla_server_preflight("ambiguous_listener_pids")

    pid = next(iter(owning_pids))
    process_root = Path(proc_root) / str(pid)
    try:
        stat_text = (process_root / "stat").read_text(encoding="ascii")
        close_parenthesis = stat_text.rfind(")")
        stat_fields = stat_text[close_parenthesis + 2 :].split()
        starttime_ticks = int(stat_fields[19])
        exe_target = os.readlink(str(process_root / "exe"))
    except (OSError, UnicodeError, ValueError, IndexError):
        return _failed_carla_server_preflight("listener_process_identity_unreadable")
    if close_parenthesis < 1 or exe_target.endswith(" (deleted)"):
        return _failed_carla_server_preflight("listener_process_identity_unreadable")
    exe_path = Path(exe_target).resolve()
    binary_path = Path(binary["path"])
    try:
        same_inode = os.path.samestat(
            os.stat(str(process_root / "exe")), binary_path.stat()
        )
    except OSError:
        return _failed_carla_server_preflight("listener_process_identity_unreadable")
    if exe_path != binary_path or not same_inode:
        return _failed_carla_server_preflight("listener_exe_mismatch")
    observed_binary = _validate_carla_server_binary(binary)
    return {
        "status": "verified",
        "failure_reason": None,
        "attestation": {
            "pid": pid,
            "process_starttime_ticks": starttime_ticks,
            **listener,
            "process_exe": observed_binary,
        },
    }


def runtime_tree_binding(root: Path, role: str) -> Dict[str, Any]:
    """Hash one immutable runtime dependency tree by relative names and bytes.

    Python bytecode/cache directories are intentionally excluded because they are
    derived and may be created by imports.  Every other regular file is included;
    symlinks and non-regular directory entries fail closed.
    """

    unresolved = Path(root)
    if not unresolved.is_absolute() or _path_has_symlink_component(unresolved):
        raise ValidationError("runtime dependency tree must be absolute and non-symlinked")
    root = unresolved.resolve()
    if not root.is_dir() or role not in _CARLA_RUNTIME_TREE_ROLES:
        raise ValidationError("runtime dependency tree role/root is invalid")
    records = []
    total_bytes = 0
    for directory, directory_names, file_names in os.walk(str(root), topdown=True):
        for name in directory_names:
            child = Path(directory) / name
            if child.is_symlink() or not child.is_dir():
                raise ValidationError(
                    "runtime dependency tree contains a symlink/non-directory entry"
                )
        directory_names[:] = sorted(
            name for name in directory_names if name != "__pycache__"
        )
        for name in sorted(file_names):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise ValidationError("runtime dependency tree contains a non-regular file")
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
            records.append(
                {"path": relative, "sha256": sha256_file(path), "bytes": size}
            )
            total_bytes += size
    if not records:
        raise ValidationError("runtime dependency tree contains no bound files")
    return {
        "role": role,
        "path": str(root),
        "sha256": sha256_bytes(canonical_json_bytes(records)),
        "bytes": total_bytes,
        "file_count": len(records),
        "policy": _RUNTIME_TREE_POLICY,
    }


def _validate_runtime_tree_binding(
    binding: Any, label: str, *, verify_bytes: bool
) -> Dict[str, Any]:
    required = {"role", "path", "sha256", "bytes", "file_count", "policy"}
    if not isinstance(binding, Mapping) or set(binding) != required:
        raise ValidationError("{} must be an exact runtime tree binding".format(label))
    if (
        binding.get("role") not in _CARLA_RUNTIME_TREE_ROLES
        or binding.get("policy") != _RUNTIME_TREE_POLICY
        or not isinstance(binding.get("path"), str)
        or not Path(binding["path"]).is_absolute()
        or not isinstance(binding.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", binding["sha256"]) is None
        or type(binding.get("bytes")) is not int
        or binding["bytes"] < 1
        or type(binding.get("file_count")) is not int
        or binding["file_count"] < 1
        or _path_has_symlink_component(Path(binding["path"]))
        or not Path(binding["path"]).is_dir()
    ):
        raise ValidationError("{} is malformed or missing".format(label))
    normalized = dict(binding, path=str(Path(binding["path"]).resolve()))
    if verify_bytes:
        observed = runtime_tree_binding(Path(normalized["path"]), normalized["role"])
        if canonical_json_bytes(observed) != canonical_json_bytes(normalized):
            raise ValidationError("{} differs from its bound tree bytes".format(label))
    return normalized


def _metadrive_tracked_source_snapshot(repository_path: Path, tracked_subpath: str):
    """Measure the audited MetaDrive git revision and every tracked source byte."""

    unresolved = Path(repository_path)
    if (
        not unresolved.is_absolute()
        or _path_has_symlink_component(unresolved)
        or tracked_subpath != _METADRIVE_TRACKED_SUBPATH
    ):
        raise ValidationError("MetaDrive tracked source repository/subpath is invalid")
    repository = unresolved.resolve()
    if not repository.is_dir():
        raise ValidationError("MetaDrive tracked source repository is missing")
    try:
        identity = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "rev-parse",
                "--show-toplevel",
                "HEAD",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        inventory = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "ls-files",
                "-z",
                "--",
                tracked_subpath,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        worktree_status = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                tracked_subpath,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError("MetaDrive tracked source cannot be probed") from exc
    try:
        identity_lines = identity.stdout.decode("utf-8").splitlines()
        relative_paths = sorted(
            value.decode("utf-8")
            for value in inventory.stdout.split(b"\0")
            if value
        )
    except UnicodeError as exc:
        raise ValidationError("MetaDrive tracked source paths are not UTF-8") from exc
    if (
        identity.returncode != 0
        or inventory.returncode != 0
        or worktree_status.returncode != 0
        or worktree_status.stdout
        or len(identity_lines) != 2
        or Path(identity_lines[0]).resolve() != repository
        or re.fullmatch(r"[0-9a-f]{40}", identity_lines[1]) is None
        or identity_lines[1] != _METADRIVE_AUDITED_REVISION
        or not relative_paths
    ):
        raise ValidationError("MetaDrive tracked source revision/inventory is not audited")

    records = []
    total_bytes = 0
    tracked_paths = set()
    source_root = (repository / tracked_subpath).resolve()
    for relative_text in relative_paths:
        relative = Path(relative_text)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != relative_text
        ):
            raise ValidationError("MetaDrive tracked source contains an unsafe path")
        unresolved_path = repository / relative
        if _path_has_symlink_component(unresolved_path) or not unresolved_path.is_file():
            raise ValidationError("MetaDrive tracked source file is missing or symlinked")
        path = unresolved_path.resolve()
        try:
            path.relative_to(source_root)
        except ValueError as exc:
            raise ValidationError("MetaDrive tracked source file escapes its subtree") from exc
        size = path.stat().st_size
        records.append(
            {"path": relative_text, "sha256": sha256_file(path), "bytes": size}
        )
        tracked_paths.add(relative_text)
        total_bytes += size
    return (
        {
            "repository_path": str(repository),
            "revision": identity_lines[1],
            "tracked_subpath": tracked_subpath,
            "tracked_tree_sha256": sha256_bytes(canonical_json_bytes(records)),
            "tracked_file_count": len(records),
            "tracked_bytes": total_bytes,
            "policy": _METADRIVE_TRACKED_TREE_POLICY,
        },
        tracked_paths,
    )


def metadrive_tracked_source_binding(
    repository_path: Path, tracked_subpath: str = _METADRIVE_TRACKED_SUBPATH
) -> Dict[str, Any]:
    """Return the complete audited MetaDrive tracked-worktree binding."""

    binding, _ = _metadrive_tracked_source_snapshot(
        repository_path, tracked_subpath
    )
    return binding


def _validate_metadrive_source_binding(binding: Any):
    required = {
        "repository_path",
        "revision",
        "tracked_subpath",
        "tracked_tree_sha256",
        "tracked_file_count",
        "tracked_bytes",
        "policy",
    }
    if (
        not isinstance(binding, Mapping)
        or set(binding) != required
        or not isinstance(binding.get("repository_path"), str)
        or not Path(binding["repository_path"]).is_absolute()
        or binding.get("revision") != _METADRIVE_AUDITED_REVISION
        or binding.get("tracked_subpath") != _METADRIVE_TRACKED_SUBPATH
        or re.fullmatch(r"[0-9a-f]{64}", str(binding.get("tracked_tree_sha256")))
        is None
        or type(binding.get("tracked_file_count")) is not int
        or binding["tracked_file_count"] < 1
        or type(binding.get("tracked_bytes")) is not int
        or binding["tracked_bytes"] < 1
        or binding.get("policy") != _METADRIVE_TRACKED_TREE_POLICY
    ):
        raise ValidationError("MetaDrive tracked source binding is malformed")
    observed, tracked_paths = _metadrive_tracked_source_snapshot(
        Path(binding["repository_path"]), binding["tracked_subpath"]
    )
    if canonical_json_bytes(observed) != canonical_json_bytes(binding):
        raise ValidationError("MetaDrive tracked source revision or bytes changed")
    return observed, tracked_paths


def _validate_metadrive_environment_manifest(
    binding: Any, interpreter: Path, *, formal: bool
) -> Dict[str, Any]:
    normalized = _exact_file_binding(
        binding, "MetaDrive runtime environment manifest"
    )
    manifest = read_json(Path(normalized["path"]))
    validate_schema_instance(
        manifest,
        "method_environment_manifest",
        context="MetaDrive runtime environment manifest",
    )
    if manifest.get("runtime_kind") != "python":
        raise ValidationError("MetaDrive runtime environment must be Python")
    executable = _exact_file_binding(
        manifest.get("executable"), "MetaDrive runtime environment executable"
    )
    if Path(executable["path"]).resolve() != Path(interpreter).resolve():
        raise ValidationError("MetaDrive runtime environment executable differs from worker")
    if formal and manifest.get("decision_status") != "frozen":
        raise ValidationError(
            "formal MetaDrive runtime requires a frozen environment manifest"
        )
    from .generation import probe_python_environment

    observed = probe_python_environment(Path(interpreter))
    if (
        manifest.get("runtime_version") != observed["runtime_version"]
        or manifest.get("locked_dependencies") != observed["locked_dependencies"]
    ):
        raise ValidationError("MetaDrive runtime Python package inventory changed")
    return normalized


def _probe_metadrive_import(
    interpreter: Path, environment: Mapping[str, str]
) -> Dict[str, str]:
    code = (
        "import json,pathlib,sys,metadrive;"
        "print(json.dumps({'executable':str(pathlib.Path(sys.executable).resolve()),"
        "'module':str(pathlib.Path(metadrive.__file__).resolve())},sort_keys=True))"
    )
    try:
        process = subprocess.run(
            [str(interpreter), "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
            env=_worker_environment(environment),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError("MetaDrive runtime import probe failed") from exc
    if process.returncode != 0:
        raise ValidationError("MetaDrive runtime cannot import the bound package")
    value = strict_json_object_bytes(process.stdout, "MetaDrive runtime import probe")
    if set(value) != {"executable", "module"} or any(
        not isinstance(value.get(field), str) or not value[field]
        for field in ("executable", "module")
    ):
        raise ValidationError("MetaDrive runtime import probe is malformed")
    return {"executable": value["executable"], "module": value["module"]}


def _validate_metadrive_runtime_dependencies(
    document: Any, worker: Mapping[str, Any], *, formal: bool
) -> Dict[str, Any]:
    required = {"environment_manifest", "metadrive_module", "source_tree"}
    if not isinstance(document, Mapping) or set(document) != required:
        raise ValidationError("MetaDrive runtime dependencies are incomplete or unknown")
    interpreter = Path(str(worker.get("interpreter", ""))).resolve()
    environment = worker.get("environment", {})
    if environment != {}:
        raise ValidationError("MetaDrive runtime requires an empty worker environment")
    environment_manifest = _validate_metadrive_environment_manifest(
        document.get("environment_manifest"), interpreter, formal=formal
    )
    module = _exact_file_binding(
        document.get("metadrive_module"), "MetaDrive imported module"
    )
    source_tree, tracked_paths = _validate_metadrive_source_binding(
        document.get("source_tree")
    )
    imported = _probe_metadrive_import(interpreter, environment)
    if Path(imported["executable"]).resolve() != interpreter:
        raise ValidationError("MetaDrive import probe used a different interpreter")
    imported_module = Path(imported["module"]).resolve()
    if imported_module != Path(module["path"]).resolve():
        raise ValidationError("MetaDrive interpreter imported an unbound package")
    repository = Path(source_tree["repository_path"])
    try:
        relative_module = imported_module.relative_to(repository).as_posix()
    except ValueError as exc:
        raise ValidationError("MetaDrive import is outside the bound repository") from exc
    if (
        relative_module != _METADRIVE_TRACKED_SUBPATH + "/__init__.py"
        or relative_module not in tracked_paths
    ):
        raise ValidationError("MetaDrive import is outside the bound tracked source tree")
    return {
        "environment_manifest": environment_manifest,
        "metadrive_module": module,
        "source_tree": source_tree,
        "verification_status": "formal_verified" if formal else "draft_verified",
    }


def _revalidate_normalized_metadrive_runtime_dependencies(
    worker_config: Mapping[str, Any]
) -> Dict[str, Any]:
    dependencies = worker_config.get("runtime_dependencies")
    if not isinstance(dependencies, Mapping) or set(dependencies) != {
        "environment_manifest",
        "metadrive_module",
        "source_tree",
        "verification_status",
    }:
        raise ValidationError("MetaDrive runtime lacks bound runtime dependencies")
    formal = dependencies.get("verification_status") == "formal_verified"
    verified = _validate_metadrive_runtime_dependencies(
        {
            "environment_manifest": dependencies["environment_manifest"],
            "metadrive_module": dependencies["metadrive_module"],
            "source_tree": dependencies["source_tree"],
        },
        {
            "interpreter": worker_config.get("interpreter"),
            "environment": worker_config.get("environment"),
        },
        formal=formal,
    )
    if canonical_json_bytes(verified) != canonical_json_bytes(dependencies):
        raise ValidationError("MetaDrive runtime dependency binding changed")
    return verified


def _validate_runtime_environment_manifest(
    binding: Any, interpreter: Path, *, formal: bool
) -> Dict[str, Any]:
    normalized = _exact_file_binding(binding, "CARLA runtime environment manifest")
    manifest = read_json(Path(normalized["path"]))
    validate_schema_instance(
        manifest,
        "method_environment_manifest",
        context="CARLA runtime environment manifest",
    )
    if manifest.get("runtime_kind") != "python":
        raise ValidationError("CARLA runtime environment must be Python")
    executable = _exact_file_binding(
        manifest.get("executable"), "CARLA runtime environment executable"
    )
    if Path(executable["path"]).resolve() != Path(interpreter).resolve():
        raise ValidationError("CARLA runtime environment executable differs from worker")
    from .generation import _validate_python_environment_controls

    _validate_python_environment_controls(
        manifest,
        Path(executable["path"]),
        required=True,
        label="CARLA runtime environment manifest",
    )
    if formal:
        if manifest.get("decision_status") != "frozen":
            raise ValidationError("formal CARLA runtime requires a frozen environment manifest")
        raise ValidationError(
            "formal CARLA Python runtime requires a complete runtime-tree closure"
        )
    return normalized


def _validate_carla_runtime_dependencies(
    document: Any,
    worker: Mapping[str, Any],
    server_binary: Mapping[str, Any],
    *,
    formal: bool,
) -> Dict[str, Any]:
    required = {
        "environment_manifest",
        "python_path_entries",
        "source_trees",
        "map_catalog",
    }
    if document is None and not formal:
        return {
            "environment_manifest": None,
            "python_path_entries": [],
            "source_trees": [],
            "map_catalog": [],
            "verification_status": "draft_unverified",
        }
    if not isinstance(document, Mapping) or set(document) != required:
        raise ValidationError("CARLA runtime dependencies are incomplete or unknown")
    interpreter = Path(str(worker.get("interpreter", ""))).resolve()
    environment_manifest = _validate_runtime_environment_manifest(
        document.get("environment_manifest"), interpreter, formal=formal
    )

    python_entries = [
        _exact_file_binding(binding, "CARLA PYTHONPATH entry")
        for binding in document.get("python_path_entries", [])
    ]
    python_paths = [binding["path"] for binding in python_entries]
    if len(python_paths) != len(set(python_paths)):
        raise ValidationError("CARLA runtime repeats a PYTHONPATH binding")
    environment = worker.get("environment", {})
    bound_environment_paths = []
    for key in sorted(_CONTENT_BEARING_ENVIRONMENT_KEYS):
        value = environment.get(key)
        if value:
            bound_environment_paths.extend(
                str(Path(part).resolve()) for part in value.split(os.pathsep) if part
            )
    if sorted(bound_environment_paths) != sorted(python_paths):
        raise ValidationError(
            "CARLA content-bearing runtime environment paths are not bound exactly"
        )

    trees = [
        _validate_runtime_tree_binding(
            binding,
            "CARLA runtime source tree",
            verify_bytes=formal,
        )
        for binding in document.get("source_trees", [])
    ]
    tree_by_role = {binding["role"]: binding for binding in trees}
    if set(tree_by_role) != _CARLA_RUNTIME_TREE_ROLES or len(trees) != len(tree_by_role):
        raise ValidationError("CARLA runtime must bind Scenic and server trees exactly once")
    normalized_server = _validate_carla_server_binary(server_binary)
    server_path = Path(normalized_server["path"])
    server_root = Path(tree_by_role["carla_server_distribution"]["path"])
    try:
        relative_server = server_path.relative_to(server_root)
    except ValueError as exc:
        raise ValidationError("CARLA server binary is outside its distribution tree") from exc
    if relative_server != _CARLA_SERVER_RELATIVE_PATH:
        raise ValidationError(
            "CARLA server binary is not the frozen shipping leaf in its distribution tree"
        )

    catalog = [
        _exact_file_binding(binding, "CARLA Scenic map")
        for binding in document.get("map_catalog", [])
    ]
    names = [Path(binding["path"]).name for binding in catalog]
    if (
        not catalog
        or names != sorted(names)
        or len(names) != len(set(names))
        or any(Path(name).suffix not in {".xodr", ".snet"} for name in names)
        or not any(Path(name).suffix == ".xodr" for name in names)
        or len({str(Path(binding["path"]).parent) for binding in catalog}) != 1
    ):
        raise ValidationError("CARLA map catalog is empty, unsorted, duplicated, or malformed")
    xodr_stems = {Path(name).stem for name in names if Path(name).suffix == ".xodr"}
    if any(Path(name).stem not in xodr_stems for name in names if Path(name).suffix == ".snet"):
        raise ValidationError("CARLA map cache lacks its bound OpenDRIVE source")

    if formal:
        probe_code = (
            "import carla,json,scenic;"
            "print(json.dumps({'carla':carla.__file__,'scenic':scenic.__file__},sort_keys=True))"
        )
        try:
            process = subprocess.run(
                [str(interpreter), "-c", probe_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
                env=_worker_environment(environment),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError("CARLA runtime import probe failed") from exc
        if process.returncode != 0:
            raise ValidationError("CARLA runtime cannot import its bound Scenic/CARLA modules")
        imported = strict_json_object_bytes(process.stdout, "CARLA runtime import probe")
        scenic_path = Path(str(imported.get("scenic", ""))).resolve()
        try:
            scenic_path.relative_to(Path(tree_by_role["scenic_editable_source"]["path"]))
        except ValueError as exc:
            raise ValidationError("CARLA runtime imported Scenic outside its bound tree") from exc
        carla_source = str(imported.get("carla", ""))
        if not any(carla_source.startswith(path + os.sep) for path in python_paths):
            raise ValidationError("CARLA runtime imported CARLA outside its bound PYTHONPATH entry")

    return {
        "environment_manifest": environment_manifest,
        "python_path_entries": python_entries,
        "source_trees": trees,
        "map_catalog": catalog,
        "verification_status": "formal_verified" if formal else "draft_bound",
    }


def _revalidate_normalized_carla_runtime_dependencies(
    worker_config: Mapping[str, Any]
) -> Dict[str, Any]:
    dependencies = worker_config.get("runtime_dependencies")
    context = worker_config.get("platform_context")
    if (
        not isinstance(dependencies, Mapping)
        or not isinstance(context, Mapping)
        or not isinstance(context.get("server_binary"), Mapping)
    ):
        raise ValidationError("CARLA runtime dependency revalidation is unavailable")
    expected_status = dependencies.get("verification_status")
    if expected_status not in {"draft_bound", "formal_verified"}:
        raise ValidationError("CARLA runtime dependency status is invalid")
    raw = {
        key: dependencies.get(key)
        for key in (
            "environment_manifest",
            "python_path_entries",
            "source_trees",
            "map_catalog",
        )
    }
    source_trees = raw.get("source_trees")
    if not isinstance(source_trees, list):
        raise ValidationError("CARLA runtime source-tree bindings are unavailable")
    # Development runs are not formally admitted, but their stage evidence must
    # still refer to one stable Scenic/server byte tree.  Recompute both tree
    # digests around every Compile/SV/NE stage instead of limiting the expensive
    # byte walk to formal mode.
    for index, binding in enumerate(source_trees):
        _validate_runtime_tree_binding(
            binding,
            "CARLA runtime source tree[{}]".format(index),
            verify_bytes=True,
        )
    verified = _validate_carla_runtime_dependencies(
        raw,
        worker_config,
        context["server_binary"],
        formal=expected_status == "formal_verified",
    )
    if canonical_json_bytes(verified) != canonical_json_bytes(dependencies):
        raise ValidationError("CARLA runtime dependency binding changed")
    return verified


def _artifact_descriptor(path: Path) -> Dict[str, Any]:
    path = Path(path).resolve()
    if not path.is_file():
        raise ValidationError("runtime capture is not a file: {}".format(path))
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _declared_carla_map_stem(artifact_path: Path) -> str:
    try:
        text = Path(artifact_path).read_text(encoding="utf-8")
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (OSError, UnicodeError, tokenize.TokenError) as exc:
        raise ValidationError("CARLA artifact map declaration is unreadable") from exc
    values = []
    significant = [
        token
        for token in tokens
        if token.type
        not in {
            tokenize.ENCODING,
            tokenize.ENDMARKER,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.COMMENT,
        }
    ]
    for index, token in enumerate(significant[:-2]):
        equals = significant[index + 1]
        literal = significant[index + 2]
        if (
            token.type == tokenize.NAME
            and token.string == "Town"
            and token.start[1] == 0
            and equals.type == tokenize.OP
            and equals.string == "="
            and literal.type == tokenize.STRING
            and token.start[0] == equals.start[0] == literal.start[0]
        ):
            try:
                value = ast.literal_eval(literal.string)
            except (SyntaxError, ValueError) as exc:
                raise ValidationError("CARLA Town declaration must be a string literal") from exc
            values.append(value)
    if (
        len(values) != 1
        or not isinstance(values[0], str)
        or re.fullmatch(r"Town(?:0[1-7]|10HD)", values[0]) is None
    ):
        raise ValidationError("CARLA artifact requires one supported literal Town declaration")
    return values[0]


def _prepare_carla_native_input(
    artifact_path: Path,
    attempt: Path,
    runtime_dependencies: Mapping[str, Any],
) -> tuple:
    """Recreate ChatScene's immutable ``dynamic_scenario/../maps`` layout.

    The finalized Scenic artifact and its selected OpenDRIVE/cache inputs are
    copied byte-for-byte, then re-hashed.  Both the frozen source catalog and the
    exact staged files are sent to the worker; no inode is shared with live inputs.
    """

    catalog = runtime_dependencies.get("map_catalog", [])
    if not catalog:
        raise ValidationError("CARLA runtime cannot stage an artifact without a map catalog")
    town = _declared_carla_map_stem(artifact_path)
    selected_names = {"{}.xodr".format(town), "{}.snet".format(town)}
    selected_catalog = [
        binding
        for binding in catalog
        if Path(binding["path"]).name in selected_names
    ]
    if not any(Path(binding["path"]).name == "{}.xodr".format(town) for binding in selected_catalog):
        raise ValidationError("CARLA artifact Town is absent from the frozen map catalog")
    native_root = attempt / "native-input"
    scenario_directory = native_root / "dynamic_scenario"
    map_directory = native_root / "maps"
    scenario_directory.mkdir(parents=True, exist_ok=False)
    map_directory.mkdir(parents=True, exist_ok=False)
    staged_artifact = scenario_directory / "finalized_artifact.scenic"
    shutil.copyfile(str(artifact_path), str(staged_artifact))
    source_artifact = _artifact_descriptor(artifact_path)
    staged_artifact_binding = _artifact_descriptor(staged_artifact)
    if (
        staged_artifact_binding["sha256"] != source_artifact["sha256"]
        or staged_artifact_binding["bytes"] != source_artifact["bytes"]
        or os.path.samefile(str(artifact_path), str(staged_artifact))
    ):
        raise ValidationError("CARLA native staging changed finalized artifact bytes")

    staged_catalog = []
    for binding in selected_catalog:
        source = Path(binding["path"])
        target = map_directory / source.name
        shutil.copyfile(str(source), str(target))
        staged = _artifact_descriptor(target)
        if (
            staged["sha256"] != binding["sha256"]
            or staged["bytes"] != binding["bytes"]
            or os.path.samefile(str(source), str(target))
        ):
            raise ValidationError("CARLA staged map differs from its source binding")
        staged_catalog.append(staged)
    return staged_artifact.resolve(), {
        "declared_town": town,
        "source_artifact": source_artifact,
        "staged_artifact": staged_artifact_binding,
        "source_catalog": [dict(binding) for binding in selected_catalog],
        "staged_catalog": staged_catalog,
    }


def _verify_artifact(descriptor: Mapping[str, Any]) -> Path:
    if not isinstance(descriptor, Mapping):
        raise ValidationError("runtime requires an artifact descriptor")
    path = Path(str(descriptor.get("path", ""))).resolve()
    if not path.is_file():
        raise ValidationError("finalized artifact is missing: {}".format(path))
    if descriptor.get("sha256") != sha256_file(path):
        raise ValidationError("finalized artifact hash changed before runtime")
    if descriptor.get("bytes") != path.stat().st_size:
        raise ValidationError("finalized artifact byte count changed before runtime")
    if path.stat().st_size <= 0:
        raise ValidationError("finalized artifact is empty")
    return path


def _validate_generation_chain_binding(
    response: Mapping[str, Any], generation_chain: Mapping[str, Any]
) -> Dict[str, Any]:
    """Bind runtime execution to one already-validated generation run manifest."""

    required = {
        "method_id",
        "platform",
        "record_count",
        "manifest_sha256",
        "implementation_bundle_sha256",
        "verified",
    }
    if not isinstance(generation_chain, Mapping) or set(generation_chain) != required:
        raise ValidationError("runtime requires one exact generation-chain binding")
    normalized = dict(generation_chain)
    for field in ("manifest_sha256", "implementation_bundle_sha256"):
        value = normalized.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValidationError("generation-chain {} is not a SHA-256".format(field))
    if type(normalized.get("record_count")) is not int or normalized["record_count"] < 1:
        raise ValidationError("generation-chain record_count must be positive")
    if type(normalized.get("verified")) is not bool:
        raise ValidationError("generation-chain verified flag must be boolean")
    if (
        normalized["method_id"] != response.get("method_id")
        or normalized["platform"] != response.get("platform")
        or normalized["implementation_bundle_sha256"]
        != response.get("implementation_bundle_sha256")
    ):
        raise ValidationError("runtime response differs from its generation chain")
    return normalized


def _next_attempt_directory(run_root: Path) -> Path:
    run_root.mkdir(parents=True, exist_ok=True)
    for index in range(10000):
        candidate = run_root / "attempt-{:04d}".format(index)
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise ValidationError("runtime attempt limit exceeded")


def _worker_environment(extra: Mapping[str, str]) -> Dict[str, str]:
    allowed = {
        "PATH": os.defpath,
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "MPLCONFIGDIR": "/tmp",
        "SDL_VIDEODRIVER": "dummy",
    }
    if set(extra) - _ALLOWED_WORKER_ENVIRONMENT_KEYS:
        raise ValidationError("runtime worker environment contains an unfrozen key")
    for key, value in extra.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValidationError("runtime worker environment must be string-to-string")
        allowed[key] = value
    return allowed


def _run_worker_stage(
    request: Mapping[str, Any],
    stage_directory: Path,
    worker_config: Mapping[str, Any],
) -> Dict[str, Any]:
    interpreter = _validate_expected_runtime_file(
        worker_config.get("interpreter"),
        worker_config.get("interpreter_sha256"),
        worker_config.get("interpreter_bytes"),
        "runtime worker interpreter",
    )
    worker = _validate_expected_runtime_file(
        worker_config.get("worker"),
        worker_config.get("worker_sha256"),
        worker_config.get("worker_bytes"),
        "runtime worker script",
    )
    interpreter_identity = _runtime_file_identity(
        interpreter, "runtime worker interpreter"
    )
    worker_identity = _runtime_file_identity(worker, "runtime worker script")
    stage_directory.mkdir(parents=True, exist_ok=False)
    request_path = stage_directory / "request.json"
    stdout_path = stage_directory / "stdout.json"
    stderr_path = stage_directory / "stderr.txt"
    bound_request = dict(request)
    bound_request["runtime_executor"] = {
        "interpreter_path": str(interpreter),
        "interpreter_sha256": worker_config["interpreter_sha256"],
        "interpreter_bytes": worker_config["interpreter_bytes"],
        "worker_path": str(worker),
        "worker_sha256": worker_config["worker_sha256"],
        "worker_bytes": worker_config["worker_bytes"],
    }
    write_json(request_path, bound_request)
    timeout_seconds = worker_config.get("timeout_seconds", 120)
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValidationError("runtime worker timeout_seconds must be a positive integer")

    started = _utc_now()
    timed_out = False
    exit_code: Optional[int]
    try:
        process = subprocess.run(
            [str(interpreter), str(worker)],
            input=canonical_json_bytes(bound_request),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
            cwd=str(stage_directory),
            env=_worker_environment(worker_config.get("environment", {})),
        )
        stdout = process.stdout
        stderr = process.stderr
        exit_code = process.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
    _validate_expected_runtime_file(
        str(interpreter),
        worker_config["interpreter_sha256"],
        worker_config["interpreter_bytes"],
        "runtime worker interpreter",
    )
    _validate_expected_runtime_file(
        str(worker),
        worker_config["worker_sha256"],
        worker_config["worker_bytes"],
        "runtime worker script",
    )
    if _runtime_file_identity(
        interpreter, "runtime worker interpreter"
    ) != interpreter_identity:
        raise ValidationError("runtime worker interpreter changed during stage")
    if _runtime_file_identity(worker, "runtime worker script") != worker_identity:
        raise ValidationError("runtime worker script changed during stage")
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    finished = _utc_now()

    result = None
    parse_error = None
    if not timed_out and exit_code == 0:
        try:
            result = strict_json_object_bytes(stdout, "runtime worker stdout")
        except ValidationError as exc:
            parse_error = str(exc)

    status = "timeout" if timed_out else "failed"
    if result is not None and result.get("ok") is True:
        status = "passed"
    error = None
    if status != "passed":
        if result is not None and isinstance(result.get("error"), str):
            error = result["error"]
        elif parse_error:
            error = "invalid worker result: {}".format(parse_error)
        elif timed_out:
            error = "worker timed out"
        else:
            error = "worker exited with code {}".format(exit_code)
    return {
        "status": status,
        "started_at_utc": started,
        "finished_at_utc": finished,
        "request": _artifact_descriptor(request_path),
        "stdout": _artifact_descriptor(stdout_path),
        "stderr": _artifact_descriptor(stderr_path),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "result": result,
        "error": error,
    }


def _run_metadrive_worker_stage(
    request: Mapping[str, Any],
    stage_directory: Path,
    worker_config: Mapping[str, Any],
) -> Dict[str, Any]:
    """Re-probe the full MetaDrive runtime immediately around every stage."""

    _revalidate_normalized_metadrive_runtime_dependencies(worker_config)
    try:
        return _run_worker_stage(request, stage_directory, worker_config)
    finally:
        _revalidate_normalized_metadrive_runtime_dependencies(worker_config)


def _run_carla_worker_stage(
    request: Mapping[str, Any],
    stage_directory: Path,
    worker_config: Mapping[str, Any],
) -> Dict[str, Any]:
    """Revalidate all normalized CARLA dependencies around every stage."""

    _revalidate_normalized_carla_runtime_dependencies(worker_config)
    try:
        return _run_worker_stage(request, stage_directory, worker_config)
    finally:
        _revalidate_normalized_carla_runtime_dependencies(worker_config)


def _run_carla_ne_stage(
    request: Mapping[str, Any],
    stage_directory: Path,
    worker_config: Mapping[str, Any],
) -> Dict[str, Any]:
    """Run CARLA NE only with auditable pre/post server-process identity."""

    endpoint = worker_config["platform_context"]["carla_endpoint"]
    server_binary = worker_config["platform_context"]["server_binary"]
    preflight = _carla_server_preflight(endpoint, server_binary)
    bound_request = dict(
        request,
        carla_server_binary=dict(server_binary),
        carla_server_preflight=preflight,
    )
    stage = _run_carla_worker_stage(bound_request, stage_directory, worker_config)
    postflight = (
        _carla_server_preflight(endpoint, server_binary)
        if preflight["status"] == "verified"
        else None
    )
    verified = (
        preflight["status"] == "verified"
        and postflight is not None
        and postflight["status"] == "verified"
        and canonical_json_bytes(preflight["attestation"])
        == canonical_json_bytes(postflight["attestation"])
    )
    if preflight["status"] != "verified":
        failure = "preflight:{}".format(preflight["failure_reason"])
    elif postflight is None or postflight["status"] != "verified":
        failure = "postflight:{}".format(
            None if postflight is None else postflight["failure_reason"]
        )
    elif not verified:
        failure = "postflight:server_process_identity_changed"
    else:
        failure = None
    session_document = {
        "schema_version": SCHEMA_VERSION,
        "verification_status": "verified" if verified else "failed",
        "verification_failure": failure,
        "endpoint": dict(endpoint),
        "server_binary": dict(server_binary),
        "preflight": preflight,
        "postflight": postflight,
    }
    session_path = stage_directory / "server-session.json"
    write_json(session_path, session_document)
    stage["server_session"] = _artifact_descriptor(session_path)
    if not verified and stage["status"] == "passed":
        stage["status"] = "failed"
        stage["error"] = "CARLA server session attestation failed: {}".format(failure)
    return stage


def _skipped_stage(reason: str) -> Dict[str, Any]:
    now = _utc_now()
    return {
        "status": "skipped",
        "started_at_utc": now,
        "finished_at_utc": now,
        "request": None,
        "stdout": None,
        "stderr": None,
        "exit_code": None,
        "timed_out": False,
        "result": None,
        "error": reason,
    }


def _iec_exec_diagnostic(ne_stage: Mapping[str, Any]) -> Dict[str, Any]:
    trace_available = (
        ne_stage.get("status") == "passed" and ne_stage.get("trace") is not None
    )
    return {
        "status": "unavailable",
        "coverage": "none",
        "trace_available": trace_available,
        "reason": (
            "frozen_query_blind_event_observer_not_registered"
            if trace_available
            else "native_rollout_trace_unavailable"
        ),
    }


def _validate_controller_config(
    controller: Mapping[str, Any], platform: str, allow_draft: bool = False
) -> None:
    validate_schema_instance(controller, "controller_config", context="controller config")
    if controller.get("platform") != platform:
        raise ValidationError("controller platform differs from response platform")
    if not allow_draft and controller.get("status") != "frozen":
        raise ValidationError(
            "runtime controller must be frozen"
        )
    if (
        controller.get("query_blind") is not True
        or controller.get("reads_query_metadata") is not False
        or controller.get("reads_evaluation_metadata") is not False
    ):
        raise ValidationError("runtime controller must be query-blind")
    implementation = controller["implementation"]
    if implementation.get("class") != _CONTROLLER_CLASSES[platform]:
        raise ValidationError("runtime controller implementation class is not frozen")
    source = Path(implementation["source_path"]).resolve()
    if not source.is_file() or sha256_file(source) != implementation["source_sha256"]:
        raise ValidationError("runtime controller source binding changed")
    parameters = controller["parameters"]
    required_parameters = {
        "metadrive": {
            "target_speed_km_h",
            "acceleration_factor",
            "deceleration_factor",
            "dt_seconds",
        },
        "carla": {
            "dt_seconds",
            "target_speed_m_s",
            "lookahead_seconds",
            "lateral_kp",
            "lateral_ki",
            "lateral_kd",
            "max_front_wheel_rate_rad_s",
            "max_front_wheel_angle_rad",
            "lane_half_width_m",
            "idm_delta",
            "max_acceleration_m_s2",
            "comfortable_deceleration_m_s2",
            "max_deceleration_m_s2",
            "max_longitudinal_jerk_m_s3",
            "minimum_gap_m",
            "time_headway_seconds",
        },
    }[platform]
    if set(parameters) != required_parameters:
        raise ValidationError("runtime controller parameters are incomplete or unknown")
    expected_parameters = {
        "carla": {
            "dt_seconds": CARLA_TIMESTEP_SECONDS,
            "max_acceleration_m_s2": 0.7,
            "max_deceleration_m_s2": 4.0,
        },
        "metadrive": {
            "dt_seconds": CARLA_TIMESTEP_SECONDS,
            "acceleration_factor": 0.7,
            "deceleration_factor": -4.0,
        },
    }[platform]
    if any(
        parameters.get(key) != value
        for key, value in expected_parameters.items()
    ):
        raise ValidationError("runtime controller violates the fixed platform parameters")
    if (
        platform == "metadrive"
        and controller["action_contract"]["longitudinal"]
        != (
            "normalized throttle/brake [-1,1]; IDM factors are dimensionless "
            "and not SI acceleration bounds"
        )
    ):
        raise ValidationError("MetaDrive controller overstates its longitudinal action semantics")


def validate_platform_runtime_config(
    config: Mapping[str, Any], platform: Optional[str] = None, allow_draft: bool = False
) -> Dict[str, Any]:
    """Validate and normalize a frozen platform worker configuration."""

    validate_schema_instance(
        config, "platform_runtime_config", context="platform runtime config"
    )
    actual_platform = config["platform"]
    if platform is not None and actual_platform != platform:
        raise ValidationError("platform runtime config differs from the roster platform")
    if not allow_draft and config["status"] != "frozen":
        raise ValidationError("platform runtime config must be frozen")
    if (
        config["sv_seeds"] != list(SV_SEEDS)
        or config["sv_max_iterations"] != SV_MAX_ITERATIONS
        or config["rollout_seconds"] != ROLLOUT_SECONDS
    ):
        raise ValidationError("platform runtime config changes the fixed stage budget")
    worker = config["worker"]
    interpreter = _regular_runtime_file(
        worker.get("interpreter"), "platform runtime interpreter"
    )
    source = _regular_runtime_file(
        worker.get("worker"), "platform runtime worker"
    )
    if (
        not interpreter.is_file()
        or sha256_file(interpreter) != worker["interpreter_sha256"]
    ):
        raise ValidationError("platform runtime interpreter binding changed")
    if not source.is_file() or sha256_file(source) != worker["worker_sha256"]:
        raise ValidationError("platform runtime worker binding changed")
    platform_context: Dict[str, Any] = {}
    runtime_dependencies = None
    if actual_platform == "carla":
        endpoint = config.get("carla_endpoint")
        server = config.get("server_binary")
        if not isinstance(endpoint, Mapping) or not isinstance(server, Mapping):
            raise ValidationError("CARLA platform runtime config lacks endpoint/server binding")
        normalized_server = _validate_carla_server_binary(server)
        if (
            endpoint.get("host") != "127.0.0.1"
            or type(endpoint.get("port")) is not int
            or type(endpoint.get("timeout_seconds")) not in (int, float)
        ):
            raise ValidationError("CARLA platform endpoint/server binding changed")
        platform_context = {
            "carla_endpoint": dict(endpoint),
            "server_binary": normalized_server,
        }
        runtime_dependencies = _validate_carla_runtime_dependencies(
            config.get("runtime_dependencies"),
            worker,
            platform_context["server_binary"],
            formal=not allow_draft,
        )
    else:
        registry = config.get("token_registry")
        if not isinstance(registry, Mapping):
            raise ValidationError(
                "MetaDrive platform runtime config lacks a PG token registry binding"
            )
        registry_path = Path(str(registry.get("path", ""))).resolve()
        if (
            registry_path.is_symlink()
            or not registry_path.is_file()
            or registry.get("sha256") != sha256_file(registry_path)
            or registry.get("bytes") != registry_path.stat().st_size
        ):
            raise ValidationError("MetaDrive PG token registry binding changed")
        from .metadrive_pg import MetaDrivePGError, load_token_registry

        try:
            load_token_registry(
                registry_path,
                expected_sha256=registry["sha256"],
                formal=not allow_draft,
                verify_files=True,
            )
        except MetaDrivePGError as exc:
            raise ValidationError(
                "MetaDrive PG token registry is invalid: {}".format(exc)
            ) from exc
        platform_context = {
            "metadrive_token_registry": {
                "path": str(registry_path),
                "sha256": registry["sha256"],
                "bytes": registry["bytes"],
            }
        }
        runtime_dependencies = _validate_metadrive_runtime_dependencies(
            config.get("runtime_dependencies"),
            worker,
            formal=not allow_draft,
        )
    normalized = {
        "interpreter": str(interpreter),
        "interpreter_sha256": worker["interpreter_sha256"],
        "interpreter_bytes": interpreter.stat().st_size,
        "worker": str(source),
        "worker_sha256": worker["worker_sha256"],
        "worker_bytes": source.stat().st_size,
        "timeout_seconds": worker["timeout_seconds"],
        "environment": dict(worker["environment"]),
        "platform_context": platform_context,
    }
    if runtime_dependencies is not None:
        normalized["runtime_dependencies"] = runtime_dependencies
    _worker_environment(normalized["environment"])
    return normalized


def execute_runtime_record(
    response: Mapping[str, Any],
    output_root: Path,
    worker_config: Mapping[str, Any],
    controller_config: Mapping[str, Any],
    generation_chain: Mapping[str, Any],
    resume: bool = False,
    allow_draft_controller: bool = False,
    allow_draft_runtime: bool = False,
) -> Dict[str, Any]:
    """Execute the fixed native-stage budget for one immutable response record."""

    platform = response.get("platform")
    if platform not in ("carla", "metadrive"):
        raise ValidationError("runtime response platform must be carla or metadrive")
    _validate_controller_config(
        controller_config, platform, allow_draft=allow_draft_controller
    )
    normalized_generation_chain = _validate_generation_chain_binding(
        response, generation_chain
    )
    source_response_sha256 = record_sha256(response)
    source_config_sha256 = response.get("config_sha256")
    if (
        not isinstance(source_config_sha256, str)
        or len(source_config_sha256) != 64
        or any(character not in "0123456789abcdef" for character in source_config_sha256)
    ):
        raise ValidationError("runtime response lacks a frozen method config hash")
    controller_config_sha256 = record_sha256(controller_config)
    normalized_worker_config = {
        "interpreter": str(
            _validate_expected_runtime_file(
                worker_config.get("interpreter"),
                worker_config.get("interpreter_sha256"),
                worker_config.get("interpreter_bytes"),
                "runtime worker interpreter",
            )
        ),
        "interpreter_sha256": worker_config.get("interpreter_sha256"),
        "interpreter_bytes": worker_config.get("interpreter_bytes"),
        "worker": str(
            _validate_expected_runtime_file(
                worker_config.get("worker"),
                worker_config.get("worker_sha256"),
                worker_config.get("worker_bytes"),
                "runtime worker script",
            )
        ),
        "worker_sha256": worker_config.get("worker_sha256"),
        "worker_bytes": worker_config.get("worker_bytes"),
        "timeout_seconds": worker_config.get("timeout_seconds", 120),
        "environment": dict(worker_config.get("environment", {})),
        "platform_context": dict(worker_config.get("platform_context", {})),
    }
    if platform == "carla":
        dependencies = worker_config.get("runtime_dependencies")
        if not isinstance(dependencies, Mapping):
            raise ValidationError("CARLA runtime lacks bound runtime dependencies")
        normalized_worker_config["runtime_dependencies"] = {
            "environment_manifest": dict(dependencies["environment_manifest"])
            if isinstance(dependencies.get("environment_manifest"), Mapping)
            else None,
            "python_path_entries": [
                dict(binding) for binding in dependencies.get("python_path_entries", [])
            ],
            "source_trees": [
                dict(binding) for binding in dependencies.get("source_trees", [])
            ],
            "map_catalog": [
                dict(binding) for binding in dependencies.get("map_catalog", [])
            ],
            "verification_status": dependencies.get("verification_status"),
        }
        _revalidate_normalized_carla_runtime_dependencies(
            normalized_worker_config
        )
        if (
            not allow_draft_runtime
            and normalized_worker_config["runtime_dependencies"]["verification_status"]
            != "formal_verified"
        ):
            raise ValidationError("formal CARLA runtime dependencies are not verified")
    else:
        dependencies = worker_config.get("runtime_dependencies")
        if not isinstance(dependencies, Mapping) or set(dependencies) != {
            "environment_manifest",
            "metadrive_module",
            "source_tree",
            "verification_status",
        }:
            raise ValidationError("MetaDrive runtime lacks bound runtime dependencies")
        formal_dependencies = dependencies.get("verification_status") == "formal_verified"
        normalized_worker_config["runtime_dependencies"] = {
            "environment_manifest": dict(dependencies["environment_manifest"]),
            "metadrive_module": dict(dependencies["metadrive_module"]),
            "source_tree": dict(dependencies["source_tree"]),
            "verification_status": dependencies["verification_status"],
        }
        _revalidate_normalized_metadrive_runtime_dependencies(
            normalized_worker_config
        )
        if not allow_draft_runtime and not formal_dependencies:
            raise ValidationError("formal MetaDrive runtime dependencies are not verified")
    if platform == "carla" and set(normalized_worker_config["platform_context"]) != {
        "carla_endpoint",
        "server_binary",
    }:
        raise ValidationError("CARLA runtime lacks its frozen platform context")
    if platform == "metadrive" and set(
        normalized_worker_config["platform_context"]
    ) != {"metadrive_token_registry"}:
        raise ValidationError("MetaDrive runtime lacks its frozen PG token registry")
    _worker_environment(normalized_worker_config["environment"])
    interpreter_path = Path(normalized_worker_config["interpreter"])
    worker_path = Path(normalized_worker_config["worker"])
    if not interpreter_path.is_file() or not worker_path.is_file():
        raise ValidationError("runtime worker interpreter/script is missing")
    worker_config_sha256 = record_sha256(normalized_worker_config)
    run_root = Path(output_root).resolve() / _safe_run_key(response.get("run_id"))
    record_path = run_root / "runtime_record.json"
    if record_path.exists():
        existing = read_json(record_path)
        if (
            resume
            and existing.get("source_response_sha256") == source_response_sha256
            and existing.get("run_id") == response.get("run_id")
            and existing.get("source_generation_manifest_sha256")
            == normalized_generation_chain["manifest_sha256"]
            and existing.get("ne", {}).get("controller_config_sha256")
            == controller_config_sha256
            and existing.get("provenance", {}).get("worker_config_sha256")
            == worker_config_sha256
        ):
            validate_runtime_record_consistency(
                existing, allow_draft_controller=allow_draft_controller
            )
            return existing
        raise ValidationError("runtime record is already terminal: {}".format(record_path))
    attempt = _next_attempt_directory(run_root)

    copied = {
        field: response.get(field)
        for field in (
            "run_id",
            "method_id",
            "platform",
            "query_id",
            "intent_group_id",
            "statistical_intent_cluster_id",
            "surface_style",
            "repetition",
            "expected_support",
        )
    }
    artifact = response.get("artifact")
    source_artifact_sha256 = artifact.get("sha256") if isinstance(artifact, Mapping) else None
    excluded_reason = None
    if response.get("expected_support") == "unsupported":
        excluded_reason = "unsupported_query_outside_runtime_metrics"
    elif not (
        response.get("disposition") in ("generate", "controlled_degradation")
        and response.get("terminal_status") == "complete"
        and isinstance(artifact, Mapping)
    ):
        excluded_reason = "no_complete_finalized_artifact"

    if excluded_reason is not None:
        compile_stage = _skipped_stage(excluded_reason)
        sv_trials = [
            dict(_skipped_stage(excluded_reason), seed=seed, iterations=None)
            for seed in SV_SEEDS
        ]
        ne_stage = dict(
            _skipped_stage(excluded_reason),
            selected_seed=None,
            simulated_seconds=0.0,
            steps=0,
            trace=None,
            controller_id=controller_config["controller_id"],
            controller_config_sha256=controller_config_sha256,
            runtime_controller_class=None,
            termination_reason=excluded_reason,
        )
        selected_seed = None
    else:
        artifact_path = _verify_artifact(artifact)
        native_artifact_path = artifact_path
        carla_map_context = None
        if platform == "carla":
            native_artifact_path, carla_map_context = _prepare_carla_native_input(
                artifact_path,
                attempt,
                normalized_worker_config["runtime_dependencies"],
            )
        base_request = {
            "schema_version": SCHEMA_VERSION,
            "platform": platform,
            "artifact_path": str(native_artifact_path),
            "artifact_sha256": source_artifact_sha256,
            "source_response_sha256": source_response_sha256,
            "source_config_sha256": source_config_sha256,
            "source_generation_manifest_sha256": normalized_generation_chain[
                "manifest_sha256"
            ],
            "source_implementation_bundle_sha256": normalized_generation_chain[
                "implementation_bundle_sha256"
            ],
            "generation_chain_verified": normalized_generation_chain["verified"],
            "runtime_environment": {
                "values": dict(normalized_worker_config["environment"]),
                "bindings": [
                    dict(binding)
                    for binding in normalized_worker_config.get(
                        "runtime_dependencies", {}
                    ).get("python_path_entries", [])
                ],
            },
        }
        if platform == "carla":
            base_request["carla_endpoint"] = dict(
                normalized_worker_config["platform_context"]["carla_endpoint"]
            )
            base_request["carla_server_binary"] = dict(
                normalized_worker_config["platform_context"]["server_binary"]
            )
            base_request["carla_map_context"] = carla_map_context
        else:
            base_request["metadrive_token_registry"] = dict(
                normalized_worker_config["platform_context"]
                ["metadrive_token_registry"]
            )
            dependencies = normalized_worker_config["runtime_dependencies"]
            base_request["metadrive_runtime_context"] = {
                "metadrive_module": dict(dependencies["metadrive_module"]),
                "source_tree": dict(dependencies["source_tree"]),
            }
        stage_runner = (
            _run_carla_worker_stage
            if platform == "carla"
            else _run_metadrive_worker_stage
        )
        compile_stage = stage_runner(
            dict(base_request, mode="compile"),
            attempt / "compile",
            normalized_worker_config,
        )
        sv_trials = []
        if compile_stage["status"] == "passed":
            for seed in SV_SEEDS:
                stage = stage_runner(
                    dict(
                        base_request,
                        mode="sv",
                        seed=seed,
                        max_iterations=SV_MAX_ITERATIONS,
                    ),
                    attempt / "sv-{}".format(seed),
                    normalized_worker_config,
                )
                result = stage.get("result") or {}
                stage["seed"] = seed
                stage["iterations"] = result.get("iterations")
                sv_trials.append(stage)
        else:
            sv_trials = [
                dict(
                    _skipped_stage("compile_did_not_pass"),
                    seed=seed,
                    iterations=None,
                )
                for seed in SV_SEEDS
            ]
        passed_seeds = [
            trial["seed"] for trial in sv_trials if trial["status"] == "passed"
        ]
        selected_seed = min(passed_seeds) if passed_seeds else None
        if selected_seed is None:
            ne_stage = dict(
                _skipped_stage("no_sv_seed_passed"),
                selected_seed=None,
                simulated_seconds=0.0,
                steps=0,
                trace=None,
                controller_id=controller_config["controller_id"],
                controller_config_sha256=controller_config_sha256,
                runtime_controller_class=None,
                termination_reason="no_sv_seed_passed",
            )
        else:
            ne_request = dict(
                base_request,
                mode="ne",
                seed=selected_seed,
                rollout_seconds=ROLLOUT_SECONDS,
                controller=controller_config,
                trace_path=str((attempt / "ne" / "trace.jsonl").resolve()),
            )
            ne_stage = (
                _run_carla_ne_stage(
                    ne_request,
                    attempt / "ne",
                    normalized_worker_config,
                )
                if platform == "carla"
                else _run_metadrive_worker_stage(
                    ne_request,
                    attempt / "ne",
                    normalized_worker_config,
                )
            )
            ne_result = ne_stage.get("result") or {}
            trace_path = attempt / "ne" / "trace.jsonl"
            ne_stage.update(
                {
                    "selected_seed": selected_seed,
                    "simulated_seconds": float(ne_result.get("simulated_seconds", 0.0)),
                    "steps": int(ne_result.get("steps", 0)),
                    "trace": _artifact_descriptor(trace_path)
                    if trace_path.is_file()
                    else None,
                    "controller_id": controller_config["controller_id"],
                    "controller_config_sha256": controller_config_sha256,
                    "runtime_controller_class": ne_result.get(
                        "runtime_controller_class"
                    ),
                    "termination_reason": ne_result.get("termination_reason"),
                }
            )

    if platform == "carla" and "server_session" not in ne_stage:
        ne_stage["server_session"] = None

    record = {
        "schema_version": SCHEMA_VERSION,
        **copied,
        "source_response_sha256": source_response_sha256,
        "source_config_sha256": source_config_sha256,
        "source_generation_manifest_sha256": normalized_generation_chain[
            "manifest_sha256"
        ],
        "source_implementation_bundle_sha256": normalized_generation_chain[
            "implementation_bundle_sha256"
        ],
        "generation_chain_verified": normalized_generation_chain["verified"],
        "source_artifact_sha256": source_artifact_sha256,
        "excluded_reason": excluded_reason,
        "compile": compile_stage,
        "sv_trials": sv_trials,
        "selected_seed": selected_seed,
        "ne": ne_stage,
        "iec_exec": _iec_exec_diagnostic(ne_stage),
        "provenance": {
            "producer_id": "bus_benchmark.runtime",
            "producer_version": SCHEMA_VERSION,
            "created_at_utc": _utc_now(),
            "worker_path": str(worker_path),
            "worker_sha256": normalized_worker_config["worker_sha256"],
            "interpreter_path": str(interpreter_path),
            "interpreter_sha256": normalized_worker_config["interpreter_sha256"],
            "worker_config": normalized_worker_config,
            "worker_config_sha256": worker_config_sha256,
            "controller_source_sha256": controller_config["implementation"][
                "source_sha256"
            ],
            "controller_config": dict(controller_config),
            "attempt_path": str(attempt.resolve()),
        },
    }
    validate_runtime_record_consistency(
        record, allow_draft_controller=allow_draft_controller
    )
    write_json(record_path, record)
    return record


def execute_runtime_grid(
    responses: Iterable[Mapping[str, Any]],
    output_root: Path,
    worker_configs: Mapping[str, Mapping[str, Any]],
    controller_configs: Mapping[str, Mapping[str, Any]],
    generation_chain: Mapping[str, Any],
    resume: bool = False,
    allow_draft_controller: bool = False,
    allow_draft_runtime: bool = False,
    output_jsonl: Optional[Path] = None,
) -> Sequence[Dict[str, Any]]:
    records = []
    for response in responses:
        platform = response.get("platform")
        if platform not in worker_configs or platform not in controller_configs:
            raise ValidationError("runtime platform has no worker/controller config")
        records.append(
            execute_runtime_record(
                response,
                output_root,
                worker_configs[platform],
                controller_configs[platform],
                generation_chain,
                resume=resume,
                allow_draft_controller=allow_draft_controller,
                allow_draft_runtime=allow_draft_runtime,
            )
        )
    if output_jsonl is not None:
        write_jsonl(output_jsonl, records)
    return records


def aggregate_runtime(
    records: Iterable[Mapping[str, Any]],
    roster: Optional[Mapping[str, Any]] = None,
    responses: Optional[Iterable[Mapping[str, Any]]] = None,
    generation_chain: Optional[Mapping[str, Any]] = None,
    allow_partial: bool = False,
    require_confirmed: bool = True,
) -> Dict[str, Any]:
    """Report platform-labeled diagnostics without folding them into SRS/IEC_spec."""

    records = list(records)
    run_ids = [record.get("run_id") for record in records]
    if any(not value for value in run_ids) or len(set(run_ids)) != len(run_ids):
        raise ValidationError("runtime aggregation requires unique non-empty run_id values")
    method_ids = {record.get("method_id") for record in records}
    if len(method_ids) > 1:
        raise ValidationError("runtime aggregation cannot mix methods")
    if not allow_partial:
        if roster is None or responses is None:
            raise ValidationError(
                "formal runtime aggregation requires a frozen roster and response records"
            )
        validate_schema_instance(roster, "query_roster", context="runtime roster")
        validate_records_against_roster(
            records, roster, require_confirmed=require_confirmed
        )
        response_records = list(responses)
        validate_records_against_roster(
            response_records, roster, require_confirmed=require_confirmed
        )
        by_run = {response["run_id"]: response for response in response_records}
        if set(by_run) != set(run_ids):
            raise ValidationError("runtime records and responses have different run sets")
        for record in records:
            source_response = by_run[record["run_id"]]
            normalized_chain = _validate_generation_chain_binding(
                source_response, generation_chain
            )
            if require_confirmed and not normalized_chain["verified"]:
                raise ValidationError(
                    "formal runtime aggregation requires a verified generation chain"
                )
            if record["source_response_sha256"] != record_sha256(source_response):
                raise ValidationError("runtime record is not bound to its response record")
            if record["source_config_sha256"] != source_response.get("config_sha256"):
                raise ValidationError("runtime record is not bound to the method config")
            if (
                record["source_generation_manifest_sha256"]
                != normalized_chain["manifest_sha256"]
                or record["source_implementation_bundle_sha256"]
                != normalized_chain["implementation_bundle_sha256"]
                or record["generation_chain_verified"]
                is not normalized_chain["verified"]
            ):
                raise ValidationError("runtime record is not bound to the generation manifest")

    grouped: Dict[str, list] = {}
    for record in records:
        validate_runtime_record_consistency(record)
        grouped.setdefault(record["platform"], []).append(record)
    result = {}
    for platform, values in sorted(grouped.items()):
        controller_hashes = {
            value["ne"]["controller_config_sha256"] for value in values
        }
        worker_hashes = {
            value["provenance"]["worker_config_sha256"] for value in values
        }
        worker_sources = {
            value["provenance"]["worker_sha256"] for value in values
        }
        interpreter_hashes = {
            value["provenance"]["interpreter_sha256"] for value in values
        }
        if any(
            len(values_) != 1
            for values_ in (
                controller_hashes,
                worker_hashes,
                worker_sources,
                interpreter_hashes,
            )
        ):
            raise ValidationError(
                "runtime aggregation mixes controller, worker, or interpreter freezes"
            )
        # Only oracle-confirmed unsupported queries are outside SV/NE.  A supported
        # method failure or missing artifact stays in the denominator and contributes
        # zero, rather than disappearing as if it had never been attempted.
        eligible = [
            value for value in values if value.get("expected_support") == "supported"
        ]
        denominator = len(eligible)
        sv_trial_denominator = denominator * len(SV_SEEDS)
        result[platform] = {
            "eligible_outputs": denominator,
            "excluded_outputs": len(values) - denominator,
            "compile_success": sum(
                value["compile"]["status"] == "passed" for value in eligible
            )
            / denominator
            if denominator
            else None,
            "scene_validity": sum(
                trial["status"] == "passed"
                for value in eligible
                for trial in value["sv_trials"]
            )
            / sv_trial_denominator
            if sv_trial_denominator
            else None,
            "native_executability": sum(
                value["ne"]["status"] == "passed" for value in eligible
            )
            / denominator
            if denominator
            else None,
            "iec_exec": {
                "status": "unavailable",
                "coverage": "none",
                "available_outputs": 0,
                "trace_outputs": sum(
                    value["iec_exec"]["trace_available"] for value in eligible
                ),
                "eligible_outputs": denominator,
                "reason": "frozen_query_blind_event_observer_not_registered",
            },
        }
    return result


def _verify_runtime_capture(descriptor: Optional[Mapping[str, Any]]) -> None:
    if descriptor is None:
        return
    path = Path(descriptor["path"]).resolve()
    if (
        not path.is_file()
        or descriptor["sha256"] != sha256_file(path)
        or descriptor["bytes"] != path.stat().st_size
    ):
        raise ValidationError("runtime capture binding changed")


def _runtime_capture_json(descriptor: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    path = Path(descriptor["path"]).resolve()
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValidationError("cannot read {} capture".format(label)) from exc
    return strict_json_object_bytes(payload, "{} capture".format(label))


def _validate_carla_preflight_document(
    document: Any,
    endpoint: Mapping[str, Any],
    server_binary: Mapping[str, Any],
) -> None:
    if not isinstance(document, Mapping) or set(document) != {
        "status",
        "failure_reason",
        "attestation",
    }:
        raise ValidationError("CARLA server preflight is malformed")
    if document.get("status") == "failed":
        if (
            not isinstance(document.get("failure_reason"), str)
            or not document["failure_reason"]
            or document.get("attestation") is not None
        ):
            raise ValidationError("failed CARLA server preflight lacks its reason")
        return
    attestation = document.get("attestation")
    if (
        document.get("status") != "verified"
        or document.get("failure_reason") is not None
        or not isinstance(attestation, Mapping)
        or set(attestation) != {
            "pid",
            "process_starttime_ticks",
            "listener_table",
            "listener_local_address_hex",
            "listener_port",
            "socket_inode",
            "process_exe",
        }
        or type(attestation.get("pid")) is not int
        or attestation["pid"] < 1
        or type(attestation.get("process_starttime_ticks")) is not int
        or attestation["process_starttime_ticks"] < 1
        or attestation.get("listener_table") not in {"tcp", "tcp6"}
        or not isinstance(attestation.get("listener_local_address_hex"), str)
        or attestation.get("listener_port") != endpoint.get("port")
        or not isinstance(attestation.get("socket_inode"), str)
        or not attestation["socket_inode"].isdigit()
        or canonical_json_bytes(attestation.get("process_exe"))
        != canonical_json_bytes(server_binary)
    ):
        raise ValidationError("verified CARLA server preflight identity is malformed")


def _validate_carla_server_session(
    descriptor: Any,
    endpoint: Mapping[str, Any],
    server_binary: Mapping[str, Any],
) -> Mapping[str, Any]:
    _verify_runtime_capture(descriptor)
    document = _runtime_capture_json(descriptor, "CARLA server session")
    if not isinstance(document, Mapping) or set(document) != {
        "schema_version",
        "verification_status",
        "verification_failure",
        "endpoint",
        "server_binary",
        "preflight",
        "postflight",
    }:
        raise ValidationError("CARLA server session evidence is malformed")
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or canonical_json_bytes(document.get("endpoint"))
        != canonical_json_bytes(endpoint)
        or canonical_json_bytes(document.get("server_binary"))
        != canonical_json_bytes(server_binary)
    ):
        raise ValidationError("CARLA server session differs from frozen provenance")
    _validate_carla_preflight_document(
        document.get("preflight"), endpoint, server_binary
    )
    postflight = document.get("postflight")
    if postflight is not None:
        _validate_carla_preflight_document(postflight, endpoint, server_binary)
    preflight = document["preflight"]
    verified = (
        preflight["status"] == "verified"
        and isinstance(postflight, Mapping)
        and postflight.get("status") == "verified"
        and canonical_json_bytes(preflight["attestation"])
        == canonical_json_bytes(postflight["attestation"])
    )
    if verified:
        if (
            document.get("verification_status") != "verified"
            or document.get("verification_failure") is not None
        ):
            raise ValidationError("verified CARLA server session is self-inconsistent")
    elif (
        document.get("verification_status") != "failed"
        or not isinstance(document.get("verification_failure"), str)
        or not document["verification_failure"]
    ):
        raise ValidationError("failed CARLA server session is self-inconsistent")
    return document


def _validate_stage_capture(stage: Mapping[str, Any], label: str) -> None:
    skipped = stage.get("status") == "skipped"
    captures = (stage.get("request"), stage.get("stdout"), stage.get("stderr"))
    if skipped:
        if any(value is not None for value in captures) or stage.get("result") is not None:
            raise ValidationError("{} skipped stage cannot claim captures/results".format(label))
        if stage.get("timed_out") is not False or stage.get("exit_code") is not None:
            raise ValidationError("{} skipped stage has an impossible process state".format(label))
    else:
        if any(value is None for value in captures):
            raise ValidationError("{} attempted stage must retain all captures".format(label))
        if stage.get("status") == "timeout":
            if stage.get("timed_out") is not True or stage.get("exit_code") is not None:
                raise ValidationError("{} timeout stage has an impossible process state".format(label))
        elif stage.get("timed_out") is not False or stage.get("exit_code") is None:
            raise ValidationError("{} attempted stage has an impossible process state".format(label))
    for descriptor in captures:
        _verify_runtime_capture(descriptor)
    if stage.get("result") is not None:
        raw_result = _runtime_capture_json(stage["stdout"], "{} stdout".format(label))
        if canonical_json_bytes(raw_result) != canonical_json_bytes(stage["result"]):
            raise ValidationError("{} result differs from captured worker stdout".format(label))
    if stage.get("status") == "passed" and (stage.get("result") or {}).get("ok") is not True:
        raise ValidationError("{} passed stage lacks a successful worker result".format(label))
    if stage.get("status") == "passed" and (
        stage.get("exit_code") != 0 or stage.get("error") is not None
    ):
        raise ValidationError("{} passed stage has an impossible success state".format(label))


def _contains_forbidden_runtime_metadata(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_RUNTIME_METADATA_KEYS:
                return True
            if _contains_forbidden_runtime_metadata(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_runtime_metadata(item) for item in value)
    return False


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _validate_runtime_trace(
    record: Mapping[str, Any], request: Mapping[str, Any]
) -> None:
    descriptor = record["ne"]["trace"]
    trace_path = Path(descriptor["path"]).resolve()
    if trace_path != Path(request["trace_path"]).resolve():
        raise ValidationError("NE trace descriptor differs from the worker request")
    rows = read_jsonl(trace_path)
    steps = record["ne"]["steps"]
    if len(rows) != steps + 1:
        raise ValidationError("NE trace row count differs from the claimed step count")
    if _contains_forbidden_runtime_metadata(rows):
        raise ValidationError("NE trace contains query or evaluation metadata")
    dt = request["controller"]["parameters"]["dt_seconds"]
    platform = record["platform"]
    action_count = 0
    for index, row in enumerate(rows):
        if set(row) != {"step", "simulated_time_seconds", "objects", "ego_action"}:
            raise ValidationError("NE trace row violates the frozen trace contract")
        if row["step"] != index or not _finite(row["simulated_time_seconds"]):
            raise ValidationError("NE trace has an invalid step/time sequence")
        if not math.isclose(
            float(row["simulated_time_seconds"]), index * dt, abs_tol=1e-8
        ):
            raise ValidationError("NE trace time does not use the frozen timestep")
        objects = row["objects"]
        if not isinstance(objects, list) or sum(
            item.get("is_ego") is True for item in objects if isinstance(item, Mapping)
        ) != 1:
            raise ValidationError("NE trace row must contain exactly one ego object")
        for item in objects:
            if not isinstance(item, Mapping):
                raise ValidationError("NE trace object must be a JSON object")
            position = item.get("position")
            if (
                not isinstance(position, list)
                or len(position) != 2
                or not all(_finite(value) for value in position)
            ):
                raise ValidationError("NE trace object contains invalid 2D geometry")
        action = row["ego_action"]
        if action is None:
            continue
        if not isinstance(action, Mapping):
            raise ValidationError("NE ego action must be an object or null")
        action_count += 1
        expected_keys = (
            {"throttle", "brake", "steering"}
            if platform == "carla"
            else {"steering", "normalized_throttle_brake"}
        )
        if set(action) != expected_keys or not all(
            _finite(value) for value in action.values()
        ):
            raise ValidationError("NE trace action violates the platform action contract")
        if not -1.0 <= float(action["steering"]) <= 1.0:
            raise ValidationError("NE trace steering is outside the normalized range")
        if platform == "carla":
            if not all(
                0.0 <= float(action[key]) <= 1.0
                for key in ("throttle", "brake")
            ):
                raise ValidationError("NE trace throttle/brake is outside the normalized range")
        elif not -1.0 <= float(action["normalized_throttle_brake"]) <= 1.0:
            raise ValidationError("NE trace longitudinal action is outside the normalized range")
    if action_count != steps:
        raise ValidationError("NE trace does not preserve one ego action per simulated step")
    claimed_seconds = record["ne"]["simulated_seconds"]
    if not math.isclose(float(claimed_seconds), steps * dt, abs_tol=1e-8):
        raise ValidationError("NE simulated time differs from steps times the frozen timestep")


def validate_runtime_record_consistency(
    record: Mapping[str, Any], allow_draft_controller: bool = False
) -> Mapping[str, Any]:
    """Validate cross-field invariants which JSON Schema cannot express."""

    validate_schema_instance(record, "runtime_record", context="runtime record")
    if (
        record.get("source_implementation_bundle_sha256") is None
        or type(record.get("generation_chain_verified")) is not bool
    ):
        raise ValidationError("runtime record lacks its generation-chain binding")
    _validate_stage_capture(record["compile"], "compile")
    trials = record["sv_trials"]
    if [trial["seed"] for trial in trials] != list(SV_SEEDS):
        raise ValidationError("runtime SV trials must contain ordered unique seeds 0..4")
    for trial in trials:
        _validate_stage_capture(trial, "SV seed {}".format(trial["seed"]))
        if trial["status"] == "passed" and (
            type(trial.get("iterations")) is not int
            or not 1 <= trial["iterations"] <= SV_MAX_ITERATIONS
        ):
            raise ValidationError("passed SV trial has an invalid iteration count")
        if trial["status"] == "passed" and (
            (trial.get("result") or {}).get("mode") != "sv"
            or (trial.get("result") or {}).get("seed") != trial["seed"]
            or (trial.get("result") or {}).get("iterations") != trial["iterations"]
        ):
            raise ValidationError("SV derived fields differ from captured worker result")
        if record["platform"] == "metadrive" and trial["status"] == "passed":
            result = trial.get("result") or {}
            if (
                result.get("sampling_semantics")
                != "deterministic_artifact_confirmation"
                or result.get("validation_repetition") != trial["seed"]
                or type(result.get("map_seed")) is not int
                or result["map_seed"] < 0
            ):
                raise ValidationError(
                    "MetaDrive SV must identify a deterministic artifact confirmation"
                )
    _validate_stage_capture(record["ne"], "NE")
    provenance = record["provenance"]
    worker_config = provenance["worker_config"]
    expected_worker_config_keys = {
        "interpreter",
        "interpreter_sha256",
        "interpreter_bytes",
        "worker",
        "worker_sha256",
        "worker_bytes",
        "timeout_seconds",
        "environment",
        "platform_context",
        "runtime_dependencies",
    }
    if (
        set(worker_config) != expected_worker_config_keys
        or record_sha256(worker_config) != provenance["worker_config_sha256"]
    ):
        raise ValidationError("runtime worker configuration binding changed")
    _worker_environment(worker_config["environment"])
    worker_path = Path(provenance["worker_path"]).resolve()
    if (
        not worker_path.is_file()
        or worker_path != Path(worker_config["worker"]).resolve()
        or sha256_file(worker_path) != provenance["worker_sha256"]
    ):
        raise ValidationError("runtime worker source binding changed")
    interpreter_path = Path(provenance["interpreter_path"]).resolve()
    if (
        not interpreter_path.is_file()
        or interpreter_path != Path(worker_config["interpreter"]).resolve()
        or sha256_file(interpreter_path) != provenance["interpreter_sha256"]
    ):
        raise ValidationError("runtime interpreter binding changed")
    platform_context = worker_config["platform_context"]
    if record["platform"] == "carla":
        if set(platform_context) != {"carla_endpoint", "server_binary"}:
            raise ValidationError("CARLA runtime platform binding is incomplete")
        endpoint = platform_context["carla_endpoint"]
        server = _validate_carla_server_binary(platform_context["server_binary"])
        if (
            endpoint.get("host") != "127.0.0.1"
            or type(endpoint.get("port")) is not int
            or type(endpoint.get("timeout_seconds")) not in (int, float)
        ):
            raise ValidationError("CARLA runtime platform binding changed")
        dependencies = worker_config["runtime_dependencies"]
        if not isinstance(dependencies, Mapping) or set(dependencies) != {
            "environment_manifest",
            "python_path_entries",
            "source_trees",
            "map_catalog",
            "verification_status",
        }:
            raise ValidationError("CARLA runtime dependency binding is malformed")
        if dependencies.get("verification_status") not in {
            "draft_bound",
            "formal_verified",
        }:
            raise ValidationError("CARLA runtime dependency verification is unavailable")
        _validate_runtime_environment_manifest(
            dependencies.get("environment_manifest"),
            interpreter_path,
            formal=False,
        )
        python_entries = [
            _exact_file_binding(binding, "recorded CARLA PYTHONPATH entry")
            for binding in dependencies.get("python_path_entries", [])
        ]
        environment_paths = [
            str(Path(part).resolve())
            for key in sorted(_CONTENT_BEARING_ENVIRONMENT_KEYS)
            for part in worker_config["environment"].get(key, "").split(os.pathsep)
            if part
        ]
        if sorted(environment_paths) != sorted(
            binding["path"] for binding in python_entries
        ):
            raise ValidationError("recorded CARLA runtime environment is not content-bound")
        trees = [
            _validate_runtime_tree_binding(
                binding,
                "recorded CARLA runtime source tree",
                verify_bytes=False,
            )
            for binding in dependencies.get("source_trees", [])
        ]
        if {binding["role"] for binding in trees} != _CARLA_RUNTIME_TREE_ROLES:
            raise ValidationError("recorded CARLA runtime source trees are incomplete")
        tree_by_role = {binding["role"]: binding for binding in trees}
        try:
            server_relative = Path(server["path"]).relative_to(
                Path(tree_by_role["carla_server_distribution"]["path"])
            )
        except ValueError as exc:
            raise ValidationError(
                "recorded CARLA server binary escapes its distribution tree"
            ) from exc
        if server_relative != _CARLA_SERVER_RELATIVE_PATH:
            raise ValidationError("recorded CARLA server binary is not the shipping leaf")
        catalog = [
            _exact_file_binding(binding, "recorded CARLA map")
            for binding in dependencies.get("map_catalog", [])
        ]
        if not catalog:
            raise ValidationError("recorded CARLA map catalog is empty")
    else:
        if set(platform_context) != {"metadrive_token_registry"}:
            raise ValidationError("MetaDrive runtime platform binding is incomplete")
        registry = platform_context["metadrive_token_registry"]
        registry_path = Path(str(registry.get("path", ""))).resolve()
        if (
            registry_path.is_symlink()
            or not registry_path.is_file()
            or registry.get("sha256") != sha256_file(registry_path)
            or registry.get("bytes") != registry_path.stat().st_size
        ):
            raise ValidationError("MetaDrive runtime token registry binding changed")
        _revalidate_normalized_metadrive_runtime_dependencies(worker_config)
    controller = provenance["controller_config"]
    _validate_controller_config(
        controller, record["platform"], allow_draft=allow_draft_controller
    )
    if (
        record_sha256(controller) != record["ne"]["controller_config_sha256"]
        or controller["implementation"]["source_sha256"]
        != provenance["controller_source_sha256"]
        or controller["controller_id"] != record["ne"]["controller_id"]
    ):
        raise ValidationError("runtime controller configuration binding changed")
    attempt_path = Path(provenance["attempt_path"]).resolve()
    if not attempt_path.is_dir():
        raise ValidationError("runtime attempt path is missing")
    descriptors = []
    for stage in [record["compile"], *trials, record["ne"]]:
        descriptors.extend(stage.get(key) for key in ("request", "stdout", "stderr"))
    descriptors.append(record["ne"].get("trace"))
    descriptors.append(record["ne"].get("server_session"))
    for descriptor in (value for value in descriptors if value is not None):
        try:
            Path(descriptor["path"]).resolve().relative_to(attempt_path)
        except ValueError as exc:
            raise ValidationError("runtime capture escapes its immutable attempt") from exc

    passed_seeds = [trial["seed"] for trial in trials if trial["status"] == "passed"]
    expected_selected = min(passed_seeds) if passed_seeds else None
    if record.get("selected_seed") != expected_selected:
        raise ValidationError("runtime selected_seed is not the lowest passing SV seed")
    if record["ne"].get("selected_seed") != expected_selected:
        raise ValidationError("runtime NE seed differs from selected_seed")

    server_session = None
    if record["platform"] == "carla":
        if record["ne"]["status"] == "skipped":
            if record["ne"].get("server_session") is not None:
                raise ValidationError("skipped CARLA NE cannot claim a server session")
        else:
            descriptor = record["ne"].get("server_session")
            if descriptor is None:
                raise ValidationError("attempted CARLA NE lacks server-session evidence")
            server_session = _validate_carla_server_session(
                descriptor,
                platform_context["carla_endpoint"],
                platform_context["server_binary"],
            )
            if (
                record["ne"]["status"] == "passed"
                and server_session["verification_status"] != "verified"
            ):
                raise ValidationError("passed CARLA NE lacks a stable server process")
    elif "server_session" in record["ne"]:
        raise ValidationError("MetaDrive NE cannot claim a CARLA server session")

    unsupported = record.get("expected_support") == "unsupported"
    if unsupported:
        if record.get("excluded_reason") != "unsupported_query_outside_runtime_metrics":
            raise ValidationError("unsupported runtime record has the wrong exclusion")
        if (
            record["compile"]["status"] != "skipped"
            or any(trial["status"] != "skipped" for trial in trials)
            or record["ne"]["status"] != "skipped"
            or expected_selected is not None
        ):
            raise ValidationError("unsupported query must remain outside all runtime stages")
        return record

    if record.get("excluded_reason") == "no_complete_finalized_artifact":
        if (
            record["compile"]["status"] != "skipped"
            or any(trial["status"] != "skipped" for trial in trials)
            or record["ne"]["status"] != "skipped"
        ):
            raise ValidationError("supported generation failure must contribute stage zeros")
        return record
    if record.get("excluded_reason") is not None:
        raise ValidationError("supported runtime record has an unknown exclusion reason")
    if record.get("source_artifact_sha256") is None:
        raise ValidationError("runtime-eligible supported record lacks an artifact hash")

    common_request_keys = {
        "schema_version",
        "platform",
        "artifact_path",
        "artifact_sha256",
        "source_response_sha256",
        "source_config_sha256",
        "source_generation_manifest_sha256",
        "source_implementation_bundle_sha256",
        "generation_chain_verified",
        "runtime_executor",
        "runtime_environment",
    }
    if record["platform"] == "carla":
        common_request_keys.update(
            {"carla_endpoint", "carla_server_binary", "carla_map_context"}
        )
    else:
        common_request_keys.update(
            {"metadrive_token_registry", "metadrive_runtime_context"}
        )

    def valid_common_request(request: Mapping[str, Any]) -> bool:
        executor = request.get("runtime_executor")
        if not isinstance(executor, Mapping) or set(executor) != {
            "interpreter_path",
            "interpreter_sha256",
            "interpreter_bytes",
            "worker_path",
            "worker_sha256",
            "worker_bytes",
        }:
            return False
        if any(
            (
                request.get("schema_version") != SCHEMA_VERSION,
                request.get("platform") != record.get("platform"),
                request.get("artifact_sha256")
                != record.get("source_artifact_sha256"),
                request.get("source_response_sha256")
                != record.get("source_response_sha256"),
                request.get("source_config_sha256")
                != record.get("source_config_sha256"),
                request.get("source_generation_manifest_sha256")
                != record.get("source_generation_manifest_sha256"),
                request.get("source_implementation_bundle_sha256")
                != record.get("source_implementation_bundle_sha256"),
                request.get("generation_chain_verified")
                is not record.get("generation_chain_verified"),
                executor.get("interpreter_path") != provenance["interpreter_path"],
                executor.get("interpreter_sha256")
                != provenance["interpreter_sha256"],
                executor.get("interpreter_bytes")
                != worker_config["interpreter_bytes"],
                executor.get("worker_path") != provenance["worker_path"],
                executor.get("worker_sha256") != provenance["worker_sha256"],
                executor.get("worker_bytes") != worker_config["worker_bytes"],
            )
        ):
            return False
        expected_runtime_environment = {
            "values": worker_config["environment"],
            "bindings": worker_config.get("runtime_dependencies", {}).get(
                "python_path_entries", []
            ),
        }
        if request.get("runtime_environment") != expected_runtime_environment:
            return False
        if record["platform"] == "carla":
            map_context = request.get("carla_map_context")
            if (
                request.get("carla_endpoint") != platform_context["carla_endpoint"]
                or request.get("carla_server_binary") != platform_context["server_binary"]
                or not isinstance(map_context, Mapping)
                or set(map_context) != {
                    "declared_town",
                    "source_artifact",
                    "staged_artifact",
                    "source_catalog",
                    "staged_catalog",
                }
                or re.fullmatch(
                    r"Town(?:0[1-7]|10HD)", str(map_context.get("declared_town", ""))
                )
                is None
                or not isinstance(map_context.get("source_catalog"), list)
            ):
                return False
            try:
                source_artifact = _exact_file_binding(
                    map_context["source_artifact"], "source CARLA artifact"
                )
                staged_artifact = _exact_file_binding(
                    map_context["staged_artifact"], "staged CARLA artifact"
                )
                Path(staged_artifact["path"]).relative_to(
                    attempt_path / "native-input" / "dynamic_scenario"
                )
            except (ValidationError, ValueError):
                return False
            if (
                request.get("artifact_path") != staged_artifact["path"]
                or source_artifact["sha256"] != record.get("source_artifact_sha256")
                or (source_artifact["sha256"], source_artifact["bytes"])
                != (staged_artifact["sha256"], staged_artifact["bytes"])
                or os.path.samefile(source_artifact["path"], staged_artifact["path"])
            ):
                return False
            frozen_catalog = {
                Path(binding["path"]).name: binding
                for binding in worker_config["runtime_dependencies"]["map_catalog"]
            }
            if (
                not map_context["source_catalog"]
                or any(
                    frozen_catalog.get(Path(binding.get("path", "")).name) != binding
                    for binding in map_context["source_catalog"]
                    if isinstance(binding, Mapping)
                )
                or any(
                    not isinstance(binding, Mapping)
                    for binding in map_context["source_catalog"]
                )
                or sum(
                    Path(binding["path"]).suffix == ".xodr"
                    for binding in map_context["source_catalog"]
                )
                != 1
            ):
                return False
            source_by_name = {
                Path(binding["path"]).name: binding
                for binding in map_context["source_catalog"]
            }
            town = map_context["declared_town"]
            if (
                set(source_by_name) - {"{}.xodr".format(town), "{}.snet".format(town)}
                or "{}.xodr".format(town) not in source_by_name
            ):
                return False
            staged = map_context.get("staged_catalog")
            if not isinstance(staged, list) or len(staged) != len(source_by_name):
                return False
            for binding in staged:
                try:
                    normalized = _exact_file_binding(binding, "staged CARLA map")
                    Path(normalized["path"]).resolve().relative_to(
                        attempt_path / "native-input" / "maps"
                    )
                except (ValidationError, ValueError):
                    return False
                source = source_by_name.get(Path(normalized["path"]).name)
                if source is None or (
                    normalized["sha256"], normalized["bytes"]
                ) != (source["sha256"], source["bytes"]) or os.path.samefile(
                    source["path"], normalized["path"]
                ):
                    return False
            return True
        return (
            "carla_endpoint" not in request
            and request.get("metadrive_token_registry")
            == platform_context["metadrive_token_registry"]
            and request.get("metadrive_runtime_context")
            == {
                "metadrive_module": worker_config["runtime_dependencies"][
                    "metadrive_module"
                ],
                "source_tree": worker_config["runtime_dependencies"]["source_tree"],
            }
        )

    compile_request = _runtime_capture_json(record["compile"]["request"], "compile request")
    if (
        set(compile_request) != common_request_keys | {"mode"}
        or not valid_common_request(compile_request)
        or compile_request.get("mode") != "compile"
    ):
        raise ValidationError("compile request violates the query-blind runtime contract")
    if (
        record["compile"]["status"] == "passed"
        and (record["compile"].get("result") or {}).get("mode") != "compile"
    ):
        raise ValidationError("compile result differs from the requested stage")
    if record["platform"] == "carla" and record["compile"]["status"] == "passed":
        static_opendrive = (record["compile"].get("result") or {}).get(
            "static_opendrive"
        )
        try:
            normalized_map = _exact_file_binding(
                static_opendrive, "compiled CARLA static OpenDRIVE"
            )
        except ValidationError as exc:
            raise ValidationError(
                "compiled CARLA result lacks its static OpenDRIVE binding"
            ) from exc
        staged_maps = {
            str(Path(binding["path"]).resolve()): binding
            for binding in compile_request["carla_map_context"]["staged_catalog"]
        }
        if staged_maps.get(normalized_map["path"]) != normalized_map:
            raise ValidationError(
                "compiled CARLA static OpenDRIVE differs from staged provenance"
            )

    if record["compile"]["status"] != "passed":
        if any(trial["status"] != "skipped" for trial in trials):
            raise ValidationError("SV cannot run after compile failure")
    elif any(trial["status"] == "skipped" for trial in trials):
        raise ValidationError("compiled artifact must receive all five SV trials")
    if record["compile"]["status"] == "passed":
        for trial in trials:
            request = _runtime_capture_json(
                trial["request"], "SV seed {} request".format(trial["seed"])
            )
            if set(request) != common_request_keys | {
                "mode",
                "seed",
                "max_iterations",
            } or not valid_common_request(request) or any(
                (
                    request.get("artifact_path") != compile_request.get("artifact_path"),
                    request.get("mode") != "sv",
                    request.get("seed") != trial["seed"],
                    request.get("max_iterations") != SV_MAX_ITERATIONS,
                )
            ):
                raise ValidationError("SV request violates the fixed seed/budget contract")
    if expected_selected is None and record["ne"]["status"] != "skipped":
        raise ValidationError("NE cannot run without a passing SV seed")
    if expected_selected is not None and record["ne"]["status"] == "skipped":
        raise ValidationError("NE must be attempted at the lowest passing SV seed")
    if expected_selected is not None:
        request = _runtime_capture_json(record["ne"]["request"], "NE request")
        expected_ne_keys = common_request_keys | {
            "mode",
            "seed",
            "rollout_seconds",
            "controller",
            "trace_path",
        }
        if record["platform"] == "carla":
            expected_ne_keys.add("carla_server_preflight")
        if set(request) != expected_ne_keys or not valid_common_request(request) or any(
            (
                request.get("artifact_path") != compile_request.get("artifact_path"),
                request.get("mode") != "ne",
                request.get("seed") != expected_selected,
                request.get("rollout_seconds") != ROLLOUT_SECONDS,
                record_sha256(request.get("controller"))
                != record["ne"]["controller_config_sha256"],
            )
        ):
            raise ValidationError("NE request violates the fixed rollout/controller contract")
        if record["platform"] == "carla":
            _validate_carla_preflight_document(
                request.get("carla_server_preflight"),
                platform_context["carla_endpoint"],
                platform_context["server_binary"],
            )
            if (
                server_session is None
                or canonical_json_bytes(request["carla_server_preflight"])
                != canonical_json_bytes(server_session["preflight"])
            ):
                raise ValidationError(
                    "CARLA NE request differs from server-session preflight evidence"
                )
        request_controller = request["controller"]
        _validate_controller_config(
            request_controller,
            record["platform"],
            allow_draft=allow_draft_controller,
        )
        if (
            canonical_json_bytes(request_controller) != canonical_json_bytes(controller)
            or request_controller["controller_id"] != record["ne"]["controller_id"]
            or request_controller["implementation"]["source_sha256"]
            != record["provenance"]["controller_source_sha256"]
        ):
            raise ValidationError("NE controller identity differs from runtime provenance")
    if record["ne"]["status"] == "passed":
        if (
            record["ne"].get("steps", 0) < 1
            or record["ne"].get("simulated_seconds", 0.0) <= 0.0
            or record["ne"].get("trace") is None
            or not record["ne"].get("runtime_controller_class")
            or not record["ne"].get("termination_reason")
        ):
            raise ValidationError("passed NE must prove advancement, trace, controller, and termination")
        result = record["ne"].get("result") or {}
        if (
            result.get("mode") != "ne"
            or result.get("steps") != record["ne"]["steps"]
            or result.get("simulated_seconds") != record["ne"]["simulated_seconds"]
            or result.get("runtime_controller_class")
            != record["ne"]["runtime_controller_class"]
            or result.get("termination_reason") != record["ne"]["termination_reason"]
            or record["ne"]["runtime_controller_class"]
            != controller["implementation"]["class"]
        ):
            raise ValidationError("NE derived fields differ from captured worker result/config")
        if record["platform"] == "carla":
            native_runtime = result.get("native_runtime")
            if not isinstance(native_runtime, Mapping) or set(native_runtime) != {
                "client_version",
                "server_version",
                "world_map_name",
                "declared_town",
                "runtime_opendrive",
                "staged_opendrive",
            }:
                raise ValidationError("CARLA NE lacks native server/map identity evidence")
            town = request["carla_map_context"]["declared_town"]
            staged_xodr = next(
                (
                    binding
                    for binding in request["carla_map_context"]["staged_catalog"]
                    if Path(binding["path"]).name == "{}.xodr".format(town)
                ),
                None,
            )
            runtime_opendrive = native_runtime.get("runtime_opendrive")
            if (
                not isinstance(native_runtime.get("client_version"), str)
                or not native_runtime["client_version"]
                or native_runtime.get("server_version")
                != native_runtime["client_version"]
                or native_runtime.get("declared_town") != town
                or Path(str(native_runtime.get("world_map_name", ""))).name != town
                or staged_xodr is None
                or native_runtime.get("staged_opendrive") != staged_xodr
                or not isinstance(runtime_opendrive, Mapping)
                or set(runtime_opendrive) != {"sha256", "bytes"}
                or runtime_opendrive.get("sha256") != staged_xodr["sha256"]
                or runtime_opendrive.get("bytes") != staged_xodr["bytes"]
            ):
                raise ValidationError(
                    "CARLA NE runtime map/version evidence differs from staged provenance"
                )
        if _contains_forbidden_runtime_metadata(result):
            raise ValidationError("NE result contains query or evaluation metadata")
        _verify_runtime_capture(record["ne"]["trace"])
        _validate_runtime_trace(record, request)
    if canonical_json_bytes(record.get("iec_exec")) != canonical_json_bytes(
        _iec_exec_diagnostic(record["ne"])
    ):
        raise ValidationError("IEC_exec diagnostic overstates available runtime evidence")
    return record
