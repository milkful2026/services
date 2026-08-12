import time

import pytest
from botocore.exceptions import ClientError

from adapters.otp_store_adapter import DynamoDbOtpStoreAdapter
from domain.exceptions import ExternalServiceUnavailableError
from domain.models import OtpRecord, OtpStatus


@pytest.fixture
def adapter(otp_table):
    return DynamoDbOtpStoreAdapter(table_name="otp_requests", region_name="ap-south-1")


def _record(**overrides) -> OtpRecord:
    now = int(time.time())
    defaults = dict(
        request_id="req-1",
        mobile="+919876543210",
        otp_hash="hashed",
        attempts=0,
        status=OtpStatus.ACTIVE,
        ttl=now + 300,
        last_sent_at=now,
        purpose="REGISTER",
    )
    defaults.update(overrides)
    return OtpRecord(**defaults)


def test_put_and_get_round_trip(adapter):
    record = _record()
    adapter.put(record)

    fetched = adapter.get("req-1")

    assert fetched == record


def test_get_missing_returns_none(adapter):
    assert adapter.get("does-not-exist") is None


def test_get_active_by_mobile_finds_active_record(adapter):
    adapter.put(_record())

    found = adapter.get_active_by_mobile("+919876543210")

    assert found is not None
    assert found.request_id == "req-1"


def test_get_active_by_mobile_ignores_consumed_records(adapter):
    adapter.put(_record(status=OtpStatus.CONSUMED))

    assert adapter.get_active_by_mobile("+919876543210") is None


def test_get_active_by_mobile_no_match_returns_none(adapter):
    assert adapter.get_active_by_mobile("+910000000000") is None


def test_get_active_by_mobile_prefers_most_recent_when_multiple_active(adapter):
    now = int(time.time())
    adapter.put(_record(request_id="older", last_sent_at=now - 100))
    adapter.put(_record(request_id="newer", last_sent_at=now))

    found = adapter.get_active_by_mobile("+919876543210")

    assert found.request_id == "newer"


def test_increment_attempts(adapter):
    adapter.put(_record())

    first = adapter.increment_attempts("req-1")
    second = adapter.increment_attempts("req-1")

    assert first == 1
    assert second == 2


def test_mark_status(adapter):
    adapter.put(_record())

    adapter.mark_status("req-1", OtpStatus.LOCKED.value)

    assert adapter.get("req-1").status == OtpStatus.LOCKED


def test_put_wraps_client_error(adapter, monkeypatch):
    def _raise(*args, **kwargs):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem")

    monkeypatch.setattr(adapter._table, "put_item", _raise)

    with pytest.raises(ExternalServiceUnavailableError):
        adapter.put(_record())
