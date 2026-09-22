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


def review_workspace(path: Path) -> Path:
    """Validate explicit caller-owned state; never create an implicit workspace."""
    from .errors import ValidationError

    if path is None or not str(path).strip():
        raise ValidationError("review workspace must be explicitly provided")
    resolved = review_state_path(path)
    if not resolved.is_dir():
        raise ValidationError("review workspace does not exist or is not a directory: {}".format(resolved))
    return resolved


def review_state_path(path: Path) -> Path:
    """Reject checkpoint/finalization paths inside installation directories."""
    from .errors import ValidationError
    import sysconfig

    resolved = Path(path).expanduser().resolve()
    roots = [PACKAGE_ROOT.resolve()]
    roots.extend(Path(p).resolve() for p in (sysconfig.get_path("purelib"), sysconfig.get_path("platlib")) if p)
    for root in roots:
        if resolved == root or root in resolved.parents:
            raise ValidationError("review state must be outside package and site-packages directories")
    return resolved


__all__ = ["ASSET_ROOT", "PACKAGE_ROOT", "asset_path", "workspace_root", "review_workspace", "review_state_path"]
