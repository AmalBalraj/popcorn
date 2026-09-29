"""Thread-safe breaker with a single half-open lease and bounded backoff."""

import threading
import time


class ProviderError(Exception):
    def __init__(self, reason, *, status=None, retryable=False, retry_after=0):
        super().__init__(reason)
        self.reason, self.status = reason, status
        self.retryable, self.retry_after = retryable, retry_after


class Health:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.success_count = self.failure_count = self.consecutive_failures = 0
        self.malformed_failures = 0
        self.latency_ms = self.last_success = self.last_failure = None
        self.failure_reason = self.status_code = None
        self.open_until = self.rate_limit_until = 0
        self.probing = False

    def acquire(self):
        with self.lock:
            now = time.time()
            if now < max(self.open_until, self.rate_limit_until) or self.probing:
                return False
            if self.open_until:
                self.probing = True
            return True

    def acquire_probe(self):
        with self.lock:
            if self.probing or time.time() < max(
                self.open_until, self.rate_limit_until
            ):
                return False
            self.probing = True
            return True

    def success(self, elapsed):
        with self.lock:
            self.success_count += 1
            self.last_success = time.time()
            self.latency_ms = round(elapsed * 1000, 1)
            self.consecutive_failures = 0
            self.open_until = self.rate_limit_until = 0
            self.probing = False

    def failure(self, error, elapsed):
        with self.lock:
            self.failure_count += 1
            self.consecutive_failures += 1
            self.last_failure = time.time()
            self.latency_ms = round(elapsed * 1000, 1)
            self.failure_reason = getattr(error, "reason", type(error).__name__)
            self.status_code = getattr(error, "status", None)
            if self.failure_reason in {
                "invalid_json",
                "schema",
                "content_type",
                "challenge",
                "invalid_payload",
                "parser",
            }:
                self.malformed_failures += 1
            if self.status_code == 429:
                self.rate_limit_until = time.time() + max(
                    self.config.cooldown, getattr(error, "retry_after", 0)
                )
            if (
                self.consecutive_failures >= self.config.failure_threshold
                or self.probing
            ):
                exponent = min(
                    12,
                    max(0, self.consecutive_failures - self.config.failure_threshold),
                )
                self.open_until = time.time() + min(
                    self.config.max_cooldown, self.config.cooldown * 2**exponent
                )
            self.probing = False

    def snapshot(self):
        with self.lock:
            state = (
                "OPEN"
                if time.time() < max(self.open_until, self.rate_limit_until)
                else "DEGRADED"
                if self.consecutive_failures
                else "HEALTHY"
                if self.success_count
                else "UNKNOWN"
            )
            return {
                name: getattr(self, name)
                for name in (
                    "success_count",
                    "failure_count",
                    "consecutive_failures",
                    "malformed_failures",
                    "latency_ms",
                    "last_success",
                    "last_failure",
                    "failure_reason",
                    "status_code",
                    "open_until",
                    "rate_limit_until",
                )
            } | {"state": state, "half_open": self.probing}
