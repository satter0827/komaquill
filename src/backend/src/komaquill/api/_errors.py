"""Translate adapter failures into stable public API errors without exposing payloads."""

from collections.abc import Iterator
from contextlib import contextmanager
from subprocess import SubprocessError

import httpx
from keyring.errors import KeyringError
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
    except KeyringError:
        raise KomaquillError(
            code=ErrorCode.AUTHENTICATION_FAILED, stage=stage, retryable=False
        ) from None
    except httpx.HTTPError:
        raise KomaquillError(code=ErrorCode.CONNECTION_FAILED, stage=stage) from None
    except SubprocessError:
        raise KomaquillError(code=ErrorCode.COLAB_FAILED, stage=stage) from None
    except FileNotFoundError:
        raise KomaquillError(
            code=ErrorCode.REQUEST_NOT_FOUND, stage=stage, retryable=False
        ) from None
    except OSError:
        raise KomaquillError(code=ErrorCode.STORAGE_FAILED, stage=stage) from None
    except (ValidationError, ValueError):
        raise KomaquillError(code=invalid, stage=stage, retryable=False) from None
