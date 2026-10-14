"""Package-import entrypoint for isolated public CARLA initial observations."""

import sys
import os
import argparse
import json
import resource
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark/src"))

PROFILE = {"profile_id": "public_initial_observer_development_v0.1",
           "cpu_seconds": 20, "address_space_bytes": 2 * 1024**3,
           "logical_cpu_count": 1, "wall_seconds": 30,
           "native_observer_wall_seconds": 20,
           "max_stdout_bytes": 8 * 1024**2, "max_stderr_bytes": 8 * 1024**2,
           "file_output_bytes": 16 * 1024**2, "open_files": 64,
           "formal_extractor_budget": False}


def extract_initial_carla(payload):
    # Keep NumPy/OpenBLAS within the existing extractor memory limit. Affinity
    # is inherited by the native observer child, whose environment is cleared.
    if not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("public initial observer profile requires CPU affinity support")
    allowed = os.sched_getaffinity(0)
    if not allowed:
        raise RuntimeError("public initial observer has no permitted CPU")
    os.sched_setaffinity(0, {min(allowed)})
    from bus_benchmark.fixture_extractor_common import extract
    return extract(payload, "carla")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenic-python", required=True)
    parser.add_argument("--scenic-2d", action="store_true")
    args = parser.parse_args()
    os.environ["BUS_BENCHMARK_SCENIC_PYTHON"] = args.scenic_python
    os.environ["BUS_BENCHMARK_SCENIC_2D"] = "1" if args.scenic_2d else "0"
    for kind, maximum in ((resource.RLIMIT_CPU, PROFILE["cpu_seconds"]),
                          (resource.RLIMIT_AS, PROFILE["address_space_bytes"]),
                          (resource.RLIMIT_FSIZE, PROFILE["file_output_bytes"]),
                          (resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, PROFILE["open_files"])):
        resource.setrlimit(kind, (maximum, maximum))
    projection = extract_initial_carla(json.load(sys.stdin))
    json.dump({"resource_profile": PROFILE, "projection": projection}, sys.stdout,
              ensure_ascii=False, sort_keys=True, allow_nan=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
