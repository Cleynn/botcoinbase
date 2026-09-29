"""Harness for safety-machinery tests: a real database, a real active PAPER pair with SYNTHETIC
candles, and the scripted FAKE exchange (a test double: no market, no matching, no fills unless a
test injects them). Nothing here talks to any network."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from app.auth.audit import AuditWriter
from app.config import Settings
from app.domain.enums import ActorRole
from app.domain.models import AuditActor, AuthContext
from app.domain.pairs import ActorClass
from app.exchange.fake import FakeExchange
from app.exchange.reader import RecordingReader, RetryingReader
from app.pairs.transitions import Actor
from app.safety.commands import CommandRunner
from app.safety.context import BookFacts
from app.safety.control import PHRASES, ControlService, Outcome
from app.safety.host_control import HostControl
from app.safety.monitor import SafetyMonitor
from app.safety.pipeline import OrderPipeline
from app.safety.reconciler import Reconciler, RunResult
from app.safety.recovery import RecoveryResult, RecoveryService
from app.safety.retry import RetryPolicy
from app.safety.types import OrderProposal
from app.storage.database import Storage
from tests.conftest import Account, FakeClock
from tests.pair_env import Reauth

# every table an operator control action or a reconciliation must leave exactly as it found it
BOT_TABLES = (
    "pairs",
    "pair_state_history",
    "products",
    "candles",
    "paper_orders",
    "paper_fills",
    "paper_ledger_entries",
    "paper_positions",
    "grid_plans",
    "users",
)


@dataclass
class SafetyEnv:
    settings: Settings
    web_storage: Storage
    ctl: Storage
    clock: FakeClock
    fake: FakeExchange
    reauth: Reauth
    admin: Account
    ctx: AuthContext
    actor: Actor
    pair_id: UUID
    control: ControlService
    host: HostControl
    reconciler: Reconciler
    recovery: RecoveryService
    monitor: SafetyMonitor
    commands: CommandRunner
    pipeline: OrderPipeline
    boot_id: str
    sleeps: list[float]

    # ---------------------------------------------------------------- scripting
    def baseline(self, usdc: str = "50") -> None:
        with self.ctl.tx() as repos:
            repos.safety.add_baseline("FAKE", "USDC", Decimal(usdc), self.clock.now())
        self.fake.set_balance("USDC", usdc)

    def recon(self, trigger: str = "MANUAL") -> RunResult:
        return self.reconciler.run(trigger)

    def recover(self) -> RecoveryResult:
        return self.recovery.run()

    def act(self, action: str, *, fresh: bool = True, typed: str | None = None) -> Outcome:
        self.reauth.available = fresh
        return self.control.execute(
            self.ctx, self.actor, action, PHRASES[action] if typed is None else typed
        )

    def running(self) -> None:
        """Baseline, recovery, a fresh reconciliation, then the ADMIN resume chain."""
        self.baseline()
        assert self.recover().complete
        assert self.act("resume").kind == "ok"

    def proposal(
        self,
        *,
        side: str = "BUY",
        price: str = "100",
        qty: str = "0.1",
        edge: str = "0.03",
    ) -> OrderProposal:
        return OrderProposal(
            venue="FAKE",
            pair_id=str(self.pair_id),
            product_id="BTC-USDC",
            side=side,
            price=Decimal(price),
            base_qty=Decimal(qty),
            expected_cycle_return=Decimal(edge),
        )

    def book(self, spread_bps: str = "10", age: int = 5) -> BookFacts:
        return BookFacts(Decimal(spread_bps), age)

    def control_row(self) -> Any:
        with self.ctl.tx() as repos:
            return repos.safety.control()

    def attempt(self, attempt_id: UUID) -> Any:
        with self.ctl.tx() as repos:
            return repos.safety.attempt(attempt_id)


def build_safety_env(
    *,
    settings: Settings,
    storage: Storage,
    ctl_storage: Storage,
    clock: FakeClock,
    admin: Account,
    pair_id: UUID,
) -> SafetyEnv:
    fake = FakeExchange(clock.now)
    sleeps: list[float] = []
    policy = RetryPolicy.from_settings(settings.safety)

    def sink(operation: str, ok: bool, code: str | None) -> None:
        with ctl_storage.tx() as repos:
            repos.safety.add_api_event("FAKE", operation, ok, code, clock.now())

    reader = RetryingReader(
        RecordingReader(fake, sink), policy, sleep=sleeps.append, rng=lambda: 0.5
    )
    reconciler = Reconciler(
        storage=ctl_storage, clock=clock, settings=settings, reader=reader, venue="FAKE"
    )
    reauth = Reauth()
    control = ControlService(
        storage=storage,
        clock=clock,
        settings=settings,
        audit=AuditWriter(clock),
        consume_reauth=reauth.consume,
        reauth_active=reauth.fresh,
    )
    host = HostControl(storage=ctl_storage, clock=clock, settings=settings)
    recovery = RecoveryService(
        storage=ctl_storage, clock=clock, settings=settings, reconciler=reconciler
    )
    monitor = SafetyMonitor(storage=ctl_storage, clock=clock, settings=settings, host=host)
    commands = CommandRunner(
        storage=ctl_storage, clock=clock, settings=settings, gateways={"FAKE": fake}
    )
    boot_id = "00000000-0000-4000-8000-00000000b007"
    pipeline = OrderPipeline(
        storage=ctl_storage,
        clock=clock,
        settings=settings,
        gateway=fake,
        boot_id=boot_id,
        reconcile=reconciler.run,
    )
    ctx = cast(AuthContext, SimpleNamespace(user=admin.user))
    actor = Actor(AuditActor(admin.user.id, ActorRole.ADMIN), ActorClass.WEB, "t" * 12, "req-1")
    return SafetyEnv(
        settings, storage, ctl_storage, clock, fake, reauth, admin, ctx, actor, pair_id, control,
        host, reconciler, recovery, monitor, commands, pipeline, boot_id, sleeps,
    )  # fmt: skip
