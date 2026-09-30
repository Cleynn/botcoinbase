"""Regression tests for the security review (B1, H1, H2, H3, M1, M2): the database clock is the
only trusted clock, restart safety is bound to a boot id, and the money rules are recomputed in SQL.
SYNTHETIC rows only."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest

from tests.conftest import TestDb
from tests.integration.safety_env import SafetyEnv
from tests.integration.test_safety_db import DENIED, VIOLATION, bump, refuse, run


# ------------------------------------------------------------------ B1 / H1: the clock
@pytest.mark.parametrize("role", ["td_app", "td_ctl"])
@pytest.mark.parametrize(
    "skew", [timedelta(days=3650), timedelta(minutes=-10), timedelta(minutes=5)]
)
def test_a_control_update_with_a_skewed_time_is_refused(
    safe: SafetyEnv, db: TestDb, role: str, skew: timedelta
) -> None:
    safe.baseline()
    when = safe.clock.now() + skew
    msg = refuse(
        db, role,
        "UPDATE bot_control SET kill_switch = 'ACTIVE', kill_reason = 'TEST', kill_activated_at = %s, "
        "version = version + 1, updated_at = %s, last_change_reason = 'TEST_CHANGE'",
        (when, when), VIOLATION,
    )  # fmt: skip
    assert "within 60 seconds" in msg


def test_the_web_cannot_jam_the_control_clock_and_the_host_still_acts(
    safe: SafetyEnv, db: TestDb
) -> None:
    far = safe.clock.now() + timedelta(days=3650)
    refuse(
        db, "td_app",
        "UPDATE bot_control SET bot_state = 'PAUSED', version = version + 1, updated_at = %s, last_change_reason = 'TEST_CHANGE'",
        (far,), VIOLATION,
    )  # fmt: skip
    safe.running()
    assert safe.act("pause").kind == "ok"
    assert safe.control_row().bot_state == "PAUSED"


def test_a_back_dated_resume_cannot_fake_a_fresh_reconciliation(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.baseline()
    assert safe.recover().complete  # a reconciliation now
    safe.clock.advance(seconds=3600)  # ... which is an hour old by the time anyone resumes
    back_dated = safe.clock.now() - timedelta(seconds=3500)
    sql_, args = bump(back_dated, set_="bot_state = 'RUNNING'")
    assert "within 60 seconds" in refuse(db, "td_app", sql_, args, VIOLATION)
    sql_, args = bump(safe.clock.now(), set_="bot_state = 'RUNNING'")
    refuse(db, "td_app", sql_, args, VIOLATION)  # honest time: the reconciliation is stale
    assert safe.recon().outcome == "OK"
    sql_, args = bump(safe.clock.now(), set_="bot_state = 'RUNNING'")
    run(db, "td_app", sql_, args)  # the legitimate path still works
    assert safe.control_row().bot_state == "RUNNING"


# ------------------------------------------------------------------ H2: boot id
def test_an_attempt_with_a_foreign_boot_id_is_refused(safe: SafetyEnv) -> None:
    safe.running()
    intent = safe.make_intent()
    decision = safe.allow(intent.id)
    with pytest.raises(VIOLATION, match="boot"), safe.ctl.tx() as repos:
        repos.safety.add_attempt(
            attempt_id=uuid4(), intent_id=intent.id, attempt_no=1, client_order_id=uuid4(),
            decision_id=decision.id, boot_id=str(uuid4()), now=safe.clock.now(),
        )  # fmt: skip


def test_a_restarted_process_without_recovery_cannot_authorize(safe: SafetyEnv) -> None:
    safe.running()
    safe.new_attempt()  # the recovered process may
    safe.boot_id = str(uuid4())  # a new process that has not run recovery
    with pytest.raises(VIOLATION):
        safe.new_attempt(no=1)
    assert safe.recover().complete  # recovery binds the new identity (and pauses the bot)
    assert safe.act("resume").kind == "ok"
    safe.new_attempt(intent=safe.make_intent())


@pytest.mark.parametrize("role", ["td_app", "td_ctl"])
def test_the_test_clock_is_reachable_by_no_runtime_role(db: TestDb, role: str) -> None:
    refuse(db, role, "SELECT * FROM td_test_clock", exc=DENIED)
    refuse(db, role, "INSERT INTO td_test_clock (id, value) VALUES (true, now())", exc=DENIED)
    refuse(db, role, "UPDATE td_test_clock SET value = now()", exc=DENIED)


# ------------------------------------------------------------------ H3: money rules in SQL
def test_a_forged_allow_cannot_breach_the_reserve(safe: SafetyEnv) -> None:
    safe.baseline("20")
    assert safe.recover().complete
    assert safe.act("resume").kind == "ok"
    with pytest.raises(VIOLATION, match="RESERVE_BREACH"):
        safe.new_attempt(intent=safe.make_intent(qty="0.10"))  # 10 USDC leaves under the 15 floor


def test_a_forged_allow_cannot_sell_inventory_that_is_not_held(safe: SafetyEnv) -> None:
    safe.running()
    with pytest.raises(VIOLATION, match="SELL_EXCEEDS_INVENTORY"):
        safe.new_attempt(intent=safe.make_intent(side="SELL", qty="0.01"))


def test_a_forged_allow_cannot_breach_the_deployment_cap(safe: SafetyEnv) -> None:
    safe.baseline("100")
    assert safe.recover().complete
    assert safe.act("resume").kind == "ok"
    for _ in range(2):
        safe.new_attempt(intent=safe.make_intent(qty="0.12"))  # 12 USDC each: 24 reserved
    with pytest.raises(VIOLATION, match="DEPLOYMENT_CAP_BREACH"):
        safe.new_attempt(intent=safe.make_intent(qty="0.12"))  # 36 > 35


# ------------------------------------------------------------------ M1 / M2
def test_one_venues_reconciliation_does_not_make_another_fresh(safe: SafetyEnv, db: TestDb) -> None:
    safe.baseline()
    assert safe.recover().complete
    with psycopg.connect(db.owner_target().conninfo(), autocommit=True) as conn:
        fresh = conn.execute(
            "SELECT td_fresh_reconciliation(td_now(), td_now() - interval '1 hour', 'PAPER')"
        ).fetchone()
        assert fresh is not None and fresh[0] is False


def test_two_back_to_back_runs_do_not_prove_absence(safe: SafetyEnv) -> None:
    safe.baseline()
    assert safe.recon().outcome == "OK"
    assert safe.recon().outcome == "OK"
    with safe.ctl.tx() as repos:
        proofs, spread = repos.safety.absence_proof(safe.clock.now() - timedelta(minutes=1), "FAKE")
    assert proofs >= 2 and spread < timedelta(seconds=60)
