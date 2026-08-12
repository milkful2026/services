"""Social auth orchestration (FR-3).

No AWS SDK / HTTP imports here — only the abstract adapter Protocols.
"""

from adapters.interfaces import CognitoPort, SocialTokenVerifierPort
from domain.exceptions import ValidationError
from domain.models import SocialAuthResult


class SocialLinkService:
    def __init__(self, token_verifier: SocialTokenVerifierPort, cognito: CognitoPort) -> None:
        self._token_verifier = token_verifier
        self._cognito = cognito

    def authenticate(self, provider: str, id_token: str) -> SocialAuthResult:
        claims = self._token_verifier.verify(provider, id_token)

        email = claims.get("email")
        if not email:
            raise ValidationError(f"{provider} idToken did not include an email address")

        provider_sub = claims["sub"]
        sub, mobile, is_new_user, mobile_verified = self._cognito.find_or_create_federated_user(
            provider, provider_sub, email
        )

        if not mobile_verified:
            # Flagged G1 (spec Q2): linking this social identity to a
            # pre-existing mobile-verified account by matching email is an
            # unresolved product decision — not implemented. partial_token
            # is a placeholder identifier, not a signed credential, and no
            # endpoint currently accepts it back (FR-2's request contract
            # has no partialToken field) — this branch is inert until that
            # follow-up is scoped, documented as tech debt in the PR.
            return SocialAuthResult(
                requires_mobile_verification=True,
                partial_token=f"partial:{sub}",
                is_new_user=is_new_user,
            )

        tokens = self._cognito.issue_tokens(mobile)
        return SocialAuthResult(tokens=tokens, is_new_user=is_new_user)
