"""Redis-backed 2FA lockout counter for the Admin Identity/RBAC feature
(MA-129 FR-2).

Per services/README.md §3.7: the only place (alongside rate_limit_adapter
and admin_session_registry) allowed to import redis for this concern.
Reuses the same ElastiCache Redis instance already provisioned for the
consumer OTP rate limiter (adapters/rate_limit_adapter.py's
build_redis_client) rather than standing up a second cache.

Keyed by `admin_user.id`, NOT by challengeToken — spec FR-2 is explicit
that a per-token counter would never accumulate across attempts, since
a fresh login always mints a fresh challengeToken.
"""

import logging

import redis
from redis.exceptions import RedisError

from domain.exceptions import ExternalServiceUnavailableError

logger = logging.getLogger(__name__)

_FAILURE_KEY_PREFIX = "admin:2fa:failures:"
_LOCK_KEY_PREFIX = "admin:2fa:locked:"


class AdminLockoutAdapter:
    def __init__(self, redis_client: redis.Redis, correlation_id: str = "") -> None:
        self._redis = redis_client
        self._correlation_id = correlation_id

    def record_failure(self, admin_id: str, window_seconds: int) -> int:
        key = f"{_FAILURE_KEY_PREFIX}{admin_id}"
        try:
            count = self._redis.incr(key)
            ttl = self._redis.ttl(key)
            if ttl < 0:
                # First failure in a fresh window, or a prior expire()
                # call failed to stick — (re)set the rolling window here
                # rather than trusting it was set exactly once, same
                # self-healing approach as RedisRateLimiterAdapter.
                self._redis.expire(key, window_seconds)
        except RedisError as exc:
            logger.error(
                "admin_lockout.record_failure failed",
                extra={"correlationId": self._correlation_id, "adminId": admin_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Lockout counter unavailable") from exc
        return int(count)

    def is_locked(self, admin_id: str) -> bool:
        try:
            return bool(self._redis.exists(f"{_LOCK_KEY_PREFIX}{admin_id}"))
        except RedisError as exc:
            logger.error(
                "admin_lockout.is_locked failed",
                extra={"correlationId": self._correlation_id, "adminId": admin_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Lockout status unavailable") from exc

    def lock(self, admin_id: str, ttl_seconds: int) -> None:
        try:
            self._redis.set(f"{_LOCK_KEY_PREFIX}{admin_id}", "1", ex=ttl_seconds)
        except RedisError as exc:
            logger.error(
                "admin_lockout.lock failed",
                extra={"correlationId": self._correlation_id, "adminId": admin_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to lock account") from exc

    def reset(self, admin_id: str) -> None:
        try:
            self._redis.delete(f"{_FAILURE_KEY_PREFIX}{admin_id}", f"{_LOCK_KEY_PREFIX}{admin_id}")
        except RedisError as exc:
            # Best-effort: a failure to reset here is not fatal to the
            # caller (a successful login just occurred) but does mean a
            # stale failure count could persist into a still-live window.
            logger.warning(
                "admin_lockout.reset failed",
                extra={"correlationId": self._correlation_id, "adminId": admin_id, "error": str(exc)},
            )
