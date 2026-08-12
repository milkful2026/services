"""Redis cache-aside adapter (ElastiCache in production), key `svc:{pincode}`.

Cache failures are swallowed, not propagated — Redis being down must not
block a serviceability check; only the repository's own DB failure
triggers the spec's fail-closed 503 (ServiceUnavailableError). A cache
miss (real or due to a Redis error) just falls through to the repository.
"""

import json
import logging

import redis
from redis.exceptions import RedisError

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


class RedisZoneCacheAdapter:
    def __init__(self, redis_client: redis.Redis, correlation_id: str = "") -> None:
        self._redis = redis_client
        self._correlation_id = correlation_id

    def get(self, pincode: str) -> dict | None:
        try:
            raw = self._redis.get(self._key(pincode))
        except RedisError as exc:
            logger.error(
                "zone_cache.get failed",
                extra={"correlationId": self._correlation_id, "pincode": pincode, "error": str(exc)},
            )
            return None
        return json.loads(raw) if raw else None

    def set(self, pincode: str, result: dict, ttl_seconds: int) -> None:
        try:
            self._redis.set(self._key(pincode), json.dumps(result), ex=ttl_seconds)
        except RedisError as exc:
            logger.error(
                "zone_cache.set failed",
                extra={"correlationId": self._correlation_id, "pincode": pincode, "error": str(exc)},
            )

    def invalidate(self, pincode: str) -> None:
        try:
            self._redis.delete(self._key(pincode))
        except RedisError as exc:
            logger.error(
                "zone_cache.invalidate failed",
                extra={"correlationId": self._correlation_id, "pincode": pincode, "error": str(exc)},
            )

    @staticmethod
    def _key(pincode: str) -> str:
        return f"svc:{pincode}"
