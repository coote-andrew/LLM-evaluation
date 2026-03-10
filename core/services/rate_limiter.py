"""
Per-model rate limiting for LLM API calls.

Respects rate_limit_rpm (requests per minute) from model config.
"""

import time
from threading import Lock


class RateLimiter:
    """Thread-safe rate limiter using token bucket / sliding window."""

    def __init__(self, requests_per_minute: int):
        self.rpm = max(1, requests_per_minute)
        self.min_interval = 60.0 / self.rpm  # seconds between requests
        self._last_request_time = 0.0
        self._lock = Lock()

    def wait_if_needed(self) -> None:
        """Block until it's safe to make another request."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self.min_interval:
                sleep_time = self.min_interval - elapsed
                time.sleep(sleep_time)
            self._last_request_time = time.monotonic()


# Global registry of limiters per model config id (for Celery workers)
_limiters: dict[str, RateLimiter] = {}
_limiters_lock = Lock()


def get_limiter(model_config_id: str, rpm: int) -> RateLimiter:
    """Get or create a rate limiter for a model config."""
    with _limiters_lock:
        if model_config_id not in _limiters:
            _limiters[model_config_id] = RateLimiter(rpm)
        return _limiters[model_config_id]
