"""Create safe storage failures shared by repositories."""

from komaquill.domain.errors import KomaquillError


def missing() -> KomaquillError:
    return KomaquillError(code="request_not_found", stage="storage", retryable=False)


def conflict() -> KomaquillError:
    return KomaquillError(code="operation_conflict", stage="storage", retryable=False)
