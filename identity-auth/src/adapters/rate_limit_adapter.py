"""Redis-backed rate limiter adapter (ElastiCache in production).

Per services/README.md §3.7: the only place allowed to import redis for
this concern. Domain code depends on adapters.interfaces.RateLimiterPort.
"""

import logging

import redis
from redis.exceptions import RedisError

from domain.exceptions import ExternalServiceUnavailableError, RateLimitExceededError

logger = logging.getLogger(__name__)


def build_redis_client(host: str, port: int, use_tls: bool) -> redis.Redis:
    return redis.Redis(
        host=host,
        port=port,
        ssl=use_tls,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )


class RedisRateLimiterAdapter:
    def __init__(self, redis_client: redis.Redis, correlation_id: str = "") -> None:
        self._redis = redis_client
        self._correlation_id = correlation_id

    def check_and_increment(self, key: str, max_requests: int, window_seconds: int) -> None:
        try:
            count = self._redis.incr(key)
            ttl = self._redis.ttl(key)
            if ttl < 0:
                # Either this is the request that created the counter, or a
                # prior expire() call failed and left the key without a TTL
                # (e.g. a transient Redis error between incr and expire) —
                # either way, self-heal by (re)setting the expiry now rather
                # than only ever attempting it once on count == 1.
                self._redis.expire(key, window_seconds)
        except RedisError as exc:
            logger.error(
                "rate_limiter.check_and_increment failed",
                extra={"correlationId": self._correlation_id, "key": key, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Rate limiter unavailable") from exc

        if count > max_requests:
            raise RateLimitExceededError(f"Rate limit exceeded: {count}/{max_requests}")


class RedisLockAdapter:
    """Short-lived mutex built on Redis SET NX/EX — used to serialize the
    otp/send check-then-act critical section per mobile so two concurrent
    requests can't both observe "no active OTP" and each create one."""

    def __init__(self, redis_client: redis.Redis, correlation_id: str = "") -> None:
        self._redis = redis_client
        self._correlation_id = correlation_id

    def acquire(self, key: str, ttl_seconds: int) -> bool:
        try:
            return bool(self._redis.set(key, "1", nx=True, ex=ttl_seconds))
        except RedisError as exc:
            logger.error(
                "lock.acquire failed",
                extra={"correlationId": self._correlation_id, "key": key, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Lock service unavailable") from exc

    def release(self, key: str) -> None:
        try:
            self._redis.delete(key)
        except RedisError as exc:
            # Best-effort: the lock's own TTL guarantees it is released even
            # if this delete fails, so a transient error here isn't fatal.
            logger.warning(
                "lock.release failed",
                extra={"correlationId": self._correlation_id, "key": key, "error": str(exc)},
            )
