"""Build identity and image scan (BI-01..BI-03, BI-10, 5.9 items 7, 18).

Phase 1.0 FAILING SKELETON (baseline 5.5, 5.9). Replace with real tests before the code exists.
"""

import pytest


def test_no_env_config_or_db_value_can_select_live():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): hostile env vars/config/DB rows naming other safety levels or venues have no effect"
    )


def test_deploy_refuses_unapproved_digest_and_mislabelled_image():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): digest must be in approved-digests.txt and match host role marker"
    )


def test_paper_image_has_no_signer_key_loader_or_private_endpoints():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): image scan finds no live-lab code, JWT signer, request builders or private constants"
    )


def test_secret_scan_rejects_key_material_patterns():
    pytest.fail(
        "NOT IMPLEMENTED (Phase 1.0 skeleton): scan flags secrets.toml and key patterns; canary is detected"
    )
