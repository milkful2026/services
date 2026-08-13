"""Cognito adapter — the only place in this service allowed to import
boto3's cognito-idp client (services/README.md §3.7).

Token issuance design (flagged for architect review — see PR description):
this service owns OTP verification itself (DynamoDB, not Cognito's native
custom-auth-challenge Lambda triggers), so after we've independently
verified an OTP or a social idToken, we mint Cognito tokens via
AdminCreateUser -> AdminSetUserPassword(random, permanent) ->
AdminInitiateAuth(USER_PASSWORD_AUTH). The random password is single-use:
generated, set, immediately consumed by AdminInitiateAuth, and never
stored or returned. This pool uses phone_number as the Cognito Username
(UsernameAttributes=[phone_number]), so Username == mobile throughout.
"""

import logging
import secrets
import string

import boto3
from botocore.exceptions import ClientError

from domain.exceptions import (
    ExternalServiceUnavailableError,
    InvalidRefreshTokenError,
    SocialAccountConflictError,
    ValidationError,
)
from domain.models import TokenBundle

logger = logging.getLogger(__name__)

_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*"


def _generate_password(length: int = 32) -> str:
    # Guarantee at least one of each required class, per Cognito's default
    # password policy (upper, lower, number, symbol).
    parts = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    parts += [secrets.choice(_PASSWORD_ALPHABET) for _ in range(length - len(parts))]
    secrets.SystemRandom().shuffle(parts)
    return "".join(parts)


