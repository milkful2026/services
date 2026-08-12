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

from adapters.interfaces import OtpStorePort, RateLimiterPort
from domain.exceptions import (
    OtpAttemptsExceededError,
    OtpExpiredError,
    OtpInvalidError,
    OtpRequestNotFoundError,
)
from domain.models import OtpRecord, OtpStatus


class OtpService:
    def __init__(
        self,
        otp_store: OtpStorePort,
        rate_limiter: RateLimiterPort,
        otp_length: int = 6,
        ttl_seconds: int = 300,
        resend_after_seconds: int = 30,
        max_attempts: int = 3,
        rate_limit_max_requests: int = 3,
        rate_limit_window_seconds: int = 900,
    ) -> None:
        self._otp_store = otp_store
        self._rate_limiter = rate_limiter
        self._otp_length = otp_length
        self._ttl_seconds = ttl_seconds
        self._resend_after_seconds = resend_after_seconds
        self._max_attempts = max_attempts
        self._rate_limit_max_requests = rate_limit_max_requests
        self._rate_limit_window_seconds = rate_limit_window_seconds

    def request_otp(self, mobile: str, purpose: str = "REGISTER") -> tuple[OtpRecord, str, bool]:
        """Returns (record, plaintext_otp, is_resend).

        `is_resend` is True when an active OTP already exists for this
        mobile — the caller must not re-publish the SMS in that case,
        only if `resendAfter` has elapsed (spec FR-1 edge case).
        """
        existing = self._otp_store.get_active_by_mobile(mobile)
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
        rate_limit_key = f"register:otp:{mobile}"
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

    def verify_otp(self, mobile: str, otp: str, request_id: str) -> OtpRecord:
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

        self._otp_store.mark_status(request_id, OtpStatus.CONSUMED.value)
        return record
