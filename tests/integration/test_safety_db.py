"""The database is the last line of defence: guards on bot control, intents, decisions, attempts,
reconciliation records, commands and API events, per actor class. SYNTHETIC rows only."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from tests.conftest import TestDb
from tests.integration.safety_env import SafetyEnv
from tests.integration.test_market_ingest import role_conn

Sql = Callable[..., list[dict[str, Any]]]
VIOLATION = psycopg.IntegrityError
DENIED = psycopg.errors.InsufficientPrivilege
ANY = psycopg.Error


def refuse(
    db: TestDb, role: str, sql: str, params: tuple[Any, ...] = (), exc: type[Exception] = ANY
) -> str:
    """Run one statement as `role` in its own transaction; it must fail. Returns the message."""
    with role_conn(db, role) as conn, pytest.raises(exc) as info:
        conn.execute(sql, params)
    return str(info.value)


def run(db: TestDb, role: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    with role_conn(db, role) as conn:
        conn.execute(sql, params)
        conn.commit()


def bump(now: Any, *, set_: str, reason: str = "TEST_CHANGE") -> tuple[str, tuple[Any, ...]]:
    return (
        f"UPDATE bot_control SET {set_}, version = version + 1, updated_at = %s, "
        "last_change_reason = %s",
        (now, reason),
    )


# ------------------------------------------------------------------ bot control
def test_the_default_control_state_is_paused_clear_and_unrecovered(sql: Sql) -> None:
    row = sql("SELECT * FROM bot_control")[0]
    assert (row["bot_state"], row["kill_switch"], row["breaker_state"], row["recovery_state"]) == (
        "PAUSED", "INACTIVE", "CLOSED", "INCOMPLETE",
    )  # fmt: skip
    assert row["version"] == 1


@pytest.mark.parametrize("role", ["td_app", "td_ctl"])
def test_control_can_never_be_inserted_deleted_or_truncated(db: TestDb, role: str) -> None:
    refuse(db, role, "DELETE FROM bot_control")
    refuse(db, role, "TRUNCATE bot_control")
    refuse(db, role, "INSERT INTO bot_control (id, updated_at) VALUES (false, now())")


def test_even_the_owner_cannot_change_control_outside_a_role(
    safe: SafetyEnv, db: TestDb, sql: Sql
) -> None:
    with psycopg.connect(db.owner_target().conninfo(), autocommit=True) as conn:
        with pytest.raises(VIOLATION, match="unknown database role"):
            conn.execute(
                "UPDATE bot_control SET version = version + 1, last_change_reason = 'X_X', updated_at = now(), boot_id = %s",
                (str(uuid4()),),
            )


def test_the_running_state_needs_recovery_and_a_reconciliation(safe: SafetyEnv, db: TestDb) -> None:
    sql_, args = bump(safe.clock.now(), set_="bot_state = 'RUNNING'")
    refuse(db, "td_app", sql_, args, VIOLATION)  # recovery is incomplete: unrepresentable
    safe.baseline()
    assert safe.recover().complete
    run(db, "td_app", sql_, args)  # recovery done and a current OK reconciliation: allowed
    assert safe.control_row().bot_state == "RUNNING"


def test_a_resume_without_a_current_reconciliation_is_refused_by_the_database(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.baseline()
    assert safe.recover().complete
    safe.clock.advance(301)  # the good run is now older than the 300 s window
    sql_, args = bump(safe.clock.now(), set_="bot_state = 'RUNNING'")
    assert "current successful reconciliation" in refuse(db, "td_app", sql_, args, VIOLATION)


def test_a_newer_failed_run_cancels_an_older_good_one(safe: SafetyEnv, db: TestDb) -> None:
    safe.baseline()
    assert safe.recover().complete
    safe.fake.fail(
        "list_orders", *[__import__("app.exchange.fake", fromlist=["Fault"]).Fault("timeout")] * 6
    )
    assert safe.recon().outcome == "FAILED"
    sql_, args = bump(safe.clock.now(), set_="bot_state = 'RUNNING'")
    assert "current successful reconciliation" in refuse(db, "td_app", sql_, args, VIOLATION)


def test_the_host_role_can_never_resume_the_bot(safe: SafetyEnv, db: TestDb) -> None:
    safe.baseline()
    assert safe.recover().complete
    sql_, args = bump(safe.clock.now(), set_="bot_state = 'RUNNING'")
    assert "only the ADMIN dashboard resumes" in refuse(db, "td_ctl", sql_, args, VIOLATION)


@pytest.mark.parametrize(
    "assignment",
    [
        "breaker_state = 'OPEN', breaker_reason = 'API_FAILURES', breaker_opened_at = now(), breaker_cooldown_until = now()",
        "recovery_state = 'COMPLETE', recovery_completed_at = now()",
        "boot_id = '00000000-0000-4000-8000-000000000001'",
    ],
)
def test_the_web_role_cannot_open_the_breaker_or_touch_recovery(
    safe: SafetyEnv, db: TestDb, assignment: str
) -> None:
    sql_, args = bump(safe.clock.now(), set_=assignment)
    refuse(db, "td_app", sql_, args)


def test_the_web_role_cannot_release_the_kill_switch(safe: SafetyEnv, db: TestDb) -> None:
    assert safe.act("kill").kind == "ok"
    sql_, args = bump(
        safe.clock.now(),
        set_="kill_switch = 'INACTIVE', kill_reason = NULL, kill_activated_at = NULL",
    )
    assert "only the host" in refuse(db, "td_app", sql_, args, VIOLATION)


def test_the_kill_switch_pauses_a_running_bot_in_the_same_change(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    assert sql("SELECT bot_state FROM bot_control")[0]["bot_state"] == "RUNNING"
    assert safe.act("kill").kind == "ok"
    row = sql("SELECT bot_state, kill_switch FROM bot_control")[0]
    assert (row["bot_state"], row["kill_switch"]) == ("PAUSED", "ACTIVE")


def test_a_running_bot_with_an_active_kill_switch_is_unrepresentable(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.running()
    sql_, args = bump(
        safe.clock.now(),
        set_="kill_switch = 'ACTIVE', kill_reason = 'X_X', kill_activated_at = now()",
    )
    refuse(db, "td_app", sql_, args, VIOLATION)  # would leave RUNNING with the kill switch on


def test_version_must_advance_by_one_and_time_never_goes_back(safe: SafetyEnv, db: TestDb) -> None:
    now = safe.clock.now()
    for extra in ("version = version + 2", "version = version", "version = version - 1"):
        refuse(
            db,
            "td_ctl",
            f"UPDATE bot_control SET {extra}, updated_at = %s, boot_id = %s, last_change_reason = 'X_X'",
            (now, str(uuid4())),
            VIOLATION,
        )
    safe.baseline()
    safe.recover()
    refuse(
        db,
        "td_ctl",
        "UPDATE bot_control SET version = version + 1, updated_at = %s, boot_id = %s, last_change_reason = 'X_X'",
        (now - timedelta(days=400), str(uuid4())),
        VIOLATION,
    )


def test_every_control_change_is_recorded_in_an_append_only_history(
    safe: SafetyEnv, sql: Sql, db: TestDb
) -> None:
    safe.running()
    safe.act("pause")
    events = [r["event"] for r in sql("SELECT event FROM bot_control_history ORDER BY id")]
    assert "RECOVERY_COMPLETE" in events and "RESUME" in events and "PAUSE" in events
    refuse(
        db,
        "td_app",
        "INSERT INTO bot_control_history (occurred_at, actor_class, event, bot_state, kill_switch, breaker_state, recovery_state, reason, version) VALUES (now(), 'WEB', 'PAUSE', 'PAUSED', 'INACTIVE', 'CLOSED', 'COMPLETE', 'X_X', 1)",
        exc=VIOLATION,
    )
    refuse(db, "td_ctl", "UPDATE bot_control_history SET reason = 'ALTERED'")
    refuse(db, "td_ctl", "DELETE FROM bot_control_history")


def test_the_breaker_closes_only_together_with_a_resume_after_its_cooldown(
    safe: SafetyEnv, sql: Sql, db: TestDb
) -> None:
    safe.running()
    assert safe.host.trip_breaker("API_FAILURES")
    row = sql("SELECT bot_state, breaker_state FROM bot_control")[0]
    assert (row["bot_state"], row["breaker_state"]) == ("PAUSED", "OPEN")
    # closing it alone is refused, and a resume inside the cooldown is refused
    alone, a1 = bump(
        safe.clock.now(),
        set_="breaker_state = 'CLOSED', breaker_reason = NULL, breaker_opened_at = NULL, breaker_cooldown_until = NULL",
    )
    refuse(db, "td_app", alone, a1, VIOLATION)
    assert safe.act("resume").kind == "not_allowed"
    safe.clock.advance(safe.settings.safety.breaker_cooldown_seconds + 1)
    stale = safe.act("resume")  # the last reconciliation is now older than the window
    assert stale.kind == "not_allowed" and "RECONCILIATION_STALE" in stale.reasons
    assert safe.recon().outcome == "OK"
    assert safe.act("resume").kind == "ok"
    assert sql("SELECT breaker_state FROM bot_control")[0]["breaker_state"] == "CLOSED"


def test_a_resume_needs_a_reconciliation_that_finished_after_the_breaker_opened(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.running()
    assert safe.recon().outcome == "OK"
    assert safe.host.trip_breaker("API_FAILURES")
    safe.clock.advance(safe.settings.safety.breaker_cooldown_seconds + 1)
    # the last run is older than the trip: refused (also stale by then)
    out = safe.act("resume")
    assert out.kind == "not_allowed"


# ------------------------------------------------------------------ the dashboard cannot make orders
WEB_FORBIDDEN_INSERTS = {
    "order_intents": "INSERT INTO order_intents (id, intent_key, venue, pair_id, product_id, side, order_type, post_only, price, base_qty, source, created_at) VALUES (gen_random_uuid(), repeat('a', 64), 'FAKE', gen_random_uuid(), 'BTC-USDC', 'BUY', 'limit_limit_gtc', true, 100, 0.1, 'web', now())",
    "risk_decisions": "INSERT INTO risk_decisions (id, intent_id, decision, reasons, inputs_hash, decided_at, expires_at) VALUES (gen_random_uuid(), gen_random_uuid(), 'ALLOW', '{}', repeat('a', 64), now(), now() + interval '10 seconds')",
    "order_attempts": "INSERT INTO order_attempts (id, intent_id, attempt_no, client_order_id, decision_id, boot_id, state, created_at, updated_at) VALUES (gen_random_uuid(), gen_random_uuid(), 1, gen_random_uuid(), gen_random_uuid(), gen_random_uuid()::text, 'AUTHORIZED', now(), now())",
    "attempt_fills": "INSERT INTO attempt_fills (attempt_id, venue, exchange_fill_id, side, price, size, fee, liquidity, occurred_at) VALUES (gen_random_uuid(), 'FAKE', 'fill-0000001', 'BUY', 1, 1, 0, 'MAKER', now())",
    "reconciliation_runs": "INSERT INTO reconciliation_runs (id, venue, trigger, started_at, finished_at, outcome) VALUES (gen_random_uuid(), 'FAKE', 'MANUAL', now(), now(), 'OK')",
    "reconciliation_findings": "INSERT INTO reconciliation_findings (run_id, code) VALUES (gen_random_uuid(), 'UNKNOWN_ORDER')",
    "order_hints": "INSERT INTO order_hints (venue, client_order_id, order_id, status, filled_qty, sequence, received_at) VALUES ('FAKE', 'abcdefgh', 'abcdefgh', 'FILLED', 1, 1, now())",
    "venue_baselines": "INSERT INTO venue_baselines (venue, currency, amount, recorded_at) VALUES ('FAKE', 'USDC', 1000, now())",
    "api_events": "INSERT INTO api_events (venue, operation, ok, occurred_at) VALUES ('FAKE', 'submit', true, now())",
}


@pytest.mark.parametrize("table", sorted(WEB_FORBIDDEN_INSERTS))
def test_the_web_role_cannot_insert_into_any_order_or_reconciliation_table(
    db: TestDb, table: str
) -> None:
    refuse(db, "td_app", WEB_FORBIDDEN_INSERTS[table], exc=DENIED)


def test_the_web_role_holds_no_write_grant_on_the_order_path(db: TestDb, sql: Sql) -> None:
    rows = sql(
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
        "WHERE grantee = 'td_app' AND privilege_type <> 'SELECT' AND table_name = ANY(%s)",
        (["order_intents", "risk_decisions", "order_attempts", "attempt_fills", "reconciliation_runs",
          "reconciliation_findings", "order_hints", "venue_baselines", "api_events", "paper_orders", "paper_fills",
          "paper_ledger_entries", "paper_positions", "paper_session", "pairs"],),
    )  # fmt: skip
    assert rows == [] or all(
        r["table_name"] == "pairs" for r in rows
    )  # pairs are managed by the pair chain
    table_writes = {
        r["table_name"]
        for r in sql(
            "SELECT table_name FROM information_schema.role_table_grants WHERE grantee = 'td_app' "
            "AND privilege_type IN ('INSERT', 'DELETE', 'TRUNCATE') AND table_name = ANY(%s)",
            (
                [
                    "order_intents",
                    "risk_decisions",
                    "order_attempts",
                    "attempt_fills",
                    "reconciliation_runs",
                ],
            ),
        )
    }
    assert table_writes == set()


# ------------------------------------------------------------------ intents and decisions (host)
def test_an_intent_is_immutable_and_capped_in_the_schema(safe: SafetyEnv, db: TestDb) -> None:
    intent = safe.make_intent()
    refuse(db, "td_ctl", "UPDATE order_intents SET price = 1")
    refuse(db, "td_ctl", "DELETE FROM order_intents")
    refuse(db, "td_ctl", "TRUNCATE order_intents")
    with pytest.raises(VIOLATION):
        safe.make_intent(price="100", qty="0.13")  # 13 USDC: over the hard per-order ceiling of 12
    assert intent.price == 100


@pytest.mark.parametrize(
    "override",
    [
        ("venue", "'LIVE'"),
        ("venue", "'CBE'"),
        ("order_type", "'market_market_ioc'"),
        ("post_only", "false"),
        ("side", "'HOLD'"),
        ("product_id", "'BTC-USD'"),
        ("price", "0"),
        ("base_qty", "-1"),
    ],
)
def test_an_intent_can_only_be_a_post_only_limit_order_on_a_representable_venue(
    safe: SafetyEnv, db: TestDb, override: tuple[str, str]
) -> None:
    cols = {
        "venue": "'FAKE'",
        "order_type": "'limit_limit_gtc'",
        "post_only": "true",
        "side": "'BUY'",
        "product_id": "'BTC-USDC'",
        "price": "100",
        "base_qty": "0.1",
    }
    cols[override[0]] = override[1]
    sql_ = (
        "INSERT INTO order_intents (id, intent_key, venue, pair_id, product_id, side, order_type, post_only, price, base_qty, source, created_at) "
        f"VALUES (gen_random_uuid(), repeat('b', 64), {cols['venue']}, %s, {cols['product_id']}, {cols['side']}, {cols['order_type']}, {cols['post_only']}, {cols['price']}, {cols['base_qty']}, 'test', now())"
    )
    refuse(db, "td_ctl", sql_, (safe.pair_id,), VIOLATION)


def test_an_intent_key_is_unique_so_the_same_proposal_is_one_intent(safe: SafetyEnv) -> None:
    safe.make_intent(key="a" * 64)
    with pytest.raises(VIOLATION):
        safe.make_intent(key="a" * 64)


def test_a_decision_is_consistent_short_lived_and_consumed_once(
    safe: SafetyEnv, db: TestDb
) -> None:
    intent = safe.make_intent()
    row = safe.allow(intent.id)
    refuse(db, "td_ctl", "UPDATE risk_decisions SET decision = 'BLOCK'")
    refuse(db, "td_ctl", "DELETE FROM risk_decisions")
    refuse(
        db,
        "td_ctl",
        "INSERT INTO risk_decisions (id, intent_id, decision, reasons, inputs_hash, decided_at, expires_at) VALUES (gen_random_uuid(), %s, 'ALLOW', '{KILL_SWITCH_ACTIVE}', repeat('a', 64), now(), now() + interval '5 seconds')",
        (intent.id,),
        VIOLATION,
    )
    refuse(
        db,
        "td_ctl",
        "INSERT INTO risk_decisions (id, intent_id, decision, reasons, inputs_hash, decided_at, expires_at) VALUES (gen_random_uuid(), %s, 'BLOCK', '{}', repeat('a', 64), now(), now() + interval '5 seconds')",
        (intent.id,),
        VIOLATION,
    )
    refuse(
        db,
        "td_ctl",
        "INSERT INTO risk_decisions (id, intent_id, decision, reasons, inputs_hash, decided_at, expires_at) VALUES (gen_random_uuid(), %s, 'ALLOW', '{}', repeat('a', 64), now(), now() + interval '90 seconds')",
        (intent.id,),
        VIOLATION,
    )
    assert row.decision == "ALLOW"


# ------------------------------------------------------------------ attempts: authorization guards
def attempt_sql(
    safe: SafetyEnv, intent_id: Any, decision_id: Any, no: int = 1
) -> tuple[str, tuple[Any, ...]]:
    return (
        "INSERT INTO order_attempts (id, intent_id, attempt_no, client_order_id, decision_id, boot_id, state, created_at, updated_at) "
        "VALUES (gen_random_uuid(), %s, %s, gen_random_uuid(), %s, %s, 'AUTHORIZED', %s, %s)",
        (intent_id, no, decision_id, safe.boot_id, safe.clock.now(), safe.clock.now()),
    )


def test_an_attempt_is_authorized_only_while_running_and_clear(safe: SafetyEnv, db: TestDb) -> None:
    intent = safe.make_intent()
    decision = safe.allow(intent.id)
    sql_, args = attempt_sql(safe, intent.id, decision.id)
    assert "not authorized while the bot is not RUNNING" in refuse(
        db, "td_ctl", sql_, args, VIOLATION
    )
    safe.running()
    intent = safe.make_intent()
    decision = safe.allow(intent.id)
    sql_, args = attempt_sql(safe, intent.id, decision.id)
    run(db, "td_ctl", sql_, args)


def test_attempts_are_refused_while_the_kill_switch_or_breaker_is_on(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.running()
    intent = safe.make_intent()
    decision = safe.allow(intent.id)
    safe.act("kill")
    sql_, args = attempt_sql(safe, intent.id, decision.id)
    assert "not RUNNING and clear" in refuse(db, "td_ctl", sql_, args, VIOLATION)


def test_attempts_need_a_current_reconciliation(safe: SafetyEnv, db: TestDb) -> None:
    safe.running()
    safe.clock.advance(301)
    intent = safe.make_intent()
    decision = safe.allow(intent.id)
    sql_, args = attempt_sql(safe, intent.id, decision.id)
    assert "current successful reconciliation" in refuse(db, "td_ctl", sql_, args, VIOLATION)


def test_an_attempt_needs_a_valid_unconsumed_allow_decision(safe: SafetyEnv, db: TestDb) -> None:
    safe.running()
    intent, other = safe.make_intent(), safe.make_intent()
    block = safe.allow(intent.id, decision="BLOCK")
    sql_, args = attempt_sql(safe, intent.id, block.id)
    assert "no valid unconsumed ALLOW" in refuse(db, "td_ctl", sql_, args, VIOLATION)
    wrong = safe.allow(other.id)
    sql_, args = attempt_sql(safe, intent.id, wrong.id)
    assert "no valid unconsumed ALLOW" in refuse(db, "td_ctl", sql_, args, VIOLATION)
    old = safe.allow(intent.id)
    safe.clock.advance(31)
    safe.recon()  # keep the reconciliation fresh; only the decision has expired
    sql_, args = attempt_sql(safe, intent.id, old.id)
    assert "no valid unconsumed ALLOW" in refuse(db, "td_ctl", sql_, args, VIOLATION)
    assert safe.new_attempt(intent=safe.make_intent())


def test_one_allow_authorizes_one_attempt(safe: SafetyEnv, db: TestDb) -> None:
    safe.running()
    intent = safe.make_intent()
    decision = safe.allow(intent.id)
    sql_, args = attempt_sql(safe, intent.id, decision.id)
    run(db, "td_ctl", sql_, args)
    other = safe.make_intent()
    sql2, args2 = attempt_sql(safe, other.id, decision.id)
    refuse(
        db, "td_ctl", sql2, args2, VIOLATION
    )  # the decision was consumed (and belongs elsewhere)


def test_an_intent_has_at_most_one_live_attempt(safe: SafetyEnv, db: TestDb) -> None:
    safe.running()
    intent = safe.make_intent()
    safe.new_attempt(intent=intent)
    decision = safe.allow(intent.id)
    sql_, args = attempt_sql(safe, intent.id, decision.id, no=2)
    assert "absent or rejected" in refuse(db, "td_ctl", sql_, args, VIOLATION)


def test_a_retry_attempt_number_must_follow_and_needs_an_absent_or_rejected_predecessor(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.running()
    intent = safe.make_intent()
    first = safe.new_attempt(intent=intent)
    safe.walk(first, "SUBMITTING", "REJECTED")
    decision = safe.allow(intent.id)
    sql_, args = attempt_sql(safe, intent.id, decision.id, no=3)
    assert "consecutive" in refuse(db, "td_ctl", sql_, args, VIOLATION)
    sql_, args = attempt_sql(safe, intent.id, decision.id, no=2)
    run(db, "td_ctl", sql_, args)


def test_no_new_attempt_after_a_filled_or_cancelled_or_unknown_predecessor(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.running()
    for path in (
        ("SUBMITTING", "WORKING", "FILLED"),
        ("SUBMITTING", "WORKING", "CANCELLED"),
        ("SUBMITTING", "WORKING", "EXPIRED"),
        ("SUBMITTING", "WORKING", "CANCEL_REQUESTED"),
    ):
        intent = safe.make_intent()
        first = safe.new_attempt(intent=intent)
        safe.walk(first, *path)
        decision = safe.allow(intent.id)
        sql_, args = attempt_sql(safe, intent.id, decision.id, no=2)
        refuse(db, "td_ctl", sql_, args, VIOLATION)
    # an UNKNOWN attempt anywhere blocks every authorization
    unknown = safe.new_attempt()
    safe.walk(unknown, "SUBMITTING", "UNKNOWN")
    intent = safe.make_intent()
    decision = safe.allow(intent.id)
    sql_, args = attempt_sql(safe, intent.id, decision.id)
    assert "unknown outcome" in refuse(db, "td_ctl", sql_, args, VIOLATION)


def test_a_client_order_id_can_never_be_reused(safe: SafetyEnv, db: TestDb) -> None:
    safe.running()
    first = safe.new_attempt()
    client = safe.attempt(first).client_order_id
    intent = safe.make_intent()
    decision = safe.allow(intent.id)
    refuse(db, "td_ctl",
           "INSERT INTO order_attempts (id, intent_id, attempt_no, client_order_id, decision_id, boot_id, state, created_at, updated_at) "
           "VALUES (gen_random_uuid(), %s, 1, %s, %s, %s, 'AUTHORIZED', %s, %s)",
           (intent.id, client, decision.id, safe.boot_id, safe.clock.now(), safe.clock.now()), VIOLATION)  # fmt: skip


# ------------------------------------------------------------------ attempts: transitions
ALLOWED = {
    "AUTHORIZED": {"SUBMITTING", "REJECTED"},
    "SUBMITTING": {"WORKING", "REJECTED", "UNKNOWN"},
    "WORKING": {"CANCEL_REQUESTED", "FILLED", "CANCELLED", "EXPIRED", "UNKNOWN"},
    "CANCEL_REQUESTED": {"CANCELLED", "FILLED", "WORKING", "EXPIRED", "UNKNOWN"},
    "UNKNOWN": {"WORKING", "FILLED", "CANCELLED", "EXPIRED", "REJECTED"},  # ABSENT needs the proof
    "FILLED": set(),
    "CANCELLED": set(),
    "EXPIRED": set(),
    "REJECTED": set(),
}
PATHS = {
    "AUTHORIZED": (),
    "SUBMITTING": ("SUBMITTING",),
    "WORKING": ("SUBMITTING", "WORKING"),
    "CANCEL_REQUESTED": ("SUBMITTING", "WORKING", "CANCEL_REQUESTED"),
    "UNKNOWN": ("SUBMITTING", "UNKNOWN"),
    "FILLED": ("SUBMITTING", "WORKING", "FILLED"),
    "CANCELLED": ("SUBMITTING", "WORKING", "CANCELLED"),
    "EXPIRED": ("SUBMITTING", "WORKING", "EXPIRED"),
    "REJECTED": ("SUBMITTING", "REJECTED"),
}
TARGETS = (
    "AUTHORIZED",
    "SUBMITTING",
    "WORKING",
    "CANCEL_REQUESTED",
    "FILLED",
    "CANCELLED",
    "EXPIRED",
    "REJECTED",
    "UNKNOWN",
    "ABSENT",
)


@pytest.mark.parametrize("source", sorted(ALLOWED))
def test_the_transition_matrix_is_exactly_the_documented_one(
    safe: SafetyEnv, db: TestDb, source: str
) -> None:
    safe.running()
    # every attempt is authorized first: an UNKNOWN attempt blocks any later authorization
    attempts = {t: safe.new_attempt(intent=safe.make_intent()) for t in TARGETS}
    for target, attempt in attempts.items():
        safe.walk(attempt, *PATHS[source])
        code = "NOT_SENT" if source == "AUTHORIZED" and target == "REJECTED" else "TEST_REJECT"
        submitting = target == "SUBMITTING"
        sql_ = (
            "UPDATE order_attempts SET state = %s, updated_at = %s, failure_code = %s"
            + (", submitting_at = %s" if submitting else "")
            + " WHERE id = %s"
        )
        args: tuple[Any, ...] = (
            (target, safe.clock.now(), code)
            + ((safe.clock.now(),) if submitting else ())
            + (attempt,)
        )
        if target in ALLOWED[source] or target == source:  # a same-state update is not a move
            if source in ("FILLED", "CANCELLED", "EXPIRED", "REJECTED") and target == source:
                args = (target, safe.clock.now(), None) + args[
                    3:
                ]  # a settled attempt keeps its code
                sql_ = sql_.replace("failure_code = %s", "failure_code = failure_code")
                args = (target, safe.clock.now()) + args[3:]
            run(db, "td_ctl", sql_, args)
        else:
            refuse(db, "td_ctl", sql_, args, VIOLATION)


def test_the_submit_mark_is_required_and_write_once_and_the_identity_is_immutable(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.running()
    attempt = safe.new_attempt()
    refuse(
        db,
        "td_ctl",
        "UPDATE order_attempts SET state = 'SUBMITTING', updated_at = %s WHERE id = %s",
        (safe.clock.now(), attempt),
        VIOLATION,
    )
    safe.walk(attempt, "SUBMITTING")
    refuse(
        db,
        "td_ctl",
        "UPDATE order_attempts SET submitting_at = %s, updated_at = %s WHERE id = %s",
        (safe.clock.now() + timedelta(hours=1), safe.clock.now(), attempt),
        VIOLATION,
    )
    for column, value in (
        ("client_order_id", str(uuid4())),
        ("attempt_no", 2),
        ("intent_id", str(uuid4())),
        ("decision_id", str(uuid4())),
        ("boot_id", str(uuid4())),
        ("created_at", "2020-01-01"),
    ):
        refuse(
            db,
            "td_ctl",
            f"UPDATE order_attempts SET {column} = %s WHERE id = %s",
            (value, attempt),
            ANY,
        )
    refuse(db, "td_ctl", "DELETE FROM order_attempts")
    refuse(db, "td_ctl", "TRUNCATE order_attempts")


def test_the_exchange_id_is_write_once_and_fills_never_decrease(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.running()
    attempt = safe.new_attempt()
    safe.walk(attempt, "SUBMITTING", "WORKING")
    refuse(
        db,
        "td_ctl",
        "UPDATE order_attempts SET exchange_order_id = 'ord-other-01', updated_at = %s WHERE id = %s",
        (safe.clock.now(), attempt),
        VIOLATION,
    )
    run(
        db,
        "td_ctl",
        "UPDATE order_attempts SET filled_qty = 0.05, updated_at = %s WHERE id = %s",
        (safe.clock.now(), attempt),
    )
    refuse(
        db,
        "td_ctl",
        "UPDATE order_attempts SET filled_qty = 0.01, updated_at = %s WHERE id = %s",
        (safe.clock.now(), attempt),
        VIOLATION,
    )


def test_the_web_role_cannot_touch_attempts_at_all(safe: SafetyEnv, db: TestDb) -> None:
    safe.running()
    attempt = safe.new_attempt()
    refuse(
        db,
        "td_app",
        "UPDATE order_attempts SET state = 'CANCELLED' WHERE id = %s",
        (attempt,),
        DENIED,
    )
    refuse(db, "td_app", "DELETE FROM order_attempts", exc=DENIED)


def _ok_runs_after(safe: SafetyEnv, attempt: Any, n: int) -> None:
    submitted = safe.attempt(attempt).submitting_at
    wait = (submitted + timedelta(seconds=121) - safe.clock.now()).total_seconds()
    safe.clock.advance(max(wait, 0))
    for _ in range(n):
        assert safe.recon().outcome == "OK"
        safe.clock.advance(61)


def test_absence_needs_two_ok_reconciliations_after_the_wait_window(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.baseline()
    assert safe.recover().complete
    assert safe.act("resume").kind == "ok"
    attempt = safe.new_attempt()
    safe.walk(attempt, "SUBMITTING", "UNKNOWN")
    move = "UPDATE order_attempts SET state = 'ABSENT', updated_at = %s WHERE id = %s"
    assert "two successful reconciliations" in refuse(
        db, "td_ctl", move, (safe.clock.now(), attempt), VIOLATION
    )
    _ok_runs_after(safe, attempt, 1)
    assert "two successful reconciliations" in refuse(
        db, "td_ctl", move, (safe.clock.now(), attempt), VIOLATION
    )


def test_absence_is_refused_if_any_run_ever_named_the_client_id(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.baseline()
    assert safe.recover().complete
    assert safe.act("resume").kind == "ok"
    attempt = safe.new_attempt()
    safe.walk(attempt, "SUBMITTING", "UNKNOWN")
    _ok_runs_after(safe, attempt, 1)
    client = str(safe.attempt(attempt).client_order_id)
    with safe.ctl.tx() as repos:
        run_row = repos.safety.latest_run()
        assert run_row is not None
        # a finding that names the client id, as a run that saw the order would record
        repos.safety._conn.execute(
            "INSERT INTO reconciliation_findings (run_id, code, subject) VALUES (%s, 'UNKNOWN_ORDER', %s)",
            (run_row.id, client),
        )
    _ok_runs_after(safe, attempt, 2)  # the reconciler itself declines to declare absence
    assert safe.attempt(attempt).state == "UNKNOWN"
    move = "UPDATE order_attempts SET state = 'ABSENT', updated_at = %s WHERE id = %s"
    assert "absence is not proven" in refuse(
        db, "td_ctl", move, (safe.clock.now(), attempt), VIOLATION
    )


def test_a_settled_attempt_keeps_its_outcome(safe: SafetyEnv, db: TestDb) -> None:
    safe.running()
    for path in (
        ("SUBMITTING", "REJECTED"),
        ("SUBMITTING", "WORKING", "FILLED"),
        ("SUBMITTING", "WORKING", "CANCELLED"),
    ):
        attempt = safe.new_attempt(intent=safe.make_intent())
        safe.walk(attempt, *path)
        refuse(
            db,
            "td_ctl",
            "UPDATE order_attempts SET failure_code = 'CHANGED', updated_at = %s WHERE id = %s",
            (safe.clock.now(), attempt),
            VIOLATION,
        )
    rejected = safe.new_attempt(intent=safe.make_intent())
    safe.walk(rejected, "SUBMITTING", "REJECTED")
    refuse(
        db,
        "td_ctl",
        "UPDATE order_attempts SET filled_qty = 0.1, updated_at = %s WHERE id = %s",
        (safe.clock.now(), rejected),
        VIOLATION,
    )
    filled = safe.new_attempt(intent=safe.make_intent())
    safe.walk(filled, "SUBMITTING", "WORKING", "FILLED")
    run(
        db,
        "td_ctl",
        "UPDATE order_attempts SET filled_qty = 0.1, updated_at = %s WHERE id = %s",
        (safe.clock.now(), filled),
    )


# ------------------------------------------------------------------ records that never change
APPEND_ONLY = [
    "order_intents",
    "reconciliation_runs",
    "reconciliation_findings",
    "attempt_fills",
    "order_hints",
    "venue_baselines",
    "bot_control_history",
]


@pytest.mark.parametrize("table", APPEND_ONLY)
@pytest.mark.parametrize("role", ["td_app", "td_ctl"])
def test_records_are_append_only(safe: SafetyEnv, db: TestDb, table: str, role: str) -> None:
    safe.baseline()
    safe.recover()
    for statement in (f"DELETE FROM {table}", f"TRUNCATE {table}"):  # noqa: S608
        refuse(db, role, statement)


def test_reconciliation_and_fill_rows_are_written_by_the_host_only(
    safe: SafetyEnv, db: TestDb
) -> None:
    refuse(db, "td_app", WEB_FORBIDDEN_INSERTS["reconciliation_runs"], exc=DENIED)
    run(
        db,
        "td_ctl",
        "INSERT INTO reconciliation_runs (id, venue, trigger, started_at, finished_at, outcome) VALUES (gen_random_uuid(), 'FAKE', 'MANUAL', td_now(), td_now(), 'OK')",
    )
    refuse(
        db,
        "td_ctl",
        "INSERT INTO reconciliation_runs (id, venue, trigger, started_at, finished_at, outcome, findings_count) VALUES (gen_random_uuid(), 'FAKE', 'MANUAL', td_now(), td_now(), 'OK', 1)",
        exc=VIOLATION,
    )
    refuse(
        db,
        "td_ctl",
        "INSERT INTO reconciliation_runs (id, venue, trigger, started_at, finished_at, outcome) VALUES (gen_random_uuid(), 'FAKE', 'MANUAL', td_now(), td_now(), 'FAILED')",
        exc=VIOLATION,
    )
    refuse(
        db,
        "td_ctl",
        "INSERT INTO reconciliation_runs (id, venue, trigger, started_at, finished_at, outcome) VALUES (gen_random_uuid(), 'LIVE', 'MANUAL', td_now(), td_now(), 'OK')",
        exc=VIOLATION,
    )


def test_no_table_can_hold_a_live_venue(db: TestDb, sql: Sql) -> None:
    for table, column in (
        ("order_intents", "venue"),
        ("reconciliation_runs", "venue"),
        ("venue_baselines", "venue"),
        ("attempt_fills", "venue"),
        ("order_hints", "venue"),
    ):
        checks = sql(
            "SELECT pg_get_constraintdef(c.oid) AS d FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = %s AND c.contype = 'c' AND pg_get_constraintdef(c.oid) LIKE %s",
            (table, f"%{column}%"),
        )
        assert checks and all("LIVE'" not in c["d"] and 'LIVE"' not in c["d"] for c in checks), (
            table
        )
        assert any("PAPER" in c["d"] and "FAKE" in c["d"] for c in checks), table


# ------------------------------------------------------------------ commands and API events
def test_the_web_role_may_only_enqueue_a_command_and_the_host_finishes_it(
    safe: SafetyEnv, db: TestDb, sql: Sql
) -> None:
    user = safe.admin.user.id
    q = "INSERT INTO control_commands (id, kind, origin, requested_by, requested_at) VALUES (%s, 'CANCEL_KNOWN', 'OPERATOR', %s, now())"
    cid = uuid4()
    run(db, "td_app", q, (cid, user))
    refuse(db, "td_app", q, (uuid4(), user), VIOLATION)  # single flight
    refuse(
        db, "td_app", "UPDATE control_commands SET state = 'DONE', finished_at = now()", exc=DENIED
    )
    refuse(db, "td_app", "DELETE FROM control_commands", exc=DENIED)
    refuse(db, "td_ctl", "UPDATE control_commands SET state = 'PENDING'", exc=VIOLATION)
    refuse(
        db,
        "td_ctl",
        "UPDATE control_commands SET kind = 'CANCEL_KNOWN', origin = 'KILL_SWITCH'",
        exc=ANY,
    )
    run(
        db,
        "td_ctl",
        "UPDATE control_commands SET state = 'DONE', finished_at = now(), result = '{\"queued\": 0}'",
    )
    refuse(
        db, "td_ctl", "UPDATE control_commands SET state = 'RUNNING'", exc=VIOLATION
    )  # a command finishes once
    assert sql("SELECT state FROM control_commands")[0]["state"] == "DONE"


def test_a_command_can_only_be_a_cancel_of_known_orders(db: TestDb, safe: SafetyEnv) -> None:
    for kind in ("CANCEL_ALL", "SELL_ALL", "MARKET_SELL", "LIQUIDATE", "CANCEL_KNOWN_AND_SELL"):
        refuse(
            db,
            "td_app",
            "INSERT INTO control_commands (id, kind, origin, requested_by, requested_at) VALUES (%s, %s, 'OPERATOR', %s, now())",
            (uuid4(), kind, safe.admin.user.id),
            VIOLATION,
        )


def test_old_api_events_can_be_pruned_by_the_host_and_recent_ones_never(
    safe: SafetyEnv, db: TestDb, sql: Sql
) -> None:
    run(
        db,
        "td_ctl",
        "INSERT INTO api_events (venue, operation, ok, occurred_at) VALUES ('FAKE', 'list_orders', true, now())",
    )
    run(
        db,
        "td_ctl",
        "INSERT INTO api_events (venue, operation, ok, occurred_at) VALUES ('FAKE', 'list_orders', true, now() - interval '8 days')",
    )
    refuse(db, "td_ctl", "UPDATE api_events SET ok = false")
    refuse(
        db,
        "td_ctl",
        "DELETE FROM api_events WHERE occurred_at > now() - interval '1 day'",
        exc=VIOLATION,
    )
    refuse(db, "td_app", "DELETE FROM api_events", exc=DENIED)
    run(db, "td_ctl", "DELETE FROM api_events WHERE occurred_at < now() - interval '7 days'")
    assert sql("SELECT count(*) AS n FROM api_events")[0]["n"] == 1
