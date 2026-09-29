"""Backtests use frozen snapshots only; reports are immutable and reproducible (synthetic data)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from app.backtest.service import BacktestError, BacktestService
from app.config import FeePolicy
from app.market.snapshots import snapshot_dir
from tests.conftest import with_fees
from tests.integration.conftest import Market
from tests.integration.test_snapshots import freeze
from tests.market_env import ingest_settings

Sql = Callable[..., list[dict[str, Any]]]


def service(m: Market) -> BacktestService:
    return BacktestService(storage=m.storage, clock=m.clock, settings=m.settings)


def snapshot(m: Market, days: int = 7) -> Any:
    if days != 7:
        m.settings = ingest_settings(m.settings, str(m.data_dir), days=days)
    m.imported()
    row, _ = freeze(m, days)
    return row


def test_a_backtest_reports_both_fee_scenarios_and_labels_itself_backtest(
    mkt: Market, sql: Sql
) -> None:
    snap = snapshot(mkt)
    report, created = service(mkt).run(snap.id)
    assert created and report.kind == "BACKTEST" and report.mode_label == "BACKTEST"
    body = json.loads(report.body_json)
    assert set(body["scenarios"]) == {"OPERATOR", "STRESS"}
    assert body["label"] == "BACKTEST" and body["growth_policy"] == "disabled"
    assert "LIVE TRADING" in body["banner"] and body["snapshot"]["id"] == str(snap.id)
    assert body["snapshot"]["file_sha256"] == snap.file_sha256
    fees = {k: v["fee_rate"] for k, v in body["scenarios"].items()}
    assert fees == {
        "OPERATOR": "0.002",
        "STRESS": str(mkt.settings.pair_policy.fees.stress_maker_rate),
    }
    assert hashlib.sha256(report.body_json.encode()).hexdigest() == report.sha256
    assert "not a promise of profit" in report.body_md and report.sha256 in report.body_md
    assert sql("SELECT count(*) AS n FROM backtest_runs")[0]["n"] == 2  # one per scenario


def test_the_stress_scenario_never_beats_the_operator_scenario_on_fees_paid(mkt: Market) -> None:
    body = json.loads(service(mkt).run(snapshot(mkt).id)[0].body_json)
    op, st = body["scenarios"]["OPERATOR"], body["scenarios"]["STRESS"]
    assert float(st["performance"]["fees_paid"]) >= float(op["performance"]["fees_paid"])
    assert float(st["performance"]["net_pnl"]) <= float(op["performance"]["net_pnl"])


def test_repeating_a_backtest_creates_nothing_new(mkt: Market, sql: Sql) -> None:
    snap = snapshot(mkt)
    first, created_first = service(mkt).run(snap.id)
    mkt.clock.advance(3600)
    second, created_second = service(mkt).run(snap.id)
    assert created_first and not created_second
    assert first.id == second.id and first.sha256 == second.sha256
    assert sql("SELECT count(*) AS n FROM reports")[0]["n"] == 1
    assert sql("SELECT count(*) AS n FROM backtest_runs")[0]["n"] == 2


def test_a_walk_forward_report_has_folds_and_out_of_sample_summary(mkt: Market) -> None:
    snap = snapshot(mkt, days=30)
    report, created = service(mkt).run(snap.id, walk=True)
    assert created and report.kind == "WALK_FORWARD" and report.mode_label == "BACKTEST"
    wf = json.loads(report.body_json)["walk_forward"]
    assert set(wf) == {"OPERATOR", "STRESS"}
    for scenario in wf.values():
        assert scenario["summary"]["fold_count"] >= 1
        assert len(scenario["folds"]) == scenario["summary"]["fold_count"]
    assert "Walk-forward" in report.body_md


def test_a_different_level_count_is_a_different_report(mkt: Market) -> None:
    snap = snapshot(mkt)
    a, _ = service(mkt).run(snap.id, levels=3)
    b, _ = service(mkt).run(snap.id, levels=5)
    assert a.sha256 != b.sha256


def test_levels_outside_three_to_five_are_refused(mkt: Market) -> None:
    snap = snapshot(mkt)
    for levels in (2, 6):
        with pytest.raises(Exception, match="levels|LEVELS"):
            service(mkt).run(snap.id, levels=levels)


def test_an_unattested_fee_refuses_to_backtest(mkt: Market) -> None:
    snap = snapshot(mkt)
    mkt.settings = with_fees(mkt.settings, FeePolicy())
    with pytest.raises(BacktestError, match="FEE_NOT_ATTESTED"):
        service(mkt).run(snap.id)


def test_an_expired_fee_attestation_refuses_to_backtest(mkt: Market) -> None:
    snap = snapshot(mkt)
    mkt.settings = with_fees(
        mkt.settings, FeePolicy(operator_maker_rate="0.002", attested_on=date(2025, 1, 1))
    )
    with pytest.raises(BacktestError, match="FEE_ATTESTATION_EXPIRED"):
        service(mkt).run(snap.id)


def test_a_snapshot_too_short_for_one_walk_forward_fold_is_refused(mkt: Market) -> None:
    with pytest.raises(BacktestError, match="TOO_SHORT_FOR_WALK_FORWARD"):
        service(mkt).run(snapshot(mkt).id, walk=True)


def test_an_unknown_snapshot_is_refused(mkt: Market) -> None:
    mkt.imported()
    with pytest.raises(BacktestError, match="UNKNOWN_SNAPSHOT"):
        service(mkt).run(uuid4())


def test_a_tampered_snapshot_file_is_refused_not_backtested(mkt: Market) -> None:
    snap = snapshot(mkt)
    path = snapshot_dir(mkt.settings) / snap.file_name
    path.chmod(0o644)
    data = bytearray(path.read_bytes())
    data[len(data) // 3] ^= 0x55
    path.write_bytes(bytes(data))
    with pytest.raises(BacktestError, match="SNAPSHOT_CHECKSUM_MISMATCH"):
        service(mkt).run(snap.id)


def test_new_candles_after_the_freeze_do_not_change_the_result(mkt: Market) -> None:
    snap = snapshot(mkt)
    before, _ = service(mkt).run(snap.id)
    mkt.clock.advance(6 * 3600)
    mkt.importer().run("BTC-USDC", commit=True)  # more data arrives; the snapshot is frozen
    after, created = service(mkt).run(snap.id)
    assert not created and after.sha256 == before.sha256


def test_the_backtest_touches_no_network(mkt: Market) -> None:
    snap = snapshot(mkt, days=30)
    seen = len(mkt.coinbase.requests)
    service(mkt).run(snap.id)
    service(mkt).run(snap.id, walk=True)
    assert len(mkt.coinbase.requests) == seen


def test_backtest_and_report_events_are_audited(mkt: Market, sql: Sql) -> None:
    service(mkt).run(snapshot(mkt).id)
    codes = [r["event_code"] for r in sql("SELECT event_code FROM audit_events ORDER BY seq")]
    assert "backtest.completed" in codes and "report.created" in codes


def test_reports_cannot_be_changed_or_deleted(mkt: Market, sql: Sql) -> None:
    import psycopg

    service(mkt).run(snapshot(mkt).id)
    with pytest.raises(psycopg.Error):
        sql("UPDATE reports SET body_json = '{}'")
    with pytest.raises(psycopg.Error):
        sql("DELETE FROM reports")
    with pytest.raises(psycopg.Error):
        sql("UPDATE backtest_runs SET summary = '{}'::jsonb")


def test_the_data_quality_report_counts_events_without_inventing_data(mkt: Market) -> None:
    mkt.imported()
    report, created = service(mkt).quality_report("BTC-USDC")
    body = json.loads(report.body_json)
    assert created and report.kind == "DATA_QUALITY" and body["stored_candles"] == 7 * 288
    assert "never filled" in report.body_md
