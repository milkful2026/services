"""Code-review fix (finding #6): /healthz must reflect only the core
REST API's own liveness, not the separate user_status_changed_consumer
background thread (MA-140) -- that thread's health is exposed
separately at /healthz/consumer so an orchestrator doesn't recycle the
whole subscription service over an unrelated consumer failure."""

from fastapi.testclient import TestClient

from handlers.app import app
from handlers.health import consumer_health


def test_healthz_is_always_ok_even_if_the_consumer_thread_died():
    consumer_health.alive = False
    try:
        client = TestClient(app)
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    finally:
        consumer_health.alive = True


def test_healthz_consumer_is_ok_while_the_consumer_thread_is_alive():
    client = TestClient(app)
    response = client.get("/healthz/consumer")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_consumer_reports_503_once_the_consumer_thread_dies():
    consumer_health.alive = False
    try:
        client = TestClient(app)
        response = client.get("/healthz/consumer")
        assert response.status_code == 503
        assert response.json()["status"] == "unhealthy"
    finally:
        consumer_health.alive = True
