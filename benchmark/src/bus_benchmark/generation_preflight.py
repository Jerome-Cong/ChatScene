"""Read-only generation readiness diagnostics which never invoke a method."""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .constants import SCHEMA_VERSION
from .errors import ValidationError
from .generation import (
    _iter_snapshot_source_files,
    _validate_python_environment_controls,
    _validate_method_config,
    build_generation_grid,
    probe_python_environment,
    running_python_environment_executable,
    validate_implementation_bundle,
)
from .jsonio import read_json, sha256_file
from .schema import validate_schema_instance


PREFLIGHT_TYPE = "generation_preflight_v0.1"
_PROBE_MARKER = "__BUS_GENERATION_PREFLIGHT__"
_IMPORT_PROBE = r"""
import importlib
import json
import sys

module = sys.argv[1]
try:
    importlib.import_module(module)
except BaseException as exc:
    message = str(exc)
    if isinstance(exc, ModuleNotFoundError):
        code = "module_not_found"
    elif "GLIBCXX_" in message:
        code = "native_runtime_incompatible"
    elif "cached_download" in message:
        code = "dependency_api_incompatible"
    elif "NVML" in message or "Driver Not Loaded" in message:
        code = "accelerator_driver_unavailable"
    else:
        code = "module_import_failed"
    result = {"ok": False, "reason_code": code, "exception_type": type(exc).__name__}
else:
    result = {"ok": True, "reason_code": "ok", "exception_type": None}
print("__BUS_GENERATION_PREFLIGHT__" + json.dumps(result, sort_keys=True, separators=(",", ":")))
"""
_CUDA_PROBE = r"""
import json
try:
    import torch
    available = bool(torch.cuda.is_available())
    count = int(torch.cuda.device_count()) if available else 0
except BaseException as exc:
    result = {"ok": False, "reason_code": "torch_probe_failed", "device_count": 0}
else:
    result = {
        "ok": available and count > 0,
        "reason_code": "ok" if available and count > 0 else "cuda_device_unavailable",
        "device_count": count,
    }
print("__BUS_GENERATION_PREFLIGHT__" + json.dumps(result, sort_keys=True, separators=(",", ":")))
"""


