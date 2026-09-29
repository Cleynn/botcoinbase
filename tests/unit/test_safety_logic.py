"""Breaker policy, anomaly checks, retry policy, staleness, ledger replay and the live gate.
Pure code, SYNTHETIC numbers."""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import SafetySettings, Settings
from app.exchange.errors import ExchangeError
from app.safety import anomaly, breaker, ledger, live_gate, staleness
from app.safety.retry import RetryPolicy

D = Decimal
LIMITS = SafetySettings()
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ breaker
def test_no_signal_means_no_trip() -> None:
    assert breaker.evaluate(breaker.BreakerSignals(), LIMITS) is None


@pytest.mark.parametrize(
    ("signals", "reason"),
    [
        (breaker.BreakerSignals(recent_api_failures=LIMITS.api_failure_threshold), "API_FAILURES"),
        (breaker.BreakerSignals(consecutive_reconcile_failures=2), "RECONCILIATION_FAILURES"),
        (breaker.BreakerSignals(unknown_order_found=True), "UNKNOWN_ORDER_FOUND"),
        (breaker.BreakerSignals(loss_limit_hit=True), "LOSS_LIMIT"),
        (breaker.BreakerSignals(drawdown_hit=True), "DRAWDOWN_LIMIT"),
        (
            breaker.BreakerSignals(anomalies=(anomaly.Anomaly("CANDLE_JUMP", "TRIP"),)),
            "CANDLE_JUMP",
        ),
        (
            breaker.BreakerSignals(anomalies=(anomaly.Anomaly("TAKER_FILL_ON_POST_ONLY", "TRIP"),)),
            "TAKER_FILL_ON_POST_ONLY",
        ),
        (breaker.BreakerSignals(anomalies=(anomaly.Anomaly("ORDER_RATE", "TRIP"),)), "ORDER_RATE"),
        (
            breaker.BreakerSignals(anomalies=(anomaly.Anomaly("REJECT_STORM", "TRIP"),)),
            "REJECT_STORM",
        ),
    ],
)
def test_each_signal_trips_with_its_reason(signals: breaker.BreakerSignals, reason: str) -> None:
    assert breaker.evaluate(signals, LIMITS) == reason
    assert reason in breaker.TRIP_REASONS


def test_below_threshold_or_warn_only_does_not_trip() -> None:
    quiet = breaker.BreakerSignals(
        recent_api_failures=LIMITS.api_failure_threshold - 1,
        consecutive_reconcile_failures=1,
        anomalies=(anomaly.Anomaly("SPREAD_SPIKE", "WARN"), anomaly.Anomaly("WS_SILENT", "WARN")),
    )
    assert breaker.evaluate(quiet, LIMITS) is None


def test_the_trip_reason_is_chosen_by_a_fixed_priority() -> None:
    both = breaker.BreakerSignals(recent_api_failures=99, loss_limit_hit=True, drawdown_hit=True)
    assert breaker.evaluate(both, LIMITS) == "API_FAILURES"
    assert (
        breaker.evaluate(breaker.BreakerSignals(loss_limit_hit=True, drawdown_hit=True), LIMITS)
        == "LOSS_LIMIT"
    )


# ------------------------------------------------------------------ anomalies
def test_candle_jump() -> None:
    assert anomaly.candle_jump(D("100"), D("130"), D("0.25")) == anomaly.Anomaly(
        "CANDLE_JUMP", "TRIP"
    )
    assert anomaly.candle_jump(D("100"), D("110"), D("0.25")) is None
    assert anomaly.candle_jump(None, D("110"), D("0.25")) is None
    assert anomaly.candle_jump(D("0"), D("1"), D("0.25")) is None


def test_taker_fill_on_a_post_only_order_trips() -> None:
    assert anomaly.fill_liquidity("TAKER", True) is not None
    assert anomaly.fill_liquidity("MAKER", True) is None
    assert anomaly.fill_liquidity("UNKNOWN", True) is None
    assert anomaly.fill_liquidity("TAKER", False) is None


@pytest.mark.parametrize(
    ("side", "limit", "fill", "bad"),
    [
        ("BUY", "100", "100", False),
        ("BUY", "100", "99", False),
        ("BUY", "100", "100.01", True),
        ("SELL", "100", "100", False),
        ("SELL", "100", "101", False),
        ("SELL", "100", "99.99", True),
    ],
)
def test_a_limit_order_never_fills_worse_than_its_limit(
    side: str, limit: str, fill: str, bad: bool
) -> None:
    assert (anomaly.fill_price(side, D(limit), D(fill)) is not None) is bad


