"""Isolated JSON worker for executing one frozen semantic extractor call."""

import importlib.util
import json
import os
import resource
import sys


def _limit_resources() -> None:
    limits = (
        (resource.RLIMIT_CPU, (5, 5)),
        (resource.RLIMIT_AS, (2 * 1024 * 1024 * 1024, 2 * 1024 * 1024 * 1024)),
        (resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024)),
        (resource.RLIMIT_CORE, (0, 0)),
        (resource.RLIMIT_NOFILE, (64, 64)),
    )
    for limit, value in limits:
        resource.setrlimit(limit, value)


def main() -> int:
    if len(sys.argv) != 3:
        raise RuntimeError("expected extractor path and callable name")
    _limit_resources()
    source_path, callable_name = sys.argv[1:]
    specification = importlib.util.spec_from_file_location(
        "_bus_benchmark_frozen_extractor", source_path
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("extractor has no import loader")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    function = getattr(module, callable_name)
    if not callable(function):
        raise RuntimeError("extractor entrypoint is not callable")
    value = json.load(sys.stdin)
    result = function(value)
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write("extractor worker failed: {}\n".format(exc))
        raise SystemExit(1)
