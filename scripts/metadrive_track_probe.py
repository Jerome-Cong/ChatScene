#!/usr/bin/env python3
"""Emit source-bound native evidence for the external MetaDrive token track."""

import argparse
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bus_benchmark.generation import ensure_immutable_json, probe_python_environment
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_json,
    sha256_bytes,
    sha256_file,
    strict_json_object_bytes,
)
from bus_benchmark.metadrive_pg import (
    ARTIFACT_TYPE,
    AUDITED_TOKEN_CLASSES,
    EGO_LENGTH_M,
    EGO_VEHICLE_MODEL,
    EGO_WIDTH_M,
    canonical_json_bytes as canonical_pg_bytes,
    load_token_registry,
    validate_artifact,
)
from bus_benchmark.runtime import validate_platform_runtime_config
from bus_benchmark.schema import validate_schema_instance


EVIDENCE_TYPE = "metadrive_native_probe_evidence_v0.1"
PROBE_ID = "metadrive_sequence_block_track_probe_v0.1"
NATIVE_OBSERVER = ROOT / "bus_benchmark" / "native_observer.py"


def _git(repository, *arguments):
    process = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError("MetaDrive source revision cannot be read")
    return process.stdout.decode("utf-8").strip()


def _file_binding(path):
    path = Path(path).resolve()
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("probe dependency is missing or symlinked")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _materialize(token, lane_num, case_id):
    from metadrive.envs.metadrive_env import MetaDriveEnv

    environment = None
    try:
        environment = MetaDriveEnv(
            {
                "map_config": {
                    "type": "block_sequence",
                    "config": token,
                    "lane_num": lane_num,
                    "lane_width": 3.5,
                    "exit_length": 50.0,
                },
                "num_scenarios": 1,
                "start_seed": 0,
                "random_spawn_lane_index": False,
                "traffic_density": 0.0,
                "random_traffic": False,
                "random_lane_width": False,
                "random_lane_num": False,
                "use_render": False,
                "show_terrain": False,
                "show_skybox": False,
                "show_interface": False,
                "log_level": 50,
                "horizon": 1,
            }
        )
        environment.reset(seed=0)
        return {
            "case_id": case_id,
            "token": token,
            "lane_num": lane_num,
            "status": "passed",
            "actual_block_ids": [block.ID for block in environment.current_map.blocks],
            "map_feature_count": len(environment.current_map.get_map_features()),
            "error": None,
        }
    except Exception as exc:
        return {
            "case_id": case_id,
            "token": token,
            "lane_num": lane_num,
            "status": "failed",
            "actual_block_ids": None,
            "map_feature_count": 0,
            "error": "{}: {}".format(type(exc).__name__, exc),
        }
    finally:
        if environment is not None:
            environment.close()


def _normalize_materialization(row, expected_outcome):
    error = row.get("error") or ""
    if row["status"] == "passed":
        observed_outcome = "materialized"
        failure_contract = None
    elif row["token"] in {"F", "f"} and "Bug exists in this block" in error:
        observed_outcome = "native_failure"
        failure_contract = "known_fork_constructor_failure"
    elif (
        row["token"] == "P"
        and row["lane_num"] == 2
        and "Lane number of previous block must be 1" in error
    ):
        observed_outcome = "native_failure"
        failure_contract = "parking_requires_one_lane"
    else:
        observed_outcome = "unexpected_failure"
        failure_contract = "unexpected_native_failure"
    return {
        "case_id": row["case_id"],
        "token": row["token"],
        "lane_num": row["lane_num"],
        "expected_outcome": expected_outcome,
        "observed_outcome": observed_outcome,
        "expected_block_ids": (
            ["I", row["token"]] if expected_outcome == "materialized" else None
        ),
        "actual_block_ids": row["actual_block_ids"],
        "map_feature_count": row["map_feature_count"],
        "failure_contract": failure_contract,
    }


def _runtime_probe_artifact(registry_sha256):
    return {
        "schema_version": "0.1",
        "artifact_type": ARTIFACT_TYPE,
        "time_step_s": 0.1,
        "horizon_steps": 2,
        "map": {
            "generation_type": "block_sequence",
            "block_sequence": "S",
            "map_seed": 0,
            "lane_num": 2,
            "lane_width_m": 3.5,
            "exit_length_m": 50.0,
            "token_registry_id": "metadrive-pg-token-registry-v0.1",
            "token_registry_sha256": registry_sha256,
        },
        "ego": {
            "actor_id": "ego",
            "actor_type": "vehicle",
            "vehicle_model": EGO_VEHICLE_MODEL,
            "length_m": EGO_LENGTH_M,
            "width_m": EGO_WIDTH_M,
            "reference_trajectory": [
                {
                    "step": 0,
                    "position_m": [0.0, 0.0],
                    "heading_rad": 0.0,
                    "speed_m_s": 5.0,
                    "valid": True,
                },
                {
                    "step": 1,
                    "position_m": [0.5, 0.0],
                    "heading_rad": 0.0,
                    "speed_m_s": 5.0,
                    "valid": True,
                },
            ],
        },
        "actors": [],
    }


