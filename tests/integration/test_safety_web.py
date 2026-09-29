"""The Bot page and its four confirmed controls over HTTP: authorization, CSRF, fresh reauth, typed
phrase, audit, the internal command and its outcome. The dashboard can never create an order."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.safety.control import PHRASES
from tests.conftest import GOOD_PASSWORD, ORIGIN, Account, csrf_from
from tests.integration.safety_env import BOT_TABLES, SafetyEnv

Sql = Callable[..., list[dict[str, Any]]]
SLUGS = {"pause": "pause", "resume": "resume", "cancel_known": "cancel-known", "kill": "kill"}


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


def token(client: TestClient, slug: str = "pause") -> str:
    return csrf_from(client.get(f"/bot/{slug}/request").text)


def reauth(client: TestClient, slug: str, pw: str = GOOD_PASSWORD) -> Any:
    return client.post(
        f"/bot/{slug}/reauth",
        data={"csrf_token": token(client, slug), "password": pw},
        follow_redirects=False,
    )


def confirm(client: TestClient, slug: str, phrase: str) -> Any:
    return client.post(
        f"/bot/{slug}/confirm",
        data={"csrf_token": token(client, slug), "confirmation": phrase},
        follow_redirects=False,
    )


def do(client: TestClient, action: str, *, phrase: str | None = None, fresh: bool = True) -> Any:
    slug = SLUGS[action]
    if fresh:
        assert reauth(client, slug).status_code == 303
    return confirm(client, slug, PHRASES[action] if phrase is None else phrase)


def fingerprint(sql: Sql) -> list[Any]:
    return [
        sql(
            f"SELECT count(*) AS n, md5(string_agg(x::text, ',' ORDER BY x::text)) AS h FROM {t} x"
        )[0]
        for t in BOT_TABLES
    ]  # noqa: S608


ROUTES = (
    ("GET", "/bot/pause/request"),
    ("GET", "/bot/resume/request"),
    ("GET", "/bot/cancel-known/request"),
    ("GET", "/bot/kill/request"),
    ("POST", "/bot/pause/reauth"),
    ("POST", "/bot/pause/confirm"),
    ("POST", "/bot/resume/reauth"),
    ("POST", "/bot/resume/confirm"),
    ("POST", "/bot/cancel-known/reauth"),
    ("POST", "/bot/cancel-known/confirm"),
    ("POST", "/bot/kill/reauth"),
    ("POST", "/bot/kill/confirm"),
)


# ------------------------------------------------------------------ access
def test_the_page_is_readable_by_viewers_but_offers_them_no_control(
    vc: TestClient, ac: TestClient
) -> None:
    page = vc.get("/bot").text
    assert "LIVE TRADING BLOCKED" in page and "Bot state" in page and "PAUSED" in page
    assert "Controls (ADMIN)" not in page and "/bot/kill/request" not in page
    admin_page = ac.get("/bot").text
    assert "Controls (ADMIN)" in admin_page
    for slug in SLUGS.values():
        assert f'href="/bot/{slug}/request"' in admin_page


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_viewers_are_refused_every_control(
    vc: TestClient, sql: Sql, method: str, path: str
) -> None:
    before = fingerprint(sql), sql("SELECT version FROM bot_control")
    resp = vc.request(
        method,
        path,
        data={"csrf_token": "x", "confirmation": "PAUSE BOT", "password": "x"}
        if method == "POST"
        else None,
    )
    assert resp.status_code in (403, 404)
    assert (fingerprint(sql), sql("SELECT version FROM bot_control")) == before
    assert "confirm your password" not in resp.text.lower()


@pytest.mark.parametrize(("method", "path"), [("GET", "/bot"), *ROUTES])
def test_anonymous_visitors_get_nothing(sapp: Any, method: str, path: str) -> None:
    anon = TestClient(
        sapp,
        base_url="https://testserver",
        raise_server_exceptions=False,
        client=("10.0.0.9", 50000),
        headers={"origin": ORIGIN},
    )
    resp = anon.request(method, path, follow_redirects=False)
    assert resp.status_code in (303, 401, 403) and "Bot state" not in resp.text


def test_viewer_denials_are_audited(vc: TestClient, sql: Sql) -> None:
    vc.get("/bot/kill/request")
    denied = sql(
        "SELECT actor_role FROM audit_events WHERE event_code = 'authz.denied' ORDER BY seq"
    )
    assert denied and denied[-1]["actor_role"] == "VIEWER"


def test_the_menu_links_the_bot_page_for_everyone(ac: TestClient, vc: TestClient) -> None:
    assert 'href="/bot"' in ac.get("/").text and 'href="/bot"' in vc.get("/").text


# ------------------------------------------------------------------ the page itself
def test_the_page_says_it_cannot_make_orders_and_holds_no_order_form(ac: TestClient) -> None:
    body = ac.get("/bot").text
    assert "cannot create, submit, change or sell any order" in body
    assert re.findall(r'<form[^>]*action="([^"]+)"', body) == ["/logout"]
    lowered = body.lower()
    for word in (
        "place order",
        "buy now",
        "sell now",
        "market sell",
        "enable live",
        "submit order",
    ):
        assert word not in lowered
    assert not re.search(r"<script(?![^>]*\bsrc=)", body)


def test_the_live_gate_panel_is_always_blocked_and_lists_why(ac: TestClient) -> None:
    body = ac.get("/bot").text
    assert "LIVE TRADING BLOCKED" in body and "NO_LIVE_GATEWAY" in body and "NO_CREDENTIALS" in body
    assert "OPEN" not in body.split("Live gate")[1].split("</section>")[0]


def test_get_requests_never_write(ac: TestClient, sql: Sql) -> None:
    before = (
        sql("SELECT count(*) AS n FROM audit_events")[0]["n"],
        fingerprint(sql),
        sql("SELECT version FROM bot_control"),
    )
    for path in ("/bot", *[f"/bot/{s}/request" for s in SLUGS.values()]):
        assert ac.get(path).status_code == 200
    assert (
        sql("SELECT count(*) AS n FROM audit_events")[0]["n"],
        fingerprint(sql),
        sql("SELECT version FROM bot_control"),
    ) == before


def test_each_request_page_names_its_exact_phrase(ac: TestClient) -> None:
    for action, slug in SLUGS.items():
        page = ac.get(f"/bot/{slug}/request").text
        assert f"<code>{PHRASES[action]}</code>" in page


def test_the_resume_page_lists_what_blocks_it(ac: TestClient) -> None:
    page = ac.get("/bot/resume/request").text
    assert (
        "Blocked right now by" in page
        and "startup recovery has not completed" in page
        and "there is no reconciliation result" in page
    )


def test_the_overview_shows_the_real_control_state(ac: TestClient, safe: SafetyEnv) -> None:
    body = ac.get("/").text
    assert re.search(r"<dt>Bot state</dt><dd>PAUSED</dd>", body) and re.search(
        r"<dt>Kill switch state</dt><dd>INACTIVE</dd>", body
    )
    assert safe.act("kill").kind == "ok"
    assert re.search(r"<dt>Kill switch state</dt><dd>ACTIVE</dd>", ac.get("/").text)


# ------------------------------------------------------------------ the chain, over HTTP
def test_pause_full_chain_and_its_audit_trail(ac: TestClient, safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    before = sql("SELECT count(*) AS n FROM audit_events")[0]["n"]
    got = do(ac, "pause")
    assert got.status_code == 303 and got.headers["location"] == "/bot?msg=bot_paused"
    assert safe.control_row().bot_state == "PAUSED"
    trail = [r["event_code"] for r in sql("SELECT event_code FROM audit_events ORDER BY seq")][
        before:
    ]
    assert [e for e in trail if e.startswith(("bot.", "auth.reauth"))] == [
        "auth.reauth.success",
        "bot.control_requested",
        "bot.paused",
    ]
    assert "The bot is PAUSED" in ac.get("/bot?msg=bot_paused").text


@pytest.mark.parametrize("action", sorted(SLUGS))
def test_a_wrong_phrase_never_spends_the_password_confirmation(
    ac: TestClient, safe: SafetyEnv, sql: Sql, action: str
) -> None:
    safe.baseline()
    slug = SLUGS[action]
    assert reauth(ac, slug).status_code == 303
    bad = confirm(ac, slug, PHRASES[action].lower())
    assert bad.status_code == 400 and "did not match exactly" in bad.text
    assert sql("SELECT count(*) AS n FROM control_commands")[0]["n"] == 0
    # the confirmation is still available: the right phrase now goes through the chain (or is
    # refused for a state reason, never for a missing reauth)
    ok = confirm(ac, slug, PHRASES[action])
    assert "Confirm your password first" not in ok.text


@pytest.mark.parametrize("action", sorted(SLUGS))
def test_without_a_password_confirmation_nothing_happens(
    ac: TestClient, safe: SafetyEnv, sql: Sql, action: str
) -> None:
    before = sql("SELECT version FROM bot_control")
    got = do(ac, action, fresh=False)
    assert got.status_code == 400 and "Confirm your password first" in got.text
    assert (
        sql("SELECT version FROM bot_control") == before
        and sql("SELECT count(*) AS n FROM control_commands")[0]["n"] == 0
    )


def test_a_wrong_password_is_refused_and_throttled_like_any_reauth(ac: TestClient) -> None:
    bad = reauth(ac, "pause", pw="not the password")
    assert bad.status_code == 400 and "password is incorrect" in bad.text


def test_the_password_confirmation_is_single_use(ac: TestClient, safe: SafetyEnv) -> None:
    safe.running()
    assert do(ac, "pause").status_code == 303
    again = confirm(ac, "pause", PHRASES["pause"])
    assert again.status_code == 400 and "Confirm your password first" in again.text


def test_kill_switch_over_http_queues_a_cancel_and_changes_nothing_else(
    ac: TestClient, vc: TestClient, safe: SafetyEnv, sql: Sql
) -> None:
    safe.running()
    before = fingerprint(sql)
    got = do(ac, "kill")
    assert got.status_code == 303 and got.headers["location"] == "/bot?msg=bot_kill"
    row = safe.control_row()
    assert (row.bot_state, row.kill_switch) == ("PAUSED", "ACTIVE")
    assert sql("SELECT origin, state FROM control_commands") == [
        {"origin": "KILL_SWITCH", "state": "PENDING"}
    ]
    assert fingerprint(sql) == before
    page = vc.get("/bot").text
    assert "ACTIVE" in page and "Cancel commands" in page  # viewers see it too
    for table in ("order_intents", "order_attempts", "risk_decisions"):
        assert sql(f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0  # noqa: S608


def test_cancel_known_over_http_needs_a_paused_bot(
    ac: TestClient, safe: SafetyEnv, sql: Sql
) -> None:
    safe.running()
    refused = do(ac, "cancel_known")
    assert refused.status_code == 409 and "must be PAUSED first" in refused.text
    assert do(ac, "pause").status_code == 303
    ok = do(ac, "cancel_known")
    assert (
        ok.status_code == 303
        and sql("SELECT origin FROM control_commands")[0]["origin"] == "OPERATOR"
    )
    conflict = do(ac, "cancel_known")
    assert conflict.status_code == 409 and "another cancel command" in conflict.text


def test_resume_over_http_is_refused_with_reasons_until_reconciled(
    ac: TestClient, safe: SafetyEnv, sql: Sql
) -> None:
    refused = do(ac, "resume")
    assert refused.status_code == 409 and "Refused" in refused.text and "recovery" in refused.text
    assert safe.control_row().bot_state == "PAUSED"
    denied = sql(
        "SELECT event_code, result FROM audit_events WHERE event_code = 'bot.control_denied' ORDER BY seq"
    )[-1]
    assert denied["result"] == "DENIED"
    safe.baseline()
    assert safe.recover().complete
    ok = do(ac, "resume")
    assert ok.status_code == 303 and ok.headers["location"] == "/bot?msg=bot_resumed"
    assert safe.control_row().bot_state == "RUNNING"
    assert "The bot is RUNNING" in ac.get("/bot?msg=bot_resumed").text


def test_resume_is_refused_after_a_failed_reconciliation_even_with_recovery_done(
    ac: TestClient, safe: SafetyEnv
) -> None:
    from app.exchange.fake import Fault

    safe.baseline()
    assert safe.recover().complete
    safe.fake.fail("list_accounts", *[Fault("timeout")] * 3)
    assert safe.recon().outcome == "FAILED"
    refused = do(ac, "resume")
    assert refused.status_code == 409 and "last reconciliation failed" in refused.text
    assert safe.control_row().bot_state == "PAUSED"


def test_resume_is_refused_while_the_kill_switch_is_active(ac: TestClient, safe: SafetyEnv) -> None:
    safe.running()
    assert do(ac, "kill").status_code == 303
    refused = do(ac, "resume")
    assert refused.status_code == 409 and "kill switch is active" in refused.text


# ------------------------------------------------------------------ CSRF and origin
@pytest.mark.parametrize("path", [p for m, p in ROUTES if m == "POST"])
def test_every_post_needs_csrf_and_a_same_origin_request(
    ac: TestClient, sql: Sql, path: str
) -> None:
    before = (
        fingerprint(sql),
        sql("SELECT version FROM bot_control"),
        sql("SELECT count(*) AS n FROM control_commands"),
    )
    assert ac.post(path, data={"confirmation": "PAUSE BOT", "password": "x"}).status_code == 403
    assert ac.post(path, data={"csrf_token": "forged", "password": "x"}).status_code == 403
    cross = ac.post(
        path,
        data={"csrf_token": token(ac), "password": "x"},
        headers={"origin": "https://evil.example"},
    )
    assert cross.status_code == 403
    assert (
        fingerprint(sql),
        sql("SELECT version FROM bot_control"),
        sql("SELECT count(*) AS n FROM control_commands"),
    ) == before


def test_an_unknown_action_slug_does_not_exist(ac: TestClient) -> None:
    for slug in ("sell", "market-sell", "liquidate", "release", "unkill", "orders"):
        assert ac.get(f"/bot/{slug}/request").status_code in (400, 404, 422)
        assert ac.post(
            f"/bot/{slug}/confirm", data={"csrf_token": token(ac), "confirmation": "X"}
        ).status_code in (400, 404, 422)


def test_no_control_action_over_http_ever_calls_the_exchange(
    ac: TestClient, safe: SafetyEnv
) -> None:
    safe.baseline()
    assert safe.recover().complete
    calls = len(safe.fake.calls)
    for action in ("resume", "pause", "cancel_known", "kill"):
        do(ac, action)
    assert len(safe.fake.calls) == calls  # the web tier has no path to any exchange
