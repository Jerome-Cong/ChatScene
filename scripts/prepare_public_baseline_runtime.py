"""Prepare one explicitly declared native backend without overwriting prior configurations."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "benchmark/src"), str(ROOT / "scripts")]
from prepare_chatscene_runtime import binding, environment
from prepare_public_baseline_generation import frozen_python
from bus_benchmark.jsonio import read_json, write_json, sha256_file
from bus_benchmark.paths import PACKAGE_ROOT
from bus_benchmark.runtime import runtime_tree_binding, validate_platform_runtime_config, _validate_controller_config


def prepare(method, source, output, server_root, maps, port):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = source.resolve()
    server_root = server_root.resolve()
    name = "scenic3" if method == "chat2scenic" else "openscenario"
    python = frozen_python(source / "runtimes" / name / ".venv/bin/python")
    manifest = environment(python)
    write_json(output / "runtime-environment.json", manifest)
    worker = PACKAGE_ROOT / "runtime_worker.py"
    platform = read_json(ROOT / "benchmark_configs/platforms/carla_runtime_draft.json")
    platform["worker"].update(interpreter=str(python), interpreter_sha256=sha256_file(python), worker=str(worker),
                              worker_sha256=sha256_file(worker), environment={}, timeout_seconds=180)
    platform["carla_endpoint"].update(host="127.0.0.1", port=port, timeout_seconds=10.0)
    platform["server_binary"] = binding(server_root / "CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping")
    site = python.parent.parent / ("lib/python3.10/site-packages" if method == "chat2scenic" else "lib/python3.8/site-packages")
    backend = site / "scenic" if method == "chat2scenic" else source / "runtimes/openscenario/scenario_runner"
    roles = [runtime_tree_binding(server_root, "carla_server_distribution"),
             runtime_tree_binding(backend, "scenic_editable_source" if method == "chat2scenic" else "openscenario_source")]
    catalog = [binding(p) for p in sorted(maps.resolve().glob("*.xodr"), key=lambda p:p.name)]
    if not catalog:
        raise ValueError("native runtime requires a real frozen map catalog")
    dependencies = {"environment_manifest": binding(output / "runtime-environment.json"), "python_path_entries": [],
                    "source_trees": roles, "map_catalog": catalog,
                    "native_carla_files": [binding(p) for p in sorted((site / "carla").iterdir()) if p.is_file() and p.suffix not in (".pyc", ".pyo")]}
    if method == "chat2scenic":
        dependencies["scenic_compile_options"] = {"mode_2d": True, "use_2d_map": True}
    platform["runtime_dependencies"] = dependencies
    validate_platform_runtime_config(platform, allow_draft=True)
    write_json(output / "platform.json", platform)
    controller = read_json(ROOT / "benchmark_configs/platforms/carla_controller_draft.json")
    controller["implementation"].update(source_path=str(worker), source_sha256=sha256_file(worker))
    _validate_controller_config(controller, "carla", allow_draft=True)
    write_json(output / "controller.json", controller)
    report = {"development_only": True, "method": method, "native_backend": roles[1]["role"], "port": port,
              "native_compile_options": dependencies.get("scenic_compile_options"), "judge_admission_unchanged": True}
    write_json(output / "preparation.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=("chat2scenic", "text2scenario"), required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server-root", type=Path, required=True)
    parser.add_argument("--maps", type=Path, required=True)
    parser.add_argument("--port", type=int, default=2000)
    args = parser.parse_args()
    source = args.source or ROOT.parent / ("Chat2scenic" if args.method == "chat2scenic" else "Text2Scenario")
    print(json.dumps(prepare(args.method, source, args.output, args.server_root, args.maps, args.port), indent=2))
