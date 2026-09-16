"""Request DTOs for the admin auth endpoints (FR-1/FR-2). Response
envelopes are the shared helpers in handlers/dto.py (success_response /
error_response) — reused, not duplicated."""

from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    email: str
    password: str


class Admin2faVerifyRequest(BaseModel):
    challenge_token: str = Field(alias="challengeToken")
    code: str

    model_config = {"populate_by_name": True}
