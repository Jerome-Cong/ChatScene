"""Immutable, query-only generation runner kept outside ChatScene itself."""

import datetime as _datetime
import ast
import difflib
import errno
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .adapters import AdapterOutcome, MethodAdapter
from .adapters.chatscene import adapter_from_method_config
from .constants import (
    CARLA_EGO_BLUEPRINT,
    EGO_PROXY_LENGTH_M,
    EGO_PROXY_WIDTH_M,
    SCHEMA_VERSION,
)
from .errors import AdapterError, ValidationError
from .jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    strict_json_object_bytes,
    write_json,
)
from . import metadrive_pg
from .provenance import record_sha256
from .roster import roster_entries
from .schema import (
    SCHEMA_DIRECTORY,
    supported_schema_paths,
    validate_schema_instance,
)


PRODUCER_ID = "immutable-generation-runner"
PRODUCER_VERSION = "0.1"
CARLA_FINALIZATION_POLICY_ID = "carla_ego_proxy_v0.1"
METADRIVE_FINALIZATION_POLICY_ID = metadrive_pg.FINALIZATION_POLICY_ID
METADRIVE_EGO_VEHICLE_MODEL = metadrive_pg.EGO_VEHICLE_MODEL

_PYTHON_ENVIRONMENT_PROBE_CODE = (
    "import importlib.metadata as metadata\n"
    "import json\n"
    "import re\n"
    "import sys\n"
    "packages = {}\n"
    "for distribution in metadata.distributions():\n"
    "    name = distribution.metadata.get('Name')\n"
    "    version = distribution.version\n"
    "    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:\n"
    "        raise RuntimeError('distribution metadata is incomplete')\n"
    "    canonical_name = re.sub(r'[-_.]+', '-', name).lower()\n"
    "    if canonical_name in packages:\n"
    "        raise RuntimeError('duplicate canonical distribution name')\n"
    "    packages[canonical_name] = version\n"
    "document = {\n"
    "    'runtime_version': sys.version.split()[0],\n"
    "    'locked_dependencies': [\n"
    "        {'name': name, 'version': packages[name]} for name in sorted(packages)\n"
    "    ],\n"
    "}\n"
    "print(json.dumps(document, sort_keys=True, separators=(',', ':')))\n"
)
_PYTHON_ENVIRONMENT_PROBE_TIMEOUT_SECONDS = 20.0
_PYTHON_ENVIRONMENT_PROBE_MAX_BYTES = 1024 * 1024
_PYTHON_ENVIRONMENT_CONTROL_PATTERNS = (
    "*.pth",
    "*.egg-link",
    "__editable__*.py",
)


def _pth_local_import_targets(site_root: Path, pth_path: Path) -> Tuple[Path, ...]:
    """Resolve local modules/packages executed or inspected by one ``.pth`` file."""

    modules = set()
    try:
        lines = pth_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValidationError("Python .pth control file is unreadable") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not stripped.startswith("import"):
            continue
        try:
            tree = ast.parse(stripped, filename=str(pth_path), mode="exec")
        except SyntaxError as exc:
            raise ValidationError("Python .pth import hook is not parseable") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
            elif isinstance(node, ast.Call):
                is_dynamic_import = (
                    isinstance(node.func, ast.Name) and node.func.id == "__import__"
                ) or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"import_module", "find_spec"}
                )
                if is_dynamic_import:
                    if not node.args or not isinstance(node.args[0], ast.Str):
                        raise ValidationError(
                            "Python .pth import hook contains a dynamic module target"
                        )
                    modules.add(node.args[0].s)
    targets = set()
    for module in modules:
        top_level = module.split(".", 1)[0]
        module_file = site_root / (top_level + ".py")
        package_root = site_root / top_level
        if module_file.is_file():
            targets.add(module_file.resolve())
        if package_root.is_dir():
            for path in package_root.rglob("*"):
                if (
                    path.is_file()
                    and "__pycache__" not in path.parts
                    and path.suffix != ".pyc"
                ):
                    targets.add(path.resolve())
    return tuple(sorted(targets, key=str))

_COMPLETE_DISPOSITIONS = {
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
_MODEL_RE = re.compile(
    r"^(?P<prefix>[ \t]*EGO_MODEL[ \t]*=[ \t]*(?P<quote>['\"]))"
    r"(?P<value>[^'\"]+)"
    r"(?P<suffix>(?P=quote)[ \t]*(?:#.*)?)$"
)
_EGO_START_RE = re.compile(r"^[ \t]*ego[ \t]*=[ \t]*(?:Car|EgoCar)\b.*$")
_BLUEPRINT_RE = re.compile(
    r"^(?P<prefix>[ \t]*with[ \t]+blueprint[ \t]+EGO_MODEL)"
    r"(?P<suffix>[ \t]*,?[ \t]*(?:#.*)?)$"
)


def _dimension_re(name: str) -> re.Pattern:
    return re.compile(
        r"^(?P<prefix>[ \t]*with[ \t]+{}[ \t]+)".format(name)
        + r"(?P<value>.+?)"
        + r"(?P<suffix>[ \t]*,?[ \t]*(?:#.*)?)$"
    )


_LENGTH_RE = _dimension_re("length")
_WIDTH_RE = _dimension_re("width")
_EGO_MODEL_TOKEN_RE = re.compile(r"\bEGO_MODEL\b")
@dataclass(frozen=True)
class GenerationJob:
    """One query/repetition cell.  Only ``query_text`` crosses the adapter API."""

    run_id: str
    method_id: str
    platform: str
    query_id: str
    intent_group_id: str
    statistical_intent_cluster_id: str
    surface_style: str
    repetition: int
    expected_support: str
    query_text: str
    request_sha256: str


@dataclass(frozen=True)
class EgoFinalization:
    final_bytes: bytes
    diff_bytes: bytes
    changes: Tuple[str, ...]


def _split_line(line: str) -> Tuple[str, str]:
    content = line.rstrip("\r\n")
    return content, line[len(content) :]


def _leading_width(content: str) -> int:
    return len(content) - len(content.lstrip(" \t"))


def _single_match(lines: Sequence[str], pattern: re.Pattern, name: str) -> Tuple[int, re.Match]:
    matches = []
    for index, line in enumerate(lines):
        content, _ = _split_line(line)
        match = pattern.fullmatch(content)
        if match:
            matches.append((index, match))
    if len(matches) != 1:
        raise ValidationError("Scenic artifact requires exactly one {}".format(name))
    return matches[0]


def _ego_block(lines: Sequence[str]) -> Tuple[int, int]:
    start, _ = _single_match(lines, _EGO_START_RE, "ego Car declaration")
    start_content, _ = _split_line(lines[start])
    base_indent = _leading_width(start_content)
    end = start + 1
    while end < len(lines):
        content, _ = _split_line(lines[end])
        if not content.strip():
            end += 1
            continue
        if _leading_width(content) <= base_indent:
            break
        end += 1
    return start, end


def _block_matches(
    lines: Sequence[str], start: int, end: int, pattern: re.Pattern
) -> List[Tuple[int, re.Match]]:
    matches = []
    for index in range(start, end):
        content, _ = _split_line(lines[index])
        match = pattern.fullmatch(content)
        if match:
            matches.append((index, match))
    return matches


def _ensure_clause_comma(content: str) -> str:
    comment_at = content.find("#")
    body = content if comment_at < 0 else content[:comment_at]
    comment = "" if comment_at < 0 else content[comment_at:]
    stripped = body.rstrip(" \t")
    spacing = body[len(stripped) :]
    if not stripped.endswith(","):
        stripped += ","
    return stripped + spacing + comment


def _normalize_blueprint_clause(content: str) -> str:
    match = _BLUEPRINT_RE.fullmatch(content)
    if not match:
        return content
    suffix = match.group("suffix")
    comment_at = suffix.find("#")
    comment = "" if comment_at < 0 else suffix[comment_at:]
    return match.group("prefix") + (" " + comment if comment else "")


def _semantic_skeleton(text: str) -> str:
    lines = text.splitlines(keepends=True)
    _assert_ego_model_scope(lines)
    model_index, model_match = _single_match(lines, _MODEL_RE, "EGO_MODEL assignment")
    _, newline = _split_line(lines[model_index])
    lines[model_index] = (
        model_match.group("prefix")
        + "<EGO_BLUEPRINT>"
        + model_match.group("suffix")
        + newline
    )
    start, end = _ego_block(lines)
    output = []
    for index, line in enumerate(lines):
        content, newline = _split_line(line)
        if start <= index < end and (
            _LENGTH_RE.fullmatch(content) or _WIDTH_RE.fullmatch(content)
        ):
            continue
        if start <= index < end and _BLUEPRINT_RE.fullmatch(content):
            line = _normalize_blueprint_clause(content) + newline
        output.append(line)
    return "".join(output)


def _assert_ego_model_scope(lines: Sequence[str]) -> None:
    """Require EGO_MODEL to be referenced by the ego blueprint only."""

    model_index, _ = _single_match(lines, _MODEL_RE, "EGO_MODEL assignment")
    start, end = _ego_block(lines)
    blueprint = _block_matches(lines, start, end, _BLUEPRINT_RE)
    if len(blueprint) != 1:
        raise ValidationError("ego declaration must reference EGO_MODEL exactly once")
    allowed = {model_index, blueprint[0][0]}
    comment_columns = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO("".join(lines)).readline)
        for token in tokens:
            if token.type == tokenize.COMMENT:
                comment_columns[token.start[0] - 1] = token.start[1]
    except (IndentationError, tokenize.TokenError) as exc:
        raise ValidationError(
            "Scenic artifact cannot be tokenized safely for ego-only finalization"
        ) from exc
    for index in range(len(lines)):
        content, _ = _split_line(lines[index])
        code_and_literals = content[: comment_columns.get(index, len(content))]
        # Count the literal token even inside strings/f-strings. Scenic allows
        # expressions such as f"{EGO_MODEL}" in blueprint clauses, and Python
        # 3.8's tokenizer reports the whole f-string as one STRING token. A
        # conservative false positive in a user string is preferable to
        # changing a non-ego actor through the global model variable.
        occurrences = len(_EGO_MODEL_TOKEN_RE.findall(code_and_literals))
        expected = 1 if index in allowed else 0
        if occurrences != expected:
            raise ValidationError("EGO_MODEL may be referenced only by the ego blueprint")


