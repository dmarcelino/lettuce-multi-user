"""Logging with a last line of defence: while vault values are being shared, any
log record that contains one is rewritten with it redacted. Nothing is supposed
to log values in the first place; this catches mistakes, not design."""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

REDACTED = "[REDACTED]"
# Shorter values ("PT", "Gold") are too common to redact without mangling logs.
MIN_REDACT_LENGTH = 6


class SecretFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._active: dict[int, list[str]] = {}
        self._next = 0

    @contextmanager
    def guarding(self, values: Iterable[str]) -> Iterator[None]:
        forms = sorted({v for v in values if len(v) >= MIN_REDACT_LENGTH}, key=len, reverse=True)
        with self._lock:
            key = self._next
            self._next += 1
            self._active[key] = forms
        try:
            yield
        finally:
            with self._lock:
                del self._active[key]

    def filter(self, record: logging.LogRecord) -> bool:
        with self._lock:
            forms = [f for fs in self._active.values() for f in fs]
        if not forms:
            return True
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        cleaned, exc = message, record.exc_text
        for f in forms:
            cleaned = cleaned.replace(f, REDACTED)
            exc = exc.replace(f, REDACTED) if exc else exc
        if cleaned != message or exc != record.exc_text:
            record.msg, record.args, record.exc_text, record.exc_info = cleaned, None, exc, None
        return True


SECRET_FILTER = SecretFilter()


def setup() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(SECRET_FILTER)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # Libraries that could log request or response details at DEBUG stay quiet.
    for name in ("httpcore", "httpx", "urllib3", "jwt", "pywebpush", "mcp", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
