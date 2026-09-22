"""Typed benchmark failures used by the CLI and tests."""


class BenchmarkError(Exception):
    """Base class for expected benchmark failures."""


class ValidationError(BenchmarkError):
    """Raised when a versioned input violates its declared contract."""


class FreezeError(BenchmarkError):
    """Raised when a freeze or locked-test gate is not satisfied."""


class AdapterError(BenchmarkError):
    """Raised when a method or platform adapter cannot complete its stage."""