def verify_carla_ego_finalization(raw_text: str, final_text: str) -> None:
    """Fail closed unless raw/final differ only in the approved ego proxy fields."""

    raw_skeleton = _semantic_skeleton(raw_text)
    final_skeleton = _semantic_skeleton(final_text)
    if raw_skeleton != final_skeleton:
        raise ValidationError("finalization changed semantics outside the approved ego proxy fields")

    lines = final_text.splitlines(keepends=True)
    _, model = _single_match(lines, _MODEL_RE, "EGO_MODEL assignment")
    if model.group("value") != CARLA_EGO_BLUEPRINT:
        raise ValidationError("finalized ego blueprint is not the approved Impala proxy")
    start, end = _ego_block(lines)
    blueprint = _block_matches(lines, start, end, _BLUEPRINT_RE)
    lengths = _block_matches(lines, start, end, _LENGTH_RE)
    widths = _block_matches(lines, start, end, _WIDTH_RE)
    if len(blueprint) != 1:
        raise ValidationError("ego declaration must reference EGO_MODEL exactly once")
    if len(lengths) != 1 or lengths[0][1].group("value").strip() != "5.33":
        raise ValidationError("finalized ego length must be exactly 5.33 m")
    if len(widths) != 1 or widths[0][1].group("value").strip() != "2.10":
        raise ValidationError("finalized ego width must be exactly 2.10 m")


def finalize_carla_ego_artifact(raw_bytes: bytes) -> EgoFinalization:
    """Apply the sole permitted post-generation normalization to Scenic text."""

    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("generated Scenic artifact is not UTF-8") from exc
    lines = raw_text.splitlines(keepends=True)
    _assert_ego_model_scope(lines)
    model_index, model_match = _single_match(lines, _MODEL_RE, "EGO_MODEL assignment")
    changes = []
    if model_match.group("value") != CARLA_EGO_BLUEPRINT:
        _, newline = _split_line(lines[model_index])
        lines[model_index] = (
            model_match.group("prefix")
            + CARLA_EGO_BLUEPRINT
            + model_match.group("suffix")
            + newline
        )
        changes.append("ego_blueprint")

    start, end = _ego_block(lines)
    blueprint = _block_matches(lines, start, end, _BLUEPRINT_RE)
    if len(blueprint) != 1:
        raise ValidationError("ego declaration must reference EGO_MODEL exactly once")

    desired = (("ego_length_m", _LENGTH_RE, "5.33"), ("ego_width_m", _WIDTH_RE, "2.10"))
    missing = []
    for change_name, pattern, value in desired:
        matches = _block_matches(lines, start, end, pattern)
        if len(matches) > 1:
            raise ValidationError("ego declaration contains duplicate {} clauses".format(change_name))
        if not matches:
            missing.append((change_name, value))
            continue
        index, match = matches[0]
        if match.group("value").strip() != value:
            _, newline = _split_line(lines[index])
            lines[index] = match.group("prefix") + value + match.group("suffix") + newline
            changes.append(change_name)

    if missing:
        blueprint_index = blueprint[0][0]
        content, newline = _split_line(lines[blueprint_index])
        newline = newline or "\n"
        content = _ensure_clause_comma(content)
        lines[blueprint_index] = content + newline
        indent = re.match(r"^[ \t]*", content).group(0)
        insertion = []
        for change_name, value in missing:
            property_name = "length" if change_name == "ego_length_m" else "width"
            insertion.append("{}with {} {},{}".format(indent, property_name, value, newline))
            changes.append(change_name)
        lines[blueprint_index + 1 : blueprint_index + 1] = insertion

    final_text = "".join(lines)
    verify_carla_ego_finalization(raw_text, final_text)
    diff = "".join(
        difflib.unified_diff(
            raw_text.splitlines(keepends=True),
            final_text.splitlines(keepends=True),
            fromfile="artifact.raw.scenic",
            tofile="artifact.final.scenic",
            n=0,
        )
    ).encode("utf-8")
    return EgoFinalization(final_text.encode("utf-8"), diff, tuple(changes))


# MetaDrive generation accepts only the strict native PG artifact defined in
# ``metadrive_pg``; imported or reconstructed road graphs are not input formats.
def verify_metadrive_ego_finalization(
    raw_text: str,
    final_text: str,
    token_registry: Mapping[str, Any],
    token_registry_sha256: str,
) -> None:
    try:
        metadrive_pg.verify_finalization(
            raw_text.encode("utf-8"),
            final_text.encode("utf-8"),
            token_registry,
            token_registry_sha256,
        )
    except (UnicodeError, metadrive_pg.MetaDrivePGError) as exc:
        raise ValidationError(str(exc)) from exc


def finalize_metadrive_ego_artifact(
    raw_bytes: bytes,
    token_registry: Mapping[str, Any],
    token_registry_sha256: str,
) -> EgoFinalization:
    try:
        result = metadrive_pg.finalize_artifact(
            raw_bytes, token_registry, token_registry_sha256
        )
    except metadrive_pg.MetaDrivePGError as exc:
        raise ValidationError(str(exc)) from exc
    return EgoFinalization(result.final_bytes, result.diff_bytes, result.changes)


def _run_id(method_id: str, platform: str, query_id: str, repetition: int) -> str:
    identity = {
        "method_id": method_id,
        "platform": platform,
        "query_id": query_id,
        "repetition": repetition,
    }
    return sha256_bytes(canonical_json_bytes(identity))


def build_generation_grid(
    library_path: Path, roster: Mapping[str, Any]
) -> List[GenerationJob]:
    """Construct the exact query-by-five execution grid bound to a roster hash."""

    library_path = Path(library_path)
    if sha256_file(library_path) != roster.get("library_sha256"):
        raise ValidationError("query library bytes differ from the roster binding")
    entries = roster_entries(roster)
    records = read_jsonl(library_path)
    by_id = {}
    for record in records:
        query_id = record.get("query_id")
        query_text = record.get("query_text")
        if not isinstance(query_id, str) or not query_id or query_id in by_id:
            raise ValidationError("query library requires unique non-empty query_id values")
        if not isinstance(query_text, str) or not query_text.strip():
            raise ValidationError("query library requires non-empty query_text values")
        by_id[query_id] = record
    if set(by_id) != set(entries):
        raise ValidationError("query library and roster must contain identical query IDs")

    method_id = roster.get("method_id")
    platform = roster.get("platform")
    if not isinstance(method_id, str) or not method_id:
        raise ValidationError("roster method_id is required")
    if platform not in ("carla", "metadrive"):
        raise ValidationError("roster platform is invalid")

    jobs = []
    for query_id in sorted(entries):
        entry = entries[query_id]
        source = by_id[query_id]
        for field in ("intent_group_id", "surface_style", "expected_support"):
            if source.get(field) != entry.get(field):
                raise ValidationError("roster {} differs from the query library".format(field))
        request_sha256 = sha256_bytes(
            canonical_json_bytes({"query_text": source["query_text"]})
        )
        for repetition in roster["repetitions"]:
            jobs.append(
                GenerationJob(
                    run_id=_run_id(method_id, platform, query_id, repetition),
                    method_id=method_id,
                    platform=platform,
                    query_id=query_id,
                    intent_group_id=entry["intent_group_id"],
                    statistical_intent_cluster_id=entry[
                        "statistical_intent_cluster_id"
                    ],
                    surface_style=entry["surface_style"],
                    repetition=repetition,
                    expected_support=entry["expected_support"],
                    query_text=source["query_text"],
                    request_sha256=request_sha256,
                )
            )
    if len(jobs) != roster.get("expected_response_count"):
        raise ValidationError("generated execution grid does not match roster count")
    return jobs


def _validate_method_config(config: Mapping[str, Any], roster: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "status",
        "method_id",
        "platform",
        "query_input_fields",
        "post_output_semantic_repair",
        "implementation_bundle",
        "adapter",
        "finalization",
    }
    allowed = set(required)
    if config.get("platform") == "metadrive":
        required.add("token_registry")
        allowed.add("token_registry")
    if not isinstance(config, Mapping) or set(config) != required or set(config) - allowed:
        raise ValidationError("method config has missing or unknown top-level fields")
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError("method config schema_version is invalid")
    if config.get("status") not in ("draft", "frozen"):
        raise ValidationError("method config status is invalid")
    if config.get("method_id") != roster.get("method_id"):
        raise ValidationError("method config differs from roster method_id")
    if config.get("platform") != roster.get("platform"):
        raise ValidationError("method config differs from roster platform")
    platform = config.get("platform")
    if platform not in ("carla", "metadrive"):
        raise ValidationError("method config platform is invalid")
    if config.get("query_input_fields") != ["query_text"]:
        raise ValidationError("method adapter input must be exactly query_text")
    if config.get("post_output_semantic_repair") is not False:
        raise ValidationError("post-output semantic repair must remain disabled")
    finalization = config.get("finalization")
    if platform == "carla":
        expected = {
            "policy_id": CARLA_FINALIZATION_POLICY_ID,
            "ego_blueprint": CARLA_EGO_BLUEPRINT,
            "ego_length_m": EGO_PROXY_LENGTH_M,
            "ego_width_m": EGO_PROXY_WIDTH_M,
        }
    else:
        expected = {
            "policy_id": METADRIVE_FINALIZATION_POLICY_ID,
            "artifact_type": metadrive_pg.ARTIFACT_TYPE,
            "ego_vehicle_model": METADRIVE_EGO_VEHICLE_MODEL,
            "ego_length_m": EGO_PROXY_LENGTH_M,
            "ego_width_m": EGO_PROXY_WIDTH_M,
        }
    if finalization != expected:
        raise ValidationError("method config finalization policy is not the approved ego proxy")
    if not isinstance(config.get("adapter"), Mapping):
        raise ValidationError("method config adapter must be an object")
    if platform == "metadrive" and config["adapter"].get("type") != "command":
        raise ValidationError("MetaDrive generation requires a native command adapter")


