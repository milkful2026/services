"""Response envelope + serialization helpers. Fixed envelope shape per
services/README.md §5."""

from typing import Any

from shared.handlers.dto import error_envelope, success_envelope  # noqa: F401

from domain.models import ServiceabilityResult
from domain.serviceability_service import result_to_dict


def serialize_result(result: ServiceabilityResult) -> dict[str, Any]:
    return result_to_dict(result)
