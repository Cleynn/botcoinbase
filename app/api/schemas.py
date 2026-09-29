"""Form schemas. Strict: unknown fields are rejected, lengths are bounded."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Form(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    csrf_token: str = Field(max_length=128)


class LoginForm(_Form):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class LogoutForm(_Form):
    pass


class PasswordChangeForm(_Form):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)
    confirm_password: str = Field(min_length=1, max_length=1024)


class ReauthForm(_Form):
    password: str = Field(min_length=1, max_length=1024)


class RevokeOthersForm(_Form):
    pass


class RevokeSessionForm(_Form):
    session_id: UUID


class RevokeUserSessionsForm(_Form):
    user_id: UUID
    confirmation: str = Field(max_length=128)


class AuditQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str | Literal[""] = Field(default="", max_length=64)
    before: int | None = Field(default=None, ge=1, le=2**62)
