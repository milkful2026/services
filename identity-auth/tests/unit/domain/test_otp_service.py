"""Domain-level tests for OtpService, using simple in-memory fakes for the
adapter ports (not moto/fakeredis) — the domain layer must not know or
care what's behind the Protocol."""

import time

import bcrypt
import pytest

from domain.exceptions import (
    OtpAttemptsExceededError,
    OtpExpiredError,
    OtpInvalidError,
    OtpRequestInProgressError,
    OtpRequestNotFoundError,
    RateLimitExceededError,
)
from domain.models import OtpRecord, OtpStatus
from domain.otp_service import OtpService


class FakeOtpStore:
    def __init__(self):
        self.records: dict[str, OtpRecord] = {}

    def put(self, record: OtpRecord) -> None:
        self.records[record.request_id] = record

    def get(self, request_id: str) -> OtpRecord | None:
        return self.records.get(request_id)

    def get_active_by_mobile(self, mobile: str, purpose: str) -> OtpRecord | None:
        for r in self.records.values():
            if r.mobile == mobile and r.status == OtpStatus.ACTIVE and r.purpose == purpose:
                return r
        return None

    def increment_attempts(self, request_id: str) -> int:
        r = self.records[request_id]
        r.attempts += 1
        return r.attempts

    def mark_status(self, request_id: str, status: str) -> None:
        self.records[request_id].status = OtpStatus(status)


class FakeRateLimiter:
    def __init__(self, exhausted: bool = False):
        self.exhausted = exhausted
        self.calls: list[str] = []

    def check_and_increment(self, key: str, max_requests: int, window_seconds: int) -> None:
        self.calls.append(key)
        if self.exhausted:
            raise RateLimitExceededError("Too many requests")


@pytest.fixture
def store():
    return FakeOtpStore()


@pytest.fixture
def limiter():
    return FakeRateLimiter()


@pytest.fixture
def service(store, limiter):
    return OtpService(
        otp_store=store,
        rate_limiter=limiter,
        otp_length=6,
        ttl_seconds=300,
        resend_after_seconds=30,
        max_attempts=3,
    )


def test_request_otp_generates_six_digit_code_and_hashes_it(service, store):
    record, plaintext, is_resend = service.request_otp("+919876543210")

    assert is_resend is False
    assert len(plaintext) == 6
    assert plaintext.isdigit()
    assert bcrypt.checkpw(plaintext.encode(), record.otp_hash.encode())
    assert store.records[record.request_id] is record


def test_request_otp_uses_rate_limit_key_scoped_to_registration(service, limiter):
    service.request_otp("+919876543210")
    assert limiter.calls == ["register:otp:+919876543210"]


def test_request_otp_login_purpose_uses_isolated_rate_limit_key(service, limiter):
    service.request_otp("+919876543210", purpose="LOGIN")
    assert limiter.calls == ["login:otp:+919876543210"]


def test_request_otp_login_and_register_counters_are_independent(store, limiter):
    service = OtpService(otp_store=store, rate_limiter=limiter)

    # Same mobile, both purposes — a user mid-registration and a login
    # attempt on the same number must not share rate-limit budget (spec
    # MA-21 FR-1). Each purpose gets its own ACTIVE record too, since
    # get_active_by_mobile's caller-side gating (find_verified_sub_by_phone)
    # means the two flows are mutually exclusive for a given mobile in
    # practice, but the rate-limit key isolation is what this test checks.
    service.request_otp("+919876543210", purpose="REGISTER")
    service.request_otp("+919876543210", purpose="LOGIN")

    assert limiter.calls == ["register:otp:+919876543210", "login:otp:+919876543210"]


def test_request_otp_login_purpose_persists_on_the_record(service, store):
    record, _, _ = service.request_otp("+919876543210", purpose="LOGIN")

    assert store.records[record.request_id].purpose == "LOGIN"


