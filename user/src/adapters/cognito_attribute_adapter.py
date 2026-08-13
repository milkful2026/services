"""AdminUpdateUserAttributes for `name` / `custom:default_pincode`
(spec §4 FR-1 step 4). User Service's own narrow, single-purpose Cognito
adapter — services/README.md §3.7's "only place" restriction is
per-service, not repo-wide (same note as MA-92's cognito_adapter.py).

**Cross-stack schema dependency, flagged not fixed here**:
`custom:default_pincode` must exist in the Cognito User Pool's schema —
custom attributes can only be added at pool *creation*, never after. That
pool is provisioned by MA-92's `identity_auth_stack.py`, which does not
currently define this attribute. This adapter is correct against the
*intended* schema, but will fail in a real deployment until MA-92's stack
is updated — a cross-PR follow-up for a human, not something this PR can
or should silently patch into someone else's already-merged stack.

Looked up by `sub` (a real, filterable standard ListUsers attribute —
unlike custom attributes, see MA-92's cognito_adapter.py for that
distinction) since this service only has the JWT's `sub` claim, not the
Cognito Username (phone number).
"""

import logging
import re

import boto3
from botocore.exceptions import ClientError

from adapters.retry import call_with_retry
from domain.exceptions import ExternalServiceUnavailableError

logger = logging.getLogger(__name__)

# Cognito's own `sub` is always a UUID it assigns itself (even for
# federated users — the upstream IdP's subject claim is never used as
# Cognito's sub). Enforcing that shape here means a value that could
# break out of the ListUsers Filter string below is rejected outright,
# rather than attempting to escape an underspecified filter grammar.
_SUB_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class CognitoAttributeAdapter:
    def __init__(
        self,
        user_pool_id: str,
        region_name: str,
        correlation_id: str = "",
        max_retries: int = 2,
        backoff_base_seconds: float = 0.2,
        endpoint_url: str | None = None,
    ) -> None:
        self._client = boto3.client("cognito-idp", region_name=region_name, endpoint_url=endpoint_url)
        self._user_pool_id = user_pool_id
        self._correlation_id = correlation_id
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds

    def set_correlation_id(self, correlation_id: str) -> None:
        self._correlation_id = correlation_id

    def sync_profile_attributes(self, cognito_sub: str, name: str, default_pincode: str) -> None:
        if not _SUB_PATTERN.match(cognito_sub):
            logger.error(
                "cognito_attribute_adapter: cognito_sub is not UUID-shaped, refusing to filter",
                extra={"correlationId": self._correlation_id},
            )
            raise ExternalServiceUnavailableError("Cognito sub has an unexpected shape")

        def _on_attempt_failure(exc: Exception, attempt: int) -> None:
            logger.error(
                "cognito_attribute_adapter request failed, retrying",
                extra={
                    "correlationId": self._correlation_id,
                    "attempt": attempt,
                    "error": str(exc),
                },
            )

        try:
            response = call_with_retry(
                lambda: self._client.list_users(
                    UserPoolId=self._user_pool_id, Filter=f'sub = "{cognito_sub}"', Limit=1
                ),
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(ClientError,),
                on_attempt_failure=_on_attempt_failure,
            )
        except ClientError as exc:
            raise self._wrap("list_users", exc) from exc

        users = response.get("Users", [])
        if not users:
            logger.error(
                "cognito_attribute_adapter: no Cognito user found for sub",
                extra={"correlationId": self._correlation_id, "cognitoSub": cognito_sub},
            )
            return

        username = users[0]["Username"]
        try:
            call_with_retry(
                lambda: self._client.admin_update_user_attributes(
                    UserPoolId=self._user_pool_id,
                    Username=username,
                    UserAttributes=[
                        {"Name": "name", "Value": name},
                        {"Name": "custom:default_pincode", "Value": default_pincode},
                    ],
                ),
                max_retries=self._max_retries,
                backoff_base_seconds=self._backoff_base_seconds,
                retryable_exceptions=(ClientError,),
                on_attempt_failure=_on_attempt_failure,
            )
        except ClientError as exc:
            raise self._wrap("admin_update_user_attributes", exc) from exc

    def _wrap(self, operation: str, exc: ClientError) -> ExternalServiceUnavailableError:
        logger.error(
            f"cognito_attribute_adapter.{operation} failed",
            extra={"correlationId": self._correlation_id, "error": str(exc)},
        )
        return ExternalServiceUnavailableError(f"Cognito {operation} failed")
