"""Harness for review package tests (real database, SYNTHETIC data)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from app.auth.audit import AuditWriter
from app.config import Settings
from app.domain.enums import ActorRole
from app.domain.models import AuditActor, AuthContext
from app.domain.pairs import ActorClass
from app.pairs.transitions import Actor
from app.review.builder import ReviewBuilder
from app.review.service import PHRASES, Outcome, ReviewService
from app.storage.database import Storage
from tests.conftest import Account, FakeClock
from tests.pair_env import Reauth

TABLES = (
    "pairs",
    "pair_state_history",
    "products",
    "product_metadata_current",
    "candles",
    "dataset_snapshots",
    "backtest_runs",
    "reports",
    "grid_plans",
    "paper_session",
    "paper_orders",
    "paper_fills",
    "paper_ledger_entries",
    "paper_positions",
    "users",
)


@dataclass
class ReviewEnv:
    service: ReviewService
    builder: ReviewBuilder
    settings: Settings
    clock: FakeClock
    reauth: Reauth
    admin: Account
    ctx: AuthContext
    actor: Actor
    review_dir: Path

    def chain(self, action: str, **kw: Any) -> Outcome:
        """Run one full-chain action with a fresh reauthentication and the exact phrase."""
        self.reauth.available = True
        if action == "enable":
            return self.service.enable(
                self.ctx, self.actor, kw.get("typed", PHRASES["enable"]), kw.get("days", 14)
            )
        if action == "disable":
            return self.service.disable(self.ctx, self.actor, kw.get("typed", PHRASES["disable"]))
        return self.service.request(
            self.ctx,
            self.actor,
            kw.get("typed", PHRASES["create"]),
            kw["start"],
            kw["end"],
            kw.get("scope", ["backtests", "audit_summary"]),
        )


def build_review_env(
    *,
    storage: Storage,
    ctl_storage: Storage,
    clock: FakeClock,
    settings: Settings,
    admin: Account,
    review_dir: Path,
) -> ReviewEnv:
    review = settings.review.model_copy(update={"dir": str(review_dir)})
    settings = settings.model_copy(update={"review": review})
    reauth = Reauth()
    service = ReviewService(
        storage=storage,
        clock=clock,
        settings=settings,
        audit=AuditWriter(clock),
        consume_reauth=reauth.consume,
        reauth_active=reauth.fresh,
    )
    builder = ReviewBuilder(storage=ctl_storage, clock=clock, settings=settings)
    ctx = cast(AuthContext, SimpleNamespace(user=admin.user))
    actor = Actor(AuditActor(admin.user.id, ActorRole.ADMIN), ActorClass.WEB, "t" * 12, "req-1")
    return ReviewEnv(service, builder, settings, clock, reauth, admin, ctx, actor, review_dir)
