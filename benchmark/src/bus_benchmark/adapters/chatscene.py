"""Subprocess adapters for fake-command tests and legacy ChatScene generation."""

import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from bus_benchmark.errors import AdapterError, ValidationError
from bus_benchmark.jsonio import read_json, sha256_file, strict_json_object_bytes

from . import AdapterOutcome, MethodAdapter


_SUPPORTED_DISPOSITIONS = {
    "generate",
    "reject",
    "clarification",
    "controlled_degradation",
}
_UNSUPPORTED_DISPOSITIONS = {
    "reject",
    "clarification",
    "controlled_degradation",
}
_DEFAULT_ENVIRONMENT = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LD_LIBRARY_PATH",
    "CUDA_VISIBLE_DEVICES",
)
_ALLOWED_ENVIRONMENT = frozenset(
    {
        *_DEFAULT_ENVIRONMENT,
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
)
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FORBIDDEN_SNAPSHOT_ROOTS = {
    ".agents",
    ".codex",
    ".git",
    "benchmark_artifacts",
    "benchmark_configs",
    "query_lib",
    "refine-logs",
    "tests",
}
def _path_has_symlink_component(path: Path) -> bool:
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _exact_file_binding(binding: Any, label: str) -> Tuple[Path, Dict[str, Any]]:
    if not isinstance(binding, Mapping) or set(binding) != {
        "path",
        "sha256",
        "bytes",
    }:
        raise ValidationError("{} must be an exact file binding".format(label))
    path_value = binding.get("path")
    digest = binding.get("sha256")
    size = binding.get("bytes")
    path = Path(str(path_value))
    if (
        not isinstance(path_value, str)
        or not path.is_absolute()
        or _path_has_symlink_component(path)
        or not path.is_file()
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or type(size) is not int
        or size < 0
        or path.stat().st_size != size
        or sha256_file(path) != digest
    ):
        raise ValidationError("{} differs from its bound bytes".format(label))
    normalized = {"path": str(path.resolve()), "sha256": digest, "bytes": size}
    return path.resolve(), normalized


def _python_environment_control_paths(environment_root: Path) -> Tuple[Path, ...]:
    # Import lazily to avoid the adapter/generation module import cycle while
    # keeping one authoritative startup-control discovery implementation.
    from bus_benchmark.generation import python_environment_control_paths

    return python_environment_control_paths(environment_root)


def _snapshot_exact_bound_file(
    binding: Mapping[str, Any], destination: Path, label: str
) -> Path:
    """Copy one exact binding through an opened descriptor into a private run.

    The copied bytes, rather than the mutable source pathname, are mounted into
    the sandbox.  Replacing the source after adapter construction therefore
    either fails this attestation or cannot affect the launched process.
    """

    source, normalized = _exact_file_binding(binding, label)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(source), flags)
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != normalized["bytes"]:
            raise ValidationError("{} changed before snapshot".format(label))
        with destination.open("xb") as output:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        live = os.stat(str(source), follow_symlinks=False)
    except OSError as exc:
        raise ValidationError("{} changed during snapshot".format(label)) from exc
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or (after.st_dev, after.st_ino) != (live.st_dev, live.st_ino)
        or digest.hexdigest() != normalized["sha256"]
        or destination.stat().st_size != normalized["bytes"]
        or sha256_file(destination) != normalized["sha256"]
    ):
        raise ValidationError("{} changed during snapshot".format(label))
    destination.chmod(0o444)
    return destination.resolve()


def _attest_private_snapshot(
    path: Path, binding: Mapping[str, Any], label: str
) -> None:
    path = Path(path)
    if (
        _path_has_symlink_component(path)
        or not path.is_file()
        or path.stat().st_size != binding["bytes"]
        or sha256_file(path) != binding["sha256"]
    ):
        raise ValidationError("{} private snapshot changed before launch".format(label))


def _regular_file_stability_identity(path: Path, label: str) -> Tuple[int, ...]:
    path = Path(path)
    if _path_has_symlink_component(path) or not path.is_file():
        raise ValidationError("{} is not a regular private snapshot".format(label))
    observed = path.stat()
    if not stat.S_ISREG(observed.st_mode):
        raise ValidationError("{} is not a regular private snapshot".format(label))
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _directory_stability_identity(path: Path, label: str) -> Tuple[int, ...]:
    path = Path(path)
    if _path_has_symlink_component(path) or not path.is_dir():
        raise ValidationError("{} is not a regular private directory".format(label))
    observed = path.stat()
    if not stat.S_ISDIR(observed.st_mode):
        raise ValidationError("{} is not a regular private directory".format(label))
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _positive_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValidationError("adapter timeout_seconds must be a positive number")
    return float(value)


