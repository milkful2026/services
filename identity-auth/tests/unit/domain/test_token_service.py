from domain.models import TokenBundle
from domain.token_service import TokenService


class FakeCognito:
    def __init__(self):
        self.calls = []

    def refresh_tokens(self, refresh_token: str) -> TokenBundle:
        self.calls.append(refresh_token)
        return TokenBundle(access_token="a", refresh_token="r2", id_token="i", expires_in=900)


def test_refresh_delegates_to_cognito_adapter():
    cognito = FakeCognito()
    service = TokenService(cognito)

    tokens = service.refresh("old-refresh-token")

    assert tokens.access_token == "a"
    assert cognito.calls == ["old-refresh-token"]
