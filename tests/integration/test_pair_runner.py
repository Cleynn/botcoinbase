"""Discovery, seeding and validation through the host runner (real database, synthetic Coinbase)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import psycopg
import pytest

from app.adapters.coinbase_public import CoinbasePublicClient
from app.adapters.ratelimit import RateLimiter
from app.config import FeePolicy, Settings
from app.domain.pairs import PairAction, PairState
from app.pairs.runner import DISCOVERY_CAP, PairRunner, RunnerError
from app.storage.database import Storage
from tests.coinbase_fakes import product_json
from tests.conftest import FakeClock, with_fees

Sql = Callable[..., list[dict[str, Any]]]
PUBLIC_PATHS = {
    "/time",
    "/market/products",
    "/market/product_book",
}


def paths(coinbase: Any) -> list[str]:
    return [r.url.path.removeprefix("/api/v3/brokerage") for r in coinbase.requests]


def audit_rows(sql: Sql, code: str) -> list[dict[str, Any]]:
    return sql("SELECT * FROM audit_events WHERE event_code = %s ORDER BY seq", (code,))


# ------------------------------------------------------------------ discovery
def test_discovery_stores_usdc_spot_products_ranked_by_volume(env: Any, sql: Sql) -> None:
    summary = env.runner.discover()
    assert (summary.seen, summary.candidates, summary.stored, summary.new) == (3, 3, 3, 3)
    rows = sql("SELECT product_id, discovered_rank FROM products ORDER BY discovered_rank")
    assert [(r["product_id"], r["discovered_rank"]) for r in rows] == [
        ("BTC-USDC", 1),
        ("ETH-USDC", 2),
        ("SOL-USDC", 3),
    ]
    assert sql("SELECT count(*) AS n FROM product_metadata_snapshots")[0]["n"] == 3
    current = sql("SELECT server_time_offset_ms, last_verified_at FROM product_metadata_current")
    assert len(current) == 3 and all(r["server_time_offset_ms"] == 0 for r in current)


def test_discovery_only_uses_public_read_paths(env: Any, coinbase: Any) -> None:
    env.runner.discover()
    assert set(paths(coinbase)) == {"/time", "/market/products"}
    for request in coinbase.requests:
        assert request.method == "GET" and request.content == b""
        assert "authorization" not in {k.lower() for k in request.headers}


def test_discovery_is_audited_once_with_counts_and_no_product_content(env: Any, sql: Sql) -> None:
    env.runner.discover()
    events = audit_rows(sql, "product.discovered")
    assert len(events) == 1 and events[0]["actor_role"] == "HOST_CLI"
    detail = json.loads(events[0]["detail"])
    assert detail == {"seen": 3, "candidates": 3, "stored": 3, "new": 3, "changed": 0, "capped": 0}


def test_a_second_discovery_changes_nothing_but_the_verification_time(
    env: Any, clock: FakeClock, sql: Sql
) -> None:
    env.runner.discover()
    before = sql("SELECT last_verified_at FROM product_metadata_current ORDER BY product_uuid")
    clock.advance(600)
    summary = env.runner.discover()
    assert (summary.new, summary.changed) == (0, 0)
    assert sql("SELECT count(*) AS n FROM product_metadata_snapshots")[0]["n"] == 3
    after = sql("SELECT last_verified_at FROM product_metadata_current ORDER BY product_uuid")
    assert all(
        a["last_verified_at"] > b["last_verified_at"] for a, b in zip(after, before, strict=True)
    )


def test_changed_metadata_creates_a_new_snapshot_and_repoints_current(
    env: Any, coinbase: Any, sql: Sql
) -> None:
    env.runner.discover()
    coinbase.products["BTC-USDC"]["trading_disabled"] = True
    summary = env.runner.discover()
    assert summary.changed == 1
    snaps = sql(
        "SELECT s.trading_disabled FROM product_metadata_snapshots s "
        "JOIN products p ON p.id = s.product_uuid WHERE p.product_id = 'BTC-USDC' ORDER BY s.first_seen_at, s.id"
    )
    assert len(snaps) == 2
    current = sql(
        "SELECT s.trading_disabled FROM product_metadata_current c "
        "JOIN product_metadata_snapshots s ON s.id = c.snapshot_id "
        "JOIN products p ON p.id = c.product_uuid WHERE p.product_id = 'BTC-USDC'"
    )
    assert current == [{"trading_disabled": True}]


def test_only_usdc_spot_products_on_the_expected_venue_are_stored(
    env: Any, coinbase: Any, sql: Sql
) -> None:
    coinbase.products.update(
        {
            "ADA-USD": {
                **product_json("ADA-USDC"),
                "product_id": "ADA-USD",
                "quote_currency_id": "USD",
            },
            "FUT-USDC": product_json("FUT-USDC", product_type="FUTURE"),
            "INT-USDC": product_json("INT-USDC", product_venue="INTX"),
            "NOV-USDC": product_json("NOV-USDC", product_venue=""),
            "BAD": {**product_json("BAD-USDC"), "product_id": "bad-usdc"},
        }
    )
    summary = env.runner.discover()
    assert summary.stored == 3 and summary.candidates == 3
    assert {r["product_id"] for r in sql("SELECT product_id FROM products")} == {
        "BTC-USDC",
        "ETH-USDC",
        "SOL-USDC",
    }


def test_the_catalogue_is_capped_at_500_products_by_volume(
    env: Any, coinbase: Any, sql: Sql
) -> None:
    for i in range(520):
        code = f"T{i:03d}"
        coinbase.products[f"{code}-USDC"] = product_json(
            f"{code}-USDC", approximate_quote_24h_volume=str(10 + i)
        )
    summary = env.runner.discover()
    assert summary.stored == DISCOVERY_CAP and summary.capped == 523 - DISCOVERY_CAP
    assert sql("SELECT count(*) AS n FROM products")[0]["n"] == DISCOVERY_CAP
    assert sql("SELECT max(discovered_rank) AS m FROM products")[0]["m"] == DISCOVERY_CAP
    top = sql("SELECT product_id FROM products WHERE discovered_rank = 1")[0]["product_id"]
    assert top == "BTC-USDC"  # the highest volume


@pytest.mark.parametrize(
    ("suffix", "status", "raw", "code"),
    [
        ("/market/products", 500, None, "PRODUCTS_HTTP_ERROR"),
        ("/market/products", 429, None, "PRODUCTS_RATE_LIMITED"),
        ("/market/products", None, b"not json", "PRODUCTS_INVALID_JSON"),
        ("/market/products", None, b'{"products": 5}', "PRODUCTS_UNEXPECTED_SHAPE"),
        ("/time", 500, None, "TIME_HTTP_ERROR"),
        ("/time", None, b'{"epochSeconds": "abc"}', "TIME_BAD_SERVER_TIME"),
    ],
)
def test_a_failed_discovery_stores_nothing_and_says_why(
    env: Any, coinbase: Any, sql: Sql, suffix: str, status: int | None, raw: bytes | None, code: str
) -> None:
    if status:
        coinbase.errors[suffix] = status
    if raw is not None:
        coinbase.raw[suffix] = raw
    with pytest.raises(RunnerError) as err:
        env.runner.discover()
    assert err.value.code == code
    assert sql("SELECT count(*) AS n FROM products")[0]["n"] == 0
    assert audit_rows(sql, "product.discovered") == []


def test_the_web_role_cannot_run_discovery(
    settings: Settings, storage: Storage, clock: FakeClock, coinbase: Any
) -> None:
    web_runner = PairRunner(
        storage=storage, clock=clock, settings=settings, client=coinbase.client()
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        web_runner.discover()


# ------------------------------------------------------------------ seeding
def test_seeding_before_discovery_proposes_nothing(env: Any, sql: Sql) -> None:
    results = env.runner.seed_watchlist()
    assert [r.outcome for r in results] == ["NOT_DISCOVERED"] * 3
    assert sql("SELECT count(*) AS n FROM pairs")[0]["n"] == 0


def test_seeding_creates_the_three_initial_candidates_as_proposed_only(env: Any, sql: Sql) -> None:
    env.discover()
    results = env.runner.seed_watchlist()
    assert [(r.product_id, r.outcome) for r in results] == [
        ("BTC-USDC", "PROPOSED"),
        ("ETH-USDC", "PROPOSED"),
        ("SOL-USDC", "PROPOSED"),
    ]
    rows = sql("SELECT state, version, proposed_via, proposed_by, ever_active FROM pairs")
    assert len(rows) == 3
    assert {(r["state"], r["version"], r["proposed_via"], r["ever_active"]) for r in rows} == {
        ("PROPOSED", 1, "HOST", False)
    }
    assert all(r["proposed_by"] is None for r in rows)
    events = audit_rows(sql, "pair.proposed")
    assert len(events) == 3 and {e["actor_role"] for e in events} == {"HOST_CLI"}
    assert sql("SELECT count(*) AS n FROM pair_validation_runs")[0]["n"] == 0


def test_seeding_is_idempotent_and_never_creates_a_duplicate(env: Any, sql: Sql) -> None:
    env.discover()
    env.runner.seed_watchlist()
    again = env.runner.seed_watchlist()
    assert [r.outcome for r in again] == ["ALREADY_A_CANDIDATE"] * 3
    assert sql("SELECT count(*) AS n FROM pairs")[0]["n"] == 3
    assert len(audit_rows(sql, "pair.transition_denied")) == 3


def test_seeding_can_queue_validation_but_never_more(env: Any, sql: Sql) -> None:
    env.discover()
    env.runner.seed_watchlist(queue_validation=True)
    assert {r["state"] for r in sql("SELECT state FROM pairs")} == {"VALIDATING"}


# ------------------------------------------------------------------ validation
def test_a_healthy_pair_becomes_paper_eligible_with_full_evidence(
    env: Any, sql: Sql, coinbase: Any
) -> None:
    pair_id = env.eligible("BTC-USDC")
    pair = env.get(pair_id)
    assert pair.state is PairState.PAPER_ELIGIBLE and pair.eligible_run_id is not None
    run = sql("SELECT * FROM pair_validation_runs")[0]
    assert run["outcome"] == "PASS" and run["id"] == pair.eligible_run_id
    checks = run["checks"]
    assert [c["code"] for c in checks][0] == "PRODUCT_STATUS" and len(checks) == 14
    assert {c["status"] for c in checks} == {"PASS"}
    assert run["expires_at"] - run["finished_at"] == timedelta(hours=24)
    assert len(run["thresholds_sha256"]) == 64 and run["pair_version"] == 2
    snapshot = sql("SELECT snapshot_id FROM product_metadata_current")
    assert run["snapshot_id"] in {r["snapshot_id"] for r in snapshot}
    assert set(paths(coinbase)) <= PUBLIC_PATHS | {
        "/market/products/BTC-USDC",
        "/market/products/BTC-USDC/candles",
    }


def test_validation_records_every_step_in_history_and_audit(env: Any, sql: Sql) -> None:
    pair_id = env.eligible("BTC-USDC")
    history = sql(
        "SELECT version_after, state_before, state_after, actor_class, transition_no, reason_code, audit_seq "
        "FROM pair_state_history WHERE pair_id = %s ORDER BY version_after",
        (pair_id,),
    )
    assert [
        (h["version_after"], h["state_before"], h["state_after"], h["actor_class"]) for h in history
    ] == [
        (1, None, "PROPOSED", "WEB"),
        (2, "PROPOSED", "VALIDATING", "WEB"),
        (3, "VALIDATING", "PAPER_ELIGIBLE", "HOST"),
    ]
    assert history[2]["reason_code"] == "VALIDATION_PASS" and history[2]["transition_no"] == 4
    seqs = [h["audit_seq"] for h in history]
    assert all(seqs) and seqs == sorted(seqs)
    eligible = audit_rows(sql, "pair.paper_eligible")
    assert len(eligible) == 1 and eligible[0]["target_id"] == str(pair_id)
    detail = json.loads(eligible[0]["detail"])
    assert (
        detail["outcome"] == "PASS"
        and detail["from"] == "VALIDATING"
        and detail["to"] == "PAPER_ELIGIBLE"
    )


def test_validation_never_activates_a_pair(env: Any, sql: Sql) -> None:
    env.eligible("BTC-USDC")
    env.eligible("ETH-USDC")
    assert sql("SELECT count(*) AS n FROM pairs WHERE state = 'PAPER_ACTIVE'")[0]["n"] == 0
    assert audit_rows(sql, "pair.activated_paper") == []


def test_a_failing_check_sends_the_pair_to_research_only_with_explicit_reasons(
    env: Any, coinbase: Any, sql: Sql
) -> None:
    coinbase.products["BTC-USDC"]["status"] = "offline"
    coinbase.products["BTC-USDC"]["trading_disabled"] = True
    env.discover()
    pair_id = env.propose("BTC-USDC")
    env.queue(pair_id)
    report = env.validate()[0]
    assert report.result == "RESEARCH_ONLY" and report.outcome == "FAIL"
    assert "PRODUCT_STATUS" in report.failed
    run = sql("SELECT checks, outcome FROM pair_validation_runs")[0]
    status = next(c for c in run["checks"] if c["code"] == "PRODUCT_STATUS")
    assert status["status"] == "FAIL" and status["reason"] == "STATUS_NOT_ALLOWED"
    assert "STATUS_NOT_ALLOWED,FLAG_TRADING_DISABLED" in status["observed"]["failures"]
    assert status["message"].endswith(".")
    assert env.get(pair_id).eligible_run_id is None
    events = audit_rows(sql, "pair.research_only")
    assert json.loads(events[0]["detail"])["failed"] == "PRODUCT_STATUS"


def test_unattested_fees_make_the_result_inconclusive_and_research_only(
    settings: Settings,
    ctl_storage: Storage,
    clock: FakeClock,
    coinbase: Any,
    env: Any,
    sql: Sql,
) -> None:
    unattested = with_fees(settings, FeePolicy())
    runner = PairRunner(
        storage=ctl_storage, clock=clock, settings=unattested, client=coinbase.client()
    )
    env.discover()
    pair_id = env.propose("BTC-USDC")
    env.queue(pair_id)
    report = runner.validate_pending()[0]
    assert report.outcome == "INCONCLUSIVE" and report.result == "RESEARCH_ONLY"
    assert report.inconclusive == ("FEE_VIABILITY",)
    run = sql("SELECT checks FROM pair_validation_runs")[0]
    fee = next(c for c in run["checks"] if c["code"] == "FEE_VIABILITY")
    assert fee["reason"] == "FEE_NOT_ATTESTED"


@pytest.mark.parametrize(
    ("mutate", "code", "reason"),
    [
        (lambda c: setattr(c, "drop_daily", 20), "HISTORY_AVAILABILITY", "HISTORY_TOO_SHORT"),
        (lambda c: setattr(c, "intraday_gap", 60), "OHLCV_QUALITY", "TOO_MANY_GAPS"),
        (
            lambda c: c.products["BTC-USDC"].update(post_only=True),
            "PRODUCT_STATUS",
            "FLAG_POST_ONLY",
        ),
        (
            lambda c: c.products["BTC-USDC"].update(price_increment="1000"),
            "PRECISION",
            "TICK_TOO_COARSE",
        ),
        (
            lambda c: c.products["BTC-USDC"].pop("price_increment"),
            "INCREMENTS",
            "PRICE_INCREMENT_MISSING",
        ),
        (lambda c: c.products["BTC-USDC"].update(alias=""), "DATA_BASIS", "DATA_BASIS_UNKNOWN"),
    ],
)
def test_each_failure_mode_is_persisted_with_its_reason(
    env: Any, coinbase: Any, sql: Sql, mutate: Callable[[Any], None], code: str, reason: str
) -> None:
    env.discover()
    pair_id = env.propose("BTC-USDC")
    mutate(coinbase)
    env.queue(pair_id)
    env.validate()
    assert env.state(pair_id) is PairState.RESEARCH_ONLY
    run = sql("SELECT checks FROM pair_validation_runs")[0]
    check = next(c for c in run["checks"] if c["code"] == code)
    assert check["status"] != "PASS" and check["reason"] == reason


@pytest.mark.parametrize(
    ("suffix", "code"),
    [
        ("/candles", "OHLCV_QUALITY"),
        ("/product_book", "LIQUIDITY_SPREAD"),
        ("/time", "CLOCK_SYNC"),
    ],
)
def test_upstream_failures_are_inconclusive_never_a_pass(
    env: Any, coinbase: Any, sql: Sql, suffix: str, code: str
) -> None:
    env.discover()
    pair_id = env.propose("BTC-USDC")
    env.queue(pair_id)
    coinbase.errors[suffix] = 503
    env.validate()
    assert env.state(pair_id) is PairState.RESEARCH_ONLY
    run = sql("SELECT checks, outcome FROM pair_validation_runs")[0]
    assert run["outcome"] == "INCONCLUSIVE"
    check = next(c for c in run["checks"] if c["code"] == code)
    assert check["status"] == "INCONCLUSIVE" and check["reason"] == "FETCH_FAILED"
    assert check["observed"]["error"] == "HTTP_ERROR"


def test_a_failed_metadata_refresh_is_reported_and_stored_metadata_is_not_trusted_as_fresh(
    env: Any, coinbase: Any, sql: Sql
) -> None:
    env.discover()
    pair_id = env.propose("BTC-USDC")
    env.queue(pair_id)
    coinbase.errors["/market/products/BTC-USDC"] = 500
    env.validate()
    run = sql("SELECT checks FROM pair_validation_runs")[0]
    fresh = next(c for c in run["checks"] if c["code"] == "METADATA_FRESHNESS")
    assert fresh["status"] == "INCONCLUSIVE" and fresh["reason"] == "FETCH_FAILED"
    assert env.state(pair_id) is PairState.RESEARCH_ONLY


def test_hostile_exchange_content_never_reaches_stored_evidence(
    env: Any, coinbase: Any, sql: Sql
) -> None:
    coinbase.products["BTC-USDC"].update(
        status="<script>alert(1)</script>",
        alias="<img src=x onerror=alert(1)>",
        display_name="<b>x</b>",
    )
    env.discover()
    pair_id = env.propose("BTC-USDC")
    env.queue(pair_id)
    env.validate()
    dump = json.dumps(sql("SELECT checks FROM pair_validation_runs")[0]["checks"], default=str)
    dump += json.dumps(sql("SELECT * FROM product_metadata_snapshots"), default=str)
    dump += json.dumps(sql("SELECT detail FROM audit_events"), default=str)
    assert "<script>" not in dump and "onerror" not in dump and "<b>" not in dump
    assert env.state(pair_id) is PairState.RESEARCH_ONLY


def test_revalidation_appends_a_new_run_and_keeps_the_old_one(
    env: Any, coinbase: Any, sql: Sql
) -> None:
    coinbase.drop_daily = 30
    env.discover()
    pair_id = env.propose("BTC-USDC")
    env.queue(pair_id)
    env.validate()
    assert env.state(pair_id) is PairState.RESEARCH_ONLY
    coinbase.drop_daily = 0
    assert env.queue(pair_id).kind == "ok"
    env.validate()
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE
    runs = sql("SELECT outcome FROM pair_validation_runs ORDER BY started_at, finished_at, id")
    assert sorted(r["outcome"] for r in runs) == ["FAIL", "PASS"]


def test_validate_pending_only_touches_pairs_in_validating(env: Any) -> None:
    env.discover()
    env.propose("BTC-USDC")  # PROPOSED, not queued
    assert env.validate() == []


def test_validate_pending_can_be_limited_to_one_pair(env: Any) -> None:
    env.discover()
    first, second = env.propose("BTC-USDC"), env.propose("ETH-USDC")
    env.queue(first)
    env.queue(second)
    reports = env.runner.validate_pending(only=second)
    assert [r.pair_id for r in reports] == [second]
    assert (
        env.state(first) is PairState.VALIDATING and env.state(second) is PairState.PAPER_ELIGIBLE
    )


def test_a_pair_archived_during_validation_is_left_alone_and_no_run_is_stored(
    env: Any,
    coinbase: Any,
    settings: Settings,
    ctl_storage: Storage,
    clock: FakeClock,
    sql: Sql,
) -> None:
    env.discover()
    pair_id = env.propose("BTC-USDC")
    env.queue(pair_id)
    fired = {"done": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/product_book") and not fired["done"]:
            fired["done"] = True
            env.act(pair_id, PairAction.ARCHIVE)  # an ADMIN archives it mid-validation
        response: httpx.Response = coinbase.handle(request)
        return response

    racing = PairRunner(
        storage=ctl_storage,
        clock=clock,
        settings=settings,
        client=CoinbasePublicClient(
            settings.exchange, transport=httpx.MockTransport(handler), limiter=RateLimiter(10_000)
        ),
    )
    reports = racing.validate_pending()
    assert fired["done"] and reports[0].result == "SKIPPED"
    assert env.state(pair_id) is PairState.ARCHIVED
    assert sql("SELECT count(*) AS n FROM pair_validation_runs")[0]["n"] == 0


def test_the_ttl_comes_from_policy(
    settings: Settings,
    ctl_storage: Storage,
    clock: FakeClock,
    coinbase: Any,
    env: Any,
    sql: Sql,
) -> None:
    short = settings.pair_policy.validation.model_copy(update={"ttl_hours": 2})
    policy = settings.pair_policy.model_copy(update={"validation": short})
    runner = PairRunner(
        storage=ctl_storage,
        clock=clock,
        settings=settings.model_copy(update={"pair_policy": policy}),
        client=coinbase.client(),
    )
    env.discover()
    pair_id = env.propose("BTC-USDC")
    env.queue(pair_id)
    runner.validate_pending()
    run = sql("SELECT finished_at, expires_at FROM pair_validation_runs")[0]
    assert run["expires_at"] - run["finished_at"] == timedelta(hours=2)
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE


# ------------------------------------------------------------------ expiry
def test_eligibility_expires_after_the_ttl(env: Any, clock: FakeClock, sql: Sql) -> None:
    pair_id = env.eligible("BTC-USDC")
    assert env.runner.expire_eligibility() == 0
    clock.advance(24 * 3600 - 1)
    assert env.runner.expire_eligibility() == 0
    clock.advance(2)
    assert env.runner.expire_eligibility() == 1
    assert env.state(pair_id) is PairState.VALIDATING
    events = audit_rows(sql, "pair.eligibility_expired")
    assert len(events) == 1 and events[0]["reason_code"] == "VALIDATION_EXPIRED"
    assert events[0]["actor_role"] == "HOST_CLI"


def test_changed_metadata_expires_eligibility_immediately(
    env: Any, coinbase: Any, sql: Sql
) -> None:
    pair_id = env.eligible("BTC-USDC")
    coinbase.products["BTC-USDC"]["limit_only"] = True
    env.runner.discover()
    assert env.runner.expire_eligibility() == 1
    assert env.state(pair_id) is PairState.VALIDATING
    assert audit_rows(sql, "pair.eligibility_expired")[0]["reason_code"] == "METADATA_CHANGED"


def test_an_expired_pair_can_be_revalidated_back_to_eligible(env: Any, clock: FakeClock) -> None:
    pair_id = env.eligible("BTC-USDC")
    clock.advance(25 * 3600)
    env.runner.expire_eligibility()
    env.validate()
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE


def test_expiry_leaves_other_states_alone(env: Any, clock: FakeClock) -> None:
    proposed = env.make("BTC-USDC", PairState.PROPOSED)
    disabled = env.make("ETH-USDC", PairState.DISABLED)
    clock.advance(48 * 3600)
    assert env.runner.expire_eligibility() == 0
    assert env.state(proposed) is PairState.PROPOSED and env.state(disabled) is PairState.DISABLED
