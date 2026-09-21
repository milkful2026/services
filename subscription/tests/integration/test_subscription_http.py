"""FastAPI HTTP surface — TestClient against the SQLite double."""

from datetime import date, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from handlers.app import app
from handlers.dependencies import get_subscription_service


@pytest.fixture
def client(service):
    get_subscription_service.cache_clear()
    app.dependency_overrides[get_subscription_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def _bearer(sub="user-1"):
    return {"Authorization": "Bearer " + jwt.encode({"sub": sub}, "x", algorithm="HS256")}


def _create_body(**overrides):
    body = {
        "productId": "prod-1",
        "quantity": 1,
        "schedule": {"type": "DAILY"},
        # +3 days, not +1 — the server computes "today" in IST while this
        # helper uses the naive local/UTC date; near a UTC-evening/
        # IST-early-morning boundary those two "today"s can disagree by a
        # day, which would flip this into the same-day-emission path
        # (already covered deterministically in the unit tests) instead
        # of the plain future-start-date case this suite exercises.
        "startDate": (date.today() + timedelta(days=3)).isoformat(),
        "slotId": "slot-1",
        "idempotencyKey": "key-1",
    }
    body.update(overrides)
    return body


def test_full_lifecycle_round_trip(client):
    created = client.post("/subscriptions", json=_create_body(), headers=_bearer()).json()["data"]
    sub_id = created["subscriptionId"]
    assert created["status"] == "ACTIVE"

    paused = client.post(
        f"/subscriptions/{sub_id}/pause", json={}, headers=_bearer()
    ).json()["data"]
    assert paused["status"] == "PAUSED"

    resumed = client.post(f"/subscriptions/{sub_id}/resume", headers=_bearer()).json()["data"]
    assert resumed["status"] == "ACTIVE"

    skip_date = resumed["nextDeliveryDate"]
    skip_resp = client.post(
        f"/subscriptions/{sub_id}/skip", json={"date": skip_date}, headers=_bearer()
    )
    assert skip_resp.status_code == 200

    detail = client.get(f"/subscriptions/{sub_id}", headers=_bearer()).json()["data"]
    assert skip_date in detail["skippedDates"]

    edited = client.patch(
        f"/subscriptions/{sub_id}", json={"quantity": 3}, headers=_bearer()
    ).json()["data"]
    assert "effectiveFrom" in edited

    stopped = client.post(f"/subscriptions/{sub_id}/stop", headers=_bearer()).json()["data"]
    assert stopped["status"] == "STOPPED"

    # Idempotent re-stop.
    stopped_again = client.post(f"/subscriptions/{sub_id}/stop", headers=_bearer()).json()["data"]
    assert stopped_again["status"] == "STOPPED"


def test_create_then_list_mine(client):
    client.post("/subscriptions", json=_create_body(), headers=_bearer())
    body = client.get("/subscriptions/me", headers=_bearer()).json()["data"]
    assert len(body["subscriptions"]) == 1


def test_get_someone_elses_subscription_is_404(client):
    created = client.post("/subscriptions", json=_create_body(), headers=_bearer()).json()["data"]
    r = client.get(f"/subscriptions/{created['subscriptionId']}", headers=_bearer(sub="user-2"))
    assert r.status_code == 404
    assert r.json()["data"]["errorCode"] == "SUBSCRIPTION_NOT_FOUND"


def test_create_non_eligible_product_is_422(client, catalog_client):
    catalog_client.seed("prod-bad", subscription_eligible=False)
    r = client.post(
        "/subscriptions", json=_create_body(productId="prod-bad"), headers=_bearer()
    )
    assert r.status_code == 422
    assert r.json()["data"]["errorCode"] == "PRODUCT_NOT_ELIGIBLE"


def test_missing_bearer_is_401(client):
    assert client.get("/subscriptions/me").status_code == 401


def test_internal_run_daily_no_auth(client):
    r = client.post("/internal/run-daily")
    assert r.status_code == 200
    assert "dueSubscriptionIds" in r.json()["data"]


def test_run_daily_emits_schema_valid_event(client, repo):
    import jsonschema
    from shared.events import load_schema

    from domain.models import IST

    # startDate = the server's own IST "tomorrow" (not the naive local
    # date +1 the other tests use) so this never collides with create's
    # same-day-emission path regardless of a UTC/IST day-boundary
    # crossing, and matches exactly what run_daily computes as "tomorrow"
    # moments later.
    ist_tomorrow = (datetime.now(IST) + timedelta(days=1)).date()
    client.post(
        "/subscriptions", json=_create_body(startDate=ist_tomorrow.isoformat()), headers=_bearer()
    )
    r = client.post("/internal/run-daily")
    assert r.status_code == 200

    unpub = repo.fetch_unpublished()
    due_events = [e for e in unpub if e["event_type"] == "SubscriptionOrderDue"]
    assert len(due_events) == 1
    jsonschema.validate(due_events[0]["payload"], load_schema("SubscriptionOrderDue"))
