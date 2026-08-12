"""HTTP client for Inventory's internal serviceability endpoint (spec §8).

Per services/README.md §3.7: the only place allowed to import `requests`
for this concern, with retry/backoff, configurable timeout, and typed
error mapping. Reaches Inventory by URL (env var) — whether that URL is
actually network-reachable from this service's Lambda is a separate,
flagged infrastructure gap (see README) since each service currently
provisions its own dedicated VPC.
"""

import logging
import time

import requests
from requests.exceptions import RequestException

from domain.exceptions import ExternalServiceUnavailableError, ValidationError

logger = logging.getLogger(__name__)


class HttpInventoryClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.2,
        correlation_id: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._correlation_id = correlation_id

    def check_serviceability(self, pincode: str, lat: float, lng: float) -> bool:
        url = f"{self._base_url}/v1/internal/serviceability/check"
        params = {"pincode": pincode, "lat": lat, "lng": lng}

        last_cause: str | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = requests.get(
                    url,
                    params=params,
                    timeout=self._timeout_seconds,
                    headers={"x-correlation-id": self._correlation_id},
                )
            except RequestException as exc:
                last_cause = str(exc)
                logger.error(
                    "inventory_client.check_serviceability request failed",
                    extra={"correlationId": self._correlation_id, "attempt": attempt, "error": last_cause},
                )
            else:
                if response.status_code == 200:
                    return bool(response.json()["data"]["serviceable"])
                if response.status_code == 400:
                    raise ValidationError(
                        "Inventory rejected pincode/coordinates",
                        details=response.json().get("data", {}),
                    )
                last_cause = f"Inventory returned HTTP {response.status_code}"
                logger.error(
                    "inventory_client.check_serviceability non-200 response",
                    extra={"correlationId": self._correlation_id, "status": response.status_code},
                )

            if attempt < self._max_retries:
                time.sleep(self._backoff_base_seconds * (2**attempt))

        raise ExternalServiceUnavailableError(
            "Inventory serviceability check failed after retries", details={"cause": last_cause}
        )
