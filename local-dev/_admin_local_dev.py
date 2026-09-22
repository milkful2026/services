"""Local-dev-only compensating adapters for the admin RBAC feature
(MA-129) — for the two things moto and real AWS make impossible to
exercise locally the way production code does. Mirrors peek_otp.py's
own precedent: a local-dev-only workaround for something no real
provider exists for on a developer's own machine, never a stand-in for
the real thing anywhere else.

**1. TOTP MFA challenge.** moto's Cognito does not implement the
SOFTWARE_TOKEN_MFA challenge at all — confirmed empirically: even with
`SetUserPoolMfaConfig(MfaConfiguration="ON",
SoftwareTokenMfaConfiguration={"Enabled": True})` set on the pool,
`AdminInitiateAuth` returns `AuthenticationResult` directly, with no
`ChallengeName` ever appearing. `admin_cognito_adapter.py`'s
`admin_password_auth` deliberately fails closed when no challenge comes
back — a real, correct production safeguard against silently allowing
single-factor login for a privileged identity — which means the real
adapter can *never* complete a login against moto, full stop, no matter
how the pool is configured. `LocalDevAdminCognitoAdapter` wraps the real
adapter: it captures the tokens moto's `AdminInitiateAuth` already
issues (real Cognito tokens, just not gated by a real challenge) behind
a fixed local code instead of a real TOTP code, so the FR-1/FR-2
password-then-2FA flow — and its lockout behavior on a wrong code —
is still exercisable locally with the exact real domain/handler code
otherwise unmodified.

**2. Admin JWT signature verification.** `admin_jwt_verifier.py`
fetches a *real* AWS JWKS URL (`cognito-idp.{region}.amazonaws.com`),
which cannot resolve anything for a fake local pool ID. Confirmed
empirically that moto's issued access tokens do decode to a normal,
usable claims set (`sub`, `client_id`, `token_use`, matching the
Cognito user's real `sub` attribute) — just not to a signature any real
JWKS could ever verify. `LocalDevUnsignedJwtVerifier` decodes the
token's claims without verifying its signature, the same trust model
`_lambda_local_server.py`'s own module docstring already documents for
the built-in consumer JWT authorizer ("moto's own Cognito tokens aren't
properly signed either ... never a stand-in for the real authorizer
anywhere else").

Neither of these touches `identity-auth/src/` — the real handler/
domain/adapter code runs unmodified; only the two Cognito-security-
feature-dependent pieces moto can't emulate are substituted, and only
when running via `run_local.py`.
"""

import secrets

import jwt as pyjwt
from botocore.exceptions import ClientError

LOCAL_MFA_CODE = "123456"  # matches portal-ui's own MOCK_TOTP_CODE, for a consistent dev experience


class LocalDevAdminCognitoAdapter:
    """Wraps a real `AdminCognitoAdapter`. Every method except the two
    MFA-challenge ones delegates straight through unmodified (`set_group`,
    `global_sign_out`, `admin_create_user`, `admin_delete_user`,
    `revoke_refresh_token` all work fine against moto — no MFA involved)."""

    def __init__(self, real_adapter) -> None:
        self._real = real_adapter
        self._pending: dict[str, dict] = {}  # fake session -> moto's real AuthenticationResult

    def admin_password_auth(self, email: str, password: str) -> str:
        from domain.admin_exceptions import IncorrectAdminCredentialsError

        client = self._real._client  # noqa: SLF001 — local-dev-only, reaching into the real
        # adapter's boto3 client deliberately, to bypass admin_password_auth's own
        # (correct, production-only) fail-closed check on ChallengeName.
        try:
            response = client.admin_initiate_auth(
                UserPoolId=self._real._user_pool_id,  # noqa: SLF001
                ClientId=self._real._client_id,  # noqa: SLF001
                AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": email, "PASSWORD": password},
            )
        except (client.exceptions.NotAuthorizedException, client.exceptions.UserNotFoundException) as exc:
            raise IncorrectAdminCredentialsError() from exc
        except ClientError as exc:
            # Mirrors the real adapter's own generic-ClientError handling
            # (throttling, a misconfigured pool, moto acting up) — without
            # this, anything other than the two exceptions above would
            # propagate as a raw botocore exception instead of the
            # structured ExternalServiceUnavailableError the domain/
            # handler layer (and a real developer) expects.
            raise self._real._log_and_wrap("admin_password_auth", exc) from exc  # noqa: SLF001

        fake_session = secrets.token_urlsafe(24)
        self._pending[fake_session] = response["AuthenticationResult"]
        return fake_session

    def respond_to_mfa_challenge(self, email: str, session: str, code: str):  # noqa: ARG002 — email unused, matches real adapter's signature
        from domain.admin_exceptions import ChallengeExpiredError, Invalid2faCodeError
        from domain.admin_models import AdminTokenBundle

        auth_result = self._pending.get(session)
        if auth_result is None:
            raise ChallengeExpiredError()
        if code != LOCAL_MFA_CODE:
            raise Invalid2faCodeError()

        del self._pending[session]
        return AdminTokenBundle(
            access_token=auth_result["AccessToken"],
            refresh_token=auth_result["RefreshToken"],
            id_token=auth_result["IdToken"],
            expires_in=auth_result["ExpiresIn"],
        )

    def __getattr__(self, name):
        return getattr(self._real, name)


class LocalDevUnsignedJwtVerifier:
    """Drop-in for `AdminJwtVerifierAdapter` — same single-method
    interface, no JWKS fetch, no signature check."""

    def verify_access_token(self, token: str) -> dict:
        from domain.admin_exceptions import AdminAuthenticationError

        try:
            return pyjwt.decode(token, options={"verify_signature": False})
        except pyjwt.PyJWTError as exc:
            raise AdminAuthenticationError("Malformed access token") from exc
