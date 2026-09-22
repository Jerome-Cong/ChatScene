"""Locations owned by the installed benchmark distribution."""

import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
ASSET_ROOT = PACKAGE_ROOT / "assets"


def asset_path(*parts: str) -> Path:
    """Return a read-only, installed benchmark asset path."""

    path = ASSET_ROOT.joinpath(*parts)
    if not path.exists():
        raise FileNotFoundError("benchmark asset is missing: {}".format(path))
    return path


def workspace_root() -> Path:
    """Return caller-owned state, never a ChatScene checkout path."""

    configured = os.environ.get("BUS_BENCHMARK_WORKSPACE")
    return Path(configured).expanduser().resolve() if configured else Path.cwd().resolve()


__all__ = ["ASSET_ROOT", "PACKAGE_ROOT", "asset_path", "workspace_root"]