def _positive_size(value: Any, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValidationError("{} must be an integer in [1, {}]".format(name, maximum))
    return value


def _string_sequence(value: Any, name: str, allow_empty: bool = False) -> Tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValidationError("{} must be a {}string array".format(name, "possibly empty " if allow_empty else "non-empty "))
    if any(not isinstance(item, str) or not item for item in value):
        raise ValidationError("{} entries must be non-empty strings".format(name))
    return tuple(value)


def _inside(root: Path, relative: str, name: str) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise ValidationError("{} must be a non-empty relative path".format(name))
    root = Path(root).resolve()
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValidationError("{} contains a symlink component".format(name))
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValidationError("{} escapes its isolated root".format(name)) from exc
    return candidate


def _subprocess_environment(
    names: Iterable[str],
    home: Optional[Path] = None,
    isolated_pythonpath: Optional[Path] = None,
) -> Dict[str, str]:
    selected = []
    for name in names:
        if not isinstance(name, str) or not _ENVIRONMENT_NAME.fullmatch(name):
            raise ValidationError("environment_passthrough contains an invalid variable name")
        if name not in _ALLOWED_ENVIRONMENT:
            raise ValidationError(
                "environment_passthrough contains a non-whitelisted variable: {}".format(
                    name
                )
            )
        if name not in selected:
            selected.append(name)
    environment = {name: os.environ[name] for name in selected if name in os.environ}
    if home is not None:
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        environment["HOME"] = str(home.resolve())
        environment["XDG_CACHE_HOME"] = str((home / ".cache").resolve())
        environment["MPLCONFIGDIR"] = str((home / ".matplotlib").resolve())
    if isolated_pythonpath is not None:
        isolated_pythonpath = Path(isolated_pythonpath).resolve()
        if not isolated_pythonpath.is_dir():
            raise ValidationError("isolated Python import root must be a directory")
        environment["PYTHONPATH"] = str(isolated_pythonpath)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
    # The group may still contain grandchildren after its leader exits.
    # Always send the final signal to the original process group ID.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _run_process(
    argv: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
    environment_passthrough: Sequence[str],
    stdin: Optional[bytes],
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    isolated_pythonpath: Optional[Path] = None,
    environment_overrides: Optional[Mapping[str, str]] = None,
) -> AdapterOutcome:
    environment = _subprocess_environment(
        environment_passthrough,
        cwd / ".adapter-home",
        isolated_pythonpath=isolated_pythonpath,
    )
    if environment_overrides is not None:
        allowed_overrides = {
            "HOME",
            "XDG_CACHE_HOME",
            "MPLCONFIGDIR",
            "PYTHONPATH",
            "PYTHON_EGG_CACHE",
        }
        if (
            set(environment_overrides) - allowed_overrides
            or any(
                not isinstance(value, str) or not value
                for value in environment_overrides.values()
            )
        ):
            raise ValidationError("adapter internal environment override is invalid")
        environment.update(environment_overrides)
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        message = "adapter launch failed: {}".format(exc)
        return AdapterOutcome(b"", message.encode("utf-8", errors="replace"), None, False, error=message)

    captured = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = []
    overflow_lock = threading.Lock()

    def read_stream(name: str, stream: Any, limit: int) -> None:
        total = 0
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                total += len(chunk)
                remaining = limit - len(captured[name])
                if remaining > 0:
                    captured[name].extend(chunk[:remaining])
                if total > limit:
                    with overflow_lock:
                        if not overflow:
                            overflow.append(name)
        finally:
            stream.close()

    readers = (
        threading.Thread(
            target=read_stream,
            args=("stdout", process.stdout, max_stdout_bytes),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=("stderr", process.stderr, max_stderr_bytes),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    if stdin is not None and process.stdin is not None:
        try:
            process.stdin.write(stdin)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while process.poll() is None:
        if overflow:
            _terminate_process_group(process)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process_group(process)
            break
        try:
            process.wait(timeout=min(0.05, remaining))
        except subprocess.TimeoutExpired:
            pass
    if not timed_out and not overflow:
        # A completed method is not allowed to leave detached group members
        # writing files after evidence capture.
        _terminate_process_group(process)
    for reader in readers:
        reader.join(timeout=2.0)
    if any(reader.is_alive() for reader in readers):
        return AdapterOutcome(
            bytes(captured["stdout"]),
            bytes(captured["stderr"]),
            process.returncode,
            False,
            error="method process streams did not close after group termination",
        )
    stdout = bytes(captured["stdout"])
    stderr = bytes(captured["stderr"])
    if timed_out:
        return AdapterOutcome(
            stdout,
            stderr,
            None,
            True,
            error="method invocation exceeded {:.3f} seconds".format(timeout_seconds),
        )
    if overflow:
        return AdapterOutcome(
            stdout,
            stderr,
            process.returncode,
            False,
            error="method {} exceeded its configured byte limit".format(overflow[0]),
        )
    return AdapterOutcome(stdout, stderr, process.returncode, False)


def _artifact_if_present(
    root: Path, relative: Optional[str], name: str, max_bytes: int
) -> Optional[Path]:
    if relative is None:
        return None
    path = _inside(root, relative, name)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise AdapterError("{} must be a regular non-symlink file".format(name))
    if path.stat().st_size > max_bytes:
        raise AdapterError("{} exceeds its configured byte limit".format(name))
    return path


def _with_artifact_protocol(
    process: AdapterOutcome,
    artifact: Optional[Path],
) -> AdapterOutcome:
    if process.timed_out or process.error or process.exit_code != 0:
        error = process.error
        if error is None:
            error = "method process exited with status {}".format(process.exit_code)
        return AdapterOutcome(
            process.stdout,
            process.stderr,
            process.exit_code,
            process.timed_out,
            artifact_path=artifact,
            error=error,
        )
    if artifact is None:
        return AdapterOutcome(
            process.stdout,
            process.stderr,
            process.exit_code,
            False,
            error="successful method process produced no configured artifact",
        )
    return AdapterOutcome(
        process.stdout,
        process.stderr,
        process.exit_code,
        False,
        disposition="generate",
        artifact_path=artifact,
    )


def _with_stdout_json_protocol(
    process: AdapterOutcome,
    artifact: Optional[Path],
) -> AdapterOutcome:
    if process.timed_out or process.error or process.exit_code != 0:
        return _with_artifact_protocol(process, artifact)
    try:
        document = strict_json_object_bytes(
            process.stdout, "stdout_json_v0.1"
        )
    except ValidationError as exc:
        return AdapterOutcome(
            process.stdout,
            process.stderr,
            process.exit_code,
            False,
            artifact_path=artifact,
            error=str(exc),
        )
    if not isinstance(document, dict) or set(document) - {"disposition", "message"}:
        return AdapterOutcome(
            process.stdout,
            process.stderr,
            process.exit_code,
            False,
            artifact_path=artifact,
            error="stdout_json_v0.1 must contain only disposition and optional message",
        )
    disposition = document.get("disposition")
    if disposition not in _SUPPORTED_DISPOSITIONS:
        return AdapterOutcome(
            process.stdout,
            process.stderr,
            process.exit_code,
            False,
            artifact_path=artifact,
            error="stdout_json_v0.1 contains an invalid disposition",
        )
    message = document.get("message")
    if message is not None and (not isinstance(message, str) or not message.strip()):
        return AdapterOutcome(
            process.stdout,
            process.stderr,
            process.exit_code,
            False,
            artifact_path=artifact,
            error="stdout_json_v0.1 message must be a non-empty string",
        )
    needs_artifact = disposition in {"generate", "controlled_degradation"}
    if needs_artifact != (artifact is not None):
        return AdapterOutcome(
            process.stdout,
            process.stderr,
            process.exit_code,
            False,
            artifact_path=artifact,
            error="{} disposition has an invalid artifact presence".format(disposition),
        )
    return AdapterOutcome(
        process.stdout,
        process.stderr,
        process.exit_code,
        False,
        disposition=disposition,
        artifact_path=artifact,
        unsupported_response_stream="stdout"
        if disposition in _UNSUPPORTED_DISPOSITIONS
        else None,
    )


class CommandAdapter(MethodAdapter):
    """Run a static, shell-free command with the query bytes on standard input."""

    def __init__(
        self,
        argv: Sequence[str],
        artifact_path: Optional[str],
        timeout_seconds: float,
        output_protocol: str = "artifact_on_zero",
        environment_passthrough: Sequence[str] = _DEFAULT_ENVIRONMENT,
        max_stdout_bytes: int = 1048576,
        max_stderr_bytes: int = 1048576,
        max_artifact_bytes: int = 16777216,
    ) -> None:
        self.argv = tuple(argv)
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValidationError("command adapter argv must contain non-empty strings")
        self.artifact_path = artifact_path
        if artifact_path is not None:
            _inside(Path.cwd(), artifact_path, "artifact_path")
        self.timeout_seconds = _positive_timeout(timeout_seconds)
        if output_protocol not in ("artifact_on_zero", "stdout_json_v0.1"):
            raise ValidationError("command adapter output_protocol is invalid")
        self.output_protocol = output_protocol
        self.environment_passthrough = tuple(environment_passthrough)
        _subprocess_environment(self.environment_passthrough)
        self.max_stdout_bytes = _positive_size(
            max_stdout_bytes, "adapter.max_stdout_bytes", 16777216
        )
        self.max_stderr_bytes = _positive_size(
            max_stderr_bytes, "adapter.max_stderr_bytes", 16777216
        )
        self.max_artifact_bytes = _positive_size(
            max_artifact_bytes, "adapter.max_artifact_bytes", 67108864
        )

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "CommandAdapter":
        artifact_path = config.get("artifact_path")
        if artifact_path is not None and not isinstance(artifact_path, str):
            raise ValidationError("adapter.artifact_path must be a relative path or null")
        return cls(
            argv=_string_sequence(config.get("argv"), "adapter.argv"),
            artifact_path=artifact_path,
            timeout_seconds=config.get("timeout_seconds"),
            output_protocol=config.get("output_protocol", "artifact_on_zero"),
            environment_passthrough=_string_sequence(
                config.get("environment_passthrough", list(_DEFAULT_ENVIRONMENT)),
                "adapter.environment_passthrough",
                allow_empty=True,
            ),
            max_stdout_bytes=config.get("max_stdout_bytes"),
            max_stderr_bytes=config.get("max_stderr_bytes"),
            max_artifact_bytes=config.get("max_artifact_bytes"),
        )

    def generate(self, query_text: str, workdir: Path) -> AdapterOutcome:
        if not isinstance(query_text, str):
            raise ValidationError("query_text must be a string")
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=False)
        process = _run_process(
            self.argv,
            workdir,
            self.timeout_seconds,
            self.environment_passthrough,
            query_text.encode("utf-8"),
            self.max_stdout_bytes,
            self.max_stderr_bytes,
        )
        try:
            artifact = _artifact_if_present(
                workdir,
                self.artifact_path,
                "artifact_path",
                self.max_artifact_bytes,
            )
        except AdapterError as exc:
            return AdapterOutcome(
                process.stdout,
                process.stderr,
                process.exit_code,
                process.timed_out,
                error=str(exc),
            )
        if self.output_protocol == "stdout_json_v0.1":
            return _with_stdout_json_protocol(process, artifact)
        return _with_artifact_protocol(process, artifact)


def _copy_snapshot(source_root: Path, destination: Path, paths: Sequence[str]) -> None:
    source_root = Path(source_root).resolve()
    try:
        destination.resolve().relative_to(source_root)
    except ValueError:
        pass
    else:
        raise AdapterError("isolated snapshot destination cannot be inside source_root")
    destination.mkdir(parents=True, exist_ok=False)
    ignored = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache")

    def reject_symlinks(source: Path) -> None:
        if source.is_symlink():
            raise AdapterError("snapshot source contains a symlink: {}".format(source))
        if not source.is_dir():
            return
        for current_root, directories, filenames in os.walk(str(source), followlinks=False):
            current = Path(current_root)
            for name in (*directories, *filenames):
                candidate = current / name
                if candidate.is_symlink():
                    raise AdapterError(
                        "snapshot source contains a symlink: {}".format(candidate)
                    )

    for relative in paths:
        first_part = Path(relative).parts[0]
        if relative == "." or first_part in _FORBIDDEN_SNAPSHOT_ROOTS:
            raise AdapterError(
                "snapshot_paths must explicitly exclude benchmark and query-library data"
            )
        unresolved_source = source_root / relative
        if unresolved_source.is_symlink():
            raise AdapterError("snapshot source contains a symlink: {}".format(relative))
        source = _inside(source_root, relative, "snapshot_paths entry")
        if not source.exists():
            raise AdapterError("snapshot source does not exist: {}".format(relative))
        reject_symlinks(source)
        target = destination if relative == "." else _inside(destination, relative, "snapshot target")
        if source.is_dir():
            if relative == ".":
                for child in source.iterdir():
                    if child.name in {".git", "__pycache__", ".pytest_cache"}:
                        continue
                    child_target = destination / child.name
                    if child.is_dir():
                        shutil.copytree(child, child_target, ignore=ignored, dirs_exist_ok=True)
                    else:
                        child_target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(child, child_target)
            else:
                shutil.copytree(source, target, ignore=ignored, dirs_exist_ok=True)
        elif source.is_file() and not source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            raise AdapterError("snapshot source must be a regular file or directory")


def _snapshot_binding_expectations(
    source_root: Path,
    snapshot_paths: Sequence[str],
    bindings: Sequence[Mapping[str, Any]],
) -> Dict[str, Tuple[str, int]]:
    """Normalize the exact bundle-bound file set expected in one snapshot."""

    source_root = Path(source_root).resolve()
    roots = tuple(
        _inside(source_root, relative, "snapshot_paths entry")
        for relative in snapshot_paths
    )
    expectations: Dict[str, Tuple[str, int]] = {}
    for index, binding in enumerate(bindings):
        if not isinstance(binding, Mapping) or set(binding) != {
            "path",
            "sha256",
            "bytes",
        }:
            raise ValidationError(
                "snapshot binding {} must be an exact file binding".format(index)
            )
        path_value = binding.get("path")
        digest = binding.get("sha256")
        size = binding.get("bytes")
        if not isinstance(path_value, str) or not Path(path_value).is_absolute():
            raise ValidationError("snapshot binding path must be absolute")
        source = Path(path_value)
        if source.is_symlink() or not source.is_file():
            raise ValidationError(
                "snapshot binding must reference a regular non-symlink file"
            )
        resolved = source.resolve()
        try:
            relative = resolved.relative_to(source_root)
        except ValueError as exc:
            raise ValidationError("snapshot binding escapes source_root") from exc
        inside_snapshot = False
        for root in roots:
            if resolved == root:
                inside_snapshot = True
                break
            if root.is_dir():
                try:
                    resolved.relative_to(root)
                except ValueError:
                    continue
                inside_snapshot = True
                break
        if not inside_snapshot:
            raise ValidationError("snapshot binding is outside snapshot_paths")
        relative_text = str(relative)
        if relative_text in expectations:
            raise ValidationError("snapshot bindings contain a duplicate path")
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(size) is not int
            or size < 0
        ):
            raise ValidationError("snapshot binding hash or byte count is invalid")
        expectations[relative_text] = (digest, size)
    if not expectations:
        raise ValidationError("formal snapshot bindings cannot be empty")
    return expectations


