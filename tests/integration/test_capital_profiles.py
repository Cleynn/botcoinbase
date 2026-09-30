"""Capital profiles end to end: the immutable table, the guarded selection per mode, the database
limits that read the selected profile, the order pipeline sizing against the funds the exchange
reports, the ADMIN control flow and the Bot page. SYNTHETIC data; the exchange is the FAKE
double."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.capital.profiles import PROFILES
from app.exchange.fake import Fault
from app.safety import live_gate
from app.safety.control import PHRASES, PROFILE_PHRASES
from app.safety.pipeline import OrderPipeline
from tests.conftest import GOOD_PASSWORD, ORIGIN, Account, TestDb, csrf_from
from tests.integration.safety_env import SafetyEnv
from tests.integration.test_safety_db import DENIED, VIOLATION, bump, refuse, run

Sql = Callable[..., list[dict[str, Any]]]
D = __import__("decimal").Decimal


def events(sql: Sql, prefix: str = "bot.") -> list[str]:
    return [
        r["event_code"]
        for r in sql(
            "SELECT event_code FROM audit_events WHERE event_code LIKE %s ORDER BY seq",
            (prefix + "%",),
        )
    ]


def select_profile(db: TestDb, safe: SafetyEnv, column: str, name: str) -> None:
    sql_, args = bump(
        safe.clock.now(),
        set_=f"{column} = '{name}'",
        reason=column.split("_")[0].upper() + "_PROFILE_CHANGE",
    )
    run(db, "td_app", sql_, args)


def running_with(safe: SafetyEnv, db: TestDb, profile: str, usdc: str) -> None:
    """Baseline and exchange balance of `usdc`, the PAPER profile selected, then RUNNING."""
    safe.baseline(usdc)
    select_profile(db, safe, "paper_profile", profile)
    assert safe.recover().complete
    assert safe.act("resume").kind == "ok"


# ------------------------------------------------------------------ the table
def test_the_database_profiles_equal_the_code_registry(sql: Sql) -> None:
    rows = {r["name"]: r for r in sql("SELECT * FROM capital_profiles")}
    assert set(rows) == set(PROFILES)
    for name, p in PROFILES.items():
        r = rows[name]
        got = (r["allocation_cap"], r["protected_reserve"], r["max_deployment"], r["max_order"])
        assert got == (p.allocation_cap, p.protected_reserve, p.max_deployment, p.max_order)


@pytest.mark.parametrize("role", ["td_app", "td_ctl"])
def test_no_runtime_role_can_change_the_profile_table(db: TestDb, role: str) -> None:
    refuse(db, role, "UPDATE capital_profiles SET max_order = 1000", exc=DENIED)
    refuse(db, role, "DELETE FROM capital_profiles", exc=DENIED)
    refuse(
        db, role,
        "INSERT INTO capital_profiles VALUES ('huge', 'Huge profile', 9999, 3000, 6000, 6000)",
        exc=DENIED,
    )  # fmt: skip


def test_even_the_owner_cannot_rewrite_a_profile(db: TestDb) -> None:
    with psycopg.connect(db.owner_target().conninfo(), autocommit=True) as conn:
        for statement in (
            "UPDATE capital_profiles SET max_order = 1000",
            "DELETE FROM capital_profiles",
            "TRUNCATE capital_profiles CASCADE",
            "INSERT INTO capital_profiles VALUES ('huge', 'Huge profile', 9999, 3000, 6000, 6000)",
        ):
            with pytest.raises(VIOLATION, match="append-only"):
                conn.execute(statement)


# ------------------------------------------------------------------ selecting a profile
def test_the_web_selects_a_profile_while_paused_and_the_history_records_it(
    safe: SafetyEnv, db: TestDb, sql: Sql
) -> None:
    assert safe.control_row().paper_profile == "pilot"
    select_profile(db, safe, "paper_profile", "expanded")
    select_profile(db, safe, "live_profile", "medium")
    row = safe.control_row()
    assert (row.paper_profile, row.live_profile) == ("expanded", "medium")
    assert [r["event"] for r in sql("SELECT event FROM bot_control_history ORDER BY id")] == [
        "PAPER_PROFILE", "LIVE_PROFILE",
    ]  # fmt: skip


def test_the_host_cannot_select_a_profile(safe: SafetyEnv, db: TestDb) -> None:
    sql_, args = bump(safe.clock.now(), set_="paper_profile = 'expanded'")
    refuse(db, "td_ctl", sql_, args, exc=DENIED)


def test_an_unknown_profile_is_refused_by_the_schema(safe: SafetyEnv, db: TestDb) -> None:
    sql_, args = bump(safe.clock.now(), set_="paper_profile = 'huge'")
    refuse(db, "td_app", sql_, args, exc=psycopg.errors.ForeignKeyViolation)


def test_a_profile_cannot_change_while_the_bot_is_running(safe: SafetyEnv, db: TestDb) -> None:
    safe.running()
    sql_, args = bump(safe.clock.now(), set_="paper_profile = 'expanded'")
    assert "PAUSED" in refuse(db, "td_app", sql_, args, VIOLATION)
    assert safe.control_row().paper_profile == "pilot"


def test_a_profile_change_is_never_combined_with_another_change(
    safe: SafetyEnv, db: TestDb
) -> None:
    both = bump(safe.clock.now(), set_="paper_profile = 'expanded', live_profile = 'expanded'")
    assert "one profile" in refuse(db, "td_app", *both, VIOLATION)
    kill = bump(
        safe.clock.now(),
        set_=(
            "paper_profile = 'expanded', kill_switch = 'ACTIVE', kill_reason = 'TEST_KILL', "
            f"kill_activated_at = '{safe.clock.now().isoformat()}'"
        ),
    )
    assert "combined" in refuse(db, "td_app", *kill, VIOLATION)
    assert safe.control_row().paper_profile == "pilot"


# ------------------------------------------------------------------ the database limits follow it
def test_the_selected_profile_sets_the_per_order_cap_in_the_database(
    safe: SafetyEnv, db: TestDb
) -> None:
    running_with(safe, db, "expanded", "100")
    safe.new_attempt(intent=safe.make_intent(qty="0.2"))  # 20 USDC: over pilot's 12, within 25
    with pytest.raises(VIOLATION, match="ORDER_CAP_BREACH"):
        safe.new_attempt(intent=safe.make_intent(qty="0.26"))  # 26 USDC: over expanded's 25


def test_the_pilot_profile_keeps_the_original_limits_in_the_database(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.running()  # pilot, baseline 50
    with pytest.raises(VIOLATION, match="ORDER_CAP_BREACH"):
        safe.new_attempt(intent=safe.make_intent(qty="0.13"))  # 13 USDC > 12


def test_the_selected_profiles_reserve_is_enforced_by_the_database(
    safe: SafetyEnv, db: TestDb
) -> None:
    running_with(safe, db, "expanded", "100")
    for _ in range(3):
        safe.new_attempt(intent=safe.make_intent(qty="0.2"))  # 3 x 20 = 60 reserved
    with pytest.raises(VIOLATION, match="RESERVE_BREACH"):
        safe.new_attempt(intent=safe.make_intent(qty="0.2"))  # 100 - 60 - 20.1 < the 25 reserve


def test_the_research_profile_authorizes_nothing(safe: SafetyEnv, db: TestDb) -> None:
    running_with(safe, db, "research", "100")
    with pytest.raises(VIOLATION, match="ORDER_CAP_BREACH"):
        safe.new_attempt(intent=safe.make_intent(qty="0.01"))


# ------------------------------------------------------------------ the pipeline and the funds
def test_a_buy_is_sized_against_the_usdc_the_exchange_reports(safe: SafetyEnv) -> None:
    safe.running()  # the ledger baseline says 50
    safe.fake.set_balance("USDC", "20")  # but the exchange now reports only 20
    result = safe.pipeline.submit(safe.proposal(), source="test", slot="a", book=safe.book())
    assert result.kind == "blocked" and "INSUFFICIENT_FUNDS" in result.reasons
    assert safe.fake.calls.count("submit") == 0


def test_the_funds_that_fit_are_allowed_through_to_the_exchange(safe: SafetyEnv) -> None:
    safe.running()
    safe.fake.set_balance("USDC", "25.06")  # 10 USDC + 0.6% stress fee = 10.06; 25.06 - 15 reserve
    result = safe.pipeline.submit(safe.proposal(), source="test", slot="a", book=safe.book())
    assert result.kind == "submitted" and safe.fake.calls.count("submit") == 1


def test_a_failed_balance_read_blocks_the_buy(safe: SafetyEnv) -> None:
    safe.running()
    safe.fake.fail("list_accounts", Fault("timeout"))
    result = safe.pipeline.submit(safe.proposal(), source="test", slot="a", book=safe.book())
    assert result.kind == "blocked" and "FUNDS_UNAVAILABLE" in result.reasons
    assert safe.fake.calls.count("submit") == 0


def test_a_failed_funds_read_is_recorded_and_repeated_failures_open_the_api_rule(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.running()
    limit = safe.settings.safety.api_failure_threshold
    for i in range(limit):
        safe.fake.fail("list_accounts", Fault("timeout"))
        safe.pipeline.submit(safe.proposal(), source="test", slot=f"f{i}", book=safe.book())
    failed = sql(
        "SELECT code FROM api_events WHERE operation = 'list_accounts' AND NOT ok ORDER BY id"
    )
    assert len(failed) == limit and {r["code"] for r in failed} == {"FUNDS_READ_FAILED"}
    result = safe.pipeline.submit(safe.proposal(), source="test", slot="after", book=safe.book())
    assert result.kind == "blocked" and "API_FAILURES" in result.reasons


def test_no_funds_source_means_no_buy(safe: SafetyEnv) -> None:
    safe.running()
    bare = OrderPipeline(
        storage=safe.ctl,
        clock=safe.clock,
        settings=safe.settings,
        gateway=safe.fake,
        boot_id=safe.boot_id,
        reconcile=safe.reconciler.run,
    )
    result = bare.submit(safe.proposal(), source="test", slot="a", book=safe.book())
    assert result.kind == "blocked" and "FUNDS_UNAVAILABLE" in result.reasons


def test_the_expanded_profile_lets_a_larger_order_through_the_whole_pipeline(
    safe: SafetyEnv, db: TestDb
) -> None:
    running_with(safe, db, "expanded", "100")
    safe.fake.set_balance("USDC", "100")
    result = safe.pipeline.submit(
        safe.proposal(qty="0.2"), source="test", slot="a", book=safe.book()
    )  # 20 USDC
    assert result.kind == "submitted", result.reasons


def test_the_selected_profile_blocks_what_its_own_limits_forbid(
    safe: SafetyEnv, db: TestDb
) -> None:
    running_with(safe, db, "expanded", "100")
    safe.fake.set_balance("USDC", "100")
    result = safe.pipeline.submit(
        safe.proposal(qty="0.26"), source="test", slot="a", book=safe.book()
    )  # 26 USDC > 25
    assert result.kind == "blocked" and "ORDER_CAP_BREACH" in result.reasons


# ------------------------------------------------------------------ the control service
@pytest.mark.parametrize("mode", ["paper", "live"])
def test_an_admin_selects_a_profile_with_phrase_and_reauth_and_it_is_audited(
    safe: SafetyEnv, sql: Sql, mode: str
) -> None:
    safe.reauth.available = True
    out = safe.control.set_profile(safe.ctx, safe.actor, mode, "expanded", PROFILE_PHRASES[mode])
    assert out.kind == "ok" and safe.reauth.consumed == 1
    row = safe.control_row()
    other = "live_profile" if mode == "paper" else "paper_profile"
    assert getattr(row, f"{mode}_profile") == "expanded" and getattr(row, other) == "pilot"
    assert events(sql) == ["bot.control_requested", "bot.profile_changed"]
    row_ = sql("SELECT detail FROM audit_events WHERE event_code = 'bot.profile_changed'")[0]
    detail = row_["detail"] if isinstance(row_["detail"], dict) else json.loads(row_["detail"])
    assert detail["to"] == "expanded" and detail["live_gate"] == "BLOCKED"


def test_a_live_profile_choice_leaves_the_live_gate_blocked(safe: SafetyEnv) -> None:
    safe.reauth.available = True
    assert (
        safe.control.set_profile(
            safe.ctx, safe.actor, "live", "medium", PROFILE_PHRASES["live"]
        ).kind
        == "ok"
    )
    assert live_gate.panel().status == "BLOCKED"
    assert safe.control_row().live_profile == "medium"
    assert live_gate.order_gate("LIVE", safe.settings) == (False, "LIVE_GATE_BLOCKED")


def test_production_accepts_only_the_pilot_profile_and_spends_nothing_on_a_refusal(
    safe: SafetyEnv, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod = safe.settings.model_copy(update={"environment": "production"})
    monkeypatch.setattr(safe.control, "_settings", prod)
    safe.reauth.available = True
    out = safe.control.set_profile(
        safe.ctx, safe.actor, "live", "expanded", PROFILE_PHRASES["live"]
    )
    assert out.kind == "not_allowed" and out.reasons == ("PROFILE_NOT_APPROVED",)
    assert safe.reauth.consumed == 0 and events(sql) == []
    assert safe.control_row().live_profile == "pilot"


def test_the_audit_records_the_real_live_gate_status(safe: SafetyEnv, sql: Sql) -> None:
    safe.reauth.available = True
    safe.control.set_profile(safe.ctx, safe.actor, "paper", "expanded", PROFILE_PHRASES["paper"])
    row_ = sql("SELECT detail FROM audit_events WHERE event_code = 'bot.profile_changed'")[0]
    detail = row_["detail"] if isinstance(row_["detail"], dict) else json.loads(row_["detail"])
    assert detail["live_gate"] == live_gate.panel(safe.settings).status == "BLOCKED"


def test_the_two_profile_phrases_are_the_specified_ones_and_distinct_from_the_four() -> None:
    assert PROFILE_PHRASES == {
        "paper": "UPDATE CAPITAL LIMITS FOR PAPER MODE",
        "live": "UPDATE CAPITAL LIMITS FOR LIVE MODE",
    }
    assert not set(PROFILE_PHRASES.values()) & set(PHRASES.values())


def test_the_wrong_phrase_or_the_other_modes_phrase_is_refused_without_spending_the_reauth(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.reauth.available = True
    for typed in (PROFILE_PHRASES["paper"].lower(), PROFILE_PHRASES["live"], ""):
        out = safe.control.set_profile(safe.ctx, safe.actor, "paper", "expanded", typed)
        assert out.kind == "phrase_mismatch"
    assert safe.reauth.consumed == 0 and safe.control_row().paper_profile == "pilot"


def test_without_a_fresh_reauth_nothing_changes(safe: SafetyEnv) -> None:
    safe.reauth.available = False
    out = safe.control.set_profile(
        safe.ctx, safe.actor, "paper", "expanded", PROFILE_PHRASES["paper"]
    )
    assert out.kind == "reauth_required" and safe.control_row().paper_profile == "pilot"


def test_an_unknown_profile_or_mode_is_refused_before_the_reauth_is_spent(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.reauth.available = True
    bad_profile = safe.control.set_profile(
        safe.ctx, safe.actor, "paper", "huge", PROFILE_PHRASES["paper"]
    )
    bad_mode = safe.control.set_profile(safe.ctx, safe.actor, "real", "pilot", "x")
    assert bad_profile.reasons == ("PROFILE",) and bad_mode.reasons == ("MODE",)
    assert safe.reauth.consumed == 0 and events(sql) == []


def test_a_profile_is_refused_while_running_and_when_unchanged(safe: SafetyEnv, sql: Sql) -> None:
    safe.reauth.available = True
    same = safe.control.set_profile(
        safe.ctx, safe.actor, "paper", "pilot", PROFILE_PHRASES["paper"]
    )
    assert same.kind == "not_allowed" and same.reasons == ("PROFILE_UNCHANGED",)
    safe.running()
    safe.reauth.available = True
    busy = safe.control.set_profile(
        safe.ctx, safe.actor, "paper", "expanded", PROFILE_PHRASES["paper"]
    )
    assert busy.kind == "not_allowed" and busy.reasons == ("BOT_MUST_BE_PAUSED",)
    assert safe.control_row().paper_profile == "pilot"


def test_choosing_a_profile_creates_no_order_and_queues_no_command(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.reauth.available = True
    safe.control.set_profile(safe.ctx, safe.actor, "paper", "expanded", PROFILE_PHRASES["paper"])
    for table in ("order_intents", "order_attempts", "risk_decisions", "control_commands"):
        assert sql(f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0  # noqa: S608
    assert safe.fake.calls == []


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


def _request_url(mode: str, profile: str) -> str:
    return f"/bot/capital/{mode}/request?profile={profile}"


def _token(client: TestClient, mode: str, profile: str) -> str:
    return csrf_from(client.get(_request_url(mode, profile)).text)


def _reauth(client: TestClient, mode: str, profile: str) -> Any:
    return client.post(
        f"/bot/capital/{mode}/reauth",
        data={
            "csrf_token": _token(client, mode, profile),
            "password": GOOD_PASSWORD,
            "profile": profile,
        },
        follow_redirects=False,
    )


def _confirm(client: TestClient, mode: str, profile: str, phrase: str) -> Any:
    return client.post(
        f"/bot/capital/{mode}/confirm",
        data={
            "csrf_token": _token(client, mode, profile),
            "confirmation": phrase,
            "profile": profile,
        },
        follow_redirects=False,
    )


def test_the_page_shows_the_profiles_and_the_selection_to_everyone(
    ac: TestClient, vc: TestClient
) -> None:
    for client in (ac, vc):
        page = client.get("/bot").text
        assert "Capital profiles (USDC)" in page and "Initial pilot (pilot)" in page
        assert "Small expanded test" in page and "Research only" in page
        assert "PAPER, LIVE (blocked)" in page
        assert "<form" not in page.split("Capital profiles")[1].split("</section>")[0]
    assert "for PAPER…" in ac.get("/bot").text and "for PAPER…" not in vc.get("/bot").text


def test_the_profile_table_scrolls_inside_its_own_box_on_narrow_screens(ac: TestClient) -> None:
    page = ac.get("/bot").text
    table = page.index("<table>")
    wrapper = page.rindex('<div class="table-wrap"', 0, table)
    assert wrapper < table < page.index("</table>") < page.index("</div>", table)
    assert 'role="region"' in page[wrapper:table] and 'tabindex="0"' in page[wrapper:table]


def test_a_viewer_cannot_reach_the_profile_routes(vc: TestClient) -> None:
    assert vc.get(_request_url("paper", "expanded")).status_code == 403
    assert (
        vc.post(
            "/bot/capital/paper/confirm",
            data={"csrf_token": "x", "confirmation": "x", "profile": "expanded"},
            follow_redirects=False,
        ).status_code
        == 403
    )


def test_a_post_without_a_csrf_token_changes_nothing(ac: TestClient, safe: SafetyEnv) -> None:
    resp = ac.post(
        "/bot/capital/paper/confirm",
        data={"confirmation": PROFILE_PHRASES["paper"], "profile": "expanded"},
        follow_redirects=False,
    )
    assert resp.status_code in (400, 403, 422)
    assert safe.control_row().paper_profile == "pilot"


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_the_full_flow_selects_the_profile(
    ac: TestClient, safe: SafetyEnv, sql: Sql, mode: str
) -> None:
    assert _reauth(ac, mode, "expanded").status_code == 303
    resp = _confirm(ac, mode, "expanded", PROFILE_PHRASES[mode])
    assert resp.status_code == 303 and resp.headers["location"] == f"/bot?msg=profile_{mode}"
    assert getattr(safe.control_row(), f"{mode}_profile") == "expanded"
    assert events(sql) == ["bot.control_requested", "bot.profile_changed"]
    page = ac.get(resp.headers["location"]).text
    assert "LIVE TRADING BLOCKED" in page
    if mode == "live":
        assert "stays BLOCKED" in page


def test_the_confirm_page_states_what_will_change_and_carries_the_profile(
    ac: TestClient,
) -> None:
    page = ac.get(_request_url("paper", "medium")).text
    assert "UPDATE CAPITAL LIMITS FOR PAPER MODE" in page
    assert "allocation cap 250" in page and "maximum deployment 150" in page
    assert page.count('name="profile" value="medium"') == 2  # both steps carry it


def test_a_wrong_phrase_is_refused_and_keeps_the_reauth(ac: TestClient, safe: SafetyEnv) -> None:
    assert _reauth(ac, "paper", "expanded").status_code == 303
    resp = _confirm(ac, "paper", "expanded", PROFILE_PHRASES["paper"].lower())
    assert resp.status_code == 400 and safe.control_row().paper_profile == "pilot"
    assert _confirm(ac, "paper", "expanded", PROFILE_PHRASES["paper"]).status_code == 303


def test_a_running_bot_refuses_a_profile_and_the_page_says_why(
    ac: TestClient, safe: SafetyEnv
) -> None:
    safe.running()
    assert "pause the bot first" in ac.get("/bot").text
    assert _reauth(ac, "paper", "expanded").status_code == 303
    resp = _confirm(ac, "paper", "expanded", PROFILE_PHRASES["paper"])
    assert resp.status_code == 409 and "must be PAUSED" in resp.text
    assert safe.control_row().paper_profile == "pilot"


def test_an_unknown_or_malformed_profile_never_reaches_the_service(ac: TestClient) -> None:
    resp = ac.get(_request_url("paper", "huge"), follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/bot?msg=invalid"
    assert ac.get(_request_url("paper", "BAD;DROP"), follow_redirects=False).status_code in (
        400,
        422,
    )
    assert ac.get(_request_url("real", "pilot"), follow_redirects=False).status_code in (
        400,
        404,
        422,
    )
