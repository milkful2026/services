"""HTTP client for User Service's internal address-state endpoint
(`GET /v1/internal/users/address-state?cognitoSub=`,
`services/user/src/handlers/internal_address_state_handler.py`).

**This call must be SigV4-signed** — unlike every other adapter in this
codebase (Catalog, Pricing, and User/Inventory's own precedent), this
specific route is protected by `HttpIamAuthorizer` (AWS_IAM), not left
unauthenticated (see `user_stack.py`'s docstring point 7 for why
"network isolation" was rejected as the boundary). A plain unsigned
`requests.get()` — the pattern every other adapter here uses — would
get a 403 from API Gateway before User's handler ever runs; it isn't a
bug in the request itself. `_sign_request` signs using this Lambda's
own execution role credentials (resolved via `boto3.Session()`'s
default credential chain — no explicit key material handled here),
service `execute-api`, the same mechanism `user_stack.py`'s
`internal_caller_role_arns` grants access for.

Correlation-id header note: sent as `x-request-id`, matching what
`internal_address_state_handler.py` actually reads
(`event["headers"]["x-request-id"]`) — not `x-correlation-id`, which is
what `catalog_client_adapter.py`/`inventory_client_adapter.py` send
despite every receiving handler in this codebase reading
`x-request-id`. That mismatch looks like a pre-existing bug in those
two adapters (correlation IDs silently never propagate), not something
to copy here — flagged, not fixed in this pass since those files belong
to a different story.
"""

import logging

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from requests.exceptions import RequestException

from adapters.retry import call_with_retry
from domain.exceptions import AddressLookupUnavailableError

logger = logging.getLogger(__name__)


class _RetryableUserError(Exception):
    pass


class HttpUserClient:
    def __init__(
        self,
        base_url: str,
        region_name: str,
        timeout_seconds: float,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.2,
        correlation_id: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._region_name = region_name
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._correlation_id = correlation_id

    def set_correlation_id(self, correlation_id: str) -> None:
        self._correlation_id = correlation_id

    def get_delivery_address_state(self, cognito_sub: str) -> str | None:
        url = f"{self._base_url}/v1/internal/users/address-state"
        params = {"cognitoSub": cognito_sub}

        def _attempt() -> str | None:
            try:
                auth_headers = self._sign_request(url, params)
            except Exception as exc:
                # Credential resolution failure (e.g. no execution role
                # attached, moto/local dev without any creds at all) is
                # not a network error, but it means this call can never
                # succeed as configured — treated the same as any other
                # attempt failure so it still goes through retry/backoff
                # and the standard typed-exception mapping below, rather
                # than raising an unmapped exception straight out of this
                # method.
                raise _RetryableUserError(f"failed to sign request: {exc}") from exc

            try:
                response = requests.get(
                    url,
                    params=params,
                    timeout=self._timeout_seconds,
                    headers={**auth_headers, "x-request-id": self._correlation_id},
                )
            except RequestException as exc:
                raise _RetryableUserError(str(exc)) from exc

            if response.status_code == 200:
                try:
                    return response.json()["data"].get("defaultAddressState")
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    # A 200 whose body isn't the documented envelope (a
                    # non-JSON API Gateway/proxy/WAF interstitial, or a
                    # payload missing "data") is a transport-layer failure
                    # from this adapter's contract's point of view, not a
                    # value to propagate raw — retried, then surfaced as
                    # AddressLookupUnavailableError. Same handling
                    # catalog_client_adapter.py already applies.
                    raise _RetryableUserError(
                        f"malformed 200 body from User service: {exc}"
                    ) from exc
            if response.status_code == 404:
                # No profile found for this cognito_sub in User Service —
                # treated the same as "no default address set" (None);
                # cart_service.py's own DeliveryAddressRequiredError
                # covers both cases identically from Cart's perspective.
                return None
            raise _RetryableUserError(f"User service returned HTTP {response.status_code}")

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "user_client.get_delivery_address_state request failed",
                extra={
                    "correlationId": self._correlation_id,
                    "attempt": attempt,
                    "error": str(exc),
                },
            )

        try:
            return call_with_retry(
                _attempt,
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(_RetryableUserError,),
                on_attempt_failure=_on_attempt_failure,
            )
        except _RetryableUserError as exc:
            raise AddressLookupUnavailableError(
                "User service address-state lookup failed after retries",
                details={"cause": str(exc)},
            ) from exc

    def _sign_request(self, url: str, params: dict[str, str]) -> dict[str, str]:
        credentials = boto3.Session().get_credentials()
        if credentials is None:
            raise RuntimeError("no AWS credentials available to sign the request")
        request = AWSRequest(method="GET", url=url, params=params)
        SigV4Auth(credentials, "execute-api", self._region_name).add_auth(request)
        return dict(request.headers)
