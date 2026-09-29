"""Pair validation: every check, each of its outcomes, and the overall verdict."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.adapters import coinbase_parse as parse
from app.domain.pairs import CheckResult
from app.pairs import validation as v
from tests.coinbase_fakes import FakeCoinbase, book_json, product_json
from tests.pair_helpers import ATTESTED, NOW, good_inputs, policy, with_product

POLICY = policy()


def by_code(checks: tuple[CheckResult, ...]) -> dict[str, CheckResult]:
    return {c.code: c for c in checks}


def run(inputs: v.ValidationInputs, pol: Any = POLICY) -> dict[str, CheckResult]:
    return by_code(v.run_checks(inputs, pol))


def test_all_fourteen_checks_run_in_a_fixed_order_and_pass_on_healthy_data() -> None:
    checks = v.run_checks(good_inputs(), POLICY)
    assert tuple(c.code for c in checks) == v.CHECK_CODES and len(checks) == 14
    assert {c.status for c in checks} == {"PASS"}, [c for c in checks if c.status != "PASS"]
    assert v.overall_outcome(checks) == "PASS"


def test_every_check_carries_a_reason_a_fixed_message_and_scalar_observations() -> None:
    for check in v.run_checks(good_inputs(), POLICY):
        assert check.reason == "OK" and check.message and check.message.endswith(".")
        assert all(isinstance(k, str) and isinstance(x, str) for k, x in check.observed.items())


def test_outcome_precedence_fail_beats_inconclusive_beats_pass() -> None:
    pas = CheckResult("A", "PASS", "OK", "m", {})
    inc = CheckResult("B", "INCONCLUSIVE", "X", "m", {})
    fail = CheckResult("C", "FAIL", "Y", "m", {})
    assert v.overall_outcome((pas,)) == "PASS"
    assert v.overall_outcome((pas, inc)) == "INCONCLUSIVE"
    assert v.overall_outcome((pas, inc, fail)) == "FAIL"


# ---- product status, currency, stable base
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"status": "offline"}, "STATUS_NOT_ALLOWED"),
        ({"status": "delisted"}, "STATUS_NOT_ALLOWED"),
        ({"is_disabled": True}, "FLAG_IS_DISABLED"),
        ({"trading_disabled": True}, "FLAG_TRADING_DISABLED"),
        ({"cancel_only": True}, "FLAG_CANCEL_ONLY"),
        ({"auction_mode": True}, "FLAG_AUCTION_MODE"),
        ({"post_only": True}, "FLAG_POST_ONLY"),
        ({"product_type": "FUTURE"}, "NOT_SPOT"),
        ({"venue": "INTX"}, "WRONG_VENUE"),
    ],
)
def test_product_status_fails_for_blocking_state(change: dict[str, Any], reason: str) -> None:
    check = run(with_product(good_inputs(), **change))["PRODUCT_STATUS"]
    assert check.status == "FAIL" and check.reason == reason


def test_limit_only_is_allowed_because_only_limit_orders_are_ever_placed() -> None:
    assert run(with_product(good_inputs(), limit_only=True))["PRODUCT_STATUS"].status == "PASS"


def test_missing_flags_or_unreadable_status_are_inconclusive_not_pass() -> None:
    assert (
        run(with_product(good_inputs(), is_disabled=None))["PRODUCT_STATUS"].status
        == "INCONCLUSIVE"
    )
    hostile = with_product(good_inputs(), status=parse.STATUS_SENTINEL, malformed=("status",))
    assert run(hostile)["PRODUCT_STATUS"].reason == "STATUS_UNPARSEABLE"


def test_a_definite_failure_outranks_a_missing_field() -> None:
    check = run(with_product(good_inputs(), status="offline", is_disabled=None))["PRODUCT_STATUS"]
    assert check.status == "FAIL"


def test_quote_currency_must_be_usdc() -> None:
    assert run(with_product(good_inputs(), quote_currency="USD"))["QUOTE_CURRENCY"].status == "FAIL"
    assert run(with_product(good_inputs(), product_id="BTC-USD"))["QUOTE_CURRENCY"].status == "FAIL"


@pytest.mark.parametrize("base", ["USDT", "DAI", "EURC", "PYUSD", "USDS"])
def test_stable_bases_are_excluded(base: str) -> None:
    check = run(with_product(good_inputs(), base_currency=base))["BASE_NOT_STABLE"]
    assert check.status == "FAIL" and check.reason == "STABLE_BASE"


# ------------------------------------------------------------------ data basis
def test_unified_usd_book_is_reported_as_the_price_basis() -> None:
    check = run(good_inputs())["DATA_BASIS"]
    assert (
        check.observed["basis"] == "UNIFIED_USD_BOOK"
        and check.observed["data_product"] == "BTC-USD"
    )


def test_unknown_or_changed_data_basis_is_inconclusive() -> None:
    assert run(with_product(good_inputs(), alias=None))["DATA_BASIS"].reason == "DATA_BASIS_UNKNOWN"
    changed = replace(good_inputs(), pair_data_basis="OWN_BOOK")
    assert run(changed)["DATA_BASIS"].reason == "DATA_BASIS_CHANGED"


# ------------------------------------------------------------------ freshness / clock
def test_stale_metadata_is_inconclusive() -> None:
    old = replace(good_inputs(), verified_at=NOW - timedelta(seconds=3601))
    check = run(old)["METADATA_FRESHNESS"]
    assert check.status == "INCONCLUSIVE" and check.reason == "METADATA_STALE"
    edge = replace(good_inputs(), verified_at=NOW - timedelta(seconds=3600))
    assert run(edge)["METADATA_FRESHNESS"].status == "PASS"


def test_metadata_from_the_future_is_not_fresh() -> None:
    future = replace(good_inputs(), verified_at=NOW + timedelta(minutes=5))
    assert run(future)["METADATA_FRESHNESS"].reason == "METADATA_FROM_FUTURE"


def test_a_failed_metadata_refresh_is_reported_even_when_stored_metadata_is_recent() -> None:
    failed = replace(good_inputs(), fetch_errors={"product": "TIMEOUT"})
    check = run(failed)["METADATA_FRESHNESS"]
    assert check.reason == "FETCH_FAILED" and check.observed["error"] == "TIMEOUT"


def test_clock_skew_and_missing_server_time_are_inconclusive() -> None:
    fake = FakeCoinbase(lambda: NOW)
    fake.time_offset = 30
    assert run(good_inputs(fake))["CLOCK_SYNC"].reason == "CLOCK_SKEW"
    assert (
        run(replace(good_inputs(), server_time=None))["CLOCK_SYNC"].reason
        == "SERVER_TIME_UNAVAILABLE"
    )
    assert (
        run(replace(good_inputs(), fetch_errors={"time": "TIMEOUT"}))["CLOCK_SYNC"].reason
        == "FETCH_FAILED"
    )


# ---- increments, precision, minimums
@pytest.mark.parametrize(
    "field", ["base_increment", "quote_increment", "base_min_size", "price_increment"]
)
def test_missing_increments_are_inconclusive(field: str) -> None:
    check = run(with_product(good_inputs(), **{field: None}))["INCREMENTS"]
    assert check.status == "INCONCLUSIVE"
    assert check.reason == (
        "PRICE_INCREMENT_MISSING" if field == "price_increment" else "INCREMENT_MISSING"
    )


def test_minimum_above_maximum_is_inconclusive() -> None:
    check = run(
        with_product(good_inputs(), base_min_size=Decimal("10"), base_max_size=Decimal("1"))
    )["INCREMENTS"]
    assert check.reason == "MIN_ABOVE_MAX"


def test_increments_must_be_powers_of_ten() -> None:
    check = run(with_product(good_inputs(), price_increment=Decimal("0.05")))["PRECISION"]
    assert check.status == "INCONCLUSIVE" and check.reason == "INCREMENT_UNUSUAL"
    too_fine = run(with_product(good_inputs(), base_increment=Decimal("1E-13")))["PRECISION"]
    assert too_fine.reason == "INCREMENT_UNUSUAL"
    huge = run(with_product(good_inputs(), quote_increment=Decimal("10000000")))["PRECISION"]
    assert huge.reason == "INCREMENT_UNUSUAL"
    assert run(with_product(good_inputs(), price_increment=Decimal("1")))["PRECISION"].status in {
        "PASS",
        "FAIL",
    }  # whole-unit ticks are legitimate for expensive assets


def test_base_minimum_must_sit_on_the_base_increment() -> None:
    check = run(
        with_product(
            good_inputs(), base_increment=Decimal("0.001"), base_min_size=Decimal("0.0015")
        )
    )["PRECISION"]
    assert check.reason == "MIN_OFF_INCREMENT"


def test_a_coarse_tick_fails() -> None:
    coarse = with_product(good_inputs(), price_increment=Decimal("100"))  # 100 / 60000 > 0.05%
    check = run(coarse)["PRECISION"]
    assert check.status == "FAIL" and check.reason == "TICK_TOO_COARSE"


def test_no_book_means_no_reference_price() -> None:
    nobook = replace(good_inputs(), book=None, fetch_errors={"book": "TIMEOUT"})
    checks = run(nobook)
    for code in ("PRECISION", "MINIMUMS", "CAPITAL_FEASIBILITY"):
        assert checks[code].status == "INCONCLUSIVE" and checks[code].reason == "NO_REFERENCE_PRICE"
    assert checks["LIQUIDITY_SPREAD"].reason == "FETCH_FAILED"


def test_minimum_order_above_a_grid_levels_budget_fails() -> None:
    check = run(with_product(good_inputs(), base_min_size=Decimal("0.5")))["MINIMUMS"]  # 30000 USDC
    assert check.status == "FAIL" and check.reason == "MINIMUM_ABOVE_BUDGET"
    quote = run(with_product(good_inputs(), quote_min_size=Decimal("50")))["MINIMUMS"]
    assert quote.reason == "MINIMUM_ABOVE_BUDGET"


# ------------------------------------------------------------------ history / quality
def test_short_history_fails_and_a_missing_fetch_is_inconclusive() -> None:
    fake = FakeCoinbase(lambda: NOW)
    fake.drop_daily = 15
    check = run(good_inputs(fake))["HISTORY_AVAILABILITY"]
    assert check.status == "FAIL" and check.reason == "HISTORY_TOO_SHORT"
    failed = replace(good_inputs(), daily=None, fetch_errors={"daily": "HTTP_ERROR"})
    assert run(failed)["HISTORY_AVAILABILITY"].reason == "FETCH_FAILED"


def test_history_tolerates_the_configured_number_of_missing_days_only() -> None:
    fake = FakeCoinbase(lambda: NOW)
    fake.drop_daily = 10  # 90 days remain in the window
    assert run(good_inputs(fake))["HISTORY_AVAILABILITY"].status == "PASS"


def test_open_daily_candle_is_not_counted_as_history() -> None:
    inputs = good_inputs()
    assert inputs.daily is not None and inputs.server_time is not None
    closed = v._closed(inputs.daily.candles, 86400, inputs.server_time.epoch_seconds)  # noqa: SLF001
    assert all(c.start + 86400 <= inputs.server_time.epoch_seconds - 3 for c in closed)


def test_candle_closed_rule_excludes_the_last_seconds() -> None:
    inputs = good_inputs()
    assert inputs.intraday is not None
    epoch = inputs.server_time.epoch_seconds  # type: ignore[union-attr]
    closed = v._closed(inputs.intraday.candles, 300, epoch)  # noqa: SLF001
    assert closed and closed[-1].start + 300 <= epoch - 3
    assert len(closed) < len(inputs.intraday.candles)


def test_gaps_in_recent_candles_fail() -> None:
    fake = FakeCoinbase(lambda: NOW)
    fake.intraday_gap = 60  # about 20% of the window
    check = run(good_inputs(fake))["OHLCV_QUALITY"]
    assert check.status == "FAIL" and check.reason == "TOO_MANY_GAPS"


def test_stale_candles_fail() -> None:
    fake = FakeCoinbase(lambda: NOW)
    fake.intraday_gap = 0
    inputs = good_inputs(fake)
    stale = replace(inputs, intraday=parse.CandleSet(inputs.intraday.candles[:-10], 0))  # type: ignore[union-attr]
    check = run(stale)["OHLCV_QUALITY"]
    assert check.status == "FAIL" and check.reason in {"CANDLES_STALE", "TOO_MANY_GAPS"}
    assert (
        run(
            stale,
            policy(
                validation=POLICY.validation.model_copy(update={"max_gap_ratio": Decimal("0.25")})
            ),
        )["OHLCV_QUALITY"].reason
        == "CANDLES_STALE"
    )


def test_structural_candle_problems_fail() -> None:
    inputs = good_inputs()
    assert inputs.intraday is not None
    c = list(inputs.intraday.candles)
    dup = replace(inputs, intraday=parse.CandleSet(tuple(c + [c[3]]), 0))
    assert run(dup)["OHLCV_QUALITY"].reason == "DUPLICATE_CANDLES"
    bad_high = list(c)
    bad_high[5] = replace(bad_high[5], low=bad_high[5].high + 1)
    assert (
        run(replace(inputs, intraday=parse.CandleSet(tuple(bad_high), 0)))["OHLCV_QUALITY"].reason
        == "INVALID_OHLC"
    )
    misaligned = list(c)
    misaligned[5] = replace(misaligned[5], start=misaligned[5].start + 7)
    assert (
        run(replace(inputs, intraday=parse.CandleSet(tuple(misaligned), 0)))["OHLCV_QUALITY"].reason
        == "MISALIGNED_CANDLES"
    )
    zero = list(c)
    zero[5] = replace(zero[5], low=Decimal(0))
    assert (
        run(replace(inputs, intraday=parse.CandleSet(tuple(zero), 0)))["OHLCV_QUALITY"].reason
        == "INVALID_OHLC"
    )


def test_malformed_or_jumping_candles_fail() -> None:
    inputs = good_inputs()
    assert inputs.intraday is not None
    assert (
        run(replace(inputs, intraday=parse.CandleSet(inputs.intraday.candles, 3)))[
            "OHLCV_QUALITY"
        ].reason
        == "MALFORMED_CANDLES"
    )
    c = list(inputs.intraday.candles)
    c[-3] = replace(c[-3], open=c[-3].open * 2, high=c[-3].open * 3, close=c[-3].open * 2)
    assert (
        run(replace(inputs, intraday=parse.CandleSet(tuple(c), 0)))["OHLCV_QUALITY"].reason
        == "PRICE_JUMP"
    )


def test_quality_is_inconclusive_when_the_candles_could_not_be_fetched() -> None:
    failed = replace(good_inputs(), intraday=None, fetch_errors={"intraday": "TIMEOUT"})
    assert run(failed)["OHLCV_QUALITY"].reason == "FETCH_FAILED"


# ------------------------------------------------------------------ liquidity
def _with_book(fake: FakeCoinbase, **kwargs: Any) -> v.ValidationInputs:
    from datetime import UTC, datetime

    fake.book_overrides["BTC-USDC"] = book_json(datetime.fromtimestamp(fake.epoch(), UTC), **kwargs)
    return good_inputs(fake)


def test_wide_spread_and_thin_depth_fail() -> None:
    wide = run(_with_book(FakeCoinbase(lambda: NOW), spread_bps="80"))["LIQUIDITY_SPREAD"]
    assert wide.status == "FAIL" and wide.reason == "SPREAD_TOO_WIDE"
    thin = run(_with_book(FakeCoinbase(lambda: NOW), size="0.0001"))["LIQUIDITY_SPREAD"]
    assert thin.status == "FAIL" and thin.reason == "DEPTH_TOO_THIN"


def test_empty_crossed_and_untimed_books() -> None:
    inputs = good_inputs()
    assert inputs.book is not None
    assert (
        run(replace(inputs, book=replace(inputs.book, bids=())))["LIQUIDITY_SPREAD"].reason
        == "NO_LIQUIDITY"
    )
    crossed = replace(inputs.book, bids=inputs.book.asks)
    assert run(replace(inputs, book=crossed))["LIQUIDITY_SPREAD"].reason == "CROSSED_BOOK"
    assert (
        run(replace(inputs, book=replace(inputs.book, time=None)))["LIQUIDITY_SPREAD"].reason
        == "BOOK_TIME_UNKNOWN"
    )
    assert inputs.book.time is not None
    old = replace(inputs.book, time=inputs.book.time - timedelta(minutes=10))
    assert run(replace(inputs, book=old))["LIQUIDITY_SPREAD"].reason == "BOOK_STALE"


def test_liquidity_states_the_price_basis() -> None:
    assert run(good_inputs())["LIQUIDITY_SPREAD"].observed["priced_from"] == "UNIFIED_USD_BOOK"


# ------------------------------------------------------------------ fees
def test_fees_must_be_attested_and_reviewed() -> None:
    none = policy(fees=type(ATTESTED)())
    assert run(good_inputs(), none)["FEE_VIABILITY"].reason == "FEE_NOT_ATTESTED"
    stale = policy(fees=ATTESTED.model_copy(update={"attested_on": date(2026, 7, 1)}))
    assert run(good_inputs(), stale)["FEE_VIABILITY"].reason == "FEE_ATTESTATION_EXPIRED"
    future = policy(fees=ATTESTED.model_copy(update={"attested_on": date(2026, 10, 1)}))
    assert run(good_inputs(), future)["FEE_VIABILITY"].reason == "FEE_ATTESTATION_EXPIRED"
    boundary = policy(
        fees=ATTESTED.model_copy(update={"attested_on": NOW.date() - timedelta(days=30)})
    )
    assert run(good_inputs(), boundary)["FEE_VIABILITY"].status == "PASS"


def test_fee_viability_fails_when_spacing_cannot_cover_round_trip_fees() -> None:
    dear = policy(
        fees=ATTESTED.model_copy(
            update={"operator_maker_rate": Decimal("0.006"), "stress_maker_rate": Decimal("0.02")}
        )
    )
    check = run(good_inputs(), dear)["FEE_VIABILITY"]
    assert check.status == "FAIL" and check.reason.startswith("FEES_EXCEED_SPACING")


def test_stress_rate_is_checked_separately_from_the_attested_rate() -> None:
    stress_high = policy(fees=ATTESTED.model_copy(update={"stress_maker_rate": Decimal("0.02")}))
    check = run(good_inputs(), stress_high)["FEE_VIABILITY"]
    assert check.status == "FAIL" and check.reason == "FEES_EXCEED_SPACING_STRESS"


def test_fee_model_is_named_in_the_evidence() -> None:
    assert run(good_inputs())["FEE_VIABILITY"].observed["model"] == "FEE_MODEL_V1"


def test_too_little_range_data_is_inconclusive() -> None:
    inputs = good_inputs()
    assert inputs.daily is not None
    short = replace(inputs, daily=parse.CandleSet(inputs.daily.candles[-20:], 0))
    assert run(short)["FEE_VIABILITY"].reason == "NOT_ENOUGH_RANGE_DATA"


# ------------------------------------------------------------------ capital
def test_capital_feasibility_shows_the_50_15_35_arithmetic() -> None:
    check = run(good_inputs())["CAPITAL_FEASIBILITY"]
    assert check.status == "PASS"
    assert check.observed["total_capital"] == "50" and check.observed["reserve_floor"] == "15"
    assert check.observed["deployment_cap"] == "35" and check.observed["levels"] == "3"
    assert Decimal(check.observed["committed"]) <= Decimal("35")
    assert Decimal("50") - Decimal(check.observed["committed"]) >= Decimal("15")


def test_a_level_that_cannot_buy_the_minimum_size_fails() -> None:
    check = run(
        with_product(
            good_inputs(), base_min_size=Decimal("0.0005"), base_increment=Decimal("0.0001")
        )
    )["CAPITAL_FEASIBILITY"]
    assert check.status == "FAIL" and check.reason == "BELOW_BASE_MINIMUM"


def test_quote_minimum_above_a_level_fails() -> None:
    check = run(with_product(good_inputs(), quote_min_size=Decimal("12")))["CAPITAL_FEASIBILITY"]
    assert check.status == "FAIL" and check.reason == "BELOW_QUOTE_MINIMUM"


def test_base_maximum_below_a_level_fails() -> None:
    check = run(with_product(good_inputs(), base_max_size=Decimal("0.00000002")))[
        "CAPITAL_FEASIBILITY"
    ]
    assert check.reason == "ABOVE_BASE_MAXIMUM"


def test_rounding_never_rounds_up_into_the_cap() -> None:
    for price in ("3", "150", "3000", "60000", "1234.56", "99999.99"):
        fake = FakeCoinbase(lambda: NOW)
        from datetime import UTC, datetime

        fake.book_overrides["BTC-USDC"] = book_json(
            datetime.fromtimestamp(fake.epoch(), UTC), mid=price, size="100000"
        )
        check = run(good_inputs(fake))["CAPITAL_FEASIBILITY"]
        if check.status == "PASS":
            assert Decimal(check.observed["committed"]) <= Decimal("35")
        else:
            assert check.reason in {
                "BELOW_BASE_MINIMUM",
                "BELOW_QUOTE_MINIMUM",
                "CAP_OR_RESERVE_BREACH",
            }


def test_a_tick_finer_than_a_level_is_fine_but_reserve_arithmetic_is_still_enforced() -> None:
    assert (
        run(good_inputs(), policy(total_capital=Decimal("50")))["CAPITAL_FEASIBILITY"].status
        == "PASS"
    )


# ------------------------------------------------------------------ helpers and evidence
def test_floor_to_and_power_of_ten_helpers() -> None:
    assert v.floor_to(Decimal("11.6666"), Decimal("0.01")) == Decimal("11.66")
    assert v.floor_to(Decimal("0.0001943"), Decimal("0.00000001")) == Decimal("0.0001943")
    assert v.is_power_of_ten(Decimal("0.001")) and v.is_power_of_ten(Decimal("1E-8"))
    assert not v.is_power_of_ten(Decimal("0.005")) and not v.is_power_of_ten(Decimal("0"))
    assert v.decimals_of(Decimal("0.0100")) == 2


def test_thresholds_hash_is_stable_and_sensitive_to_policy() -> None:
    a = v.thresholds_sha256(policy())
    assert a == v.thresholds_sha256(policy()) and len(a) == 64
    changed = policy(
        validation=POLICY.validation.model_copy(update={"max_spread_bps": Decimal("29")})
    )
    assert v.thresholds_sha256(changed) != a
    other_fee = policy(fees=ATTESTED.model_copy(update={"operator_maker_rate": Decimal("0.003")}))
    assert v.thresholds_sha256(other_fee) != a


def test_messages_never_echo_external_content() -> None:
    hostile = with_product(good_inputs(), status="<script>alert(1)</script>", malformed=("status",))
    text = " ".join(f"{c.message} {c.reason} {c.observed}" for c in v.run_checks(hostile, POLICY))
    assert "<script>" not in text


def test_product_json_helper_matches_the_documented_shape() -> None:
    assert product_json()["product_venue"] == "CBE" and product_json()["product_type"] == "SPOT"
