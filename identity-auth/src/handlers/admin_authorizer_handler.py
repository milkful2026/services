"""API Gateway HttpApi Lambda authorizer (REQUEST, simple response
format) protecting every `/v1/admin/*` route EXCEPT the two pre-auth
login endpoints (spec FR-5 / §6 point 4).

Authorizer result caching is disabled in the CDK stack (TTL=0) per spec
§11.2's own explicit recommendation — every request re-checks Aurora's
live status/role/ip_allowlist here rather than trusting a cached
decision, which is how this satisfies the NFR that role/deactivation/
IP-allowlist changes take effect "within one authorizer cache TTL": that
TTL is zero.

A JWT-verification failure (including a JWKS-fetch outage) is mapped to
a plain deny (403) rather than distinguishing 401 vs 502 — a Lambda
REQUEST authorizer's simple-response format has no field to customize
the denied status code, so a finer-grained mapping isn't available here
the way it is for regular handlers (services/README.md §5c's error
table). A live Aurora failure during the repository lookup is
deliberately left unguarded so it propagates as an authorizer error
(API Gateway surfaces this as 500) instead of being silently mapped to
"unauthorized" — the two failure modes are handled differently on
purpose, and this asymmetry is called out in this service's README.
"""

import ipaddress
import logging
import uuid

from sqlalchemy import create_engine

from adapters.admin_jwt_verifier import AdminJwtVerifierAdapter
from adapters.admin_user_repository import SqlAlchemyAdminUserRepository
from adapters.notification_publisher import EventBridgeNotificationPublisher
from config.env import get_settings
from domain.admin_models import AdminStatus
from domain.exceptions import IdentityAuthError

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    verifier = AdminJwtVerifierAdapter(
        settings.admin_cognito_user_pool_id, settings.admin_cognito_client_id, settings.aws_region
    )
    engine = create_engine(settings.admin_database_url)
    repo = SqlAlchemyAdminUserRepository(engine)
    publisher = EventBridgeNotificationPublisher(
        settings.event_bus_name, settings.event_source, settings.aws_region
    )
    _deps = {"verifier": verifier, "repo": repo, "publisher": publisher}
    return _deps


def _deny() -> dict:
    return {"isAuthorized": False, "context": {}}


def _allow(admin) -> dict:
    return {
        "isAuthorized": True,
        "context": {"adminId": admin.id, "email": admin.email, "role": admin.role.value},
    }


def _extract_bearer_token(event: dict) -> str | None:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth_header = headers.get("authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        return None
    return auth_header[len("bearer ") :].strip() or None


def _extract_source_ip(event: dict) -> str | None:
    request_context = event.get("requestContext") or {}
    return (request_context.get("http") or {}).get("sourceIp") or (
        request_context.get("identity") or {}
    ).get("sourceIp")


def _ip_allowed(source_ip: str | None, allowlist: list[str]) -> bool:
    if not allowlist:
        return True
    if not source_ip:
        return False
    try:
        addr = ipaddress.ip_address(source_ip)
    except ValueError:
        return False
    for cidr in allowlist:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = (event.get("headers") or {}).get("x-request-id", str(uuid.uuid4()))

    token = _extract_bearer_token(event)
    if token is None:
        return _deny()

    try:
        claims = deps["verifier"].verify_access_token(token)
    except IdentityAuthError as exc:
        logger.info(
            "admin_authorizer rejected token",
            extra={"correlationId": correlation_id, "errorCode": exc.error_code},
        )
        return _deny()

    admin = deps["repo"].get_by_cognito_sub(claims["sub"])
    if admin is None or admin.status != AdminStatus.ACTIVE:
        return _deny()

    source_ip = _extract_source_ip(event)
    if not _ip_allowed(source_ip, admin.ip_allowlist):
        deps["publisher"].publish_admin_event(
            "admin.session.blocked",
            {"adminId": admin.id, "email": admin.email, "reason": "ip_not_allowed", "sourceIp": source_ip},
            correlation_id,
        )
        return _deny()

    return _allow(admin)
