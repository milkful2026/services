"""DynamoDB adapter for the single-table `cart` design (MA-121 §7, as
merged). Per services/README.md §3.7: the only place allowed to import
boto3 for this concern. Domain code depends on
`adapters.interfaces.CartRepositoryPort` only.

Schema (single table, `SK` prefix distinguishes item shape):
    PK: userId (S)
    SK: SK (S)   -- "ITEM#{uuid}" | "META" | "IDEMPOTENCY#{key}" | "OUTBOX#{eventId}"

    ITEM# rows:       productId, quantity, frequency, startDate (nullable),
                       addedAt, expiresAt (TTL, 30 days from last write)
    META row:         cartVersion (monotonic, FR-1/FR-3), expiresAt
    IDEMPOTENCY# rows: responseBody (serialized LineItem), expiresAt (TTL, 24h)
    OUTBOX# rows:     type, payload, publishedAt (absent = unpublished)

**`META`'s own TTL (open item #6 from specs PR #11's review, resolved
here):** the original plan gave `ITEM#`/`IDEMPOTENCY#` rows a TTL but
left `META`'s `cartVersion` permanent, so a fully-expired cart could
still report a stale non-zero `cartVersion` — inconsistent with FR-1's
"no cart and empty cart are the same state." Fixed by refreshing
`META`'s own `expiresAt` to the same 30-day window on every mutation,
identical to `ITEM#` rows — once everything in a cart has expired
(including `META`), `get_cart` simply finds no rows at all and returns
`Cart(line_items=[], cart_version=0)`, same as a cart that never
existed.

Reads (`get_cart`) use the higher-level `boto3.resource("dynamodb")`
`Table` API for its Python-native item (de)serialization; writes that
need atomicity (`add_item`/`replace_cart`/`delete_item`, all of which
must also write an `OUTBOX#` row in the same transaction) use the
low-level `boto3.client("dynamodb").transact_write_items`, since
`transact_write_items` has no resource-level equivalent — `TypeSerializer`/
`TypeDeserializer` convert between the two representations. Mixing both
boto3 APIs on the same table is a normal, supported pattern.
"""

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from domain.exceptions import CartServiceError, CartVersionMismatchError, LineItemNotFoundError
from domain.models import Cart, Frequency, LineItem

logger = logging.getLogger(__name__)

_ITEM_PREFIX = "ITEM#"
_META_SK = "META"
_IDEMPOTENCY_PREFIX = "IDEMPOTENCY#"
_OUTBOX_PREFIX = "OUTBOX#"

_CART_TTL_SECONDS = 30 * 24 * 3600
_IDEMPOTENCY_TTL_SECONDS = 24 * 3600

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _now_epoch() -> int:
    return int(time.time())


def _cart_expires_at() -> int:
    return _now_epoch() + _CART_TTL_SECONDS


def _item_to_line_item(item: dict[str, Any]) -> LineItem:
    return LineItem(
        id=item["SK"].removeprefix(_ITEM_PREFIX),
        product_id=item["productId"],
        quantity=int(item["quantity"]),
        frequency=Frequency(item["frequency"]),
        start_date=item.get("startDate"),
        added_at=item["addedAt"],
    )


def _line_item_to_response_dict(line_item: LineItem) -> dict[str, Any]:
    # What add_item returns, and what an idempotent replay must reproduce
    # exactly — serialized once here so both the first response and every
    # replay use the identical shape.
    return {
        "id": line_item.id,
        "productId": line_item.product_id,
        "quantity": line_item.quantity,
        "frequency": str(line_item.frequency),
        "startDate": line_item.start_date,
        "addedAt": line_item.added_at,
    }


def _response_dict_to_line_item(data: dict[str, Any]) -> LineItem:
    return LineItem(
        id=data["id"],
        product_id=data["productId"],
        quantity=data["quantity"],
        frequency=Frequency(data["frequency"]),
        start_date=data["startDate"],
        added_at=data["addedAt"],
    )


def _to_av(value: Any) -> dict[str, Any]:
    return _serializer.serialize(value)