def _check(
    check_id: str,
    status: str,
    scope: str,
    reason_code: str,
    observed: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if status not in {"pass", "blocker", "warning", "not_applicable"}:
        raise ValidationError("generation preflight check status is invalid")
    if scope not in {"formal", "execution", "both", "informational"}:
        raise ValidationError("generation preflight check scope is invalid")
    result: Dict[str, Any] = {
        "check_id": check_id,
        "status": status,
        "scope": scope,
        "reason_code": reason_code,
    }
    if observed is not None:
        result["observed"] = dict(observed)
    return result


def _binding_path(binding: Any) -> Optional[Path]:
    if not isinstance(binding, Mapping):
        return None
    value = binding.get("path")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_file() and not path.is_symlink() else None


def _safe_probe_environment(home: Path) -> Dict[str, str]:
    egg_cache = home / "python-eggs"
    egg_cache.mkdir(parents=True, exist_ok=True)
    environment = {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "HF_HOME": str(home / ".cache" / "huggingface"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHON_EGG_CACHE": str(egg_cache),
    }
    for name in (
        "PATH",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "CUDA_VISIBLE_DEVICES",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _run_marked_probe(
    executable: Path,
    code: str,
    arguments: Sequence[str] = (),
    python_paths: Sequence[Path] = (),
) -> Dict[str, Any]:
    trusted_paths = [str(Path(path).resolve()) for path in python_paths]
    effective_code = "import sys\nsys.path[:0] = {!r}\n{}".format(
        trusted_paths, code
    )
    with tempfile.TemporaryDirectory(prefix="bus-generation-preflight-") as directory:
        try:
            completed = subprocess.run(
                [str(executable), "-I", "-c", effective_code, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20.0,
                env=_safe_probe_environment(Path(directory)),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "reason_code": "probe_launch_failed"}
    if len(completed.stdout) > 1024 * 1024 or len(completed.stderr) > 1024 * 1024:
        return {"ok": False, "reason_code": "probe_output_limit_exceeded"}
    for raw_line in reversed(completed.stdout.splitlines()):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeError:
            continue
        if not line.startswith(_PROBE_MARKER):
            continue
        try:
            document = json.loads(line[len(_PROBE_MARKER) :])
        except json.JSONDecodeError:
            break
        if isinstance(document, dict):
            return document
    return {"ok": False, "reason_code": "probe_output_invalid"}


def _environment_checks(
    label: str,
    binding: Any,
    expected_executable: Optional[Path],
    *,
    require_environment_root: bool = False,
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    path = _binding_path(binding)
    if path is None:
        return [
            _check(
                "{}_environment_manifest".format(label),
                "blocker",
                "formal",
                "environment_manifest_unavailable",
            )
        ]
    try:
        manifest = read_json(path)
    except Exception:
        return [
            _check(
                "{}_environment_manifest".format(label),
                "blocker",
                "formal",
                "environment_manifest_unreadable",
            )
        ]
    status = (
        manifest.get("decision_status") if isinstance(manifest, Mapping) else None
    )
    checks.append(
        _check(
            "{}_environment_status".format(label),
            "pass" if status == "frozen" else "blocker",
            "formal",
            "ok" if status == "frozen" else "environment_manifest_not_frozen",
            {"decision_status": status if status in {"draft", "frozen"} else "invalid"},
        )
    )
    executable = (
        _binding_path(manifest.get("executable"))
        if isinstance(manifest, Mapping)
        else None
    )
    if executable is None:
        checks.append(
            _check(
                "{}_environment_probe".format(label),
                "blocker",
                "both",
                "environment_executable_unavailable",
            )
        )
        return checks
    try:
        environment_root = _validate_python_environment_controls(
            manifest,
            executable,
            required=require_environment_root,
            label="{} environment manifest".format(label),
        )
    except Exception:
        checks.append(
            _check(
                "{}_environment_root".format(label),
                "blocker",
                "both",
                "python_environment_root_invalid",
            )
        )
    else:
        checks.append(
            _check(
                "{}_environment_root".format(label),
                "pass" if environment_root is not None else "not_applicable",
                "both" if environment_root is not None else "informational",
                "ok" if environment_root is not None else "environment_root_not_declared",
            )
        )
    if (
        expected_executable is not None
        and executable.resolve() != expected_executable.resolve()
    ):
        checks.append(
            _check(
                "{}_environment_executable".format(label),
                "blocker",
                "formal",
                "environment_executable_mismatch",
            )
        )
    else:
        checks.append(
            _check(
                "{}_environment_executable".format(label),
                "pass",
                "formal",
                "ok",
            )
        )
    try:
        observed = probe_python_environment(executable)
    except Exception:
        checks.append(
            _check(
                "{}_environment_probe".format(label),
                "blocker",
                "both",
                "environment_probe_failed",
            )
        )
        return checks
    declared_dependencies = manifest.get("locked_dependencies")
    declared_count = (
        len(declared_dependencies)
        if isinstance(declared_dependencies, list)
        else -1
    )
    runtime_match = manifest.get("runtime_version") == observed["runtime_version"]
    dependency_match = declared_dependencies == observed["locked_dependencies"]
    checks.append(
        _check(
            "{}_environment_inventory".format(label),
            "pass" if runtime_match and dependency_match else "blocker",
            "formal",
            "ok"
            if runtime_match and dependency_match
            else "environment_inventory_mismatch",
            {
                "runtime_version_matches": runtime_match,
                "declared_dependency_count": declared_count,
                "observed_dependency_count": len(observed["locked_dependencies"]),
                "dependency_inventory_matches": dependency_match,
            },
        )
    )
    return checks


def _source_overrides_credential(adapter: Mapping[str, Any]) -> bool:
    source_root = Path(str(adapter.get("source_root", "")))
    entrypoint = Path(str(adapter.get("entrypoint", "")))
    source = source_root / entrypoint.parent / "architecture.py"
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if not isinstance(target, ast.Subscript):
                continue
            value = target.value
            if not (
                isinstance(value, ast.Attribute)
                and value.attr == "environ"
                and isinstance(value.value, ast.Name)
                and value.value.id == "os"
            ):
                continue
            slice_value = target.slice
            if hasattr(ast, "Index") and isinstance(slice_value, ast.Index):
                slice_value = slice_value.value
            if isinstance(slice_value, ast.Constant) and slice_value.value == "OPENAI_API_KEY":
                return True
    return False


def _configured_model(argv: Any) -> Optional[str]:
    if not isinstance(argv, list):
        return None
    for index, value in enumerate(argv[:-1]):
        if value == "--model" and isinstance(argv[index + 1], str):
            return argv[index + 1]
    return None


def _configured_port(argv: Any) -> int:
    if isinstance(argv, list):
        for index, value in enumerate(argv[:-1]):
            if value == "--port_ip":
                try:
                    port = int(argv[index + 1])
                except (TypeError, ValueError):
                    return 2000
                return port if 1 <= port <= 65535 else 2000
    return 2000


def _has_loopback_listener(port: int) -> bool:
    expected = "{:04X}".format(port)
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = table.read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeError):
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            local = fields[1]
            if ":" not in local:
                continue
            address, local_port = local.rsplit(":", 1)
            if local_port.upper() != expected:
                continue
            if address in {"00000000", "0100007F", "0" * 32, "0" * 24 + "0100007F"}:
                return True
    return False


def _legacy_execution_checks(
    adapter: Mapping[str, Any], method_executable: Optional[Path]
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    python_paths = []
    python_path_error = None
    entries = adapter.get("python_path_entries")
    if not isinstance(entries, list) or not entries:
        python_path_error = "python_path_entries_unavailable"
    else:
        for binding in entries:
            path = _binding_path(binding)
            if (
                path is None
                or path.stat().st_size != binding.get("bytes")
                or sha256_file(path) != binding.get("sha256")
            ):
                python_path_error = "python_path_entry_binding_invalid"
                python_paths = []
                break
            python_paths.append(path.resolve())
        if python_paths != sorted(python_paths, key=str) or len(python_paths) != len(
            set(python_paths)
        ):
            python_path_error = "python_path_entry_binding_invalid"
            python_paths = []
    checks.append(
        _check(
            "legacy_python_path_entries",
            "pass" if python_path_error is None else "blocker",
            "both",
            "ok" if python_path_error is None else python_path_error,
            {"bound_entry_count": len(python_paths)},
        )
    )
    sandbox_value = adapter.get("sandbox_executable")
    sandbox = Path(sandbox_value) if isinstance(sandbox_value, str) else None
    sandbox_ok = bool(
        sandbox is not None
        and sandbox.is_absolute()
        and sandbox.is_file()
        and not sandbox.is_symlink()
        and os.access(str(sandbox), os.X_OK)
    )
    if sandbox_ok:
        try:
            completed = subprocess.run(
                [str(sandbox), "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5.0,
                check=False,
                env={"PATH": os.environ.get("PATH", os.defpath)},
            )
            sandbox_ok = completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            sandbox_ok = False
    checks.append(
        _check(
            "legacy_sandbox_executable",
            "pass" if sandbox_ok else "blocker",
            "execution",
            "ok" if sandbox_ok else "sandbox_executable_unavailable",
        )
    )
    if method_executable is None:
        checks.append(
            _check(
                "method_import_probes",
                "blocker",
                "execution",
                "method_executable_unavailable",
            )
        )
    else:
        for module in ("setGPU", "sentence_transformers", "carla"):
            result = _run_marked_probe(
                method_executable,
                _IMPORT_PROBE,
                (module,),
                python_paths=python_paths,
            )
            checks.append(
                _check(
                    "method_import_{}".format(module.lower()),
                    "pass" if result.get("ok") is True else "blocker",
                    "execution",
                    str(result.get("reason_code", "probe_output_invalid")),
                    {
                        "importable": result.get("ok") is True,
                        "exception_type": result.get("exception_type")
                        if isinstance(result.get("exception_type"), str)
                        else None,
                    },
                )
            )
        cuda = _run_marked_probe(
            method_executable, _CUDA_PROBE, python_paths=python_paths
        )
        checks.append(
            _check(
                "method_cuda_device",
                "pass" if cuda.get("ok") is True else "blocker",
                "execution",
                str(cuda.get("reason_code", "probe_output_invalid")),
                {
                    "available": cuda.get("ok") is True,
                    "device_count": cuda.get("device_count")
                    if type(cuda.get("device_count")) is int
                    else 0,
                },
            )
        )
    source_root = Path(str(adapter.get("source_root", "")))
    entrypoint = Path(str(adapter.get("entrypoint", "")))
    try:
        entrypoint_text = (source_root / entrypoint).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        entrypoint_text = ""
    requires_external_model = "sentence-t5-large" in entrypoint_text
    checks.append(
        _check(
            "retrieval_model_asset",
            "blocker" if requires_external_model else "not_applicable",
            "formal" if requires_external_model else "informational",
            "model_cache_not_bundle_bound" if requires_external_model else "not_required",
            {"bundle_bound": False} if requires_external_model else None,
        )
    )
    model = _configured_model(adapter.get("argv"))
    if isinstance(model, str) and model.startswith("gpt"):
        override = _source_overrides_credential(adapter)
        credential_present = bool(os.environ.get("OPENAI_API_KEY"))
        checks.append(
            _check(
                "llm_credential_contract",
                "blocker" if override or not credential_present else "warning",
                "execution" if override or not credential_present else "formal",
                "credential_overridden_by_method_source"
                if override
                else (
                    "credential_identity_not_formally_bound"
                    if credential_present
                    else "credential_unavailable"
                ),
                {
                    "environment_credential_present": credential_present,
                    "method_source_overrides_environment": override,
                },
            )
        )
    else:
        checks.append(
            _check(
                "llm_credential_contract",
                "not_applicable",
                "informational",
                "not_required_by_configured_model",
            )
        )
    port = _configured_port(adapter.get("argv"))
    listener = _has_loopback_listener(port)
    checks.append(
        _check(
            "carla_loopback_listener",
            "pass" if listener else "blocker",
            "execution",
            "ok" if listener else "carla_listener_unavailable",
            {"port": port, "listening": listener},
        )
    )
    return checks


def generation_preflight(
    library_path: Path,
    roster: Mapping[str, Any],
    method_config_path: Path,
) -> Dict[str, Any]:
    """Inspect generation readiness without calling an adapter or consuming a run."""

    library_path = Path(library_path)
    method_config_path = Path(method_config_path)
    config = read_json(method_config_path)
    validate_schema_instance(
        config, "method_config", context="generation preflight method config"
    )
    validate_schema_instance(
        roster, "query_roster", context="generation preflight roster"
    )
    _validate_method_config(config, roster)
    jobs = build_generation_grid(library_path, roster)
    physical_line_compatible = all(
        "\n" not in job.query_text and "\r" not in job.query_text for job in jobs
    )
    checks: List[Dict[str, Any]] = [
        _check(
            "query_roster_grid",
            "pass" if physical_line_compatible else "blocker",
            "both",
            "ok"
            if physical_line_compatible
            else "legacy_query_requires_one_physical_line",
            {
                "query_count": roster.get("query_count"),
                "job_count": len(jobs),
                "physical_line_compatible": physical_line_compatible,
            },
        ),
        _check(
            "method_config_status",
            "pass" if config.get("status") == "frozen" else "blocker",
            "formal",
            "ok" if config.get("status") == "frozen" else "method_config_not_frozen",
            {"status": config.get("status")},
        ),
        _check(
            "query_roster_status",
            "pass" if roster.get("decision_status") == "confirmed" else "blocker",
            "formal",
            "ok"
            if roster.get("decision_status") == "confirmed"
            else "query_roster_not_confirmed",
            {"decision_status": roster.get("decision_status")},
        ),
    ]
    try:
        validate_implementation_bundle(config, formal=False)
    except Exception:
        checks.append(
            _check(
                "implementation_bundle_live_bindings",
                "blocker",
                "both",
                "implementation_bundle_live_binding_invalid",
            )
        )
    else:
        checks.append(
            _check(
                "implementation_bundle_live_bindings",
                "pass",
                "both",
                "ok",
            )
        )
    try:
        validate_implementation_bundle(config, formal=True)
    except Exception:
        checks.append(
            _check(
                "implementation_bundle_formal_contract",
                "blocker",
                "formal",
                "implementation_bundle_formal_contract_invalid",
            )
        )
    else:
        checks.append(
            _check(
                "implementation_bundle_formal_contract",
                "pass",
                "formal",
                "ok",
            )
        )
    bundle = config.get("implementation_bundle")
    bundle_status = bundle.get("status") if isinstance(bundle, Mapping) else None
    checks.append(
        _check(
            "implementation_bundle_status",
            "pass" if bundle_status == "frozen" else "blocker",
            "formal",
            "ok" if bundle_status == "frozen" else "implementation_bundle_not_frozen",
            {"status": bundle_status},
        )
    )
    files = bundle.get("files") if isinstance(bundle, Mapping) else []
    registered = {
        str(Path(binding["path"]).resolve())
        for binding in files
        if isinstance(binding, Mapping) and isinstance(binding.get("path"), str)
    }
    adapter = config.get("adapter")
    if isinstance(adapter, Mapping) and adapter.get("type") == "chatscene_legacy":
        snapshot_files = _iter_snapshot_source_files(adapter)
        missing_count = sum(1 for path in snapshot_files if str(path) not in registered)
        checks.append(
            _check(
                "legacy_snapshot_bundle_coverage",
                "pass" if missing_count == 0 else "blocker",
                "formal",
                "ok" if missing_count == 0 else "snapshot_bundle_incomplete",
                {
                    "snapshot_file_count": len(snapshot_files),
                    "registered_snapshot_file_count": len(snapshot_files) - missing_count,
                    "missing_snapshot_file_count": missing_count,
                },
            )
        )
        passthrough = adapter.get("environment_passthrough")
        passthrough_count = len(passthrough) if isinstance(passthrough, list) else -1
        checks.append(
            _check(
                "formal_environment_passthrough",
                "pass" if passthrough_count == 0 else "blocker",
                "formal",
                "ok" if passthrough_count == 0 else "unbound_environment_passthrough",
                {"entry_count": passthrough_count},
            )
        )
    else:
        checks.append(
            _check(
                "legacy_snapshot_bundle_coverage",
                "not_applicable",
                "informational",
                "not_a_legacy_adapter",
            )
        )
    method_executable = None
    if isinstance(adapter, Mapping):
        executable_value = (
            adapter.get("python_executable")
            if adapter.get("type") == "chatscene_legacy"
            else (adapter.get("argv") or [None])[0]
        )
        if isinstance(executable_value, str) and Path(executable_value).is_file():
            method_executable = Path(executable_value).resolve()
    environment_binding = (
        bundle.get("environment_manifest") if isinstance(bundle, Mapping) else None
    )
    harness_binding = (
        bundle.get("harness_environment_manifest")
        if isinstance(bundle, Mapping)
        else None
    )
    legacy_adapter = (
        isinstance(adapter, Mapping)
        and adapter.get("type") == "chatscene_legacy"
    )
    checks.extend(
        _environment_checks(
            "method",
            environment_binding,
            method_executable,
            require_environment_root=legacy_adapter,
        )
    )
    try:
        running_harness_executable = running_python_environment_executable()
    except ValidationError:
        running_harness_executable = None
        checks.append(
            _check(
                "harness_running_executable_identity",
                "blocker",
                "formal",
                "running_python_executable_not_regular",
            )
        )
    else:
        checks.append(
            _check(
                "harness_running_executable_identity",
                "pass",
                "formal",
                "ok",
            )
        )
    checks.extend(
        _environment_checks(
            "harness",
            harness_binding,
            running_harness_executable,
            require_environment_root=legacy_adapter,
        )
    )
    if isinstance(adapter, Mapping) and adapter.get("type") == "chatscene_legacy":
        checks.extend(_legacy_execution_checks(adapter, method_executable))
    else:
        checks.append(
            _check(
                "method_specific_execution_probe",
                "warning",
                "execution",
                "no_safe_generic_command_probe",
            )
        )
    formal_blockers = [
        check
        for check in checks
        if check["status"] == "blocker" and check["scope"] in {"formal", "both"}
    ]
    execution_blockers = [
        check
        for check in checks
        if check["status"] == "blocker" and check["scope"] in {"execution", "both"}
    ]
    blockers = [
        {
            "check_id": check["check_id"],
            "scope": check["scope"],
            "reason_code": check["reason_code"],
        }
        for check in checks
        if check["status"] == "blocker"
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "preflight_type": PREFLIGHT_TYPE,
        "method_id": config.get("method_id"),
        "platform": config.get("platform"),
        "query_count": roster.get("query_count"),
        "job_count": len(jobs),
        "repetition_consumed": False,
        "adapter_invoked": False,
        "query_text_disclosed": False,
        "secret_values_disclosed": False,
        "formal_ready": not formal_blockers,
        "execution_ready": not execution_blockers,
        "ready": not formal_blockers and not execution_blockers,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "checks": checks,
    }


__all__ = ["PREFLIGHT_TYPE", "generation_preflight"]
