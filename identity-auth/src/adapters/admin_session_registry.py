"""Redis-backed active-session registry for `maxConcurrentSessions` LRU
eviction (MA-129 FR-5).

**Flagged architecture decision** (see this service's README): Cognito
has no native API to enumerate or selectively revoke a user's active
refresh tokens by age — `AdminUserGlobalSignOut` revokes *all* of them.
Implementing "evict the oldest session when a new login exceeds the
limit" therefore requires this service to track issued refresh tokens
itself. That means refresh tokens (bearer-equivalent credentials) are
held server-side in Redis for the sole purpose of later calling
`RevokeToken` on the oldest one — a real secret-handling trade-off,
called out for security review rather than silently accepted. Entries
carry a generous TTL so a forgotten/never-evicted entry doesn't linger
indefinitely if this bookkeeping ever falls out of sync with Cognito.
"""

import logging

import redis
from redis.exceptions import RedisError

from domain.exceptions import ExternalServiceUnavailableError

logger = logging.getLogger(__name__)

_SESSIONS_KEY_PREFIX = "admin:sessions:"
_ENTRY_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days — well beyond any realistic refresh-token lifetime


class AdminSessionRegistryAdapter:
    def __init__(self, redis_client: redis.Redis, correlation_id: str = "") -> None:
        self._redis = redis_client
        self._correlation_id = correlation_id

    def register_session(
        self, admin_id: str, refresh_token: str, max_concurrent_sessions: int | None
    ) -> str | None:
        key = f"{_SESSIONS_KEY_PREFIX}{admin_id}"
        try:
            # A Redis List used as an append-only-at-the-right-end queue:
            # RPUSH adds the newest session at the tail, LPOP evicts the
            # oldest (leftmost) one once the list exceeds the limit.
            self._redis.rpush(key, refresh_token)
            self._redis.expire(key, _ENTRY_TTL_SECONDS)

            if not max_concurrent_sessions or max_concurrent_sessions <= 0:
                return None

            length = self._redis.llen(key)
            if length <= max_concurrent_sessions:
                return None

            evicted = self._redis.lpop(key)
            return evicted.decode() if isinstance(evicted, bytes) else evicted
        except RedisError as exc:
            logger.error(
                "admin_session_registry.register_session failed",
                extra={"correlationId": self._correlation_id, "adminId": admin_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Session registry unavailable") from exc

    def invalidate_all(self, admin_id: str) -> None:
        try:
            self._redis.delete(f"{_SESSIONS_KEY_PREFIX}{admin_id}")
        except RedisError as exc:
            logger.warning(
                "admin_session_registry.invalidate_all failed",
                extra={"correlationId": self._correlation_id, "adminId": admin_id, "error": str(exc)},
            )
