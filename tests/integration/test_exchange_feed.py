"""Exchange feed: the host reads the account with GET-only calls and stores it; the web tier shows
what was stored and never reaches the exchange (FAKE exchange, real PostgreSQL)."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.batch_cli import _parser, main
from app.exchange.fake import Fault
from app.exchange.models import KeyPermissions
from app.feed.service import ExchangeFeed, FeedOutcome
from app.storage.database import Storage
from tests.conftest import ROOT, TestDb
from tests.integration.safety_env import SafetyEnv

Sql = Callable[..., list[dict[str, Any]]]
APP = ROOT / "app"


def feed(safe: SafetyEnv) -> ExchangeFeed:
    return ExchangeFeed(storage=safe.ctl, reader=safe.fake, clock=safe.clock)


def account(safe: SafetyEnv) -> str:
    safe.fake.set_balance("USDC", "50", "2.5")
    safe.fake.set_balance("BTC", "0.001")
    safe.fake.set_balance("ETH", "0")  # an empty wallet is not stored
    order_id = safe.fake.add_foreign_order()
    safe.fake.inject_fill(order_id, "60000", "0.0001", "0.01")
    return order_id


def cli(safe: SafetyEnv, db: TestDb, args: list[str], out: list[str], **kwargs: Any) -> int:
    settings = safe.settings.model_copy(update={"database": db.settings_for("td_ctl")})
    return main(
        ["feed", *args],
        settings=settings,
        storage=safe.ctl,
        clock=safe.clock,
        out=out.append,
        **kwargs,
    )


# ------------------------------------------------------------------ the host side
def test_a_refresh_only_reads_and_stores_everything(safe: SafetyEnv, sql: Sql) -> None:
    order_id = account(safe)
    assert feed(safe).refresh() == FeedOutcome(True, None, 2, 1, 1)
    assert safe.fake.calls == ["key_permissions", "list_accounts", "list_orders", "list_fills"]
    status = sql("SELECT * FROM exchange_feed")[0]
    assert (status["state"], status["error_code"]) == ("OK", None)
    assert status["succeeded_at"] == status["attempted_at"] == safe.clock.now()
    assert (status["can_view"], status["can_trade"], status["can_transfer"]) == (True, True, False)
    balances = sql("SELECT currency, available, hold FROM exchange_feed_balances ORDER BY 1")
    assert [(b["currency"], str(b["available"]), str(b["hold"])) for b in balances] == [
        ("BTC", "0.001", "0"),
        ("USDC", "50", "2.5"),
    ]
    assert sql("SELECT order_id, side FROM exchange_feed_orders") == [
        {"order_id": order_id, "side": "BUY"}
    ]
    assert sql("SELECT order_id, liquidity FROM exchange_feed_fills") == [
        {"order_id": order_id, "liquidity": "MAKER"}
    ]


def test_a_refresh_succeeds_on_the_real_database_clock(safe: SafetyEnv, sql: Sql) -> None:
    # Production has no test clock: td_now() is clock_timestamp(), which differs on every call.
    account(safe)
    sql("DELETE FROM td_test_clock")
    assert feed(safe).refresh().ok
    row = sql("SELECT state, succeeded_at = attempted_at AS same FROM exchange_feed")[0]
    assert row == {"state": "OK", "same": True}
    assert feed(safe).refresh().ok  # the update path too
    safe.fake.fail("list_fills", Fault("timeout"))
    assert not feed(safe).refresh().ok
    assert sql("SELECT succeeded_at < attempted_at AS later FROM exchange_feed")[0]["later"]


def test_a_malformed_response_is_a_recorded_failure_not_a_crash(safe: SafetyEnv, sql: Sql) -> None:
    safe.fake.fail("list_accounts", Fault("malformed"))  # raises ParseError, not ExchangeError
    assert feed(safe).refresh() == FeedOutcome(False, "UNEXPECTED_RESPONSE")
    assert sql("SELECT error_code FROM exchange_feed")[0]["error_code"] == "UNEXPECTED_RESPONSE"


def test_a_second_refresh_replaces_the_rows(safe: SafetyEnv, sql: Sql) -> None:
    account(safe)
    feed(safe).refresh()
    safe.fake.set_balance("BTC", "0")
    safe.fake.set_balance("USDC", "49")
    safe.clock.advance(60)
    assert feed(safe).refresh().balances == 1
    rows = sql("SELECT currency, available FROM exchange_feed_balances")
    assert [(r["currency"], str(r["available"])) for r in rows] == [("USDC", "49")]
    assert sql("SELECT succeeded_at FROM exchange_feed")[0]["succeeded_at"] == safe.clock.now()


def test_a_failed_read_keeps_the_last_good_data(safe: SafetyEnv, sql: Sql) -> None:
    account(safe)
    feed(safe).refresh()
    good = safe.clock.now()
    safe.clock.advance(60)
    safe.fake.fail("list_accounts", Fault("timeout"))
    assert feed(safe).refresh() == FeedOutcome(False, "TIMEOUT")
    status = sql("SELECT state, error_code, succeeded_at, attempted_at FROM exchange_feed")[0]
    assert (status["state"], status["error_code"]) == ("FAILED", "TIMEOUT")
    assert status["succeeded_at"] == good and status["attempted_at"] == safe.clock.now()
    assert sql("SELECT count(*) AS n FROM exchange_feed_balances")[0]["n"] == 2


def test_a_first_read_that_fails_stores_only_the_failure(safe: SafetyEnv, sql: Sql) -> None:
    safe.fake.fail("key_permissions", Fault("network"))
    assert not feed(safe).refresh().ok
    status = sql("SELECT state, succeeded_at, can_view FROM exchange_feed")[0]
    assert status == {"state": "FAILED", "succeeded_at": None, "can_view": None}


def test_an_unexpected_portfolio_type_is_stored_as_unknown(safe: SafetyEnv, sql: Sql) -> None:
    safe.fake.permissions = KeyPermissions(True, True, False, "weird type!")
    assert feed(safe).refresh().ok
    assert sql("SELECT portfolio_type FROM exchange_feed")[0]["portfolio_type"] == "UNKNOWN"


def test_the_feed_writes_nothing_the_order_path_or_the_breaker_reads(
    safe: SafetyEnv, sql: Sql
) -> None:
    account(safe)
    before = safe.control_row()
    safe.fake.fail("list_orders", Fault("timeout"))
    feed(safe).refresh()
    feed(safe).refresh()
    assert safe.control_row() == before
    for table in ("api_events", "order_intents", "order_attempts", "reconciliation_runs"):
        assert sql(f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0, table  # noqa: S608


def test_only_the_host_role_can_write_the_feed(safe: SafetyEnv, storage: Storage, sql: Sql) -> None:
    feed(safe).refresh()
    with pytest.raises(psycopg.errors.InsufficientPrivilege), storage.tx() as repos:
        repos.feed.record_failure("COINBASE", "TIMEOUT")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), storage.tx() as repos:
        repos.feed.record_success(
            "COINBASE",
            can_view=True,
            can_trade=True,
            can_transfer=False,
            portfolio_type="DEFAULT",
            balances=[],
            orders=[],
            fills=[],
        )
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):  # not even the owner
        sql("UPDATE exchange_feed SET state = 'OK', error_code = NULL")
    with storage.tx() as repos:  # the web role reads
        assert repos.feed.status("COINBASE") is not None


# ------------------------------------------------------------------ the host commands
def test_feed_has_two_commands_and_neither_can_trade() -> None:
    parser = _parser()
    assert parser.parse_args(["feed", "once"]).command == "once"
    assert parser.parse_args(["feed", "run"]).interval == 60
    for forbidden in ("order", "submit", "buy", "sell", "cancel", "arm"):
        with pytest.raises(SystemExit):
            parser.parse_args(["feed", forbidden])
    for interval in ("14", "3601", "x"):
        with pytest.raises(SystemExit):
            parser.parse_args(["feed", "run", "--interval", interval])


def test_feed_once_reports_the_result(safe: SafetyEnv, db: TestDb) -> None:
    account(safe)
    out: list[str] = []
    assert cli(safe, db, ["once"], out, reader=safe.fake) == 0
    assert out == ["feed: OK balances=2 orders=1 fills=1"]
    safe.fake.fail("list_fills", Fault("rate_limited"))
    out.clear()
    assert cli(safe, db, ["once"], out, reader=safe.fake) == 1
    assert out == ["feed: FAILED RATE_LIMITED (the previous data is kept)"]


def test_feed_without_a_key_file_says_so(safe: SafetyEnv, db: TestDb) -> None:
    out: list[str] = []
    assert cli(safe, db, ["once"], out) == 1 and "NO_CREDENTIALS" in out[0]


def test_feed_run_repeats_and_reports_only_changes(safe: SafetyEnv, db: TestDb) -> None:
    account(safe)
    out: list[str] = []
    sleeps: list[float] = []
    args = ["run", "--interval", "15", "--max-runs", "3"]
    assert cli(safe, db, args, out, reader=safe.fake, sleep=sleeps.append) == 0
    assert sleeps == [15, 15]
    assert out == ["feed: OK balances=2 orders=1 fills=1"]  # said once, not three times
    assert safe.fake.calls.count("list_accounts") == 3
    assert not {"submit", "cancel"} & set(safe.fake.calls)


def test_feed_run_survives_a_database_refusal(
    safe: SafetyEnv, db: TestDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real = ExchangeFeed.refresh

    def flaky(self: ExchangeFeed) -> FeedOutcome:
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.errors.CheckViolation("refused")
        return real(self)

    monkeypatch.setattr(ExchangeFeed, "refresh", flaky)
    out: list[str] = []
    args = ["run", "--interval", "15", "--max-runs", "2"]
    assert cli(safe, db, args, out, reader=safe.fake, sleep=lambda _s: None) == 0
    assert "DATABASE_REFUSED (CheckViolation" in out[0] and out[1].startswith("feed: OK")


# ------------------------------------------------------------------ the web side
def test_the_page_says_so_when_the_feed_never_ran(
    admin_client: TestClient, viewer_client: TestClient
) -> None:
    for client in (admin_client, viewer_client):
        page = client.get("/coinbase")
        assert page.status_code == 200
        assert "No Coinbase data yet" in page.text and "NEVER" in page.text
        assert "No balance stored." in page.text


def test_the_page_shows_the_stored_account(
    safe: SafetyEnv, admin_client: TestClient, viewer_client: TestClient
) -> None:
    order_id = account(safe)
    feed(safe).refresh()
    for client in (admin_client, viewer_client):
        page = client.get("/coinbase")
        assert page.status_code == 200
        for text in ("FRESH", "REAL account", "USDC", "52.5", "0.001", order_id, "MAKER", "60000"):
            assert text in page.text, text
        assert "view yes, trade yes, transfer no" in page.text
        assert "<form" not in page.text.split('<main id="main">')[1]  # nothing to submit


def test_the_fragment_is_polled_with_get_and_marks_old_or_failed_data(
    safe: SafetyEnv, admin_client: TestClient
) -> None:
    account(safe)
    feed(safe).refresh()
    fragment = admin_client.get("/partials/coinbase")
    assert fragment.status_code == 200 and "<html" not in fragment.text
    assert 'hx-get="/partials/coinbase"' in fragment.text and "FRESH" in fragment.text
    safe.fake.fail("key_permissions", Fault("timeout"))
    feed(safe).refresh()
    failed = admin_client.get("/partials/coinbase").text
    assert "FAILED" in failed and "TIMEOUT" in failed and "52.5" in failed
    feed(safe).refresh()
    safe.clock.advance(301)
    assert "STALE" in admin_client.get("/partials/coinbase").text


def test_a_key_that_can_transfer_is_called_out(safe: SafetyEnv, admin_client: TestClient) -> None:
    safe.fake.permissions = KeyPermissions(True, True, True, "DEFAULT")
    feed(safe).refresh()
    assert "This API key can transfer funds" in admin_client.get("/coinbase").text


def test_the_overview_shows_the_real_usdc_balance(
    safe: SafetyEnv, admin_client: TestClient
) -> None:
    before = admin_client.get("/").text
    assert "Coinbase account data (REAL account)</dt><dd>None yet" in before
    account(safe)
    feed(safe).refresh()
    page = admin_client.get("/").text
    assert "Coinbase USDC available (REAL account)" in page and "Coinbase USDC on hold" in page


def test_the_routes_are_get_only_and_need_a_session(
    client: TestClient, admin_client: TestClient
) -> None:
    for path in ("/coinbase", "/partials/coinbase"):
        assert client.get(path, follow_redirects=False).status_code in (303, 401)
        assert admin_client.post(path, follow_redirects=False).status_code in (403, 405)


# ------------------------------------------------------------------ source-level boundaries
def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_web_side_imports_no_exchange_feed_or_network_code() -> None:
    for name in ("api/exchange.py", "web/exchange_views.py"):
        bad = [
            m
            for m in _imports(APP / name)
            if m.startswith(("app.exchange", "app.feed", "app.adapters", "httpx", "app.live"))
        ]
        assert not bad, (name, bad)


def test_the_feed_never_touches_the_order_gateway() -> None:
    for path in (APP / "feed").glob("*.py"):
        source = path.read_text()
        assert not [m for m in _imports(path) if "gateway" in m or "coinbase_live" in m], path
        for word in ("build_gateway", ".submit(", ".cancel(", "ExecutionGateway"):
            assert word not in source, (path, word)