def _attest_snapshot(
    repository: Path, expectations: Mapping[str, Tuple[str, int]]
) -> None:
    """Fail closed unless copied files exactly match the frozen bundle."""

    repository = Path(repository).resolve()
    observed: Dict[str, Path] = {}
    for candidate in repository.rglob("*"):
        if candidate.is_symlink():
            raise AdapterError("copied snapshot contains a symlink")
        if not candidate.is_file():
            continue
        relative = str(candidate.relative_to(repository))
        observed[relative] = candidate
    expected_paths = set(expectations)
    observed_paths = set(observed)
    missing = sorted(expected_paths - observed_paths)
    extra = sorted(observed_paths - expected_paths)
    if missing:
        raise AdapterError(
            "copied snapshot attestation is missing a bundle-bound file: {}".format(
                missing[0]
            )
        )
    if extra:
        raise AdapterError(
            "copied snapshot attestation contains an unbound extra file: {}".format(
                extra[0]
            )
        )
    for relative in sorted(expected_paths):
        path = observed[relative]
        digest, size = expectations[relative]
        if path.stat().st_size != size or sha256_file(path) != digest:
            raise AdapterError(
                "copied snapshot attestation hash mismatch: {}".format(relative)
            )


class ChatSceneLegacyAdapter(MethodAdapter):
    """Run the unchanged ``retrieve/retrieve.py`` contract in a private snapshot."""

    def __init__(
        self,
        source_root: Path,
        snapshot_paths: Sequence[str],
        entrypoint: str,
        python_executable: str,
        sandbox_executable: str,
        argv: Sequence[str],
        artifact_path: str,
        failure_artifact_path: Optional[str],
        timeout_seconds: float,
        query_file: str = "retrieve/scenario_descriptions.txt",
        environment_passthrough: Sequence[str] = _DEFAULT_ENVIRONMENT,
        max_stdout_bytes: int = 1048576,
        max_stderr_bytes: int = 1048576,
        max_artifact_bytes: int = 16777216,
        snapshot_bindings: Optional[Sequence[Mapping[str, Any]]] = None,
        python_environment_root: Optional[str] = None,
        python_environment_control_bindings: Optional[
            Sequence[Mapping[str, Any]]
        ] = None,
        python_path_entries: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        source_root = Path(source_root)
        if source_root.is_symlink():
            raise ValidationError("legacy adapter source_root must not be a symlink")
        self.source_root = source_root.resolve()
        if not self.source_root.is_dir():
            raise ValidationError("legacy adapter source_root must be an existing directory")
        self.snapshot_paths = tuple(snapshot_paths)
        if not self.snapshot_paths:
            raise ValidationError("legacy adapter snapshot_paths cannot be empty")
        for relative in self.snapshot_paths:
            _inside(self.source_root, relative, "snapshot_paths entry")
        self.entrypoint = entrypoint
        self.python_executable = python_executable
        if python_environment_root is None:
            python_environment_root = str(Path(python_executable).parent.parent)
        environment_root = Path(str(python_environment_root))
        executable = Path(str(python_executable))
        if (
            not environment_root.is_absolute()
            or _path_has_symlink_component(environment_root)
            or not environment_root.is_dir()
        ):
            raise ValidationError(
                "legacy adapter python_environment_root must be an absolute regular directory"
            )
        if (
            not executable.is_absolute()
            or _path_has_symlink_component(executable)
            or not executable.is_file()
        ):
            raise ValidationError(
                "legacy adapter python_executable must be an absolute regular file"
            )
        environment_root = environment_root.resolve()
        executable = executable.resolve()
        try:
            executable.relative_to(environment_root / "bin")
        except ValueError as exc:
            raise ValidationError(
                "legacy adapter python_executable is outside python_environment_root/bin"
            ) from exc
        self.python_environment_root = environment_root
        self.python_executable = str(executable)
        self.python_executable_relative = executable.relative_to(environment_root)
        expected_controls = _python_environment_control_paths(environment_root)
        if python_environment_control_bindings is None:
            python_environment_control_bindings = [
                {
                    "path": str(path),
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
                for path in expected_controls
            ]
        validated_controls = []
        normalized_control_bindings = []
        for index, binding in enumerate(python_environment_control_bindings):
            path, normalized = _exact_file_binding(
                binding,
                "legacy adapter python_environment_control_files[{}]".format(index),
            )
            validated_controls.append(path)
            normalized_control_bindings.append(normalized)
        if tuple(validated_controls) != expected_controls:
            raise ValidationError(
                "legacy adapter Python environment control-file binding is incomplete or stale"
            )
        self.python_environment_control_bindings = tuple(
            normalized_control_bindings
        )

        validated_python_paths = []
        normalized_python_path_bindings = []
        for index, binding in enumerate(python_path_entries):
            path, normalized = _exact_file_binding(
                binding,
                "legacy adapter python_path_entries[{}]".format(index),
            )
            validated_python_paths.append(path)
            normalized_python_path_bindings.append(normalized)
        if validated_python_paths != sorted(validated_python_paths, key=str):
            raise ValidationError("legacy adapter python_path_entries must be sorted")
        if len(validated_python_paths) != len(set(validated_python_paths)):
            raise ValidationError("legacy adapter python_path_entries contain duplicates")
        self.python_path_entries = tuple(validated_python_paths)
        self.python_path_entry_bindings = tuple(normalized_python_path_bindings)
        sandbox = Path(str(sandbox_executable))
        if (
            not sandbox.is_absolute()
            or sandbox.is_symlink()
            or not sandbox.is_file()
        ):
            raise ValidationError(
                "legacy adapter sandbox_executable must be an absolute regular file"
            )
        self.sandbox_executable = str(sandbox.resolve())
        self.argv = tuple(argv)
        if not isinstance(python_executable, str) or not python_executable:
            raise ValidationError("legacy adapter python_executable must be a non-empty string")
        if any(not isinstance(item, str) or not item for item in self.argv):
            raise ValidationError("legacy adapter argv entries must be non-empty strings")
        self.artifact_path = artifact_path
        self.failure_artifact_path = failure_artifact_path
        self.query_file = query_file
        for value, name in (
            (entrypoint, "entrypoint"),
            (artifact_path, "artifact_path"),
            (query_file, "query_file"),
        ):
            _inside(self.source_root, value, name)
        if failure_artifact_path is not None:
            _inside(self.source_root, failure_artifact_path, "failure_artifact_path")
        self.timeout_seconds = _positive_timeout(timeout_seconds)
        self.environment_passthrough = tuple(environment_passthrough)
        _subprocess_environment(self.environment_passthrough)
        self.max_stdout_bytes = _positive_size(
            max_stdout_bytes, "adapter.max_stdout_bytes", 16777216
        )
        self.max_stderr_bytes = _positive_size(
            max_stderr_bytes, "adapter.max_stderr_bytes", 16777216
        )
        self.max_artifact_bytes = _positive_size(
            max_artifact_bytes, "adapter.max_artifact_bytes", 67108864
        )
        self.snapshot_expectations = (
            None
            if snapshot_bindings is None
            else _snapshot_binding_expectations(
                self.source_root, self.snapshot_paths, snapshot_bindings
            )
        )

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        snapshot_bindings: Optional[Sequence[Mapping[str, Any]]] = None,
        python_environment_control_bindings: Optional[
            Sequence[Mapping[str, Any]]
        ] = None,
    ) -> "ChatSceneLegacyAdapter":
        source_root = config.get("source_root")
        python_executable = config.get("python_executable")
        python_environment_root = config.get("python_environment_root")
        python_path_entries = config.get("python_path_entries")
        sandbox_executable = config.get("sandbox_executable")
        if not isinstance(source_root, str) or not source_root:
            raise ValidationError("adapter.source_root must be a non-empty path string")
        if not isinstance(python_executable, str) or not python_executable:
            raise ValidationError("adapter.python_executable must be a non-empty string")
        if not isinstance(python_environment_root, str) or not python_environment_root:
            raise ValidationError(
                "adapter.python_environment_root must be a non-empty string"
            )
        if not isinstance(python_path_entries, list) or not python_path_entries:
            raise ValidationError("adapter.python_path_entries must be a non-empty array")
        if not isinstance(sandbox_executable, str) or not sandbox_executable:
            raise ValidationError("adapter.sandbox_executable must be a non-empty string")
        return cls(
            source_root=Path(source_root),
            snapshot_paths=_string_sequence(config.get("snapshot_paths"), "adapter.snapshot_paths"),
            entrypoint=config.get("entrypoint", "retrieve/retrieve.py"),
            python_executable=python_executable,
            python_environment_root=python_environment_root,
            python_environment_control_bindings=(
                python_environment_control_bindings
            ),
            python_path_entries=python_path_entries,
            sandbox_executable=sandbox_executable,
            argv=_string_sequence(config.get("argv", []), "adapter.argv", allow_empty=True),
            artifact_path=config.get(
                "artifact_path",
                "safebench/scenario/scenario_data/scenic_data/dynamic_scenario/dynamic_0.scenic",
            ),
            failure_artifact_path=config.get(
                "failure_artifact_path",
                "safebench/scenario/scenario_data/scenic_data/dynamic_scenario/dynamic_0.txt",
            ),
            timeout_seconds=config.get("timeout_seconds"),
            query_file=config.get("query_file", "retrieve/scenario_descriptions.txt"),
            environment_passthrough=_string_sequence(
                config.get("environment_passthrough", list(_DEFAULT_ENVIRONMENT)),
                "adapter.environment_passthrough",
                allow_empty=True,
            ),
            max_stdout_bytes=config.get("max_stdout_bytes"),
            max_stderr_bytes=config.get("max_stderr_bytes"),
            max_artifact_bytes=config.get("max_artifact_bytes"),
            snapshot_bindings=snapshot_bindings,
        )

    def generate(self, query_text: str, workdir: Path) -> AdapterOutcome:
        if not isinstance(query_text, str):
            raise ValidationError("query_text must be a string")
        if "\n" in query_text or "\r" in query_text:
            message = "legacy ChatScene input requires exactly one physical query line"
            return AdapterOutcome(b"", message.encode("utf-8"), None, False, error=message)

        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=False)
        repository = workdir / "repository"
        try:
            dependency_snapshot = workdir / "runtime-dependencies"
            dependency_snapshot.mkdir(parents=True, exist_ok=False)
            control_snapshots = []
            for index, binding in enumerate(
                self.python_environment_control_bindings
            ):
                source = Path(binding["path"])
                relative = source.relative_to(self.python_environment_root)
                snapshot = _snapshot_exact_bound_file(
                    binding,
                    dependency_snapshot / "python-environment" / relative,
                    "legacy Python environment control[{}]".format(index),
                )
                control_snapshots.append((snapshot, relative, binding))
            python_path_snapshots = []
            for index, binding in enumerate(self.python_path_entry_bindings):
                source = Path(binding["path"])
                snapshot = _snapshot_exact_bound_file(
                    binding,
                    dependency_snapshot / "python-path" / str(index) / source.name,
                    "legacy Python path entry[{}]".format(index),
                )
                python_path_snapshots.append((snapshot, binding))
            _copy_snapshot(self.source_root, repository, self.snapshot_paths)
            if self.snapshot_expectations is not None:
                _attest_snapshot(repository, self.snapshot_expectations)
            # SafeBench is an editable namespace package in the legacy environment.
            # Materialize an empty package marker only inside the private snapshot
            # so its namespace cannot merge with the original repository through
            # a site-packages ``.egg-link``.
            safebench_root = repository / "safebench"
            if safebench_root.is_dir():
                package_marker = safebench_root / "__init__.py"
                if package_marker.exists():
                    if package_marker.is_symlink() or not package_marker.is_file():
                        raise AdapterError(
                            "private safebench package marker is not a regular file"
                        )
                else:
                    package_marker.write_bytes(b"")
            query_path = _inside(repository, self.query_file, "query_file")
            query_path.parent.mkdir(parents=True, exist_ok=True)
            # No trailing newline: legacy ``split('\\n')`` would treat it as a
            # second, empty query and violate the one-request contract.
            query_path.write_bytes(query_text.encode("utf-8"))
            entrypoint = _inside(repository, self.entrypoint, "entrypoint")
            if not entrypoint.is_file():
                raise AdapterError("legacy entrypoint is missing from the isolated snapshot")
            for relative in (self.artifact_path, self.failure_artifact_path):
                if relative is not None:
                    _inside(repository, relative, "output path").parent.mkdir(
                        parents=True, exist_ok=True
                    )
            # The legacy repository normally contains old dynamic_0 outputs.
            # Remove them only from the private snapshot so an old artifact can
            # never be mistaken for this invocation's response.
            for relative, name in (
                (self.artifact_path, "artifact_path"),
                (self.failure_artifact_path, "failure_artifact_path"),
            ):
                if relative is None:
                    continue
                prior = _inside(repository, relative, name)
                if prior.exists():
                    if prior.is_symlink() or not prior.is_file():
                        raise AdapterError("stale {} is not a regular file".format(name))
                    prior.unlink()
            sandbox_root = Path("/run/chatscene-benchmark")
            sandbox_repository = sandbox_root / "repository"
            sandbox_entrypoint = sandbox_repository / self.entrypoint
            sandbox_environment = sandbox_root / "python-env"
            sandbox_python = sandbox_environment / self.python_executable_relative
            sandbox_python_path_root = sandbox_root / "python-path"
            sandbox_python_path_directories = tuple(
                sandbox_python_path_root / str(index)
                for index in range(len(self.python_path_entries))
            )
            sandbox_python_paths = tuple(
                directory / Path(binding["path"]).name
                for directory, binding in zip(
                    sandbox_python_path_directories, self.python_path_entry_bindings
                )
            )
            sandbox_home = sandbox_root / "home"
            python_control_mounts = tuple(
                argument
                for source, relative, binding in control_snapshots
                for argument in (
                    "--ro-bind",
                    str(source),
                    str(sandbox_environment / relative),
                )
            )
            python_path_mounts = tuple(
                argument
                for (source, binding), directory, target in zip(
                    python_path_snapshots,
                    sandbox_python_path_directories,
                    sandbox_python_paths,
                )
                for argument in (
                    "--dir",
                    str(directory),
                    "--ro-bind",
                    str(source),
                    str(target),
                )
            )
            for index, (snapshot, relative, binding) in enumerate(control_snapshots):
                _attest_private_snapshot(
                    snapshot,
                    binding,
                    "legacy Python environment control[{}]".format(index),
                )
            for index, (snapshot, binding) in enumerate(python_path_snapshots):
                _attest_private_snapshot(
                    snapshot,
                    binding,
                    "legacy Python path entry[{}]".format(index),
                )
            dependency_snapshot_identity = _directory_stability_identity(
                dependency_snapshot, "legacy private dependency snapshot"
            )
            control_snapshot_identities = tuple(
                _regular_file_stability_identity(
                    snapshot,
                    "legacy Python environment control[{}]".format(index),
                )
                for index, (snapshot, relative, binding) in enumerate(
                    control_snapshots
                )
            )
            python_path_snapshot_identities = tuple(
                _regular_file_stability_identity(
                    snapshot, "legacy Python path entry[{}]".format(index)
                )
                for index, (snapshot, binding) in enumerate(python_path_snapshots)
            )
            sandbox_argv = (
                self.sandbox_executable,
                "--ro-bind",
                "/",
                "/",
                "--tmpfs",
                "/run",
                "--dir",
                str(sandbox_root),
                "--bind",
                str(repository),
                str(sandbox_repository),
                "--tmpfs",
                str(self.source_root),
                # Re-expose the repository-local uv environment only at a fixed
                # sandbox path after hiding the mutable checkout.  The original
                # absolute repository path remains inaccessible.
                "--ro-bind",
                str(self.python_environment_root),
                str(sandbox_environment),
                *python_control_mounts,
                "--dir",
                str(sandbox_python_path_root),
                *python_path_mounts,
                "--tmpfs",
                "/tmp",
                "--proc",
                "/proc",
                "--dev-bind",
                "/dev",
                "/dev",
                "--unshare-user",
                "--unshare-pid",
                "--unshare-uts",
                "--unshare-ipc",
                "--die-with-parent",
                "--new-session",
                "--dir",
                str(sandbox_home),
                "--dir",
                str(sandbox_home / ".cache"),
                "--dir",
                str(sandbox_home / ".matplotlib"),
                "--dir",
                str(sandbox_root / "python-eggs"),
                "--chdir",
                str(sandbox_entrypoint.parent),
                "--",
                str(sandbox_python),
                str(sandbox_entrypoint),
                *self.argv,
            )
            process = _run_process(
                sandbox_argv,
                entrypoint.parent,
                self.timeout_seconds,
                self.environment_passthrough,
                None,
                self.max_stdout_bytes,
                self.max_stderr_bytes,
                environment_overrides={
                    "HOME": str(sandbox_home),
                    "XDG_CACHE_HOME": str(sandbox_home / ".cache"),
                    "MPLCONFIGDIR": str(sandbox_home / ".matplotlib"),
                    # The legacy environment installs both SafeBench and Scenic
                    # through editable paths which point back at the live checkout.
                    # Import only the immutable per-run snapshot inside bwrap.
                    "PYTHONPATH": os.pathsep.join(
                        (
                            str(sandbox_repository),
                            str(sandbox_repository / "Scenic" / "src"),
                            *(str(path) for path in sandbox_python_paths),
                        )
                    ),
                    "PYTHON_EGG_CACHE": str(sandbox_root / "python-eggs"),
                },
            )
            if _directory_stability_identity(
                dependency_snapshot, "legacy private dependency snapshot"
            ) != dependency_snapshot_identity:
                raise ValidationError(
                    "legacy private dependency snapshot directory changed during launch"
                )
            if _python_environment_control_paths(
                self.python_environment_root
            ) != tuple(
                Path(binding["path"])
                for binding in self.python_environment_control_bindings
            ):
                raise ValidationError(
                    "legacy Python environment control set changed during launch"
                )
            for index, (snapshot, relative, binding) in enumerate(control_snapshots):
                if _regular_file_stability_identity(
                    snapshot,
                    "legacy Python environment control[{}]".format(index),
                ) != control_snapshot_identities[index]:
                    raise ValidationError(
                        "legacy Python environment control[{}] changed during launch".format(
                            index
                        )
                    )
                _exact_file_binding(
                    binding,
                    "legacy Python environment control[{}]".format(index),
                )
                _attest_private_snapshot(
                    snapshot,
                    binding,
                    "legacy Python environment control[{}]".format(index),
                )
            for index, (snapshot, binding) in enumerate(python_path_snapshots):
                if _regular_file_stability_identity(
                    snapshot, "legacy Python path entry[{}]".format(index)
                ) != python_path_snapshot_identities[index]:
                    raise ValidationError(
                        "legacy Python path entry[{}] changed during launch".format(
                            index
                        )
                    )
                _exact_file_binding(
                    binding, "legacy Python path entry[{}]".format(index)
                )
                _attest_private_snapshot(
                    snapshot,
                    binding,
                    "legacy Python path entry[{}]".format(index),
                )
            primary = _artifact_if_present(
                repository,
                self.artifact_path,
                "artifact_path",
                self.max_artifact_bytes,
            )
            failure_artifact = _artifact_if_present(
                repository,
                self.failure_artifact_path,
                "failure_artifact_path",
                self.max_artifact_bytes,
            )
        except (OSError, UnicodeError, AdapterError, ValidationError) as exc:
            message = "legacy adapter preparation failed: {}".format(exc)
            return AdapterOutcome(b"", message.encode("utf-8", errors="replace"), None, False, error=message)

        if process.timed_out or process.error or process.exit_code != 0:
            artifact = primary or failure_artifact
            error = process.error or "legacy process exited with status {}".format(process.exit_code)
            return AdapterOutcome(
                process.stdout,
                process.stderr,
                process.exit_code,
                process.timed_out,
                artifact_path=artifact,
                error=error,
            )
        if primary is None:
            return AdapterOutcome(
                process.stdout,
                process.stderr,
                process.exit_code,
                False,
                artifact_path=failure_artifact,
                error="legacy ChatScene produced no compilable dynamic_0.scenic artifact",
            )
        return AdapterOutcome(
            process.stdout,
            process.stderr,
            process.exit_code,
            False,
            disposition="generate",
            artifact_path=primary,
        )


