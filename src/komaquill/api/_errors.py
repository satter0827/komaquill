"""Translate adapter failures into stable public API errors without exposing payloads."""

from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import ValidationError

from komaquill.domain.errors import ErrorCode, KomaquillError, OperationStage


@contextmanager
def api_operation(
    stage: OperationStage, *, invalid: ErrorCode = ErrorCode.INVALID_OUTPUT
) -> Iterator[None]:
    try:
        yield
    except KomaquillError:
        raise
    except FileNotFoundError:
        raise KomaquillError(
            code=ErrorCode.REQUEST_NOT_FOUND, stage=stage, retryable=False
        ) from None
    except OSError:
        raise KomaquillError(code=ErrorCode.STORAGE_FAILED, stage=stage) from None
    except (ValidationError, ValueError):
        raise KomaquillError(code=invalid, stage=stage, retryable=False) from None
