"""OTP business rules: generation, hashing, expiry, lockout, duplicate-send
handling, and rate-limit delegation.

No AWS SDK imports here (services/README.md §3.4) — only the abstract
adapter Protocols and the `bcrypt` hashing library (a business rule, not a
vendor SDK).
"""

import secrets
import time
import uuid

import bcrypt

from adapters.interfaces import OtpSendLockPort, OtpStorePort, RateLimiterPort
from domain.exceptions import (
    OtpAttemptsExceededError,
    OtpExpiredError,
    OtpInvalidError,
    OtpRequestInProgressError,
    OtpRequestNotFoundError,
)
from domain.models import OtpRecord, OtpStatus

# Distinct prefixes per spec MA-21 FR-1: a user mid-registration and a
# user attempting login on the same mobile must not exhaust each other's
# rate-limit/lock budget. Unknown purposes fall back to the REGISTER
# prefix rather than raising — this mirrors the DynamoDB record's own
# "absence of purpose == REGISTER" back-compat default.
_KEY_PREFIXES = {"REGISTER": "register:otp:", "LOGIN": "login:otp:"}


def _key_prefix(purpose: str) -> str:
    return _KEY_PREFIXES.get(purpose, _KEY_PREFIXES["REGISTER"])


class OtpService:
    def __init__(
        self,
        otp_store: OtpStorePort,
        rate_limiter: RateLimiterPort | None = None,
        otp_length: int = 6,
        ttl_seconds: int = 300,
        resend_after_seconds: int = 30,
        max_attempts: int = 3,
        rate_limit_max_requests: int = 3,
        rate_limit_window_seconds: int = 900,
        send_lock: OtpSendLockPort | None = None,
        send_lock_ttl_seconds: int = 10,
    ) -> None:
        self._otp_store = otp_store
        self._rate_limiter = rate_limiter
        self._otp_length = otp_length
        self._ttl_seconds = ttl_seconds
        self._resend_after_seconds = resend_after_seconds
        self._max_attempts = max_attempts
        self._rate_limit_max_requests = rate_limit_max_requests
        self._rate_limit_window_seconds = rate_limit_window_seconds
        self._send_lock = send_lock
        self._send_lock_ttl_seconds = send_lock_ttl_seconds

    def request_otp(self, mobile: str, purpose: str = "REGISTER") -> tuple[OtpRecord, str, bool]:
        """Returns (record, plaintext_otp, is_resend).

        `is_resend` is True when an active OTP already exists for this
        mobile — the caller must not re-publish the SMS in that case,
        only if `resendAfter` has elapsed (spec FR-1 edge case).
        """
        if self._send_lock is None:
            return self._request_otp_locked(mobile, purpose)

        lock_key = f"{_key_prefix(purpose)}lock:{mobile}"
        if not self._send_lock.acquire(lock_key, self._send_lock_ttl_seconds):
            # Another request for this mobile is already inside the
            # check-then-act section below — without this, both could see
            # no active OTP and each create a separate ACTIVE record.
            raise OtpRequestInProgressError(
                "A request for this mobile is already being processed"
            )
        try:
            return self._request_otp_locked(mobile, purpose)
        finally:
            self._send_lock.release(lock_key)

    def _request_otp_locked(self, mobile: str, purpose: str) -> tuple[OtpRecord, str, bool]:
        existing = self._otp_store.get_active_by_mobile(mobile, purpose)
        now = int(time.time())

        if existing is not None and existing.ttl > now:
            elapsed = now - existing.last_sent_at
            if elapsed < self._resend_after_seconds:
                # Still within the resend cooldown — caller must not
                # generate a new OTP or publish a new SMS.
                return existing, "", True

        # Fresh send, or a resend after cooldown elapsed. Rate limit is
        # checked here, not on every duplicate-send lookup, so cooldown
        # polling doesn't itself burn rate-limit budget.
        #
        # rate_limiter is typed Optional purely so verify-only callers
        # (e.g. login_otp_verify_handler) aren't forced to construct a
        # Redis connection they'll never use — verify_otp()/
        # mark_otp_consumed() never reach this method. request_otp()
        # itself still requires a real one: intentionally left
        # unconditional (no `is not None` guard) so a composition root
        # that forgets to wire it fails loudly here instead of silently
        # shipping with rate limiting disabled.
        rate_limit_key = f"{_key_prefix(purpose)}{mobile}"
        self._rate_limiter.check_and_increment(
            rate_limit_key, self._rate_limit_max_requests, self._rate_limit_window_seconds
        )

        plaintext_otp = "".join(secrets.choice("0123456789") for _ in range(self._otp_length))
        otp_hash = bcrypt.hashpw(plaintext_otp.encode(), bcrypt.gensalt()).decode()

        # A resend after cooldown reuses the existing requestId and
        # overwrites it in place — it must NOT create a second ACTIVE
        # record for the same mobile, or the old OTP would remain valid
        # alongside the new one until its original TTL naturally expires.
        record = OtpRecord(
            request_id=existing.request_id if existing is not None else str(uuid.uuid4()),
            mobile=mobile,
            otp_hash=otp_hash,
            attempts=0,
            status=OtpStatus.ACTIVE,
            ttl=now + self._ttl_seconds,
            last_sent_at=now,
            purpose=purpose,
        )
        self._otp_store.put(record)
        return record, plaintext_otp, False

    def mark_send_failed(self, request_id: str) -> None:
        """Called by the handler when publishing the OTP SMS fails after
        retries. Moves the record out of ACTIVE so the next request_otp()
        call for this mobile is treated as a fresh send instead of being
        silently swallowed by the resend cooldown for an OTP that was
        never actually delivered.
        """
        self._otp_store.mark_status(request_id, OtpStatus.SEND_FAILED.value)

    def verify_otp(self, mobile: str, otp: str, request_id: str) -> OtpRecord:
        """Checks the code is correct and still usable, but deliberately
        does NOT mark it consumed — callers that still have a further
        step that can itself fail (e.g. issuing Cognito tokens) must call
        mark_otp_consumed() only after that step succeeds. Otherwise a
        transient failure in that later step would burn the OTP for an
        operation that never actually completed, forcing the caller to
        restart the whole send/verify cycle. This narrows (rather than
        eliminates) the window in which the same code could be replayed —
        accepted since verify-then-act is one logical operation split
        only for downstream-failure safety, and the window is bounded by
        that downstream call's own latency.
        """
        record = self._otp_store.get(request_id)
        if record is None or record.mobile != mobile:
            raise OtpRequestNotFoundError("No matching OTP request")

        if record.status == OtpStatus.LOCKED:
            raise OtpAttemptsExceededError("Maximum verification attempts exceeded")

        now = int(time.time())
        if record.status != OtpStatus.ACTIVE or record.ttl <= now:
            raise OtpExpiredError("OTP has expired")

        if not bcrypt.checkpw(otp.encode(), record.otp_hash.encode()):
            attempts = self._otp_store.increment_attempts(request_id)
            if attempts >= self._max_attempts:
                self._otp_store.mark_status(request_id, OtpStatus.LOCKED.value)
                raise OtpAttemptsExceededError("Maximum verification attempts exceeded")
            raise OtpInvalidError("Incorrect OTP")

        return record

    def mark_otp_consumed(self, request_id: str) -> None:
        self._otp_store.mark_status(request_id, OtpStatus.CONSUMED.value)
