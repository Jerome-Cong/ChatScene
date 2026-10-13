#!/usr/bin/env python3
"""Prepare host-bound TTSG native-CARLA DEVELOPMENT configuration."""

import argparse
import json
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark/src"))
sys.path.insert(0, str(ROOT / "scripts"))
from prepare_chatscene_runtime import binding, environment
from bus_benchmark.generation import (
    generation_harness_paths,
    implementation_bundle_sha256,
    validate_implementation_bundle,
)
from bus_benchmark.jsonio import (
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
    sha256_file,
)
from bus_benchmark.paths import PACKAGE_ROOT
from bus_benchmark.runtime import (
    runtime_tree_binding,
    validate_platform_runtime_config,
    _validate_controller_config,
)


def prepare(source, output, api_env, port=2200, pilot=False):
    from dotenv import dotenv_values

    source = source.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError("choose a fresh configuration directory")
    values = dotenv_values(api_env, interpolate=False)
    model = values.get("OPENAI_MODEL")
    if not model:
        raise ValueError("model profile is missing OPENAI_MODEL")
    python = source / ".venv/bin/python-frozen"
    server_root = source / ".carla-runtime/CARLA_0.9.15_with_additional"
    server = server_root / "CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping"
    cache = source / ".carla-runtime/graph_cache-full/graph.pkl"
    output.mkdir(parents=True)
    method_env = environment(python)
    harness_env = environment(Path(sys.executable).absolute())
    write_json(output / "method-environment.json", method_env)
    write_json(output / "harness-environment.json", harness_env)
    api_timeout = float(values.get("OPENAI_COMPAT_TIMEOUT_S") or "60")
    api_retries = int(values.get("OPENAI_COMPAT_MAX_RETRIES") or "2")
    if not 0 < api_timeout <= 600 or not 0 <= api_retries <= 10:
        raise ValueError("invalid API timeout/retry profile")
    write_json(
        output / "map-source-catalog.json",
        {
            "directory": str(source / "maps"),
            "files": [binding(p) for p in sorted((source / "maps").glob("*.xodr"))],
        },
    )
    adapter = {
        "type": "command",
        "argv": [
            str(python),
            str(source / "benchmark_query_adapter.py"),
            "--model-name",
            model,
            "--map-catalog",
            str(output / "map-source-catalog.json"),
            "--graph-cache-file",
            str(cache),
            "--graph-cache-sha256",
            sha256_file(cache),
            "--port",
            str(port),
            "--max-retry",
            "3",
            "--api-timeout",
            str(api_timeout),
            "--api-max-retries",
            str(api_retries),
            "--capture-scene",
            "--capture-rollout-seconds",
            "30.0",
        ],
        "artifact_path": "ttsg-scene.json",
        "output_protocol": "artifact_on_zero",
        "timeout_seconds": 600,
        "environment_passthrough": [
            "PATH",
            "LANG",
            "LC_ALL",
            "LD_LIBRARY_PATH",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
        ],
        "max_stdout_bytes": 8388608,
        "max_stderr_bytes": 8388608,
        "max_artifact_bytes": 67108864,
    }
    files = {
        python,
        Path(sys.executable).absolute(),
        cache,
        output / "map-source-catalog.json",
        *generation_harness_paths(),
    }
    files.update(
        p
        for p in source.rglob("*.py")
        if not any(
            k in {".venv", ".carla-runtime", ".git", "__pycache__"}
            for k in p.relative_to(source).parts
        )
    )
    files.update((source / "maps").glob("*.xodr"))
    files.update(
        Path(b["path"])
        for env in (method_env, harness_env)
        for b in env["python_environment_control_files"]
    )
    bundle = {
        "status": "draft",
        "bundle_id": "ttsg-captured-native-v0.1",
        "environment_manifest": binding(output / "method-environment.json"),
        "harness_environment_manifest": binding(output / "harness-environment.json"),
        "files": [binding(p) for p in sorted(files, key=str)],
    }
    bundle["bundle_sha256"] = implementation_bundle_sha256(bundle)
    method = {
        "schema_version": "0.1",
        "status": "draft",
        "method_id": "ttsg_carla_native_v0_1",
        "platform": "carla",
        "query_input_fields": ["query_text"],
        "post_output_semantic_repair": False,
        "implementation_bundle": bundle,
        "adapter": adapter,
        "finalization": {
            "policy_id": "carla_ego_proxy_v0.1",
            "ego_blueprint": "vehicle.chevrolet.impala",
            "ego_length_m": 5.33,
            "ego_width_m": 2.1,
            "artifact_type": "carla_native_scene_v0.1",
        },
    }
    validate_implementation_bundle(method, formal=False)
    write_json(output / "method.json", method)
    library = ROOT / "query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
    roster = read_json(
        ROOT / "benchmark_artifacts/drafts/chatscene_carla_dev_roster_draft.json"
    )
    if pilot:
        record = read_jsonl(library)[0]
        write_jsonl(output / "pilot-library.jsonl", [record])
        library = output / "pilot-library.jsonl"
        roster["queries"] = [
            r for r in roster["queries"] if r["query_id"] == record["query_id"]
        ]
        roster["query_count"] = 1
        roster["expected_response_count"] = 5
    roster.update(
        method_id=method["method_id"],
        library_path=str(library.resolve()),
        library_sha256=sha256_file(library),
    )
    write_json(output / "roster.json", roster)
    runtime = read_json(ROOT / "benchmark_configs/platforms/carla_runtime_draft.json")
    runtime["carla_endpoint"]["port"] = port
    worker = PACKAGE_ROOT / "runtime_worker.py"
    runtime["worker"].update(
        interpreter=str(python),
        interpreter_sha256=sha256_file(python),
        worker=str(worker),
        worker_sha256=sha256_file(worker),
        environment={},
    )
    runtime["server_binary"] = binding(server)
    map_dir = output / "maps"
    map_dir.mkdir()
    code = """import sys,json,carla
from pathlib import Path
client=carla.Client('127.0.0.1',int(sys.argv[2]));client.set_timeout(30)
root=Path(sys.argv[1])
for name in ['Town'+str(i).zfill(2) for i in range(1,8)]:
 world=client.load_world(name)
 (root/(name+'.xodr')).write_text(world.get_map().to_opendrive())
"""
    subprocess.run(
        [str(python), "-I", "-c", code, str(map_dir), str(port)],
        check=True,
        timeout=240,
    )
    scenic_root = source / ".venv/lib/python3.10/site-packages/scenic"
    print("Binding native server and runtime sources...", flush=True)
    runtime["runtime_dependencies"] = {
        "environment_manifest": binding(output / "method-environment.json"),
        "python_path_entries": [],
        "source_trees": [
            runtime_tree_binding(server_root, "carla_server_distribution"),
            runtime_tree_binding(scenic_root, "scenic_editable_source"),
        ],
        "map_catalog": [
            binding(p) for p in sorted(map_dir.glob("*.xodr"), key=lambda p: p.name)
        ],
        "native_carla_files": [
            binding(p)
            for p in sorted(
                (source / ".venv/lib/python3.10/site-packages/carla").iterdir()
            )
            if p.is_file() and p.suffix not in (".pyc", ".pyo")
        ],
    }
    validate_platform_runtime_config(runtime, allow_draft=True)
    write_json(output / "platform.json", runtime)
    controller = read_json(
        ROOT / "benchmark_configs/platforms/carla_controller_draft.json"
    )
    controller["implementation"].update(
        source_path=str(worker), source_sha256=sha256_file(worker)
    )
    _validate_controller_config(controller, "carla", allow_draft=True)
    write_json(output / "controller.json", controller)
    report = {
        "status": "development_only",
        "method_id": method["method_id"],
        "model": model,
        "temperature": 0.9,
        "port": port,
        "artifact_type": "carla_native_scene_v0.1",
        "native_observation_budget_seconds": 30.0,
        "native_sv": "five deterministic confirmations of the captured realization",
        "oracle_input": False,
        "judge_kernel_gate_excluded_from_this_task": True,
    }
    write_json(output / "preparation.json", report)
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=ROOT.parent / "TTSG")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--api-env", type=Path, default=ROOT / ".env")
    p.add_argument("--port", type=int, default=2200)
    p.add_argument("--pilot", action="store_true")
    a = p.parse_args()
    print(
        json.dumps(
            prepare(a.source, a.output, a.api_env, a.port, a.pilot),
            ensure_ascii=False,
            indent=2,
        )
    )