def _live_file_binding(
    binding: Mapping[str, Any], label: str, *, require_absolute: bool = True
) -> Path:
    if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256", "bytes"}:
        raise ValidationError("{} must be an exact file binding".format(label))
    path_value = binding.get("path")
    digest = binding.get("sha256")
    size = binding.get("bytes")
    if not isinstance(path_value, str) or not path_value:
        raise ValidationError("{}.path must be non-empty".format(label))
    path = Path(path_value)
    if require_absolute and not path.is_absolute():
        raise ValidationError("{}.path must be absolute".format(label))
    if _path_has_symlink_component(path) or not path.is_file():
        raise ValidationError("{} must reference a regular non-symlink file".format(label))
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or type(size) is not int
        or size < 0
    ):
        raise ValidationError("{} contains an invalid hash or byte count".format(label))
    if path.stat().st_size != size or sha256_file(path) != digest:
        raise ValidationError("{} differs from its frozen live bytes".format(label))
    return path.resolve()


def _path_has_symlink_component(path: Path) -> bool:
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _regular_contained_file(path: Path, root: Path, label: str) -> Path:
    path = Path(path)
    root = Path(root).resolve()
    if _path_has_symlink_component(path) or not path.is_file():
        raise ValidationError("{} must be a regular non-symlink file".format(label))
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError("{} escapes its committed directory".format(label)) from exc
    return resolved


