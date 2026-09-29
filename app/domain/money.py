"""Decimal-only arithmetic for every financial value.

Rules (enforced by tests that scan the financial modules for `float`):
- Money, prices, quantities, fees and ratios are `Decimal`. A float never enters a calculation.
- Values come from text or integers only (`dec`). Anything else is refused.
- Rounding is explicit and conservative: buy prices and quantities round DOWN, sell prices and
  fees round UP, proceeds round DOWN. Nothing relies on the default rounding of a context.
- The arithmetic context is fixed (34 digits, half-even) so results are identical on every machine.
"""

from __future__ import annotations

from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    InvalidOperation,
    localcontext,
)
from typing import Final

ZERO: Final = Decimal(0)
ONE: Final = Decimal(1)
BPS: Final = Decimal(10000)
PRECISION: Final = 34
_CONTEXT: Final = Context(prec=PRECISION, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)
_MAX_DIGITS: Final = 40
_MAX_EXPONENT: Final = 30


class MoneyError(ValueError):
    """A value could not be used as money. The message never contains the offending input."""


def dec(value: str | int | Decimal) -> Decimal:
    """Parse a Decimal from text, an integer or a Decimal. Floats, bools and None are refused."""
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise MoneyError("value must be a string, integer or Decimal")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise MoneyError("not a decimal number") from exc
    if not result.is_finite():
        raise MoneyError("value must be finite")
    if len(result.as_tuple().digits) > _MAX_DIGITS:
        raise MoneyError("too many digits")
    if result != 0 and not -_MAX_EXPONENT <= result.adjusted() <= _MAX_EXPONENT:
        raise MoneyError("magnitude out of range")
    return result


def context() -> Context:
    return _CONTEXT.copy()


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    """Largest multiple of `step` that is <= value (buy prices and quantities)."""
    _positive(step)
    with localcontext(_CONTEXT):
        return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def quantize_up(value: Decimal, step: Decimal) -> Decimal:
    """Smallest multiple of `step` that is >= value (sell prices, fees)."""
    _positive(step)
    with localcontext(_CONTEXT):
        return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _positive(step: Decimal) -> None:
    if not step.is_finite() or step <= 0:
        raise MoneyError("step must be positive")


def on_step(value: Decimal, step: Decimal) -> bool:
    _positive(step)
    with localcontext(_CONTEXT):
        return value % step == 0


def fee_for(notional: Decimal, rate: Decimal, quote_step: Decimal) -> Decimal:
    """Fee on a notional, rounded UP to the quote increment (never under-charge)."""
    if notional < 0 or rate < 0:
        raise MoneyError("notional and rate must not be negative")
    with localcontext(_CONTEXT):
        return quantize_up(notional * rate, quote_step)


def ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        raise MoneyError("division by zero")
    with localcontext(_CONTEXT):
        return numerator / denominator


def geometric_ratio(lower: Decimal, upper: Decimal, cells: int) -> Decimal:
    """r such that lower * r**cells == upper, computed with exp and ln in the fixed context."""
    if lower <= 0 or upper <= lower or cells < 1:
        raise MoneyError("invalid geometric range")
    with localcontext(_CONTEXT):
        return (((upper / lower).ln()) / cells).exp()


def canonical(value: Decimal) -> str:
    """Stable text for hashing and reports: no exponent, no trailing zeros."""
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def pct(value: Decimal, places: str = "0.0001") -> str:
    """A ratio as a percentage string with fixed places (display only)."""
    with localcontext(_CONTEXT):
        return format((value * 100).quantize(Decimal(places), rounding=ROUND_HALF_EVEN), "f") + "%"
