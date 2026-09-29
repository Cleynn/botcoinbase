"""Pair validation: fourteen deterministic checks over already-fetched public data.

Pure functions. No network, no database, no clock other than the `now` passed in. Every check
returns PASS, FAIL (definitely unusable) or INCONCLUSIVE (cannot tell), a fixed reason code, a fixed
message from this file and scalar observed values. External content is never copied into messages.

Outcome: any FAIL gives FAIL; otherwise any INCONCLUSIVE gives INCONCLUSIVE; otherwise PASS. Only a
PASS can make a pair PAPER_ELIGIBLE. FAIL and INCONCLUSIVE both lead to RESEARCH_ONLY.

FEE_MODEL_V1 is an engineering assumption, not an exchange fact (see docs/pair-management.md): the
strategy does not exist yet, so viability is judged on arithmetic only.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Final

from app import constants
from app.adapters.coinbase_parse import (
    Candle,
    CandleSet,
    OrderBook,
    ServerTime,
    decimal_text,
)
from app.config import PairPolicy
from app.domain.pairs import CheckResult, ProductMetadata
from app.pairs import policy as pair_policy

PASS: Final = "PASS"
FAIL: Final = "FAIL"
INCONCLUSIVE: Final = "INCONCLUSIVE"
FEE_MODEL_VERSION: Final = "FEE_MODEL_V1"
CANDLE_LAG_SECONDS: Final = 3  # 2 s exchange lag + 1 s public cache margin (CB-5)
DAY: Final = 86400
FIVE_MINUTES: Final = 300

CHECK_CODES: Final = (
    "PRODUCT_STATUS",
    "QUOTE_CURRENCY",
    "BASE_NOT_STABLE",
    "DATA_BASIS",
    "METADATA_FRESHNESS",
    "CLOCK_SYNC",
    "INCREMENTS",
    "PRECISION",
    "MINIMUMS",
    "HISTORY_AVAILABILITY",
    "OHLCV_QUALITY",
    "LIQUIDITY_SPREAD",
    "FEE_VIABILITY",
    "CAPITAL_FEASIBILITY",
)


@dataclass(frozen=True)
class ValidationInputs:
    product: ProductMetadata
    verified_at: datetime  # when the metadata was last confirmed against the exchange
    pair_data_basis: str  # basis recorded on the pair row when it was proposed
    now: datetime
    server_time: ServerTime | None
    daily: CandleSet | None
    intraday: CandleSet | None
    book: OrderBook | None
    fetch_errors: dict[str, str] = field(default_factory=dict)  # dataset -> fixed error code


# ---------------------------------------------------------------------------- helpers
def _r(code: str, status: str, reason: str, message: str, **observed: object) -> CheckResult:
    return CheckResult(
        code, status, reason, message, {k: _text(v) for k, v in observed.items() if v is not None}
    )


def _text(value: object) -> str:
    if isinstance(value, Decimal):
        return decimal_text(value) or "0"
    return str(value)[:120]


def _ok(code: str, message: str, **observed: object) -> CheckResult:
    return _r(code, PASS, "OK", message, **observed)


def floor_to(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


def is_power_of_ten(value: Decimal) -> bool:
    if value <= 0:
        return False
    digits, exponent = value.normalize().as_tuple().digits, value.normalize().as_tuple().exponent
    return digits == (1,) and isinstance(exponent, int)


def decimals_of(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return max(-exponent, 0) if isinstance(exponent, int) else 0


def _closed(candles: tuple[Candle, ...], seconds: int, server_epoch: int) -> list[Candle]:
    """Closed candles only: start + granularity <= server time - lag (CB-5)."""
    limit = server_epoch - CANDLE_LAG_SECONDS
    return [c for c in candles if c.start + seconds <= limit]


def _mid(book: OrderBook | None) -> Decimal | None:
    if book is None or not book.bids or not book.asks:
        return None
    bid, ask = book.bids[0].price, book.asks[0].price
    return (bid + ask) / 2 if bid < ask else None


def _percentile_low(values: list[Decimal], fraction: Decimal) -> Decimal:
    ordered = sorted(values)
    index = max(math.ceil(fraction * len(ordered)) - 1, 0)
    return ordered[index]


def _fetch_failed(
    code: str, dataset: str, inputs: ValidationInputs, what: str
) -> CheckResult | None:
    error = inputs.fetch_errors.get(dataset)
    if error is None:
        return None
    return _r(code, INCONCLUSIVE, "FETCH_FAILED", f"{what} could not be fetched.", error=error)


# ---------------------------------------------------------------------------- the checks
def check_product_status(inputs: ValidationInputs) -> CheckResult:
    verdict = pair_policy.assess_product(inputs.product)
    if verdict.failures:
        return _r(
            "PRODUCT_STATUS",
            FAIL,
            verdict.failures[0],
            "The product is not tradable as required (type, venue, status and flags).",
            failures=",".join(verdict.failures),
            product_status=inputs.product.status,
        )
    if verdict.inconclusive:
        return _r(
            "PRODUCT_STATUS",
            INCONCLUSIVE,
            verdict.inconclusive[0],
            "Product status or flags are missing or unreadable.",
            unknown=",".join(verdict.inconclusive),
        )
    return _ok(
        "PRODUCT_STATUS",
        "Spot product on the expected venue, online, no blocking flags.",
        product_status=inputs.product.status,
    )


def check_quote_currency(inputs: ValidationInputs) -> CheckResult:
    p = inputs.product
    if p.quote_currency != pair_policy.QUOTE or not p.product_id.endswith("-USDC"):
        return _r(
            "QUOTE_CURRENCY",
            FAIL,
            "QUOTE_NOT_USDC",
            "The quote currency is not USDC.",
            quote=p.quote_currency,
        )
    return _ok("QUOTE_CURRENCY", "Quoted in USDC.", quote=p.quote_currency)


def check_base_not_stable(inputs: ValidationInputs) -> CheckResult:
    if pair_policy.is_stable_base(inputs.product):
        return _r(
            "BASE_NOT_STABLE",
            FAIL,
            "STABLE_BASE",
            "The base is a stable or pegged asset, so a grid has no range to trade.",
            base=inputs.product.base_currency,
        )
    return _ok("BASE_NOT_STABLE", "The base is not a stable or pegged asset.")


def check_data_basis(inputs: ValidationInputs) -> CheckResult:
    data_product, basis = pair_policy.derive_data_basis(inputs.product)
    if basis == "UNKNOWN":
        return _r(
            "DATA_BASIS",
            INCONCLUSIVE,
            "DATA_BASIS_UNKNOWN",
            "Which order book prices this product cannot be determined from its aliases.",
        )
    if basis != inputs.pair_data_basis:
        return _r(
            "DATA_BASIS",
            INCONCLUSIVE,
            "DATA_BASIS_CHANGED",
            "The data basis differs from the one recorded when the pair was proposed.",
            now=basis,
            recorded=inputs.pair_data_basis,
        )
    return _ok(
        "DATA_BASIS", "Data basis is known and unchanged.", basis=basis, data_product=data_product
    )


def check_metadata_freshness(inputs: ValidationInputs, policy: PairPolicy) -> CheckResult:
    failed = _fetch_failed("METADATA_FRESHNESS", "product", inputs, "Fresh product metadata")
    if failed:
        return failed
    age = (inputs.now - inputs.verified_at).total_seconds()
    limit = policy.validation.max_metadata_age_seconds
    if age < -5:
        return _r(
            "METADATA_FRESHNESS",
            INCONCLUSIVE,
            "METADATA_FROM_FUTURE",
            "Metadata is timestamped in the future; the local clock is suspect.",
            age_seconds=int(age),
        )
    if age > limit:
        return _r(
            "METADATA_FRESHNESS",
            INCONCLUSIVE,
            "METADATA_STALE",
            "Product metadata is older than the allowed age.",
            age_seconds=int(age),
            limit_seconds=limit,
        )
    return _ok(
        "METADATA_FRESHNESS",
        "Product metadata is fresh.",
        age_seconds=max(int(age), 0),
        limit_seconds=limit,
    )


def check_clock_sync(inputs: ValidationInputs, policy: PairPolicy) -> CheckResult:
    failed = _fetch_failed("CLOCK_SYNC", "time", inputs, "The exchange time")
    if failed:
        return failed
    if inputs.server_time is None:
        return _r(
            "CLOCK_SYNC",
            INCONCLUSIVE,
            "SERVER_TIME_UNAVAILABLE",
            "The exchange time is unavailable.",
        )
    offset = (inputs.now - inputs.server_time.as_datetime()).total_seconds()
    limit = policy.validation.max_clock_offset_seconds
    if abs(offset) > limit:
        return _r(
            "CLOCK_SYNC",
            INCONCLUSIVE,
            "CLOCK_SKEW",
            "The local clock differs from the exchange clock by more than allowed.",
            offset_seconds=round(offset, 1),
            limit_seconds=limit,
        )
    return _ok(
        "CLOCK_SYNC",
        "Local clock agrees with the exchange clock.",
        offset_seconds=round(offset, 1),
        limit_seconds=limit,
    )


def check_increments(inputs: ValidationInputs) -> CheckResult:
    p = inputs.product
    missing = [
        n
        for n in ("base_increment", "quote_increment", "base_min_size", "price_increment")
        if getattr(p, n) is None
    ]
    if missing:
        reason = (
            "PRICE_INCREMENT_MISSING" if missing == ["price_increment"] else "INCREMENT_MISSING"
        )
        return _r(
            "INCREMENTS",
            INCONCLUSIVE,
            reason,
            "A required increment or minimum is missing or unreadable.",
            missing=",".join(missing),
        )
    if (
        p.base_min_size is not None
        and p.base_max_size is not None
        and p.base_min_size > p.base_max_size
    ):
        return _r(
            "INCREMENTS",
            INCONCLUSIVE,
            "MIN_ABOVE_MAX",
            "The base minimum exceeds the base maximum.",
        )
    if (
        p.quote_min_size is not None
        and p.quote_max_size is not None
        and p.quote_min_size > p.quote_max_size
    ):
        return _r(
            "INCREMENTS",
            INCONCLUSIVE,
            "MIN_ABOVE_MAX",
            "The quote minimum exceeds the quote maximum.",
        )
    return _ok(
        "INCREMENTS",
        "Base, quote and price increments and the base minimum are present and positive.",
        base_increment=p.base_increment,
        quote_increment=p.quote_increment,
        price_increment=p.price_increment,
        base_min_size=p.base_min_size,
    )


def check_precision(inputs: ValidationInputs, policy: PairPolicy) -> CheckResult:
    p = inputs.product
    if not (p.base_increment and p.quote_increment and p.price_increment and p.base_min_size):
        return _r("PRECISION", INCONCLUSIVE, "INCREMENT_MISSING", "Increments are unavailable.")
    for name in ("base_increment", "quote_increment", "price_increment"):
        value: Decimal = getattr(p, name)
        if not is_power_of_ten(value) or value > 1_000_000 or decimals_of(value) > 12:
            return _r(
                "PRECISION",
                INCONCLUSIVE,
                "INCREMENT_UNUSUAL",
                "An increment is not a power of ten up to 12 decimals; it is not trusted.",
                field=name,
                value=value,
            )
    if p.base_min_size % p.base_increment != 0:
        return _r(
            "PRECISION",
            INCONCLUSIVE,
            "MIN_OFF_INCREMENT",
            "The base minimum is not a multiple of the base increment.",
            base_min_size=p.base_min_size,
            base_increment=p.base_increment,
        )
    price = _mid(inputs.book)
    if price is None:
        return _r(
            "PRECISION",
            INCONCLUSIVE,
            "NO_REFERENCE_PRICE",
            "There is no valid order book to give a reference price.",
        )
    tick = p.price_increment / price
    if tick > policy.validation.max_tick_ratio:
        return _r(
            "PRECISION",
            FAIL,
            "TICK_TOO_COARSE",
            "The price increment is too coarse relative to the price.",
            tick_ratio=tick.quantize(Decimal("0.0000001")),
            limit=policy.validation.max_tick_ratio,
        )
    return _ok(
        "PRECISION",
        "Increments are powers of ten and the tick is fine enough.",
        tick_ratio=tick.quantize(Decimal("0.0000001")),
        limit=policy.validation.max_tick_ratio,
    )


def per_level_budget(policy: PairPolicy, quote_increment: Decimal) -> Decimal:
    return floor_to(policy.max_deployment / policy.grid_levels, quote_increment)


def check_minimums(inputs: ValidationInputs, policy: PairPolicy) -> CheckResult:
    p = inputs.product
    if not (p.base_min_size and p.quote_increment):
        return _r("MINIMUMS", INCONCLUSIVE, "INCREMENT_MISSING", "Minimum sizes are unavailable.")
    price = _mid(inputs.book)
    if price is None:
        return _r(
            "MINIMUMS",
            INCONCLUSIVE,
            "NO_REFERENCE_PRICE",
            "There is no valid order book to give a reference price.",
        )
    budget = per_level_budget(policy, p.quote_increment)
    min_notional = p.base_min_size * price
    quote_min = p.quote_min_size or Decimal(0)
    if min_notional > budget or quote_min > budget:
        return _r(
            "MINIMUMS",
            FAIL,
            "MINIMUM_ABOVE_BUDGET",
            "The smallest allowed order costs more than one grid level may spend.",
            min_notional=min_notional.quantize(Decimal("0.01")),
            quote_min_size=p.quote_min_size,
            budget=budget,
        )
    return _ok(
        "MINIMUMS",
        "The smallest allowed order fits within one grid level.",
        min_notional=min_notional.quantize(Decimal("0.01")),
        quote_min_size=p.quote_min_size,
        budget=budget,
    )


def check_history(inputs: ValidationInputs, policy: PairPolicy) -> CheckResult:
    failed = _fetch_failed("HISTORY_AVAILABILITY", "daily", inputs, "Daily history")
    if failed:
        return failed
    if inputs.daily is None or inputs.server_time is None:
        return _r(
            "HISTORY_AVAILABILITY",
            INCONCLUSIVE,
            "NO_HISTORY_DATA",
            "Daily history was not retrieved.",
        )
    need = policy.validation.min_history_days - policy.validation.max_missing_history_days
    closed = _closed(inputs.daily.candles, DAY, inputs.server_time.epoch_seconds)
    days = {c.start for c in closed}
    newest = inputs.server_time.epoch_seconds - policy.validation.min_history_days * DAY - DAY
    in_window = {d for d in days if d >= newest}
    if len(in_window) < need:
        return _r(
            "HISTORY_AVAILABILITY",
            FAIL,
            "HISTORY_TOO_SHORT",
            "Fewer daily candles than required are available.",
            daily_candles=len(in_window),
            required=need,
            window_days=policy.validation.min_history_days,
        )
    return _ok(
        "HISTORY_AVAILABILITY",
        "Enough daily history is available.",
        daily_candles=len(in_window),
        required=need,
        window_days=policy.validation.min_history_days,
    )


def _structure_problem(candles: tuple[Candle, ...], seconds: int) -> str | None:
    starts = [c.start for c in candles]
    if len(starts) != len(set(starts)):
        return "DUPLICATE_CANDLES"
    for c in candles:
        if c.start % seconds != 0:
            return "MISALIGNED_CANDLES"
        if min(c.low, c.high, c.open, c.close) <= 0 or c.low > c.high:
            return "INVALID_OHLC"
        if c.open < c.low or c.open > c.high or c.close < c.low or c.close > c.high:
            return "INVALID_OHLC"
    return None


def check_ohlcv_quality(inputs: ValidationInputs, policy: PairPolicy) -> CheckResult:
    failed = _fetch_failed("OHLCV_QUALITY", "intraday", inputs, "Five-minute candles")
    if failed:
        return failed
    if inputs.intraday is None or inputs.server_time is None:
        return _r(
            "OHLCV_QUALITY",
            INCONCLUSIVE,
            "NO_CANDLE_DATA",
            "Five-minute candles were not retrieved.",
        )
    server = inputs.server_time.epoch_seconds
    if inputs.intraday.malformed or (inputs.daily is not None and inputs.daily.malformed):
        return _r(
            "OHLCV_QUALITY",
            FAIL,
            "MALFORMED_CANDLES",
            "Some candles could not be parsed.",
            malformed=inputs.intraday.malformed + (inputs.daily.malformed if inputs.daily else 0),
        )
    closed = _closed(inputs.intraday.candles, FIVE_MINUTES, server)
    window = policy.validation.quality_window_candles
    if not closed:
        return _r(
            "OHLCV_QUALITY", FAIL, "NO_CLOSED_CANDLES", "There are no closed five-minute candles."
        )
    problem = _structure_problem(tuple(closed), FIVE_MINUTES)
    if problem is None and inputs.daily is not None:
        problem = _structure_problem(inputs.daily.candles, DAY)
    if problem:
        return _r(
            "OHLCV_QUALITY",
            FAIL,
            problem,
            "Candle structure is invalid (duplicates, alignment or OHLC).",
        )
    newest = closed[-1].start
    age = server - (newest + FIVE_MINUTES)
    if age > policy.validation.max_stale_seconds:
        return _r(
            "OHLCV_QUALITY",
            FAIL,
            "CANDLES_STALE",
            "The newest closed candle is too old.",
            age_seconds=age,
            limit_seconds=policy.validation.max_stale_seconds,
        )
    floor_start = newest - (window - 1) * FIVE_MINUTES
    recent = [c for c in closed if c.start >= floor_start]
    gap_ratio = Decimal(window - len(recent)) / Decimal(window)
    if gap_ratio > policy.validation.max_gap_ratio:
        return _r(
            "OHLCV_QUALITY",
            FAIL,
            "TOO_MANY_GAPS",
            "Too many five-minute intervals have no candle.",
            gap_ratio=gap_ratio.quantize(Decimal("0.001")),
            limit=policy.validation.max_gap_ratio,
            window=window,
            present=len(recent),
        )
    jump = Decimal(0)
    for previous, current in zip(recent, recent[1:], strict=False):
        if current.start - previous.start == FIVE_MINUTES:
            jump = max(
                jump,
                abs(current.open - previous.close) / previous.close,
                abs(current.close - previous.close) / previous.close,
            )
    if jump > policy.validation.max_candle_jump_ratio:
        return _r(
            "OHLCV_QUALITY",
            FAIL,
            "PRICE_JUMP",
            "Adjacent candles differ by more than allowed.",
            largest_jump=jump.quantize(Decimal("0.0001")),
            limit=policy.validation.max_candle_jump_ratio,
        )
    return _ok(
        "OHLCV_QUALITY",
        "Recent five-minute candles are complete, ordered and sane.",
        window=window,
        present=len(recent),
        gap_ratio=gap_ratio.quantize(Decimal("0.001")),
        newest_age_seconds=age,
        largest_jump=jump.quantize(Decimal("0.0001")),
    )


def check_liquidity(inputs: ValidationInputs, policy: PairPolicy) -> CheckResult:
    failed = _fetch_failed("LIQUIDITY_SPREAD", "book", inputs, "The order book")
    if failed:
        return failed
    book = inputs.book
    if book is None or inputs.server_time is None:
        return _r(
            "LIQUIDITY_SPREAD", INCONCLUSIVE, "NO_BOOK_DATA", "The order book was not retrieved."
        )
    if not book.bids or not book.asks:
        return _r("LIQUIDITY_SPREAD", FAIL, "NO_LIQUIDITY", "One side of the order book is empty.")
    bid, ask = book.bids[0].price, book.asks[0].price
    if bid >= ask:
        return _r(
            "LIQUIDITY_SPREAD", FAIL, "CROSSED_BOOK", "The best bid is not below the best ask."
        )
    if book.time is None:
        return _r(
            "LIQUIDITY_SPREAD",
            INCONCLUSIVE,
            "BOOK_TIME_UNKNOWN",
            "The order book has no usable timestamp.",
        )
    age = (inputs.server_time.as_datetime() - book.time.astimezone(UTC)).total_seconds()
    if (
        age > policy.validation.book_max_age_seconds
        or age < -policy.validation.book_max_age_seconds
    ):
        return _r(
            "LIQUIDITY_SPREAD",
            INCONCLUSIVE,
            "BOOK_STALE",
            "The order book timestamp is too far from exchange time.",
            age_seconds=int(age),
            limit_seconds=policy.validation.book_max_age_seconds,
        )
    mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 10000
    band = policy.validation.depth_band_ratio
    bid_depth = sum(
        (lv.price * lv.size for lv in book.bids if lv.price >= mid * (1 - band)), Decimal(0)
    )
    ask_depth = sum(
        (lv.price * lv.size for lv in book.asks if lv.price <= mid * (1 + band)), Decimal(0)
    )
    shown = {
        "spread_bps": spread_bps.quantize(Decimal("0.01")),
        "bid_depth": bid_depth.quantize(Decimal("0.01")),
        "ask_depth": ask_depth.quantize(Decimal("0.01")),
        "min_depth": policy.validation.min_depth_quote,
        "max_spread_bps": policy.validation.max_spread_bps,
        "priced_from": inputs.pair_data_basis,
    }
    if spread_bps > policy.validation.max_spread_bps:
        return _r(
            "LIQUIDITY_SPREAD",
            FAIL,
            "SPREAD_TOO_WIDE",
            "The bid-ask spread is wider than allowed.",
            **shown,
        )
    if min(bid_depth, ask_depth) < policy.validation.min_depth_quote:
        return _r(
            "LIQUIDITY_SPREAD",
            FAIL,
            "DEPTH_TOO_THIN",
            "Order book depth near the mid price is below the minimum.",
            **shown,
        )
    return _ok("LIQUIDITY_SPREAD", "Spread and depth are within policy.", **shown)


def check_fee_viability(inputs: ValidationInputs, policy: PairPolicy) -> CheckResult:
    fees = policy.fees
    today = inputs.now.astimezone(UTC).date()
    if fees.operator_maker_rate is None or fees.attested_on is None:
        return _r(
            "FEE_VIABILITY",
            INCONCLUSIVE,
            "FEE_NOT_ATTESTED",
            "No operator-attested maker fee exists; no primary fee schedule has been retrieved.",
            model=FEE_MODEL_VERSION,
        )
    if fees.attested_on > today or today - fees.attested_on > timedelta(
        days=fees.review_interval_days
    ):
        return _r(
            "FEE_VIABILITY",
            INCONCLUSIVE,
            "FEE_ATTESTATION_EXPIRED",
            "The fee attestation is in the future or older than the review interval.",
            attested_on=fees.attested_on.isoformat(),
            review_days=fees.review_interval_days,
            model=FEE_MODEL_VERSION,
        )
    failed = _fetch_failed("FEE_VIABILITY", "daily", inputs, "Daily history")
    if failed:
        return failed
    if inputs.daily is None or inputs.server_time is None:
        return _r(
            "FEE_VIABILITY", INCONCLUSIVE, "NO_HISTORY_DATA", "Daily history was not retrieved."
        )
    closed = _closed(inputs.daily.candles, DAY, inputs.server_time.epoch_seconds)
    ranges = [(c.high - c.low) / c.close for c in closed if c.close > 0]
    if len(ranges) < 30:
        return _r(
            "FEE_VIABILITY",
            INCONCLUSIVE,
            "NOT_ENOUGH_RANGE_DATA",
            "Fewer than 30 daily ranges are available to judge fee viability.",
            days=len(ranges),
        )
    typical = _percentile_low(ranges, Decimal("0.25"))
    spacing = typical / policy.grid_levels
    need_operator = 2 * fees.operator_maker_rate + fees.min_net_edge
    need_stress = 2 * fees.stress_maker_rate + fees.min_net_edge
    shown = {
        "model": FEE_MODEL_VERSION,
        "p25_daily_range": typical.quantize(Decimal("0.0001")),
        "grid_levels": policy.grid_levels,
        "spacing": spacing.quantize(Decimal("0.0001")),
        "need_operator": need_operator.quantize(Decimal("0.0001")),
        "need_stress": need_stress.quantize(Decimal("0.0001")),
    }
    if spacing < need_operator:
        return _r(
            "FEE_VIABILITY",
            FAIL,
            "FEES_EXCEED_SPACING_OPERATOR",
            "Round-trip fees at the attested rate exceed the achievable grid spacing.",
            **shown,
        )
    if spacing < need_stress:
        return _r(
            "FEE_VIABILITY",
            FAIL,
            "FEES_EXCEED_SPACING_STRESS",
            "Round-trip fees at the stress rate exceed the achievable grid spacing.",
            **shown,
        )
    return _ok(
        "FEE_VIABILITY",
        "Grid spacing covers round-trip maker fees at the attested and stress rates.",
        **shown,
    )


def check_capital(inputs: ValidationInputs, policy: PairPolicy) -> CheckResult:
    p = inputs.product
    if not policy.total_capital - policy.max_deployment >= policy.min_reserve:
        return _r(
            "CAPITAL_FEASIBILITY",
            FAIL,
            "RESERVE_NOT_PROTECTED",
            "Total capital minus the deployment cap is below the protected reserve.",
        )
    if not (p.base_increment and p.quote_increment and p.base_min_size):
        return _r(
            "CAPITAL_FEASIBILITY", INCONCLUSIVE, "INCREMENT_MISSING", "Increments are unavailable."
        )
    price = _mid(inputs.book)
    if price is None:
        return _r(
            "CAPITAL_FEASIBILITY",
            INCONCLUSIVE,
            "NO_REFERENCE_PRICE",
            "There is no valid order book to give a reference price.",
        )
    budget = per_level_budget(policy, p.quote_increment)
    quantity = floor_to(budget / price, p.base_increment)
    notional = quantity * price
    shown = {
        "total_capital": policy.total_capital,
        "reserve_floor": policy.min_reserve,
        "deployment_cap": policy.max_deployment,
        "levels": policy.grid_levels,
        "per_level_budget": budget,
        "base_quantity": quantity,
        "level_notional": notional.quantize(Decimal("0.01")),
    }
    if quantity < p.base_min_size:
        return _r(
            "CAPITAL_FEASIBILITY",
            FAIL,
            "BELOW_BASE_MINIMUM",
            "One grid level cannot buy the minimum base size.",
            base_min_size=p.base_min_size,
            **shown,
        )
    if p.quote_min_size is not None and notional < p.quote_min_size:
        return _r(
            "CAPITAL_FEASIBILITY",
            FAIL,
            "BELOW_QUOTE_MINIMUM",
            "One grid level is below the minimum quote size.",
            quote_min_size=p.quote_min_size,
            **shown,
        )
    if p.base_max_size is not None and quantity > p.base_max_size:
        return _r(
            "CAPITAL_FEASIBILITY",
            FAIL,
            "ABOVE_BASE_MAXIMUM",
            "One grid level exceeds the maximum base size.",
            **shown,
        )
    committed = notional * policy.grid_levels
    if committed > policy.max_deployment or policy.total_capital - committed < policy.min_reserve:
        return _r(
            "CAPITAL_FEASIBILITY",
            FAIL,
            "CAP_OR_RESERVE_BREACH",
            "The grid would exceed the deployment cap or eat into the protected reserve.",
            committed=committed.quantize(Decimal("0.01")),
            **shown,
        )
    return _ok(
        "CAPITAL_FEASIBILITY",
        "The grid fits the 50 USDC capital, 15 USDC reserve and 35 USDC deployment cap.",
        committed=committed.quantize(Decimal("0.01")),
        **shown,
    )


# ---------------------------------------------------------------------------- orchestration
def run_checks(inputs: ValidationInputs, policy: PairPolicy) -> tuple[CheckResult, ...]:
    return (
        check_product_status(inputs),
        check_quote_currency(inputs),
        check_base_not_stable(inputs),
        check_data_basis(inputs),
        check_metadata_freshness(inputs, policy),
        check_clock_sync(inputs, policy),
        check_increments(inputs),
        check_precision(inputs, policy),
        check_minimums(inputs, policy),
        check_history(inputs, policy),
        check_ohlcv_quality(inputs, policy),
        check_liquidity(inputs, policy),
        check_fee_viability(inputs, policy),
        check_capital(inputs, policy),
    )


def overall_outcome(checks: tuple[CheckResult, ...]) -> str:
    statuses = {c.status for c in checks}
    if FAIL in statuses:
        return FAIL
    if INCONCLUSIVE in statuses:
        return INCONCLUSIVE
    return PASS


def thresholds_sha256(policy: PairPolicy) -> str:
    """Hash of every threshold that influenced a run (recorded with it as evidence)."""
    body = {
        "model": FEE_MODEL_VERSION,
        "capital": [str(policy.total_capital), str(policy.min_reserve), str(policy.max_deployment)],
        "levels": policy.grid_levels,
        "hard_ceilings": [
            str(constants.POLICY_TOTAL_CAPITAL),
            str(constants.POLICY_MIN_RESERVE),
            str(constants.POLICY_MAX_DEPLOYMENT),
        ],
        "validation": {k: str(v) for k, v in sorted(policy.validation.model_dump().items())},
        "fees": {k: str(v) for k, v in sorted(policy.fees.model_dump().items())},
        "statuses": sorted(pair_policy.ALLOWED_STATUSES),
        "stable_bases": sorted(pair_policy.STABLE_BASES),
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()
