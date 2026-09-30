"""Scheduled Lambda (MA-139 §4 FR-7 / §6 point 4): once daily, auto-lifts
every `Suspended` account whose `suspended_until` has passed — sets
`status = Active`, clears `suspended_until`, calls Cognito
`AdminEnableUser`, writes a `user_status_history` row with
`actor = "system:suspension-sweep"`, and publishes `user.status.changed`.

Mirrors outbox_publisher_handler.py's own shape (a scheduled, no-HTTP-
event Lambda triggered by an EventBridge Scheduler rule in prod — see
infra/user/user_stack.py's `_build_suspension_sweep_scheduler`) — kept
deliberately small per the implementation task's own note not to
over-engineer this FR: a plain daily sweep, no new queue, no new table.
"""

import logging
import uuid

from sqlalchemy import create_engine

from adapters.cognito_attribute_adapter import CognitoAttributeAdapter
from adapters.user_repository import SqlAlchemyUserRepository
from config.env import get_settings
from domain.customer_status_service import CustomerStatusService

logger = logging.getLogger(__name__)

_deps: dict | None = None


def _get_deps() -> dict:
    global _deps
    if _deps is not None:
        return _deps

    settings = get_settings()
    engine = create_engine(settings.database_url)
    repository = SqlAlchemyUserRepository(engine)
    cognito_attributes = CognitoAttributeAdapter(settings.cognito_user_pool_id, settings.aws_region)
    service = CustomerStatusService(repository, cognito_attributes)
    _deps = {"service": service}
    return _deps


def handler(event: dict, context) -> dict:
    deps = _get_deps()
    correlation_id = str(uuid.uuid4())
    deps["service"].set_correlation_id(correlation_id)

    lifted_count = deps["service"].run_suspension_sweep()
    logger.info(
        "suspension_sweep_handler: run complete",
        extra={"correlationId": correlation_id, "liftedCount": lifted_count},
    )
    return {"liftedCount": lifted_count}
