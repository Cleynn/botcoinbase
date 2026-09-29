"""REST reconciliation under fault injection: every discrepancy is found, adopted or flagged, and a
failed read never looks like a clean run. Scripted FAKE exchange, SYNTHETIC numbers, no network."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from app.exchange.fake import Fault
from app.exchange.gateway import CancelResult
from app.safety import commands
from app.storage.safety_repositories import HintRow
from tests.integration.safety_env import SafetyEnv

Sql = Callable[..., list[dict[str, Any]]]
D = Decimal


def working(safe: SafetyEnv) -> tuple[UUID, str]:
    """A RUNNING bot with one WORKING order on the exchange. Returns (attempt id, exchange id)."""
    safe.running()
    result = safe.pipeline.submit(safe.proposal(), source="test", slot="w", book=safe.book())
    assert result.kind == "submitted" and result.attempt_id is not None
    return result.attempt_id, next(iter(safe.fake.orders))


def set_balances(safe: SafetyEnv, usdc: str, btc: str | None = None) -> None:
    safe.fake.set_balance("USDC", usdc)
    if btc is not None:
        safe.fake.set_balance("BTC", btc)


def state(safe: SafetyEnv, attempt: UUID) -> str:
    return str(safe.attempt(attempt).state)


# ------------------------------------------------------------------ a clean run
def test_a_clean_reconciliation_is_ok_and_repeatable(safe: SafetyEnv, sql: Sql) -> None:
    attempt, _ = working(safe)
    first, second = safe.recon(), safe.recon()
    assert first.outcome == second.outcome == "OK" and first.findings == ()
    assert state(safe, attempt) == "WORKING"
    runs = sql("SELECT outcome, orders_seen, findings_count FROM reconciliation_runs ORDER BY seq")
    assert runs[-1] == {"outcome": "OK", "orders_seen": 1, "findings_count": 0}


def test_reconciliation_is_idempotent_for_fills(safe: SafetyEnv, sql: Sql) -> None:
    attempt, oid = working(safe)
    safe.fake.inject_fill(oid, "100", "0.1", "0.02")
    set_balances(safe, "39.98", "0.1")
    for _ in range(3):
        assert safe.recon().outcome == "OK"
    assert sql("SELECT count(*) AS n FROM attempt_fills")[0]["n"] == 1
    assert state(safe, attempt) == "FILLED" and safe.attempt(attempt).filled_qty == D("0.1")


def test_a_partial_fill_keeps_the_order_working_and_the_books_balanced(safe: SafetyEnv) -> None:
    attempt, oid = working(safe)
    safe.fake.inject_fill(oid, "100", "0.04", "0.008")
    set_balances(safe, "45.992", "0.04")
    assert safe.recon().outcome == "OK"
    row = safe.attempt(attempt)
    assert row.state == "WORKING" and row.filled_qty == D("0.04")


# ------------------------------------------------------------------ orders that should not be there or are missing
def test_a_foreign_working_order_blocks_and_is_never_touched(safe: SafetyEnv, sql: Sql) -> None:
    working(safe)
    foreign = safe.fake.add_foreign_order()
    run = safe.recon()
    assert run.outcome == "MISMATCH" and "UNKNOWN_ORDER" in run.codes
    safe.act("kill")
    outcome = safe.commands.run_pending()
    assert outcome is not None
    assert safe.fake.orders[foreign].status == "WORKING"  # foreign orders are never cancelled


def test_a_foreign_order_in_a_terminal_state_is_history_not_a_problem(safe: SafetyEnv) -> None:
    working(safe)
    foreign = safe.fake.add_foreign_order()
    safe.fake.set_status(foreign, "CANCELLED")
    assert safe.recon().outcome == "OK"


def test_an_order_missing_from_the_listing_blocks_and_becomes_unknown_then_heals(
    safe: SafetyEnv,
) -> None:
    attempt, oid = working(safe)
    client = safe.fake.orders[oid].client_order_id
    safe.fake.hidden.add(client)  # the listing lags
    run = safe.recon()
    assert run.outcome == "MISMATCH" and "MISSING_ORDER" in run.codes
    assert state(safe, attempt) == "UNKNOWN"
    safe.fake.hidden.clear()
    healed = safe.recon()
    assert healed.outcome == "OK" and healed.resolved == 1 and state(safe, attempt) == "WORKING"


def test_an_empty_listing_after_an_order_was_placed_is_a_missing_order(safe: SafetyEnv) -> None:
    attempt, _ = working(safe)
    safe.fake.fail("list_orders", Fault("empty"))
    run = safe.recon()
    assert (
        run.outcome == "MISMATCH"
        and "MISSING_ORDER" in run.codes
        and state(safe, attempt) == "UNKNOWN"
    )


def test_two_exchange_orders_with_one_client_id_block(safe: SafetyEnv) -> None:
    _, oid = working(safe)
    original = safe.fake.orders[oid]
    safe.fake.orders["fx-dup-0001"] = replace(original, order_id="fx-dup-0001")
    run = safe.recon()
    assert run.outcome == "MISMATCH" and "DUPLICATE_CLIENT_ID" in run.codes


def test_an_order_that_differs_from_its_intent_is_not_adopted(safe: SafetyEnv) -> None:
    attempt, oid = working(safe)
    safe.fake.orders[oid] = replace(safe.fake.orders[oid], price=D("101"))
    run = safe.recon()
    assert (
        run.outcome == "MISMATCH"
        and "ORDER_MISMATCH" in run.codes
        and state(safe, attempt) == "UNKNOWN"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"side": "SELL"},
        {"product_id": "ETH-USDC"},
        {"base_qty": D("0.2")},
        {"post_only": False},
        {"order_type": "market_market_ioc"},
    ],
)
def test_every_field_of_the_order_is_compared(safe: SafetyEnv, change: dict[str, Any]) -> None:
    attempt, oid = working(safe)
    safe.fake.orders[oid] = replace(safe.fake.orders[oid], **change)
    assert "ORDER_MISMATCH" in safe.recon().codes and state(safe, attempt) == "UNKNOWN"


def test_an_unmapped_status_blocks_and_becomes_unknown(safe: SafetyEnv) -> None:
    attempt, oid = working(safe)
    safe.fake.set_status(oid, "UNKNOWN")
    run = safe.recon()
    assert (
        run.outcome == "MISMATCH"
        and "UNKNOWN_STATUS" in run.codes
        and state(safe, attempt) == "UNKNOWN"
    )


def test_an_order_that_fails_after_acceptance_is_a_status_mismatch(safe: SafetyEnv) -> None:
    attempt, oid = working(safe)
    safe.fake.set_status(oid, "FAILED")
    run = safe.recon()
    assert "STATUS_MISMATCH" in run.codes and state(safe, attempt) == "UNKNOWN"


def test_a_terminal_attempt_whose_order_comes_back_to_life_is_flagged(safe: SafetyEnv) -> None:
    attempt, oid = working(safe)
    safe.fake.set_status(oid, "CANCELLED")
    assert safe.recon().outcome == "OK" and state(safe, attempt) == "CANCELLED"
    safe.fake.set_status(oid, "WORKING")
    assert "STATUS_MISMATCH" in safe.recon().codes


# ------------------------------------------------------------------ fills
def test_a_taker_fill_on_a_post_only_order_is_an_anomaly_and_trips_the_breaker(
    safe: SafetyEnv, sql: Sql
) -> None:
    _, oid = working(safe)
    safe.fake.inject_fill(oid, "100", "0.1", "0.02", liquidity="TAKER")
    set_balances(safe, "39.98", "0.1")
    run = safe.recon()
    assert run.outcome == "MISMATCH" and "FILL_ANOMALY" in run.codes
    tick = safe.monitor.tick()
    assert tick.tripped == "TAKER_FILL_ON_POST_ONLY"
    row = safe.control_row()
    assert (row.bot_state, row.breaker_state) == ("PAUSED", "OPEN")
    assert (
        sql("SELECT count(*) AS n FROM audit_events WHERE event_code = 'bot.breaker_opened'")[0][
            "n"
        ]
        == 1
    )


def test_a_fill_worse_than_the_limit_is_an_anomaly(safe: SafetyEnv) -> None:
    _, oid = working(safe)
    safe.fake.inject_fill(oid, "100.5", "0.1", "0.02")
    set_balances(safe, "39.93", "0.1")
    assert "FILL_ANOMALY" in safe.recon().codes


def test_a_fill_for_an_order_the_bot_does_not_know_is_blocking(safe: SafetyEnv) -> None:
    working(safe)
    foreign = safe.fake.add_foreign_order()
    safe.fake.inject_fill(foreign, "100", "0.5", "0.1")
    codes = safe.recon().codes
    assert "UNKNOWN_FILL" in codes and "UNKNOWN_ORDER" in codes


def test_fills_that_do_not_add_up_to_the_order_block(safe: SafetyEnv) -> None:
    _, oid = working(safe)
    safe.fake.inject_fill(oid, "100", "0.1", "0.02")
    safe.fake.fills.clear()  # the order says filled, the fills list is empty
    set_balances(safe, "39.98", "0.1")
    assert "FILL_MISMATCH" in safe.recon().codes


def test_a_fill_listed_twice_is_recorded_once(safe: SafetyEnv, sql: Sql) -> None:
    _, oid = working(safe)
    fill = safe.fake.inject_fill(oid, "100", "0.1", "0.02")
    safe.fake.fills.append(fill)  # a duplicate in the listing
    set_balances(safe, "39.98", "0.1")
    run = safe.recon()
    assert sql("SELECT count(*) AS n FROM attempt_fills")[0]["n"] == 1
    assert run.outcome == "OK"  # the duplicate listing entry is deduplicated by fill id


# ------------------------------------------------------------------ balances
def test_a_wrong_balance_blocks(safe: SafetyEnv) -> None:
    working(safe)
    safe.fake.set_balance("USDC", "50.01")
    run = safe.recon()
    assert run.outcome == "MISMATCH" and run.codes == ("BALANCE_MISMATCH",)
    assert safe.monitor.tick().tripped == "BALANCE_MISMATCH"


def test_an_unexpected_currency_blocks(safe: SafetyEnv) -> None:
    working(safe)
    safe.fake.set_balance("DOGE", "5")
    assert "BALANCE_MISMATCH" in safe.recon().codes


def test_holds_do_not_change_the_total(safe: SafetyEnv) -> None:
    working(safe)
    safe.fake.set_balance("USDC", "39.9", hold="10.1")  # available + hold is still 50
    assert safe.recon().outcome == "OK"


# ------------------------------------------------------------------ read failures never look clean
@pytest.mark.parametrize("operation", ["list_orders", "list_fills", "list_accounts"])
def test_a_read_that_keeps_failing_gives_a_failed_run_and_changes_nothing(
    safe: SafetyEnv, sql: Sql, operation: str
) -> None:
    attempt, _ = working(safe)
    safe.fake.fail(operation, *[Fault("timeout")] * 3)
    before = sql("SELECT state, filled_qty FROM order_attempts")
    run = safe.recon()
    assert run.outcome == "FAILED" and run.failure_code == "TIMEOUT" and run.findings == ()
    assert sql("SELECT state, filled_qty FROM order_attempts") == before
    assert safe.fake.calls.count(operation) >= 3
    assert (
        sql("SELECT count(*) AS n FROM api_events WHERE NOT ok AND code = 'TIMEOUT'")[0]["n"] == 3
    )
    assert state(safe, attempt) == "WORKING"


def test_transient_read_errors_are_retried_with_backoff_and_then_succeed(safe: SafetyEnv) -> None:
    working(safe)
    before = safe.fake.calls.count("list_orders")
    safe.fake.fail("list_orders", Fault("timeout"), Fault("rate_limited"))
    safe.sleeps.clear()
    run = safe.recon()
    assert run.outcome == "OK" and safe.fake.calls.count("list_orders") - before == 3
    assert safe.sleeps == [0.375, 0.75]  # exponential, jitter fixed at the middle in the test


@pytest.mark.parametrize("fault", [Fault("malformed")])
def test_a_malformed_response_is_a_failed_run(safe: SafetyEnv, fault: Fault) -> None:
    working(safe)
    safe.fake.fail("list_orders", fault)
    run = safe.recon()
    assert run.outcome == "FAILED" and run.failure_code == "UNEXPECTED_RESPONSE"


def test_a_failed_run_is_recorded_and_blocks_resume_and_orders(safe: SafetyEnv, sql: Sql) -> None:
    working(safe)
    safe.fake.fail("list_orders", *[Fault("server_error")] * 3)
    assert safe.recon().outcome == "FAILED"
    result = safe.pipeline.submit(safe.proposal(), source="test", slot="after", book=safe.book())
    assert result.kind == "blocked" and "RECONCILIATION_FAILED" in result.reasons
    assert "reconciliation.failed" in [
        r["event_code"] for r in sql("SELECT event_code FROM audit_events")
    ]


def test_reconciliation_needs_a_recorded_starting_balance(safe: SafetyEnv) -> None:
    run = safe.recon()
    assert run.outcome == "FAILED" and run.failure_code == "NO_BASELINE"


def test_two_failed_runs_open_the_breaker(safe: SafetyEnv) -> None:
    working(safe)
    for _ in range(2):
        safe.fake.fail("list_orders", *[Fault("timeout")] * 3)
        assert safe.recon().outcome == "FAILED"
    assert safe.monitor.tick().tripped in ("RECONCILIATION_FAILURES", "API_FAILURES")


# ------------------------------------------------------------------ the feed is supplemental
def hint(safe: SafetyEnv, client: str, status: str, filled: str = "0") -> None:
    with safe.ctl.tx() as repos:
        repos.safety.add_hint(
            HintRow("FAKE", client, "hint-order-1", status, D(filled), 1, safe.clock.now())
        )


def test_a_hint_that_contradicts_rest_loses_and_changes_nothing(safe: SafetyEnv) -> None:
    attempt, oid = working(safe)
    hint(safe, safe.fake.orders[oid].client_order_id, "FILLED", "0.1")
    before = safe.attempt(attempt)
    run = safe.recon()
    assert run.outcome == "MISMATCH" and "WS_REST_CONFLICT" in run.codes
    after = safe.attempt(attempt)
    assert (
        (after.state, after.filled_qty) == (before.state, before.filled_qty) == ("WORKING", D("0"))
    )


def test_a_hint_for_an_unknown_order_blocks(safe: SafetyEnv) -> None:
    working(safe)
    hint(safe, "0aaaaaaa-1111-4222-8333-444444444444", "WORKING")
    assert "UNKNOWN_ORDER" in safe.recon().codes


def test_a_consistent_hint_is_harmless(safe: SafetyEnv) -> None:
    _, oid = working(safe)
    hint(safe, safe.fake.orders[oid].client_order_id, "WORKING")
    assert safe.recon().outcome == "OK"


def test_hints_alone_can_never_move_an_attempt(safe: SafetyEnv, sql: Sql) -> None:
    attempt, oid = working(safe)
    for status in ("FILLED", "CANCELLED", "EXPIRED", "FAILED", "UNKNOWN"):
        hint(safe, safe.fake.orders[oid].client_order_id, status)
    assert state(safe, attempt) == "WORKING"  # nothing but reconciliation changes attempts
    assert sql("SELECT count(*) AS n FROM order_hints")[0]["n"] == 5


# ------------------------------------------------------------------ absence
def test_an_order_that_appears_after_it_was_declared_absent_blocks(safe: SafetyEnv) -> None:
    safe.running()
    safe.fake.fail("submit", Fault("timeout"))
    first = safe.pipeline.submit(safe.proposal(), source="test", slot="a", book=safe.book())
    assert first.kind == "unknown"
    submitted = safe.attempt(first.attempt_id).submitting_at  # type: ignore[arg-type]
    safe.clock.advance(max((submitted - safe.clock.now()).total_seconds() + 125, 0))
    assert safe.recon().outcome == "OK"
    safe.clock.advance(5)
    assert safe.recon().outcome == "OK" and state(safe, first.attempt_id) == "ABSENT"  # type: ignore[arg-type]
    late = safe.attempt(first.attempt_id)  # type: ignore[arg-type]
    from app.exchange.gateway import OrderRequest

    safe.fake.submit(
        OrderRequest(str(late.client_order_id), "BTC-USDC", "BUY", D("100"), D("0.1"))
    )  # it shows up late
    run = safe.recon()
    assert run.outcome == "MISMATCH" and "ORDER_APPEARED_AFTER_ABSENCE" in run.codes
    assert safe.monitor.tick().tripped == "UNKNOWN_ORDER_FOUND"


def test_absence_is_not_declared_before_two_ok_runs_after_the_wait_window(safe: SafetyEnv) -> None:
    safe.running()
    safe.fake.fail("submit", Fault("timeout"))
    first = safe.pipeline.submit(safe.proposal(), source="test", slot="a", book=safe.book())
    for _ in range(5):  # many OK runs, but all inside the wait window
        safe.clock.advance(10)
        assert safe.recon().outcome == "OK"
    assert state(safe, first.attempt_id) == "UNKNOWN"  # type: ignore[arg-type]


# ------------------------------------------------------------------ cancelling known orders
def test_cancel_known_queues_cancels_and_only_reconciliation_confirms_them(
    safe: SafetyEnv, sql: Sql
) -> None:
    attempt, oid = working(safe)
    foreign = safe.fake.add_foreign_order()
    safe.act("kill")
    outcome = safe.commands.run_pending()
    assert outcome is not None and outcome.state == "DONE" and outcome.counts["queued"] == 1
    assert state(safe, attempt) == "CANCEL_REQUESTED"  # an HTTP success is not a cancellation
    assert safe.fake.orders[foreign].status == "WORKING"
    safe.fake.finish_cancel(oid)
    assert safe.recon().codes == (
        "UNKNOWN_ORDER",
    )  # the foreign order is still flagged, nothing else
    assert state(safe, attempt) == "CANCELLED"
    assert "bot.cancel_completed" in [
        r["event_code"] for r in sql("SELECT event_code FROM audit_events")
    ]
    assert safe.fake.calls.count("submit") == 1  # no new order, nothing sold


def test_a_failed_cancel_call_marks_the_command_failed_and_keeps_the_orders(
    safe: SafetyEnv, sql: Sql
) -> None:
    attempt, _ = working(safe)
    safe.act("kill")
    safe.fake.fail("cancel", Fault("timeout"))
    outcome = safe.commands.run_pending()
    assert outcome is not None and outcome.state == "FAILED" and outcome.failure_code == "TIMEOUT"
    assert state(safe, attempt) == "WORKING"
    (cmd,) = sql("SELECT state, failure_code FROM control_commands")
    assert (cmd["state"], cmd["failure_code"]) == ("FAILED", "TIMEOUT")
    assert "bot.cancel_failed" in [
        r["event_code"] for r in sql("SELECT event_code FROM audit_events")
    ]


def test_cancels_go_in_batches_of_at_most_one_hundred(
    safe: SafetyEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe.running()
    for i in range(5):
        assert (
            safe.pipeline.submit(
                safe.proposal(qty="0.05"), source="test", slot=f"b{i}", book=safe.book()
            ).kind
            == "submitted"
        )
    monkeypatch.setattr(commands, "BATCH", 2)
    safe.act("kill")
    outcome = safe.commands.run_pending()
    assert (
        outcome is not None
        and outcome.counts["requested"] == 5
        and safe.fake.calls.count("cancel") == 3
    )


def test_a_rejected_cancel_leaves_the_state_to_reconciliation(
    safe: SafetyEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt, oid = working(safe)
    safe.act("kill")
    monkeypatch.setattr(
        safe.fake,
        "cancel",
        lambda ids: {i: CancelResult("REJECTED", "ORDER_ALREADY_TERMINAL") for i in ids},
    )
    outcome = safe.commands.run_pending()
    assert outcome is not None and outcome.counts["rejected"] == 1
    assert state(safe, attempt) == "WORKING"
    del oid


def test_the_kill_switch_never_places_an_order_or_sells(safe: SafetyEnv) -> None:
    working(safe)
    submits = safe.fake.calls.count("submit")
    safe.act("kill")
    safe.commands.run_pending()
    safe.recon()
    assert safe.fake.calls.count("submit") == submits
    assert not any(o.side == "SELL" for o in safe.fake.orders.values())


def test_without_a_gateway_a_cancel_command_fails_loudly(safe: SafetyEnv) -> None:
    attempt, _ = working(safe)
    safe.act("kill")
    runner = commands.CommandRunner(
        storage=safe.ctl, clock=safe.clock, settings=safe.settings, gateways={}
    )
    outcome = runner.run_pending()
    assert (
        outcome is not None
        and outcome.state == "FAILED"
        and outcome.failure_code == "GATEWAY_UNAVAILABLE"
    )
    assert state(safe, attempt) == "WORKING"
