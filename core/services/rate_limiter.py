"""
Per-model rate limiting for LLM API calls.

Respects rate_limit_rpm (requests per minute) and max_concurrency from model
config.  The limiter acts as a context manager:

    with get_limiter(model_config_id, rpm, concurrency):
        result = call_llm(...)

- Acquiring blocks until both a concurrency slot is free *and* the minimum
  inter-request interval has elapsed (RPM throttle).
- Releasing frees the concurrency slot so another thread can proceed.
"""

import time
from threading import Lock, Semaphore


class RateLimiter:
    """Thread-safe rate limiter: token-bucket RPM + concurrency semaphore."""

    def __init__(self, requests_per_minute: int, max_concurrency: int = 1):
        self.rpm = max(1, requests_per_minute)
        self.min_interval = 60.0 / self.rpm  # seconds between *dispatches*
        self._last_dispatch_time = 0.0
        self._dispatch_lock = Lock()
        self._semaphore = Semaphore(max(1, max_concurrency))

    # ------------------------------------------------------------------
    # Context manager interface (preferred)
    # ------------------------------------------------------------------

    def __enter__(self) -> "RateLimiter":
        self._semaphore.acquire()
        # Once we hold a slot, enforce the RPM interval before dispatching.
        with self._dispatch_lock:
            now = time.monotonic()
            elapsed = now - self._last_dispatch_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_dispatch_time = time.monotonic()
        return self

    def __exit__(self, *_) -> None:
        self._semaphore.release()

    # ------------------------------------------------------------------
    # Legacy sequential interface (kept for backward compatibility)
    # ------------------------------------------------------------------

    def wait_if_needed(self) -> None:
        """Block until it's safe to make another request (sequential callers)."""
        with self._dispatch_lock:
            now = time.monotonic()
            elapsed = now - self._last_dispatch_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_dispatch_time = time.monotonic()


# Global registry of limiters per model config id (for Celery workers)
_limiters: dict[str, RateLimiter] = {}
_limiters_lock = Lock()


def get_limiter(model_config_id: str, rpm: int, max_concurrency: int = 1) -> RateLimiter:
    """Get or create a rate limiter for a model config.

    If the concurrency or RPM settings differ from an existing limiter, the
    limiter is recreated so the new settings take effect.
    """
    with _limiters_lock:
        existing = _limiters.get(model_config_id)
        if existing is None or existing.rpm != max(1, rpm) or existing._semaphore._value != max(1, max_concurrency):
            _limiters[model_config_id] = RateLimiter(rpm, max_concurrency)
        return _limiters[model_config_id]
