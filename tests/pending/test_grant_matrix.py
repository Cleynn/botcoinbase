"""Grant matrix on real login roles vs an allowlist file independent of migrations (5.9 item 8).

Phase 1.0 FAILING SKELETON (baseline 5.5, 5.9). Replace with real tests before the code exists.
"""

import pytest


def test_td_web_cannot_write_evidence_sessions_ledger_orders():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): td_web has no write privileges on evidence, sessions, challenges, ledger, order or fill tables"
    )


def test_real_privileges_equal_allowlist():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): privileges read from a live Postgres with real login roles equal tests/invariants allowlist"
    )


def test_td_fn_is_nologin_sole_function_owner():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): td_fn is NOLOGIN and owns every SQL function; seven login roles have connection limits"
    )
