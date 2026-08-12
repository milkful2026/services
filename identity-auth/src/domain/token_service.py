"""Thin refresh-token passthrough (FR-4). Kept as a domain module (rather
than calling the adapter directly from the handler) to respect the fixed
Handler -> Domain -> Adapters dependency direction, even though there's
no business rule to add beyond delegation."""

from adapters.interfaces import CognitoPort
from domain.models import TokenBundle


class TokenService:
    def __init__(self, cognito: CognitoPort) -> None:
        self._cognito = cognito

    def refresh(self, refresh_token: str) -> TokenBundle:
        return self._cognito.refresh_tokens(refresh_token)
