"""Settings fail closed (5.9 item 6).

Phase 1.0 FAILING SKELETON (baseline 5.5, 5.9). Replace with real tests before the code exists.
"""

import pytest


def test_missing_secret_unknown_setting_or_mode_live_refuses_to_start():
    pytest.fail("NOT IMPLEMENTED (Phase 1.0 skeleton): fail closed")


def test_no_live_setting_exists():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): no LIVE_* setting; SecretStr loaded from files"
    )
