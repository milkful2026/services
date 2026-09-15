"""Signature verification against known vectors — no network, no real
Razorpay SDK calls."""

import hashlib
import hmac

from adapters.razorpay_gateway import RazorpayGateway


def _gateway(key_secret="key_secret_x", webhook_secret="webhook_secret_y"):
    return RazorpayGateway(
        key_id="rzp_test_x", key_secret=key_secret, webhook_secret=webhook_secret
    )


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
