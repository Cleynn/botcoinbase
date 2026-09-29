"""Decimal-only money arithmetic, and a scan proving no financial module uses float."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.domain import money as m
from tests.conftest import ROOT

FINANCIAL = [
    "app/domain/money.py",
    "app/market/candles.py",
    "app/strategy",
    "app/backtest",
    "app/paper",
    "app/reports/builder.py",
]


@pytest.mark.parametrize(
    "value",
    [
        1.5,
        0.1,
        None,
        True,
        False,
        [],
        {},
        b"1",
        "abc",
        "NaN",
        "Infinity",
        "1e999999999",
        "-Infinity",
    ],
)
def test_dec_refuses_floats_bools_and_non_numbers(value: Any) -> None:
    with pytest.raises(m.MoneyError):
        m.dec(value)


def test_dec_accepts_text_ints_and_decimals_exactly() -> None:
    assert m.dec("0.1") + m.dec("0.2") == Decimal("0.3")  # no binary rounding
    assert m.dec(5) == 5 and m.dec(Decimal("1.50")) == Decimal("1.5")
    with pytest.raises(m.MoneyError):
        m.dec("1" * 60)


def test_quantize_is_conservative() -> None:
    step = Decimal("0.01")
    assert m.quantize_down(Decimal("1.239"), step) == Decimal("1.23")
    assert m.quantize_up(Decimal("1.231"), step) == Decimal("1.24")
    assert (
        m.quantize_down(Decimal("1.23"), step)
        == m.quantize_up(Decimal("1.23"), step)
        == Decimal("1.23")
    )
    assert m.quantize_down(Decimal("0.00999"), step) == 0
    for bad in (Decimal(0), Decimal(-1), Decimal("NaN")):
        with pytest.raises(m.MoneyError):
            m.quantize_down(Decimal(1), bad)


def test_fees_round_up_and_never_undercharge() -> None:
    assert m.fee_for(Decimal("10.01"), Decimal("0.004"), Decimal("0.01")) == Decimal(
        "0.05"
    )  # 0.04004 -> 0.05
    assert m.fee_for(Decimal("0"), Decimal("0.004"), Decimal("0.01")) == 0
    with pytest.raises(m.MoneyError):
        m.fee_for(Decimal("-1"), Decimal("0.004"), Decimal("0.01"))


def test_geometric_ratio_reproduces_the_range() -> None:
    lower, upper = Decimal("100"), Decimal("108")
    r = m.geometric_ratio(lower, upper, 4)
    assert abs(lower * r**4 - upper) < Decimal("1e-20")
    assert r > 1
    for bad in (
        (Decimal(0), Decimal(1), 2),
        (Decimal(2), Decimal(1), 2),
        (Decimal(1), Decimal(2), 0),
    ):
        with pytest.raises(m.MoneyError):
            m.geometric_ratio(*bad)


def test_results_do_not_depend_on_the_ambient_context() -> None:
    import decimal

    reference = m.geometric_ratio(Decimal("100"), Decimal("110"), 3)
    with decimal.localcontext() as ctx:
        ctx.prec = 5
        ctx.rounding = decimal.ROUND_DOWN
        assert m.geometric_ratio(Decimal("100"), Decimal("110"), 3) == reference


def test_canonical_text_is_stable() -> None:
    assert m.canonical(Decimal("1.2300")) == "1.23" and m.canonical(Decimal("100")) == "100"
    assert m.canonical(Decimal("1E+2")) == "100" and m.canonical(Decimal("-0")) == "0"
    assert m.pct(Decimal("0.0234545")) == "2.3454%" or m.pct(Decimal("0.0234545")) == "2.3455%"


def _files(target: str) -> list[Path]:
    path = ROOT / target
    return [path] if path.is_file() else sorted(path.rglob("*.py"))


def test_no_financial_module_uses_float_or_float_literals_or_math() -> None:
    offences: list[str] = []
    for target in FINANCIAL:
        for path in _files(target):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Name) and node.id == "float":
                    offences.append(f"{path.name}:{node.lineno} float")
                if isinstance(node, ast.Constant) and isinstance(node.value, float):
                    offences.append(f"{path.name}:{node.lineno} float literal")
                if isinstance(node, ast.Import) and any(
                    a.name in ("math", "numpy", "pandas", "random") for a in node.names
                ):
                    offences.append(f"{path.name}:{node.lineno} import")
                if isinstance(node, ast.ImportFrom) and node.module in (
                    "math",
                    "numpy",
                    "pandas",
                    "random",
                ):
                    offences.append(f"{path.name}:{node.lineno} import")
    assert offences == []
