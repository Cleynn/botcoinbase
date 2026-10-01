"""Choosing BACKTEST, PAPER or LIVE: the ControlService flow, the database guard and the Bot page.
Choosing a mode never places an order and never arms live trading. SYNTHETIC data."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.safety import live_gate
from app.safety.control import MODE_PHRASES, PHRASES
from tests.conftest import GOOD_PASSWORD, ORIGIN, Account, csrf_from
from tests.integration.safety_env import SafetyEnv

Sql = Callable[..., list[dict[str, Any]]]


def events(sql: Sql) -> list[str]:
    return [
        r["event_code"]
        for r in sql(
            "SELECT event_code FROM audit_events WHERE event_code LIKE 'bot.%' ORDER BY seq"
        )
    ]


def active_mode(sql: Sql) -> str:
    return str(sql("SELECT active_mode FROM trading_state")[0]["active_mode"])


def test_the_three_phrases_are_the_specified_ones() -> None:
    assert MODE_PHRASES == {
        "backtest": "SWITCH TO BACKTEST MODE",
        "paper": "SWITCH TO PAPER MODE",
        "live": "SWITCH TO LIVE MODE",
    }
    assert not set(MODE_PHRASES.values()) & set(PHRASES.values())


@pytest.mark.parametrize("mode", ["backtest", "live", "paper"])
def test_an_admin_switches_the_mode_with_phrase_and_reauth_and_it_is_audited(
    safe: SafetyEnv, sql: Sql, mode: str
) -> None:
    if mode == "paper":  # PAPER is the default: switch away first
        safe.reauth.available = True
        assert (
            safe.control.switch_mode(
                safe.ctx, safe.actor, "backtest", MODE_PHRASES["backtest"]
            ).kind
            == "ok"
        )
        safe.clock.advance(1)
    safe.reauth.available = True
    out = safe.control.switch_mode(safe.ctx, safe.actor, mode, MODE_PHRASES[mode])
    assert out.kind == "ok", out
    assert active_mode(sql) == mode.upper()
    assert events(sql)[-2:] == ["bot.control_requested", "bot.mode_switched"]
    raw = sql(
        "SELECT detail FROM audit_events WHERE event_code = 'bot.mode_switched' ORDER BY seq DESC"
    )[0]
    detail = raw["detail"] if isinstance(raw["detail"], dict) else json.loads(raw["detail"])
    assert detail["to"] == mode.upper() and detail["live_armed"] is False


def test_switching_to_live_never_arms_live_trading(safe: SafetyEnv, sql: Sql) -> None:
    safe.reauth.available = True
    assert safe.control.switch_mode(safe.ctx, safe.actor, "live", MODE_PHRASES["live"]).kind == "ok"
    now = safe.clock.now()
    assert sql("SELECT td_live_armed(%s) AS armed", (now,))[0]["armed"] is False


def test_a_wrong_phrase_or_unknown_mode_spends_no_reauth(safe: SafetyEnv, sql: Sql) -> None:
    safe.reauth.available = True
    assert (
        safe.control.switch_mode(safe.ctx, safe.actor, "live", "SWITCH TO PAPER MODE").kind
        == "phrase_mismatch"
    )
    assert safe.control.switch_mode(safe.ctx, safe.actor, "margin", "x").kind == "invalid"
    assert safe.reauth.consumed == 0 and active_mode(sql) == "PAPER"


def test_without_a_fresh_reauth_nothing_changes(safe: SafetyEnv, sql: Sql) -> None:
    safe.reauth.available = False
    out = safe.control.switch_mode(safe.ctx, safe.actor, "live", MODE_PHRASES["live"])
    assert out.kind == "reauth_required" and active_mode(sql) == "PAPER"


def test_the_same_mode_is_refused(safe: SafetyEnv) -> None:
    safe.reauth.available = True
    out = safe.control.switch_mode(safe.ctx, safe.actor, "paper", MODE_PHRASES["paper"])
    assert out.kind == "not_allowed" and out.reasons == ("MODE_UNCHANGED",)


def test_the_mode_cannot_change_while_the_bot_runs(safe: SafetyEnv, sql: Sql) -> None:
    safe.baseline("50")
    assert safe.recover().complete
    assert safe.act("resume").kind == "ok"
    safe.reauth.available = True
    out = safe.control.switch_mode(safe.ctx, safe.actor, "live", MODE_PHRASES["live"])
    assert out.kind == "not_allowed" and out.reasons == ("BOT_MUST_BE_PAUSED",)
    assert active_mode(sql) == "PAPER"


# ------------------------------------------------------------------ the Bot page
@pytest.fixture
def sapp(safe: SafetyEnv, storage: Any, clock: Any) -> Any:
    return create_app(safe.settings, storage=storage, clock=clock)


def _client(app: Any, login: Callable[..., Any], account: Account, peer: str) -> TestClient:
    client = TestClient(
        app,
        base_url="https://testserver",
        raise_server_exceptions=False,
        client=(peer, 50000),
        headers={"origin": ORIGIN},
    )
    assert login(client, account.username, account.password).status_code == 303
    return client


@pytest.fixture
def ac(sapp: Any, login: Callable[..., Any], admin: Account) -> TestClient:
    return _client(sapp, login, admin, "10.0.0.5")


@pytest.fixture
def vc(sapp: Any, login: Callable[..., Any], viewer: Account) -> TestClient:
    return _client(sapp, login, viewer, "10.0.0.6")


def _post(client: TestClient, mode: str, step: str, **fields: str) -> Any:
    token = csrf_from(client.get(f"/bot/mode/{mode}/request").text)
    return client.post(
        f"/bot/mode/{mode}/{step}", data={"csrf_token": token, **fields}, follow_redirects=False
    )


def test_the_page_shows_the_mode_buttons_to_an_admin_only(ac: TestClient, vc: TestClient) -> None:
    admin_page = ac.get("/bot").text
    assert "Trading mode" in admin_page and "/bot/mode/live/request" in admin_page
    assert "/bot/mode/backtest/request" in admin_page
    assert "/bot/mode/paper/request" not in admin_page  # PAPER is the active mode
    viewer_page = vc.get("/bot").text
    assert "Trading mode" in viewer_page and "/bot/mode/live/request" not in viewer_page


def test_the_full_flow_switches_the_mode(ac: TestClient, sql: Sql) -> None:
    assert _post(ac, "live", "reauth", password=GOOD_PASSWORD).status_code == 303
    resp = _post(ac, "live", "confirm", confirmation=MODE_PHRASES["live"])
    assert resp.status_code == 303 and resp.headers["location"] == "/bot?msg=mode_live"
    assert active_mode(sql) == "LIVE"
    page = ac.get(resp.headers["location"]).text
    assert "Mode set to LIVE" in page and "Active mode: <strong>LIVE</strong>" in page
    assert live_gate.panel().status == "BLOCKED"  # the build's static gate text is unchanged
    assert events(sql)[-1] == "bot.mode_switched"


def test_a_wrong_phrase_changes_nothing(ac: TestClient, sql: Sql) -> None:
    _post(ac, "live", "reauth", password=GOOD_PASSWORD)
    resp = _post(ac, "live", "confirm", confirmation="switch to live mode")
    assert resp.status_code == 400 and active_mode(sql) == "PAPER"


def test_a_viewer_cannot_reach_the_mode_routes(vc: TestClient) -> None:
    assert vc.get("/bot/mode/live/request").status_code == 403
    resp = vc.post(
        "/bot/mode/live/confirm",
        data={"csrf_token": "x", "confirmation": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_a_post_without_csrf_changes_nothing(ac: TestClient, sql: Sql) -> None:
    resp = ac.post(
        "/bot/mode/live/confirm",
        data={"confirmation": MODE_PHRASES["live"]},
        follow_redirects=False,
    )
    assert resp.status_code in (400, 403, 422) and active_mode(sql) == "PAPER"


def test_an_unknown_mode_is_not_a_route(ac: TestClient) -> None:
    assert ac.get("/bot/mode/margin/request").status_code in (400, 404, 422)


# ------------------------------------------------------------------ editing the configuration
from decimal import Decimal  # noqa: E402

from app.capital.trading import TradingConfig  # noqa: E402
from app.safety.control import CONFIG_PHRASES  # noqa: E402


def cfg(mode: str = "LIVE", **kw: Any) -> TradingConfig:
    base = {
        "max_pairs": 3,
        "levels_per_grid": 6,
        "quote_per_grid": Decimal("20"),
        "invested_cap": Decimal("60"),
        "reserve": Decimal("20"),
        "per_order_cap": Decimal("10"),
    }
    base.update(kw)
    return TradingConfig(mode, **base)  # type: ignore[arg-type]


def stored(sql: Sql, mode: str) -> dict[str, Any]:
    return sql("SELECT * FROM trading_config WHERE mode = %s", (mode,))[0]


def chosen(sql: Sql, mode: str) -> list[str]:
    rows = sql(
        "SELECT pr.product_id FROM trading_pairs t JOIN pairs p ON p.id = t.pair_id "
        "JOIN products pr ON pr.id = p.product_uuid WHERE t.mode = %s ORDER BY 1",
        (mode,),
    )
    return [r["product_id"] for r in rows]


# ---------------------------------------------------------------- choosing the pairs
def choose(safe: SafetyEnv, pairs: list[str], **kw: Any) -> Any:
    safe.reauth.available = True
    return safe.control.set_trading_config(
        safe.ctx, safe.actor, "live", cfg(**kw), CONFIG_PHRASES["live"], selection=pairs
    )


def test_choosing_pairs_stores_them_and_the_count_follows(safe: SafetyEnv, sql: Sql) -> None:
    assert chosen(sql, "LIVE") == []  # nothing is chosen until the operator chooses
    assert choose(safe, ["BTC-USDC", "BTC-USDC"]).kind == "ok"
    assert chosen(sql, "LIVE") == ["BTC-USDC"]
    row = stored(sql, "LIVE")
    assert (row["max_pairs"], row["version"]) == (1, 2)  # not the 3 of the proposal
    detail = sql("SELECT detail FROM audit_events ORDER BY seq DESC LIMIT 1")[0]["detail"]
    assert '"selected":"BTC-USDC"' in str(detail).replace(" ", "").replace("'", '"')
    assert safe.control.trading_selection("live") == (
        ("BTC-USDC",),
        (("BTC-USDC", "PAPER_ACTIVE"),),
    )


def test_a_change_of_the_chosen_pairs_alone_is_a_new_version(safe: SafetyEnv, sql: Sql) -> None:
    assert choose(safe, ["BTC-USDC"]).kind == "ok"
    safe.clock.advance(1)
    same = choose(safe, ["BTC-USDC"])
    assert same.kind == "not_allowed" and same.reasons == ("CONFIG_UNCHANGED",)
    safe.clock.advance(1)
    assert choose(safe, []).kind == "ok"  # same numbers, no pair: a live arming ends with it
    assert chosen(sql, "LIVE") == [] and stored(sql, "LIVE")["version"] == 3
    assert stored(sql, "LIVE")["max_pairs"] == 1  # never below one


def test_an_unknown_or_unvalidated_pair_is_refused(safe: SafetyEnv, sql: Sql) -> None:
    out = choose(safe, ["DOGE-USDC"])
    assert out.kind == "not_allowed" and out.reasons == ("PAIR_NOT_SELECTABLE",)
    assert chosen(sql, "LIVE") == [] and stored(sql, "LIVE")["version"] == 1
    many = choose(safe, [f"C{i}-USDC" for i in range(11)])
    assert many.kind == "invalid" and many.reasons == ("MAX_PAIRS",)


def test_the_caps_are_checked_for_the_number_of_chosen_pairs(safe: SafetyEnv) -> None:
    # one chosen pair at 50 per grid fits an invested cap of 60; the proposal's own count is ignored
    assert choose(safe, ["BTC-USDC"], quote_per_grid=Decimal("50")).kind == "ok"


def test_the_database_guards_the_selection(safe: SafetyEnv, sql: Sql) -> None:
    import psycopg

    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):  # an unknown role
        sql("INSERT INTO trading_pairs (mode, pair_id) VALUES ('LIVE', %s)", (safe.pair_id,))
    safe.baseline("50")
    assert safe.recover().complete
    assert safe.act("resume").kind == "ok"
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation), safe.ctl.tx() as repos:
        repos.safety.set_trading_pairs("LIVE", [safe.pair_id])  # the bot is RUNNING
    assert chosen(sql, "LIVE") == []


def test_an_admin_edits_the_live_configuration_and_it_is_audited(safe: SafetyEnv, sql: Sql) -> None:
    safe.reauth.available = True
    out = safe.control.set_trading_config(
        safe.ctx, safe.actor, "live", cfg(), CONFIG_PHRASES["live"]
    )
    assert out.kind == "ok", out
    row = stored(sql, "LIVE")
    assert (row["max_pairs"], row["levels_per_grid"], row["invested_cap"]) == (3, 6, Decimal(60))
    assert stored(sql, "PAPER")["max_pairs"] == 1  # the other mode is untouched
    assert events(sql)[-2:] == ["bot.control_requested", "bot.config_changed"]


def test_an_invalid_proposal_spends_no_reauth_and_changes_nothing(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.reauth.available = True
    for bad, code in (
        (cfg(max_pairs=11), "MAX_PAIRS"),
        (cfg(levels_per_grid=2), "LEVELS"),
        (cfg(quote_per_grid=Decimal("30")), "GRIDS_EXCEED_INVESTED_CAP"),
        (cfg(per_order_cap=Decimal("25")), "ORDER_EXCEEDS_GRID"),
        (cfg(reserve=Decimal("5")), "RESERVE_BELOW_FLOOR"),
    ):
        out = safe.control.set_trading_config(
            safe.ctx, safe.actor, "live", bad, CONFIG_PHRASES["live"]
        )
        assert out.kind == "invalid" and code in out.reasons, (code, out)
    assert safe.reauth.consumed == 0 and stored(sql, "LIVE")["max_pairs"] == 1


def test_a_config_change_needs_the_bot_paused(safe: SafetyEnv, sql: Sql) -> None:
    safe.baseline("50")
    assert safe.recover().complete
    assert safe.act("resume").kind == "ok"
    safe.reauth.available = True
    out = safe.control.set_trading_config(
        safe.ctx, safe.actor, "live", cfg(), CONFIG_PHRASES["live"]
    )
    assert out.kind == "not_allowed" and out.reasons == ("BOT_MUST_BE_PAUSED",)
    assert stored(sql, "LIVE")["max_pairs"] == 1


def test_the_edit_form_and_the_full_flow(ac: TestClient, sql: Sql) -> None:
    form = ac.get("/bot/trading/live/edit").text
    assert "Pairs chosen for LIVE mode" in form and "Pairs traded in parallel" not in form
    assert "only the chosen pairs are traded" in form
    paper = ac.get("/bot/trading/paper/edit").text  # the paper trader does not follow the choice
    assert "still trades the one active pair" in paper
    assert "only the chosen pairs are traded" not in paper
    assert 'name="pick" type="checkbox" value="BTC-USDC"' in form and "checked" not in form
    q = "pick=BTC-USDC&levels=6&per_grid=20&invested=60&reserve=20&per_order=10"
    page = ac.get(f"/bot/trading/live/request?{q}")
    assert page.status_code == 200 and "pairs chosen: none -&gt; BTC-USDC" in page.text
    assert page.text.count('name="pick" value="BTC-USDC"') == 2  # carried by both forms
    token = csrf_from(page.text)
    fields = {
        "csrf_token": token, "pick": "BTC-USDC", "levels": "6", "per_grid": "20",
        "invested": "60", "reserve": "20", "per_order": "10",
    }  # fmt: skip
    assert (
        ac.post(
            "/bot/trading/live/reauth",
            data={**fields, "password": GOOD_PASSWORD},
            follow_redirects=False,
        ).status_code
        == 303
    )
    resp = ac.post(
        "/bot/trading/live/confirm",
        data={**fields, "confirmation": CONFIG_PHRASES["live"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/bot?msg=config_live"
    assert stored(sql, "LIVE")["max_pairs"] == 1  # the count is the number of chosen pairs
    assert chosen(sql, "LIVE") == ["BTC-USDC"] and chosen(sql, "PAPER") == []
    bot = ac.get("/bot?msg=config_live").text
    assert "LIVE trading configuration was updated" in bot and "BTC-USDC (active)" in bot
    assert "checked" in ac.get("/bot/trading/live/edit").text


def test_a_pair_that_is_not_validated_cannot_be_chosen(ac: TestClient, sql: Sql) -> None:
    q = "pick=DOGE-USDC&levels=6&per_grid=20&invested=60&reserve=20&per_order=10"
    resp = ac.get(f"/bot/trading/live/request?{q}", follow_redirects=False)
    assert resp.status_code == 303 and "err=PAIR_NOT_SELECTABLE" in resp.headers["location"]
    assert "not a validated pair" in ac.get(resp.headers["location"]).text
    bad = ac.get("/bot/trading/live/request?pick=<script>&levels=6&per_grid=20&invested=60"
                 "&reserve=20&per_order=10")  # fmt: skip
    assert bad.status_code == 400 and chosen(sql, "LIVE") == []


def test_an_invalid_query_goes_back_to_the_form(ac: TestClient) -> None:
    resp = ac.get(
        "/bot/trading/live/request?pairs=3&levels=6&per_grid=99&invested=60&reserve=20&per_order=10",
        follow_redirects=False,
    )
    assert resp.status_code == 303 and "err=GRIDS_EXCEED_INVESTED_CAP" in resp.headers["location"]


def test_a_viewer_cannot_edit_the_configuration(vc: TestClient) -> None:
    assert vc.get("/bot/trading/live/edit").status_code == 403
