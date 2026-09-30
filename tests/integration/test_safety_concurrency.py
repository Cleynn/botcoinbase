"""Order authorization is serialized: concurrent attempts cannot both pass the money rules, and a
kill switch, pause or profile change waits for an authorization in flight. Real PostgreSQL."""

from __future__ import annotations

import threading
import time
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from tests.conftest import TestDb
from tests.integration.safety_env import SafetyEnv
from tests.integration.test_capital_profiles import running_with
from tests.integration.test_market_ingest import role_conn
from tests.integration.test_safety_db import bump


def _attempt(safe: SafetyEnv, intent: Any, decision: Any, gate: threading.Barrier) -> str:
    gate.wait(timeout=10)
    try:
        with safe.ctl.tx() as repos:
            repos.safety.add_attempt(
                attempt_id=uuid4(),
                intent_id=intent.id,
                attempt_no=1,
                client_order_id=uuid4(),
                decision_id=decision.id,
                boot_id=safe.boot_id,
                now=safe.clock.now(),
            )
        return "authorized"
    except psycopg.errors.IntegrityConstraintViolation as exc:
        return str(exc)


def test_two_concurrent_authorizations_cannot_both_pass_the_reserve(
    safe: SafetyEnv, db: TestDb
) -> None:
    running_with(safe, db, "expanded", "100")  # reserve 25 of 100; each order is 20 USDC
    for _ in range(2):
        safe.new_attempt(intent=safe.make_intent(qty="0.2"))  # 40 reserved: two more would be 80
    first, second = (safe.make_intent(qty="0.2") for _ in range(2))
    d1, d2 = safe.allow(first.id), safe.allow(second.id)
    inside, release = threading.Event(), threading.Event()
    results: dict[str, str] = {}

    def holder() -> None:  # authorizes, then keeps its transaction open
        with safe.ctl.tx() as repos:
            repos.safety.add_attempt(
                attempt_id=uuid4(), intent_id=first.id, attempt_no=1, client_order_id=uuid4(),
                decision_id=d1.id, boot_id=safe.boot_id, now=safe.clock.now(),
            )  # fmt: skip
            inside.set()
            release.wait(timeout=10)
        results["first"] = "authorized"

    def rival() -> None:  # tries while the first is uncommitted
        results["second"] = _attempt(safe, second, d2, threading.Barrier(1))

    a = threading.Thread(target=holder)
    a.start()
    assert inside.wait(timeout=10)
    b = threading.Thread(target=rival)
    b.start()
    time.sleep(0.6)  # without serialization the rival would already have finished (and passed)
    assert "second" not in results, results  # it is waiting for the first authorization
    release.set()
    a.join(timeout=10)
    b.join(timeout=10)
    assert results["first"] == "authorized"
    assert "RESERVE_BREACH" in results["second"], results  # it saw the first one's reserve
    with safe.ctl.tx() as repos:
        assert len(repos.safety.attempts_in(("AUTHORIZED",), "FAKE")) == 3


def test_a_kill_switch_waits_for_an_authorization_in_flight(safe: SafetyEnv, db: TestDb) -> None:
    safe.running()
    intent = safe.make_intent()
    decision = safe.allow(intent.id)
    inside, release = threading.Event(), threading.Event()
    outcome: dict[str, str] = {}

    def authorizing() -> None:
        with safe.ctl.tx() as repos:
            repos.safety.add_attempt(
                attempt_id=uuid4(),
                intent_id=intent.id,
                attempt_no=1,
                client_order_id=uuid4(),
                decision_id=decision.id,
                boot_id=safe.boot_id,
                now=safe.clock.now(),
            )
            inside.set()  # the row is inserted and the transaction is still open
            release.wait(timeout=10)
        outcome["a"] = "committed"

    thread = threading.Thread(target=authorizing)
    thread.start()
    try:
        assert inside.wait(timeout=10)
        kill_sql, args = bump(
            safe.clock.now(),
            set_=(
                "kill_switch = 'ACTIVE', kill_reason = 'TEST_KILL', "
                f"kill_activated_at = '{safe.clock.now().isoformat()}', bot_state = 'PAUSED'"
            ),
        )
        with role_conn(db, "td_app") as conn:
            conn.execute("SET lock_timeout = '400ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                conn.execute(kill_sql, args)  # waits for the authorization, so it times out here
    finally:
        release.set()
        thread.join(timeout=10)
    assert outcome.get("a") == "committed"
    assert safe.act("kill").kind == "ok"  # once the authorization finished the kill switch works
