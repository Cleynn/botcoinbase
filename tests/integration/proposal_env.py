"""Harness for proposal tests (real database, real review package, SYNTHETIC content)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from app.auth.audit import AuditWriter
from app.config import Settings
from app.domain.enums import ActorRole
from app.domain.models import AuditActor, AuthContext
from app.domain.pairs import ActorClass
from app.pairs.transitions import Actor
from app.proposals.service import (
    PHRASE_ENABLE,
    PHRASE_IMPORT,
    Outcome,
    ProposalService,
    phrase_change_request,
)
from app.proposals.validator import ProposalValidator, ValidationResult
from app.storage.database import Storage
from tests.conftest import Account, FakeClock
from tests.integration.review_env import ReviewEnv
from tests.pair_env import Reauth

# every table an approved proposal must leave exactly as it found it
UNTOUCHED = (
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
    "review_settings",
    "review_packages",
    "users",
    "sessions",
)


@dataclass
class ProposalEnv:
    service: ProposalService
    validator: ProposalValidator
    settings: Settings
    clock: FakeClock
    reauth: Reauth
    admin: Account
    ctx: AuthContext
    actor: Actor
    directory: Path
    package: Any  # the READY review package row the proposals link to

    def enable(self) -> Outcome:
        self.reauth.available = True
        return self.service.enable_import(self.ctx, self.actor, PHRASE_ENABLE)

    def document(self, **changes: Any) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "proposal_version": 1,
            "proposal_id": "prop-2026-001",
            "linked_review_package_id": str(self.package.id),
            "linked_review_package_sha256": self.package.package_sha256,
            "category": "strategy",
            "summary": "Require a wider minimum band before the grid is built.",
            "evidence_references": ["data/backtest_runs.jsonl#L1", "efficiency_summary.json"],
            "assumptions": ["Fees stay at the attested rate."],
            "suggested_change": (
                "Raise the minimum band ratio from 0.02 to 0.03 so cells clear the fees."
            ),
            "expected_benefit": "Fewer fee-negative cycles in the stress scenario.",
            "risk_tradeoffs": "Fewer trading windows and lower activity overall.",
            "required_validation": "Walk-forward backtest on a frozen snapshot, then a paper run.",
            "rollback_plan": (
                "Revert the parameter through a reviewed release and rerun the backtest."
            ),
            "should_remain_no_trade_until_validated": True,
            "no_profit_guarantee": True,
        }
        doc.update(changes)
        return doc

    def import_bytes(
        self,
        data: bytes,
        *,
        mime: str = "application/json",
        filename: str | None = "proposal.json",
        typed: str = PHRASE_IMPORT,
        fresh: bool = True,
    ) -> Outcome:
        self.reauth.available = fresh
        return self.service.import_proposal(
            self.ctx, self.actor, typed, declared_mime=mime, filename=filename, data=data
        )

    def import_doc(self, doc: dict[str, Any] | None = None, **kw: Any) -> UUID:
        outcome = self.import_bytes(json.dumps(doc or self.document()).encode(), **kw)
        assert outcome.kind == "ok" and outcome.proposal_id, outcome
        return outcome.proposal_id

    def validated(self, doc: dict[str, Any] | None = None) -> UUID:
        pid = self.import_doc(doc)
        (result,) = self.validator.run()
        assert result.state == "VALIDATED", result
        return pid

    def validate(self) -> list[ValidationResult]:
        return self.validator.run()

    def review(self, pid: UUID, notes: str = "Looks worth a manual look.") -> Outcome:
        return self.service.review(self.actor, pid, notes)

    def change_request(self, pid: UUID, **kw: Any) -> Outcome:
        self.reauth.available = kw.pop("fresh", True)
        return self.service.create_change_request(
            self.ctx,
            self.actor,
            pid,
            kw.pop("typed", phrase_change_request(pid)),
            change_type=kw.pop("change_type", "PARAMETER_CHANGE"),
            impact=kw.pop("impact", "Affects the band filter only; fees and ceilings unchanged."),
            ceilings_unaffected=kw.pop("ceilings", True),
        )

    def attest(self, pid: UUID, kind: str, **kw: Any) -> Outcome:
        self.reauth.available = kw.pop("fresh", True)
        return self.service.attest(self.ctx, self.actor, pid, kind, **kw)

    def state(self, sql: Any, pid: UUID) -> str:
        return str(sql("SELECT state FROM proposals WHERE id = %s", (pid,))[0]["state"])


def build_proposal_env(
    *,
    storage: Storage,
    ctl_storage: Storage,
    clock: FakeClock,
    settings: Settings,
    admin: Account,
    directory: Path,
    review: ReviewEnv,
    package: Any,
) -> ProposalEnv:
    props = settings.proposals.model_copy(update={"dir": str(directory)})
    settings = settings.model_copy(update={"proposals": props, "review": review.settings.review})
    reauth = Reauth()
    service = ProposalService(
        storage=storage,
        clock=clock,
        settings=settings,
        audit=AuditWriter(clock),
        consume_reauth=reauth.consume,
        reauth_active=reauth.fresh,
    )
    validator = ProposalValidator(storage=ctl_storage, clock=clock, settings=settings)
    ctx = cast(AuthContext, SimpleNamespace(user=admin.user))
    actor = Actor(AuditActor(admin.user.id, ActorRole.ADMIN), ActorClass.WEB, "t" * 12, "req-1")
    return ProposalEnv(
        service, validator, settings, clock, reauth, admin, ctx, actor, directory, package
    )