def test_request_otp_within_resend_cooldown_returns_same_request_and_skips_rate_limit(
    service, limiter
):
    first, _, _ = service.request_otp("+919876543210")
    second, plaintext, is_resend = service.request_otp("+919876543210")

    assert is_resend is True
    assert second.request_id == first.request_id
    assert plaintext == ""
    # Only the original send should have consumed rate-limit budget.
    assert limiter.calls == ["register:otp:+919876543210"]


def test_request_otp_after_cooldown_elapsed_issues_new_otp(service, store):
    first, _, _ = service.request_otp("+919876543210")
    # Simulate cooldown having elapsed.
    store.records[first.request_id].last_sent_at -= 31

    second, plaintext, is_resend = service.request_otp("+919876543210")

    assert is_resend is False
    assert plaintext != ""
    assert second.request_id == first.request_id  # same active record, refreshed


def test_request_otp_propagates_rate_limit_exceeded(store):
    limiter = FakeRateLimiter(exhausted=True)
    service = OtpService(otp_store=store, rate_limiter=limiter)

    with pytest.raises(RateLimitExceededError):
        service.request_otp("+919876543210")


def test_verify_otp_success_does_not_mark_consumed(service, store):
    # verify_otp() alone must leave the record ACTIVE — only
    # mark_otp_consumed() (called by the handler after its own further
    # step, e.g. issuing tokens, succeeds) consumes it. See
    # OtpService.verify_otp's docstring.
    record, plaintext, _ = service.request_otp("+919876543210")

    result = service.verify_otp("+919876543210", plaintext, record.request_id)

    assert result.request_id == record.request_id
    assert store.records[record.request_id].status == OtpStatus.ACTIVE


def test_mark_otp_consumed_marks_record_consumed(service, store):
    record, plaintext, _ = service.request_otp("+919876543210")
    service.verify_otp("+919876543210", plaintext, record.request_id)

    service.mark_otp_consumed(record.request_id)

    assert store.records[record.request_id].status == OtpStatus.CONSUMED


def test_verify_otp_without_mark_consumed_can_be_retried(service, store):
    # The exact scenario this split guards against: a downstream failure
    # after verify_otp() succeeds (e.g. Cognito throttling) must not burn
    # the code — a second verify_otp() call with the same code must still
    # succeed.
    record, plaintext, _ = service.request_otp("+919876543210")

    service.verify_otp("+919876543210", plaintext, record.request_id)
    result = service.verify_otp("+919876543210", plaintext, record.request_id)  # must not raise

    assert result.request_id == record.request_id


def test_verify_otp_works_without_a_rate_limiter_configured(store):
    # rate_limiter is optional so verify-only callers aren't forced to
    # wire one — verify_otp()/mark_otp_consumed() never touch it.
    service = OtpService(otp_store=store)
    record = OtpRecord(
        request_id="req-1",
        mobile="+919876543210",
        otp_hash=bcrypt.hashpw(b"123456", bcrypt.gensalt()).decode(),
        attempts=0,
        status=OtpStatus.ACTIVE,
        ttl=int(time.time()) + 300,
        last_sent_at=int(time.time()),
    )
    store.put(record)

    result = service.verify_otp("+919876543210", "123456", "req-1")
    service.mark_otp_consumed("req-1")

    assert result.request_id == "req-1"
    assert store.records["req-1"].status == OtpStatus.CONSUMED


def test_request_otp_without_rate_limiter_raises_attribute_error(store):
    # The flip side of the above: request_otp() genuinely needs a rate
    # limiter — omitting it is a composition-root bug, not a supported
    # "no rate limiting" mode, and must fail loudly rather than silently
    # skip the check.
    service = OtpService(otp_store=store)

    with pytest.raises(AttributeError):
        service.request_otp("+919876543210")


def test_verify_otp_unknown_request_id_raises_not_found(service):
    with pytest.raises(OtpRequestNotFoundError):
        service.verify_otp("+919876543210", "123456", "does-not-exist")


def test_verify_otp_mismatched_mobile_raises_not_found(service, store):
    record, plaintext, _ = service.request_otp("+919876543210")

    with pytest.raises(OtpRequestNotFoundError):
        service.verify_otp("+919999999999", plaintext, record.request_id)


