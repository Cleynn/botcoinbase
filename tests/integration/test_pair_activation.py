"""Activation is a separate, guarded ADMIN action; one active pair globally; races and guards."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.domain.pairs import PairAction, PairState, phrase_for
from tests.conftest import GOOD_PASSWORD, Account, FakeClock, csrf_from

Sql = Callable[..., list[dict[str, Any]]]


def token(client: TestClient) -> str:
    return csrf_from(client.get("/security").text)


def post(client: TestClient, path: str, **data: Any) -> Any:
    return client.post(path, data={"csrf_token": token(client), **data}, follow_redirects=False)


def audit(sql: Sql, code: str) -> list[dict[str, Any]]:
    return sql("SELECT * FROM audit_events WHERE event_code = %s ORDER BY seq", (code,))


def denials(sql: Sql) -> list[str]:
    return [r["reason_code"] for r in audit(sql, "pair.transition_denied")]


# ------------------------------------------------------------------ the production default gate
def test_by_default_activation_is_refused_because_no_bot_state_exists(
    closed_env: Any, admin_client: TestClient, sql: Sql
) -> None:
    pair_id = closed_env.eligible("BTC-USDC")
    page = admin_client.get(f"/pairs/{pair_id}/activate/request")
    assert page.status_code == 200
    assert "BOT_STATE_UNAVAILABLE" in page.text and "MODE_NOT_PAPER" in page.text
    assert "No confirmation is offered" in page.text
    assert 'action="/pairs/' not in page.text.replace(f'href="/pairs/{pair_id}"', "")
    post(
        admin_client,
        f"/pairs/{pair_id}/activate/reauth",
        version=closed_env.get(pair_id).version,
        password=GOOD_PASSWORD,
    )
    response = post(
        admin_client,
        f"/pairs/{pair_id}/activate/confirm",
        version=closed_env.get(pair_id).version,
        confirmation="ACTIVATE PAPER PAIR BTC-USDC",
    )
    assert response.status_code == 409
    assert closed_env.state(pair_id) is PairState.PAPER_ELIGIBLE
    assert denials(sql) == ["GUARD_FAILED"]
    assert (
        "BOT_STATE_UNAVAILABLE"
        in json.loads(audit(sql, "pair.transition_denied")[0]["detail"])["reasons"]
    )
    assert audit(sql, "pair.activated_paper") == []


def test_a_refused_activation_does_not_consume_the_password_confirmation(closed_env: Any) -> None:
    pair_id = closed_env.eligible("BTC-USDC")
    closed_env.reauth.available = True
    outcome = closed_env.service.confirm(
        closed_env.ctx,
        closed_env.actor,
        pair_id,
        PairAction.ACTIVATE,
        closed_env.get(pair_id).version,
        "ACTIVATE PAPER PAIR BTC-USDC",
    )
    assert (
        outcome.kind == "blocked"
        and closed_env.reauth.available
        and closed_env.reauth.consumed == 0
    )


def test_the_default_gate_explains_the_mode_when_it_is_paper() -> None:
    from app.pairs.service import UnavailableRuntimeGate

    paper: Any = UnavailableRuntimeGate("PAPER")
    backtest: Any = UnavailableRuntimeGate("BACKTEST")
    assert paper.activation_blockers(None, None) == ("BOT_STATE_UNAVAILABLE",)
    assert backtest.activation_blockers(None, None) == ("MODE_NOT_PAPER", "BOT_STATE_UNAVAILABLE")
    assert paper.is_clean(None, None) == (False, ("RECONCILIATION_UNAVAILABLE",))


# ------------------------------------------------------------------ activation with an open gate (tests only)
def test_activation_needs_eligibility_phrase_and_password_and_is_separate_from_validation(
    env: Any, sql: Sql
) -> None:
    pair_id = env.eligible("BTC-USDC")
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE  # validation alone never activates
    version = env.get(pair_id).version
    env.reauth.available = True
    wrong = env.service.confirm(
        env.ctx, env.actor, pair_id, PairAction.ACTIVATE, version, "ACTIVATE PAIR BTC-USDC"
    )
    assert wrong.kind == "phrase_mismatch"
    env.reauth.available = False
    no_pw = env.service.confirm(
        env.ctx, env.actor, pair_id, PairAction.ACTIVATE, version, "ACTIVATE PAPER PAIR BTC-USDC"
    )
    assert no_pw.kind == "reauth_required"
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE
    assert env.act(pair_id, PairAction.ACTIVATE).kind == "ok"
    pair = env.get(pair_id)
    assert pair.state is PairState.PAPER_ACTIVE and pair.ever_active and pair.version == version + 1
    event = audit(sql, "pair.activated_paper")[0]
    assert event["actor_role"] == "ADMIN" and event["target_id"] == str(pair_id)
    history = sql(
        "SELECT * FROM pair_state_history WHERE pair_id = %s ORDER BY version_after DESC",
        (pair_id,),
    )[0]
    assert (history["state_after"], history["transition_no"], history["actor_class"]) == (
        "PAPER_ACTIVE",
        7,
        "WEB",
    )


def test_only_paper_eligible_or_paused_pairs_can_be_activated(env: Any, sql: Sql) -> None:
    proposed = env.make("BTC-USDC", PairState.PROPOSED)
    outcome = env.act(proposed, PairAction.ACTIVATE)
    assert outcome.kind == "not_allowed" and env.state(proposed) is PairState.PROPOSED
    disabled = env.make("ETH-USDC", PairState.DISABLED)
    assert env.act(disabled, PairAction.ACTIVATE).kind == "not_allowed"
    assert denials(sql) == ["ILLEGAL_TRANSITION", "ILLEGAL_TRANSITION"]


def test_a_research_only_pair_cannot_be_activated(env: Any, coinbase: Any) -> None:
    coinbase.drop_daily = 40
    env.discover()
    pair_id = env.propose("BTC-USDC")
    env.queue(pair_id)
    env.validate()
    assert env.state(pair_id) is PairState.RESEARCH_ONLY
    assert env.act(pair_id, PairAction.ACTIVATE).kind == "not_allowed"


def test_the_second_pair_cannot_be_activated_while_one_is_active(env: Any, sql: Sql) -> None:
    first = env.make("BTC-USDC", PairState.PAPER_ACTIVE)
    second = env.eligible("ETH-USDC")
    outcome = env.act(second, PairAction.ACTIVATE)
    assert outcome.kind == "blocked" and "ANOTHER_PAIR_ACTIVE" in outcome.reasons
    assert (
        env.state(first) is PairState.PAPER_ACTIVE and env.state(second) is PairState.PAPER_ELIGIBLE
    )
    assert sql("SELECT count(*) AS n FROM pairs WHERE state = 'PAPER_ACTIVE'")[0]["n"] == 1


def test_a_paused_pair_blocks_activating_another(env: Any) -> None:
    env.make("BTC-USDC", PairState.PAUSED)
    other = env.eligible("ETH-USDC")
    outcome = env.act(other, PairAction.ACTIVATE)
    assert outcome.kind == "blocked" and outcome.reasons == ("ANOTHER_PAIR_PAUSED",)


def test_the_database_backstops_the_service_when_the_guard_is_bypassed(
    env: Any, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = env.make("BTC-USDC", PairState.PAPER_ACTIVE)
    second = env.eligible("ETH-USDC")
    monkeypatch.setattr(env.service, "_blockers", lambda *a, **k: ())  # a bug: no guard at all
    outcome = env.act(second, PairAction.ACTIVATE)
    assert outcome.kind == "conflict" and outcome.reasons == ("ANOTHER_PAIR_ACTIVE",)
    assert (
        env.state(first) is PairState.PAPER_ACTIVE and env.state(second) is PairState.PAPER_ELIGIBLE
    )
    assert denials(sql) == ["ANOTHER_PAIR_ACTIVE"]
    assert sql("SELECT count(*) AS n FROM pairs WHERE state = 'PAPER_ACTIVE'")[0]["n"] == 1


@pytest.mark.parametrize("round_", range(3))
def test_concurrent_activations_produce_exactly_one_active_pair(
    env: Any, coinbase: Any, sql: Sql, round_: int
) -> None:
    for code in ("ADA", "XRP"):
        coinbase.products[f"{code}-USDC"] = {
            **coinbase.products["SOL-USDC"],
            "product_id": f"{code}-USDC",
            "base_currency_id": code,
            "alias": f"{code}-USD",
        }
        coinbase.mids[f"{code}-USDC"] = "100"
    env.discover()
    ids = []
    for code in ("BTC", "ETH", "SOL", "ADA", "XRP"):
        pair_id = env.propose(f"{code}-USDC")
        env.queue(pair_id)
        ids.append(pair_id)
    env.validate()
    assert all(env.state(i) is PairState.PAPER_ELIGIBLE for i in ids)
    env.service._consume_reauth = lambda _r, _c: True  # noqa: SLF001  every caller has "just reauthenticated"
    barrier = threading.Barrier(len(ids))
    versions = {i: env.get(i).version for i in ids}

    def go(pair_id: UUID) -> str:
        barrier.wait()
        pair = env.get(pair_id)
        outcome = env.service.confirm(
            env.ctx,
            env.actor,
            pair_id,
            PairAction.ACTIVATE,
            versions[pair_id],
            phrase_for(PairAction.ACTIVATE, pair.product_id) or "",
        )
        return str(outcome.kind)

    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        results = list(pool.map(go, ids))
    assert sorted(results) == ["blocked"] * 4 + ["ok"]
    assert sql("SELECT count(*) AS n FROM pairs WHERE state = 'PAPER_ACTIVE'")[0]["n"] == 1
    assert len(audit(sql, "pair.activated_paper")) == 1
    assert sorted(denials(sql)) == ["GUARD_FAILED"] * 4
    chain = sql("SELECT count(*) AS n FROM audit_events")[0]["n"]
    assert chain > 0


def test_concurrent_proposals_never_exceed_the_cap_or_duplicate_a_product(
    settings: Any, env: Any, coinbase: Any, sql: Sql
) -> None:
    env.discover()
    product = env.product_uuid("BTC-USDC")
    barrier = threading.Barrier(6)

    def go(_: int) -> str:
        barrier.wait()
        return str(env.service.propose(env.actor, product).kind)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(go, range(6)))
    assert sorted(results) == ["duplicate"] * 5 + ["ok"]
    assert sql("SELECT count(*) AS n FROM pairs")[0]["n"] == 1


# ------------------------------------------------------------------ validity at activation time
def test_expired_validation_blocks_activation(env: Any, clock: FakeClock) -> None:
    pair_id = env.eligible("BTC-USDC")
    clock.advance(24 * 3600 + 1)
    outcome = env.act(pair_id, PairAction.ACTIVATE)
    assert outcome.kind == "blocked" and "VALIDATION_EXPIRED" in outcome.reasons
    assert "METADATA_STALE" in outcome.reasons  # a day-old snapshot is also stale


def test_stale_metadata_alone_blocks_activation(env: Any, sql: Sql) -> None:
    pair_id = env.eligible("BTC-USDC")
    sql(
        "UPDATE product_metadata_current SET last_verified_at = last_verified_at - interval '2 hours'"
    )
    outcome = env.act(pair_id, PairAction.ACTIVATE)
    assert outcome.kind == "blocked" and outcome.reasons == ("METADATA_STALE",)


def test_changed_metadata_blocks_activation_until_revalidated(env: Any, coinbase: Any) -> None:
    pair_id = env.eligible("BTC-USDC")
    coinbase.products["BTC-USDC"]["status"] = "offline"
    env.runner.discover()
    outcome = env.act(pair_id, PairAction.ACTIVATE)
    assert outcome.kind == "blocked"
    assert {"METADATA_CHANGED", "PRODUCT_NOT_OK"} <= set(outcome.reasons)
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE


def test_a_pair_without_a_passing_run_cannot_be_activated_even_if_the_state_says_eligible(
    env: Any, sql: Sql
) -> None:
    pair_id = env.eligible("BTC-USDC")
    sql("ALTER TABLE pair_validation_runs DISABLE TRIGGER USER")
    sql("UPDATE pair_validation_runs SET outcome = 'FAIL'")
    sql("ALTER TABLE pair_validation_runs ENABLE TRIGGER USER")
    outcome = env.act(pair_id, PairAction.ACTIVATE)
    assert outcome.kind == "blocked" and "NO_PASS_VALIDATION" in outcome.reasons


# ------------------------------------------------------------------ pause, resume, deactivate
def test_pause_is_a_restrictive_one_click_action(env: Any, sql: Sql) -> None:
    pair_id = env.make("BTC-USDC", PairState.PAPER_ACTIVE)
    outcome = env.service.simple_action(
        env.actor, pair_id, PairAction.PAUSE, env.get(pair_id).version
    )
    assert outcome.kind == "ok" and env.state(pair_id) is PairState.PAUSED
    assert audit(sql, "pair.paused")[0]["reason_code"] == "PAUSE"
    assert env.get(pair_id).ever_active is True


def test_resume_needs_its_own_phrase_and_a_still_valid_run(env: Any, clock: FakeClock) -> None:
    pair_id = env.make("BTC-USDC", PairState.PAUSED)
    assert (
        env.act(pair_id, PairAction.RESUME, typed="ACTIVATE PAPER PAIR BTC-USDC").kind
        == "phrase_mismatch"
    )
    assert (
        env.act(pair_id, PairAction.RESUME).kind == "ok"
        and env.state(pair_id) is PairState.PAPER_ACTIVE
    )
    env.service.simple_action(env.actor, pair_id, PairAction.PAUSE, env.get(pair_id).version)
    clock.advance(25 * 3600)
    outcome = env.act(pair_id, PairAction.RESUME)
    assert outcome.kind == "blocked" and "VALIDATION_EXPIRED" in outcome.reasons


def test_deactivate_returns_a_paused_pair_to_eligible_and_it_must_be_activated_again(
    env: Any,
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PAUSED)
    outcome = env.service.simple_action(
        env.actor, pair_id, PairAction.DEACTIVATE, env.get(pair_id).version
    )
    assert outcome.kind == "ok" and env.state(pair_id) is PairState.PAPER_ELIGIBLE
    assert env.act(pair_id, PairAction.ACTIVATE).kind == "ok"


def test_a_paused_pair_can_be_sent_back_for_validation(env: Any) -> None:
    pair_id = env.make("BTC-USDC", PairState.PAUSED)
    assert env.queue(pair_id).kind == "ok" and env.state(pair_id) is PairState.VALIDATING
    env.validate()
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE


# ------------------------------------------------------------------ disable/archive of previously active pairs
def test_a_previously_active_pair_can_be_archived_only_when_reconciliation_shows_it_clean(
    env: Any, open_gate: Any
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PAUSED)
    open_gate.clean = False
    blocked = env.act(pair_id, PairAction.ARCHIVE)
    assert blocked.kind == "blocked" and blocked.reasons == ("NOT_CLEAN",)
    assert env.state(pair_id) is PairState.PAUSED
    open_gate.clean = True
    assert (
        env.act(pair_id, PairAction.ARCHIVE).kind == "ok"
        and env.state(pair_id) is PairState.ARCHIVED
    )


def test_a_pair_that_was_never_active_needs_no_reconciliation_to_archive(
    env: Any, open_gate: Any
) -> None:
    open_gate.clean = False
    pair_id = env.eligible("BTC-USDC")
    assert env.act(pair_id, PairAction.ARCHIVE).kind == "ok"


# ------------------------------------------------------------------ end to end over HTTP
@pytest.fixture
def open_client(open_app: Any, login: Callable[..., Any], admin: Account) -> TestClient:
    client = TestClient(
        open_app,
        base_url="https://testserver",
        raise_server_exceptions=False,
        client=("10.0.0.7", 50000),
        headers={"origin": "https://testserver"},
    )
    assert login(client, admin.username, admin.password).status_code == 303
    return client


def test_the_full_http_chain_activates_one_pair_and_then_protects_it(
    open_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.eligible("BTC-USDC")
    version = env.get(pair_id).version
    page = open_client.get(f"/pairs/{pair_id}/activate/request").text
    assert "<code>ACTIVATE PAPER PAIR BTC-USDC</code>" in page and "Step 2" in page
    assert (
        post(
            open_client,
            f"/pairs/{pair_id}/activate/reauth",
            version=version,
            password=GOOD_PASSWORD,
        ).status_code
        == 303
    )
    done = post(
        open_client,
        f"/pairs/{pair_id}/activate/confirm",
        version=version,
        confirmation="ACTIVATE PAPER PAIR BTC-USDC",
    )
    assert done.status_code == 303 and done.headers["location"].endswith("msg=pair_activated")
    assert env.state(pair_id) is PairState.PAPER_ACTIVE
    listing = open_client.get("/pairs").text
    assert "PAPER_ACTIVE" in listing and "BTC-USDC" in listing
    detail = open_client.get(f"/pairs/{pair_id}").text
    assert "activated for paper trading" in open_client.get(done.headers["location"]).text
    assert (
        "Pause" in detail
        and "Archive" not in detail.split("Actions")[1].split("Product metadata")[0]
    )
    # the active pair cannot be archived over HTTP either
    post(
        open_client, f"/pairs/{pair_id}/archive/reauth", version=version + 1, password=GOOD_PASSWORD
    )
    refused = post(
        open_client,
        f"/pairs/{pair_id}/archive/confirm",
        version=version + 1,
        confirmation="ARCHIVE PAIR BTC-USDC",
    )
    assert refused.status_code == 409 and env.state(pair_id) is PairState.PAPER_ACTIVE
    assert "ACTIVE_PAIR_CANNOT_ARCHIVE" in denials(sql)


def test_a_second_activation_over_http_is_refused_with_the_reason_shown(
    open_client: TestClient, env: Any, sql: Sql
) -> None:
    env.make("BTC-USDC", PairState.PAPER_ACTIVE)
    second = env.eligible("ETH-USDC")
    version = env.get(second).version
    page = open_client.get(f"/pairs/{second}/activate/request").text
    assert "ANOTHER_PAIR_ACTIVE" in page and "Only one active pair is allowed" in page
    post(open_client, f"/pairs/{second}/activate/reauth", version=version, password=GOOD_PASSWORD)
    refused = post(
        open_client,
        f"/pairs/{second}/activate/confirm",
        version=version,
        confirmation="ACTIVATE PAPER PAIR ETH-USDC",
    )
    assert refused.status_code == 409
    assert env.state(second) is PairState.PAPER_ELIGIBLE
    assert sql("SELECT count(*) AS n FROM pairs WHERE state = 'PAPER_ACTIVE'")[0]["n"] == 1


def test_pause_over_http_is_csrf_only(open_client: TestClient, env: Any, sql: Sql) -> None:
    pair_id = env.make("BTC-USDC", PairState.PAPER_ACTIVE)
    response = post(open_client, f"/pairs/{pair_id}/pause", version=env.get(pair_id).version)
    assert response.status_code == 303 and env.state(pair_id) is PairState.PAUSED
    assert len(audit(sql, "pair.paused")) == 1