def _run_reset_step_probe(artifact_bytes, registry_path):
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MPLCONFIGDIR": "/tmp",
        "PYTHONHASHSEED": "0",
    }
    process = subprocess.run(
        [
            str(Path(sys.executable).resolve()),
            str(NATIVE_OBSERVER.resolve()),
            "metadrive",
            "--runtime-probe",
            "--token-registry",
            str(Path(registry_path).resolve()),
        ],
        input=artifact_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env=environment,
    )
    if process.returncode != 0:
        raise RuntimeError("MetaDrive reset-step probe failed")
    return strict_json_object_bytes(
        process.stdout, "MetaDrive native reset-step probe"
    )


def probe(registry_path, source_repository, platform_config_path, *, development):
    import metadrive
    from metadrive.component.algorithm.blocks_prob_dist import PGBlockDistConfig
    from metadrive.version import VERSION

    registry_path = Path(registry_path).resolve()
    source_repository = Path(source_repository).resolve()
    platform_config_path = Path(platform_config_path).resolve()
    platform_config = read_json(platform_config_path)
    worker = validate_platform_runtime_config(
        platform_config, "metadrive", allow_draft=development
    )
    if Path(worker["interpreter"]).resolve() != Path(sys.executable).resolve():
        raise RuntimeError("probe must run under the bound MetaDrive interpreter")
    configured_registry = worker["platform_context"]["metadrive_token_registry"]
    if Path(configured_registry["path"]).resolve() != registry_path:
        raise RuntimeError("probe registry differs from the platform runtime config")
    registry, registry_sha256 = load_token_registry(
        registry_path, formal=not development, verify_files=True
    )
    if configured_registry["sha256"] != registry_sha256:
        raise RuntimeError("probe registry hash differs from the platform runtime config")
    interpreter = Path(sys.executable).resolve()
    environment = probe_python_environment(interpreter)
    module_path = Path(metadrive.__file__).resolve()
    source_subtree = (source_repository / "metadrive").resolve()
    module_path.relative_to(source_subtree)
    runtime_dependencies = worker["runtime_dependencies"]
    source_binding = runtime_dependencies["source_tree"]
    if Path(source_binding["repository_path"]).resolve() != source_repository:
        raise RuntimeError("probe source differs from the platform runtime config")
    if Path(runtime_dependencies["metadrive_module"]["path"]).resolve() != module_path:
        raise RuntimeError("probe imported a different MetaDrive module")

    live_mapping = {
        token: PGBlockDistConfig.get_block(token).__name__
        for token in AUDITED_TOKEN_CLASSES
    }
    registry_lane_num_constraints = {
        entry["token"]: tuple(
            entry["map_parameter_constraints"]["lane_num_allowed"]
        )
        for entry in registry["tokens"]
    }
    positive_raw = [
        _materialize(
            token,
            2 if 2 in registry_lane_num_constraints[token] else 1,
            token,
        )
        for token in sorted(AUDITED_TOKEN_CLASSES)
    ]
    negative_raw = [
        _materialize("F", 2, "F@lane2"),
        _materialize("f", 2, "f@lane2"),
        _materialize("P", 2, "P@lane2"),
    ]
    positive = [
        _normalize_materialization(row, "materialized") for row in positive_raw
    ]
    negative = [
        _normalize_materialization(row, "native_failure") for row in negative_raw
    ]
    positive_passed = all(
        row["observed_outcome"] == "materialized"
        and row["actual_block_ids"] == row["expected_block_ids"]
        and row["map_feature_count"] > 0
        and row["failure_contract"] is None
        for row in positive
    )
    negative_passed = all(
        row["observed_outcome"] == "native_failure"
        and row["actual_block_ids"] is None
        and row["failure_contract"]
        in {"known_fork_constructor_failure", "parking_requires_one_lane"}
        for row in negative
    )
    runtime_fixture = _runtime_probe_artifact(registry_sha256)
    validate_artifact(runtime_fixture, registry, registry_sha256)
    runtime_fixture_bytes = canonical_pg_bytes(runtime_fixture)
    with contextlib.redirect_stdout(sys.stderr):
        reset_step = _run_reset_step_probe(runtime_fixture_bytes, registry_path)
    expected_reset_step = {
        "reset_succeeded": True,
        "step_succeeded": True,
        "vehicle_class": "XLVehicle",
        "vehicle_model": EGO_VEHICLE_MODEL,
        "top_down_length_m": EGO_LENGTH_M,
        "top_down_width_m": EGO_WIDTH_M,
        "actual_block_ids": ["I", "S"],
        "block_sequence": "S",
    }
    reset_step_passed = all(
        reset_step.get(key) == value for key, value in expected_reset_step.items()
    ) and Path(reset_step.get("metadrive_module_path", "")).resolve() == module_path
    environment_manifest_path = Path(
        runtime_dependencies["environment_manifest"]["path"]
    ).resolve()
    environment_manifest = read_json(environment_manifest_path)
    result = {
        "schema_version": "0.1",
        "artifact_type": EVIDENCE_TYPE,
        "probe_id": PROBE_ID,
        "platform": "metadrive",
        "status": "passed"
        if positive_passed
        and negative_passed
        and reset_step_passed
        and live_mapping == AUDITED_TOKEN_CLASSES
        else "failed",
        "evidence_mode": "development" if development else "formal",
        "claim_scope": {
            "real_map_used": False,
            "method_run_evidence": False,
            "human_gold": False,
            "token_coverage": "single_token_materialization",
            "validation_level": "native_pgmap_reset_step",
            "long_sequence_constructibility_proven": False,
            "thirty_second_ne_proven": False,
            "iec_execution_proven": False,
        },
        "bindings": {
            "platform_runtime_config": _file_binding(platform_config_path),
            "token_registry": _file_binding(registry_path),
            "runtime_environment_manifest": _file_binding(
                environment_manifest_path
            ),
            "probe_runner": _file_binding(Path(__file__)),
            "native_observer": _file_binding(NATIVE_OBSERVER),
        },
        "runtime": {
            "interpreter": _file_binding(interpreter),
            "python_runtime_version": environment["runtime_version"],
            "locked_dependency_count": len(environment["locked_dependencies"]),
            "locked_dependencies_sha256": sha256_bytes(
                canonical_json_bytes(environment["locked_dependencies"])
            ),
            "environment_decision_status": environment_manifest[
                "decision_status"
            ],
            "metadrive_version": VERSION,
            "metadrive_module": _file_binding(module_path),
            "source_tree": source_binding,
            "source_describe": _git(
                source_repository, "describe", "--always", "--tags"
            ),
        },
        "runtime_fixture": {
            "fixture_status": "benchmark_constructed_development_probe"
            if development
            else "benchmark_constructed_formal_probe",
            "artifact_sha256": sha256_bytes(runtime_fixture_bytes),
            "artifact_bytes": len(runtime_fixture_bytes),
            "artifact": runtime_fixture,
        },
        "reset_step": {
            "status": "passed" if reset_step_passed else "failed",
            "step_action": [0.0, 0.0],
            "reset_succeeded": reset_step.get("reset_succeeded"),
            "step_succeeded": reset_step.get("step_succeeded"),
            "vehicle_class": reset_step.get("vehicle_class"),
            "vehicle_model": reset_step.get("vehicle_model"),
            "physical_length_m": reset_step.get("physical_length_m"),
            "physical_width_m": reset_step.get("physical_width_m"),
            "physical_height_m": reset_step.get("physical_height_m"),
            "top_down_length_m": reset_step.get("top_down_length_m"),
            "top_down_width_m": reset_step.get("top_down_width_m"),
            "block_sequence": reset_step.get("block_sequence"),
            "expected_block_ids": ["I", "S"],
            "actual_block_ids": reset_step.get("actual_block_ids"),
        },
        "token_registry_check": {
            "registry_id": registry["registry_id"],
            "registry_decision_status": registry["decision_status"],
            "token_mapping": live_mapping,
            "lane_num_constraints": {
                token: list(registry_lane_num_constraints[token])
                for token in sorted(registry_lane_num_constraints)
            },
            "positive_cases": positive,
            "expected_native_failures": negative,
        },
    }
    validate_schema_instance(
        result,
        "metadrive_native_probe_evidence",
        context="MetaDrive native probe evidence",
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry",
        default="benchmark_configs/methods/metadrive_pg_token_registry_draft.json",
    )
    parser.add_argument(
        "--source-repository",
        default="/home/shijie20/CodeSpace/mdsn/metadrive",
    )
    parser.add_argument(
        "--platform-config",
        default="benchmark_configs/platforms/metadrive_runtime_draft.json",
    )
    parser.add_argument(
        "--development",
        action="store_true",
        help="allow the explicitly draft runtime config and token registry",
    )
    parser.add_argument(
        "--output",
        help="immutably create or verify the canonical evidence artifact",
    )
    args = parser.parse_args(argv)
    result = probe(
        args.registry,
        args.source_repository,
        args.platform_config,
        development=args.development,
    )
    if args.output:
        ensure_immutable_json(Path(args.output), result)
    sys.stdout.buffer.write(canonical_json_bytes(result) + b"\n")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
