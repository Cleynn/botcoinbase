"""A small harness for driving pairs through their lifecycle in tests (service level)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.auth.audit import AuditWriter
from app.domain.enums import ActorRole
from app.domain.models import AuditActor, AuthContext
from app.domain.pairs import ActorClass, PairAction, PairState, phrase_for
from app.pairs.runner import PairRunner
from app.pairs.service import Outcome, PairService, RuntimeGate
from app.pairs.transitions import Actor
from app.storage.database import Storage
from tests.coinbase_fakes import FakeCoinbase
from tests.conftest import Account, FakeClock


class Reauth:
    """Stand-in for the single-use reauthentication: `available` is consumed by each success."""

    def __init__(self) -> None:
        self.available = True
        self.consumed = 0

    def consume(self, _repos: Any, _ctx: Any) -> bool:
        if self.available:
            self.available = False
            self.consumed += 1
            return True
        return False

    def fresh(self, _ctx: Any) -> bool:
        return self.available


@dataclass
class PairEnv:
    storage: Storage
    ctl_storage: Storage
    clock: FakeClock
    service: PairService
    runner: PairRunner
    coinbase: FakeCoinbase
    admin: Account
    reauth: Reauth
    ctx: AuthContext = field(default=None)  # type: ignore[assignment]

    @property
    def actor(self) -> Actor:
        return Actor(
            AuditActor(self.admin.user.id, ActorRole.ADMIN), ActorClass.WEB, "t" * 12, "req-1"
        )

    # ------------------------------------------------------------------ building blocks
    def discover(self) -> None:
        self.runner.discover()

    def product_uuid(self, product_id: str) -> UUID:
        with self.storage.tx() as repos:
            product = repos.products.get_by_product_id(product_id)
            assert product is not None, product_id
            return product.id

    def propose(self, product_id: str = "BTC-USDC") -> UUID:
        outcome = self.service.propose(self.actor, self.product_uuid(product_id))
        assert outcome.kind == "ok" and outcome.pair_id, outcome
        return outcome.pair_id

    def get(self, pair_id: UUID) -> Any:
        with self.storage.tx() as repos:
            pair = repos.pairs.get(pair_id)
        assert pair is not None
        return pair

    def state(self, pair_id: UUID) -> PairState:
        state: PairState = self.get(pair_id).state
        return state

    def queue(self, pair_id: UUID) -> Outcome:
        return self.service.simple_action(
            self.actor, pair_id, PairAction.VALIDATE, self.get(pair_id).version
        )

    def validate(self) -> list[Any]:
        return self.runner.validate_pending()

    def eligible(self, product_id: str = "BTC-USDC") -> UUID:
        """Discover, propose, queue and validate until PAPER_ELIGIBLE."""
        self.discover()
        pair_id = self.propose(product_id)
        assert self.queue(pair_id).kind == "ok"
        self.validate()
        assert self.state(pair_id) is PairState.PAPER_ELIGIBLE, self.state(pair_id)
        return pair_id

    def act(self, pair_id: UUID, action: PairAction, *, typed: str | None = None) -> Outcome:
        pair = self.get(pair_id)
        phrase = typed if typed is not None else (phrase_for(action, pair.product_id) or "")
        self.reauth.available = True
        return self.service.confirm(self.ctx, self.actor, pair_id, action, pair.version, phrase)

    def make(self, product_id: str, target: PairState) -> UUID:
        """Bring a fresh pair to `target` through legal transitions only."""
        if target is PairState.PROPOSED:
            self.discover()
            return self.propose(product_id)
        pair_id = self.eligible(product_id)
        if target is PairState.PAPER_ELIGIBLE:
            return pair_id
        if target is PairState.PAUSED:
            assert self.act(pair_id, PairAction.ACTIVATE).kind == "ok"
            paused = self.service.simple_action(
                self.actor, pair_id, PairAction.PAUSE, self.get(pair_id).version
            )
            assert paused.kind == "ok", paused
        else:
            action = {
                PairState.PAPER_ACTIVE: PairAction.ACTIVATE,
                PairState.DISABLED: PairAction.DISABLE,
                PairState.ARCHIVED: PairAction.ARCHIVE,
            }[target]
            assert self.act(pair_id, action).kind == "ok"
        assert self.state(pair_id) is target
        return pair_id


def build_env(
    *,
    storage: Storage,
    ctl_storage: Storage,
    clock: FakeClock,
    settings: Any,
    coinbase: FakeCoinbase,
    admin: Account,
    gate: RuntimeGate,
) -> PairEnv:
    reauth = Reauth()
    service = PairService(
        storage=storage,
        clock=clock,
        policy=settings.pair_policy,
        audit=AuditWriter(clock),
        gate=gate,
        consume_reauth=reauth.consume,
        reauth_active=reauth.fresh,
    )
    runner = PairRunner(
        storage=ctl_storage, clock=clock, settings=settings, client=coinbase.client()
    )
    return PairEnv(storage, ctl_storage, clock, service, runner, coinbase, admin, reauth)