def generation_harness_paths() -> Tuple[Path, ...]:
    """Return every local source/schema file required by formal generation."""

    package_root = Path(__file__).resolve().parent
    paths = [
        path
        for path in package_root.rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    paths.extend(supported_schema_paths())
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValidationError(
            "generation harness dependency is missing: {}".format(missing[0])
        )
    return tuple(sorted((path.resolve() for path in paths), key=str))


def _validated_dependency_inventory(
    dependencies: Any, label: str
) -> List[Dict[str, str]]:
    if not isinstance(dependencies, list):
        raise ValidationError("{} must be an array".format(label))
    normalized: List[Dict[str, str]] = []
    names = []
    for dependency in dependencies:
        if (
            not isinstance(dependency, Mapping)
            or set(dependency) != {"name", "version"}
            or not isinstance(dependency.get("name"), str)
            or not dependency["name"]
            or not isinstance(dependency.get("version"), str)
            or not dependency["version"]
        ):
            raise ValidationError("{} entries are malformed".format(label))
        normalized.append(
            {"name": dependency["name"], "version": dependency["version"]}
        )
        names.append(dependency["name"])
    if normalized != sorted(normalized, key=lambda item: (item["name"], item["version"])):
        raise ValidationError("{} must be sorted by name and version".format(label))
    if len(names) != len(set(names)):
        raise ValidationError("{} distribution names must be unique".format(label))
    return normalized


def probe_python_environment(
    executable: Path, *, _pass_fds: Sequence[int] = ()
) -> Dict[str, Any]:
    """Measure one Python runtime with an isolated, fixed inventory probe."""

    executable = Path(executable)
    pass_fds = tuple(_pass_fds)
    sealed_fd_path = (
        len(pass_fds) == 1
        and isinstance(pass_fds[0], int)
        and pass_fds[0] >= 0
        and str(executable) == "/proc/self/fd/{}".format(pass_fds[0])
    )
    if pass_fds and not sealed_fd_path:
        raise ValidationError(
            "Python environment probe accepts only one exact inherited fd path"
        )
    if not pass_fds and (
        not executable.is_absolute()
        or _path_has_symlink_component(executable)
        or not executable.is_file()
    ):
        raise ValidationError(
            "Python environment probe requires an absolute regular non-symlink executable"
        )
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    try:
        completed = subprocess.run(
            [str(executable), "-I", "-c", _PYTHON_ENVIRONMENT_PROBE_CODE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_PYTHON_ENVIRONMENT_PROBE_TIMEOUT_SECONDS,
            env=environment,
            check=False,
            pass_fds=pass_fds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError("bound Python environment probe failed") from exc
    if (
        len(completed.stdout) > _PYTHON_ENVIRONMENT_PROBE_MAX_BYTES
        or len(completed.stderr) > _PYTHON_ENVIRONMENT_PROBE_MAX_BYTES
    ):
        raise ValidationError("bound Python environment probe exceeded its byte limit")
    if completed.returncode != 0:
        raise ValidationError("bound Python environment probe exited unsuccessfully")
    document = strict_json_object_bytes(
        completed.stdout, "bound Python environment probe output"
    )
    if set(document) != {"runtime_version", "locked_dependencies"}:
        raise ValidationError("bound Python environment probe output is malformed")
    runtime_version = document.get("runtime_version")
    if not isinstance(runtime_version, str) or not runtime_version:
        raise ValidationError("bound Python environment probe runtime_version is invalid")
    dependencies = _validated_dependency_inventory(
        document.get("locked_dependencies"),
        "bound Python environment probe locked_dependencies",
    )
    for dependency in dependencies:
        canonical_name = re.sub(r"[-_.]+", "-", dependency["name"]).lower()
        if dependency["name"] != canonical_name:
            raise ValidationError(
                "bound Python environment probe emitted a non-canonical distribution name"
            )
    return {
        "runtime_version": runtime_version,
        "locked_dependencies": dependencies,
    }


def python_environment_control_paths(environment_root: Path) -> Tuple[Path, ...]:
    """Return the exact files which control one repository-local Python venv.

    Package inventories describe installed distributions, but ``pyvenv.cfg``
    and site-packages control files decide which environment and editable
    source trees Python actually imports.  Bind these small files explicitly;
    the full environment directory is mounted read-only by the legacy adapter.
    """

    root = Path(environment_root)
    if (
        not root.is_absolute()
        or _path_has_symlink_component(root)
        or not root.is_dir()
    ):
        raise ValidationError(
            "python_environment_root must be an absolute non-symlink directory"
        )
    root = root.resolve()
    pyvenv = root / "pyvenv.cfg"
    if _path_has_symlink_component(pyvenv) or not pyvenv.is_file():
        raise ValidationError("python environment is missing a regular pyvenv.cfg")
    site_roots = sorted(
        path.resolve()
        for path in (root / "lib").glob("python*/site-packages")
        if path.is_dir() and not _path_has_symlink_component(path)
    )
    if len(site_roots) != 1:
        raise ValidationError(
            "python environment must contain exactly one regular site-packages root"
        )
    site_root = site_roots[0]
    controls = {pyvenv.resolve()}
    for pattern in _PYTHON_ENVIRONMENT_CONTROL_PATTERNS:
        controls.update(path.resolve() for path in site_root.glob(pattern))
    for path in site_root.glob("*.dist-info/direct_url.json"):
        controls.add(path.resolve())
    for hook_name in ("sitecustomize.py", "usercustomize.py"):
        hook = site_root / hook_name
        if hook.is_file():
            controls.add(hook.resolve())
    for pth_path in site_root.glob("*.pth"):
        controls.update(_pth_local_import_targets(site_root, pth_path))
    for path in controls:
        if _path_has_symlink_component(path) or not path.is_file():
            raise ValidationError(
                "python environment control files must be regular non-symlink files"
            )
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValidationError("python environment control file escapes its root") from exc
    return tuple(sorted(controls, key=str))


def _validate_python_environment_controls(
    manifest: Mapping[str, Any],
    executable: Path,
    implementation_paths: Optional[Mapping[str, Path]] = None,
    *,
    required: bool,
    label: str,
) -> Optional[Path]:
    """Validate an optional, exact venv root/control-file binding."""

    root_value = manifest.get("python_environment_root")
    controls_value = manifest.get("python_environment_control_files")
    if root_value is None and controls_value is None and not required:
        return None
    if not isinstance(root_value, str) or not root_value:
        raise ValidationError("{} python_environment_root is required".format(label))
    root = Path(root_value)
    expected_controls = python_environment_control_paths(root)
    if not isinstance(controls_value, list) or not controls_value:
        raise ValidationError(
            "{} python_environment_control_files must be non-empty".format(label)
        )
    observed_controls = []
    for index, binding in enumerate(controls_value):
        path = _live_file_binding(
            binding,
            "{} python_environment_control_files[{}]".format(label, index),
        )
        observed_controls.append(path)
        if implementation_paths is not None and str(path) not in implementation_paths:
            raise ValidationError(
                "{} Python environment control file is absent from implementation files".format(
                    label
                )
            )
    if observed_controls != sorted(observed_controls, key=str):
        raise ValidationError(
            "{} python_environment_control_files must be sorted".format(label)
        )
    if len(observed_controls) != len(set(observed_controls)):
        raise ValidationError(
            "{} python_environment_control_files contain duplicates".format(label)
        )
    if tuple(observed_controls) != expected_controls:
        raise ValidationError(
            "{} Python environment control-file binding is incomplete or stale".format(
                label
            )
        )
    executable = Path(executable).resolve()
    try:
        executable.relative_to(root.resolve() / "bin")
    except ValueError as exc:
        raise ValidationError(
            "{} executable is outside python_environment_root/bin".format(label)
        ) from exc
    return root.resolve()


def running_python_environment_executable() -> Path:
    """Return the exact regular launcher which started this process.

    Formal generation must be launched through the bound regular executable
    itself.  In particular, running through ``bin/python`` and then silently
    substituting the sibling ``python-frozen`` path would attest bytes which did
    not start the current process.
    """

    executable = Path(sys.executable)
    if (
        not executable.is_absolute()
        or _path_has_symlink_component(executable)
        or not executable.is_file()
    ):
        raise ValidationError(
            "running Python executable must be an absolute regular non-symlink file"
        )
    return executable.resolve()


def implementation_bundle_sha256(bundle: Mapping[str, Any]) -> str:
    """Hash the exact implementation bundle fields excluding its self hash."""

    if not isinstance(bundle, Mapping):
        raise ValidationError("implementation_bundle must be an object")
    payload = {
        "status": bundle.get("status"),
        "bundle_id": bundle.get("bundle_id"),
        "environment_manifest": bundle.get("environment_manifest"),
        "harness_environment_manifest": bundle.get(
            "harness_environment_manifest"
        ),
        "files": bundle.get("files"),
    }
    return sha256_bytes(canonical_json_bytes(payload))


def _validate_environment_manifest(
    path: Path,
    implementation_paths: Mapping[str, Path],
    *,
    formal: bool,
    label: str,
    require_python: bool = False,
    require_python_environment_root: bool = False,
) -> Dict[str, Any]:
    manifest = read_json(path)
    required_fields = {
        "schema_version",
        "decision_status",
        "runtime_kind",
        "executable",
        "runtime_version",
        "locked_dependencies",
    }
    optional_fields = {
        "python_environment_root",
        "python_environment_control_files",
    }
    if (
        not isinstance(manifest, Mapping)
        or not required_fields.issubset(manifest)
        or set(manifest) - required_fields - optional_fields
    ):
        raise ValidationError(
            "{} manifest has missing or unknown fields".format(label)
        )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError("{} schema_version is invalid".format(label))
    if manifest.get("decision_status") not in {"draft", "frozen"}:
        raise ValidationError("{} decision_status is invalid".format(label))
    if formal and manifest.get("decision_status") != "frozen":
        raise ValidationError(
            "formal generation requires a frozen {}".format(label)
        )
    runtime_kind = manifest.get("runtime_kind")
    if runtime_kind not in {"python", "native_command"}:
        raise ValidationError("{} runtime_kind is invalid".format(label))
    if require_python and runtime_kind != "python":
        raise ValidationError("{} runtime_kind must be python".format(label))
    if not isinstance(manifest.get("runtime_version"), str) or not manifest[
        "runtime_version"
    ]:
        raise ValidationError("{} runtime_version is required".format(label))
    executable = _live_file_binding(
        manifest.get("executable"), "{} executable".format(label)
    )
    if str(executable) not in implementation_paths:
        raise ValidationError(
            "{} executable is absent from implementation files".format(label)
        )
    dependencies = _validated_dependency_inventory(
        manifest.get("locked_dependencies"),
        "{} locked_dependencies".format(label),
    )
    environment_root = _validate_python_environment_controls(
        manifest,
        executable,
        implementation_paths,
        required=require_python_environment_root,
        label=label,
    )
    if formal and require_python_environment_root:
        # The draft ChatScene migration binds the launcher, package inventory,
        # startup controls and explicit CARLA egg, but not every third-party
        # package byte, standard-library file, or shared library loaded by the
        # interpreter.  Refuse formal status until that complete runtime-tree
        # closure is implemented and verified.
        raise ValidationError(
            "formal legacy Python environment requires a complete runtime-tree closure"
        )
    if formal and runtime_kind == "python":
        observed = probe_python_environment(executable)
        if manifest["runtime_version"] != observed["runtime_version"]:
            raise ValidationError(
                "{} runtime_version differs from the bound Python probe".format(
                    label
                )
            )
        if dependencies != observed["locked_dependencies"]:
            raise ValidationError(
                "{} locked_dependencies differ from the complete bound Python "
                "probe inventory".format(label)
            )
    return {
        "executable": executable,
        "runtime_kind": runtime_kind,
        "runtime_version": manifest["runtime_version"],
        "locked_dependencies": dependencies,
        "python_environment_root": environment_root,
    }


def _iter_snapshot_source_files(adapter: Mapping[str, Any]) -> Sequence[Path]:
    source_root = Path(adapter["source_root"])
    if _path_has_symlink_component(source_root) or not source_root.is_dir():
        raise ValidationError("legacy source_root must be a regular directory")
    source_root = source_root.resolve()
    files = set()
    for relative in adapter["snapshot_paths"]:
        source = (source_root / relative)
        if (
            _path_has_symlink_component(source)
            or not source.exists()
            or not (source.is_file() or source.is_dir())
        ):
            raise ValidationError(
                "legacy snapshot source must be an existing regular file or directory"
            )
        candidates = [source] if source.is_file() else source.rglob("*")
        for candidate in candidates:
            if candidate.is_symlink():
                raise ValidationError("legacy snapshot source must not contain symlinks")
            if not candidate.is_file() or any(
                part in {".git", "__pycache__", ".pytest_cache"}
                for part in candidate.relative_to(source_root).parts
            ):
                continue
            files.add(candidate.resolve())
    return tuple(sorted(files, key=str))


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _legacy_snapshot_bindings(
    config: Mapping[str, Any], *, formal: bool
) -> Optional[Tuple[Dict[str, Any], ...]]:
    """Select the exact frozen bindings copied by a formal legacy run.

    The bundle is the source of truth.  Re-enumerating only the files which
    still happen to exist would allow a deletion between bundle validation and
    adapter construction to shrink the attested snapshot.
    """

    adapter = config.get("adapter")
    if (
        not formal
        or not isinstance(adapter, Mapping)
        or adapter.get("type") != "chatscene_legacy"
    ):
        return None
    bundle = config.get("implementation_bundle")
    files = bundle.get("files") if isinstance(bundle, Mapping) else None
    if not isinstance(files, list):
        raise ValidationError("legacy implementation bundle files are malformed")
    source_root = Path(adapter["source_root"]).resolve()
    snapshot_roots = tuple(
        (source_root / relative).resolve() for relative in adapter["snapshot_paths"]
    )
    frozen: Dict[str, Dict[str, Any]] = {}
    for binding in files:
        if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str):
            raise ValidationError("legacy implementation bundle files are malformed")
        path = Path(binding["path"]).resolve()
        try:
            relative = path.relative_to(source_root)
        except ValueError:
            continue
        if any(
            path == root or _path_is_within(path, root)
            for root in snapshot_roots
        ) and not any(
            part in {".git", "__pycache__", ".pytest_cache"}
            for part in relative.parts
        ):
            frozen[str(path)] = dict(binding)
    if not frozen:
        raise ValidationError("formal legacy snapshot has no frozen file bindings")

    live_paths = {str(path) for path in _iter_snapshot_source_files(adapter)}
    frozen_paths = set(frozen)
    unbound_live = sorted(live_paths - frozen_paths)
    missing_frozen = sorted(frozen_paths - live_paths)
    if unbound_live:
        raise ValidationError(
            "legacy implementation bundle omits snapshot source files: {}".format(
                ", ".join(unbound_live[:3])
            )
        )
    if missing_frozen:
        raise ValidationError(
            "frozen legacy snapshot source files are missing: {}".format(
                ", ".join(missing_frozen[:3])
            )
        )
    return tuple(frozen[path] for path in sorted(frozen))


def validate_implementation_bundle(
    config: Mapping[str, Any], *, formal: bool
) -> str:
    """Re-hash the frozen implementation and verify snapshot coverage."""

    bundle = config.get("implementation_bundle")
    expected = {
        "status",
        "bundle_id",
        "bundle_sha256",
        "environment_manifest",
        "harness_environment_manifest",
        "files",
    }
    if not isinstance(bundle, Mapping) or set(bundle) != expected:
        raise ValidationError("method implementation_bundle is malformed")
    if bundle.get("status") not in {"draft", "frozen"}:
        raise ValidationError("implementation bundle status is invalid")
    if formal and bundle.get("status") != "frozen":
        raise ValidationError("formal generation requires a frozen implementation bundle")
    if not isinstance(bundle.get("bundle_id"), str) or not bundle["bundle_id"]:
        raise ValidationError("implementation bundle_id is required")
    digest = implementation_bundle_sha256(bundle)
    if bundle.get("bundle_sha256") != digest:
        raise ValidationError("implementation bundle self hash mismatch")
    files = bundle.get("files")
    if not isinstance(files, list) or not files:
        raise ValidationError("implementation bundle files must be non-empty")
    implementation_paths: Dict[str, Path] = {}
    ordered_paths = []
    schema_root = SCHEMA_DIRECTORY.resolve()
    supported_schemas = set(supported_schema_paths()) if formal else set()
    for index, binding in enumerate(files):
        path = _live_file_binding(binding, "implementation files[{}]".format(index))
        if (
            formal
            and _path_is_within(path, schema_root)
            and path not in supported_schemas
        ):
            raise ValidationError(
                "formal implementation bundle contains an unsupported benchmark schema: "
                "{}".format(path.name)
            )
        key = str(path)
        if key in implementation_paths:
            raise ValidationError("implementation file paths must be unique")
        implementation_paths[key] = path
        ordered_paths.append(key)
    if ordered_paths != sorted(ordered_paths):
        raise ValidationError("implementation files must be sorted by resolved path")
    environment_path = _live_file_binding(
        bundle.get("environment_manifest"), "implementation environment_manifest"
    )
    harness_environment_path = _live_file_binding(
        bundle.get("harness_environment_manifest"),
        "implementation harness_environment_manifest",
    )
    if environment_path == harness_environment_path:
        raise ValidationError(
            "method and harness environment manifests must be distinct files"
        )
    if formal:
        missing_harness_paths = [
            str(path)
            for path in generation_harness_paths()
            if str(path) not in implementation_paths
        ]
        if missing_harness_paths:
            raise ValidationError(
                "formal implementation bundle omits generation harness dependency: {}".format(
                    missing_harness_paths[0]
                )
            )
    method_environment = _validate_environment_manifest(
        environment_path,
        implementation_paths,
        formal=formal,
        label="method environment manifest",
        require_python_environment_root=(
            config.get("adapter", {}).get("type") == "chatscene_legacy"
        ),
    )
    harness_environment = _validate_environment_manifest(
        harness_environment_path,
        implementation_paths,
        formal=formal,
        label="harness environment manifest",
        require_python=True,
        require_python_environment_root=(
            config.get("adapter", {}).get("type") == "chatscene_legacy"
        ),
    )
    if formal and harness_environment["executable"] != running_python_environment_executable():
        raise ValidationError(
            "formal harness environment executable differs from the running interpreter"
        )

    adapter = config["adapter"]
    if formal and adapter.get("environment_passthrough"):
        raise ValidationError(
            "formal generation forbids unbound host environment passthrough"
        )
    if adapter.get("type") == "command":
        executable = Path(adapter["argv"][0])
        if not executable.is_absolute() or _path_has_symlink_component(executable):
            raise ValidationError(
                "command adapter executable must be an absolute non-symlink path"
            )
        executable = executable.resolve()
        if str(executable) not in implementation_paths:
            raise ValidationError("command executable is absent from implementation files")
        if executable != method_environment["executable"]:
            raise ValidationError(
                "command adapter executable differs from the method environment executable"
            )
        if formal and method_environment["runtime_kind"] != "python":
            raise ValidationError(
                "formal command adapters require a probed python method environment"
            )
        for argument in adapter["argv"][1:]:
            candidate = Path(argument)
            if candidate.is_absolute() and candidate.exists():
                if _path_has_symlink_component(candidate) or str(candidate.resolve()) not in implementation_paths:
                    raise ValidationError(
                        "absolute command file arguments must be frozen implementation files"
                    )
    elif adapter.get("type") == "chatscene_legacy":
        python_path = Path(adapter["python_executable"])
        if (
            not python_path.is_absolute()
            or _path_has_symlink_component(python_path)
            or str(python_path.resolve()) not in implementation_paths
        ):
            raise ValidationError("legacy Python executable is absent from implementation files")
        if method_environment["runtime_kind"] != "python":
            raise ValidationError(
                "legacy method environment manifest runtime_kind must be python"
            )
        if python_path.resolve() != method_environment["executable"]:
            raise ValidationError(
                "legacy Python executable differs from the method environment executable"
            )
        environment_root_value = adapter.get("python_environment_root")
        if not isinstance(environment_root_value, str) or not environment_root_value:
            raise ValidationError("legacy python_environment_root is required")
        environment_root = Path(environment_root_value)
        if (
            _path_has_symlink_component(environment_root)
            or not environment_root.is_absolute()
            or not environment_root.is_dir()
        ):
            raise ValidationError(
                "legacy python_environment_root must be an absolute non-symlink directory"
            )
        environment_root = environment_root.resolve()
        if method_environment["python_environment_root"] != environment_root:
            raise ValidationError(
                "legacy python_environment_root differs from the method environment"
            )
        python_path_entries = adapter.get("python_path_entries")
        if not isinstance(python_path_entries, list) or not python_path_entries:
            raise ValidationError("legacy python_path_entries must be non-empty")
        normalized_python_paths = []
        for index, binding in enumerate(python_path_entries):
            path = _live_file_binding(
                binding, "legacy python_path_entries[{}]".format(index)
            )
            if str(path) not in implementation_paths:
                raise ValidationError(
                    "legacy python_path_entries file is absent from implementation files"
                )
            normalized_python_paths.append(path)
        if normalized_python_paths != sorted(normalized_python_paths, key=str):
            raise ValidationError("legacy python_path_entries must be sorted")
        if len(normalized_python_paths) != len(set(normalized_python_paths)):
            raise ValidationError("legacy python_path_entries contain duplicates")
        sandbox_path = Path(adapter["sandbox_executable"])
        if (
            not sandbox_path.is_absolute()
            or _path_has_symlink_component(sandbox_path)
            or not sandbox_path.is_file()
            or str(sandbox_path.resolve()) not in implementation_paths
        ):
            raise ValidationError(
                "legacy sandbox_executable must be an absolute non-symlink "
                "implementation file"
            )
        entrypoint = (Path(adapter["source_root"]) / adapter["entrypoint"]).resolve()
        if str(entrypoint) not in implementation_paths:
            raise ValidationError("legacy entrypoint is absent from implementation files")
        snapshot_files = _iter_snapshot_source_files(adapter)
        missing = [str(path) for path in snapshot_files if str(path) not in implementation_paths]
        if formal and missing:
            raise ValidationError(
                "legacy implementation bundle omits snapshot source files: {}".format(
                    ", ".join(missing[:3])
                )
            )
    return digest


def _created_at_utc() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _safe_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix) else ".bin"


def _write_blob(
    staging_directory: Path,
    final_directory: Path,
    filename: str,
    payload: bytes,
) -> Dict[str, Any]:
    staging_path = staging_directory / filename
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    with staging_path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "path": str((final_directory / filename).resolve()),
        "sha256": sha256_bytes(payload),
        "bytes": len(payload),
    }


