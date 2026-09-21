"""Unified bus-scene benchmark tooling.

This package is intentionally external to ChatScene's generation pipeline.  It
owns benchmark data validation, immutable manifests, semantic scoring, and
platform-track adapters, but it does not repair generated scene semantics.
"""

from .constants import BENCHMARK_VERSION, SCHEMA_VERSION

__all__ = ["BENCHMARK_VERSION", "SCHEMA_VERSION"]