def test_verify_otp_expired_raises(service, store):
    record, plaintext, _ = service.request_otp("+919876543210")
    store.records[record.request_id].ttl = int(time.time()) - 1

    with pytest.raises(OtpExpiredError):
        service.verify_otp("+919876543210", plaintext, record.request_id)


def test_verify_otp_wrong_code_raises_invalid_and_increments_attempts(service, store):
    record, _, _ = service.request_otp("+919876543210")

    with pytest.raises(OtpInvalidError):
        service.verify_otp("+919876543210", "000000", record.request_id)

    assert store.records[record.request_id].attempts == 1


def test_verify_otp_locks_after_max_attempts(service, store):
    record, _, _ = service.request_otp("+919876543210")

    for _ in range(2):
        with pytest.raises(OtpInvalidError):
            service.verify_otp("+919876543210", "000000", record.request_id)

    with pytest.raises(OtpAttemptsExceededError):
        service.verify_otp("+919876543210", "000000", record.request_id)

    assert store.records[record.request_id].status == OtpStatus.LOCKED


def test_verify_otp_already_locked_raises_attempts_exceeded_without_reincrementing(
    service, store
):
    record, _, _ = service.request_otp("+919876543210")
    store.records[record.request_id].status = OtpStatus.LOCKED

    with pytest.raises(OtpAttemptsExceededError):
        service.verify_otp("+919876543210", "999999", record.request_id)

    assert store.records[record.request_id].attempts == 0


def test_mark_send_failed_moves_record_out_of_active(service, store):
    record, _, _ = service.request_otp("+919876543210")

    service.mark_send_failed(record.request_id)

    assert store.records[record.request_id].status == OtpStatus.SEND_FAILED


def test_request_otp_after_send_failed_is_treated_as_fresh_send_not_cooldown(
    service, store, limiter
):
    record, _, _ = service.request_otp("+919876543210")
    service.mark_send_failed(record.request_id)

    # Immediately retrying — well within what would have been the resend
    # cooldown — must generate and publish a brand-new OTP rather than
    # being swallowed as a silent within-cooldown resend, since the first
    # one was never actually delivered.
    second, plaintext, is_resend = service.request_otp("+919876543210")

    assert is_resend is False
    assert plaintext != ""
    assert limiter.calls == ["register:otp:+919876543210", "register:otp:+919876543210"]


class FakeLock:
    def __init__(self):
        self.held: set[str] = set()
        self.acquire_calls: list[str] = []

    def acquire(self, key: str, ttl_seconds: int) -> bool:
        self.acquire_calls.append(key)
        if key in self.held:
            return False
        self.held.add(key)
        return True

    def release(self, key: str) -> None:
        self.held.discard(key)


def test_request_otp_without_lock_configured_is_unaffected(service):
    # Default fixture has no send_lock — must behave exactly as before.
    record, plaintext, is_resend = service.request_otp("+919876543210")
    assert is_resend is False
    assert plaintext != ""


def test_request_otp_acquires_and_releases_send_lock_per_mobile(store, limiter):
    lock = FakeLock()
    service = OtpService(otp_store=store, rate_limiter=limiter, send_lock=lock)

    service.request_otp("+919876543210")

    assert lock.acquire_calls == ["register:otp:lock:+919876543210"]
    assert lock.held == set()  # released after the call completes


def test_request_otp_raises_when_lock_already_held(store, limiter):
    lock = FakeLock()
    lock.held.add("register:otp:lock:+919876543210")
    service = OtpService(otp_store=store, rate_limiter=limiter, send_lock=lock)

    with pytest.raises(OtpRequestInProgressError):
        service.request_otp("+919876543210")

    # A rejected concurrent request must not have consumed rate-limit budget.
    assert limiter.calls == []


def test_request_otp_login_purpose_uses_isolated_lock_key(store, limiter):
    lock = FakeLock()
    service = OtpService(otp_store=store, rate_limiter=limiter, send_lock=lock)

    service.request_otp("+919876543210", purpose="LOGIN")

    assert lock.acquire_calls == ["login:otp:lock:+919876543210"]
