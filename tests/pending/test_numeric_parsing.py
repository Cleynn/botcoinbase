"""core.numeric: one Decimal context (BI-11, CB-6, 5.9 item 6).

Phase 1.0 FAILING SKELETON (baseline 5.5, 5.9). Replace with real tests before the code exists.
"""

import pytest


def test_rejects_nan_infinity_exponent_and_excess_scale():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): NaN, Infinity, 1e3 forms and scale > 18 rejected"
    )


def test_json_numbers_parse_with_decimal():
    pytest.fail("NOT IMPLEMENTED (Phase 1.0 skeleton): parse_float=Decimal; no float ever produced")


def test_float_in_money_code_is_lint_failure():
    pytest.fail("NOT IMPLEMENTED (Phase 1.0 skeleton): float ban lint fires on a scratch violation")
