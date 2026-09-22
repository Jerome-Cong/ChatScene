"""Hash-addressed benchmark freeze manifests and locked-test gate checks."""

import datetime as _datetime
import copy
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from .calibration import (
    CALIBRATION_ITEMS_PER_PLATFORM,
    CALIBRATION_PLATFORMS,
    CALIBRATION_SELECTION_SEED,
    build_judge_calibration_roster,
    compute_judge_calibration,
)
from .constants import ATOM_CATEGORIES, BENCHMARK_VERSION, SCHEMA_VERSION
from .constants import CARLA_EGO_BLUEPRINT, EGO_PROXY_LENGTH_M, EGO_PROXY_WIDTH_M
from .cpd import compute_coverage, validate_cpd_policy_routing
from .decisions import apply_statistical_cluster, statistical_cluster_map
from .errors import FreezeError, ValidationError
from .jsonio import (
    canonical_json_bytes,
    iter_jsonl,
    read_json,
    sha256_bytes,
    sha256_file,
    strict_json_object_bytes,
)
from .judge import (
    evidence_projection,
    judge_config_sha256,
    judge_item_id,
    judge_request_sha256,
    judge_response_id,
    parse_raw_judge_response,
)
from .library import validate_library
from .metrics import atoms_match, deterministic_atom_verdict
from .metadrive_pg import MetaDrivePGError, load_token_registry, parse_artifact
from .metadrive_pg_contract import (
    PG_BLOCK_SCENE_CONTRACT,
    build_metadrive_capability_audit_subject,
    build_metadrive_fixture_audit_subject,
    build_metadrive_registry_audit_subject,
    validate_metadrive_pg_registry,
    validate_metadrive_registry_fixture_coverage,
)
from .oracle import REQUIRED_REVIEW_CHECKS
from .ontology import build_common_ontology, deterministic_representative_arguments
from .roster import roster_entries, validate_records_against_roster
from .schema import (
    SCHEMA_DIRECTORY,
    load_schema,
    supported_schema_paths,
    validate_schema_instance,
    validate_schema_records,
)
from .paths import workspace_root


REQUIRED_FREEZE_ROLES = {
    "query_library_test",
    "query_library_dev",
    "requirement_oracle_test",
    "requirement_oracle_dev",
    "cpd_policy",
    "common_ontology_registry",
    "statistical_clusters",
    "platform_capability_carla",
    "platform_capability_metadrive",
    "metadrive_pg_token_registry",
    "metadrive_pg_block_registry",
    "semantic_extractor_manifest",
    "semantic_fixture_corpus",
    "cross_platform_fixture_corpus",
    "judge_runner_manifest",
    "judge_rubric",
    "judge_calibration_source_config",
    "judge_calibration_roster",
    "development_query_roster",
    "development_response_records",
    "development_evidence_records",
    "judge_calibration_context_manifest",
    "judge_calibration_request_bundle_manifest",
    "judge_calibration_run_manifest",
    "uqh_assessor_registry",
    "platform_fixture_review",
    "protocol_decision_review",
    "human_query_gold",
    "method_registry",
    "method_config",
    "query_roster",
    "platform_config_carla",
    "platform_config_metadrive",
    "controller_config_carla",
    "controller_config_metadrive",
    "ego_proxy_config",
}

REPEATABLE_FREEZE_ROLES = {
    "method_config",
    "query_roster",
    "judge_calibration_source_config",
    "development_query_roster",
    "development_response_records",
    "development_evidence_records",
}

REQUIRED_PROTOCOL = {
    "repetitions": 5,
    "sv_seeds": [0, 1, 2, 3, 4],
    "sv_max_iterations": 2000,
    "rollout_seconds": 30.0,
    "query_input_fields": ["query_text"],
    "post_output_repair": False,
    "test_library_locked": True,
    "benchmark_suite_visibility": "design_visible",
    "held_out_generalization_claim": False,
}

UNFINISHED_STATUSES = {"draft", "pending", "todo", "running", "unreviewed", "unresolved"}

EXTRACTOR_PROJECTION_FIELDS = (
    "ego",
    "atoms",
    "common_atoms",
    "complete_categories",
)


def _require_formal_judge_calibration_profile(
    calibration_context: Mapping[str, Any]
) -> None:
    if (
        calibration_context.get("calibration_profile")
        != "cross_platform_final"
        or calibration_context.get("formal_freeze_eligible") is not True
    ):
        raise FreezeError(
            "formal freeze requires the cross-platform final Judge calibration profile"
        )


EXTRACTOR_TIMEOUT_SECONDS = 10.0
PROTOCOL_DECISION_IDS = (
    "BENCHMARK_SUITE_VISIBILITY",
    "DEV_TEST_OVERLAP",
    "TEST_DUPLICATE_CLUSTERS",
    "B08_SUPPORTED_REJECTION",
    "ACTOR_CARDINALITY",
    "LANE_TRISTATE",
    "RISK_METADATA",
    "EVENT_ONTOLOGY",
    "CPD_ELIGIBILITY",
    "JUDGE_CALIBRATION_ROSTER",
    "RUNTIME_VALIDATION_CARLA",
    "RUNTIME_VALIDATION_METADRIVE",
    "EGO_PROXY_AND_CONTROLLERS",
)

_ACTIVE_NESTED_BINDING_SNAPSHOTS = None


def _expected_protocol_decision_subjects(
    role_sha256: Mapping[str, List[str]],
    extractor: Mapping[str, Any],
) -> Dict[str, Mapping[str, Any]]:
    """Build exact one-time decisions which humans must approve before freeze."""

    def bindings(*roles: str) -> Mapping[str, Any]:
        result = {}
        for role in roles:
            values = sorted(role_sha256.get(role, []))
            if not values:
                raise FreezeError(
                    "protocol decision subject lacks {} asset binding".format(role)
                )
            result[role] = values
        return result

    environments = {
        value.get("platform"): value
        for value in extractor.get("runtime_environments", [])
        if isinstance(value, Mapping)
    }

    def runtime_subject(platform: str, validation_level: str) -> Mapping[str, Any]:
        environment = environments.get(platform)
        if not isinstance(environment, Mapping):
            raise FreezeError(
                "protocol decision subject lacks {} runtime environment".format(
                    platform
                )
            )
        return {
            "decision": "approve_declared_runtime_validation_level",
            "platform": platform,
            "validation_level": validation_level,
            "runtime_environment_sha256": sha256_bytes(
                canonical_json_bytes(environment)
            ),
            "asset_sha256": bindings("semantic_extractor_manifest"),
        }

    oracle_bindings = bindings(
        "requirement_oracle_test", "requirement_oracle_dev"
    )
    return {
        "BENCHMARK_SUITE_VISIBILITY": {
            "decision": "treat_252_query_suite_as_design_visible_and_locked_after_freeze",
            "benchmark_suite_visibility": "design_visible",
            "held_out_generalization_claim": False,
            "asset_sha256": bindings("query_library_test"),
        },
        "DEV_TEST_OVERLAP": {
            "decision": "accept_all_16_development_intents_as_semantically_non_overlapping",
            "development_intent_count": 16,
            "test_intent_count": 84,
            "asset_sha256": bindings("query_library_test", "query_library_dev"),
        },
        "TEST_DUPLICATE_CLUSTERS": {
            "decision": "keep_all_queries_and_apply_only_frozen_statistical_clusters",
            "asset_sha256": bindings("statistical_clusters"),
        },
        "B08_SUPPORTED_REJECTION": {
            "decision": "freeze_supported_acceptance_and_independent_unsupported_handling_assessment",
            "controlled_degradation_credit_v0_1": False,
            "missing_required_assessment_is_pipeline_error": True,
            "asset_sha256": bindings(
                "requirement_oracle_test",
                "uqh_assessor_registry",
                "semantic_extractor_manifest",
            ),
        },
        "ACTOR_CARDINALITY": {
            "decision": "integer_exact_multiple_at_least_two_optional_permitted_zero_forbidden",
            "asset_sha256": oracle_bindings,
        },
        "LANE_TRISTATE": {
            "decision": "missing_unknown_optional_permitted_false_not_forbidden_without_surface_text",
            "asset_sha256": oracle_bindings,
        },
        "RISK_METADATA": {
            "decision": "score_only_when_surface_expresses_risk_otherwise_analysis_tag",
            "asset_sha256": oracle_bindings,
        },
        "EVENT_ONTOLOGY": {
            "decision": "approve_event_state_trigger_constraint_outcome_classification_and_bindings",
            "asset_sha256": oracle_bindings,
        },
        "CPD_ELIGIBILITY": {
            "decision": "approve_dev_only_common_ontology_and_query_level_cpd_eligibility",
            "asset_sha256": bindings(
                "cpd_policy",
                "common_ontology_registry",
                "platform_capability_carla",
                "platform_capability_metadrive",
                "metadrive_pg_token_registry",
                "metadrive_pg_block_registry",
                "semantic_fixture_corpus",
                "cross_platform_fixture_corpus",
            ),
        },
        "JUDGE_CALIBRATION_ROSTER": {
            "decision": "approve_fixed_platform_balanced_label_blind_calibration_protocol",
            "asset_sha256": bindings(
                "judge_rubric",
                "judge_runner_manifest",
                "judge_calibration_source_config",
                "judge_calibration_roster",
                "development_query_roster",
                "development_response_records",
                "development_evidence_records",
                "judge_calibration_context_manifest",
                "judge_calibration_request_bundle_manifest",
                "judge_calibration_run_manifest",
            ),
        },
        "RUNTIME_VALIDATION_CARLA": runtime_subject(
            "carla", "native_compile_and_five_seed_static_sv"
        ),
        "RUNTIME_VALIDATION_METADRIVE": runtime_subject(
            "metadrive", "native_pgmap_reset_step"
        ),
        "EGO_PROXY_AND_CONTROLLERS": {
            "decision": "approve_fixed_query_blind_controllers_and_dimensioned_bus_proxy",
            "asset_sha256": bindings(
                "ego_proxy_config",
                "controller_config_carla",
                "controller_config_metadrive",
            ),
        },
    }


