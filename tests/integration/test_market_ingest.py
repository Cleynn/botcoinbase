"""Importer behaviour against a real database and SYNTHETIC candles (never real Coinbase data)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import psycopg
import pytest

from app.market.candles import last_closed_end
from app.market.ingest import STEP
from app.pairs.runner import RunnerError
from tests.conftest import TestDb
from tests.integration.conftest import Market

Sql = Callable[..., list[dict[str, Any]]]
DAYS = 7
EXPECTED = DAYS * 288


def discover(m: Market) -> None:
    from app.pairs.runner import PairRunner

    PairRunner(
        storage=m.storage, clock=m.clock, settings=m.settings, client=m.coinbase.client()
    ).discover()


def counts(sql: Sql) -> dict[str, int]:
    return {
        t: sql(f"SELECT count(*) AS n FROM {t}")[0]["n"]  # noqa: S608 - fixed table names
        for t in (
            "candles",
            "ingest_runs",
            "candle_conflicts",
            "data_quality_events",
            "ingest_cursors",
        )
    }


def test_the_default_is_a_dry_run_and_writes_nothing(mkt: Market, sql: Sql) -> None:
    discover(mkt)
    before = counts(sql)
    audit_before = sql("SELECT count(*) AS n FROM audit_events")[0]["n"]
    result = mkt.importer().run("BTC-USDC")
    assert result.mode == "DRY_RUN" and result.status == "COMPLETE"
    assert result.to_insert == EXPECTED and result.inserted == 0 and result.run_id is None
    assert counts(sql) == before
    assert sql("SELECT count(*) AS n FROM audit_events")[0]["n"] == audit_before


def test_commit_writes_candles_a_run_a_cursor_and_one_audit_event(mkt: Market, sql: Sql) -> None:
    discover(mkt)
    result = mkt.importer().run("BTC-USDC", commit=True)
    assert result.mode == "COMMIT" and result.inserted == EXPECTED
    assert result.gaps == 0 and result.missing == 0
    c = counts(sql)
    assert c["candles"] == EXPECTED and c["ingest_runs"] == 1 and c["ingest_cursors"] == 1
    cursor = sql("SELECT covered_until FROM ingest_cursors")[0]["covered_until"]
    assert cursor == last_closed_end(int(mkt.clock.now().timestamp()), STEP) == result.cursor_after
    events = sql("SELECT * FROM audit_events WHERE event_code = 'market.ingested'")
    assert len(events) == 1 and events[0]["actor_role"] == "HOST_CLI"
    detail = json.loads(events[0]["detail"])
    assert detail["inserted"] == EXPECTED and detail["requests"] == result.requests


def test_a_second_commit_is_idempotent_and_only_asks_for_new_candles(mkt: Market, sql: Sql) -> None:
    mkt.imported()
    first_calls = len(mkt.source.calls)
    again = mkt.importer().run("BTC-USDC", commit=True)
    assert again.status == "NOTHING_TO_DO" and again.inserted == 0
    assert counts(sql)["candles"] == EXPECTED
    assert len(mkt.source.calls) == first_calls  # nothing was fetched
    mkt.clock.advance(3600)
    more = mkt.importer().run("BTC-USDC", commit=True)
    assert more.status == "COMPLETE" and more.inserted == 12 and more.duplicates == 0
    assert counts(sql)["candles"] == EXPECTED + 12


def test_gaps_are_recorded_and_never_filled(mkt: Market, sql: Sql) -> None:
    discover(mkt)
    end = last_closed_end(int(mkt.clock.now().timestamp()), STEP)
    hole = {end - 500 * STEP + i * STEP for i in range(10)}
    mkt.source.missing = hole
    result = mkt.importer().run("BTC-USDC", commit=True)
    assert result.status == "COMPLETE" and result.gaps == 1 and result.missing == 10
    stored = {r["start_ts"] for r in sql("SELECT start_ts FROM candles")}
    assert not (stored & hole) and len(stored) == EXPECTED - 10
    gap = sql("SELECT * FROM data_quality_events WHERE code = 'GAP'")
    assert len(gap) == 1 and gap[0]["count"] == 10


def test_malformed_and_invalid_candles_are_excluded_and_reported(mkt: Market, sql: Sql) -> None:
    discover(mkt)
    end = last_closed_end(int(mkt.clock.now().timestamp()), STEP)
    bad_ohlc, negative = end - 300 * STEP, end - 200 * STEP
    mkt.source.overrides = {
        bad_ohlc: {"high": "1", "low": "500"},  # high below low
        negative: {"close": "-5"},
    }
    result = mkt.importer().run("BTC-USDC", commit=True)
    stored = {r["start_ts"] for r in sql("SELECT start_ts FROM candles")}
    assert bad_ohlc not in stored and negative not in stored
    codes = {r["code"] for r in sql("SELECT code FROM data_quality_events")}
    assert "INVALID_OHLC" in codes and result.gaps >= 1


def test_duplicate_candles_in_a_response_are_stored_once(mkt: Market, sql: Sql) -> None:
    discover(mkt)
    end = last_closed_end(int(mkt.clock.now().timestamp()), STEP)
    mkt.source.duplicates = {end - 100 * STEP}
    mkt.importer().run("BTC-USDC", commit=True)
    assert counts(sql)["candles"] == EXPECTED
    assert (
        sql("SELECT count(*) AS n FROM data_quality_events WHERE code = 'DUPLICATE'")[0]["n"] >= 1
    )


def test_a_changed_repeat_is_a_conflict_and_the_stored_candle_stands(mkt: Market, sql: Sql) -> None:
    mkt.imported()
    row = sql("SELECT start_ts, close FROM candles ORDER BY start_ts OFFSET 100 LIMIT 1")[0]
    # An operator who lost the cursor re-imports the window; the cursor guard is bypassed as owner.
    sql("ALTER TABLE ingest_cursors DISABLE TRIGGER USER")
    sql("DELETE FROM ingest_cursors")
    sql("ALTER TABLE ingest_cursors ENABLE TRIGGER USER")
    mkt.source.overrides = {row["start_ts"]: {"close": "100.01", "high": "999", "low": "1"}}
    result = mkt.importer().run("BTC-USDC", commit=True)
    assert result.conflicts == 1 and result.inserted == 0 and result.duplicates == EXPECTED - 1
    stored = sql("SELECT close FROM candles WHERE start_ts = %s", (row["start_ts"],))[0]["close"]
    assert stored == row["close"]  # the stored value stands
    conflict = sql("SELECT stored, incoming FROM candle_conflicts")
    assert len(conflict) == 1 and conflict[0]["stored"] != conflict[0]["incoming"]
    assert sql("SELECT count(*) AS n FROM data_quality_events WHERE code = 'CONFLICT'")[0]["n"] == 1


def test_transient_failures_are_retried_a_bounded_number_of_times(mkt: Market) -> None:
    discover(mkt)
    mkt.coinbase.errors["/candles"] = 503
    result = mkt.importer().run("BTC-USDC", commit=False)
    assert result.status == "FAILED"
    candle_requests = [r for r in mkt.coinbase.requests if r.url.path.endswith("/candles")]
    assert len(candle_requests) == mkt.settings.data.max_attempts  # bounded, then it stops
    assert mkt.sleeps == [0.0, 0.0] and result.retries == 2


def test_a_client_error_is_not_retried(mkt: Market) -> None:
    discover(mkt)
    mkt.coinbase.errors["/candles"] = 400
    result = mkt.importer().run("BTC-USDC", commit=False)
    assert result.status == "FAILED" and mkt.sleeps == []
    candle_requests = [r for r in mkt.coinbase.requests if r.url.path.endswith("/candles")]
    assert len(candle_requests) == 1


def test_a_failed_chunk_keeps_the_cursor_behind_the_good_span(mkt: Market, sql: Sql) -> None:
    discover(mkt)
    original, seen = mkt.coinbase.handle, {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/candles"):
            seen["n"] += 1
            if seen["n"] > 2:  # the first two chunks answer, the rest are a client error
                return httpx.Response(
                    400, content=b"{}", headers={"content-type": "application/json"}
                )
        return original(request)  # type: ignore[no-any-return]

    mkt.coinbase.handle = handle
    result = mkt.importer().run("BTC-USDC", commit=True)
    assert result.status == "PARTIAL" and result.cursor_after is not None
    assert result.cursor_after < last_closed_end(int(mkt.clock.now().timestamp()), STEP)
    assert (
        sql("SELECT covered_until FROM ingest_cursors")[0]["covered_until"] == result.cursor_after
    )
    assert sql("SELECT max(start_ts) AS m FROM candles")[0]["m"] < result.cursor_after
    fetch_errors = sql("SELECT count(*) AS n FROM data_quality_events WHERE code = 'FETCH_ERROR'")
    assert fetch_errors[0]["n"] == 1


def test_unknown_or_unhealthy_products_are_refused(mkt: Market) -> None:
    discover(mkt)
    with pytest.raises(RunnerError, match="UNKNOWN_PRODUCT"):
        mkt.importer().run("DOGE-USDC")
    mkt.coinbase.products["BTC-USDC"]["trading_disabled"] = True
    with pytest.raises(RunnerError, match="PRODUCT_NOT_OK"):
        mkt.importer().run("BTC-USDC", commit=True)


def test_bad_day_counts_are_refused(mkt: Market) -> None:
    discover(mkt)
    for days in (0, 366):
        with pytest.raises(RunnerError, match="BAD_DAYS"):
            mkt.importer().run("BTC-USDC", days=days)


def test_only_public_get_requests_are_made_and_none_carry_credentials(mkt: Market) -> None:
    mkt.imported()
    assert mkt.coinbase.requests
    for request in mkt.coinbase.requests:
        assert request.method == "GET" and request.content == b""
        assert "authorization" not in {k.lower() for k in request.headers}
        path = request.url.path.removeprefix("/api/v3/brokerage")
        assert path == "/time" or path.startswith("/market/")


def role_conn(db: TestDb, role: str) -> psycopg.Connection[Any]:
    cfg = db.settings_for(role)
    return psycopg.connect(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.name,
        user=cfg.user,
        password=cfg.password.get_secret_value() if cfg.password else None,
    )


def test_the_web_role_reads_but_cannot_write_market_tables(mkt: Market, db: TestDb) -> None:
    mkt.imported()
    with role_conn(db, "td_app") as conn:
        row = conn.execute("SELECT count(*) FROM candles").fetchone()
        assert row is not None and row[0] == EXPECTED
        for statement in (
            "UPDATE candles SET close = 1",
            "DELETE FROM candles",
            "INSERT INTO ingest_runs (product_uuid) VALUES (gen_random_uuid())",
            "UPDATE ingest_cursors SET covered_until = 0",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


def test_stored_candles_cannot_be_changed_even_by_the_host_role(mkt: Market, db: TestDb) -> None:
    mkt.imported()
    with role_conn(db, "td_ctl") as conn:
        for statement in ("UPDATE candles SET close = close + 1", "DELETE FROM candles"):
            with pytest.raises(psycopg.Error):
                conn.execute(statement)
            conn.rollback()
