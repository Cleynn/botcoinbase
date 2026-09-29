"""SQL function inventory (BQ-3, 5.9 item 8).

Phase 1.0 FAILING SKELETON (baseline 5.5, 5.9). Replace with real tests before the code exists.
"""
import pytest


def test_at_most_24_functions_each_at_most_80_lines():
    pytest.fail("NOT IMPLEMENTED (Phase 1.0 skeleton): count <= 24 and each <= 80 lines")


def test_every_function_has_invariant_header_and_pinned_search_path():
    pytest.fail("NOT IMPLEMENTED (Phase 1.0 skeleton): header names a BI id; search_path pinned; REVOKE ALL FROM PUBLIC")


def test_no_dynamic_identifiers_and_no_actor_parameter():
    pytest.fail("NOT IMPLEMENTED (Phase 1.0 skeleton): no EXECUTE with identifiers; no actor parameter (BI-23)")