class CognitoAdapter:
    def __init__(
        self,
        user_pool_id: str,
        client_id: str,
        region_name: str,
        correlation_id: str = "",
        endpoint_url: str | None = None,
    ) -> None:
        self._client = boto3.client("cognito-idp", region_name=region_name, endpoint_url=endpoint_url)
        self._user_pool_id = user_pool_id
        self._client_id = client_id
        self._correlation_id = correlation_id

    def _log_and_wrap(self, operation: str, exc: ClientError) -> ExternalServiceUnavailableError:
        logger.error(
            f"cognito_adapter.{operation} failed",
            extra={"correlationId": self._correlation_id, "error": str(exc)},
        )
        return ExternalServiceUnavailableError(f"Cognito {operation} failed")

    def _attrs(self, user_attributes: list[dict]) -> dict[str, str]:
        return {a["Name"]: a["Value"] for a in user_attributes}

    def find_verified_sub_by_phone(self, mobile: str) -> str | None:
        try:
            response = self._client.list_users(
                UserPoolId=self._user_pool_id,
                Filter=f'phone_number = "{mobile}"',
                Limit=1,
            )
        except ClientError as exc:
            raise self._log_and_wrap("list_users", exc) from exc

        users = response.get("Users", [])
        if not users:
            return None
        attrs = self._attrs(users[0].get("Attributes", []))
        if attrs.get("phone_number_verified") == "true":
            return attrs.get("sub")
        return None

    def register_and_issue_tokens(self, mobile: str) -> tuple[TokenBundle, bool]:
        is_new_user = False
        try:
            self._client.admin_get_user(UserPoolId=self._user_pool_id, Username=mobile)
        except self._client.exceptions.UserNotFoundException:
            is_new_user = True
        except ClientError as exc:
            raise self._log_and_wrap("admin_get_user", exc) from exc

        if is_new_user:
            try:
                self._client.admin_create_user(
                    UserPoolId=self._user_pool_id,
                    Username=mobile,
                    UserAttributes=[
                        {"Name": "phone_number", "Value": mobile},
                        {"Name": "phone_number_verified", "Value": "true"},
                    ],
                    MessageAction="SUPPRESS",
                )
            except ClientError as exc:
                raise self._log_and_wrap("admin_create_user", exc) from exc
        else:
            try:
                self._client.admin_update_user_attributes(
                    UserPoolId=self._user_pool_id,
                    Username=mobile,
                    UserAttributes=[{"Name": "phone_number_verified", "Value": "true"}],
                )
            except ClientError as exc:
                raise self._log_and_wrap("admin_update_user_attributes", exc) from exc

        tokens = self.issue_tokens(mobile)
        return tokens, is_new_user

    def find_or_create_federated_user(
        self, provider: str, provider_sub: str, email: str
    ) -> tuple[str, str | None, bool, bool]:
        """Looked up (and, if new, created) by `email` as the Cognito
        Username — NOT a synthetic `{provider}:{provider_sub}` string.

        Real Cognito requires Username to itself be one of the pool's
        UsernameAttributes values (phone_number or email here) whenever
        UsernameAttributes is set; an arbitrary string is rejected with
        InvalidParameterException ("Username should be either an email or
        a phone number"). `email` is required by the caller for this
        reason — a federated user with no email has nothing valid to use
        as a Cognito username in this pool configuration, and callers
        must not have already reached this method in that case.

        The provider's own `sub` is stored as a custom attribute purely
        for audit/support lookup, never as the identity key.
        """
        provider_attr = "custom:google_sub" if provider == "google" else "custom:apple_sub"

        try:
            existing = self._client.admin_get_user(
                UserPoolId=self._user_pool_id, Username=email
            )
        except self._client.exceptions.UserNotFoundException:
            existing = None
        except ClientError as exc:
            raise self._log_and_wrap("admin_get_user", exc) from exc

        if existing is not None:
            attrs = self._attrs(existing["UserAttributes"])
            mobile_verified = attrs.get("phone_number_verified") == "true"
            try:
                self._client.admin_update_user_attributes(
                    UserPoolId=self._user_pool_id,
                    Username=email,
                    UserAttributes=[{"Name": provider_attr, "Value": provider_sub}],
                )
            except ClientError as exc:
                raise self._log_and_wrap("admin_update_user_attributes", exc) from exc
            return (
                attrs.get("sub"),
                attrs.get("phone_number"),
                False,
                mobile_verified,
            )

        # No email-username record exists, but a phone-username record from
        # OTP registration may already own this email — Username lookup
        # above can't find it (Username == mobile there, not email). Check
        # by email attribute before creating a second, disconnected
        # identity for the same person. Full merge UX is an unresolved
        # product decision (README "Deferred / tech debt"); this only
        # prevents silently creating a duplicate account — it does not
        # attempt to link them.
        try:
            response = self._client.list_users(
                UserPoolId=self._user_pool_id,
                Filter=f'email = "{email}"',
                Limit=1,
            )
        except ClientError as exc:
            raise self._log_and_wrap("list_users", exc) from exc
        if response.get("Users"):
            raise SocialAccountConflictError(
                f"{provider} email is already associated with an existing account",
                merge_instruction_code="CONTACT_SUPPORT",
            )

        try:
            self._client.admin_create_user(
                UserPoolId=self._user_pool_id,
                Username=email,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": provider_attr, "Value": provider_sub},
                ],
                MessageAction="SUPPRESS",
            )
            created = self._client.admin_get_user(UserPoolId=self._user_pool_id, Username=email)
        except ClientError as exc:
            raise self._log_and_wrap("admin_create_user", exc) from exc

        attrs = self._attrs(created["UserAttributes"])
        return attrs.get("sub"), None, True, False

    def issue_tokens(self, username: str) -> TokenBundle:
        password = _generate_password()
        try:
            self._client.admin_set_user_password(
                UserPoolId=self._user_pool_id,
                Username=username,
                Password=password,
                Permanent=True,
            )
            auth_response = self._client.admin_initiate_auth(
                UserPoolId=self._user_pool_id,
                ClientId=self._client_id,
                AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": username, "PASSWORD": password},
            )
        except ClientError as exc:
            raise self._log_and_wrap("issue_tokens", exc) from exc

        result = auth_response["AuthenticationResult"]
        return TokenBundle(
            access_token=result["AccessToken"],
            refresh_token=result["RefreshToken"],
            id_token=result["IdToken"],
            expires_in=result["ExpiresIn"],
        )

    def refresh_tokens(self, refresh_token: str) -> TokenBundle:
        try:
            auth_response = self._client.initiate_auth(
                ClientId=self._client_id,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={"REFRESH_TOKEN": refresh_token},
            )
        except self._client.exceptions.NotAuthorizedException as exc:
            raise InvalidRefreshTokenError("Refresh token is invalid or expired") from exc
        except ClientError as exc:
            raise self._log_and_wrap("refresh_tokens", exc) from exc

        result = auth_response["AuthenticationResult"]
        return TokenBundle(
            access_token=result["AccessToken"],
            refresh_token=result.get("RefreshToken", refresh_token),
            id_token=result["IdToken"],
            expires_in=result["ExpiresIn"],
        )

    def revoke_token(self, refresh_token: str) -> None:
        """Revokes only the given refresh token (per-device logout) —
        never AdminUserGlobalSignOut, which would end every session and
        violate the "concurrent sessions allowed" product decision (spec
        MA-21 FR-3). Per that same FR, logout must not fail closed: an
        already-revoked/expired token, or any other Cognito-side error
        revoking it, still results in a 204 to the client — only a
        genuinely malformed token is a real client error (400).
        """
        try:
            self._client.revoke_token(Token=refresh_token, ClientId=self._client_id)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "InvalidParameterException":
                raise ValidationError("Malformed refresh token") from exc
            logger.warning(
                "cognito_adapter.revoke_token non-fatal error (logout still succeeds)",
                extra={"correlationId": self._correlation_id, "error": str(exc)},
            )
