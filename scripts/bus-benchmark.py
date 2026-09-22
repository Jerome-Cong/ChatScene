#!/usr/bin/env python3
"""Compatibility script for the installed benchmark CLI (install first)."""
from bus_benchmark.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
