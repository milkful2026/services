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
from redis.exceptions import RedisError, WatchError

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
    ) -> list[str]:
        """Push-then-trim is one WATCH/MULTI/EXEC transaction (retried
        on WatchError) rather than separate RPUSH/LLEN/LPOP calls, so
        two concurrent logins for the same admin can't both read a
        stale length and either double-evict or let the tracked-session
        count silently exceed `max_concurrent_sessions`. `0` is treated
        as a real, distinct limit ("no sessions allowed", evicting the
        just-registered token itself) rather than falling through to
        "unlimited" the way a bare `if not max_concurrent_sessions`
        check would."""
        key = f"{_SESSIONS_KEY_PREFIX}{admin_id}"
        try:
            with self._redis.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch(key)
                        current_length = pipe.llen(key)
                        evict_count = 0
                        if max_concurrent_sessions is not None and max_concurrent_sessions >= 0:
                            evict_count = max(0, (current_length + 1) - max_concurrent_sessions)

                        pipe.multi()
                        pipe.rpush(key, refresh_token)
                        pipe.expire(key, _ENTRY_TTL_SECONDS)
                        for _ in range(evict_count):
                            pipe.lpop(key)
                        results = pipe.execute()
                        break
                    except WatchError:
                        continue

            evicted_raw = results[2:]
            return [e.decode() if isinstance(e, bytes) else e for e in evicted_raw if e is not None]
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
