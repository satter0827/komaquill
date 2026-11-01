"""Keep known persistence failures safe at the adapter boundary."""

from collections.abc import Callable
from functools import wraps
from sqlite3 import Error as SQLiteError

from sqlalchemy.exc import SQLAlchemyError

from komaquill.domain.errors import KomaquillError


def stored[**P, T](function: Callable[P, T]) -> Callable[P, T]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return function(*args, **kwargs)
        except (OSError, ValueError, SQLAlchemyError, SQLiteError):
            raise KomaquillError(code="storage_failed", stage="storage") from None

    return wrapped
