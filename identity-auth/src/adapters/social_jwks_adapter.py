"""Google/Apple JWKS validation adapter.

Per services/README.md §3.7: the only place allowed to import requests/jwt
for this concern, with retry/timeout/typed-error mapping. JWKS documents
are cached per-provider (TTL) across warm invocations to avoid a network
round trip on every social-auth request.
"""

import json
import logging

import jwt
import requests
from cachetools import TTLCache

from domain.exceptions import ExternalServiceUnavailableError, InvalidSocialTokenError

logger = logging.getLogger(__name__)

_EXPECTED_ISSUERS = {
    "google": ("https://accounts.google.com", "accounts.google.com"),
    "apple": ("https://appleid.apple.com",),
}


class SocialJwksAdapter:
    def __init__(
        self,
        google_client_id: str,
        apple_client_id: str,
        google_jwks_url: str,
        apple_jwks_url: str,
        cache_ttl_seconds: int = 3600,
        request_timeout_seconds: float = 5.0,
        correlation_id: str = "",
    ) -> None:
        self._client_ids = {"google": google_client_id, "apple": apple_client_id}
        self._jwks_urls = {"google": google_jwks_url, "apple": apple_jwks_url}
        self._request_timeout_seconds = request_timeout_seconds
        self._correlation_id = correlation_id
        self._cache: TTLCache = TTLCache(maxsize=8, ttl=cache_ttl_seconds)

    def verify(self, provider: str, id_token: str) -> dict:
        jwks = self._get_jwks(provider)

        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
        except jwt.PyJWTError as exc:
            logger.info(
                "social_jwks.verify rejected token",
                extra={
                    "correlationId": self._correlation_id,
                    "provider": provider,
                    "error": str(exc),
                },
            )
            raise InvalidSocialTokenError(f"Invalid {provider} idToken") from exc

        signing_key = self._find_key(jwks, kid)
        if signing_key is None:
            # The cached JWKS may be stale relative to a real key rotation
            # on the provider's side — force one refetch before concluding
            # the token is actually invalid, rather than rejecting valid
            # tokens for up to `cache_ttl_seconds` after every rotation.
            jwks = self._get_jwks(provider, force_refresh=True)
            signing_key = self._find_key(jwks, kid)
        if signing_key is None:
            raise InvalidSocialTokenError("Signing key not found in JWKS")

        try:
            claims = jwt.decode(
                id_token,
                key=signing_key,
                algorithms=["RS256"],
                audience=self._client_ids[provider],
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            logger.info(
                "social_jwks.verify rejected token",
                extra={
                    "correlationId": self._correlation_id,
                    "provider": provider,
                    "error": str(exc),
                },
            )
            raise InvalidSocialTokenError(f"Invalid {provider} idToken") from exc

        if claims.get("iss") not in _EXPECTED_ISSUERS[provider]:
            raise InvalidSocialTokenError(f"Unexpected issuer for {provider} idToken")

        return claims

    def _get_jwks(self, provider: str, force_refresh: bool = False) -> dict:
        cached = None if force_refresh else self._cache.get(provider)
        if cached is not None:
            return cached

        try:
            response = requests.get(self._jwks_urls[provider], timeout=self._request_timeout_seconds)
            response.raise_for_status()
            jwks = response.json()
        except requests.RequestException as exc:
            logger.error(
                "social_jwks.fetch failed",
                extra={"correlationId": self._correlation_id, "provider": provider, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError(f"Failed to fetch {provider} JWKS") from exc

        self._cache[provider] = jwks
        return jwks

    def _find_key(self, jwks: dict, kid: str | None):
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
        return None