def test_balance_rate_reject_and_silence_checks() -> None:
    assert anomaly.balance_mismatch(D("1"), D("1")) is None
    assert anomaly.balance_mismatch(D("1"), D("1.00000001")) is not None
    assert anomaly.order_rate(10, 10) is None and anomaly.order_rate(11, 10) is not None
    assert anomaly.reject_storm(4, 5) is None and anomaly.reject_storm(5, 5) is not None
    assert anomaly.ws_silence(None, 30) is not None
    assert anomaly.ws_silence(31, 30) is not None and anomaly.ws_silence(30, 30) is None
    assert anomaly.spread_spike(D("40"), D("10"), D("3")) is not None
    assert anomaly.spread_spike(D("20"), D("10"), D("3")) is None
    assert anomaly.spread_spike(None, D("10"), D("3")) is None


# ------------------------------------------------------------------ staleness
def test_freshness_classification() -> None:
    assert staleness.classify(0, 60) == "FRESH" and staleness.classify(60, 60) == "FRESH"
    assert staleness.classify(61, 60) == "STALE"
    assert staleness.classify(None, 60) == "UNKNOWN" and staleness.classify(-1, 60) == "UNKNOWN"
    assert staleness.age_seconds(T0, T0 - timedelta(seconds=90)) == 90
    assert staleness.age_seconds(T0, None) is None
    assert staleness.candle_data_age(1_000_600, 1_000_000) == 300  # the candle ended 300 s ago
    assert staleness.candle_data_age(1_000_600, None) is None


# ------------------------------------------------------------------ retry policy
POLICY = RetryPolicy(max_attempts=3, base_seconds=D("0.5"), cap_seconds=D("8"))


def test_backoff_is_exponential_jittered_and_capped() -> None:
    lo = [POLICY.delay(n, lambda: 0.0) for n in (1, 2, 3, 4, 5, 6)]
    hi = [POLICY.delay(n, lambda: 1.0) for n in (1, 2, 3, 4, 5, 6)]
    assert lo == [0.25, 0.5, 1.0, 2.0, 4.0, 4.0]
    assert hi == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]


def test_a_read_is_retried_on_transient_errors_only_and_never_beyond_the_bound() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ExchangeError("TIMEOUT")
        return "ok"

    assert POLICY.run_read(flaky, sleep=sleeps.append, rng=lambda: 0.5) == "ok"
    assert len(calls) == 3 and len(sleeps) == 2

    always = 0

    def down() -> str:
        nonlocal always
        always += 1
        raise ExchangeError("SERVER_ERROR", status=503)

    with pytest.raises(ExchangeError):
        POLICY.run_read(down, sleep=lambda _s: None)
    assert always == 3


@pytest.mark.parametrize(
    "code", ["AUTH_REJECTED", "BAD_REQUEST", "NO_CREDENTIALS", "UNEXPECTED_RESPONSE"]
)
def test_a_permanent_error_is_not_retried(code: str) -> None:
    calls = 0

    def bad() -> str:
        nonlocal calls
        calls += 1
        raise ExchangeError(code)

    with pytest.raises(ExchangeError):
        POLICY.run_read(bad, sleep=lambda _s: None)
    assert calls == 1


def test_there_is_no_retry_helper_for_orders() -> None:
    source = Path(__file__).resolve().parents[2].joinpath("app/safety/retry.py").read_text()
    names = {n.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)}
    assert "run_read" in names and not {n for n in names if "submit" in n or "order" in n}


def test_retry_settings_are_bounded() -> None:
    assert SafetySettings().retry_max_attempts <= 5
    with pytest.raises(ValueError):
        SafetySettings(retry_max_attempts=6)


# ------------------------------------------------------------------ ledger replay
def fill(
    product: str, side: str, price: str, size: str, fee: str, minutes: int
) -> ledger.FillEvent:
    return ledger.FillEvent(
        product, side, D(price), D(size), D(fee), T0 + timedelta(minutes=minutes)
    )


def test_replay_moves_cash_and_inventory_exactly() -> None:
    state = ledger.replay(
        {"USDC": D("50")},
        [
            fill("BTC-USDC", "BUY", "100", "0.1", "0.02", 1),
            fill("BTC-USDC", "SELL", "110", "0.1", "0.022", 2),
        ],
    )
    assert state.balances["USDC"] == D("50") - D("10.02") + D("10.978")
    assert state.balances["BTC"] == D("0")
    assert state.realized == D("10.978") - D("10.02")
    assert state.positions["BTC-USDC"].qty == 0 and state.positions["BTC-USDC"].cost == 0


