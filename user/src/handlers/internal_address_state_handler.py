"""GET /v1/internal/users/address-state?cognitoSub= — thin Lambda
entrypoint (MA-96 impl plan §4A).

Service-to-service call for Cart Service (and any future caller) to
resolve a user's default-address state without re-authenticating as
that user — not JWT-authenticated the way /users/me is.

**Auth boundary: AWS_IAM (SigV4) on the route, not network isolation.**
An earlier revision of this handler and its impl plan claimed "network
isolation (this route is never exposed outside the VPC)" as the
boundary, matching Inventory's public-vs-internal split
(serviceability_check_handler.py vs
internal_serviceability_check_handler.py) — that reasoning doesn't
transfer here: Inventory's isolation comes from being a Fargate service
behind a private ALB, never registered on API Gateway at all, whereas
this service is Lambda + apigwv2.HttpApi, a standard public regional
endpoint with no VPC boundary of its own. Left as documented in the
impl plan, this route would have been public and unauthenticated.

The actual mechanism (see user_stack.py's `_build_http_api`): the CDK
route for this handler uses `HttpIamAuthorizer`, so API Gateway itself
rejects any request that isn't a validly SigV4-signed call from a
principal explicitly granted `execute-api:Invoke` on this route's ARN —
by default, no principal is granted, so the route is effectively closed
until a caller (e.g. Cart Service's own Lambda execution role, once
MA-96's CDK stack exists) is added via `internal_caller_role_arns`.
Nothing in this handler itself needs to change to enforce that — the
authorizer runs before Lambda is ever invoked, same as the Cognito JWT
authorizer on every public route in this service.

Local dev: this local Lambda shim (services/local-dev/
_lambda_local_server.py) doesn't emulate IAM/SigV4 at all — every route
is reachable directly from localhost, matching how it also doesn't
verify the Cognito JWT authorizer's signature on public routes. Not a
stand-in for the real authorizer in either case.

Query parameter, not a path parameter, matching Inventory's own
internal endpoint's convention (`?pincode=&lat=&lng=`) and this
codebase's local Lambda shim, which only matches routes by exact
literal path (services/local-dev/_lambda_local_server.py).
"""

import logging
import uuid

from config.env import get_settings
from domain.exceptions import UserServiceError, ValidationError
from handlers.composition import build_registration_service
from handlers.dto import error_response, success_response

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    _deps = {"registration_service": build_registration_service(settings)}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))
    deps["registration_service"].set_correlation_id(correlation_id)

    cognito_sub = (event.get("queryStringParameters") or {}).get("cognitoSub")
    if not cognito_sub:
        return error_response(ValidationError("cognitoSub query parameter is required"))

    try:
        profile = deps["registration_service"].get_my_profile(cognito_sub)
        return success_response({"defaultAddressState": profile.default_address_state})
    except UserServiceError as exc:
        logger.info(
            "internal_address_state rejected",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return error_response(exc)
    except Exception:
        logger.exception(
            "internal_address_state: unexpected error", extra={"correlationId": correlation_id}
        )
        return error_response(UserServiceError("An unexpected error occurred"))
