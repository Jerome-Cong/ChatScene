"""Operating-system locking for atomic, source-bound review checkpoints."""
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from .errors import ValidationError


@contextmanager
def _checkpoint_guard(checkpoint_path: Path):
    """Serialize checkpoint creation and compare-and-swap across kernels."""

    checkpoint_path = Path(checkpoint_path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(checkpoint_path) + ".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        raise ValidationError("cannot open workbench checkpoint lock") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
