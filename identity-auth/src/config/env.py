"""Environment configuration, validated at import time (cold start).

Per services/README.md §3: config is read at the composition root only —
never deep inside domain modules — and every value comes from env vars /
Secrets Manager, never hardcoded.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Optional — only present in local dev, written by
# services/local-dev/bootstrap.py. Silently ignored by pydantic-settings
# when absent, so this has no effect on tests or real deployments.
_LOCAL_ENV_FILE = Path(__file__).resolve().parents[2] / ".env.local"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IDENTITY_AUTH_", env_file=_LOCAL_ENV_FILE, env_file_encoding="utf-8"
    )

    # Cognito
    cognito_user_pool_id: str
    cognito_client_id: str
    aws_region: str = "ap-south-1"
    # Local dev only: points boto3 at moto_server instead of real AWS.
    # None in every deployed environment, so behavior there is unchanged.
    aws_endpoint_url: str | None = None

    # DynamoDB
    otp_requests_table_name: str

    # Redis (rate limiting)
    redis_host: str
    redis_port: int = 6379
    redis_use_tls: bool = True

    # EventBridge
    event_bus_name: str = "default"
    event_source: str = "identity-auth"

    # OTP business rules
    otp_length: int = 6
    otp_ttl_seconds: int = 300
    otp_resend_after_seconds: int = 30
    otp_max_attempts: int = 3
    otp_rate_limit_max_requests: int = 3
    otp_rate_limit_window_seconds: int = 900

    # Social auth
    google_client_id: str = ""
    apple_client_id: str = ""
    google_jwks_url: str = "https://www.googleapis.com/oauth2/v3/certs"
    apple_jwks_url: str = "https://appleid.apple.com/auth/keys"
    jwks_cache_ttl_seconds: int = 3600


def get_settings() -> Settings:
    """Instantiated lazily so tests can inject env vars before first access."""
    return Settings()
