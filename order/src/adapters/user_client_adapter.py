"""HTTP client for User Service's internal address-state endpoint
(`GET /v1/internal/users/address-state?cognitoSub=`,
`services/user/src/handlers/internal_address_state_handler.py`) — the
same internal call Cart Service already makes for the identical need
(`cart/src/adapters/user_client_adapter.py`), mirrored here including its
SigV4 signing.

**This call must be SigV4-signed** — unlike every other adapter in this
service (Pricing, Wallet), this specific route is protected by
`HttpIamAuthorizer` (AWS_IAM). A plain unsigned `requests.get()` gets a
403 from API Gateway before User's handler ever runs. `_sign_request`
signs using this service's own execution role credentials (resolved via
`boto3.Session()`'s default credential chain — no explicit key material
handled here).

Correlation-id header: sent as `x-request-id`, matching what
`internal_address_state_handler.py` actually reads — same fix cart's own
adapter already applies (its docstring flags `x-correlation-id` as a
pre-existing bug in catalog/inventory's own adapters elsewhere)."""

import logging

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from requests.exceptions import RequestException
from shared.adapters.retry import call_with_retry

from domain.exceptions import AddressLookupUnavailableError

logger = logging.getLogger(__name__)


class _RetryableUserError(Exception):
    pass


class HttpUserClient:
    def __init__(
        self,
        base_url: str,
        region_name: str,
        timeout_seconds: float = 3.0,
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

    def get_delivery_address_state(self, cognito_sub: str) -> str | None:
        url = f"{self._base_url}/v1/internal/users/address-state"
        params = {"cognitoSub": cognito_sub}

        def _attempt() -> str | None:
            try:
                auth_headers = self._sign_request(url, params)
            except Exception as exc:
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
                    raise _RetryableUserError(
                        f"malformed 200 body from User service: {exc}"
                    ) from exc
            if response.status_code == 404:
                # No profile for this cognito_sub — a definite fact, not
                # unavailability. materialize() treats None the same as
                # "no default address set": PAYMENT_FAILED/DELIVERY_ADDRESS_UNKNOWN.
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
