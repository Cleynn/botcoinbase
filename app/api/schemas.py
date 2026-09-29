"""Form schemas. Strict: unknown fields are rejected, lengths are bounded."""

from __future__ import annotations

from datetime import date
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


class ProposeForm(_Form):
    product_id: UUID  # the discovered product's UUID, never free text


class PairActionForm(_Form):
    version: int = Field(ge=1, le=2**31 - 1)


class PairReauthForm(_Form):
    password: str = Field(min_length=1, max_length=1024)
    version: int = Field(ge=1, le=2**31 - 1)


class PairConfirmForm(_Form):
    version: int = Field(ge=1, le=2**31 - 1)
    confirmation: str = Field(max_length=128)


# ---------------------------------------------------------------- review packages (Phase 6)
_SCOPE_CHOICES = Literal[
    "backtests", "paper", "pairs", "data_quality", "grid_plans", "audit_summary"
]


class ReviewEnableFields(_Form):
    retention_days: int = Field(ge=1, le=90)


class ReviewEnableReauth(ReviewEnableFields):
    password: str = Field(min_length=1, max_length=1024)


class ReviewEnableConfirm(ReviewEnableFields):
    confirmation: str = Field(max_length=128)


class ReviewPlainReauth(_Form):
    password: str = Field(min_length=1, max_length=1024)


class ReviewPlainConfirm(_Form):
    confirmation: str = Field(max_length=128)


class ReviewRequestFields(_Form):
    period_start: date
    period_end: date
    scope: list[_SCOPE_CHOICES] = Field(min_length=1, max_length=6)


class ReviewRequestReauth(ReviewRequestFields):
    password: str = Field(min_length=1, max_length=1024)


class ReviewRequestConfirm(ReviewRequestFields):
    confirmation: str = Field(max_length=128)


class ReviewPackageAction(_Form):
    pass
