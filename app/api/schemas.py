"""Form schemas. Strict: unknown fields are rejected, lengths are bounded."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


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


_PROFILE = Field(pattern=r"^[a-z][a-z0-9_]{1,23}$")


class BotProfileReauth(_Form):
    password: str = Field(min_length=1, max_length=1024)
    profile: str = _PROFILE


class BotProfileConfirm(_Form):
    confirmation: str = Field(max_length=128)
    profile: str = _PROFILE


_AMOUNT = Field(pattern=r"^[0-9]{1,9}(\.[0-9]{1,8})?$")


PairPick = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{1,20}-USDC$")]


class _TradingNumbers(_Form):
    pick: list[PairPick] = Field(default_factory=list, max_length=10)  # the pairs chosen
    levels: int = Field(ge=3, le=20)
    per_grid: str = _AMOUNT
    invested: str = _AMOUNT
    reserve: str = _AMOUNT
    per_order: str = _AMOUNT


class BotTradingReauth(_TradingNumbers):
    password: str = Field(min_length=1, max_length=1024)


class BotTradingConfirm(_TradingNumbers):
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


# ---------------------------------------------------------------- proposals (Phase 7)
class ProposalDisableForm(_Form):
    pass


class ProposalReviewForm(_Form):
    notes: str = Field(min_length=1, max_length=1000)


class ProposalChangeRequestForm(_Form):
    confirmation: str = Field(max_length=200)
    change_type: str = Field(max_length=32)
    impact_assessment: str = Field(max_length=2000)
    ceilings_unaffected: bool = False


class ProposalAttestForm(_Form):
    reference: str | None = Field(default=None, max_length=64)
    report_ids: str | None = Field(default=None, max_length=400)


class ProposalCloseForm(_Form):
    reason: str = Field(max_length=32)
