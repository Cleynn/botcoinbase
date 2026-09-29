"""`review build|verify|cleanup|list` on the host CLI (td_ctl only)."""

from __future__ import annotations

from typing import Any

import pytest

from app.batch_cli import main
from tests.conftest import TestDb
from tests.integration.review_env import ReviewEnv
from tests.integration.test_review_service import enabled, requested


def run(
    review: ReviewEnv,
    ctl_storage: Any,
    db: TestDb,
    args: list[str],
    out: list[str],
    role: str = "td_ctl",
) -> int:
    settings = review.settings.model_copy(update={"database": db.settings_for(role)})
    return main(
        ["review", *args],
        settings=settings,
        storage=ctl_storage,
        clock=review.clock,
        out=out.append,
    )


def test_build_verify_list_and_cleanup(review: ReviewEnv, ctl_storage: Any, db: TestDb) -> None:
    enabled(review, days=1)
    pid = requested(review)
    out: list[str] = []
    assert run(review, ctl_storage, db, ["build"], out) == 0
    assert f"package {pid} READY" in out[0] and "1 package(s) processed" in out[-1]
    out.clear()
    assert run(review, ctl_storage, db, ["verify", "--all"], out) == 0
    assert out == [f"package {pid} OK"]
    out.clear()
    assert run(review, ctl_storage, db, ["verify", "--id", str(pid)], out) == 0
    out.clear()
    assert run(review, ctl_storage, db, ["list"], out) == 0 and str(pid) in out[0]
    out.clear()
    review.clock.advance(2 * 86400)
    assert run(review, ctl_storage, db, ["cleanup"], out) == 0
    assert out == ["expired=1 content_removed=1 orphan_files_removed=0"]
    assert list(review.review_dir.glob("*.zip")) == []


def test_verify_exits_nonzero_and_names_the_problem_for_a_damaged_package(
    review: ReviewEnv, ctl_storage: Any, db: TestDb, sql: Any
) -> None:
    enabled(review)
    requested(review)
    review.builder.build_pending()
    path = review.review_dir / sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    path.chmod(0o644)
    path.write_bytes(path.read_bytes()[:-25])
    out: list[str] = []
    assert run(review, ctl_storage, db, ["verify", "--all"], out) == 1
    assert "CORRUPT" in out[0]


def test_a_failed_build_exits_nonzero(review: ReviewEnv, ctl_storage: Any, db: TestDb) -> None:
    enabled(review)
    requested(review)
    review.chain("disable")
    out: list[str] = []
    assert run(review, ctl_storage, db, ["build"], out) == 1
    assert "FAILED FEATURE_DISABLED" in out[0]


def test_the_web_role_cannot_run_the_review_cli(
    review: ReviewEnv, ctl_storage: Any, db: TestDb, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(review, ctl_storage, db, ["build"], [], role="td_app") == 2
    assert "host CLI role" in capsys.readouterr().err


def test_the_cli_offers_no_way_to_enable_request_download_or_send_anything(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.batch_cli import _parser

    with pytest.raises(SystemExit):
        _parser().parse_args(["review", "--help"])
    words = capsys.readouterr().out.lower().replace("build every requested review package", "")
    for forbidden in ("enable", "create", "request", "download", "upload", "send", "llm", "import"):
        assert forbidden not in words, forbidden