def _read_isolated_artifact(path: Path, workdir: Path, max_bytes: int) -> bytes:
    path = Path(path)
    if _path_has_symlink_component(path) or not path.is_file():
        raise AdapterError("adapter artifact must be a regular non-symlink file")
    resolved = path.resolve()
    try:
        resolved.relative_to(workdir.resolve())
    except ValueError as exc:
        raise AdapterError("adapter artifact escapes the isolated working directory") from exc
    if resolved.stat().st_size > max_bytes:
        raise AdapterError("adapter artifact exceeds its configured byte limit")
    return resolved.read_bytes()


def _remove_workspace(workdir: Path) -> None:
    if workdir.exists():
        shutil.rmtree(str(workdir), ignore_errors=True)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _finalization_policy_id(platform: str) -> str:
    if platform == "carla":
        return CARLA_FINALIZATION_POLICY_ID
    if platform == "metadrive":
        return METADRIVE_FINALIZATION_POLICY_ID
    raise ValidationError("generation platform is invalid")


def _finalize_ego_artifact(
    platform: str,
    raw_bytes: bytes,
    token_registry: Optional[Mapping[str, Any]] = None,
    token_registry_sha256: Optional[str] = None,
) -> EgoFinalization:
    if platform == "carla":
        return finalize_carla_ego_artifact(raw_bytes)
    if platform == "metadrive":
        if token_registry is None or token_registry_sha256 is None:
            raise ValidationError("MetaDrive finalization requires a bound token registry")
        return finalize_metadrive_ego_artifact(
            raw_bytes, token_registry, token_registry_sha256
        )
    raise ValidationError("generation platform is invalid")


def _final_artifact_name(platform: str) -> str:
    if platform == "carla":
        return "artifact.final.scenic"
    if platform == "metadrive":
        return "artifact.final.json"
    raise ValidationError("generation platform is invalid")


def _descriptor_bytes(
    descriptor: Mapping[str, Any],
    expected_directory: Optional[Path] = None,
    max_bytes: int = 67108864,
) -> bytes:
    path = Path(descriptor.get("path", ""))
    if not path.is_file() or _path_has_symlink_component(path):
        raise ValidationError("persisted generation evidence file is missing")
    if expected_directory is not None:
        try:
            path.resolve().relative_to(expected_directory.resolve())
        except ValueError as exc:
            raise ValidationError("persisted evidence path escapes its run directory") from exc
    if path.stat().st_size > max_bytes:
        raise ValidationError("persisted generation evidence exceeds its byte limit")
    payload = path.read_bytes()
    if len(payload) != descriptor.get("bytes") or sha256_bytes(payload) != descriptor.get("sha256"):
        raise ValidationError("persisted generation evidence hash mismatch")
    return payload


