#!/usr/bin/env python3
"""Repository-local entry point for the bus benchmark package."""

from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from bus_benchmark.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
