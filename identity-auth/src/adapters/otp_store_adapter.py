"""DynamoDB adapter for the `otp_requests` table.

Per services/README.md §3.7: the only place allowed to import boto3 for
this concern. Domain code depends on adapters.interfaces.OtpStorePort only.
"""

import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

from domain.exceptions import ExternalServiceUnavailableError
from domain.models import OtpRecord, OtpStatus

logger = logging.getLogger(__name__)


def _to_item(record: OtpRecord) -> dict[str, Any]:
    return {
        "requestId": record.request_id,
        "mobile": record.mobile,
        "otpHash": record.otp_hash,
        "attempts": record.attempts,
        "status": record.status.value,
        "ttl": record.ttl,
        "lastSentAt": record.last_sent_at,
        "purpose": record.purpose,
    }


def _from_item(item: dict[str, Any]) -> OtpRecord:
    return OtpRecord(
        request_id=item["requestId"],
        mobile=item["mobile"],
        otp_hash=item["otpHash"],
        attempts=int(item["attempts"]),
        status=OtpStatus(item["status"]),
        ttl=int(item["ttl"]),
        last_sent_at=int(item["lastSentAt"]),
        purpose=item.get("purpose", "REGISTER"),
    )


class DynamoDbOtpStoreAdapter:
    def __init__(
        self,
        table_name: str,
        region_name: str,
        correlation_id: str = "",
        endpoint_url: str | None = None,
    ) -> None:
        self._table = boto3.resource(
            "dynamodb", region_name=region_name, endpoint_url=endpoint_url
        ).Table(table_name)
        self._correlation_id = correlation_id

    def put(self, record: OtpRecord) -> None:
        try:
            self._table.put_item(Item=_to_item(record))
        except ClientError as exc:
            logger.error(
                "otp_store.put failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to persist OTP request") from exc

    def get(self, request_id: str) -> OtpRecord | None:
        try:
            response = self._table.get_item(Key={"requestId": request_id})
        except ClientError as exc:
            logger.error(
                "otp_store.get failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to read OTP request") from exc
        item = response.get("Item")
        return _from_item(item) if item else None

    def get_active_by_mobile(self, mobile: str, purpose: str) -> OtpRecord | None:
        try:
            response = self._table.query(
                IndexName="mobile-index",
                KeyConditionExpression="mobile = :m",
                FilterExpression="#s = :active",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":m": mobile, ":active": OtpStatus.ACTIVE.value},
            )
        except ClientError as exc:
            logger.error(
                "otp_store.get_active_by_mobile failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to query OTP requests") from exc
        items = response.get("Items", [])
        # purpose is filtered here (not in the DynamoDB FilterExpression)
        # so the same "absent purpose == REGISTER" default used by
        # _from_item also governs matching, rather than duplicating that
        # default logic as a second, harder-to-read DynamoDB expression.
        items = [i for i in items if i.get("purpose", "REGISTER") == purpose]
        if not items:
            return None
        # Defensive: should be at most one ACTIVE record per (mobile,
        # purpose) by design (resend reuses the record in place); if more
        # than one somehow exists, prefer the most recently sent.
        most_recent = max(items, key=lambda i: int(i["lastSentAt"]))
        return _from_item(most_recent)

    def increment_attempts(self, request_id: str) -> int:
        try:
            response = self._table.update_item(
                Key={"requestId": request_id},
                UpdateExpression="SET attempts = attempts + :one",
                ExpressionAttributeValues={":one": 1},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            logger.error(
                "otp_store.increment_attempts failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to update OTP attempts") from exc
        return int(response["Attributes"]["attempts"])

    def mark_status(self, request_id: str, status: str) -> None:
        try:
            self._table.update_item(
                Key={"requestId": request_id},
                UpdateExpression="SET #s = :status",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":status": status},
            )
        except ClientError as exc:
            logger.error(
                "otp_store.mark_status failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise ExternalServiceUnavailableError("Failed to update OTP status") from exc
