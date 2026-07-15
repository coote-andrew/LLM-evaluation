"""Helpers for dispatching Celery tasks with a thread fallback when Redis is down."""

from __future__ import annotations

import socket
import threading
from urllib.parse import urlparse

from django.conf import settings


def broker_reachable() -> bool:
    """Return True if the Celery broker TCP port is accepting connections."""
    try:
        parsed = urlparse(settings.CELERY_BROKER_URL)
        host = parsed.hostname or "localhost"
        port = parsed.port or 6379
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def dispatch_task(task, *args, **kwargs) -> None:
    """
    Run ``task`` via Celery when the broker is up; otherwise in a daemon thread.

    The thread fallback is emergency-only (e.g. local/dev without Redis). It
    nests long-running work inside the web process and should not be relied on
    in production.
    """
    if broker_reachable():
        task.delay(*args, **kwargs)
    else:
        threading.Thread(
            target=task,
            args=args,
            kwargs=kwargs,
            daemon=True,
        ).start()