def test_partial_sell_realises_a_proportional_cost() -> None:
    state = ledger.replay(
        {"USDC": D("50")},
        [
            fill("BTC-USDC", "BUY", "100", "0.2", "0", 1),
            fill("BTC-USDC", "SELL", "105", "0.1", "0", 2),
        ],
    )
    assert state.positions["BTC-USDC"].qty == D("0.1") and state.positions["BTC-USDC"].cost == D(
        "10"
    )
    assert state.realized == D("0.5")


def test_equity_marks_inventory_and_never_invents_a_gain_without_a_mark() -> None:
    state = ledger.replay({"USDC": D("50")}, [fill("BTC-USDC", "BUY", "100", "0.1", "0", 1)])
    current, peak, day_open = ledger.equity_view(state, D("50"), day_start=T0, current_marks={})
    assert current == D("50")  # valued at cost with no mark
    lower, _, _ = ledger.equity_view(
        state, D("50"), day_start=T0, current_marks={"BTC-USDC": D("90")}
    )
    assert lower == D("49")
    assert peak == D("50") and day_open == D("50")


def test_day_open_uses_the_last_point_before_the_day_started() -> None:
    fills = [
        fill("BTC-USDC", "BUY", "100", "0.1", "0", -120),
        fill("BTC-USDC", "SELL", "90", "0.1", "0", 5),
    ]
    state = ledger.replay({"USDC": D("50")}, fills)
    _, _, day_open = ledger.equity_view(state, D("50"), day_start=T0, current_marks={})
    assert day_open == D("50")  # equity after the buy, marked at its own price
    current, peak, _ = ledger.equity_view(state, D("50"), day_start=T0, current_marks={})
    assert current == D("49") and peak == D("50")


def test_replay_is_order_independent_for_the_same_times() -> None:
    a = fill("BTC-USDC", "BUY", "100", "0.1", "0", 1)
    b = fill("BTC-USDC", "SELL", "101", "0.05", "0", 2)
    assert (
        ledger.replay({"USDC": D("50")}, [a, b]).balances
        == ledger.replay({"USDC": D("50")}, [b, a]).balances
    )


# ------------------------------------------------------------------ live gate
def test_the_panel_is_blocked_and_lists_every_unmet_condition(
    settings: Settings | None = None,
) -> None:
    result = live_gate.panel()
    assert result.status == "BLOCKED" and live_gate.STATUS_TEXT == "LIVE TRADING BLOCKED"
    assert set(result.reasons) == set(live_gate.BLOCKERS) and len(result.reasons) >= 12


def test_the_gate_module_contains_no_path_that_opens_it() -> None:
    source = Path(__file__).resolve().parents[2].joinpath("app/safety/live_gate.py").read_text()
    tree = ast.parse(source)
    literals = {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert not {v for v in literals if v.upper() in {"OPEN", "ALLOWED", "LIVE"}}
    assert live_gate.LIVE_UNBLOCK_PRESENT is False
    assert not re.search(r"os\.environ|getenv|open\(|read_text", source)


def test_a_status_other_than_blocked_cannot_be_constructed_by_the_panel() -> None:
    for _ in range(3):
        assert live_gate.panel().status == "BLOCKED"


@pytest.fixture
def cfg() -> Settings:
    from app.config import load_settings
    from tests.conftest import ROOT  # noqa: F401

    return load_settings(
        {"TD_ENVIRONMENT": "development", "TD_MODE": "PAPER", "TD_SECRET_KEY": "x" * 48}
    )


def test_order_gate_passes_only_non_live_venues_in_a_representable_mode(cfg: Settings) -> None:
    assert live_gate.order_gate("PAPER", cfg) == (True, None)
    assert live_gate.order_gate("FAKE", cfg) == (True, None)
    for venue in ("LIVE", "CBE", "COINBASE", "LIVE_READ", "", "paper"):
        assert live_gate.order_gate(venue, cfg) == (False, "LIVE_GATE_BLOCKED")


def test_the_fake_venue_is_refused_in_production(cfg: Settings) -> None:
    prod = cfg.model_copy(update={"environment": "production"})
    assert live_gate.order_gate("FAKE", prod) == (False, "LIVE_GATE_BLOCKED")


def test_an_unrepresentable_mode_blocks(cfg: Settings) -> None:
    for mode in ("LIVE", "live", "", "PAPER "):
        assert live_gate.order_gate("PAPER", cfg.model_copy(update={"mode": mode}))[0] is False


def test_an_unreadable_settings_object_fails_closed() -> None:
    class Broken:
        @property
        def mode(self) -> str:
            raise RuntimeError("unreadable")

    assert live_gate.order_gate("PAPER", Broken()) == (False, "LIVE_GATE_BLOCKED")  # type: ignore[arg-type]
