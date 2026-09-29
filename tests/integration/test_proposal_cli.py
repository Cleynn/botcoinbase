"""`proposal validate|cleanup|list` on the host CLI (td_ctl only). Synthetic content."""

from __future__ import annotations

from typing import Any

import pytest

from app.batch_cli import _parser, main
from tests.conftest import TestDb
from tests.integration.proposal_env import ProposalEnv


def run(
    prop: ProposalEnv,
    ctl_storage: Any,
    db: TestDb,
    args: list[str],
    out: list[str],
    role: str = "td_ctl",
) -> int:
    settings = prop.settings.model_copy(update={"database": db.settings_for(role)})
    return main(
        ["proposal", *args],
        settings=settings,
        storage=ctl_storage,
        clock=prop.clock,
        out=out.append,
    )


def test_validate_list_and_cleanup(prop: ProposalEnv, ctl_storage: Any, db: TestDb) -> None:
    prop.enable()
    good = prop.import_doc()
    bad = prop.import_doc(
        prop.document(proposal_id="prop-2026-002", suggested_change="Disable the risk limits.")
    )
    out: list[str] = []
    assert run(prop, ctl_storage, db, ["validate"], out) == 0
    text = "\n".join(out)
    assert f"proposal {good} VALIDATED" in text
    assert f"proposal {bad} REJECTED" in text and "RISK_BYPASS" in text
    assert "2 proposal(s) processed" in out[-1]
    out.clear()
    assert run(prop, ctl_storage, db, ["list"], out) == 0
    assert any(str(good) in line and "VALIDATED" in line for line in out)
    out.clear()
    assert run(prop, ctl_storage, db, ["cleanup"], out) == 0
    assert out == ["content_removed=0 orphan_files_removed=0"]


def test_cleanup_removes_only_orphans_and_old_closed_content(
    prop: ProposalEnv, ctl_storage: Any, db: TestDb
) -> None:
    prop.enable()
    prop.import_doc(prop.document(suggested_change="Disable the risk limits."))
    run(prop, ctl_storage, db, ["validate"], [])
    orphan = prop.directory / "00000000-0000-4000-8000-000000000000.proposal"
    orphan.write_bytes(b"{}")
    out: list[str] = []
    prop.clock.advance(100 * 86400)
    assert run(prop, ctl_storage, db, ["cleanup"], out) == 0
    assert out == ["content_removed=1 orphan_files_removed=1"]
    assert not orphan.exists()


def test_validate_with_nothing_to_do_exits_zero(
    prop: ProposalEnv, ctl_storage: Any, db: TestDb
) -> None:
    out: list[str] = []
    assert run(prop, ctl_storage, db, ["validate"], out) == 0
    assert out == ["0 proposal(s) processed"]


def test_the_web_role_cannot_run_the_proposal_cli(
    prop: ProposalEnv, ctl_storage: Any, db: TestDb, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(prop, ctl_storage, db, ["validate"], [], role="td_app") == 2
    assert "host CLI role" in capsys.readouterr().err


def test_the_cli_offers_no_way_to_import_approve_apply_or_send(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["proposal", "--help"])
    words = capsys.readouterr().out.lower().replace("validate every imported proposal", "")
    for forbidden in ("apply", "approve", "upload", "import", "send", "enable", "implement"):
        assert forbidden not in words, forbidden