def adapter_from_method_config(
    method_config: Mapping[str, Any],
    *,
    legacy_snapshot_bindings: Optional[Sequence[Mapping[str, Any]]] = None,
) -> MethodAdapter:
    """Build a supported adapter without exposing roster data to it."""

    adapter = method_config.get("adapter")
    if not isinstance(adapter, Mapping):
        raise ValidationError("method config requires an adapter object")
    adapter_type = adapter.get("type")
    if adapter_type == "command":
        return CommandAdapter.from_config(adapter)
    if adapter_type == "chatscene_legacy":
        implementation_bundle = method_config.get("implementation_bundle")
        environment_binding = (
            implementation_bundle.get("environment_manifest")
            if isinstance(implementation_bundle, Mapping)
            else None
        )
        environment_path, _ = _exact_file_binding(
            environment_binding,
            "legacy adapter method environment manifest",
        )
        environment_manifest = read_json(environment_path)
        control_bindings = environment_manifest.get(
            "python_environment_control_files"
        )
        if not isinstance(control_bindings, list) or not control_bindings:
            raise ValidationError(
                "legacy adapter method environment lacks Python control bindings"
            )
        return ChatSceneLegacyAdapter.from_config(
            adapter,
            snapshot_bindings=legacy_snapshot_bindings,
            python_environment_control_bindings=control_bindings,
        )
    raise ValidationError("unsupported generation adapter type: {}".format(adapter_type))


__all__ = ["ChatSceneLegacyAdapter", "CommandAdapter", "adapter_from_method_config"]
