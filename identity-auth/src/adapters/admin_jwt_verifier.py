"""Verifies Cognito Admin Pool ACCESS tokens for the custom API Gateway
authorizer (spec FR-5 / §6.4).

Same JWKS-fetch-with-cache-and-force-refresh shape as
social_jwks_adapter.py, adapted for Cognito's own JWKS endpoint instead
of Google/Apple's. Cognito ACCESS tokens (unlike ID tokens) carry no
`aud` claim — the app client is instead verified via the token's own
`client_id` claim, per AWS's documented Cognito JWT verification
guidance.

Per services/README.md §3.7: the only place allowed to import
requests/jwt for this concern.
"""

import json
import logging

import jwt
import requests
from cachetools import TTLCache

from domain.admin_exceptions import AdminAuthenticationError
from domain.exceptions import ExternalServiceUnavailableError

logger = logging.getLogger(__name__)


class AdminJwtVerifierAdapter:
    def __init__(
        self,
        user_pool_id: str,
        client_id: str,
        region_name: str,
        cache_ttl_seconds: int = 3600,
        request_timeout_seconds: float = 5.0,
        correlation_id: str = "",
    ) -> None:
        self._user_pool_id = user_pool_id
        self._client_id = client_id
        self._issuer = f"https://cognito-idp.{region_name}.amazonaws.com/{user_pool_id}"
        self._jwks_url = f"{self._issuer}/.well-known/jwks.json"
        self._request_timeout_seconds = request_timeout_seconds
        self._correlation_id = correlation_id
        self._cache: TTLCache = TTLCache(maxsize=1, ttl=cache_ttl_seconds)

    def verify_access_token(self, token: str) -> dict:
        # Parsed before any JWKS fetch — a malformed token is a cheap,
        # purely local rejection that shouldn't cost a network round trip
        # (or a cold-cache fetch) first.
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as exc:
            raise AdminAuthenticationError("Malformed access token") from exc

        jwks = self._get_jwks()
        signing_key = self._find_key(jwks, kid)
        if signing_key is None:
            jwks = self._get_jwks(force_refresh=True)
            signing_key = self._find_key(jwks, kid)
        if signing_key is None:
            raise AdminAuthenticationError("Signing key not found in JWKS")

        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=["RS256"],
                issuer=self._issuer,
                options={"require": ["exp", "iss", "sub"], "verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            logger.info(
                "admin_jwt_verifier.verify_access_token rejected token",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise AdminAuthenticationError("Invalid or expired access token") from exc

        if claims.get("token_use") != "access":
            raise AdminAuthenticationError("Token is not an access token")
        if claims.get("client_id") != self._client_id:
            raise AdminAuthenticationError("Token was not issued for this app client")

        return claims

    def _get_jwks(self, force_refresh: bool = False) -> dict:
        cached = None if force_refresh else self._cache.get("jwks")
        if cached is not None:
            return cached

        try:
            response = requests.get(self._jwks_url, timeout=self._request_timeout_seconds)
            response.raise_for_status()
            jwks = response.json()
        except requests.RequestException as exc:
            logger.error(
                "admin_jwt_verifier.fetch failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to fetch Admin Pool JWKS") from exc

        self._cache["jwks"] = jwks
        return jwks

    def _find_key(self, jwks: dict, kid: str | None):
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
        return None
