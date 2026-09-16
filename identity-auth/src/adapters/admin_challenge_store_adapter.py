"""Redis-backed store for the FR-1/FR-2 login `challengeToken`.

Reuses the same ElastiCache Redis instance already provisioned for the
consumer OTP rate limiter, instead of standing up a new DynamoDB table
for what is structurally the same kind of ephemeral, short-TTL,
single-use-on-success record `otp_requests` already models — Redis's
native key TTL is a natural fit and avoids new infrastructure for this
additive feature.

Per services/README.md §3.7: the only place allowed to import redis for
this concern.
"""

import json
import logging

import redis
from redis.exceptions import RedisError

from domain.admin_models import LoginChallenge
from domain.exceptions import ExternalServiceUnavailableError

logger = logging.getLogger(__name__)

_KEY_PREFIX = "admin:challenge:"


class AdminChallengeStoreAdapter:
    def __init__(self, redis_client: redis.Redis, correlation_id: str = "") -> None:
        self._redis = redis_client
        self._correlation_id = correlation_id

    def put(self, challenge: LoginChallenge, ttl_seconds: int) -> None:
        key = f"{_KEY_PREFIX}{challenge.challenge_token}"
        value = json.dumps(
            {
                "adminId": challenge.admin_id,
                "email": challenge.email,
                "cognitoSession": challenge.cognito_session,
            }
        )
        try:
            self._redis.set(key, value, ex=max(ttl_seconds, 1))
        except RedisError as exc:
            logger.error(
                "admin_challenge_store.put failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to persist login challenge") from exc

    def get(self, challenge_token: str) -> LoginChallenge | None:
        try:
            raw = self._redis.get(f"{_KEY_PREFIX}{challenge_token}")
        except RedisError as exc:
            logger.error(
                "admin_challenge_store.get failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to read login challenge") from exc

        if raw is None:
            return None
        data = json.loads(raw)
        return LoginChallenge(
            challenge_token=challenge_token,
            admin_id=data["adminId"],
            email=data["email"],
            cognito_session=data["cognitoSession"],
            expires_at=0,  # not needed by callers — Redis's own key TTL is authoritative
        )

    def consume(self, challenge_token: str) -> None:
        try:
            self._redis.delete(f"{_KEY_PREFIX}{challenge_token}")
        except RedisError as exc:
            logger.warning(
                "admin_challenge_store.consume failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
