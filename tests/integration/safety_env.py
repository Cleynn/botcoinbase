"""Harness for safety-machinery tests: a real database, a real active PAPER pair with SYNTHETIC
candles, and the scripted FAKE exchange (a test double: no market, no matching, no fills unless a
test injects them). Nothing here talks to any network."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg

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
from app.storage.safety_repositories import DecisionRow, IntentRow
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

    def recover(self, *, new_process: bool = False) -> RecoveryResult:
        """Recovery for this process (its fixed boot id), or for a brand-new process."""
        if new_process:
            self.boot_id = str(uuid4())
        return self.recovery.run(self.boot_id)

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

    # ---------------------------------------------------------------- direct rows (host role)
    def make_intent(
        self, *, side: str = "BUY", price: str = "100", qty: str = "0.01", key: str | None = None
    ) -> IntentRow:
        row = IntentRow(
            id=uuid4(),
            intent_key=key or uuid4().hex + uuid4().hex,
            venue="FAKE",
            pair_id=self.pair_id,
            product_id="BTC-USDC",
            side=side,
            order_type="limit_limit_gtc",
            post_only=True,
            price=Decimal(price),
            base_qty=Decimal(qty),
            expected_cycle_return=Decimal("0.03"),
            source="test",
            created_at=self.clock.now(),
        )
        with self.ctl.tx() as repos:
            repos.safety.add_intent(row)
        return row

    def allow(self, intent_id: UUID, *, decision: str = "ALLOW") -> DecisionRow:
        now = self.clock.now()
        row = DecisionRow(
            id=uuid4(),
            intent_id=intent_id,
            decision=decision,
            reasons=() if decision == "ALLOW" else ("KILL_SWITCH_ACTIVE",),
            inputs_hash="0" * 64,
            decided_at=now,
            expires_at=now + timedelta(seconds=30),
            consumed_at=None,
        )
        with self.ctl.tx() as repos:
            repos.safety.add_decision(row)
        return row

    def new_attempt(self, *, intent: IntentRow | None = None, no: int = 1) -> UUID:
        """An AUTHORIZED attempt (the bot must be RUNNING with a fresh reconciliation)."""
        intent = intent or self.make_intent()
        decision = self.allow(intent.id)
        attempt_id = uuid4()
        with self.ctl.tx() as repos:
            repos.safety.add_attempt(
                attempt_id=attempt_id,
                intent_id=intent.id,
                attempt_no=no,
                client_order_id=uuid4(),
                decision_id=decision.id,
                boot_id=self.boot_id,
                now=self.clock.now(),
            )
        return attempt_id

    def walk(self, attempt_id: UUID, *states: str) -> None:
        """Move an attempt through legal states (SUBMITTING sets the submit mark first)."""
        for state in states:
            with self.ctl.tx() as repos:
                if state == "SUBMITTING":
                    assert repos.safety.mark_submitting(attempt_id, self.clock.now())
                else:
                    row = repos.safety.attempt(attempt_id)
                    assert row is not None
                    code = "TEST_REJECT" if state == "REJECTED" else None
                    assert repos.safety.transition(
                        attempt_id, (row.state,), state, self.clock.now(),
                        exchange_order_id="ord-test-0001" if state == "WORKING" else None,
                        failure_code=code,
                    )  # fmt: skip

    def control_row(self) -> Any:
        with self.ctl.tx() as repos:
            return repos.safety.control()

    def attempt(self, attempt_id: UUID) -> Any:
        with self.ctl.tx() as repos:
            return repos.safety.attempt(attempt_id)


class DbClock:
    """Moves the database's test clock (owner-only table, empty in every deployment)."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def __call__(self, now: Any) -> None:
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO td_test_clock (id, value) VALUES (true, %s) "
                "ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value",
                (now,),
            )


def build_safety_env(
    *,
    settings: Settings,
    storage: Storage,
    ctl_storage: Storage,
    clock: FakeClock,
    admin: Account,
    pair_id: UUID,
    owner_conninfo: str,
) -> SafetyEnv:
    set_clock = DbClock(owner_conninfo)
    set_clock(clock.now())
    clock.hooks.append(set_clock)
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
