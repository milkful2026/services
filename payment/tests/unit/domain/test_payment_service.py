import json

import pytest

from domain.exceptions import (
    AmountOutOfRangeError,
    GatewayUnavailableError,
    IdempotencyKeyReusedError,
    OrderMismatchError,
    PaymentNotFoundError,
    SignatureInvalidError,
    UnsupportedPurposeError,
)


def _create(service, **overrides):
    kwargs = dict(
        user_id="user-1",
        purpose="WALLET_RECHARGE",
        amount_paise=50000,
        currency="INR",
        method="UPI",
        idempotency_key="idem-1",
        correlation_id="corr-1",
    )
    kwargs.update(overrides)
    return service.create(**kwargs)


def _webhook_body(event: str, order_id: str, payment_id: str, amount_paise: int, **extra) -> bytes:
    entity = {"id": payment_id, "order_id": order_id, "amount": amount_paise, "method": "upi"}
    entity.update(extra)
    return json.dumps({"event": event, "payload": {"payment": {"entity": entity}}}).encode()


class TestCreate:
    def test_new_key_creates_order_once(self, service, gateway, metrics):
        result = _create(service)
        assert result["status"] == "CREATED"
        assert result["razorpayOrderId"] == "order_1"
        assert len(gateway.orders_created) == 1
        assert metrics.count("recharge.created") == 1

    def test_duplicate_key_with_order_is_verbatim_replay(self, service, gateway):
        first = _create(service)
        second = _create(service)
        assert second == first
        assert len(gateway.orders_created) == 1  # not called again

    def test_duplicate_key_without_order_resumes(self, service, gateway, repo):
        gateway.raise_on_create = GatewayUnavailableError("down")
        with pytest.raises(GatewayUnavailableError):
            _create(service)
        gateway.raise_on_create = None
        result = _create(service)
        assert result["status"] == "CREATED"
        assert result["razorpayOrderId"] == "order_1"
        assert len(gateway.orders_created) == 1  # only the successful resume call

    def test_duplicate_key_different_amount_is_conflict(self, service):
        _create(service)
        with pytest.raises(IdempotencyKeyReusedError) as exc:
            _create(service, amount_paise=99999)
        assert exc.value.details["amountPaise"] == 50000

    def test_amount_out_of_range(self, service):
        with pytest.raises(AmountOutOfRangeError):
            _create(service, amount_paise=5000)  # below the fake's 10000 min

    def test_unsupported_purpose(self, service):
        with pytest.raises(UnsupportedPurposeError):
            _create(service, purpose="ORDER")


class TestConfirm:
    def test_valid_signature_moves_to_confirming(self, service, metrics):
        created = _create(service)
        result = service.confirm(
            payment_id=created["paymentId"],
            user_id="user-1",
            razorpay_payment_id="rzp_pay_1",
            razorpay_order_id=created["razorpayOrderId"],
            razorpay_signature="sig",
        )
        assert result["status"] == "CONFIRMING"
        assert metrics.count("recharge.confirming") == 1

    def test_invalid_signature_rejected(self, service, gateway):
        created = _create(service)
        gateway.client_signature_valid = False
        with pytest.raises(SignatureInvalidError):
            service.confirm(
                payment_id=created["paymentId"],
                user_id="user-1",
                razorpay_payment_id="rzp_pay_1",
                razorpay_order_id=created["razorpayOrderId"],
                razorpay_signature="bad",
            )

    def test_order_mismatch(self, service):
        created = _create(service)
        with pytest.raises(OrderMismatchError):
            service.confirm(
                payment_id=created["paymentId"],
                user_id="user-1",
                razorpay_payment_id="rzp_pay_1",
                razorpay_order_id="some_other_order",
                razorpay_signature="sig",
            )

    def test_wrong_owner_is_not_found(self, service):
        created = _create(service)
        with pytest.raises(PaymentNotFoundError):
            service.confirm(
                payment_id=created["paymentId"],
                user_id="someone-else",
                razorpay_payment_id="rzp_pay_1",
                razorpay_order_id=created["razorpayOrderId"],
                razorpay_signature="sig",
            )

    def test_already_confirmed_is_noop(self, service):
        created = _create(service)
        order_id = created["razorpayOrderId"]
        service.confirm(
            payment_id=created["paymentId"], user_id="user-1",
            razorpay_payment_id="rzp_pay_1", razorpay_order_id=order_id, razorpay_signature="sig",
        )
        service.apply_webhook(
            _webhook_body("payment.captured", order_id, "rzp_pay_1", 50000), "any-sig-fake-ok"
        )
        result = service.confirm(
            payment_id=created["paymentId"], user_id="user-1",
            razorpay_payment_id="rzp_pay_1", razorpay_order_id=order_id, razorpay_signature="sig",
        )
        assert result["status"] == "CONFIRMED"


