"""Admin Cognito Pool adapter (MA-129) — the only place allowed to import
boto3's cognito-idp client for the SECOND, separate Admin User Pool
(spec §11.1's explicit human decision; never the consumer pool that
adapters/cognito_adapter.py owns).

**Known test-fidelity gap** (same category already documented in this
service's README for the consumer pool's `RevokeToken`): moto's
cognito-idp mock does not emulate the TOTP/MFA challenge flow at all —
`admin_initiate_auth` returns `AuthenticationResult` directly even when
the pool has `MfaConfiguration="ON"` and the user has no software token
associated. Tests therefore monkeypatch `admin_initiate_auth` /
`admin_respond_to_auth_challenge` at the boto3-client level to simulate
the real SOFTWARE_TOKEN_MFA challenge/response Cognito performs, the
same technique already used for `revoke_token` in
tests/unit/adapters/test_cognito_adapter.py and the login integration
test. None of this validates real Cognito TOTP semantics — that needs a
human against a real (or LocalStack) pool.
"""

import logging

import boto3
from botocore.exceptions import ClientError

from domain.admin_exceptions import (
    AdminEmailExistsError,
    ChallengeExpiredError,
    IncorrectAdminCredentialsError,
    Invalid2faCodeError,
)
from domain.admin_models import AdminTokenBundle
from domain.exceptions import ExternalServiceUnavailableError

logger = logging.getLogger(__name__)

_ALL_GROUPS = ("Ops", "Finance", "Support", "Marketing", "SuperAdmin")


