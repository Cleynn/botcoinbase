"""Startup recovery, the circuit-breaker monitor and the paper gate. SYNTHETIC, no network."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from app.exchange.fake import Fault
from app.paper.exchange import PaperError, PaperExchange
from app.safety.monitor import SafetyMonitor
from app.safety.recovery import RecoveryService
from app.storage.safety_repositories import FillRow
from tests.integration.safety_env import SafetyEnv

Sql = Callable[..., list[dict[str, Any]]]
D = Decimal


def audit(sql: Sql, prefix: str = "bot.") -> list[str]:
    return [
        r["event_code"]
        for r in sql(
            "SELECT event_code FROM audit_events WHERE event_code LIKE %s ORDER BY seq",
            (prefix + "%",),
        )
    ]


# ------------------------------------------------------------------ startup recovery
def test_recovery_reconciles_first_and_never_resumes(safe: SafetyEnv, sql: Sql) -> None:
    safe.baseline()
    result = safe.recover()
    assert result.complete and result.run_outcome == "OK" and result.blockers == ()
    row = safe.control_row()
    assert (row.bot_state, row.recovery_state) == (
        "PAUSED",
        "COMPLETE",
    ) and row.boot_id == result.boot_id
    events = [
        r["event_code"]
        for r in sql("SELECT event_code FROM audit_events ORDER BY seq")
        if r["event_code"].startswith(("bot.", "reconciliation."))
    ]
    assert events == ["bot.recovery_started", "reconciliation.ok", "bot.recovery_completed"]


def test_every_start_takes_a_new_boot_id_and_pauses_a_running_bot(safe: SafetyEnv) -> None:
    safe.running()
    first = safe.control_row().boot_id
    result = safe.recover(new_process=True)
    row = safe.control_row()
    assert row.boot_id != first and row.boot_id == result.boot_id
    assert result.complete and row.bot_state == "PAUSED"  # a restart never leaves the bot RUNNING


def test_recovery_with_no_exchange_reader_cannot_complete(safe: SafetyEnv) -> None:
    safe.baseline()
    service = RecoveryService(
        storage=safe.ctl, clock=safe.clock, settings=safe.settings, reconciler=None
    )
    result = service.run()
    assert not result.complete and result.blockers == ("NO_EXCHANGE_READER",)
    row = safe.control_row()
    assert (row.bot_state, row.recovery_state) == ("PAUSED", "INCOMPLETE")


def test_recovery_stays_incomplete_when_reconciliation_fails_or_mismatches(safe: SafetyEnv) -> None:
    safe.baseline()
    safe.fake.fail("list_orders", *[Fault("timeout")] * 3)
    failed = safe.recover()
    assert not failed.complete and failed.blockers == ("RECONCILIATION_FAILED",)
    safe.fake.add_foreign_order()
    mismatch = safe.recover()
    assert not mismatch.complete and mismatch.blockers == ("RECONCILIATION_MISMATCH",)
    assert safe.control_row().recovery_state == "INCOMPLETE"


def test_incomplete_recovery_blocks_orders(safe: SafetyEnv) -> None:
    safe.running()
    safe.fake.add_foreign_order()
    assert not safe.recover().complete
    result = safe.pipeline.submit(safe.proposal(), source="test", slot="r", book=safe.book())
    assert result.kind == "blocked" and {"RECOVERY_INCOMPLETE", "BOT_NOT_RUNNING"} <= set(
        result.reasons
    )


def test_recovery_is_repeatable_until_it_converges(safe: SafetyEnv) -> None:
    safe.baseline()
    foreign = safe.fake.add_foreign_order()
    assert not safe.recover().complete
    safe.fake.set_status(foreign, "CANCELLED")  # the operator dealt with it
    assert safe.recover().complete


# ------------------------------------------------------------------ the monitor and the breaker
def test_a_quiet_system_does_not_trip(safe: SafetyEnv) -> None:
    safe.running()
    tick = safe.monitor.tick()
    assert tick.tripped is None and safe.control_row().breaker_state == "CLOSED"


def test_api_failures_open_the_breaker_and_pause_the_bot(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    with safe.ctl.tx() as repos:
        for _ in range(safe.settings.safety.api_failure_threshold):
            repos.safety.add_api_event("FAKE", "list_orders", False, "TIMEOUT", safe.clock.now())
    assert safe.monitor.tick().tripped == "API_FAILURES"
    row = safe.control_row()
    assert (row.bot_state, row.breaker_state, row.breaker_reason) == (
        "PAUSED",
        "OPEN",
        "API_FAILURES",
    )
    assert row.breaker_cooldown_until is not None and row.breaker_cooldown_until > safe.clock.now()
    assert safe.monitor.tick().tripped == "API_FAILURES"  # still true, but it is not opened twice
    assert audit(sql).count("bot.breaker_opened") == 1


def test_old_api_failures_age_out_of_the_window(safe: SafetyEnv) -> None:
    safe.running()
    with safe.ctl.tx() as repos:
        for _ in range(9):
            repos.safety.add_api_event("FAKE", "list_orders", False, "TIMEOUT", safe.clock.now())
    safe.clock.advance(safe.settings.safety.api_failure_window_seconds + 1)
    assert safe.monitor.tick().tripped is None


def losing_trade(safe: SafetyEnv, sell_price: str) -> None:
    """Buy 0.1 at 100, sell 0.1 at `sell_price` (recorded fills on two attempts)."""
    for side, price in (("BUY", "100"), ("SELL", sell_price)):
        intent = safe.make_intent(side=side, price=price, qty="0.1")
        attempt = safe.new_attempt(intent=intent)
        safe.walk(attempt, "SUBMITTING", "WORKING", "FILLED")
        with safe.ctl.tx() as repos:
            repos.safety.add_fill(
                FillRow(
                    attempt,
                    "FAKE",
                    f"fill-{side}-0001",
                    side,
                    D(price),
                    D("0.1"),
                    D("0"),
                    "MAKER",
                    safe.clock.now(),
                )
            )
        safe.clock.advance(1)


def test_the_daily_loss_limit_opens_the_breaker(safe: SafetyEnv) -> None:
    safe.running()
    losing_trade(safe, "60")  # 50 -> 46: a 4 USDC loss against a 3 USDC limit
    assert safe.monitor.tick().tripped == "LOSS_LIMIT"


def test_the_drawdown_limit_opens_the_breaker(safe: SafetyEnv) -> None:
    safe.running()
    relaxed = safe.settings.model_copy(
        update={"safety": safe.settings.safety.model_copy(update={"daily_loss_limit": D("10")})}
    )
    monitor = SafetyMonitor(storage=safe.ctl, clock=safe.clock, settings=relaxed, host=safe.host)
    losing_trade(safe, "40")  # 50 -> 44: 12% below the peak, under the 10 USDC daily limit
    assert monitor.tick().tripped == "DRAWDOWN_LIMIT"


def test_a_burst_of_intents_opens_the_breaker(safe: SafetyEnv) -> None:
    safe.running()
    for _ in range(safe.settings.safety.max_intents_per_minute + 1):
        safe.make_intent()
    assert safe.monitor.tick().tripped == "ORDER_RATE"


def test_a_streak_of_rejections_opens_the_breaker(safe: SafetyEnv) -> None:
    safe.running()
    for _ in range(safe.settings.safety.max_reject_streak):
        safe.walk(safe.new_attempt(intent=safe.make_intent()), "SUBMITTING", "REJECTED")
        safe.clock.advance(1)
    assert safe.monitor.tick().tripped == "REJECT_STORM"


def test_a_breaker_trip_cancels_nothing_and_sells_nothing(safe: SafetyEnv) -> None:
    safe.running()
    assert (
        safe.pipeline.submit(safe.proposal(), source="test", slot="b", book=safe.book()).kind
        == "submitted"
    )
    assert safe.host.trip_breaker("API_FAILURES")
    assert next(iter(safe.fake.orders.values())).status == "WORKING"
    assert safe.fake.calls.count("cancel") == 0 and safe.fake.calls.count("submit") == 1


@pytest.mark.parametrize("reason", ["NOT_A_REASON", "", "api_failures"])
def test_only_known_trip_reasons_are_accepted(safe: SafetyEnv, reason: str) -> None:
    with pytest.raises(ValueError):
        safe.host.trip_breaker(reason)


def test_the_host_kill_and_release_need_their_exact_phrases(safe: SafetyEnv, sql: Sql) -> None:
    assert safe.host.activate_kill("activate kill switch").kind == "phrase_mismatch"
    assert safe.control_row().kill_switch == "INACTIVE"
    assert safe.host.activate_kill("ACTIVATE KILL SWITCH").kind == "ok"
    assert safe.host.release_kill("release kill switch").kind == "phrase_mismatch"
    assert safe.control_row().kill_switch == "ACTIVE"
    assert "bot.control_denied" in audit(sql)


# ------------------------------------------------------------------ the paper venue honours kill and breaker
def paper(safe: SafetyEnv) -> PaperExchange:
    return PaperExchange(storage=safe.ctl, clock=safe.clock, settings=safe.settings)


def test_paper_starts_when_nothing_forbids_it(safe: SafetyEnv) -> None:
    exchange = paper(safe)
    assert exchange.start().state == "RUNNING"
    assert exchange.cancel_for_safety("TEST") >= 0
    assert exchange.status().state == "PAUSED"


def test_paper_refuses_to_start_under_the_kill_switch_or_an_open_breaker(safe: SafetyEnv) -> None:
    safe.act("kill")
    with pytest.raises(PaperError, match="KILL_SWITCH_ACTIVE"):
        paper(safe).start()
    assert safe.commands.run_pending() is not None
    assert safe.host.release_kill("RELEASE KILL SWITCH").kind == "ok"
    assert safe.host.trip_breaker("API_FAILURES")
    with pytest.raises(PaperError, match="BREAKER_OPEN"):
        paper(safe).start()


@pytest.mark.parametrize("cause", ["kill", "breaker"])
def test_a_running_paper_session_is_paused_by_the_next_step(
    safe: SafetyEnv, sql: Sql, cause: str
) -> None:
    exchange = paper(safe)
    exchange.start()
    before = sql("SELECT count(*) AS n, COALESCE(sum(base_qty), 0) AS q FROM paper_positions")[0]
    if cause == "kill":
        assert safe.host.activate_kill("ACTIVATE KILL SWITCH").kind == "ok"
    else:
        assert safe.host.trip_breaker("LOSS_LIMIT")
    result = exchange.step()
    assert result.placed == 0 and exchange.status().state == "PAUSED"
    reason = sql(
        "SELECT reason_code FROM audit_events WHERE event_code = 'paper.stopped' ORDER BY seq"
    )[-1]["reason_code"]
    assert reason == ("KILL_SWITCH_ACTIVE" if cause == "kill" else "BREAKER_OPEN")
    after = sql("SELECT count(*) AS n, COALESCE(sum(base_qty), 0) AS q FROM paper_positions")[0]
    assert after == before  # cancelled orders only: no inventory was sold


def test_the_paper_cancel_command_path_is_used_by_the_host_runner(
    safe: SafetyEnv, sql: Sql
) -> None:
    from app.safety.commands import CommandRunner

    exchange = paper(safe)
    exchange.start()
    safe.act("kill")
    runner = CommandRunner(
        storage=safe.ctl,
        clock=safe.clock,
        settings=safe.settings,
        gateways={},
        paper_cancel=exchange.cancel_for_safety,
    )
    outcome = runner.run_pending()
    assert outcome is not None and outcome.state == "DONE" and "paper_cancelled" in outcome.counts
    assert exchange.status().state == "PAUSED"
