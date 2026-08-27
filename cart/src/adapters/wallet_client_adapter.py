"""Client for Wallet Service's balance read (MA-121 FR-6). **No real
contract exists to code against** — MA-100 (Wallet Service) has no spec
of its own anywhere in this repo (`specs/services/tasks/MA/` has no
`MA-100` directory, as of 2026-08-27), unlike Pricing & Offer (MA-101),
which at least shipped a scoped-down real implementation.

This adapter always raises `WalletCheckUnavailableError` — the same
failure mode MA-121 §9 documents for "Wallet balance check fails
mid-request" — rather than making an HTTP call to an invented endpoint
shape that would just be guessed. `cart_service.py`'s FR-6 wallet gate
already has to handle this exception for the real case (Wallet
temporarily down); today, it's the *only* case. Once MA-100 ships a
real spec, this becomes a normal `HttpWalletClient` following
`pricing_client_adapter.py`'s shape — not a rewrite of the call sites
that use `WalletClientPort`.
"""

from domain.exceptions import WalletCheckUnavailableError


class HttpWalletClient:
    def __init__(self, correlation_id: str = "") -> None:
        self._correlation_id = correlation_id

    def set_correlation_id(self, correlation_id: str) -> None:
        self._correlation_id = correlation_id

    def get_balance(self, cognito_sub: str) -> int:
        raise WalletCheckUnavailableError(
            "Wallet Service does not exist yet (MA-100 has no spec or implementation)",
            details={"cognitoSub": cognito_sub},
        )
