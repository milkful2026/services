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
            if count == 1:
                # Only the request that created the counter sets its
                # expiry — avoids resetting the window on every request.
                self._redis.expire(key, window_seconds)
        except RedisError as exc:
            logger.error(
                "rate_limiter.check_and_increment failed",
                extra={"correlationId": self._correlation_id, "key": key, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Rate limiter unavailable") from exc

        if count > max_requests:
            raise RateLimitExceededError(f"Rate limit exceeded: {count}/{max_requests}")
