"""Method-adapter contracts for the external generation harness.

Adapters deliberately receive only the surface query and a fresh working
directory.  Roster metadata, oracle fields, and repetition identifiers stay in
the harness and are never part of the method request.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol


@dataclass(frozen=True)
class AdapterOutcome:
    """Raw result of one method invocation.

    ``disposition`` is populated only after a process exits successfully and
    its configured output protocol is valid.  An artifact may still be present
    for a failed invocation; the runner preserves it as raw evidence but never
    promotes it to the finalized benchmark artifact.
    """

    stdout: bytes
    stderr: bytes
    exit_code: Optional[int]
    timed_out: bool
    disposition: Optional[str] = None
    artifact_path: Optional[Path] = None
    unsupported_response_stream: Optional[str] = None
    error: Optional[str] = None


class MethodAdapter(Protocol):
    """Minimal query-only method boundary used by :mod:`bus_benchmark.generation`."""

    def generate(self, query_text: str, workdir: Path) -> AdapterOutcome:
        """Invoke a method once in ``workdir`` using only ``query_text``."""


__all__ = ["AdapterOutcome", "MethodAdapter"]