def _load_token_registry_binding(
    config: Mapping[str, Any], *, formal: bool
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if config.get("platform") != "metadrive":
        return None, None
    binding = config.get("token_registry")
    path = _live_file_binding(binding, "MetaDrive token_registry")
    try:
        registry, digest = metadrive_pg.load_token_registry(
            path,
            expected_sha256=binding["sha256"],
            formal=formal,
            verify_files=True,
        )
    except metadrive_pg.MetaDrivePGError as exc:
        raise ValidationError("invalid MetaDrive token registry: {}".format(exc)) from exc
    return registry, digest


class GenerationRunner:
    """Execute and atomically commit every cell in one frozen five-run grid."""

    def __init__(
        self,
        library_path: Path,
        roster: Mapping[str, Any],
        method_config_path: Path,
        output_root: Path,
        adapter: Optional[MethodAdapter] = None,
        development: bool = False,
        resume: bool = False,
    ) -> None:
        library_path = Path(library_path)
        if _path_has_symlink_component(library_path) or not library_path.is_file():
            raise ValidationError("query library must be a regular non-symlink file")
        self.library_path = library_path.resolve()
        self.roster = dict(roster)
        method_config_path = Path(method_config_path)
        if _path_has_symlink_component(method_config_path) or not method_config_path.is_file():
            raise ValidationError("method config must be a regular non-symlink file")
        self.method_config_path = method_config_path.resolve()
        config = read_json(self.method_config_path)
        validate_schema_instance(config, "method_config", context="generation method config")
        _validate_method_config(config, self.roster)
        self.development = bool(development)
        if self.roster.get("decision_status") not in ("draft", "confirmed"):
            raise ValidationError("generation roster decision_status is invalid")
        if not self.development:
            if config.get("status") != "frozen":
                raise ValidationError("formal generation requires a frozen method config")
            if self.roster.get("decision_status") != "confirmed":
                raise ValidationError("formal generation requires a confirmed roster")
            if adapter is not None:
                raise ValidationError("formal generation forbids a custom adapter instance")
        self.method_config = config
        self.config_sha256 = sha256_file(self.method_config_path)
        self.implementation_bundle_sha256 = validate_implementation_bundle(
            config, formal=not self.development
        )
        self.token_registry, self.token_registry_sha256 = _load_token_registry_binding(
            config, formal=not self.development
        )
        output_root = Path(output_root)
        if _path_has_symlink_component(output_root):
            raise ValidationError("generation output_root must not be a symlink")
        self.output_root = output_root.resolve()
        self.adapter = adapter or adapter_from_method_config(
            config,
            legacy_snapshot_bindings=_legacy_snapshot_bindings(
                config, formal=not self.development
            ),
        )
        self.resume = bool(resume)
        self.max_artifact_bytes = config["adapter"]["max_artifact_bytes"]
        self.jobs = build_generation_grid(self.library_path, self.roster)

    def run(self) -> List[Dict[str, Any]]:
        runs = self.output_root / "runs"
        staging_root = runs / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        responses = [self._run_job(job, runs, staging_root) for job in self.jobs]
        evidence = self.evidence_records(responses)
        self.run_manifest = self._commit_run_manifest(responses, evidence)
        return responses

    def _manifest_core(
        self,
        responses: Sequence[Mapping[str, Any]],
        evidence: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        if len(responses) != len(evidence) or len(responses) != len(self.jobs):
            raise ValidationError("generation manifest requires the complete response grid")
        records = []
        for response, evidence_record in zip(responses, evidence):
            raw = response.get("raw_method_response")
            stdout = raw.get("stdout") if isinstance(raw, Mapping) else None
            path = stdout.get("path") if isinstance(stdout, Mapping) else None
            if not isinstance(path, str) or not path:
                raise ValidationError("generation response has no committed run directory")
            run_directory = Path(path).resolve().parent
            try:
                run_directory.relative_to((self.output_root / "runs").resolve())
            except ValueError as exc:
                raise ValidationError("generation run directory escapes output_root") from exc
            records.append(
                {
                    "run_id": response["run_id"],
                    "run_directory": str(run_directory),
                    "response_record_sha256": record_sha256(response),
                    "evidence_record_sha256": record_sha256(evidence_record),
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "manifest_type": "generation_run_manifest_v0.1",
            "method_id": self.roster["method_id"],
            "platform": self.roster["platform"],
            "library_sha256": sha256_file(self.library_path),
            "roster_sha256": sha256_bytes(canonical_json_bytes(self.roster)),
            "config_sha256": self.config_sha256,
            "implementation_bundle_sha256": self.implementation_bundle_sha256,
            "record_count": len(records),
            "records": records,
        }

    def _validate_run_manifest(
        self,
        manifest: Mapping[str, Any],
        responses: Sequence[Mapping[str, Any]],
        evidence: Sequence[Mapping[str, Any]],
    ) -> None:
        expected_keys = {
            "schema_version",
            "manifest_type",
            "method_id",
            "platform",
            "library_sha256",
            "roster_sha256",
            "config_sha256",
            "implementation_bundle_sha256",
            "record_count",
            "records",
            "producer",
            "manifest_sha256",
        }
        if not isinstance(manifest, Mapping) or set(manifest) != expected_keys:
            raise ValidationError("generation run manifest is malformed")
        producer = manifest.get("producer")
        if (
            not isinstance(producer, Mapping)
            or set(producer) != {"producer_id", "producer_version", "created_at_utc"}
            or producer.get("producer_id") != PRODUCER_ID
            or producer.get("producer_version") != PRODUCER_VERSION
            or not isinstance(producer.get("created_at_utc"), str)
            or not producer["created_at_utc"]
        ):
            raise ValidationError("generation run manifest producer is malformed")
        without_hash = dict(manifest)
        manifest_hash = without_hash.pop("manifest_sha256")
        if manifest_hash != sha256_bytes(canonical_json_bytes(without_hash)):
            raise ValidationError("generation run manifest self hash mismatch")
        expected_core = self._manifest_core(responses, evidence)
        if any(manifest.get(key) != value for key, value in expected_core.items()):
            raise ValidationError("generation run manifest differs from committed records")

    def _commit_run_manifest(
        self,
        responses: Sequence[Mapping[str, Any]],
        evidence: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        path = self.output_root / "generation_run_manifest.json"
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValidationError("generation run manifest path is not a regular file")
            manifest = read_json(path)
            self._validate_run_manifest(manifest, responses, evidence)
            return dict(manifest)
        manifest = self._manifest_core(responses, evidence)
        manifest["producer"] = {
            "producer_id": PRODUCER_ID,
            "producer_version": PRODUCER_VERSION,
            "created_at_utc": _created_at_utc(),
        }
        manifest["manifest_sha256"] = sha256_bytes(canonical_json_bytes(manifest))
        ensure_immutable_json(path, manifest)
        self._validate_run_manifest(manifest, responses, evidence)
        return manifest

    def evidence_records(
        self, responses: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Load immutable per-run generation evidence in response order."""

        records = []
        for response in responses:
            raw = response.get("raw_method_response")
            stdout = raw.get("stdout") if isinstance(raw, Mapping) else None
            path = stdout.get("path") if isinstance(stdout, Mapping) else None
            if not isinstance(path, str) or not path:
                raise ValidationError("generation response lacks an evidence directory")
            run_directory = Path(path).resolve().parent
            evidence_path = _regular_contained_file(
                run_directory / "generation_evidence.json",
                run_directory,
                "generation evidence",
            )
            evidence = read_json(evidence_path)
            if (
                evidence.get("run_id") != response.get("run_id")
                or evidence.get("response_record_sha256") != record_sha256(response)
            ):
                raise ValidationError("generation evidence is not bound to its response")
            records.append(evidence)
        return records

    def _existing_response(self, job: GenerationJob, final_directory: Path) -> Dict[str, Any]:
        if final_directory.is_symlink() or not final_directory.is_dir():
            raise ValidationError("existing run path must be a regular directory")
        response_path = _regular_contained_file(
            final_directory / "response.json", final_directory, "terminal response"
        )
        response = read_json(response_path)
        validate_schema_instance(response, "response_record", context="generation response")
        expected = {
            "run_id": job.run_id,
            "method_id": job.method_id,
            "platform": job.platform,
            "query_id": job.query_id,
            "intent_group_id": job.intent_group_id,
            "statistical_intent_cluster_id": job.statistical_intent_cluster_id,
            "surface_style": job.surface_style,
            "repetition": job.repetition,
            "expected_support": job.expected_support,
            "request_sha256": job.request_sha256,
            "config_sha256": self.config_sha256,
            "implementation_bundle_sha256": self.implementation_bundle_sha256,
        }
        if any(response.get(field) != value for field, value in expected.items()):
            raise ValidationError("existing terminal response differs from the requested grid cell")
        raw = response.get("raw_method_response")
        if not isinstance(raw, Mapping):
            raise ValidationError("existing terminal response lacks raw_method_response")
        stdout = raw.get("stdout")
        stderr = raw.get("stderr")
        if not isinstance(stdout, Mapping) or not isinstance(stderr, Mapping):
            raise ValidationError("existing raw method streams are malformed")
        _descriptor_bytes(stdout, final_directory)
        _descriptor_bytes(stderr, final_directory)
        envelope = {
            "stdout": dict(stdout),
            "stderr": dict(stderr),
            "exit_code": raw.get("exit_code"),
            "timed_out": raw.get("timed_out"),
        }
        if raw.get("envelope_sha256") != sha256_bytes(canonical_json_bytes(envelope)):
            raise ValidationError("existing raw method envelope hash mismatch")
        descriptor = response.get("artifact")
        if descriptor is not None:
            if not isinstance(descriptor, Mapping):
                raise ValidationError("existing artifact descriptor is malformed")
            _descriptor_bytes(descriptor, final_directory)
        evidence_path = _regular_contained_file(
            final_directory / "generation_evidence.json",
            final_directory,
            "generation evidence",
        )
        evidence = read_json(evidence_path)
        if set(evidence) != {
            "schema_version",
            "run_id",
            "method_id",
            "platform",
            "query_id",
            "intent_group_id",
            "statistical_intent_cluster_id",
            "surface_style",
            "repetition",
            "expected_support",
            "terminal_status",
            "request_sha256",
            "config_sha256",
            "implementation_bundle_sha256",
            "response_record_sha256",
            "raw_artifact",
            "finalization",
            "failure_reason",
        } or evidence.get("schema_version") != SCHEMA_VERSION:
            raise ValidationError("existing generation evidence is malformed")
        if (
            evidence.get("run_id") != job.run_id
            or evidence.get("method_id") != job.method_id
            or evidence.get("platform") != job.platform
            or evidence.get("query_id") != job.query_id
            or evidence.get("intent_group_id") != job.intent_group_id
            or evidence.get("statistical_intent_cluster_id")
            != job.statistical_intent_cluster_id
            or evidence.get("surface_style") != job.surface_style
            or evidence.get("repetition") != job.repetition
            or evidence.get("expected_support") != job.expected_support
            or evidence.get("terminal_status") != response.get("terminal_status")
            or evidence.get("request_sha256") != job.request_sha256
            or evidence.get("config_sha256") != self.config_sha256
            or evidence.get("implementation_bundle_sha256")
            != self.implementation_bundle_sha256
            or evidence.get("response_record_sha256") != record_sha256(response)
        ):
            raise ValidationError("existing generation evidence differs from its response")
        raw_artifact = evidence.get("raw_artifact")
        if raw_artifact is not None:
            if not isinstance(raw_artifact, Mapping):
                raise ValidationError("existing raw artifact descriptor is malformed")
            raw_artifact_bytes = _descriptor_bytes(raw_artifact, final_directory)
        else:
            raw_artifact_bytes = None
        finalization = evidence.get("finalization")
        if finalization is not None and (
            not isinstance(finalization, Mapping)
            or finalization.get("policy_id") != _finalization_policy_id(job.platform)
            or finalization.get("status") not in {"complete", "failed"}
        ):
            raise ValidationError("existing generation finalization policy is malformed")
        requires_finalization = (
            response.get("terminal_status") == "complete"
            and response.get("disposition") in {"generate", "controlled_degradation"}
        )
        if requires_finalization and (
            not isinstance(finalization, Mapping)
            or finalization.get("status") != "complete"
            or not isinstance(finalization.get("diff"), Mapping)
        ):
            raise ValidationError("completed artifact lacks reproducible ego finalization evidence")
        if (
            response.get("terminal_status") == "complete"
            and response.get("disposition") in {"reject", "clarification"}
            and (raw_artifact is not None or finalization is not None)
        ):
            raise ValidationError("no-artifact disposition contains generation artifact evidence")
        if isinstance(finalization, Mapping) and isinstance(finalization.get("diff"), Mapping):
            diff_bytes = _descriptor_bytes(finalization["diff"], final_directory)
            if finalization.get("status") == "complete":
                expected_keys = {
                    "policy_id",
                    "status",
                    "raw_sha256",
                    "final_sha256",
                    "diff",
                    "changes",
                }
                artifact = response.get("artifact")
                if (
                    set(finalization) != expected_keys
                    or finalization.get("policy_id")
                    != _finalization_policy_id(job.platform)
                    or raw_artifact is None
                    or artifact is None
                    or finalization.get("raw_sha256") != raw_artifact.get("sha256")
                    or finalization.get("final_sha256") != artifact.get("sha256")
                ):
                    raise ValidationError("existing completed finalization binding is malformed")
                recomputed = _finalize_ego_artifact(
                    job.platform,
                    raw_artifact_bytes,
                    self.token_registry,
                    self.token_registry_sha256,
                )
                if (
                    recomputed.final_bytes != _descriptor_bytes(artifact, final_directory)
                    or recomputed.diff_bytes != diff_bytes
                    or list(recomputed.changes) != finalization.get("changes")
                ):
                    raise ValidationError("existing ego finalization cannot be reproduced")
        elif isinstance(finalization, Mapping):
            expected_keys = {
                "policy_id",
                "status",
                "raw_sha256",
                "final_sha256",
                "diff",
                "changes",
                "error",
            }
            if (
                set(finalization) != expected_keys
                or finalization.get("status") != "failed"
                or raw_artifact is None
                or finalization.get("raw_sha256") != raw_artifact.get("sha256")
                or finalization.get("final_sha256") is not None
                or finalization.get("diff") is not None
                or finalization.get("changes") != []
                or not isinstance(finalization.get("error"), str)
                or not finalization["error"]
            ):
                raise ValidationError("existing failed finalization binding is malformed")
        if response.get("terminal_status") == "complete":
            if evidence.get("failure_reason") is not None:
                raise ValidationError("completed generation evidence cannot contain a failure")
        elif not isinstance(evidence.get("failure_reason"), str) or not evidence[
            "failure_reason"
        ]:
            raise ValidationError("failed generation evidence requires a failure reason")
        return response

    def _run_job(
        self, job: GenerationJob, runs: Path, staging_root: Path
    ) -> Dict[str, Any]:
        final_directory = runs / job.run_id
        if final_directory.exists():
            if not self.resume:
                raise ValidationError(
                    "run {} already exists; use --resume to verify and reuse it".format(
                        job.run_id
                    )
                )
            return self._existing_response(job, final_directory)

        staging_directory = Path(tempfile.mkdtemp(prefix=".pending-", dir=str(staging_root)))
        workspace_root = Path(tempfile.mkdtemp(prefix="bus-method-"))
        workdir = workspace_root / "work"
        try:
            self._verify_live_method_inputs()
            try:
                outcome = self.adapter.generate(job.query_text, workdir)
                if not isinstance(outcome, AdapterOutcome):
                    raise AdapterError("adapter did not return AdapterOutcome")
            except Exception as exc:  # adapter failures are terminal benchmark outcomes
                message = "adapter raised {}: {}".format(type(exc).__name__, exc)
                outcome = AdapterOutcome(
                    b"",
                    message.encode("utf-8", errors="replace"),
                    None,
                    False,
                    error=message,
                )
            self._verify_live_method_inputs()
            response, evidence = self._build_response(
                job, outcome, staging_directory, final_directory, workdir
            )
            _remove_workspace(workspace_root)
            write_json(staging_directory / "generation_evidence.json", evidence)
            write_json(staging_directory / "response.json", response)
            _fsync_directory(staging_directory)
            try:
                os.rename(str(staging_directory), str(final_directory))
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) and not final_directory.exists():
                    raise
                shutil.rmtree(str(staging_directory), ignore_errors=True)
                if not self.resume:
                    raise ValidationError(
                        "run {} was committed concurrently; rerun with --resume".format(
                            job.run_id
                        )
                    )
                return self._existing_response(job, final_directory)
            _fsync_directory(runs)
            return response
        except Exception:
            _remove_workspace(workspace_root)
            if staging_directory.exists():
                shutil.rmtree(str(staging_directory), ignore_errors=True)
            raise

    def _verify_live_method_inputs(self) -> None:
        """Detect source/config/registry drift around every formal invocation."""

        if self.development:
            return
        if sha256_file(self.method_config_path) != self.config_sha256:
            raise ValidationError("method config changed during formal generation")
        if sha256_file(self.library_path) != self.roster.get("library_sha256"):
            raise ValidationError("query library changed during formal generation")
        digest = validate_implementation_bundle(self.method_config, formal=True)
        if digest != self.implementation_bundle_sha256:
            raise ValidationError("implementation bundle changed during formal generation")
        registry, registry_sha256 = _load_token_registry_binding(
            self.method_config, formal=True
        )
        if (
            registry_sha256 != self.token_registry_sha256
            or canonical_json_bytes(registry) != canonical_json_bytes(self.token_registry)
        ):
            raise ValidationError("MetaDrive token registry changed during generation")

    def _build_response(
        self,
        job: GenerationJob,
        outcome: AdapterOutcome,
        staging_directory: Path,
        final_directory: Path,
        workdir: Path,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if not isinstance(outcome.stdout, bytes) or not isinstance(outcome.stderr, bytes):
            raise AdapterError("adapter stdout/stderr must be raw bytes")
        if outcome.exit_code is not None and (
            isinstance(outcome.exit_code, bool) or not isinstance(outcome.exit_code, int)
        ):
            raise AdapterError("adapter exit_code must be an integer or null")
        stdout = _write_blob(
            staging_directory, final_directory, "stdout.bin", outcome.stdout
        )
        stderr = _write_blob(
            staging_directory, final_directory, "stderr.bin", outcome.stderr
        )
        envelope = {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": outcome.exit_code,
            "timed_out": bool(outcome.timed_out),
        }
        raw_method_response = dict(envelope)
        raw_method_response["envelope_sha256"] = sha256_bytes(
            canonical_json_bytes(envelope)
        )

        failure_reason = outcome.error
        if outcome.timed_out:
            failure_reason = failure_reason or "method invocation timed out"
        elif outcome.exit_code != 0:
            failure_reason = failure_reason or "method process did not exit successfully"
        elif outcome.disposition not in _COMPLETE_DISPOSITIONS:
            failure_reason = failure_reason or "adapter produced no valid terminal disposition"

        raw_artifact = None
        raw_bytes = None
        if outcome.artifact_path is not None:
            try:
                raw_bytes = _read_isolated_artifact(
                    outcome.artifact_path, workdir, self.max_artifact_bytes
                )
                suffix = _safe_suffix(Path(outcome.artifact_path))
                raw_artifact = _write_blob(
                    staging_directory,
                    final_directory,
                    "artifact.raw{}".format(suffix),
                    raw_bytes,
                )
            except (OSError, AdapterError) as exc:
                failure_reason = "raw artifact capture failed: {}".format(exc)

        disposition = outcome.disposition
        final_artifact = None
        finalization_record = None
        if failure_reason is None:
            needs_artifact = disposition in {"generate", "controlled_degradation"}
            if needs_artifact and raw_bytes is None:
                failure_reason = "{} disposition requires a raw artifact".format(disposition)
            elif not needs_artifact and raw_bytes is not None:
                failure_reason = "{} disposition forbids an artifact".format(disposition)

        if failure_reason is None and disposition in {"generate", "controlled_degradation"}:
            policy_id = _finalization_policy_id(job.platform)
            try:
                finalized = _finalize_ego_artifact(
                    job.platform,
                    raw_bytes,
                    self.token_registry,
                    self.token_registry_sha256,
                )
                final_artifact = _write_blob(
                    staging_directory,
                    final_directory,
                    _final_artifact_name(job.platform),
                    finalized.final_bytes,
                )
                diff = _write_blob(
                    staging_directory,
                    final_directory,
                    "finalization.diff",
                    finalized.diff_bytes,
                )
                finalization_record = {
                    "policy_id": policy_id,
                    "status": "complete",
                    "raw_sha256": raw_artifact["sha256"],
                    "final_sha256": final_artifact["sha256"],
                    "diff": diff,
                    "changes": list(finalized.changes),
                }
            except (OSError, UnicodeError, ValidationError) as exc:
                failure_reason = "ego finalization rejected the artifact: {}".format(exc)
                finalization_record = {
                    "policy_id": policy_id,
                    "status": "failed",
                    "raw_sha256": raw_artifact["sha256"] if raw_artifact else None,
                    "final_sha256": None,
                    "diff": None,
                    "changes": [],
                    "error": str(exc),
                }

        unsupported_handling = None
        if failure_reason is None and disposition in _UNSUPPORTED_DISPOSITIONS:
            stream = outcome.unsupported_response_stream
            if stream not in ("stdout", "stderr"):
                failure_reason = "unsupported disposition lacks a bound response stream"
            else:
                descriptor = stdout if stream == "stdout" else stderr
                unsupported_handling = {
                    "response_stream": stream,
                    "response_sha256": descriptor["sha256"],
                }
        elif disposition not in _UNSUPPORTED_DISPOSITIONS and outcome.unsupported_response_stream is not None:
            failure_reason = "unsupported response stream is forbidden for this disposition"

        terminal_status = "complete"
        if outcome.timed_out:
            terminal_status = "timeout"
        elif failure_reason is not None:
            terminal_status = "failed"
        if terminal_status != "complete":
            disposition = "failure"
            final_artifact = None
            unsupported_handling = None

        response = {
            "schema_version": SCHEMA_VERSION,
            "run_id": job.run_id,
            "method_id": job.method_id,
            "platform": job.platform,
            "query_id": job.query_id,
            "intent_group_id": job.intent_group_id,
            "statistical_intent_cluster_id": job.statistical_intent_cluster_id,
            "surface_style": job.surface_style,
            "repetition": job.repetition,
            "expected_support": job.expected_support,
            "disposition": disposition,
            "request_sha256": job.request_sha256,
            "config_sha256": self.config_sha256,
            "implementation_bundle_sha256": self.implementation_bundle_sha256,
            "terminal_status": terminal_status,
            "artifact": final_artifact,
            "raw_method_response": raw_method_response,
            "provenance": {
                "producer_id": PRODUCER_ID,
                "producer_version": PRODUCER_VERSION,
                "created_at_utc": _created_at_utc(),
            },
        }
        if unsupported_handling is not None:
            response["unsupported_handling"] = unsupported_handling
        validate_schema_instance(response, "response_record", context="generation response")
        evidence = {
            "schema_version": SCHEMA_VERSION,
            "run_id": job.run_id,
            "method_id": job.method_id,
            "platform": job.platform,
            "query_id": job.query_id,
            "intent_group_id": job.intent_group_id,
            "statistical_intent_cluster_id": job.statistical_intent_cluster_id,
            "surface_style": job.surface_style,
            "repetition": job.repetition,
            "expected_support": job.expected_support,
            "terminal_status": terminal_status,
            "request_sha256": job.request_sha256,
            "config_sha256": self.config_sha256,
            "implementation_bundle_sha256": self.implementation_bundle_sha256,
            "response_record_sha256": record_sha256(response),
            "raw_artifact": raw_artifact,
            "finalization": finalization_record,
            "failure_reason": failure_reason,
        }
        return response, evidence


def run_generation(
    library_path: Path,
    roster: Mapping[str, Any],
    method_config_path: Path,
    output_root: Path,
    adapter: Optional[MethodAdapter] = None,
    development: bool = False,
    resume: bool = False,
) -> List[Dict[str, Any]]:
    """Convenience API for one complete, immutable query-by-five execution."""

    return GenerationRunner(
        library_path,
        roster,
        method_config_path,
        output_root,
        adapter=adapter,
        development=development,
        resume=resume,
    ).run()


def ensure_immutable_json(path: Path, record: Mapping[str, Any]) -> bool:
    """Create one canonical immutable JSON object, never replacing bytes."""

    path = Path(path)
    if _path_has_symlink_component(path):
        raise ValidationError("immutable JSON output path must not contain symlinks")
    path = path.resolve()
    payload = canonical_json_bytes(dict(record)) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise ValidationError("immutable JSON output already exists with different bytes")
        return False
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}-".format(path.name), dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(str(temporary), str(path))
        except FileExistsError:
            if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
                raise ValidationError(
                    "immutable JSON output was concurrently created with different bytes"
                )
            return False
        _fsync_directory(path.parent)
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def validate_generation_response_chain(
    library_path: Path,
    roster: Mapping[str, Any],
    method_config_path: Path,
    output_root: Path,
    *,
    responses: Optional[Sequence[Mapping[str, Any]]] = None,
    evidence: Optional[Sequence[Mapping[str, Any]]] = None,
    manifest: Optional[Mapping[str, Any]] = None,
    development: bool = False,
) -> Dict[str, Any]:
    """Validate the complete committed generation chain without executing it.

    This is the single score/UQH admission API.  It re-hashes live method
    implementation inputs, checks the exact roster-by-five grid, loads every
    per-run response/evidence file, replays the finalizer, and verifies the run
    manifest.  Optional in-memory indexes must match the committed records
    byte-for-byte at the JSON-value level.
    """

    runner = GenerationRunner(
        library_path,
        roster,
        method_config_path,
        output_root,
        development=development,
        resume=True,
    )
    committed_responses = []
    for job in runner.jobs:
        committed_responses.append(
            runner._existing_response(job, runner.output_root / "runs" / job.run_id)
        )
    committed_evidence = runner.evidence_records(committed_responses)

    def require_same_records(
        supplied: Sequence[Mapping[str, Any]],
        committed: Sequence[Mapping[str, Any]],
        label: str,
    ) -> None:
        if len(supplied) != len(committed) or any(
            canonical_json_bytes(dict(left)) != canonical_json_bytes(dict(right))
            for left, right in zip(supplied, committed)
        ):
            raise ValidationError("{} differs from committed generation records".format(label))

    if responses is not None:
        require_same_records(list(responses), committed_responses, "response index")
    response_index = runner.output_root / "response.jsonl"
    if not development and not response_index.is_file():
        raise ValidationError("formal generation response index is missing")
    if response_index.exists():
        if _path_has_symlink_component(response_index) or not response_index.is_file():
            raise ValidationError("response index must be a regular non-symlink file")
        require_same_records(
            read_jsonl(response_index), committed_responses, "persisted response index"
        )
    if evidence is not None:
        require_same_records(list(evidence), committed_evidence, "evidence index")
    evidence_index = runner.output_root / "evidence.jsonl"
    if not development and not evidence_index.is_file():
        raise ValidationError("formal generation evidence index is missing")
    if evidence_index.exists():
        if _path_has_symlink_component(evidence_index) or not evidence_index.is_file():
            raise ValidationError("evidence index must be a regular non-symlink file")
        require_same_records(
            read_jsonl(evidence_index), committed_evidence, "persisted evidence index"
        )

    manifest_path = runner.output_root / "generation_run_manifest.json"
    if _path_has_symlink_component(manifest_path) or not manifest_path.is_file():
        raise ValidationError("generation run manifest is missing")
    manifest_value = read_json(manifest_path)
    if manifest is not None and canonical_json_bytes(dict(manifest)) != canonical_json_bytes(
        manifest_value
    ):
        raise ValidationError("supplied run manifest differs from the committed manifest")
    runner._validate_run_manifest(
        manifest_value, committed_responses, committed_evidence
    )
    return {
        "method_id": runner.roster["method_id"],
        "platform": runner.roster["platform"],
        "record_count": len(committed_responses),
        "manifest_sha256": manifest_value["manifest_sha256"],
        "implementation_bundle_sha256": runner.implementation_bundle_sha256,
    }


def ensure_immutable_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> bool:
    """Create one canonical JSONL index without ever replacing existing bytes.

    Returns ``True`` when this call created the file and ``False`` when an
    identical immutable index already existed during resume.
    """

    path = Path(path)
    if _path_has_symlink_component(path):
        raise ValidationError("immutable JSONL output path must not contain symlinks")
    path = path.resolve()
    payload = b"\n".join(canonical_json_bytes(dict(record)) for record in records)
    if records:
        payload += b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise ValidationError("immutable JSONL output already exists with different bytes")
        return False

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}-".format(path.name), dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(str(temporary), str(path))
        except FileExistsError:
            if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
                raise ValidationError(
                    "immutable JSONL output was concurrently created with different bytes"
                )
            return False
        _fsync_directory(path.parent)
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "EgoFinalization",
    "GenerationJob",
    "GenerationRunner",
    "build_generation_grid",
    "ensure_immutable_json",
    "ensure_immutable_jsonl",
    "finalize_carla_ego_artifact",
    "finalize_metadrive_ego_artifact",
    "generation_harness_paths",
    "probe_python_environment",
    "run_generation",
    "implementation_bundle_sha256",
    "validate_generation_response_chain",
    "validate_implementation_bundle",
    "verify_carla_ego_finalization",
    "verify_metadrive_ego_finalization",
]