class TestApplyWebhookCaptured:
    def test_captured_confirms_and_emits_payment_confirmed(self, service, repo, metrics):
        created = _create(service)
        order_id = created["razorpayOrderId"]
        service.apply_webhook(
            _webhook_body("payment.captured", order_id, "rzp_pay_1", 50000), "sig"
        )
        payment = repo.get(created["paymentId"])
        assert payment.status.value == "CONFIRMED"
        unpub = repo.fetch_unpublished()
        assert len(unpub) == 1
        assert unpub[0]["event_type"] == "PaymentConfirmed"
        assert unpub[0]["payload"]["purpose"] == "WALLET_RECHARGE"
        assert metrics.count("recharge.confirmed") == 1

    def test_duplicate_webhook_is_noop(self, service, repo):
        created = _create(service)
        order_id = created["razorpayOrderId"]
        body = _webhook_body("payment.captured", order_id, "rzp_pay_1", 50000)
        service.apply_webhook(body, "sig")
        service.apply_webhook(body, "sig")
        assert len(repo.fetch_unpublished()) == 1  # only one PaymentConfirmed

    def test_amount_mismatch_fails_the_payment(self, service, repo, metrics):
        created = _create(service)
        order_id = created["razorpayOrderId"]
        service.apply_webhook(
            _webhook_body("payment.captured", order_id, "rzp_pay_1", 1), "sig"
        )
        payment = repo.get(created["paymentId"])
        assert payment.status.value == "FAILED"
        assert payment.failure_code == "AMOUNT_MISMATCH"
        assert metrics.count("recharge.failed") == 1

    def test_bad_signature_rejected_before_parsing(self, service, gateway):
        gateway.webhook_signature_valid = False
        with pytest.raises(SignatureInvalidError):
            service.apply_webhook(b"not even json", "bad-sig")


class TestApplyWebhookFailed:
    def test_failed_event_fails_the_payment(self, service, repo, metrics):
        created = _create(service)
        order_id = created["razorpayOrderId"]
        body = _webhook_body(
            "payment.failed", order_id, "rzp_pay_1", 50000,
            error_code="BAD_REQUEST_ERROR", error_description="card declined",
        )
        service.apply_webhook(body, "sig")
        payment = repo.get(created["paymentId"])
        assert payment.status.value == "FAILED"
        assert payment.failure_code == "BAD_REQUEST_ERROR"
        unpub = repo.fetch_unpublished()
        assert unpub[0]["event_type"] == "PaymentFailed"
        assert metrics.count("recharge.failed") == 1


class TestLateCaptureRecovery:
    def test_late_capture_after_provisional_timeout_recovers(self, service, repo):
        created = _create(service)
        payment_id, order_id = created["paymentId"], created["razorpayOrderId"]
        repo.set_status(payment_id, status="FAILED", failure_code="TIMEOUT")

        service.apply_webhook(
            _webhook_body("payment.captured", order_id, "rzp_pay_1", 50000), "sig"
        )
        payment = repo.get(payment_id)
        assert payment.status.value == "CONFIRMED"
        assert any(p["event_type"] == "PaymentConfirmed" for p in repo.fetch_unpublished())

    def test_late_capture_after_real_failure_stays_failed(self, service, repo):
        created = _create(service)
        payment_id, order_id = created["paymentId"], created["razorpayOrderId"]
        repo.set_status(payment_id, status="FAILED", failure_code="BAD_REQUEST_ERROR")

        service.apply_webhook(
            _webhook_body("payment.captured", order_id, "rzp_pay_1", 50000), "sig"
        )
        payment = repo.get(payment_id)
        assert payment.status.value == "FAILED"
        assert payment.failure_code == "BAD_REQUEST_ERROR"
        assert repo.fetch_unpublished() == []  # no PaymentConfirmed emitted