class AdminCognitoAdapter:
    def __init__(
        self,
        user_pool_id: str,
        client_id: str,
        region_name: str,
        correlation_id: str = "",
    ) -> None:
        self._client = boto3.client("cognito-idp", region_name=region_name)
        self._user_pool_id = user_pool_id
        self._client_id = client_id
        self._correlation_id = correlation_id

    def _log_and_wrap(self, operation: str, exc: ClientError) -> ExternalServiceUnavailableError:
        logger.error(
            f"admin_cognito_adapter.{operation} failed",
            extra={"correlationId": self._correlation_id, "error": str(exc)},
        )
        return ExternalServiceUnavailableError(f"Admin Cognito {operation} failed")

    def admin_password_auth(self, email: str, password: str) -> str:
        """Spec FR-1 — first factor. Never distinguishes "no such user"
        from "wrong password" in the exception it raises, so callers
        never leak account existence through this path."""
        try:
            response = self._client.admin_initiate_auth(
                UserPoolId=self._user_pool_id,
                ClientId=self._client_id,
                AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": email, "PASSWORD": password},
            )
        except (
            self._client.exceptions.NotAuthorizedException,
            self._client.exceptions.UserNotFoundException,
        ) as exc:
            raise IncorrectAdminCredentialsError() from exc
        except ClientError as exc:
            raise self._log_and_wrap("admin_password_auth", exc) from exc

        session = response.get("Session")
        if response.get("ChallengeName") != "SOFTWARE_TOKEN_MFA" or not session:
            # A correctly-configured Admin Pool (TOTP MFA required, spec
            # §6) always challenges here. Anything else means the pool
            # isn't actually enforcing MFA — fail closed rather than
            # silently letting a single-factor login through for a
            # privileged admin identity.
            logger.error(
                "admin_cognito_adapter.admin_password_auth: pool did not "
                "return the expected SOFTWARE_TOKEN_MFA challenge",
                extra={"correlationId": self._correlation_id},
            )
            raise ExternalServiceUnavailableError("Admin pool MFA challenge is misconfigured")
        return session

    def respond_to_mfa_challenge(self, email: str, session: str, code: str) -> AdminTokenBundle:
        """Spec FR-2 — second factor. `ChallengeExpiredError` covers both
        an actually-expired Cognito session and any other reason the
        session itself is no longer valid (distinct from a merely wrong
        code, per spec §9's CHALLENGE_EXPIRED edge case)."""
        try:
            response = self._client.admin_respond_to_auth_challenge(
                UserPoolId=self._user_pool_id,
                ClientId=self._client_id,
                ChallengeName="SOFTWARE_TOKEN_MFA",
                ChallengeResponses={"USERNAME": email, "SOFTWARE_TOKEN_MFA_CODE": code},
                Session=session,
            )
        except self._client.exceptions.CodeMismatchException as exc:
            raise Invalid2faCodeError() from exc
        except (
            self._client.exceptions.NotAuthorizedException,
            self._client.exceptions.ExpiredCodeException,
        ) as exc:
            raise ChallengeExpiredError() from exc
        except ClientError as exc:
            raise self._log_and_wrap("respond_to_mfa_challenge", exc) from exc

        result = response["AuthenticationResult"]
        return AdminTokenBundle(
            access_token=result["AccessToken"],
            refresh_token=result["RefreshToken"],
            id_token=result["IdToken"],
            expires_in=result["ExpiresIn"],
        )

    def admin_create_user(self, email: str, name: str) -> str:
        """Spec FR-3 — AdminCreateUser with no password set (Cognito
        auto-generates one internally; the account sits in
        FORCE_CHANGE_PASSWORD state until the admin completes the
        password-set + TOTP-enrollment invitation). `MessageAction=
        SUPPRESS` — the invite email itself is delegated to the existing
        EventBridge->Notification pattern (spec §3 "no new notification
        channel invented"), not Cognito's own templated email."""
        try:
            self._client.admin_create_user(
                UserPoolId=self._user_pool_id,
                Username=email,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": "name", "Value": name},
                ],
                MessageAction="SUPPRESS",
            )
            created = self._client.admin_get_user(UserPoolId=self._user_pool_id, Username=email)
        except self._client.exceptions.UsernameExistsException as exc:
            raise AdminEmailExistsError() from exc
        except ClientError as exc:
            raise self._log_and_wrap("admin_create_user", exc) from exc

        attrs = {a["Name"]: a["Value"] for a in created["UserAttributes"]}
        return attrs["sub"]

    def admin_delete_user(self, email: str) -> None:
        """Saga compensation (spec FR-3) — idempotent: a retry of the
        compensation step (or a compensation for a user that was somehow
        already removed) must not itself raise."""
        try:
            self._client.admin_delete_user(UserPoolId=self._user_pool_id, Username=email)
        except self._client.exceptions.UserNotFoundException:
            logger.info(
                "admin_cognito_adapter.admin_delete_user: user already absent (idempotent)",
                extra={"correlationId": self._correlation_id},
            )
        except ClientError as exc:
            raise self._log_and_wrap("admin_delete_user", exc) from exc

    def set_group(self, email: str, role: str) -> None:
        """Ensures Cognito group membership mirrors exactly one role
        (spec §11.3 — Aurora's `role` column and Cognito Group membership
        are two sources of truth kept in sync on every role change)."""
        try:
            for group in _ALL_GROUPS:
                if group == role:
                    continue
                # AdminRemoveUserFromGroup on a group the user isn't a
                # member of is a documented no-op in real Cognito, not an
                # error — no need to check membership first.
                self._client.admin_remove_user_from_group(
                    UserPoolId=self._user_pool_id, Username=email, GroupName=group
                )
            self._client.admin_add_user_to_group(
                UserPoolId=self._user_pool_id, Username=email, GroupName=role
            )
        except ClientError as exc:
            raise self._log_and_wrap("set_group", exc) from exc

    def global_sign_out(self, email: str) -> None:
        """Spec FR-4 deactivate — AdminUserGlobalSignOut revokes every
        refresh token for this admin immediately."""
        try:
            self._client.admin_user_global_sign_out(UserPoolId=self._user_pool_id, Username=email)
        except ClientError as exc:
            raise self._log_and_wrap("global_sign_out", exc) from exc

    def revoke_refresh_token(self, refresh_token: str) -> None:
        """Single-token revocation for role-change invalidation (FR-4)
        and max-concurrent-session LRU eviction (FR-5). Same non-fatal-
        on-error semantics as the consumer CognitoAdapter.revoke_token:
        this is a defense-in-depth/best-effort action, and moto does not
        implement RevokeToken at all (raises a raw NotImplementedError,
        not even a ClientError) — see this service's README "Known
        test-fidelity gaps"."""
        try:
            self._client.revoke_token(Token=refresh_token, ClientId=self._client_id)
        except ClientError as exc:
            logger.warning(
                "admin_cognito_adapter.revoke_refresh_token non-fatal error",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
