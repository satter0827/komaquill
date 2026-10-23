"""Expose safe error categories without including remote payloads."""


class KomaquillError(Exception):
    """Describe an actionable failure and safe diagnostic attributes."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "operation_failed",
        stage: str = "operation",
        http_status: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.http_status = http_status
        self.retryable = retryable


class SubmissionUnknown(KomaquillError):
    """Indicate that a request must be reconciled rather than resubmitted."""


class AllocationRefused(KomaquillError):
    """Indicate that the provider explicitly refused allocation."""


class RuntimeEnded(KomaquillError):
    """Indicate that the owned runtime is confirmed absent."""