class TestReconcileOnce:
    def test_captured_confirms(self, service, repo, gateway):
        created = _create(service)
        payment_id, order_id = created["paymentId"], created["razorpayOrderId"]
        repo.set_status(payment_id, status="CONFIRMING")
        _age_payment(repo, payment_id, 300)
        gateway.order_payments[order_id] = [
            {
                "status": "captured",
                "id": "rzp_pay_1",
                "order_id": order_id,
                "amount": 50000,
                "method": "upi",
            }
        ]

        outcomes = service.reconcile_once()
        assert outcomes == ["confirmed"]
        assert repo.get(payment_id).status.value == "CONFIRMED"

    def test_all_failed_fails_the_payment(self, service, repo, gateway):
        created = _create(service)
        payment_id, order_id = created["paymentId"], created["razorpayOrderId"]
        repo.set_status(payment_id, status="CONFIRMING")
        _age_payment(repo, payment_id, 300)
        gateway.order_payments[order_id] = [
            {"status": "failed", "id": "rzp_pay_1", "order_id": order_id, "error_code": "X"}
        ]

        outcomes = service.reconcile_once()
        assert outcomes == ["failed"]
        assert repo.get(payment_id).status.value == "FAILED"

    def test_still_payable_under_30min_stays_confirming_no_alert(self, service, repo, metrics):
        created = _create(service)
        payment_id = created["paymentId"]
        repo.set_status(payment_id, status="CONFIRMING")
        _age_payment(repo, payment_id, 300)  # < reconcile_confirming_alert_seconds (1800)

        outcomes = service.reconcile_once()
        assert outcomes == ["still_confirming"]
        assert metrics.count("payment.confirming_over_30m") == 0
        assert repo.get(payment_id).status.value == "CONFIRMING"

    def test_still_payable_over_30min_emits_alert_metric(self, service, repo, metrics):
        created = _create(service)
        payment_id = created["paymentId"]
        repo.set_status(payment_id, status="CONFIRMING")
        _age_payment(repo, payment_id, 1900)  # > 1800s

        outcomes = service.reconcile_once()
        assert outcomes == ["still_confirming_alert"]
        assert metrics.count("payment.confirming_over_30m") == 1
        assert repo.get(payment_id).status.value == "CONFIRMING"

    def test_hard_cap_marks_provisional_timeout(self, service, repo, metrics):
        created = _create(service)
        payment_id = created["paymentId"]
        repo.set_status(payment_id, status="CONFIRMING")
        _age_payment(repo, payment_id, 22000)  # > 21600s hard cap

        outcomes = service.reconcile_once()
        assert outcomes == ["timeout"]
        payment = repo.get(payment_id)
        assert payment.status.value == "FAILED"
        assert payment.failure_code == "TIMEOUT"
        assert any(p["event_type"] == "PaymentFailed" for p in repo.fetch_unpublished())

    def test_created_without_order_is_skipped(self, service, repo):
        # A row that never even got a razorpay_order_id (orders_create
        # failed before persisting one) has nothing to reconcile against.
        payment = repo.insert_created(
            payment_id="pay_orphan", user_id="user-1", purpose="WALLET_RECHARGE",
            amount_paise=10000, currency="INR", method=None,
            idempotency_key="k2", correlation_id="c2",
        )
        _age_payment(repo, payment.id, 300)
        assert service.reconcile_once() == []

    def test_every_branch_emits_reconciled_metric(self, service, repo, gateway, metrics):
        created = _create(service)
        payment_id = created["paymentId"]
        repo.set_status(payment_id, status="CONFIRMING")
        _age_payment(repo, payment_id, 300)
        service.reconcile_once()
        assert metrics.count("recharge.reconciled") == 1


def _age_payment(repo, payment_id: str, seconds: float) -> None:
    """Backdate `updated_at` so the reconciliation sweep treats this row
    as stale, without needing real wall-clock time to pass."""
    from datetime import UTC, datetime, timedelta

    from adapters.payment_repository import payments_table

    backdated = datetime.now(UTC) - timedelta(seconds=seconds)
    with repo._engine.begin() as conn:  # noqa: SLF001 — test-only reach-in
        conn.execute(
            payments_table.update()
            .where(payments_table.c.id == payment_id)
            .values(updated_at=backdated)
        )
