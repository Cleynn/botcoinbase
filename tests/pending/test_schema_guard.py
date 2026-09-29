"""Schema guard both directions (BI-35, 5.9 item 9).

Phase 1.0 FAILING SKELETON (baseline 5.5, 5.9). Replace with real tests before the code exists.
"""

import pytest


def test_refuses_older_schema():
    pytest.fail("NOT IMPLEMENTED (Phase 1.0 skeleton): code newer than DB head refuses to start")


def test_refuses_newer_schema():
    pytest.fail("NOT IMPLEMENTED (Phase 1.0 skeleton): DB newer than code minimum refuses to start")


def test_alembic_downgrade_base_works():
    pytest.fail("NOT IMPLEMENTED (Phase 1.0 skeleton): downgrade base tested (5.9 item 22)")
