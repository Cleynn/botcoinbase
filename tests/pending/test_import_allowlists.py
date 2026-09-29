"""Import-linter contracts of baseline 3.6 (5.9 item 5).

Phase 1.0 FAILING SKELETON (baseline 5.5, 5.9). Replace with real tests before the code exists.
"""

import pytest


def test_web_imports_only_allowed_packages():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): web -> core, domain, db.queries, db.commands, services.shared, services.web_ops, adapters.filestore"
    )


def test_services_ops_packages_mutually_independent():
    pytest.fail("NOT IMPLEMENTED (Phase 1.0 skeleton): services/*_ops do not import each other")


def test_scratch_forbidden_import_makes_contract_fail():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): adding a forbidden import proves the contract fails"
    )


def test_live_lab_never_importable_from_paper_package():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): tradingdots must never import tradingdots_live_lab (BI-01)"
    )
