"""Shared SQLAlchemy-error-translation mixin for Core repositories.

Was hand-duplicated byte-for-byte across wallet/subscription/order/
payment/catalog's own `_db_operation` context managers — moved here per
services/README.md §2. Each service still raises its OWN domain
`ServiceUnavailableError` subclass (there is no shared exception
hierarchy to unify on — `shared/errors.py`'s own `ServiceUnavailableError`
is explicitly documented as being for adapters with no owning domain
hierarchy, not a drop-in replacement for these), so the exception type
and log-line prefix are parameterized per subclass via two required class
attributes rather than hardcoded here.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar

from sqlalchemy.exc import SQLAlchemyError


class SqlAlchemyOperationMixin:
    """Mix into a repository class that sets `self._correlation_id: str`
    in its own `__init__`. Concrete subclasses must also set:
      _unavailable_error: the service's own domain ServiceUnavailableError
        subclass, raised (wrapping the original exception) on any
        SQLAlchemyError.
      _log_prefix: the log-message prefix matching this repository
        module's own conventional name (e.g. "wallet_repository") —
        preserves today's exact log line text, since it isn't always
        derivable from the class name (SqlAlchemyProductRepository's file
        is product_repository.py, not productrepository.py).
    """

    _unavailable_error: ClassVar[type[Exception]]
    _log_prefix: ClassVar[str]
    _correlation_id: str

    @contextmanager
    def _db_operation(self, operation: str, failure_message: str) -> Iterator[None]:
        try:
            yield
        except SQLAlchemyError as exc:
            logging.getLogger(type(self).__module__).error(
                f"{self._log_prefix}.{operation} failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise self._unavailable_error(failure_message) from exc
