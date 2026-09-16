"""Signature verification against known vectors — no network, no real
Razorpay SDK calls."""

import hashlib
import hmac

import pytest
from razorpay.errors import BadRequestError as RazorpayBadRequestError

from adapters.razorpay_gateway import RazorpayGateway
from domain.exceptions import GatewayRequestInvalidError, GatewayUnavailableError


def _gateway(key_secret="key_secret_x", webhook_secret="webhook_secret_y"):
    return RazorpayGateway(
        key_id="rzp_test_x",
        key_secret=key_secret,
        webhook_secret=webhook_secret,
        max_retries=2,
        backoff_base_seconds=0,
    )


class _CountingFailure:
    """Stands in for `self._client.order` — records how many times its
    method was called and always raises `exc`."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    def create(self, *_args, **_kwargs):
        self.calls += 1
        raise self._exc

    def payments(self, *_args, **_kwargs):
        self.calls += 1
        raise self._exc


class TestOrdersCreateErrorHandling:
    def test_bad_request_is_not_retried_and_surfaces_as_request_invalid(self):
        gw = _gateway()
        failure = _CountingFailure(RazorpayBadRequestError("bad amount"))
        gw._client.order = failure

        with pytest.raises(GatewayRequestInvalidError):
            gw.orders_create(amount_paise=50000, receipt="pay_1", notes={})

        assert failure.calls == 1

    def test_transient_error_is_retried_then_surfaces_as_gateway_unavailable(self):
        gw = _gateway()
        failure = _CountingFailure(RuntimeError("connection reset"))
        gw._client.order = failure

        with pytest.raises(GatewayUnavailableError):
            gw.orders_create(amount_paise=50000, receipt="pay_1", notes={})

        # max_retries=2 -> 3 total attempts.
        assert failure.calls == 3


class TestFetchOrderPaymentsErrorHandling:
    def test_bad_request_is_not_retried_and_surfaces_as_request_invalid(self):
        gw = _gateway()
        failure = _CountingFailure(RazorpayBadRequestError("bad order id"))
        gw._client.order = failure

        with pytest.raises(GatewayRequestInvalidError):
            gw.fetch_order_payments("order_1")

        assert failure.calls == 1


class TestWebhookSignature:
    def test_valid_signature_verifies(self):
        gw = _gateway()
        body = b'{"event":"payment.captured"}'
        sig = hmac.new(b"webhook_secret_y", body, hashlib.sha256).hexdigest()
        assert gw.verify_webhook_signature(body, sig) is True

    def test_tampered_body_fails(self):
        gw = _gateway()
        body = b'{"event":"payment.captured"}'
        sig = hmac.new(b"webhook_secret_y", body, hashlib.sha256).hexdigest()
        assert gw.verify_webhook_signature(b'{"event":"payment.failed"}', sig) is False

    def test_wrong_secret_fails(self):
        gw = _gateway()
        body = b'{"event":"payment.captured"}'
        sig = hmac.new(b"wrong_secret", body, hashlib.sha256).hexdigest()
        assert gw.verify_webhook_signature(body, sig) is False

    def test_missing_signature_fails(self):
        assert _gateway().verify_webhook_signature(b"{}", "") is False


class TestClientSignature:
    def test_valid_signature_verifies(self):
        gw = _gateway()
        message = b"order_abc|pay_xyz"
        sig = hmac.new(b"key_secret_x", message, hashlib.sha256).hexdigest()
        assert gw.verify_client_signature("order_abc", "pay_xyz", sig) is True

    def test_wrong_payment_id_fails(self):
        gw = _gateway()
        sig = hmac.new(b"key_secret_x", b"order_abc|pay_xyz", hashlib.sha256).hexdigest()
        assert gw.verify_client_signature("order_abc", "pay_other", sig) is False

    def test_missing_signature_fails(self):
        assert _gateway().verify_client_signature("o", "p", "") is False
