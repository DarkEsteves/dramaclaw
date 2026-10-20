"""Shared finite API drain backstop; safe to import without Gunicorn."""
from __future__ import annotations

import logging
import os

GRACEFUL_SHUTDOWN_TIMEOUT_ENV = 'NOVELVIDEO_API_GRACEFUL_SHUTDOWN_TIMEOUT'
DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT = 20


def backstop_timeout_from_env() -> int:
    raw = os.environ.get(GRACEFUL_SHUTDOWN_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        logging.getLogger(__name__).warning(
            '%s=%r is not a positive integer; using default %ss',
            GRACEFUL_SHUTDOWN_TIMEOUT_ENV, raw, DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT,
        )
        return DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT
    return value