class DynamoDbCartRepository:
    def __init__(
        self,
        table_name: str,
        region_name: str,
        event_source: str,
        correlation_id: str = "",
    ) -> None:
        self._table_name = table_name
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)
        self._client = boto3.client("dynamodb", region_name=region_name)
        self._event_source = event_source
        self._correlation_id = correlation_id

    def set_correlation_id(self, correlation_id: str) -> None:
        self._correlation_id = correlation_id

    # -- reads --------------------------------------------------------

    def get_cart(self, user_id: str) -> Cart:
        try:
            response = self._table.query(
                KeyConditionExpression="userId = :uid",
                ExpressionAttributeValues={":uid": user_id},
            )
        except ClientError as exc:
            logger.error(
                "cart_repository.get_cart failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise CartServiceError("Failed to read cart") from exc

        line_items: list[LineItem] = []
        cart_version = 0
        for item in response.get("Items", []):
            sk = item["SK"]
            if sk.startswith(_ITEM_PREFIX):
                line_items.append(_item_to_line_item(item))
            elif sk == _META_SK:
                cart_version = int(item.get("cartVersion", 0))
            # IDEMPOTENCY#/OUTBOX# rows share the partition but aren't
            # part of the cart's own contents — ignored here.
        return Cart(line_items=line_items, cart_version=cart_version)

    # -- writes ---------------------------------------------------------

    def add_item(
        self,
        user_id: str,
        product_id: str,
        quantity: int,
        frequency: Frequency,
        start_date: str | None,
        idempotency_key: str | None,
    ) -> LineItem:
        if idempotency_key:
            replay = self._find_idempotency_replay(user_id, idempotency_key)
            if replay is not None:
                return replay

        line_item_id = str(uuid.uuid4())
        added_at = datetime.now(UTC).isoformat()
        line_item = LineItem(
            id=line_item_id,
            product_id=product_id,
            quantity=quantity,
            frequency=frequency,
            start_date=start_date,
            added_at=added_at,
        )
        expires_at = _cart_expires_at()
        event_id = str(uuid.uuid4())

        transact_items = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": {k: _to_av(v) for k, v in {
                        "userId": user_id,
                        "SK": f"{_ITEM_PREFIX}{line_item_id}",
                        "productId": product_id,
                        "quantity": quantity,
                        "frequency": str(frequency),
                        "startDate": start_date,
                        "addedAt": added_at,
                        "expiresAt": expires_at,
                    }.items()},
                }
            },
            self._meta_upsert_transact_item(user_id, expires_at),
            self._outbox_put_transact_item(
                user_id, event_id, "ITEM_ADDED", cart_id=user_id
            ),
        ]
        # Inserted at a fixed index (1) when present, so the exception
        # handler below always knows exactly which transact item's
        # condition to check for a lost idempotency race — never derived
        # from len(transact_items), which shifts depending on whether
        # this branch ran.
        idempotency_item_index = 1 if idempotency_key else None
        if idempotency_key:
            transact_items.insert(idempotency_item_index, self._idempotency_put_transact_item(
                user_id, idempotency_key, _line_item_to_response_dict(line_item)
            ))

        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except self._client.exceptions.TransactionCanceledException as exc:
            if idempotency_key and self._condition_failed_for(exc, idempotency_item_index):
                # Lost a race against a concurrent identical request that
                # committed the IDEMPOTENCY# row first — re-read and
                # return *its* result, per FR-2's idempotency contract,
                # not a duplicate line item.
                replay = self._find_idempotency_replay(user_id, idempotency_key)
                if replay is not None:
                    return replay
            logger.error(
                "cart_repository.add_item failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise CartServiceError("Failed to add cart item") from exc
        except ClientError as exc:
            logger.error(
                "cart_repository.add_item failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise CartServiceError("Failed to add cart item") from exc

        return line_item

    def replace_cart(self, user_id: str, items: list[dict], if_version: int) -> Cart:
        current = self.get_cart(user_id)
        current_ids = {li.id for li in current.line_items}
        desired_ids = {item["id"] for item in items if item.get("id")}
        to_delete = current_ids - desired_ids
        expires_at = _cart_expires_at()
        event_id = str(uuid.uuid4())
        new_version = if_version + 1

        # No separate ConditionCheck row for the version guard: DynamoDB
        # rejects a transaction that targets the same item (userId, META)
        # twice, and the META row is also written below. The condition is
        # folded directly into that Put's own ConditionExpression instead
        # — meta_index is recorded so the exception handler can identify
        # *which* transact item's condition failed.
        transact_items: list[dict[str, Any]] = []
        for line_item_id in to_delete:
            transact_items.append(
                {
                    "Delete": {
                        "TableName": self._table_name,
                        "Key": {
                            "userId": _to_av(user_id),
                            "SK": _to_av(f"{_ITEM_PREFIX}{line_item_id}"),
                        },
                    }
                }
            )
        for item in items:
            line_item_id = item.get("id") or str(uuid.uuid4())
            transact_items.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": {k: _to_av(v) for k, v in {
                            "userId": user_id,
                            "SK": f"{_ITEM_PREFIX}{line_item_id}",
                            "productId": item["product_id"],
                            "quantity": item["quantity"],
                            "frequency": str(item["frequency"]),
                            "startDate": item.get("start_date"),
                            "addedAt": item.get("added_at") or datetime.now(UTC).isoformat(),
                            "expiresAt": expires_at,
                        }.items()},
                    }
                }
            )
        meta_index = len(transact_items)
        transact_items.append(
            self._meta_upsert_transact_item(
                user_id, expires_at, explicit_version=new_version, expected_version=if_version
            )
        )
        transact_items.append(
            self._outbox_put_transact_item(user_id, event_id, "CART_REPLACED", cart_id=user_id)
        )

        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except self._client.exceptions.TransactionCanceledException as exc:
            if self._condition_failed_for(exc, meta_index):
                raise CartVersionMismatchError(
                    "Cart was modified by another device — refetch and retry",
                    details={"expectedVersion": if_version},
                ) from exc
            logger.error(
                "cart_repository.replace_cart failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise CartServiceError("Failed to replace cart") from exc
        except ClientError as exc:
            logger.error(
                "cart_repository.replace_cart failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise CartServiceError("Failed to replace cart") from exc

        return self.get_cart(user_id)

    def delete_item(self, user_id: str, line_item_id: str) -> None:
        event_id = str(uuid.uuid4())
        transact_items = [
            {
                "Delete": {
                    "TableName": self._table_name,
                    "Key": {
                        "userId": _to_av(user_id),
                        "SK": _to_av(f"{_ITEM_PREFIX}{line_item_id}"),
                    },
                    "ConditionExpression": "attribute_exists(SK)",
                }
            },
            self._meta_upsert_transact_item(user_id, _cart_expires_at()),
            self._outbox_put_transact_item(user_id, event_id, "ITEM_REMOVED", cart_id=user_id),
        ]

        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except self._client.exceptions.TransactionCanceledException as exc:
            if self._condition_failed_for(exc, 0):
                raise LineItemNotFoundError(
                    "No such line item for this account", details={"lineItemId": line_item_id}
                ) from exc
            logger.error(
                "cart_repository.delete_item failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise CartServiceError("Failed to delete cart item") from exc
        except ClientError as exc:
            logger.error(
                "cart_repository.delete_item failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise CartServiceError("Failed to delete cart item") from exc

    # -- outbox (outbox_publisher_handler only) --------------------------

    def get_unpublished_outbox_events(self, limit: int) -> list[dict[str, Any]]:
        # No GSI exists for "every unpublished OUTBOX# row across all
        # users" — this table's only key is (userId, SK), so finding rows
        # across *every* partition needs a Scan, not a Query. Acceptable
        # for this story's expected volume (a periodic, best-effort poll,
        # same cadence as user's own outbox publisher) but doesn't scale
        # indefinitely — a GSI (e.g. a constant partition key + SK) would
        # be the fix if outbox volume ever became Scan-prohibitive.
        # `Limit` bounds items *examined*, not items *matching* the
        # filter — a single call may return fewer than `limit` rows even
        # when more unpublished events exist; the next scheduled run
        # picks up whatever this one didn't reach, same as user's own
        # publisher already tolerates via its own outbox_batch_size.
        try:
            response = self._client.scan(
                TableName=self._table_name,
                FilterExpression="begins_with(SK, :prefix) AND attribute_not_exists(publishedAt)",
                ExpressionAttributeValues={":prefix": _to_av(_OUTBOX_PREFIX)},
                Limit=limit,
            )
        except ClientError as exc:
            logger.error(
                "cart_repository.get_unpublished_outbox_events failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise CartServiceError("Failed to read outbox events") from exc

        events = []
        for raw_item in response.get("Items", []):
            item = {k: _deserializer.deserialize(v) for k, v in raw_item.items()}
            payload = json.loads(item["payload"])
            events.append(
                {
                    "userId": item["userId"],
                    "eventId": item["SK"].removeprefix(_OUTBOX_PREFIX),
                    "type": item["type"],
                    "payload": payload,
                }
            )
        return events

    def mark_outbox_published(self, user_id: str, event_id: str) -> None:
        try:
            self._table.update_item(
                Key={"userId": user_id, "SK": f"{_OUTBOX_PREFIX}{event_id}"},
                UpdateExpression="SET publishedAt = :now",
                ExpressionAttributeValues={":now": datetime.now(UTC).isoformat()},
            )
        except ClientError as exc:
            logger.error(
                "cart_repository.mark_outbox_published failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise CartServiceError("Failed to mark outbox event published") from exc

    # -- transaction-item builders ---------------------------------------

    def _meta_upsert_transact_item(
        self,
        user_id: str,
        expires_at: int,
        explicit_version: int | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if explicit_version is not None:
            # replace_cart's optimistic-concurrency guard (FR-3) lives
            # here, on this Put's own ConditionExpression — not a separate
            # ConditionCheck transact item, since DynamoDB rejects a
            # transaction with two operations against the same item
            # (userId, META). Same condition as the old standalone
            # ConditionCheck would have used: either META doesn't exist
            # yet and the caller expected version 0, or it exists and its
            # cartVersion matches exactly.
            put_item: dict[str, Any] = {
                "TableName": self._table_name,
                "Item": {
                    "userId": _to_av(user_id),
                    "SK": _to_av(_META_SK),
                    "cartVersion": _to_av(explicit_version),
                    "expiresAt": _to_av(expires_at),
                },
            }
            if expected_version is not None:
                put_item["ConditionExpression"] = (
                    "(attribute_not_exists(cartVersion) AND :ifver = :zero) OR cartVersion = :ifver"
                )
                put_item["ExpressionAttributeValues"] = {
                    ":ifver": _to_av(expected_version),
                    ":zero": _to_av(0),
                }
            return {"Put": put_item}
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": {"userId": _to_av(user_id), "SK": _to_av(_META_SK)},
                "UpdateExpression": (
                    "SET cartVersion = if_not_exists(cartVersion, :zero) + :one, expiresAt = :exp"
                ),
                "ExpressionAttributeValues": {
                    ":zero": _to_av(0),
                    ":one": _to_av(1),
                    ":exp": _to_av(expires_at),
                },
            }
        }

    def _idempotency_put_transact_item(
        self, user_id: str, idempotency_key: str, response_dict: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": {
                    "userId": _to_av(user_id),
                    "SK": _to_av(f"{_IDEMPOTENCY_PREFIX}{idempotency_key}"),
                    "responseBody": _to_av(json.dumps(response_dict)),
                    "expiresAt": _to_av(_now_epoch() + _IDEMPOTENCY_TTL_SECONDS),
                },
                "ConditionExpression": "attribute_not_exists(SK)",
            }
        }

    def _outbox_put_transact_item(
        self, user_id: str, event_id: str, change_type: str, cart_id: str
    ) -> dict[str, Any]:
        payload = {
            "eventId": event_id,
            "userId": user_id,
            "cartId": cart_id,
            "changeType": change_type,
            "occurredAt": datetime.now(UTC).isoformat(),
        }
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": {
                    "userId": _to_av(user_id),
                    "SK": _to_av(f"{_OUTBOX_PREFIX}{event_id}"),
                    "type": _to_av("CartUpdated"),
                    "payload": _to_av(json.dumps(payload)),
                    "source": _to_av(self._event_source),
                },
            }
        }

    def _find_idempotency_replay(self, user_id: str, idempotency_key: str) -> LineItem | None:
        try:
            response = self._table.get_item(
                Key={"userId": user_id, "SK": f"{_IDEMPOTENCY_PREFIX}{idempotency_key}"}
            )
        except ClientError as exc:
            logger.error(
                "cart_repository._find_idempotency_replay failed",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
            raise CartServiceError("Failed to check idempotency record") from exc
        item = response.get("Item")
        if item is None:
            return None
        return _response_dict_to_line_item(json.loads(item["responseBody"]))

    @staticmethod
    def _condition_failed_for(exc: ClientError, index: int) -> bool:
        reasons = exc.response.get("CancellationReasons", [])
        if index < 0 or index >= len(reasons):
            return any(r.get("Code") == "ConditionalCheckFailed" for r in reasons)
        return reasons[index].get("Code") == "ConditionalCheckFailed"
