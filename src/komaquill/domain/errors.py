"""Expose safe error categories without including remote payloads."""

from enum import StrEnum

from pydantic import BaseModel


class ErrorCode(StrEnum):
    """Identify safe, machine-readable product failure categories."""

    AUTHENTICATION_FAILED = "authentication_failed"
    BROWSER_FAILED = "browser_failed"
    COLAB_FAILED = "colab_failed"
    COLLECTION_FAILED = "collection_failed"
    CONNECTION_FAILED = "connection_failed"
    DIAGNOSTICS_FAILED = "diagnostics_failed"
    GENERATION_FAILED = "generation_failed"
    GENERATION_TIMEOUT = "generation_timeout"
    HTTP_FAILED = "http_failed"
    IDENTITY_MISMATCH = "identity_mismatch"
    IMAGE_INVALID = "image_invalid"
    INTERNAL_ERROR = "internal_error"
    INVALID_EVALUATION = "invalid_evaluation"
    REAL_LLM_NOT_ALLOWED = "real_llm_not_allowed"
    LLM_DEPENDENCIES_MISSING = "llm_dependencies_missing"
    LLM_UNAVAILABLE = "llm_unavailable"
    STORY_PLAN_INVALID = "story_plan_invalid"
    STORY_REPLAN_REQUIRED = "story_replan_required"
    INVALID_STORY_COUNT = "invalid_story_count"
    IMAGE_BACKEND_UNAVAILABLE = "image_backend_unavailable"
    FAKE_STORY_MISMATCH = "fake_story_mismatch"
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_OUTPUT = "invalid_output"
    INVALID_REQUEST = "invalid_request"
    INVALID_RESPONSE = "invalid_response"
    LOG_TIMEOUT = "log_timeout"
    LOG_UNAVAILABLE = "log_unavailable"
    MISSING_IMAGE = "missing_image"
    OPERATION_FAILED = "operation_failed"
    REQUEST_NOT_FOUND = "request_not_found"
    RUNTIME_ENDED = "runtime_ended"
    SHUTDOWN_TIMEOUT = "shutdown_timeout"
    STOPPING = "stopping"
    PUBLICATION_FAILED = "publication_failed"
    MIGRATION_REQUIRED = "migration_required"
    STORAGE_FAILED = "storage_failed"
    SUBMISSION_UNKNOWN = "submission_unknown"
    UNSUPPORTED_ASSET = "unsupported_asset"
    EXTENSION_UNAVAILABLE = "extension_unavailable"
    UNSUPPORTED_IMAGE = "unsupported_image"
    UNSUPPORTED_BATCH = "unsupported_batch"


class OperationStage(StrEnum):
    """Identify the operation phase independently of displayed text."""

    CHECK = "check"
    CLEANUP = "cleanup"
    COLAB_CONTROL = "colab_control"
    COLLECTION = "collection"
    COMMUNICATION = "communication"
    CONNECTION = "connection"
    DISCONNECT = "disconnect"
    DISPLAY = "display"
    FINALIZE = "finalize"
    EVALUATION = "evaluation"
    GENERATION = "generation"
    GPU_STOP = "gpu_stop"
    HISTORY_VALIDATION = "history_validation"
    IMAGE_COLLECTION = "image_collection"
    IMAGE_VALIDATION = "image_validation"
    LOG_CLEANUP = "log_cleanup"
    LOG_COLLECTION = "log_collection"
    LOG_DOWNLOAD = "log_download"
    LOG_TIMEOUT = "log_timeout"
    OPERATION = "operation"
    PREPARATION = "preparation"
    RELEASE = "release"
    RESPONSE_VALIDATION = "response_validation"
    STORAGE = "storage"
    SUBMISSION = "submission"
    STORY_PLANNING = "story_planning"
    STORY_RENDERING = "story_rendering"
    SUMMARY = "summary"


class FailureInfo(BaseModel):
    """Describe a failure without copying exception messages or response bodies."""

    code: ErrorCode
    stage: OperationStage
    exception_type: str
    http_status: int | None = None
    retryable: bool = True


class KomaquillError(Exception):
    """Describe an actionable failure and safe diagnostic attributes."""

    def __init__(
        self,
        message: str = "",
        *,
        code: ErrorCode | str = ErrorCode.OPERATION_FAILED,
        stage: OperationStage | str = OperationStage.OPERATION,
        http_status: int | None = None,
        retryable: bool = True,
        details: dict[str, str] | None = None,
        display_key: str | None = None,
    ) -> None:
        super().__init__(message or display_key or str(code))
        self.code = ErrorCode(code)
        self.stage = OperationStage(stage)
        self.http_status = http_status
        self.retryable = retryable
        self.details = details or {}
        self.display_key = display_key


class SubmissionUnknown(KomaquillError):
    """Indicate that a request must be reconciled rather than resubmitted."""


class AllocationRefused(KomaquillError):
    """Indicate that the provider explicitly refused allocation."""


class RuntimeEnded(KomaquillError):
    """Indicate that the owned runtime is confirmed absent."""


def failure_info(error: BaseException, stage: OperationStage | str) -> "FailureInfo":
    """Keep allowlisted attributes, never exception text or local variables."""
    from pydantic import ValidationError

    if isinstance(error, KomaquillError):
        code, retryable = error.code, error.retryable
    elif isinstance(error, ValidationError):
        code, retryable = ErrorCode.INVALID_RESPONSE, False
    elif isinstance(error, OSError):
        code, retryable = ErrorCode.STORAGE_FAILED, True
    else:
        code, retryable = ErrorCode.INTERNAL_ERROR, False
    return FailureInfo(
        code=ErrorCode(code),
        stage=error.stage
        if isinstance(error, KomaquillError) and error.stage != OperationStage.OPERATION
        else OperationStage(stage),
        exception_type=type(error).__name__,
        http_status=error.http_status if isinstance(error, KomaquillError) else None,
        retryable=retryable,
    )
