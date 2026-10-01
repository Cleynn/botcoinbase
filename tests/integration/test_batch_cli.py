"""The host CLI for market data, backtests and paper trading (td_ctl only; synthetic data)."""

from __future__ import annotations

from typing import Any

import pytest

from app.batch_cli import main
from app.cli import main as cli_main
from tests.conftest import TestDb
from tests.integration.conftest import Market


def run(m: Market, db: TestDb, args: list[str], out: list[str], role: str = "td_ctl") -> int:
    settings = m.settings.model_copy(update={"database": db.settings_for(role)})
    return main(
        args,
        settings=settings,
        storage=m.storage,
        client=m.coinbase.client(),
        clock=m.clock,
        sleep=m.sleeps.append,
        out=out.append,
    )


def discover(m: Market) -> None:
    from tests.integration.test_market_ingest import discover as d

    d(m)


def test_import_is_a_dry_run_unless_commit_is_given(mkt: Market, db: TestDb, sql: Any) -> None:
    discover(mkt)
    out: list[str] = []
    assert run(mkt, db, ["market", "import", "--product", "BTC-USDC"], out) == 0
    assert "DRY RUN (nothing written; pass --commit to write)" in out[0]
    assert sql("SELECT count(*) AS n FROM candles")[0]["n"] == 0
    out.clear()
    assert run(mkt, db, ["market", "import", "--product", "BTC-USDC", "--commit"], out) == 0
    assert out[0].startswith("COMMIT: COMPLETE")
    assert sql("SELECT count(*) AS n FROM candles")[0]["n"] == 7 * 288


def test_the_full_flow_snapshot_backtest_quality_and_report(
    mkt: Market, db: TestDb, sql: Any
) -> None:
    mkt.imported()
    out: list[str] = []
    assert run(mkt, db, ["market", "snapshot", "--product", "BTC-USDC"], out) == 0
    assert "created" in out[0] and "sha256=" in out[0]
    snap_id = sql("SELECT id FROM dataset_snapshots")[0]["id"]
    out.clear()
    assert run(mkt, db, ["market", "snapshots"], out) == 0 and str(snap_id) in out[0]
    out.clear()
    assert run(mkt, db, ["backtest", "run", "--snapshot", str(snap_id)], out) == 0
    assert "BACKTEST" in out[0] and "created" in out[0]
    out.clear()
    assert run(mkt, db, ["backtest", "run", "--snapshot", str(snap_id)], out) == 0
    assert "already existed" in out[0]
    out.clear()
    assert run(mkt, db, ["market", "quality", "--product", "BTC-USDC"], out) == 0
    assert "DATA_QUALITY" in out[0]


def test_the_web_role_may_not_run_the_host_cli(
    mkt: Market, db: TestDb, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(mkt, db, ["paper", "status"], [], role="td_app") == 2
    assert "host CLI role" in capsys.readouterr().err


def test_refusals_print_a_fixed_code_and_exit_nonzero(
    mkt: Market, db: TestDb, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(mkt, db, ["market", "snapshot", "--product", "BTC-USDC"], []) == 1
    assert "error: UNKNOWN_PRODUCT" in capsys.readouterr().err
    assert run(mkt, db, ["paper", "start"], []) == 1
    assert "error: MODE_NOT_PAPER" in capsys.readouterr().err
    assert run(mkt, db, ["paper", "step"], []) == 1
    assert "error: NOT_RUNNING" in capsys.readouterr().err


def test_paper_status_and_report_work_before_anything_runs(mkt: Market, db: TestDb) -> None:
    out: list[str] = []
    assert run(mkt, db, ["paper", "status"], out) == 0
    assert out[0].startswith("MODE: PAPER | session=PAUSED")
    out.clear()
    assert run(mkt, db, ["paper", "report"], out) == 0 and "PAPER_DAILY" in out[0]


def test_the_top_level_cli_routes_the_new_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def record(argv: Any) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr("app.batch_cli.main", record)
    for group in ("market", "backtest", "paper", "review"):
        assert cli_main([group, "x"]) == 0
    assert [s[0] for s in seen] == ["market", "backtest", "paper", "review"]


def test_the_cli_has_no_order_withdraw_or_sell_command() -> None:  # `live` exists since DEC-026
    from app.batch_cli import _parser

    text = _parser().format_help().lower() + " ".join(
        a.dest
        for a in _parser()._actions  # noqa: SLF001
    )
    for word in ("order", "withdraw", "sell", "kill"):
        assert word not in text