def _asset_record(path: Path, role: str) -> Dict[str, Any]:
    path = Path(path).resolve()
    if not path.is_file():
        raise FreezeError("freeze asset does not exist: {}".format(path))
    return {"role": role, "path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}


def _assert_asset_records_unchanged(records: Iterable[Mapping[str, Any]]) -> None:
    """Recheck frozen bytes after live validation to close the local TOCTOU window."""

    mismatches = []
    for record in records:
        path = Path(str(record.get("path", "")))
        if not path.is_file():
            mismatches.append({"path": str(path), "reason": "missing"})
            continue
        if sha256_file(path) != record.get("sha256"):
            mismatches.append({"path": str(path), "reason": "sha256_mismatch"})
            continue
        if path.stat().st_size != record.get("bytes"):
            mismatches.append({"path": str(path), "reason": "byte_count_mismatch"})
    if mismatches:
        raise FreezeError("freeze assets changed during validation: {}".format(mismatches))


def _unfinished_paths(value: Any, path: str = "$") -> List[str]:
    unfinished = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = "{}.{}".format(path, key)
            if (
                isinstance(child, str)
                and (key == "status" or key.endswith("_status"))
                and child.lower() in UNFINISHED_STATUSES
            ):
                unfinished.append(child_path)
            unfinished.extend(_unfinished_paths(child, child_path))
        return unfinished
    if isinstance(value, list):
        for index, child in enumerate(value):
            unfinished.extend(_unfinished_paths(child, "{}[{}]".format(path, index)))
    return unfinished


def _read_decision_document(path: Path) -> Any:
    if path.suffix == ".json":
        return read_json(path)
    if path.suffix == ".jsonl":
        return [record for _, record in iter_jsonl(path)]
    return None


def _validate_frozen_contract(roles: set, protocol: Mapping[str, Any]) -> None:
    missing_roles = sorted(REQUIRED_FREEZE_ROLES - roles)
    if missing_roles:
        raise FreezeError("freeze is missing required asset roles: {}".format(missing_roles))
    mismatches = {
        key: {"expected": expected, "actual": protocol.get(key)}
        for key, expected in REQUIRED_PROTOCOL.items()
        if protocol.get(key) != expected
    }
    if mismatches:
        raise FreezeError("freeze protocol mismatch: {}".format(mismatches))


def _load_role_documents(records: List[Mapping[str, Any]]) -> Dict[str, List[Any]]:
    documents = {}
    seen_paths = set()
    for record in records:
        path = str(Path(record["path"]).resolve())
        if path in seen_paths:
            raise FreezeError("one asset path cannot impersonate multiple freeze roles: {}".format(path))
        seen_paths.add(path)
        role = record["role"]
        document = _read_decision_document(Path(path))
        if document is None:
            raise FreezeError("formal freeze assets must be JSON or JSONL: {}".format(path))
        documents.setdefault(role, []).append(document)
    for role in REQUIRED_FREEZE_ROLES - REPEATABLE_FREEZE_ROLES:
        if len(documents.get(role, [])) != 1:
            raise FreezeError("freeze role {} must occur exactly once".format(role))
    for role in REPEATABLE_FREEZE_ROLES:
        if not documents.get(role):
            raise FreezeError("freeze role {} must occur at least once".format(role))
    return documents


def _require(document: Mapping[str, Any], **expected: Any) -> None:
    mismatches = {
        key: {"expected": value, "actual": document.get(key)}
        for key, value in expected.items()
        if document.get(key) != value
    }
    if mismatches:
        raise FreezeError("freeze asset contract mismatch: {}".format(mismatches))


def _record_map(rows: Any, label: str) -> Dict[str, Mapping[str, Any]]:
    if not isinstance(rows, list):
        raise FreezeError("{} must be a JSONL record list".format(label))
    result = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise FreezeError("{} contains a non-object record".format(label))
        query_id = row.get("query_id")
        if not query_id or query_id in result:
            raise FreezeError("{} query IDs must be unique and non-empty".format(label))
        result[query_id] = row
    return result


def _verify_file_binding(binding: Any, label: str) -> Path:
    if not isinstance(binding, Mapping):
        raise FreezeError("{} must contain a path and sha256".format(label))
    path = Path(str(binding.get("path", ""))).resolve()
    expected_hash = binding.get("sha256")
    if not path.is_file() or not isinstance(expected_hash, str):
        raise FreezeError("{} references a missing or unhashed file".format(label))
    if path.stat().st_size == 0:
        raise FreezeError("{} references an empty file".format(label))
    if sha256_file(path) != expected_hash:
        raise FreezeError("{} file hash mismatch".format(label))
    if _ACTIVE_NESTED_BINDING_SNAPSHOTS is not None:
        _ACTIVE_NESTED_BINDING_SNAPSHOTS.append(
            {
                "path": str(path),
                "sha256": expected_hash,
                "bytes": path.stat().st_size,
            }
        )
    return path


def _verified_metadrive_registry_dependency(
    binding: Any, label: str
) -> Dict[str, Any]:
    """Normalize one live-verified MetaDrive registry into extractor input form."""

    path = _verify_file_binding(binding, label)
    if binding.get("bytes") != path.stat().st_size:
        raise FreezeError("{} byte count mismatch".format(label))
    return {
        "kind": "metadrive_pg_token_registry",
        "path": str(path),
        "sha256": binding["sha256"],
        "bytes": binding["bytes"],
    }


def _require_exact_metadrive_registry_binding(
    binding: Any,
    frozen_asset: Any,
    label: str,
) -> Dict[str, Any]:
    """Require a nested binding to be the unique globally frozen registry."""

    expected = _verified_metadrive_registry_dependency(
        frozen_asset, "frozen MetaDrive token registry"
    )
    actual = _verified_metadrive_registry_dependency(binding, label)
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise FreezeError("{} differs from the frozen registry asset".format(label))
    return actual


def tracked_source_tree_sha256(repository_path: Path, tracked_subpath: str) -> str:
    """Hash the bytes and names of all git-tracked files below one source subtree."""

    repository = Path(repository_path).resolve()
    if not repository.is_dir() or not isinstance(tracked_subpath, str) or not tracked_subpath:
        raise FreezeError("runtime source tree binding is malformed")
    try:
        process = subprocess.run(
            ["git", "-C", str(repository), "ls-files", "-z", "--", tracked_subpath],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FreezeError("runtime source tree cannot be enumerated") from exc
    if process.returncode != 0:
        raise FreezeError("runtime source tree is not a readable git worktree")
    relative_paths = sorted(
        value.decode("utf-8")
        for value in process.stdout.split(b"\0")
        if value
    )
    if not relative_paths:
        raise FreezeError("runtime source subtree contains no tracked files")
    records = []
    for relative_path in relative_paths:
        path = (repository / relative_path).resolve()
        try:
            path.relative_to(repository)
        except ValueError as exc:
            raise FreezeError("runtime source file escapes its repository") from exc
        if not path.is_file():
            raise FreezeError("runtime source binding references a missing tracked file")
        digest = sha256_file(path)
        size = path.stat().st_size
        records.append(
            {"path": relative_path, "sha256": digest, "bytes": size}
        )
        if _ACTIVE_NESTED_BINDING_SNAPSHOTS is not None:
            _ACTIVE_NESTED_BINDING_SNAPSHOTS.append(
                {"path": str(path), "sha256": digest, "bytes": size}
            )
    return sha256_bytes(canonical_json_bytes(records))


def _git_revision(repository_path: Path) -> str:
    try:
        process = subprocess.run(
            ["git", "-C", str(Path(repository_path).resolve()), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FreezeError("runtime source revision cannot be read") from exc
    revision = process.stdout.decode("ascii", errors="ignore").strip()
    if process.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise FreezeError("runtime source revision is invalid")
    return revision


def _probe_python_runtime(interpreter: Path) -> Mapping[str, Any]:
    code = (
        "import importlib.metadata as m,json,sys;"
        "norm=lambda value:value.lower().replace('_','-');"
        "pairs=sorted((norm(d.metadata['Name']),d.version) for d in m.distributions() if d.metadata.get('Name'));"
        "print(json.dumps({'python_version':sys.version.split()[0],"
        "'distributions':dict(pairs)},sort_keys=True))"
    )
    try:
        process = subprocess.run(
            [str(interpreter), "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "MPLCONFIGDIR": "/tmp",
                "PYTHONHASHSEED": "0",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FreezeError("runtime environment probe could not execute") from exc
    if process.returncode != 0:
        raise FreezeError("runtime environment probe failed")
    try:
        value = json.loads(process.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FreezeError("runtime environment probe returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise FreezeError("runtime environment probe returned a non-object")
    return value


def _validate_runtime_environments(
    extractor: Mapping[str, Any],
    deterministic_fixture_corpus: Mapping[str, Any],
) -> None:
    environments = extractor.get("runtime_environments")
    if not isinstance(environments, list) or len(environments) != 2:
        raise FreezeError("extractor must freeze exactly two runtime environments")
    by_platform = {
        value.get("platform"): value
        for value in environments
        if isinstance(value, Mapping)
    }
    if set(by_platform) != {"carla", "metadrive"} or len(by_platform) != 2:
        raise FreezeError("runtime environments require unique CARLA and MetaDrive entries")
    for platform, expected_level in (
        ("carla", "native_compile_and_five_seed_static_sv"),
        ("metadrive", "native_pgmap_reset_step"),
    ):
        environment = by_platform[platform]
        if environment.get("validation_level") != expected_level:
            raise FreezeError("{} runtime validation level is overstated".format(platform))
        interpreter = _verify_file_binding(
            environment.get("interpreter"), "{} runtime interpreter".format(platform)
        )
        distributions = environment.get("distributions")
        if not isinstance(distributions, Mapping) or not distributions:
            raise FreezeError("{} runtime distributions are missing".format(platform))
        live = _probe_python_runtime(interpreter)
        if (
            live.get("python_version") != environment.get("python_version")
            or live.get("distributions") != distributions
        ):
            raise FreezeError("{} runtime package environment changed".format(platform))
        source = environment.get("source")
        if not isinstance(source, Mapping):
            raise FreezeError("{} runtime source binding is missing".format(platform))
        repository = Path(str(source.get("repository_path", ""))).resolve()
        if _git_revision(repository) != source.get("revision"):
            raise FreezeError("{} runtime source revision changed".format(platform))
        if tracked_source_tree_sha256(
            repository, source.get("tracked_subpath")
        ) != source.get("tracked_tree_sha256"):
            raise FreezeError("{} runtime source tree changed".format(platform))
        runtime_probe = environment.get("runtime_probe")
        if platform == "carla":
            if runtime_probe is not None:
                raise FreezeError("CARLA compile-only environment cannot claim a runtime probe")
            continue
        if not isinstance(runtime_probe, Mapping):
            raise FreezeError("MetaDrive reset-step evidence is missing")
        observer = _verify_file_binding(
            runtime_probe.get("observer"), "MetaDrive runtime observer"
        )
        registered_extractor_files = {
            (
                str(Path(str(binding.get("path", ""))).resolve()),
                binding.get("sha256"),
            )
            for binding in extractor.get("extractor_files", [])
            if isinstance(binding, Mapping)
        }
        if (
            (str(observer), runtime_probe["observer"].get("sha256"))
            not in registered_extractor_files
            or observer.name != "native_observer.py"
            or sha256_file(observer)
            != sha256_file(Path(__file__).with_name("native_observer.py"))
        ):
            raise FreezeError(
                "MetaDrive runtime probe does not use the frozen trusted observer"
            )
        input_path = _verify_file_binding(
            runtime_probe.get("input"), "MetaDrive runtime probe input"
        )
        result_path = _verify_file_binding(
            runtime_probe.get("result"), "MetaDrive runtime probe result"
        )
        runtime_fixture = read_json(input_path)
        _validate_native_fixture_input(
            runtime_fixture, "metadrive", "MetaDrive runtime probe fixture"
        )
        audited_runtime_inputs = {
            canonical_json_bytes(case.get("input"))
            for case in deterministic_fixture_corpus.get("cases", [])
            if case.get("platform") == "metadrive"
            and case.get("test_kind") == "ego_approved"
        }
        if canonical_json_bytes(runtime_fixture) not in audited_runtime_inputs:
            raise FreezeError(
                "MetaDrive runtime probe input is not an audited ego fixture"
            )
        token_registry = runtime_fixture["artifact"]["dependencies"][0]
        token_registry_path = _verify_file_binding(
            token_registry, "MetaDrive runtime probe token registry"
        )
        try:
            process = subprocess.run(
                [
                    str(interpreter),
                    str(observer),
                    "metadrive",
                    "--runtime-probe",
                    "--token-registry",
                    str(token_registry_path),
                ],
                input=runtime_fixture["artifact"]["text"].encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "MPLCONFIGDIR": "/tmp",
                    "PYTHONHASHSEED": "0",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FreezeError("MetaDrive reset-step probe could not execute") from exc
        if process.returncode != 0:
            raise FreezeError("MetaDrive reset-step probe failed")
        try:
            live_probe = json.loads(process.stdout.decode("utf-8"))
            frozen_probe = read_json(result_path)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FreezeError("MetaDrive runtime evidence is invalid JSON") from exc
        if canonical_json_bytes(live_probe) != canonical_json_bytes(frozen_probe):
            raise FreezeError("MetaDrive reset-step evidence changed")
        expected_probe = {
            "reset_succeeded": True,
            "step_succeeded": True,
            "vehicle_class": "XLVehicle",
            "vehicle_model": "xl",
            "top_down_length_m": EGO_PROXY_LENGTH_M,
            "top_down_width_m": EGO_PROXY_WIDTH_M,
        }
        if any(live_probe.get(key) != value for key, value in expected_probe.items()):
            raise FreezeError("MetaDrive reset-step evidence does not prove the ego proxy")
        expected_blocks = ["I"] + list(
            json.loads(runtime_fixture["artifact"]["text"])["map"]["block_sequence"]
        )
        if live_probe.get("actual_block_ids") != expected_blocks:
            raise FreezeError(
                "MetaDrive reset-step evidence does not prove the submitted PG tokens"
            )
        module_path = Path(str(live_probe.get("metadrive_module_path", ""))).resolve()
        source_root = (
            repository / str(source.get("tracked_subpath", ""))
        ).resolve()
        try:
            module_path.relative_to(source_root)
        except ValueError as exc:
            raise FreezeError(
                "MetaDrive runtime imported code outside the frozen source tree"
            ) from exc


def _load_extractor_entrypoints(
    manifest: Mapping[str, Any]
) -> Dict[str, Any]:
    entries = manifest.get("entrypoints")
    if not isinstance(entries, list) or len(entries) != 2:
        raise FreezeError("extractor manifest requires CARLA and MetaDrive entrypoints")
    result = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise FreezeError("extractor entrypoint is malformed")
        platform = entry.get("platform")
        if platform not in ("carla", "metadrive") or platform in result:
            raise FreezeError("extractor entrypoints require unique platform names")
        path = _verify_file_binding(entry, "{} extractor entrypoint".format(platform))
        callable_name = entry.get("callable")
        if not isinstance(callable_name, str) or not callable_name:
            raise FreezeError("extractor entrypoint lacks a callable name")
        result[platform] = {"path": path, "callable": callable_name}
    return result


def _execute_extractor_once(entrypoint: Mapping[str, Any], value: Any, label: str) -> Any:
    worker = Path(__file__).with_name("extractor_worker.py").resolve()
    command = [
        sys.executable,
        "-I",
        str(worker),
        str(entrypoint["path"]),
        str(entrypoint["callable"]),
    ]
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="bus-extractor-") as directory:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=directory,
                env=environment,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(
                    input=canonical_json_bytes(value), timeout=EXTRACTOR_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise FreezeError("{} extractor timed out".format(label)) from exc
            completed_returncode = process.returncode
    except subprocess.TimeoutExpired as exc:
        raise FreezeError("{} extractor timed out".format(label)) from exc
    except OSError as exc:
        raise FreezeError("{} extractor could not start: {}".format(label, exc)) from exc
    if completed_returncode != 0:
        error = stderr.decode("utf-8", errors="replace")[-1000:]
        raise FreezeError("{} extractor failed: {}".format(label, error.strip()))
    try:
        return json.loads(stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FreezeError("{} extractor returned non-JSON output".format(label)) from exc


def _execute_extractor(entrypoint: Mapping[str, Any], value: Any, label: str) -> Any:
    first = _execute_extractor_once(entrypoint, copy.deepcopy(value), label)
    second = _execute_extractor_once(entrypoint, copy.deepcopy(value), label)
    if canonical_json_bytes(first) != canonical_json_bytes(second):
        raise FreezeError("{} extractor is nondeterministic".format(label))
    return first


def _validate_extractor_projection(
    value: Any, label: str = "semantic"
) -> Mapping[str, Any]:
    _schema_or_freeze(
        validate_schema_instance,
        value,
        "semantic_projection",
        context="{} projection".format(label),
    )
    if not isinstance(value, Mapping) or set(value) != set(EXTRACTOR_PROJECTION_FIELDS):
        raise FreezeError(
            "{} extractor must return the exact semantic projection fields".format(label)
        )
    if not isinstance(value.get("ego"), Mapping):
        raise FreezeError("{} extractor returned a malformed ego".format(label))
    for field in ("atoms", "common_atoms", "complete_categories"):
        if not isinstance(value.get(field), list):
            raise FreezeError("{} extractor returned malformed {}".format(label, field))
    return value


def load_frozen_extractor_runtime(
    manifest: Mapping[str, Any]
) -> tuple:
    """Load frozen extractors plus benchmark-owned platform input context."""

    assets = [
        asset
        for asset in manifest.get("assets", [])
        if asset.get("role") == "semantic_extractor_manifest"
    ]
    if len(assets) != 1:
        raise FreezeError("freeze lacks one semantic extractor manifest")
    manifest_path = _verify_file_binding(assets[0], "semantic extractor manifest")
    document = read_json(manifest_path)
    _require(
        document,
        decision_status="confirmed",
        input_contract="artifact_observation_v0.1",
        output_contract="semantic_projection_v0.1",
    )
    if not document.get("extractor_id") or not document.get("extractor_version"):
        raise FreezeError("semantic extractor manifest lacks a frozen identity")
    registry_assets = [
        asset
        for asset in manifest.get("assets", [])
        if asset.get("role") == "metadrive_pg_token_registry"
    ]
    if len(registry_assets) != 1:
        raise FreezeError("freeze lacks one MetaDrive PG token registry")
    registry_dependency = _verified_metadrive_registry_dependency(
        registry_assets[0], "frozen MetaDrive token registry"
    )
    entrypoints = _load_extractor_entrypoints(document)
    runtime = {
        "carla": {
            "entrypoint": entrypoints["carla"],
            "artifact_format": "scenic",
            "trusted_dependencies": [],
        },
        "metadrive": {
            "entrypoint": entrypoints["metadrive"],
            "artifact_format": PG_BLOCK_SCENE_CONTRACT,
            "trusted_dependencies": [registry_dependency],
        },
    }
    return document, runtime


def build_extractor_input(
    response: Mapping[str, Any],
    *,
    artifact_format: str,
    trusted_dependencies: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build the only payload visible to a formal semantic extractor."""

    platform = response.get("platform")
    expected_format = {
        "carla": "scenic",
        "metadrive": PG_BLOCK_SCENE_CONTRACT,
    }.get(platform)
    if artifact_format != expected_format:
        raise FreezeError("semantic extractor artifact format/platform mismatch")
    if not isinstance(trusted_dependencies, (list, tuple)):
        raise FreezeError("semantic extractor trusted dependencies are malformed")
    if any(not isinstance(value, Mapping) for value in trusted_dependencies):
        raise FreezeError("semantic extractor trusted dependencies are malformed")
    if platform == "carla" and trusted_dependencies:
        raise FreezeError("CARLA extractor cannot receive MetaDrive dependencies")
    if platform == "metadrive" and (
        len(trusted_dependencies) != 1
        or trusted_dependencies[0].get("kind")
        != "metadrive_pg_token_registry"
    ):
        raise FreezeError("MetaDrive extractor requires one trusted token registry")
    dependencies = (
        [
            _verified_metadrive_registry_dependency(
                trusted_dependencies[0],
                "trusted MetaDrive extractor token registry",
            )
        ]
        if platform == "metadrive"
        else []
    )
    artifact = response.get("artifact")
    artifact_payload = None
    if isinstance(artifact, Mapping):
        path = Path(str(artifact.get("path", "")))
        try:
            encoded = path.read_bytes()
        except OSError as exc:
            raise FreezeError("semantic artifact must be readable UTF-8: {}".format(exc)) from exc
        if (
            len(encoded) != artifact.get("bytes")
            or sha256_bytes(encoded) != artifact.get("sha256")
        ):
            raise FreezeError("semantic artifact byte binding changed")
        try:
            content = encoded.decode("utf-8")
        except UnicodeError as exc:
            raise FreezeError("semantic artifact must be readable UTF-8") from exc
        artifact_payload = {
            "sha256": artifact.get("sha256"),
            "bytes": artifact.get("bytes"),
            "text": content,
            "dependencies": dependencies,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "artifact_format": artifact_format,
        "terminal_status": response.get("terminal_status"),
        "disposition": response.get("disposition"),
        "artifact": artifact_payload,
    }


def terminal_semantic_projection() -> Dict[str, Any]:
    """Return the query-blind projection for a response with no usable artifact."""

    return {
        "ego": {
            "count": 0,
            "semantic_role": "missing",
            "approved_proxy": False,
            "deterministic": True,
            "length_m": 0.0,
            "width_m": 0.0,
            "blueprint": "",
            "vehicle_model": "",
        },
        "atoms": [],
        "common_atoms": [],
        "complete_categories": list(ATOM_CATEGORIES),
    }


def execute_frozen_extractor(
    runtime: Mapping[str, Any],
    response: Mapping[str, Any],
) -> Mapping[str, Any]:
    platform = response.get("platform")
    if platform not in runtime:
        raise FreezeError("no frozen extractor for response platform")
    if (
        response.get("disposition") in ("reject", "clarification")
        or not isinstance(response.get("artifact"), Mapping)
    ):
        return _validate_extractor_projection(
            terminal_semantic_projection(), "{} terminal".format(platform)
        )
    context = runtime[platform]
    if not isinstance(context, Mapping) or not isinstance(
        context.get("entrypoint"), Mapping
    ):
        raise FreezeError("frozen extractor runtime context is malformed")
    output = _execute_extractor_once(
        context["entrypoint"],
        build_extractor_input(
            response,
            artifact_format=context.get("artifact_format"),
            trusted_dependencies=context.get("trusted_dependencies"),
        ),
        "{} formal".format(platform),
    )
    return _validate_extractor_projection(output, "{} formal".format(platform))


def _validate_fixture_results(
    corpus: Mapping[str, Any],
    results_path: Path,
    label: str,
    entrypoints: Mapping[str, Any],
    equivalence: bool = False,
) -> Dict[str, Any]:
    document = _read_decision_document(results_path)
    if not isinstance(document, Mapping) or not isinstance(corpus, Mapping):
        raise FreezeError("{} fixture result must be a JSON object".format(label))
    _schema_or_freeze(
        validate_schema_instance,
        document,
        "semantic_fixture_results",
        context="{} fixture results".format(label),
    )
    _require(document, decision_status="confirmed")
    _require(
        corpus,
        decision_status="confirmed",
        benchmark_owned=True,
        input_contract="raw_platform_artifact_v0.1",
    )
    cases = corpus.get("cases")
    if not isinstance(cases, list) or not cases:
        raise FreezeError("{} fixture corpus has no cases".format(label))
    result_cases = document.get("cases")
    if not isinstance(result_cases, list):
        raise FreezeError("{} fixture result has no cases".format(label))
    result_by_id = {
        case.get("case_id"): case
        for case in result_cases
        if isinstance(case, Mapping) and case.get("case_id")
    }
    corpus_ids = [case.get("case_id") for case in cases if isinstance(case, Mapping)]
    if (
        len(result_by_id) != len(result_cases)
        or len(set(corpus_ids)) != len(cases)
        or set(result_by_id) != set(corpus_ids)
    ):
        raise FreezeError("{} fixture corpus/result case IDs differ".format(label))
    equal_cases = 0
    different_cases = 0
    platforms = set()
    categories = set()
    ego_positive = 0
    ego_negative = 0

    for case in cases:
        if not isinstance(case, Mapping) or not case.get("case_id"):
            raise FreezeError("{} fixture case is malformed".format(label))
        stored = result_by_id[case["case_id"]]
        if equivalence:
            expected_equal = case.get("expected_equal")
            if not isinstance(expected_equal, bool):
                raise FreezeError("equivalence fixture lacks expected_equal")
            left_platform = case.get("left_platform")
            right_platform = case.get("right_platform")
            if left_platform not in entrypoints or right_platform not in entrypoints:
                raise FreezeError("equivalence fixture references an unknown platform")
            platforms.update((left_platform, right_platform))
            live_left = _execute_extractor(
                entrypoints[left_platform],
                case.get("left_input"),
                left_platform,
            )
            live_right = _execute_extractor(
                entrypoints[right_platform],
                case.get("right_input"),
                right_platform,
            )
            _validate_extractor_projection(
                live_left,
                "{} left".format(label),
            )
            _validate_extractor_projection(
                live_right,
                "{} right".format(label),
            )
            if (
                live_left != case.get("expected_left")
                or live_right != case.get("expected_right")
                or live_left != stored.get("actual_left")
                or live_right != stored.get("actual_right")
            ):
                raise FreezeError("stored equivalence fingerprints are not live outputs")
            actual_equal = live_left == live_right
            if actual_equal != expected_equal:
                raise FreezeError("cross-platform equivalence fixture failed")
            equal_cases += int(expected_equal)
            different_cases += int(not expected_equal)
        else:
            platform = case.get("platform")
            if platform not in entrypoints:
                raise FreezeError("deterministic fixture references an unknown platform")
            platforms.add(platform)
            live = _execute_extractor(
                entrypoints[platform], case.get("input"), platform
            )
            _validate_extractor_projection(live, label)
            if canonical_json_bytes(live) == canonical_json_bytes(case.get("input")):
                raise FreezeError("deterministic fixture cannot be an identity echo")
            if live != case.get("expected") or live != stored.get("actual"):
                raise FreezeError("deterministic semantic fixture failed")
            categories.update(
                atom.get("category")
                for atom in live.get("atoms", [])
                if isinstance(atom, Mapping)
            )
            ego = live.get("ego", {})
            if ego.get("count") == 1 and ego.get("approved_proxy") is True:
                ego_positive += 1
            else:
                ego_negative += 1
    if equivalence and (equal_cases == 0 or different_cases == 0):
        raise FreezeError(
            "cross-platform fixtures require equivalent and controlled-difference cases"
        )
    return {
        "case_count": len(cases),
        "platforms": platforms,
        "categories": categories,
        "ego_positive": ego_positive,
        "ego_negative": ego_negative,
    }


def _validate_native_fixture_input(value: Any, platform: str, label: str) -> None:
    _schema_or_freeze(
        validate_schema_instance,
        value,
        "semantic_fixture_input",
        context="{} fixture input".format(label),
    )
    if not isinstance(value, Mapping) or not isinstance(value.get("artifact"), Mapping):
        raise FreezeError("{} lacks a raw platform artifact input".format(label))
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("platform") != platform
        or value.get("terminal_status") != "complete"
        or value.get("disposition") != "generate"
    ):
        raise FreezeError("{} raw fixture envelope is malformed".format(label))
    artifact = value["artifact"]
    text = artifact.get("text")
    if not isinstance(text, str) or not text:
        raise FreezeError("{} raw artifact is empty".format(label))
    encoded = text.encode("utf-8")
    if (
        artifact.get("sha256") != sha256_bytes(encoded)
        or artifact.get("bytes") != len(encoded)
    ):
        raise FreezeError("{} raw artifact byte binding is invalid".format(label))
    lowered = text.lower()
    if any(
        forbidden in lowered
        for forbidden in (
            "fixture_fact",
            "concept_id",
            "semantic_category",
            "expected_left",
            "expected_right",
            "semanticoverage",
            "actorroles",
            "eventplan",
            "cpdvariation",
            "laneconfiguration",
            "roadtopology",
            "relativeactors",
            "numericconstraints",
            "eventsequence",
            "conditionaltrigger",
            "parallelevents",
            "temporalmode",
            "risklevel",
            "trafficrule",
            "rulemode",
        )
    ):
        raise FreezeError("{} raw artifact contains benchmark truth sidecar".format(label))
    dependencies = artifact.get("dependencies")
    if not isinstance(dependencies, list):
        raise FreezeError("{} raw artifact dependencies are malformed".format(label))
    if platform == "carla":
        params = re.findall(r"^\s*param\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", text, re.MULTILINE)
        scenic_line_patterns = (
            r"param map = localPath\(['\"].+\.xodr['\"]\)",
            r"model scenic\.simulators\.carla\.model",
            r"behavior B\(p\):",
            r"    do FollowTrajectoryBehavior\(trajectory=p\)",
            r"P[0-9]+ = \[(?:-?[0-9.]+@-?[0-9.]+)(?:, -?[0-9.]+@-?[0-9.]+)*\]",
            r"(?:ego|O[0-9]+|X[0-9]+) = (?:Car|Bicycle|Motorcycle|Pedestrian|BusStop|Cone) at (?:P[0-9]+\[0\]|-?[0-9.]+@-?[0-9.]+),?",
            r"X[0-9]+ = (?:Bicycle|Cone) at -?[0-9.]+@-?[0-9.]+, with regionContainedIn None",
            r"    with regionContainedIn None,?",
            r"    with blueprint ['\"][A-Za-z0-9_.-]+['\"],?",
            r"    with (?:length|width) [0-9.]+,?",
            r"    with behavior B\(P[0-9]+\),?",
        )
        scenic_lines = [line for line in text.splitlines() if line]
        if (
            value.get("artifact_format") != "scenic"
            or "model scenic.simulators.carla.model" not in text
            or "param map = localPath(" not in text
            or params != ["map"]
            or "#" in text
            or len(dependencies) != 1
            or any(
                not any(re.fullmatch(pattern, line) for pattern in scenic_line_patterns)
                for line in scenic_lines
            )
        ):
            raise FreezeError("{} is not a CARLA Scenic fixture".format(label))
        dependency = dependencies[0]
        path = _verify_file_binding(
            dependency, "{} CARLA fixture map dependency".format(label)
        )
        if (
            dependency.get("kind") != "opendrive"
            or dependency.get("bytes") != path.stat().st_size
            or str(path) not in text
        ):
            raise FreezeError("{} CARLA fixture map dependency is not frozen".format(label))
    elif platform == "metadrive":
        if value.get("artifact_format") != "metadrive_pg_block_scene_v0.1":
            raise FreezeError("{} is not a MetaDrive PG block-token fixture".format(label))
        if len(dependencies) != 1:
            raise FreezeError(
                "{} MetaDrive fixture requires exactly one token registry".format(label)
            )
        dependency = dependencies[0]
        registry_path = _verify_file_binding(
            dependency, "{} MetaDrive token registry dependency".format(label)
        )
        if (
            dependency.get("kind") != "metadrive_pg_token_registry"
            or dependency.get("bytes") != registry_path.stat().st_size
        ):
            raise FreezeError("{} MetaDrive token registry is not frozen".format(label))
        try:
            registry, registry_sha256 = load_token_registry(
                registry_path,
                expected_sha256=dependency["sha256"],
                formal=True,
                verify_files=True,
            )
            document = parse_artifact(encoded, registry, registry_sha256)
        except MetaDrivePGError as exc:
            raise FreezeError(
                "{} has an invalid MetaDrive PG block-token artifact: {}".format(
                    label, exc
                )
            ) from exc
        if "map_features" in document or any(
            key in document.get("map", {})
            for key in ("map", "map_id", "map_path", "map_file", "map_features")
        ):
            raise FreezeError("{} attempts to submit real-map material".format(label))
    else:
        raise FreezeError("{} has an unknown fixture platform".format(label))


def _projection_concept_ids(
    projection: Any,
    ontology_by_id: Mapping[str, Mapping[str, Any]],
    platform: str = None,
) -> set:
    if not isinstance(projection, Mapping):
        raise FreezeError("fixture expected projection is malformed")
    _validate_extractor_projection(projection, label="fixture expected")
    result = set()
    ego = projection.get("ego", {})
    matched_atom_indexes = set()
    matched_common_indexes = set()
    for concept_id, concept in ontology_by_id.items():
        definition = concept["definition"]
        kind = definition["kind"]
        if kind == "ego_proxy":
            platform_proxy_matches = (
                ego.get("blueprint") == definition["carla_blueprint"]
                if platform == "carla"
                else ego.get("vehicle_model") == definition["metadrive_vehicle_model"]
                if platform == "metadrive"
                else False
            )
            if (
                ego.get("count") == definition["count"]
                and ego.get("semantic_role") in ("ego_bus", "bus", "bus_proxy")
                and ego.get("approved_proxy") is True
                and ego.get("deterministic") is True
                and ego.get("length_m") == definition["length_m"]
                and ego.get("width_m") == definition["width_m"]
                and platform_proxy_matches
            ):
                result.add(concept_id)
        elif kind == "requirement_atom_domain":
            matching_indexes = {
                index
                for index, atom in enumerate(projection.get("atoms", []))
                if isinstance(atom, Mapping)
                and
                atom.get("category") == definition["category"]
                and atom.get("predicate") == definition["predicate"]
                and atom.get("polarity") == definition["polarity"]
                and atom.get("arguments") in definition["argument_domain"]
            }
            if matching_indexes:
                result.add(concept_id)
                matched_atom_indexes.update(matching_indexes)
        elif kind == "cpd_dimension":
            allowed_values = {
                canonical_json_bytes(value)
                for variant in definition["variants"]
                for value in variant.get("allowed_values", [])
            }
            matching_indexes = {
                index
                for index, atom in enumerate(projection.get("common_atoms", []))
                if isinstance(atom, Mapping)
                and
                atom.get("dimension") == definition["name"]
                and canonical_json_bytes(atom.get("value")) in allowed_values
            }
            if matching_indexes:
                result.add(concept_id)
                matched_common_indexes.update(matching_indexes)
    if matched_atom_indexes != set(range(len(projection.get("atoms", [])))):
        raise FreezeError("fixture projection contains an atom outside the ontology")
    if matched_common_indexes != set(range(len(projection.get("common_atoms", [])))):
        raise FreezeError("fixture projection contains a CPD value outside the ontology")
    categories = projection.get("complete_categories", [])
    if len(set(categories)) != len(categories) or any(
        category not in ("actor", "road", "spatial", "event", "temporal", "normative")
        for category in categories
    ):
        raise FreezeError("fixture projection contains malformed complete categories")
    return result


def _validate_fixture_ego_control(
    platform: str, test_kind: str, ego: Mapping[str, Any]
) -> None:
    proxy_matches = (
        ego.get("blueprint") == CARLA_EGO_BLUEPRINT
        if platform == "carla"
        else ego.get("vehicle_model") == "xl"
    )
    role_matches = ego.get("semantic_role") in ("ego_bus", "bus", "bus_proxy")
    footprint_matches = (
        ego.get("length_m") == EGO_PROXY_LENGTH_M
        and ego.get("width_m") == EGO_PROXY_WIDTH_M
    )
    deterministic = ego.get("deterministic") is True
    if test_kind == "ego_approved":
        valid = (
            ego.get("count") == 1
            and role_matches
            and ego.get("approved_proxy") is True
            and deterministic
            and footprint_matches
            and proxy_matches
        )
    elif test_kind == "ego_missing":
        valid = ego.get("count") == 0 and ego.get("approved_proxy") is False
    elif test_kind == "ego_wrong_proxy":
        valid = (
            ego.get("count") == 1
            and deterministic
            and footprint_matches
            and (not proxy_matches or not role_matches)
            and ego.get("approved_proxy") is False
        )
    elif test_kind == "ego_wrong_footprint":
        valid = (
            ego.get("count") == 1
            and role_matches
            and deterministic
            and proxy_matches
            and not footprint_matches
        )
    else:
        valid = False
    if not valid:
        raise FreezeError("ego fixture does not match its declared control kind")


def _projection_without_concept(
    projection: Mapping[str, Any], definition: Mapping[str, Any]
) -> Mapping[str, Any]:
    value = copy.deepcopy(projection)
    if definition["kind"] == "requirement_atom_domain":
        value["atoms"] = [
            atom
            for atom in value.get("atoms", [])
            if not (
                atom.get("category") == definition["category"]
                and atom.get("predicate") == definition["predicate"]
                and atom.get("polarity") == definition["polarity"]
                and atom.get("arguments") in definition["argument_domain"]
            )
        ]
    elif definition["kind"] == "cpd_dimension":
        value["common_atoms"] = [
            atom
            for atom in value.get("common_atoms", [])
            if atom.get("dimension") != definition["name"]
        ]
    return value


def _validate_capability_fixture_contract(
    documents: Mapping[str, List[Any]], ontology_by_id: Mapping[str, Mapping[str, Any]]
) -> None:
    deterministic = documents["semantic_fixture_corpus"][0]
    cross_platform = documents["cross_platform_fixture_corpus"][0]
    _schema_or_freeze(
        validate_schema_instance,
        deterministic,
        "semantic_fixture_corpus",
        context="semantic fixture corpus",
    )
    _schema_or_freeze(
        validate_schema_instance,
        cross_platform,
        "cross_platform_fixture_corpus",
        context="cross-platform fixture corpus",
    )
    deterministic_cases = deterministic.get("cases", [])
    cross_cases = cross_platform.get("cases", [])
    requirement_definitions = [
        concept["definition"]
        for concept in ontology_by_id.values()
        if concept["definition"].get("kind") == "requirement_atom_domain"
    ]
    cpd_definitions = [
        concept["definition"]
        for concept in ontology_by_id.values()
        if concept["definition"].get("kind") == "cpd_dimension"
    ]
    required_categories = {
        definition["category"] for definition in requirement_definitions
    }
    cpd_value_counts = {
        definition["name"]: len(
            {
                canonical_json_bytes(value)
                for variant in definition["variants"]
                for value in variant.get("allowed_values", [])
            }
        )
        for definition in cpd_definitions
    }
    deterministic_categories = {
        definition["category"]
        for definition in requirement_definitions
        if deterministic_representative_arguments(
            definition["category"],
            definition["predicate"],
            definition["polarity"],
            definition["argument_domain"],
        )
    }
    # CARLA covers approved/missing/wrong-proxy/wrong-footprint ego cases.
    # MetaDrive's strict PG schema rejects the missing-ego artifact before the
    # semantic extractor, so its deterministic corpus has the other three.
    expected_deterministic_cases = (
        7
        + 4 * len(requirement_definitions)
        + 2 * sum(1 + count for count in cpd_value_counts.values())
    )
    expected_cross_cases = (
        len(required_categories)
        + len(deterministic_categories)
        + sum(cpd_value_counts.values())
    )
    if (
        len(deterministic_cases) != expected_deterministic_cases
        or len(cross_cases) != expected_cross_cases
    ):
        raise FreezeError(
            "fixture corpus counts differ from the confirmed development ontology: "
            "expected {}/{} got {}/{}".format(
                expected_deterministic_cases,
                expected_cross_cases,
                len(deterministic_cases),
                len(cross_cases),
            )
        )

    case_index = {}
    category_tests = {
        platform: {category: set() for category in required_categories}
        for platform in ("carla", "metadrive")
    }
    ego_controls = {platform: set() for platform in ("carla", "metadrive")}
    ego_expected = {platform: {} for platform in ("carla", "metadrive")}
    concept_tests = {
        platform: {concept_id: set() for concept_id in ontology_by_id}
        for platform in ("carla", "metadrive")
    }
    concept_expected = {
        platform: {concept_id: {} for concept_id in ontology_by_id}
        for platform in ("carla", "metadrive")
    }
    for case in deterministic_cases:
        case_id = case.get("case_id")
        platform = case.get("platform")
        concept_ids = case.get("concept_ids")
        test_kind = case.get("test_kind")
        if (
            not case_id
            or case_id in case_index
            or platform not in ("carla", "metadrive")
            or not isinstance(concept_ids, list)
            or len(concept_ids) != 1
            or concept_ids[0] not in ontology_by_id
        ):
            raise FreezeError("deterministic fixture case metadata is malformed")
        _validate_native_fixture_input(case.get("input"), platform, case_id)
        try:
            embedded = json.loads(case["input"]["artifact"]["text"])
        except (json.JSONDecodeError, TypeError):
            embedded = None
        if embedded is not None and canonical_json_bytes(
            case.get("expected")
        ) == canonical_json_bytes(embedded):
            raise FreezeError("raw fixture artifact embeds its expected projection")
        concept_id = concept_ids[0]
        definition = ontology_by_id[concept_id]["definition"]
        expected = case.get("expected", {})
        expected_concepts = _projection_concept_ids(
            expected, ontology_by_id, platform=platform
        )
        if definition["kind"] == "requirement_atom_domain":
            judge_required = not deterministic_representative_arguments(
                definition["category"],
                definition["predicate"],
                definition["polarity"],
                definition["argument_domain"],
            )
            allowed_test_kinds = (
                {"judge_required_positive", "judge_required_negative"}
                if judge_required
                else {"positive", "negative"}
            )
            if test_kind not in allowed_test_kinds:
                raise FreezeError("atom fixture has the wrong evidence-routing test kind")
            matching = [
                atom
                for atom in expected.get("atoms", [])
                if atom.get("category") == definition["category"]
                and atom.get("predicate") == definition["predicate"]
                and atom.get("polarity") == definition["polarity"]
                and atom.get("arguments") in definition["argument_domain"]
            ]
            if test_kind == "positive" and len(matching) != 1:
                raise FreezeError("positive atom fixture lacks its registered semantic concept")
            if test_kind == "negative" and matching:
                raise FreezeError("negative atom fixture still contains its tested concept")
            if judge_required and (
                matching or definition["category"] in expected.get("complete_categories", [])
            ):
                raise FreezeError("judge-required fixture was falsely decided deterministically")
            category_tests[platform][definition["category"]].add(test_kind)
            concept_tests[platform][concept_id].add(test_kind)
            concept_expected[platform][concept_id].setdefault(test_kind, []).append(
                expected
            )
        elif definition["kind"] == "cpd_dimension":
            if test_kind not in ("positive", "negative"):
                raise FreezeError("CPD fixture requires a positive or negative test kind")
            matching = [
                atom
                for atom in expected.get("common_atoms", [])
                if atom.get("dimension") == definition["name"]
            ]
            if test_kind == "positive" and len(matching) != 1:
                raise FreezeError("CPD fixture lacks its registered common dimension")
            if test_kind == "negative" and matching:
                raise FreezeError("negative CPD fixture still contains its tested dimension")
            concept_tests[platform][concept_id].add(test_kind)
            concept_expected[platform][concept_id].setdefault(test_kind, []).append(
                expected
            )
        elif definition["kind"] == "ego_proxy":
            if test_kind not in (
                "ego_approved",
                "ego_missing",
                "ego_wrong_proxy",
                "ego_wrong_footprint",
            ):
                raise FreezeError("ego fixture lacks a registered control kind")
            should_be_present = test_kind == "ego_approved"
            if (concept_id in expected_concepts) is not should_be_present:
                raise FreezeError("ego fixture control does not match its expected projection")
            _validate_fixture_ego_control(platform, test_kind, expected.get("ego", {}))
            ego_controls[platform].add(test_kind)
            ego_expected[platform][test_kind] = expected.get("ego", {})
            concept_tests[platform][concept_id].add(test_kind)
        if test_kind in ("positive", "ego_approved") and concept_id not in expected_concepts:
            raise FreezeError("positive fixture does not contain its declared concept")
        if test_kind in (
            "negative",
            "judge_required_positive",
            "judge_required_negative",
            "ego_missing",
            "ego_wrong_proxy",
            "ego_wrong_footprint",
        ) and concept_id in expected_concepts:
            raise FreezeError("negative fixture still contains its declared concept")
        case_index[case_id] = {"platforms": {platform}, "concept_ids": {concept_id}}

    cross_coverage = {
        category: set()
        for category in ("actor", "road", "spatial", "event", "temporal", "normative")
    }
    cpd_cross_values = {
        concept_id: set()
        for concept_id, concept in ontology_by_id.items()
        if concept["definition"]["kind"] == "cpd_dimension"
    }
    for case in cross_cases:
        case_id = case.get("case_id")
        concept_id = case.get("concept_id")
        category = case.get("semantic_category")
        if (
            not case_id
            or case_id in case_index
            or concept_id not in ontology_by_id
            or category not in tuple(cross_coverage) + ("cpd",)
        ):
            raise FreezeError("cross-platform fixture case metadata is malformed")
        _validate_native_fixture_input(case.get("left_input"), "carla", case_id)
        _validate_native_fixture_input(case.get("right_input"), "metadrive", case_id)
        definition = ontology_by_id[concept_id]["definition"]
        if category == "cpd":
            if definition["kind"] != "cpd_dimension":
                raise FreezeError("cross-platform CPD fixture has a non-CPD concept")
        elif definition["kind"] != "requirement_atom_domain" or definition[
            "category"
        ] != category:
            raise FreezeError("cross-platform fixture category does not match its concept")
        left_expected = case.get("expected_left")
        right_expected = case.get("expected_right")
        left_concepts = _projection_concept_ids(
            left_expected, ontology_by_id, platform="carla"
        )
        right_concepts = _projection_concept_ids(
            right_expected, ontology_by_id, platform="metadrive"
        )
        semantic_delta = left_concepts.symmetric_difference(right_concepts)
        expected_equal = case.get("expected_equal")
        changed = case.get("changed_concept_ids")
        if expected_equal is True:
            concept_is_deterministic = (
                definition["kind"] == "cpd_dimension"
                or bool(
                    deterministic_representative_arguments(
                        definition["category"],
                        definition["predicate"],
                        definition["polarity"],
                        definition["argument_domain"],
                    )
                )
            )
            concept_present_on_both = (
                concept_id in left_concepts and concept_id in right_concepts
            )
            judge_routing_is_incomplete_on_both = (
                definition["kind"] == "requirement_atom_domain"
                and concept_id not in left_concepts
                and concept_id not in right_concepts
                and definition["category"]
                not in left_expected.get("complete_categories", [])
                and definition["category"]
                not in right_expected.get("complete_categories", [])
            )
            if (
                changed != []
                or semantic_delta
                or (
                    concept_is_deterministic
                    and not concept_present_on_both
                )
                or (
                    not concept_is_deterministic
                    and not judge_routing_is_incomplete_on_both
                )
                or canonical_json_bytes(left_expected)
                != canonical_json_bytes(right_expected)
            ):
                raise FreezeError("equivalent fixture declares a changed concept")
            if category == "cpd":
                values = [
                    atom.get("value")
                    for atom in left_expected.get("common_atoms", [])
                    if atom.get("dimension") == definition["name"]
                ]
                if len(values) != 1:
                    raise FreezeError("cross-platform CPD fixture lacks one value")
                cpd_cross_values[concept_id].add(canonical_json_bytes(values[0]))
            else:
                cross_coverage[category].add("equivalent")
        elif expected_equal is False:
            if category == "cpd":
                raise FreezeError("CPD cross-platform fixtures must be equivalent pairs")
            left_without = _projection_without_concept(left_expected, definition)
            right_without = _projection_without_concept(right_expected, definition)
            if (
                changed != [concept_id]
                or semantic_delta != {concept_id}
                or ((concept_id in left_concepts) == (concept_id in right_concepts))
                or canonical_json_bytes(left_without)
                != canonical_json_bytes(right_without)
            ):
                raise FreezeError("difference fixture must change exactly its declared concept")
            cross_coverage[category].add("difference")
        else:
            raise FreezeError("cross-platform fixture lacks an equality expectation")
        case_index[case_id] = {
            "platforms": {"carla", "metadrive"},
            "concept_ids": {concept_id},
        }

    if any(
        not (
            {"positive", "negative"} <= tests
            or {"judge_required_positive", "judge_required_negative"} <= tests
        )
        for platform in category_tests.values()
        for tests in platform.values()
    ):
        raise FreezeError("fixtures lack per-platform paired category coverage")
    required_ego_controls = {
        "carla": {
            "ego_approved",
            "ego_missing",
            "ego_wrong_proxy",
            "ego_wrong_footprint",
        },
        "metadrive": {
            "ego_approved",
            "ego_wrong_proxy",
            "ego_wrong_footprint",
        },
    }
    if any(
        controls != required_ego_controls[platform]
        for platform, controls in ego_controls.items()
    ):
        raise FreezeError("fixtures lack per-platform ego proxy controls")
    if any(
        len({canonical_json_bytes(value) for value in controls.values()})
        != len(required_ego_controls[platform])
        for platform, controls in ego_expected.items()
    ):
        raise FreezeError("ego fixture controls are not semantically distinct")
    expected_cross_coverage = {}
    for category in required_categories:
        has_deterministic_concept = any(
            concept["definition"].get("kind") == "requirement_atom_domain"
            and concept["definition"].get("category") == category
            and bool(
                deterministic_representative_arguments(
                    category,
                    concept["definition"]["predicate"],
                    concept["definition"]["polarity"],
                    concept["definition"]["argument_domain"],
                )
            )
            for concept in ontology_by_id.values()
        )
        expected_cross_coverage[category] = (
            {"equivalent", "difference"}
            if has_deterministic_concept
            else {"equivalent"}
        )
    if any(
        cross_coverage[category] != expected_cross_coverage[category]
        for category in required_categories
    ):
        raise FreezeError("cross-platform fixtures lack category-level controlled pairs")
    for concept_id, observed_values in cpd_cross_values.items():
        allowed_values = {
            canonical_json_bytes(value)
            for variant in ontology_by_id[concept_id]["definition"]["variants"]
            for value in variant.get("allowed_values", [])
        }
        if observed_values != allowed_values:
            raise FreezeError("cross-platform fixtures do not cover every CPD value")

    for platform in ("carla", "metadrive"):
        capability = documents["platform_capability_{}".format(platform)][0]
        by_id = {concept["concept_id"]: concept for concept in capability["concepts"]}
        for concept_id, concept in by_id.items():
            for case_id in concept.get("fixture_case_ids", []):
                case = case_index.get(case_id)
                if (
                    case is None
                    or platform not in case["platforms"]
                    or concept_id not in case["concept_ids"]
                ):
                    raise FreezeError("capability references an unrelated fixture case")
            if concept["status"] != "unsupported" and not concept["fixture_case_ids"]:
                raise FreezeError("supported capability has no fixture evidence")
            if concept["status"] != "unsupported":
                kind = ontology_by_id[concept_id]["definition"]["kind"]
                definition = ontology_by_id[concept_id]["definition"]
                required_tests = (
                    required_ego_controls[platform]
                    if kind == "ego_proxy"
                    else {"judge_required_positive", "judge_required_negative"}
                    if concept["status"] == "judge_required"
                    else {"positive", "negative"}
                )
                if not required_tests <= concept_tests[platform][concept_id]:
                    raise FreezeError(
                        "supported capability lacks concept-specific deterministic evidence"
                    )
                if concept["status"] == "judge_required":
                    expected_by_kind = concept_expected[platform][concept_id]
                    positives = expected_by_kind["judge_required_positive"]
                    negatives = expected_by_kind["judge_required_negative"]
                    if len(positives) != 1 or len(negatives) != 1:
                        raise FreezeError("judge-required fixtures are not uniquely paired")
                    if any(
                        concept_id
                        in _projection_concept_ids(
                            projection, ontology_by_id, platform=platform
                        )
                        or definition["category"]
                        in projection.get("complete_categories", [])
                        for projection in positives + negatives
                    ):
                        raise FreezeError("judge-required fixture bypasses unknown routing")
                elif kind in ("requirement_atom_domain", "cpd_dimension"):
                    expected_by_kind = concept_expected[platform][concept_id]
                    positives = expected_by_kind["positive"]
                    negatives = expected_by_kind["negative"]
                    if len(negatives) != 1 or (
                        kind == "requirement_atom_domain" and len(positives) != 1
                    ):
                        raise FreezeError("concept fixture pairs are not uniquely controlled")
                    if kind == "cpd_dimension":
                        observed_values = {
                            canonical_json_bytes(atom["value"])
                            for expected in positives
                            for atom in expected.get("common_atoms", [])
                            if atom.get("dimension") == definition["name"]
                        }
                        allowed_values = {
                            canonical_json_bytes(value)
                            for variant in definition["variants"]
                            for value in variant.get("allowed_values", [])
                        }
                        if observed_values != allowed_values or len(positives) != len(
                            allowed_values
                        ):
                            raise FreezeError(
                                "CPD fixtures do not cover every allowed value exactly once"
                            )
                    negative_baseline = _projection_without_concept(
                        negatives[0], definition
                    )
                    if any(
                        canonical_json_bytes(
                            _projection_without_concept(positive, definition)
                        )
                        != canonical_json_bytes(negative_baseline)
                        for positive in positives
                    ):
                        raise FreezeError(
                            "positive/negative fixture pair changes more than its concept"
                        )
        for case_id, case in case_index.items():
            if platform not in case["platforms"]:
                continue
            for concept_id in case["concept_ids"]:
                if case_id not in by_id[concept_id].get("fixture_case_ids", []):
                    raise FreezeError("fixture evidence is not linked back from capability")


def _validate_platform_fixture_reviews(
    documents: Mapping[str, List[Any]],
    ontology_by_id: Mapping[str, Mapping[str, Any]],
    record_by_role: Mapping[str, List[Mapping[str, Any]]],
) -> None:
    reviews = documents["platform_fixture_review"][0]
    _schema_or_freeze(
        validate_schema_records,
        reviews,
        "platform_fixture_review",
        context="platform fixture review",
    )
    registry = documents["metadrive_pg_block_registry"][0]
    registry_sha256 = record_by_role["metadrive_pg_block_registry"][0]["sha256"]
    token_registry = documents["metadrive_pg_token_registry"][0]
    token_registry_record = record_by_role["metadrive_pg_token_registry"][0]
    token_registry_sha256 = token_registry_record["sha256"]
    token_registry_binding = {
        key: token_registry_record[key] for key in ("path", "sha256", "bytes")
    }
    ontology_sha256 = record_by_role["common_ontology_registry"][0]["sha256"]
    expected = {
        ("registry_implementation", "metadrive:pg_block_registry"): {
            "platform": "metadrive",
            "subject_sha256": registry_sha256,
            "audit_subject_sha256": sha256_bytes(
                canonical_json_bytes(
                    build_metadrive_registry_audit_subject(
                        registry,
                        registry_sha256,
                        token_registry,
                        token_registry_binding,
                    )
                )
            ),
            "ontology_definition_sha256": ontology_sha256,
            "metadrive_pg_registry_sha256": registry_sha256,
            "metadrive_pg_token_registry_sha256": token_registry_sha256,
            "input_contract": PG_BLOCK_SCENE_CONTRACT,
        }
    }
    deterministic = documents["semantic_fixture_corpus"][0]
    for case in deterministic.get("cases", []):
        subject_id = case["case_id"]
        concept_id = case["concept_ids"][0]
        platform = case["platform"]
        key = ("fixture_case", subject_id)
        if key in expected:
            raise FreezeError("platform fixture review subjects are not unique")
        audit_subject = (
            _schema_or_freeze(
                build_metadrive_fixture_audit_subject,
                case,
                registry,
                registry_sha256,
                cross_platform=False,
            )
            if platform == "metadrive"
            else case
        )
        expected[key] = {
            "platform": platform,
            "subject_sha256": sha256_bytes(canonical_json_bytes(case)),
            "audit_subject_sha256": sha256_bytes(
                canonical_json_bytes(audit_subject)
            ),
            "ontology_definition_sha256": ontology_by_id[concept_id][
                "definition_sha256"
            ],
            "metadrive_pg_registry_sha256": (
                registry_sha256 if platform == "metadrive" else None
            ),
            "metadrive_pg_token_registry_sha256": (
                token_registry_sha256 if platform == "metadrive" else None
            ),
            "input_contract": (
                PG_BLOCK_SCENE_CONTRACT
                if platform == "metadrive"
                else deterministic["input_contract"]
            ),
        }
    for case in documents["cross_platform_fixture_corpus"][0].get("cases", []):
        subject_id = case["case_id"]
        key = ("fixture_case", subject_id)
        if key in expected:
            raise FreezeError("platform fixture review subjects are not unique")
        audit_subject = _schema_or_freeze(
            build_metadrive_fixture_audit_subject,
            case,
            registry,
            registry_sha256,
            cross_platform=True,
        )
        expected[key] = {
            "platform": "cross_platform",
            "subject_sha256": sha256_bytes(canonical_json_bytes(case)),
            "audit_subject_sha256": sha256_bytes(
                canonical_json_bytes(audit_subject)
            ),
            "ontology_definition_sha256": ontology_by_id[case["concept_id"]][
                "definition_sha256"
            ],
            "metadrive_pg_registry_sha256": registry_sha256,
            "metadrive_pg_token_registry_sha256": token_registry_sha256,
            "input_contract": PG_BLOCK_SCENE_CONTRACT,
        }
    for platform in ("carla", "metadrive"):
        capability = documents["platform_capability_{}".format(platform)][0]
        for concept in capability["concepts"]:
            subject_id = "{}:{}".format(platform, concept["concept_id"])
            audit_subject = (
                _schema_or_freeze(
                    build_metadrive_capability_audit_subject,
                    concept,
                    registry,
                    registry_sha256,
                )
                if platform == "metadrive"
                else concept
            )
            expected[("capability_status", subject_id)] = {
                "platform": platform,
                "subject_sha256": sha256_bytes(canonical_json_bytes(concept)),
                "audit_subject_sha256": sha256_bytes(
                    canonical_json_bytes(audit_subject)
                ),
                "ontology_definition_sha256": ontology_by_id[concept["concept_id"]][
                    "definition_sha256"
                ],
                "metadrive_pg_registry_sha256": (
                    registry_sha256 if platform == "metadrive" else None
                ),
                "metadrive_pg_token_registry_sha256": (
                    token_registry_sha256 if platform == "metadrive" else None
                ),
                "input_contract": (
                    PG_BLOCK_SCENE_CONTRACT
                    if platform == "metadrive"
                    else "raw_platform_artifact_v0.1"
                ),
            }
    actual = {}
    for review in reviews:
        key = (review["review_kind"], review["subject_id"])
        if key in actual:
            raise FreezeError("platform fixture review repeats a subject")
        actual[key] = review
        binding = expected.get(key)
        if binding is None or any(
            review.get(field) != value for field, value in binding.items()
        ):
            raise FreezeError("platform fixture review is not bound to its subject")
        reviewer = review.get("reviewer", {})
        if (
            review.get("gold_record_id") != _human_gold_record_id(review)
            or review.get("workflow_type")
            != "platform_fixture_capability_human_gold"
            or review.get("review_status") != "complete"
            or review.get("decision_status") != "confirmed"
            or not reviewer.get("reviewer_id")
            or reviewer.get("verdict") != "approve"
        ):
            raise FreezeError("platform fixture review lacks complete human gold")
    if set(actual) != set(expected):
        raise FreezeError("platform fixture review does not exactly cover all subjects")


def _validate_protocol_decision_reviews(
    documents: Mapping[str, List[Any]],
    record_by_role: Mapping[str, List[Mapping[str, Any]]],
) -> None:
    review_document = documents["protocol_decision_review"][0]
    _schema_or_freeze(
        validate_schema_instance,
        review_document,
        "protocol_decision_review",
        context="protocol decision review",
    )
    _require(
        review_document,
        workflow_type="protocol_decision_human_gold_collection",
        review_policy="one_complete_human_gold_per_subject",
        review_status="complete",
        decision_status="confirmed",
    )
    role_sha256 = {
        role: [record["sha256"] for record in records]
        for role, records in record_by_role.items()
    }
    expected = _expected_protocol_decision_subjects(
        role_sha256,
        documents["semantic_extractor_manifest"][0],
    )
    if set(expected) != set(PROTOCOL_DECISION_IDS):
        raise FreezeError("internal protocol decision registry is incomplete")
    actual = {}
    for review in review_document.get("decisions", []):
        decision_id = review.get("decision_id")
        if decision_id in actual:
            raise FreezeError("protocol decision review repeats a subject")
        actual[decision_id] = review
        subject = expected.get(decision_id)
        reviewer = review.get("reviewer", {})
        if (
            subject is None
            or canonical_json_bytes(review.get("subject"))
            != canonical_json_bytes(subject)
            or review.get("subject_sha256")
            != sha256_bytes(canonical_json_bytes(subject))
            or review.get("gold_record_id") != _human_gold_record_id(review)
            or not reviewer.get("reviewer_id")
            or reviewer.get("verdict") != "approve"
            or review.get("review_status") != "complete"
            or review.get("decision_status") != "confirmed"
        ):
            raise FreezeError(
                "protocol decision {} lacks exact complete human gold".format(
                    decision_id
                )
            )
    if set(actual) != set(expected):
        raise FreezeError("protocol decision review does not exactly cover all subjects")


def _validate_development_calibration_sources(
    documents: Mapping[str, List[Any]],
    record_by_role: Mapping[str, List[Mapping[str, Any]]],
    dev_path: Path,
    dev_ids: set,
    dev_by_id: Mapping[str, Mapping[str, Any]],
    cluster_mapping: Mapping[str, str],
    extractor_asset_sha256: str,
    extractor: Mapping[str, Any],
    entrypoints: Mapping[str, Mapping[str, Any]],
) -> tuple:
    """Validate two benchmark-owned 48x5 calibration reference corpora."""

    configs = documents["judge_calibration_source_config"]
    config_assets = record_by_role["judge_calibration_source_config"]
    if len(configs) != len(CALIBRATION_PLATFORMS):
        raise FreezeError("judge calibration requires one source per platform")
    source_by_platform = {}
    source_by_id = {}
    for config, asset in zip(configs, config_assets):
        _schema_or_freeze(
            validate_schema_instance,
            config,
            "judge_calibration_source_config",
            context="judge calibration source",
        )
        platform = config["platform"]
        source_id = config["source_id"]
        if platform in source_by_platform or source_id in source_by_id:
            raise FreezeError("judge calibration source IDs/platforms must be unique")
        implementation_bundle_path = _verify_file_binding(
            config.get("implementation_bundle"),
            "judge calibration source implementation bundle",
        )
        implementation_binding = config["implementation_bundle"]
        if implementation_binding.get("bytes") != implementation_bundle_path.stat().st_size:
            raise FreezeError("judge calibration implementation bundle byte count changed")
        try:
            implementation_bundle = strict_json_object_bytes(
                implementation_bundle_path.read_bytes(),
                "judge calibration implementation bundle",
            )
        except (OSError, ValidationError) as exc:
            raise FreezeError(
                "judge calibration implementation bundle is not valid UTF-8 JSON"
            ) from exc
        expected_bundle_keys = {
            "schema_version",
            "bundle_id",
            "platform",
            "source_id",
            "source_config",
            "producer_source",
            "semantic_extractor_manifest",
            "metadrive_token_registry",
        }
        if (
            not isinstance(implementation_bundle, Mapping)
            or set(implementation_bundle) != expected_bundle_keys
            or implementation_bundle.get("schema_version") != SCHEMA_VERSION
            or implementation_bundle.get("bundle_id")
            != "{}-implementation-v0.1".format(source_id)
            or implementation_bundle.get("platform") != platform
            or implementation_bundle.get("source_id") != source_id
            or implementation_bundle.get("source_config")
            != {
                key: value
                for key, value in config.items()
                if key != "implementation_bundle"
            }
        ):
            raise FreezeError("judge calibration implementation bundle contract changed")
        _verify_file_binding(
            implementation_bundle.get("producer_source"),
            "judge calibration implementation producer source",
        )
        extractor_binding = implementation_bundle.get("semantic_extractor_manifest")
        extractor_path = _verify_file_binding(
            extractor_binding,
            "judge calibration implementation extractor manifest",
        )
        if (
            extractor_binding.get("bytes") != extractor_path.stat().st_size
            or extractor_binding.get("sha256") != extractor_asset_sha256
        ):
            raise FreezeError(
                "judge calibration implementation uses another extractor manifest"
            )
        token_binding = implementation_bundle.get("metadrive_token_registry")
        if platform == "carla":
            if token_binding is not None:
                raise FreezeError("CARLA calibration implementation cannot bind MetaDrive tokens")
            extractor_artifact_format = "scenic"
            extractor_trusted_dependencies = []
        else:
            token_assets = record_by_role["metadrive_pg_token_registry"]
            if len(token_assets) != 1:
                raise FreezeError(
                    "freeze lacks one MetaDrive PG token registry"
                )
            token_dependency = _require_exact_metadrive_registry_binding(
                token_binding,
                token_assets[0],
                "MetaDrive calibration implementation token registry",
            )
            extractor_artifact_format = PG_BLOCK_SCENE_CONTRACT
            extractor_trusted_dependencies = [token_dependency]
        source = {
            "config": config,
            "config_asset": asset,
            "implementation_bundle_sha256": implementation_binding["sha256"],
            "extractor_artifact_format": extractor_artifact_format,
            "extractor_trusted_dependencies": extractor_trusted_dependencies,
        }
        source_by_platform[platform] = source
        source_by_id[source_id] = source
    if set(source_by_platform) != set(CALIBRATION_PLATFORMS):
        raise FreezeError("judge calibration sources do not cover both platforms")

    for roster in documents["development_query_roster"]:
        _schema_or_freeze(
            validate_schema_instance,
            roster,
            "query_roster",
            context="development query roster",
        )
        _require(roster, decision_status="confirmed")
        source = source_by_id.get(roster.get("method_id"))
        if source is None or source["config"]["platform"] != roster.get("platform"):
            raise FreezeError("development roster lacks a matching reference source")
        if "roster" in source:
            raise FreezeError("reference source has multiple development rosters")
        development_entries = roster_entries(roster)
        if set(development_entries) != dev_ids or len(development_entries) != 48:
            raise FreezeError("judge development roster must contain all 48 dev queries")
        if (
            roster.get("library_sha256")
            != record_by_role["query_library_dev"][0]["sha256"]
            or Path(roster.get("library_path", "")).resolve() != dev_path.resolve()
        ):
            raise FreezeError("judge development roster is not bound to the dev library")
        for query_id, entry in development_entries.items():
            query = dev_by_id[query_id]
            expected = {
                "intent_group_id": query["intent_group_id"],
                "statistical_intent_cluster_id": apply_statistical_cluster(
                    query["intent_group_id"], cluster_mapping
                ),
                "surface_style": query["surface_style"],
                "expected_support": query["expected_support"],
            }
            if any(entry.get(field) != value for field, value in expected.items()):
                raise FreezeError("development roster metadata differs from the dev library")
        source["roster"] = roster
    if any("roster" not in source for source in source_by_platform.values()):
        raise FreezeError("each calibration source requires one development roster")

    for response_records in documents["development_response_records"]:
        _schema_or_freeze(
            validate_schema_records,
            response_records,
            "response_record",
            context="development response",
        )
        identities = {
            (record.get("method_id"), record.get("platform"))
            for record in response_records
        }
        if len(identities) != 1:
            raise FreezeError("one development response asset must contain one source")
        source_id, platform = next(iter(identities))
        source = source_by_id.get(source_id)
        if source is None or source["config"]["platform"] != platform:
            raise FreezeError("development responses lack a matching reference source")
        if "responses" in source:
            raise FreezeError("reference source has multiple response assets")
        _schema_or_freeze(
            validate_records_against_roster,
            response_records,
            source["roster"],
        )
        if len(response_records) != 240:
            raise FreezeError("each reference source requires 48x5 responses")
        for response in response_records:
            query = dev_by_id[response["query_id"]]
            expected_request = sha256_bytes(
                canonical_json_bytes({"query_text": query["query_text"]})
            )
            if response.get("request_sha256") != expected_request:
                raise FreezeError("development response is not query_text-only")
            if response.get("config_sha256") != source["config_asset"]["sha256"]:
                raise FreezeError("development response config hash is not frozen")
            if (
                response.get("implementation_bundle_sha256")
                != source["implementation_bundle_sha256"]
            ):
                raise FreezeError(
                    "development response implementation bundle hash is not frozen"
                )
            if response.get("disposition") != "generate" or response.get(
                "terminal_status"
            ) != "complete":
                raise FreezeError(
                    "judge calibration requires finalized generated dev outputs"
                )
            artifact_path = _verify_file_binding(
                response.get("artifact"), "development response artifact"
            )
            if artifact_path.stat().st_size != response["artifact"]["bytes"]:
                raise FreezeError("development response artifact byte count mismatch")
        source["responses"] = response_records
    if any("responses" not in source for source in source_by_platform.values()):
        raise FreezeError("each calibration source requires one response asset")

    for evidence_records in documents["development_evidence_records"]:
        _schema_or_freeze(
            validate_schema_records,
            evidence_records,
            "semantic_evidence",
            context="development semantic evidence",
        )
        identities = {
            (record.get("method_id"), record.get("platform"))
            for record in evidence_records
        }
        if len(identities) != 1:
            raise FreezeError("one development evidence asset must contain one source")
        source_id, platform = next(iter(identities))
        source = source_by_id.get(source_id)
        if source is None or source["config"]["platform"] != platform:
            raise FreezeError("development evidence lacks a matching reference source")
        if "evidence" in source:
            raise FreezeError("reference source has multiple evidence assets")
        _schema_or_freeze(
            validate_records_against_roster,
            evidence_records,
            source["roster"],
        )
        if len(evidence_records) != 240:
            raise FreezeError("each reference source requires 48x5 evidence records")
        source["evidence"] = evidence_records
    if any("evidence" not in source for source in source_by_platform.values()):
        raise FreezeError("each calibration source requires one evidence asset")

    response_by_run = {}
    evidence_by_run = {}
    live_projection_cache = {}
    for source in source_by_platform.values():
        source_responses = {record["run_id"]: record for record in source["responses"]}
        source_evidence = {record["run_id"]: record for record in source["evidence"]}
        if (
            len(source_responses) != len(source["responses"])
            or len(source_evidence) != len(source["evidence"])
            or set(source_responses) != set(source_evidence)
        ):
            raise FreezeError("development response/evidence run sets differ")
        if set(response_by_run) & set(source_responses):
            raise FreezeError("development run IDs must be globally unique")
        response_by_run.update(source_responses)
        evidence_by_run.update(source_evidence)
        for run_id, evidence in source_evidence.items():
            response = source_responses[run_id]
            for field in (
                "query_id",
                "intent_group_id",
                "statistical_intent_cluster_id",
                "surface_style",
                "expected_support",
                "repetition",
                "method_id",
                "platform",
                "terminal_status",
            ):
                if evidence.get(field) != response.get(field):
                    raise FreezeError("development evidence/response metadata mismatch")
            provenance = evidence.get("provenance", {})
            response_sha256 = sha256_bytes(canonical_json_bytes(response))
            artifact_sha256 = response["artifact"]["sha256"]
            if (
                provenance.get("source_response_sha256") != response_sha256
                or provenance.get("source_artifact_sha256") != artifact_sha256
                or provenance.get("extractor_config_sha256")
                != extractor_asset_sha256
                or provenance.get("extractor_id") != extractor.get("extractor_id")
                or provenance.get("extractor_version")
                != extractor.get("extractor_version")
            ):
                raise FreezeError("development evidence provenance is not frozen")
            extractor_input = build_extractor_input(
                response,
                artifact_format=source["extractor_artifact_format"],
                trusted_dependencies=source["extractor_trusted_dependencies"],
            )
            cache_key = (
                response["platform"],
                sha256_bytes(canonical_json_bytes(extractor_input)),
            )
            if cache_key not in live_projection_cache:
                live_projection_cache[cache_key] = _validate_extractor_projection(
                    _execute_extractor_once(
                        entrypoints[response["platform"]],
                        extractor_input,
                        "development {}".format(response["platform"]),
                    ),
                    "development semantic evidence",
                )
            if canonical_json_bytes(
                live_projection_cache[cache_key]
            ) != canonical_json_bytes(evidence_projection(evidence)):
                raise FreezeError("development evidence differs from the live extractor")
    return source_by_platform, response_by_run, evidence_by_run


def _oracle_record_hash(record: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(record))


def _schema_or_freeze(function: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except ValidationError as exc:
        raise FreezeError(str(exc)) from exc


def _human_gold_record_id(record: Mapping[str, Any]) -> str:
    """Hash a human-gold record without its self-identifying hash field."""

    return sha256_bytes(
        canonical_json_bytes(
            {key: value for key, value in record.items() if key != "gold_record_id"}
        )
    )


def _validate_completed_oracle_decision(
    decision: Mapping[str, Any], source: Mapping[str, Any], label: str
) -> None:
    _schema_or_freeze(
        validate_schema_instance, decision, "oracle_record", context=label
    )
    for field, source_field in (
        ("query_id", "query_id"),
        ("intent_group_id", "intent_group_id"),
        ("surface_style", "surface_style"),
        ("expected_support", "expected_support"),
        ("acceptable_response", "acceptable_response"),
        ("unsupported_reasons", "expected_reason_if_unsupported"),
    ):
        if decision.get(field, []) != source.get(source_field, []):
            raise FreezeError("{} differs from query-library identity".format(label))
    if decision.get("decision_status") != "confirmed":
        raise FreezeError("{} is not a completed oracle decision".format(label))
    if any(
        atom.get("decision_status") != "confirmed"
        for atom in decision.get("atoms", [])
    ):
        raise FreezeError("{} contains an unfinished atom decision".format(label))
    if decision.get("cpd_policy", {}).get("decision_status") != "confirmed":
        raise FreezeError("{} contains an unfinished CPD decision".format(label))


ATOM_SEMANTIC_FIELDS = (
    "category",
    "predicate",
    "arguments",
    "layer",
    "polarity",
    "weight",
)


def _atom_semantic_projection(atom: Mapping[str, Any]) -> Dict[str, Any]:
    return {field: copy.deepcopy(atom.get(field)) for field in ATOM_SEMANTIC_FIELDS}


def _atom_identity_id(atom: Mapping[str, Any]) -> str:
    identity = {
        field: copy.deepcopy(atom.get(field))
        for field in ("category", "predicate", "arguments", "polarity")
    }
    return sha256_bytes(canonical_json_bytes(identity))[:16]


def _atom_projection_hash(atoms: Iterable[Mapping[str, Any]]) -> str:
    return sha256_bytes(
        canonical_json_bytes([_atom_semantic_projection(atom) for atom in atoms])
    )


def _validate_review_payload(
    payload: Mapping[str, Any],
    draft: Mapping[str, Any],
    source: Mapping[str, Any],
    label: str,
) -> None:
    """Require an explicit decision for every machine-drafted atom and check."""

    decision = payload["decision"]
    _validate_completed_oracle_decision(decision, source, label)
    check_status = payload.get("required_check_status", {})
    if set(check_status) != set(REQUIRED_REVIEW_CHECKS) or any(
        check_status.get(check) != "complete" for check in REQUIRED_REVIEW_CHECKS
    ):
        raise FreezeError("{} does not cover every required review check".format(label))

    draft_by_id = {atom["atom_id"]: atom for atom in draft.get("atoms", [])}
    draft_atom_ids = set(draft_by_id)
    reviews = payload.get("draft_atom_reviews", [])
    review_by_id = {}
    for review in reviews:
        draft_atom_id = review.get("draft_atom_id")
        if draft_atom_id in review_by_id:
            raise FreezeError("{} repeats a draft atom review".format(label))
        review_by_id[draft_atom_id] = review
    if set(review_by_id) != draft_atom_ids:
        raise FreezeError("{} does not explicitly review every draft atom".format(label))

    decision_atoms = decision.get("atoms", [])
    decision_by_id = {atom["atom_id"]: atom for atom in decision_atoms}
    if len(decision_by_id) != len(decision_atoms):
        raise FreezeError("{} repeats a final atom ID".format(label))
    for atom_id, atom in decision_by_id.items():
        if atom_id != _atom_identity_id(atom):
            raise FreezeError("{} contains a forged semantic atom ID".format(label))

    target_uses = {}
    merge_groups = {}
    for draft_atom_id, review in review_by_id.items():
        verdict = review.get("verdict")
        replacement_ids = review.get("replacement_atom_ids", [])
        replacements = set(replacement_ids)
        if len(replacements) != len(replacement_ids):
            raise FreezeError("{} repeats a replacement atom".format(label))
        draft_atom = draft_by_id[draft_atom_id]
        if review.get("before_sha256") != _atom_projection_hash([draft_atom]):
            raise FreezeError("{} draft-atom before hash mismatch".format(label))
        if any(atom_id not in decision_by_id for atom_id in replacements):
            raise FreezeError("{} references a missing replacement atom".format(label))
        after_atoms = [decision_by_id[atom_id] for atom_id in replacement_ids]
        if verdict == "accept":
            after_atoms = [decision_by_id[draft_atom_id]] if draft_atom_id in decision_by_id else []
        if review.get("after_sha256") != _atom_projection_hash(after_atoms):
            raise FreezeError("{} draft-atom after hash mismatch".format(label))
        changed_fields = {
            field
            for target in after_atoms
            for field in ATOM_SEMANTIC_FIELDS
            if draft_atom.get(field) != target.get(field)
        }
        if set(review.get("changed_fields", [])) != changed_fields:
            raise FreezeError("{} draft-atom changed fields mismatch".format(label))
        if verdict == "accept":
            if (
                replacements
                or draft_atom_id not in decision_by_id
                or _atom_semantic_projection(decision_by_id[draft_atom_id])
                != _atom_semantic_projection(draft_atom)
                or review.get("operation_group_id") is not None
            ):
                raise FreezeError("{} has an inconsistent accepted atom".format(label))
            replacements = {draft_atom_id}
        elif verdict == "reject":
            if (
                replacements
                or draft_atom_id in decision_by_id
                or review.get("operation_group_id") is not None
            ):
                raise FreezeError("{} has an inconsistent rejected atom".format(label))
        elif verdict == "modify":
            if (
                len(replacements) != 1
                or not changed_fields
                or review.get("operation_group_id") is not None
            ):
                raise FreezeError("{} has an inconsistent modified atom".format(label))
        elif verdict == "split":
            if (
                len(replacements) < 2
                or not changed_fields
                or review.get("operation_group_id") is not None
            ):
                raise FreezeError("{} has an inconsistent split atom".format(label))
        elif verdict == "merge":
            group_id = review.get("operation_group_id")
            if len(replacements) != 1 or not group_id or not changed_fields:
                raise FreezeError("{} has an inconsistent merged atom".format(label))
            merge_groups.setdefault(group_id, []).append(
                (draft_atom_id, next(iter(replacements)))
            )
        else:
            raise FreezeError("{} contains an invalid draft atom verdict".format(label))
        for target_id in replacements:
            target_uses.setdefault(target_id, []).append(
                (draft_atom_id, verdict, review.get("operation_group_id"))
            )

    valid_merge_targets = set()
    for group_id, members in merge_groups.items():
        targets = {target for _, target in members}
        if len(members) < 2 or len(targets) != 1:
            raise FreezeError("{} has an incomplete merge group".format(label))
        target_id = next(iter(targets))
        uses = target_uses.get(target_id, [])
        if len(uses) != len(members) or any(
            verdict != "merge" or used_group != group_id
            for _, verdict, used_group in uses
        ):
            raise FreezeError("{} has an implicit or cross-group merge".format(label))
        valid_merge_targets.add(target_id)
    for target_id, uses in target_uses.items():
        if target_id not in valid_merge_targets and len(uses) != 1:
            raise FreezeError("{} reuses a final atom without an explicit merge".format(label))

    added_reviews = payload.get("added_atom_reviews", [])
    added_by_id = {review.get("atom_id"): review for review in added_reviews}
    if len(added_by_id) != len(added_reviews):
        raise FreezeError("{} repeats an added-atom review".format(label))
    derived_targets = set(target_uses)
    expected_additions = set(decision_by_id) - derived_targets
    if set(added_by_id) != expected_additions:
        raise FreezeError("{} does not explicitly review every added atom".format(label))
    for atom_id in expected_additions:
        if decision_by_id[atom_id].get("provenance", {}).get("source") != "human_review":
            raise FreezeError("{} added atom lacks human-review provenance".format(label))


def _validate_asset_contract_once(records: List[Mapping[str, Any]]) -> None:
    documents = _load_role_documents(records)
    record_by_role = {}
    for record in records:
        record_by_role.setdefault(record["role"], []).append(record)

    test_path = Path(record_by_role["query_library_test"][0]["path"])
    dev_path = Path(record_by_role["query_library_dev"][0]["path"])
    validate_library(test_path, 252, 84, 228)
    validate_library(dev_path, 48, 16, 36)
    test_rows = documents["query_library_test"][0]
    dev_rows = documents["query_library_dev"][0]
    test_by_id = _record_map(test_rows, "query_library_test")
    dev_by_id = _record_map(dev_rows, "query_library_dev")
    test_ids = set(test_by_id)
    dev_ids = set(dev_by_id)

    oracle_by_role = {}
    for role, library_by_id in (
        ("requirement_oracle_test", test_by_id),
        ("requirement_oracle_dev", dev_by_id),
    ):
        oracle_by_id = _record_map(documents[role][0], role)
        _schema_or_freeze(
            validate_schema_records, oracle_by_id.values(), "oracle_record", context=role
        )
        if set(oracle_by_id) != set(library_by_id):
            raise FreezeError("{} does not exactly match its query library".format(role))
        for query_id, row in oracle_by_id.items():
            source = library_by_id[query_id]
            for field in (
                "intent_group_id",
                "surface_style",
                "expected_support",
                "acceptable_response",
            ):
                if row.get(field) != source.get(field):
                    raise FreezeError(
                        "{} {} differs from the query library".format(role, field)
                    )
            if row.get("unsupported_reasons", []) != source.get(
                "expected_reason_if_unsupported", []
            ):
                raise FreezeError("{} unsupported reasons differ from the library".format(role))
            if row.get("decision_status") != "confirmed":
                raise FreezeError("{} contains an unconfirmed oracle".format(role))
            atoms = row.get("atoms")
            if not isinstance(atoms, list):
                raise FreezeError("{} atoms must be a list".format(role))
            atom_ids = [atom.get("atom_id") for atom in atoms if isinstance(atom, Mapping)]
            if len(atom_ids) != len(atoms) or any(not atom_id for atom_id in atom_ids):
                raise FreezeError("{} contains a malformed atom".format(role))
            if len(set(atom_ids)) != len(atom_ids):
                raise FreezeError("{} contains duplicate atom IDs within a query".format(role))
            if any(atom.get("decision_status") != "confirmed" for atom in atoms):
                raise FreezeError("{} contains an unconfirmed atom".format(role))
            policy = row.get("cpd_policy", {})
            if policy.get("decision_status") != "confirmed":
                raise FreezeError("{} contains an unconfirmed CPD policy".format(role))
            expected_candidate = source["expected_support"] == "supported" and source[
                "surface_style"
            ] in ("partial", "vague")
            if policy.get("candidate") is not expected_candidate:
                raise FreezeError("{} contains an inconsistent CPD candidate flag".format(role))
        oracle_by_role[role] = oracle_by_id

    # The evaluator ontology is pre-registered and may be extended by dev only;
    # locked-test values must never shape extractor or Judge capability claims.
    computed_ontology = build_common_ontology(
        oracle_by_role["requirement_oracle_dev"].values()
    )
    ontology = documents["common_ontology_registry"][0]
    _schema_or_freeze(
        validate_schema_instance,
        ontology,
        "common_ontology",
        context="common ontology",
    )
    if canonical_json_bytes(ontology) != canonical_json_bytes(computed_ontology):
        raise FreezeError(
            "common ontology registry differs from the confirmed oracle domains"
        )
    ontology_by_id = {
        concept["concept_id"]: concept for concept in ontology["concepts"]
    }

    cpd = documents["cpd_policy"][0]
    _schema_or_freeze(
        validate_schema_instance,
        cpd,
        "cpd_coverage",
        context="CPD coverage",
    )
    computed_cpd = compute_coverage(oracle_by_role["requirement_oracle_test"].values())
    _schema_or_freeze(
        validate_cpd_policy_routing,
        oracle_by_role["requirement_oracle_test"].values(),
    )
    for field in (
        "decision_status",
        "eligible",
        "candidate",
        "coverage",
        "by_style",
        "joint",
        "eligible_query_ids",
        "eligible_query_ids_sha256",
    ):
        if cpd.get(field) != computed_cpd.get(field):
            raise FreezeError("CPD policy summary differs from confirmed test oracles")
    if computed_cpd["candidate"] != 152:
        raise FreezeError("CPD policy must cover all 152 supported partial/vague candidates")
    clusters = documents["statistical_clusters"][0]
    _require(clusters, decision_status="confirmed")
    cluster_mapping = statistical_cluster_map(clusters)
    known_intent_ids = {row["intent_group_id"] for row in test_rows + dev_rows}
    unknown_cluster_members = sorted(set(cluster_mapping) - known_intent_ids)
    if unknown_cluster_members:
        raise FreezeError(
            "statistical clusters reference unknown intents: {}".format(
                unknown_cluster_members
            )
        )

    required_capability_concepts = set(ontology_by_id)
    capability_status_by_platform = {}
    for platform in ("carla", "metadrive"):
        capability = documents["platform_capability_{}".format(platform)][0]
        _schema_or_freeze(
            validate_schema_instance,
            capability,
            "platform_capability",
            context="{} capability".format(platform),
        )
        _require(
            capability,
            platform=platform,
            decision_status="confirmed",
            method_independent=True,
            ontology_id="bus-common-v0.2",
            ontology_sha256=record_by_role["common_ontology_registry"][0]["sha256"],
        )
        concepts = capability.get("concepts")
        if not isinstance(concepts, list) or not concepts:
            raise FreezeError("{} capability manifest is empty".format(platform))
        concept_ids = [concept.get("concept_id") for concept in concepts]
        if (
            any(not concept_id for concept_id in concept_ids)
            or len(set(concept_ids)) != len(concept_ids)
            or any(
                concept.get("status")
                not in (
                    "native",
                    "constructible",
                    "judge_required",
                    "sidecar_only",
                    "unsupported",
                )
                for concept in concepts
            )
        ):
            raise FreezeError("{} capability concepts are malformed".format(platform))
        if set(concept_ids) != required_capability_concepts:
            raise FreezeError(
                "{} capability manifest does not exactly cover the benchmark ontology".format(
                    platform
                )
            )
        for concept in concepts:
            concept_id = concept["concept_id"]
            definition = ontology_by_id[concept_id]["definition"]
            expected_mode = {
                "native": "native_observation",
                "constructible": "constructed_native",
                "judge_required": "judge",
                "sidecar_only": "sidecar",
                "unsupported": "none",
            }[concept["status"]]
            if concept.get("evidence_mode") != expected_mode:
                raise FreezeError(
                    "{} capability status/evidence mode disagree".format(platform)
                )
            if concept.get("definition_sha256") != ontology_by_id[concept_id].get(
                "definition_sha256"
            ):
                raise FreezeError(
                    "{} capability concept definition hash mismatch".format(platform)
                )
            if not isinstance(concept.get("rationale"), str) or not concept[
                "rationale"
            ].strip():
                raise FreezeError("{} capability concept lacks a rationale".format(platform))
            if definition["kind"] == "requirement_atom_domain":
                expected_values = deterministic_representative_arguments(
                    definition["category"],
                    definition["predicate"],
                    definition["polarity"],
                    definition["argument_domain"],
                )
                if (
                    concept.get("coverage_scope") != "registered_values_only"
                    or concept.get("uncovered_route") != "judge"
                    or concept.get("status")
                    != ("constructible" if expected_values else "judge_required")
                    or canonical_json_bytes(
                        concept.get("deterministic_argument_values")
                    )
                    != canonical_json_bytes(expected_values)
                    or any(
                        value not in definition["argument_domain"]
                        for value in expected_values
                    )
                ):
                    raise FreezeError(
                        "{} capability overstates requirement argument coverage".format(
                            platform
                        )
                    )
            elif (
                concept.get("coverage_scope") != "full_definition"
                or concept.get("uncovered_route") != "not_applicable"
                or concept.get("deterministic_argument_values") != []
            ):
                raise FreezeError(
                    "{} non-requirement capability has invalid coverage metadata".format(
                        platform
                    )
                )
            fixture_case_ids = concept.get("fixture_case_ids")
            if (
                not isinstance(fixture_case_ids, list)
                or len(set(fixture_case_ids)) != len(fixture_case_ids)
                or any(not isinstance(case_id, str) or not case_id for case_id in fixture_case_ids)
            ):
                raise FreezeError("{} capability fixture references are malformed".format(platform))
            binding = concept.get("evidence_binding")
            if concept["status"] == "unsupported":
                if fixture_case_ids or binding is not None:
                    raise FreezeError(
                        "unsupported {} capability cannot claim implementation evidence".format(
                            platform
                        )
                    )
            elif not isinstance(binding, Mapping) or not fixture_case_ids:
                raise FreezeError(
                    "supported {} capability lacks implementation and fixture evidence".format(
                        platform
                    )
                )
        capability_status_by_platform[platform] = {
            concept["concept_id"]: concept["status"] for concept in concepts
        }
        platform_config = documents["platform_config_{}".format(platform)][0]
        _schema_or_freeze(
            validate_schema_instance,
            platform_config,
            "platform_runtime_config",
            context="{} platform runtime config".format(platform),
        )
        _require(
            platform_config,
            platform=platform,
            status="frozen",
            sv_seeds=[0, 1, 2, 3, 4],
            sv_max_iterations=2000,
            rollout_seconds=30.0,
        )
        runtime_worker = platform_config["worker"]
        try:
            from .runtime import validate_platform_runtime_config

            validate_platform_runtime_config(
                platform_config,
                platform,
                allow_draft=False,
            )
        except ValidationError as exc:
            raise FreezeError(
                "{} platform runtime config is not fully provenance-bound: {}".format(
                    platform, exc
                )
            ) from exc
        if platform == "metadrive":
            token_assets = record_by_role["metadrive_pg_token_registry"]
            if len(token_assets) != 1:
                raise FreezeError("freeze lacks one MetaDrive PG token registry")
            _require_exact_metadrive_registry_binding(
                platform_config.get("token_registry"),
                token_assets[0],
                "MetaDrive platform runtime token registry",
            )
        for path_field, hash_field in (
            ("interpreter", "interpreter_sha256"),
            ("worker", "worker_sha256"),
        ):
            bound_path = Path(runtime_worker[path_field]).resolve()
            if (
                not bound_path.is_file()
                or sha256_file(bound_path) != runtime_worker[hash_field]
            ):
                raise FreezeError(
                    "{} platform runtime {} binding changed".format(
                        platform, path_field
                    )
                )
        controller = documents["controller_config_{}".format(platform)][0]
        _schema_or_freeze(
            validate_schema_instance,
            controller,
            "controller_config",
            context="{} controller config".format(platform),
        )
        try:
            from .runtime import _validate_controller_config

            _validate_controller_config(controller, platform)
        except ValidationError as exc:
            raise FreezeError(
                "{} controller config is not runnable/frozen: {}".format(platform, exc)
            ) from exc
        if (
            Path(controller["implementation"]["source_path"]).resolve()
            != Path(runtime_worker["worker"]).resolve()
            or controller["implementation"]["source_sha256"]
            != runtime_worker["worker_sha256"]
        ):
            raise FreezeError(
                "{} controller and runtime worker source bindings differ".format(platform)
            )

    uqh_registry = documents["uqh_assessor_registry"][0]
    _schema_or_freeze(
        validate_schema_instance,
        uqh_registry,
        "uqh_assessor_registry",
        context="UQH assessor registry",
    )
    _require(
        uqh_registry,
        status="frozen",
        protocol_version="0.1",
        assessment_binding_contract="uqh_assessment_binding_v0.1",
        controlled_degradation_credit=False,
    )
    assessor_ids = []
    from .generation import PRODUCER_ID as GENERATION_PRODUCER_ID

    for assessor in uqh_registry.get("assessors", []):
        assessor_id = assessor.get("assessor_id")
        assessor_ids.append(assessor_id)
        _verify_file_binding(
            assessor.get("assessment_config"),
            "UQH assessor {} config".format(assessor_id),
        )
        _verify_file_binding(
            assessor.get("assessor_source"),
            "UQH assessor {} source".format(assessor_id),
        )
        public_key_path = _verify_file_binding(
            assessor.get("attestation_public_key"),
            "UQH assessor {} attestation public key".format(assessor_id),
        )
        if (
            assessor.get("attestation_algorithm") != "rsa_pss_sha256"
            or assessor.get("key_custody") != "external_to_method_runner"
            or assessor.get("independent_from_producer_ids")
            != [GENERATION_PRODUCER_ID]
        ):
            raise FreezeError(
                "UQH assessor is not independently attested from the formal generation runner"
            )
        try:
            key_check = subprocess.run(
                [
                    "openssl",
                    "pkey",
                    "-pubin",
                    "-in",
                    str(public_key_path),
                    "-text",
                    "-noout",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FreezeError("UQH public-key verification is unavailable") from exc
        key_description = key_check.stdout.decode("utf-8", errors="replace")
        key_bits = re.search(r"Public-Key:\s*\((\d+) bit\)", key_description)
        if (
            key_check.returncode != 0
            or key_bits is None
            or int(key_bits.group(1)) < 2048
        ):
            raise FreezeError("UQH assessor attestation public key is invalid")
    if len(assessor_ids) != len(set(assessor_ids)):
        raise FreezeError("UQH assessor registry contains duplicate assessor IDs")

    eligible_cpd_concepts = {
        "cpd:{}".format(dimension["name"])
        for oracle in oracle_by_role["requirement_oracle_test"].values()
        if oracle.get("cpd_policy", {}).get("eligible") is True
        for dimension in oracle.get("cpd_policy", {}).get("dimensions", [])
    }
    unknown_eligible_cpd = sorted(
        eligible_cpd_concepts - required_capability_concepts
    )
    if unknown_eligible_cpd:
        raise FreezeError(
            "test-only CPD dimensions must be frozen ineligible: {}".format(
                unknown_eligible_cpd
            )
        )
    unavailable_cpd = sorted(
        concept_id
        for concept_id in eligible_cpd_concepts
        if any(
            capability_status_by_platform[platform][concept_id]
            not in ("native", "constructible")
            for platform in ("carla", "metadrive")
        )
    )
    if unavailable_cpd:
        raise FreezeError(
            "eligible CPD dimensions are not common across platforms: {}".format(
                unavailable_cpd
            )
        )

    _validate_capability_fixture_contract(documents, ontology_by_id)
    token_registry_record = record_by_role["metadrive_pg_token_registry"][0]
    token_registry_binding = {
        key: token_registry_record[key] for key in ("path", "sha256", "bytes")
    }
    _schema_or_freeze(
        validate_metadrive_pg_registry,
        documents["metadrive_pg_block_registry"][0],
        documents["metadrive_pg_token_registry"][0],
        token_registry_binding,
        verify_implementation_files=True,
    )
    _schema_or_freeze(
        validate_metadrive_registry_fixture_coverage,
        documents["metadrive_pg_block_registry"][0],
        documents["metadrive_pg_token_registry"][0],
        token_registry_record["sha256"],
        documents["semantic_fixture_corpus"][0],
        documents["cross_platform_fixture_corpus"][0],
    )

    extractor = documents["semantic_extractor_manifest"][0]
    _schema_or_freeze(
        validate_schema_instance,
        extractor,
        "semantic_extractor_manifest",
        context="semantic extractor manifest",
    )
    _require(
        extractor,
        decision_status="confirmed",
        deterministic_fixture_accuracy=1.0,
        cross_platform_equivalence_passed=True,
        input_contract="artifact_observation_v0.1",
        output_contract="semantic_projection_v0.1",
    )
    if not extractor.get("extractor_id") or not extractor.get("extractor_version"):
        raise FreezeError("semantic extractor manifest lacks a frozen identity")
    _validate_runtime_environments(
        extractor, documents["semantic_fixture_corpus"][0]
    )
    extractor_files = extractor.get("extractor_files")
    if not isinstance(extractor_files, list) or not extractor_files:
        raise FreezeError("semantic extractor manifest has no hashed files")
    extractor_paths = [
        _verify_file_binding(binding, "semantic extractor") for binding in extractor_files
    ]
    if len(set(extractor_paths)) != len(extractor_paths):
        raise FreezeError("semantic extractor manifest repeats a source file")
    entrypoints = _load_extractor_entrypoints(extractor)
    entrypoint_paths = {
        entrypoint["path"] for entrypoint in entrypoints.values()
    }
    if not entrypoint_paths <= set(extractor_paths):
        raise FreezeError("extractor_files must include every registered entrypoint")
    extractor_hash_by_platform = {
        platform: sha256_file(entrypoint["path"])
        for platform, entrypoint in entrypoints.items()
    }
    for platform in ("carla", "metadrive"):
        capability = documents["platform_capability_{}".format(platform)][0]
        for concept in capability["concepts"]:
            if concept["status"] == "unsupported":
                continue
            binding = concept["evidence_binding"]
            if (
                binding.get("kind") != "extractor"
                or binding.get("sha256") != extractor_hash_by_platform[platform]
            ):
                raise FreezeError(
                    "{} capability is not bound to its frozen extractor".format(platform)
                )
    metric_files = extractor.get("metric_files")
    if not isinstance(metric_files, list) or not metric_files:
        raise FreezeError("semantic extractor manifest has no hashed metric files")
    metric_paths = [
        _verify_file_binding(binding, "metric implementation") for binding in metric_files
    ]
    expected_metric_paths = {
        path.resolve() for path in Path(__file__).resolve().parent.glob("*.py")
    }
    if set(metric_paths) != expected_metric_paths or len(metric_paths) != len(
        expected_metric_paths
    ):
        raise FreezeError("complete evaluator implementation is not frozen exactly once")
    schema_files = extractor.get("schema_files")
    if not isinstance(schema_files, list) or not schema_files:
        raise FreezeError("semantic extractor manifest has no hashed schema files")
    schema_paths = [
        _verify_file_binding(binding, "evaluator schema") for binding in schema_files
    ]
    expected_schema_paths = set(supported_schema_paths())
    if set(schema_paths) != expected_schema_paths or len(schema_paths) != len(
        expected_schema_paths
    ):
        raise FreezeError("complete evaluator schemas are not frozen exactly once")
    deterministic_results = _verify_file_binding(
        extractor.get("deterministic_fixture_results"),
        "deterministic fixture results",
    )
    equivalence_results = _verify_file_binding(
        extractor.get("cross_platform_equivalence_results"),
        "cross-platform equivalence results",
    )
    deterministic_summary = _validate_fixture_results(
        documents["semantic_fixture_corpus"][0],
        deterministic_results,
        "deterministic",
        entrypoints,
    )
    deterministic_ontology_categories = {
        concept["definition"]["category"]
        for concept in ontology_by_id.values()
        if concept["definition"].get("kind") == "requirement_atom_domain"
        and deterministic_representative_arguments(
            concept["definition"]["category"],
            concept["definition"]["predicate"],
            concept["definition"]["polarity"],
            concept["definition"]["argument_domain"],
        )
    }
    if (
        deterministic_summary["case_count"] < 4
        or deterministic_summary["platforms"] != {"carla", "metadrive"}
        or deterministic_summary["categories"] != deterministic_ontology_categories
        or deterministic_summary["ego_positive"] == 0
        or deterministic_summary["ego_negative"] == 0
    ):
        raise FreezeError(
            "deterministic fixtures lack both platforms, registered deterministic categories, or ego controls"
        )
    _validate_fixture_results(
        documents["cross_platform_fixture_corpus"][0],
        equivalence_results,
        "cross-platform",
        entrypoints,
        equivalence=True,
    )
    _validate_platform_fixture_reviews(documents, ontology_by_id, record_by_role)
    _validate_protocol_decision_reviews(documents, record_by_role)

    extractor_asset_sha256 = record_by_role["semantic_extractor_manifest"][0][
        "sha256"
    ]
    development_sources, response_by_run, evidence_by_run = (
        _validate_development_calibration_sources(
            documents,
            record_by_role,
            dev_path,
            dev_ids,
            dev_by_id,
            cluster_mapping,
            extractor_asset_sha256,
            extractor,
            entrypoints,
        )
    )

    from .judge_pipeline import load_frozen_judge_runner

    runner_asset, runner = _schema_or_freeze(
        load_frozen_judge_runner, {"assets": records}
    )
    judge = documents["judge_rubric"][0]
    _schema_or_freeze(
        validate_schema_instance,
        judge,
        "judge_rubric",
        context="judge rubric",
    )
    _require(judge, decision_status="confirmed", request_contract="judge_atom_v0.1")
    model_id = judge.get("model_id")
    inference_config = judge.get("inference_config")
    if not isinstance(model_id, str) or not model_id or not isinstance(
        inference_config, Mapping
    ):
        raise FreezeError("judge rubric lacks a frozen model/config")
    prompt_path = _verify_file_binding(judge.get("prompt"), "judge prompt")
    output_schema_path = _verify_file_binding(
        judge.get("output_schema"), "judge output schema"
    )
    for binding, path, label in (
        (judge["prompt"], prompt_path, "judge prompt"),
        (judge["output_schema"], output_schema_path, "judge output schema"),
    ):
        if binding.get("bytes") != path.stat().st_size:
            raise FreezeError("{} byte count mismatch".format(label))
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FreezeError("judge prompt is not readable UTF-8") from exc
    output_schema = read_json(output_schema_path)
    if canonical_json_bytes(output_schema) != canonical_json_bytes(
        load_schema("judge_output")
    ):
        raise FreezeError("judge output schema differs from the registered strict schema")
    computed_judge_config_sha256 = judge_config_sha256(
        model_id=model_id,
        prompt_sha256=judge["prompt"]["sha256"],
        inference_config=inference_config,
        output_schema_sha256=judge["output_schema"]["sha256"],
        request_contract=judge["request_contract"],
        runner_manifest_sha256=runner_asset["sha256"],
    )
    if judge.get("runner_manifest_sha256") != runner_asset["sha256"]:
        raise FreezeError("judge rubric is not bound to the frozen runner")
    if judge.get("judge_config_sha256") != computed_judge_config_sha256:
        raise FreezeError("judge config hash does not match the frozen rubric")

    calibration_roster_path = _verify_file_binding(
        judge.get("calibration_roster"), "judge calibration roster"
    )
    calibration_roster_assets = record_by_role["judge_calibration_roster"]
    if (
        len(calibration_roster_assets) != 1
        or Path(calibration_roster_assets[0]["path"]).resolve()
        != calibration_roster_path
        or calibration_roster_assets[0]["sha256"]
        != judge["calibration_roster"]["sha256"]
        or judge["calibration_roster"]["bytes"]
        != calibration_roster_path.stat().st_size
    ):
        raise FreezeError("judge calibration roster binding is not a frozen asset")
    calibration_roster = read_json(calibration_roster_path)
    _schema_or_freeze(
        validate_schema_instance,
        calibration_roster,
        "judge_calibration_roster",
        context="judge calibration roster",
    )
    calibration_candidates = []
    eligible_categories = {
        platform: set() for platform in CALIBRATION_PLATFORMS
    }
    eligible_query_ids = {
        platform: set() for platform in CALIBRATION_PLATFORMS
    }
    for platform, source in development_sources.items():
        source_id = source["config"]["source_id"]
        for response in source["responses"]:
            if response["expected_support"] != "supported":
                continue
            evidence = evidence_by_run[response["run_id"]]
            query_id = response["query_id"]
            oracle = oracle_by_role["requirement_oracle_dev"][query_id]
            response_sha256 = sha256_bytes(canonical_json_bytes(response))
            evidence_sha256 = sha256_bytes(canonical_json_bytes(evidence))
            for atom in oracle["atoms"]:
                if atom.get("layer") not in (
                    "core_required",
                    "surface_required",
                    "forbidden",
                ):
                    continue
                if deterministic_atom_verdict(atom, evidence) != "unknown":
                    continue
                calibration_candidates.append(
                    {
                        "item_id": judge_item_id(
                            response_sha256, evidence_sha256, atom["atom_id"]
                        ),
                        "platform": platform,
                        "source_id": source_id,
                        "query_id": query_id,
                        "run_id": response["run_id"],
                        "repetition": response["repetition"],
                        "atom_id": atom["atom_id"],
                        "category": atom["category"],
                        "surface_style": oracle["surface_style"],
                        "source_response_sha256": response_sha256,
                        "source_evidence_sha256": evidence_sha256,
                    }
                )
                eligible_categories[platform].add(atom["category"])
                eligible_query_ids[platform].add(query_id)
    if calibration_roster.get("seed") != CALIBRATION_SELECTION_SEED:
        raise FreezeError("judge calibration roster does not use the frozen seed")
    recomputed_calibration_roster = _schema_or_freeze(
        build_judge_calibration_roster,
        calibration_candidates,
        calibration_roster["seed"],
    )
    if canonical_json_bytes(calibration_roster) != canonical_json_bytes(
        recomputed_calibration_roster
    ):
        raise FreezeError("judge calibration roster is not the deterministic selection")
    roster_item_by_id = {
        item["item_id"]: item for item in calibration_roster["items"]
    }

    calibration_path = _verify_file_binding(judge.get("calibration"), "judge calibration")
    calibration_records = _read_decision_document(calibration_path)
    if not isinstance(calibration_records, list):
        raise FreezeError("judge calibration must be JSONL")
    _schema_or_freeze(
        validate_schema_records,
        calibration_records,
        "judge_calibration",
        context="judge calibration",
    )
    context_asset = record_by_role["judge_calibration_context_manifest"][0]
    request_asset = record_by_role[
        "judge_calibration_request_bundle_manifest"
    ][0]
    run_asset = record_by_role["judge_calibration_run_manifest"][0]
    context_path = Path(context_asset["path"]).resolve()
    request_manifest_path = Path(request_asset["path"]).resolve()
    run_manifest_path = Path(run_asset["path"]).resolve()
    calibration_context = documents["judge_calibration_context_manifest"][0]
    _require_formal_judge_calibration_profile(calibration_context)
    for rubric_field, asset, expected_path in (
        ("calibration_context", context_asset, context_path),
        ("calibration_request_manifest", request_asset, request_manifest_path),
        ("calibration_run_manifest", run_asset, run_manifest_path),
    ):
        binding = judge.get(rubric_field)
        if (
            not isinstance(binding, Mapping)
            or Path(binding.get("path", "")).resolve() != expected_path
            or binding.get("sha256") != asset["sha256"]
            or binding.get("bytes") != asset["bytes"]
        ):
            raise FreezeError(
                "judge rubric {} is not its frozen asset".format(rubric_field)
            )
    expected_context_bindings = {
        "query_library_dev": record_by_role["query_library_dev"][0],
        "requirement_oracle_dev": record_by_role["requirement_oracle_dev"][0],
        "calibration_roster": record_by_role["judge_calibration_roster"][0],
        "runner_manifest": runner_asset,
    }
    for field, asset in expected_context_bindings.items():
        binding = calibration_context.get(field)
        if not isinstance(binding, Mapping) or any(
            binding.get(key) != asset.get(key) for key in ("path", "sha256", "bytes")
        ):
            raise FreezeError(
                "judge calibration context {} is not its frozen asset".format(field)
            )
    for field in ("prompt", "output_schema"):
        if calibration_context.get(field) != judge.get(field):
            raise FreezeError(
                "judge calibration context {} differs from rubric".format(field)
            )
    if (
        calibration_context.get("model_id") != model_id
        or calibration_context.get("inference_config") != inference_config
        or calibration_context.get("request_contract") != judge["request_contract"]
        or calibration_context.get("judge_config_sha256")
        != computed_judge_config_sha256
    ):
        raise FreezeError("judge calibration context differs from frozen Judge config")
    if calibration_context.get("calibration_human_gold") != judge.get("calibration"):
        raise FreezeError("pre-run calibration context does not bind the human gold")
    expected_source_bindings = {
        role: {
            (record["path"], record["sha256"], record["bytes"])
            for record in record_by_role[role]
        }
        for role in (
            "judge_calibration_source_config",
            "development_query_roster",
            "development_response_records",
            "development_evidence_records",
        )
    }
    context_source_bindings = {
        "judge_calibration_source_config": set(),
        "development_query_roster": set(),
        "development_response_records": set(),
        "development_evidence_records": set(),
    }
    context_field_by_role = {
        "judge_calibration_source_config": "source_config",
        "development_query_roster": "query_roster",
        "development_response_records": "response_records",
        "development_evidence_records": "evidence_records",
    }
    for source in calibration_context.get("development_sources", []):
        for role, field in context_field_by_role.items():
            binding = source.get(field, {})
            context_source_bindings[role].add(
                (binding.get("path"), binding.get("sha256"), binding.get("bytes"))
            )
    if context_source_bindings != expected_source_bindings:
        raise FreezeError("judge calibration context source inventory is incomplete")

    from .judge_pipeline import validate_judge_calibration_run_bundle

    _, raw_judge_records = _schema_or_freeze(
        validate_judge_calibration_run_bundle,
        run_manifest_path.parent,
        context_path,
        request_directory=request_manifest_path.parent,
    )
    raw_by_item = {}
    for raw_record in raw_judge_records:
        item_id = raw_record["item_id"]
        if item_id in raw_by_item:
            raise FreezeError("judge calibration response item IDs must be unique")
        raw_by_item[item_id] = raw_record
    calibration_pairs = set()
    calibration_item_ids = set()
    predictions_by_item = {}
    calibrated_categories = {
        platform: set() for platform in CALIBRATION_PLATFORMS
    }
    calibrated_query_ids = {
        platform: set() for platform in CALIBRATION_PLATFORMS
    }
    referenced_raw_item_ids = set()
    for record in calibration_records:
        query_id = record["query_id"]
        run_id = record["run_id"]
        atom_id = record["atom_id"]
        pair = (run_id, atom_id)
        if pair in calibration_pairs:
            raise FreezeError("judge calibration repeats one run/atom pair")
        calibration_pairs.add(pair)
        response = response_by_run.get(run_id)
        evidence = evidence_by_run.get(run_id)
        if (
            response is None
            or evidence is None
            or response.get("query_id") != query_id
            or response.get("repetition") != record.get("repetition")
        ):
            raise FreezeError("judge calibration is outside the development records")
        response_sha256 = sha256_bytes(canonical_json_bytes(response))
        evidence_sha256 = sha256_bytes(canonical_json_bytes(evidence))
        if (
            record["source_response_sha256"] != response_sha256
            or record["source_evidence_sha256"] != evidence_sha256
        ):
            raise FreezeError("judge calibration source hashes do not match frozen records")
        expected_item_id = judge_item_id(response_sha256, evidence_sha256, atom_id)
        if record["item_id"] != expected_item_id or expected_item_id in calibration_item_ids:
            raise FreezeError("judge calibration item ID is invalid or duplicated")
        calibration_item_ids.add(expected_item_id)
        roster_item = roster_item_by_id.get(expected_item_id)
        if roster_item is None or any(
            record.get(field) != roster_item.get(field)
            for field in (
                "platform",
                "source_id",
                "query_id",
                "run_id",
                "repetition",
                "atom_id",
                "category",
                "surface_style",
                "source_response_sha256",
                "source_evidence_sha256",
            )
        ):
            raise FreezeError("judge calibration row differs from its frozen roster item")
        oracle_atoms = {
            atom["atom_id"]: atom
            for atom in oracle_by_role["requirement_oracle_dev"][query_id]["atoms"]
        }
        oracle_atom = oracle_atoms.get(atom_id)
        if oracle_atom is None:
            raise FreezeError("judge calibration atom is outside the development oracle")
        if (
            deterministic_atom_verdict(oracle_atom, evidence) != "unknown"
        ):
            raise FreezeError("judge calibration atom was not deterministically routed to judge")
        platform = record["platform"]
        calibrated_categories[platform].add(oracle_atom["category"])
        calibrated_query_ids[platform].add(query_id)
        raw_record = raw_by_item.get(expected_item_id)
        if raw_record is None or expected_item_id in referenced_raw_item_ids:
            raise FreezeError("judge calibration lacks a unique raw judge response")
        referenced_raw_item_ids.add(expected_item_id)
        artifact_path = Path(response["artifact"]["path"])
        try:
            artifact_text = artifact_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise FreezeError("judge source artifact is not readable UTF-8") from exc
        expected_request_sha256 = judge_request_sha256(
            model_id=model_id,
            judge_config_sha256_value=computed_judge_config_sha256,
            inference_config=inference_config,
            prompt_text=prompt_text,
            query_text=dev_by_id[query_id]["query_text"],
            platform=response["platform"],
            artifact_sha256=response["artifact"]["sha256"],
            artifact_text=artifact_text,
            source_response_sha256=response_sha256,
            source_evidence_sha256=evidence_sha256,
            evidence=evidence,
            oracle_atom=oracle_atom,
            output_schema=output_schema,
            request_contract=judge["request_contract"],
        )
        expected_response_id = judge_response_id(expected_request_sha256, attempt=0)
        if (
            raw_record.get("judge_response_id") != expected_response_id
            or raw_record.get("item_id") != expected_item_id
            or raw_record.get("query_id") != query_id
            or raw_record.get("run_id") != run_id
            or raw_record.get("atom_id") != atom_id
            or raw_record.get("model_id") != model_id
            or raw_record.get("judge_config_sha256")
            != computed_judge_config_sha256
            or raw_record.get("judge_request_sha256") != expected_request_sha256
            or raw_record.get("provenance", {}).get("runner_id")
            != runner["runner_id"]
            or raw_record.get("provenance", {}).get("runner_version")
            != runner["runner_version"]
        ):
            raise FreezeError("raw judge response is not bound to its exact request")
        raw_response = raw_record["raw_response"]
        if raw_record["raw_response_sha256"] != sha256_bytes(
            raw_response.encode("utf-8")
        ):
            raise FreezeError("judge raw-response byte hash mismatch")
        parsed = _schema_or_freeze(parse_raw_judge_response, raw_response)
        predictions_by_item[expected_item_id] = parsed["verdict"]
    if referenced_raw_item_ids != set(raw_by_item):
        raise FreezeError("judge response records contain missing or orphan records")
    if calibration_item_ids != set(roster_item_by_id):
        raise FreezeError("judge calibration does not exactly cover the frozen roster")
    calibration = _schema_or_freeze(
        compute_judge_calibration, calibration_records, predictions_by_item
    )
    for platform in CALIBRATION_PLATFORMS:
        if calibrated_categories[platform] != eligible_categories[platform]:
            raise FreezeError(
                "{} judge calibration does not cover every judge-eligible category".format(
                    platform
                )
            )
        if calibrated_query_ids[platform] != eligible_query_ids[platform]:
            raise FreezeError(
                "{} judge calibration does not cover every judge-eligible development query".format(
                    platform
                )
            )
    declared_f1 = judge.get("macro_f1")
    declared_kappa = judge.get("cohen_kappa")
    if not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
        for value in (declared_f1, declared_kappa)
    ):
        raise FreezeError("judge calibration metrics must be finite values in [0,1]")
    if not math.isclose(float(declared_f1), calibration["macro_f1"], abs_tol=1e-12):
        raise FreezeError("judge macro-F1 does not match frozen calibration labels")
    if not math.isclose(float(declared_kappa), calibration["cohen_kappa"], abs_tol=1e-12):
        raise FreezeError("judge kappa does not match frozen calibration labels")
    if float(declared_f1) < 0.85 or float(declared_kappa) < 0.8:
        raise FreezeError("judge calibration gate is not satisfied")
    if judge.get("calibration_record_count") != calibration["record_count"]:
        raise FreezeError("judge calibration record count is not frozen")
    if judge.get("gold_support_by_verdict") != calibration[
        "gold_support_by_verdict"
    ]:
        raise FreezeError("judge calibration gold support is not frozen")
    declared_platform_metrics = judge.get("platform_metrics", {})
    for platform in CALIBRATION_PLATFORMS:
        declared = declared_platform_metrics.get(platform, {})
        actual = calibration["by_platform"][platform]
        if (
            declared.get("record_count") != CALIBRATION_ITEMS_PER_PLATFORM
            or declared.get("gold_support_by_verdict")
            != actual["gold_support_by_verdict"]
            or not math.isclose(
                float(declared.get("macro_f1", -1.0)),
                actual["macro_f1"],
                abs_tol=1e-12,
            )
            or not math.isclose(
                float(declared.get("cohen_kappa", -1.0)),
                actual["cohen_kappa"],
                abs_tol=1e-12,
            )
            or actual["macro_f1"] < 0.85
            or actual["cohen_kappa"] < 0.8
        ):
            raise FreezeError(
                "{} judge calibration gate is not satisfied".format(platform)
            )

    reviews = _record_map(documents["human_query_gold"][0], "human_query_gold")
    _schema_or_freeze(
        validate_schema_records,
        reviews.values(),
        "human_query_gold",
        context="query human gold",
    )
    if len(reviews) != 300:
        raise FreezeError("query human gold must contain all 300 query records")
    if set(reviews) != test_ids | dev_ids:
        raise FreezeError("query human-gold IDs do not match both libraries")
    all_oracles = dict(oracle_by_role["requirement_oracle_test"])
    all_oracles.update(oracle_by_role["requirement_oracle_dev"])
    all_sources = dict(test_by_id)
    all_sources.update(dev_by_id)
    repository_root = workspace_root()
    draft_paths = {
        "test": repository_root
        / "benchmark_artifacts"
        / "drafts"
        / "test_oracle_draft.jsonl",
        "dev": repository_root
        / "benchmark_artifacts"
        / "drafts"
        / "dev_oracle_draft.jsonl",
    }
    draft_by_split = {
        split: _record_map(
            _read_decision_document(path), "{}_oracle_draft".format(split)
        )
        for split, path in draft_paths.items()
    }
    if set(draft_by_split["test"]) != test_ids or set(draft_by_split["dev"]) != dev_ids:
        raise FreezeError("checked-in oracle drafts do not match the query split IDs")
    draft_by_id = dict(draft_by_split["test"])
    draft_by_id.update(draft_by_split["dev"])
    expected_source_bindings = {
        "test": {
            "library_sha256": record_by_role["query_library_test"][0]["sha256"],
            "library_hash_scope": "file_bytes",
            "oracle_sha256": sha256_file(draft_paths["test"]),
            "oracle_hash_scope": "file_bytes",
            "record_count": len(test_ids),
        },
        "dev": {
            "library_sha256": record_by_role["query_library_dev"][0]["sha256"],
            "library_hash_scope": "file_bytes",
            "oracle_sha256": sha256_file(draft_paths["dev"]),
            "oracle_hash_scope": "file_bytes",
            "record_count": len(dev_ids),
        },
    }
    for query_id, review in reviews.items():
        payload = review.get("review_payload")
        split = "test" if query_id in test_ids else "dev"
        expected_subject_sha256 = sha256_bytes(
            canonical_json_bytes(
                {
                    "query_record": all_sources[query_id],
                    "oracle_draft": draft_by_id[query_id],
                }
            )
        )
        if (
            not isinstance(payload, Mapping)
            or payload.get("status") != "complete"
            or not review.get("reviewer_id")
            or payload.get("reviewer_id") != review.get("reviewer_id")
            or not isinstance(payload.get("decision"), Mapping)
            or review.get("review_status") != "complete"
            or review.get("decision_status") != "confirmed"
            or review.get("subject_sha256") != expected_subject_sha256
            or canonical_json_bytes(review.get("source_binding"))
            != canonical_json_bytes(expected_source_bindings[split])
            or review.get("gold_record_id") != _human_gold_record_id(review)
        ):
            raise FreezeError("query review lacks one complete bound human-gold payload")
        if payload["decision"].get("query_id") != query_id:
            raise FreezeError("review decision/query ID mismatch")
        _validate_review_payload(
            payload,
            draft_by_id[query_id],
            all_sources[query_id],
            "{} human reviewer".format(query_id),
        )
        final_oracle = all_oracles[query_id]
        if _oracle_record_hash(payload["decision"]) != _oracle_record_hash(final_oracle):
            raise FreezeError("human-gold decision does not equal the frozen oracle")

    ego = documents["ego_proxy_config"][0]
    _require(
        ego,
        status="frozen",
        length_m=EGO_PROXY_LENGTH_M,
        width_m=EGO_PROXY_WIDTH_M,
        carla_blueprint=CARLA_EGO_BLUEPRINT,
        metadrive_vehicle_model="xl",
    )

    method_configs = documents["method_config"]
    config_records = record_by_role["method_config"]
    config_by_method = {}
    config_hash_by_method = {}
    for config, record in zip(method_configs, config_records):
        _require(
            config,
            status="frozen",
            query_input_fields=["query_text"],
            post_output_semantic_repair=False,
        )
        method_id = config.get("method_id")
        if not method_id or method_id in config_by_method:
            raise FreezeError("method configs require unique method_id values")
        if config.get("platform") == "metadrive":
            token_assets = record_by_role["metadrive_pg_token_registry"]
            if len(token_assets) != 1:
                raise FreezeError("freeze lacks one MetaDrive PG token registry")
            _require_exact_metadrive_registry_binding(
                config.get("token_registry"),
                token_assets[0],
                "MetaDrive method token registry",
            )
        elif config.get("token_registry") is not None:
            raise FreezeError("CARLA method cannot bind a MetaDrive token registry")
        config_by_method[method_id] = config
        config_hash_by_method[method_id] = record["sha256"]

    roster_by_method = {}
    roster_hash_by_method = {}
    test_library_sha256 = record_by_role["query_library_test"][0]["sha256"]
    for roster, record in zip(documents["query_roster"], record_by_role["query_roster"]):
        _schema_or_freeze(
            validate_schema_instance, roster, "query_roster", context="query roster"
        )
        _require(roster, decision_status="confirmed")
        entries = roster_entries(roster)
        if set(entries) != test_ids or len(entries) != 252:
            raise FreezeError("formal method roster must contain all 252 locked queries")
        if roster.get("library_sha256") != test_library_sha256:
            raise FreezeError("formal method roster is not bound to the locked test library")
        if roster.get("platform") not in ("carla", "metadrive"):
            raise FreezeError("formal method roster has an invalid platform")
        for query_id, entry in entries.items():
            source = test_by_id[query_id]
            expected = {
                "intent_group_id": source["intent_group_id"],
                "statistical_intent_cluster_id": apply_statistical_cluster(
                    source["intent_group_id"], cluster_mapping
                ),
                "surface_style": source["surface_style"],
                "expected_support": source["expected_support"],
            }
            if any(entry.get(key) != value for key, value in expected.items()):
                raise FreezeError(
                    "formal method roster metadata differs from the locked test library"
                )
        method_id = roster.get("method_id")
        if not method_id or method_id in roster_by_method:
            raise FreezeError("method has more than one formal query roster")
        roster_by_method[method_id] = roster
        roster_hash_by_method[method_id] = record["sha256"]

    registry = documents["method_registry"][0]
    _require(registry, status="frozen")
    registered = registry.get("methods", [])
    registered_ids = {method.get("method_id") for method in registered}
    if registered_ids != set(config_by_method) or registered_ids != set(roster_by_method):
        raise FreezeError("method registry/config/roster method IDs disagree")
    for method in registered:
        method_id = method["method_id"]
        if method.get("platform") != config_by_method[method_id].get("platform"):
            raise FreezeError("method platform differs between registry and config")
        if method.get("platform") != roster_by_method[method_id].get("platform"):
            raise FreezeError("method platform differs between registry and roster")
        if method.get("config_sha256") != config_hash_by_method[method_id]:
            raise FreezeError("method registry config hash mismatch")
        if method.get("roster_sha256") != roster_hash_by_method[method_id]:
            raise FreezeError("method registry roster hash mismatch")


def _validate_asset_contract(records: List[Mapping[str, Any]]) -> None:
    """Validate the contract and recheck every transitively bound file afterward."""

    global _ACTIVE_NESTED_BINDING_SNAPSHOTS
    if _ACTIVE_NESTED_BINDING_SNAPSHOTS is not None:
        raise FreezeError("nested freeze validation is not supported")
    snapshots: List[Mapping[str, Any]] = []
    _ACTIVE_NESTED_BINDING_SNAPSHOTS = snapshots
    try:
        _validate_asset_contract_once(records)
    finally:
        _ACTIVE_NESTED_BINDING_SNAPSHOTS = None

    unique_snapshots = {
        (record["path"], record["sha256"], record["bytes"]): record
        for record in snapshots
    }
    _assert_asset_records_unchanged(unique_snapshots.values())


def create_freeze_manifest(
    assets: Iterable[Mapping[str, Any]],
    protocol: Mapping[str, Any],
    allow_draft: bool = False,
) -> Dict[str, Any]:
    records = []
    unfinished = []
    for asset in assets:
        path = Path(asset["path"])
        record = _asset_record(path, str(asset["role"]))
        if path.suffix in (".json", ".jsonl"):
            unfinished.extend(
                "{}:{}".format(path, decision_path)
                for decision_path in _unfinished_paths(_read_decision_document(path))
            )
        records.append(record)
    unfinished.extend(
        "protocol:{}".format(decision_path) for decision_path in _unfinished_paths(protocol)
    )
    roles = {record["role"] for record in records}
    if not allow_draft:
        if unfinished:
            raise FreezeError(
                "freeze contains {} unfinished decisions: {}".format(
                    len(unfinished), unfinished[:5]
                )
            )
        _validate_frozen_contract(roles, protocol)
        _schema_or_freeze(
            validate_schema_records, records, "freeze_asset", context="freeze asset"
        )
        _schema_or_freeze(
            validate_schema_instance,
            protocol,
            "freeze_protocol",
            context="freeze protocol",
        )
        _validate_asset_contract(records)
        _assert_asset_records_unchanged(records)
    body = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_version": BENCHMARK_VERSION,
        "created_at_utc": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        "status": "draft" if allow_draft or unfinished else "frozen",
        "unfinished_decision_count": len(unfinished),
        "unfinished_decision_paths": unfinished,
        "assets": sorted(records, key=lambda value: (value["role"], value["path"])),
        "protocol": dict(protocol),
    }
    body["manifest_sha256"] = sha256_bytes(canonical_json_bytes(body))
    if not allow_draft:
        _schema_or_freeze(
            validate_schema_instance, body, "freeze_manifest", context="freeze manifest"
        )
    return body


def verify_freeze_manifest(manifest: Mapping[str, Any], require_frozen: bool = True) -> Dict[str, Any]:
    if require_frozen:
        _schema_or_freeze(
            validate_schema_instance,
            manifest,
            "freeze_manifest",
            context="freeze manifest",
        )
    expected_hash = manifest.get("manifest_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    actual_hash = sha256_bytes(canonical_json_bytes(unsigned))
    if expected_hash != actual_hash:
        raise FreezeError("freeze manifest self-hash mismatch")
    if require_frozen and manifest.get("status") != "frozen":
        raise FreezeError("locked test requires a frozen manifest with no draft decisions")
    _assert_asset_records_unchanged(manifest.get("assets", []))
    if require_frozen:
        if manifest.get("unfinished_decision_count") != 0 or manifest.get(
            "unfinished_decision_paths"
        ) != []:
            raise FreezeError("frozen manifest declares unfinished decisions")
        records = list(manifest.get("assets", []))
        _validate_frozen_contract({record.get("role") for record in records}, manifest.get("protocol", {}))
        unfinished = []
        for record in records:
            document = _read_decision_document(Path(record["path"]))
            unfinished.extend(_unfinished_paths(document))
        unfinished.extend(_unfinished_paths(manifest.get("protocol", {})))
        if unfinished:
            raise FreezeError("frozen manifest contains unfinished decisions")
        _validate_asset_contract(records)
        _assert_asset_records_unchanged(records)
    return {"verified": True, "asset_count": len(manifest.get("assets", [])), "status": manifest.get("status")}
