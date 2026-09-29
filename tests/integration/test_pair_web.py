"""The Pairs pages and actions over HTTP: roles, CSRF, the full chain, escaping, audit."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import Settings
from app.domain.pairs import PairAction, PairState
from tests.conftest import GOOD_PASSWORD, Account, FakeClock, csrf_from, walk_routes

Sql = Callable[..., list[dict[str, Any]]]


def token(client: TestClient) -> str:
    return csrf_from(client.get("/security").text)


def post(client: TestClient, path: str, **data: Any) -> Any:
    return client.post(path, data={"csrf_token": token(client), **data}, follow_redirects=False)


def reauth(
    client: TestClient, pair_id: UUID, action: str, version: int, password: str = GOOD_PASSWORD
) -> Any:
    return post(client, f"/pairs/{pair_id}/{action}/reauth", password=password, version=version)


def confirm(client: TestClient, pair_id: UUID, action: str, version: int, phrase: str) -> Any:
    return post(client, f"/pairs/{pair_id}/{action}/confirm", version=version, confirmation=phrase)


def audit(sql: Sql, code: str) -> list[dict[str, Any]]:
    return sql("SELECT * FROM audit_events WHERE event_code = %s ORDER BY seq", (code,))


def denials(sql: Sql) -> list[str]:
    return [r["reason_code"] for r in audit(sql, "pair.transition_denied")]


# ------------------------------------------------------------------ reading
def test_pair_pages_need_a_session(client: TestClient, env: Any) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    for path in ("/pairs", "/pairs/products", f"/pairs/{pair_id}"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"


def test_a_viewer_can_read_everything_and_change_nothing(
    viewer_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.eligible("BTC-USDC")
    for path in ("/pairs", "/pairs/products", f"/pairs/{pair_id}"):
        body = viewer_client.get(path).text
        assert viewer_client.get(path).status_code == 200
        assert 'action="/pairs' not in body, path  # no pair forms for a VIEWER
        assert "Add as candidate" not in body and "Queue validation" not in body
    # every write is refused for a VIEWER, and each refusal is audited
    version = env.get(pair_id).version
    for path, data in (
        ("/pairs/candidates", {"product_id": str(uuid4())}),
        (f"/pairs/{pair_id}/validate", {"version": version}),
        (f"/pairs/{pair_id}/pause", {"version": version}),
        (f"/pairs/{pair_id}/deactivate", {"version": version}),
        (f"/pairs/{pair_id}/disable/reauth", {"version": version, "password": GOOD_PASSWORD}),
        (
            f"/pairs/{pair_id}/disable/confirm",
            {"version": version, "confirmation": "DISABLE PAIR BTC-USDC"},
        ),
        (
            f"/pairs/{pair_id}/archive/confirm",
            {"version": version, "confirmation": "ARCHIVE PAIR BTC-USDC"},
        ),
    ):
        assert post(viewer_client, path, **data).status_code == 403, path
    assert viewer_client.get(f"/pairs/{pair_id}/disable/request").status_code == 403
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE
    assert {r["target_id"] for r in audit(sql, "authz.denied")} >= {
        "/pairs/candidates",
        "/pairs/{pair_id}/validate",
        "/pairs/{pair_id}/{action}/confirm",
    }


def test_the_list_shows_state_validation_and_metadata_age(
    admin_client: TestClient, env: Any
) -> None:
    pair_id = env.eligible("BTC-USDC")
    env.make("ETH-USDC", PairState.PROPOSED)
    body = admin_client.get("/pairs").text
    assert f'href="/pairs/{pair_id}"' in body
    assert "PAPER_ELIGIBLE" in body and "PROPOSED" in body and "PASS" in body
    assert "Not validated" in body and "UNIFIED_USD_BOOK" in body
    assert "Active pair" in body and "None" in body
    assert "LIVE_ELIGIBLE" in body and "LIVE_ACTIVE" in body and "blocked" in body


def test_the_products_page_lists_default_candidates_first_and_paginates(
    admin_client: TestClient, env: Any, coinbase: Any
) -> None:
    coinbase.products["OFF-USDC"] = {
        **coinbase.products["BTC-USDC"],
        "product_id": "OFF-USDC",
        "base_currency_id": "OFF",
        "status": "offline",
    }
    coinbase.products["USDT-USDC"] = {
        **coinbase.products["BTC-USDC"],
        "product_id": "USDT-USDC",
        "base_currency_id": "USDT",
        "alias": "",
    }
    for i in range(60):
        code = f"T{i:02d}"
        coinbase.products[f"{code}-USDC"] = {
            **coinbase.products["BTC-USDC"],
            "product_id": f"{code}-USDC",
            "base_currency_id": code,
        }
    env.discover()
    default = admin_client.get("/pairs/products").text
    assert "BTC-USDC" in default and "OFF-USDC" not in default and "USDT-USDC" not in default
    everything = admin_client.get("/pairs/products?show=all&page=3").text
    assert "Page 3 of 3" in everything
    all_first = admin_client.get("/pairs/products?show=all").text
    assert "OFF-USDC" in all_first or "OFF-USDC" in everything
    assert "STATUS_NOT_ALLOWED" in all_first + everything or "STABLE_BASE" in all_first + everything


@pytest.mark.parametrize(
    "query", ["page=0", "page=-1", "page=abc", "page=99999", "show=evil", "show="]
)
def test_bad_product_page_queries_are_rejected(admin_client: TestClient, query: str) -> None:
    assert admin_client.get(f"/pairs/products?{query}").status_code == 400


def test_the_admin_sees_add_buttons_only_for_products_without_an_open_pair(
    admin_client: TestClient, env: Any
) -> None:
    env.discover()
    env.propose("BTC-USDC")
    body = admin_client.get("/pairs/products").text
    assert body.count("Add as candidate") == 2  # ETH and SOL, not BTC
    assert 'aria-label="Add BTC-USDC as a candidate"' not in body


# ------------------------------------------------------------------ propose
def test_adding_a_candidate_creates_proposed_only(
    admin_client: TestClient, admin: Account, env: Any, sql: Sql
) -> None:
    env.discover()
    product = env.product_uuid("BTC-USDC")
    response = post(admin_client, "/pairs/candidates", product_id=str(product))
    assert response.status_code == 303
    row = sql("SELECT * FROM pairs")[0]
    assert response.headers["location"] == f"/pairs/{row['id']}?msg=pair_proposed"
    assert (row["state"], row["version"], row["proposed_via"]) == ("PROPOSED", 1, "WEB")
    assert (
        row["proposed_by"] == admin.user.id
        and row["ever_active"] is False
        and row["eligible_run_id"] is None
    )
    assert sql("SELECT count(*) AS n FROM pair_validation_runs")[0]["n"] == 0
    event = audit(sql, "pair.proposed")[0]
    assert event["actor_role"] == "ADMIN" and event["target_id"] == str(row["id"])
    page = admin_client.get(response.headers["location"]).text
    assert "Candidate added as PROPOSED" in page and "will not trade" in page


def test_adding_never_validates_or_activates(admin_client: TestClient, env: Any, sql: Sql) -> None:
    env.discover()
    for product_id in ("BTC-USDC", "ETH-USDC", "SOL-USDC"):
        post(admin_client, "/pairs/candidates", product_id=str(env.product_uuid(product_id)))
    assert {r["state"] for r in sql("SELECT state FROM pairs")} == {"PROPOSED"}
    assert audit(sql, "pair.activated_paper") == [] and audit(sql, "pair.validation_started") == []


def test_adding_the_same_product_twice_is_refused_and_audited(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    env.discover()
    product = str(env.product_uuid("BTC-USDC"))
    assert post(admin_client, "/pairs/candidates", product_id=product).status_code == 303
    again = post(admin_client, "/pairs/candidates", product_id=product)
    assert again.status_code == 409 and "already a candidate" in again.text
    assert sql("SELECT count(*) AS n FROM pairs")[0]["n"] == 1
    assert denials(sql) == ["ALREADY_A_CANDIDATE"]


def test_an_unknown_product_id_is_a_404_style_refusal(admin_client: TestClient, env: Any) -> None:
    env.discover()
    response = post(admin_client, "/pairs/candidates", product_id=str(uuid4()))
    assert response.status_code == 404


@pytest.mark.parametrize(
    "value", ["BTC-USDC", "1", "'; DROP TABLE pairs;--", "", "<script>", "../x"]
)
def test_only_a_product_uuid_is_accepted_never_free_text(
    admin_client: TestClient, env: Any, value: str
) -> None:
    env.discover()
    assert post(admin_client, "/pairs/candidates", product_id=value).status_code == 400


def test_unknown_form_fields_are_rejected(admin_client: TestClient, env: Any) -> None:
    env.discover()
    response = post(
        admin_client,
        "/pairs/candidates",
        product_id=str(env.product_uuid("BTC-USDC")),
        state="PAPER_ACTIVE",
    )
    assert response.status_code == 400


def test_the_open_pair_cap_is_enforced(
    settings: Settings,
    storage: Any,
    clock: FakeClock,
    env: Any,
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin: Account,
    sql: Sql,
) -> None:
    capped = settings.model_copy(
        update={"pair_policy": settings.pair_policy.model_copy(update={"max_pairs": 2})}
    )
    app = create_app(capped, storage=storage, clock=clock)
    client = TestClient(
        app,
        base_url="https://testserver",
        raise_server_exceptions=False,
        client=("10.0.0.9", 50000),
        headers={"origin": "https://testserver"},
    )
    assert login(client, admin.username, admin.password).status_code == 303
    env.discover()
    for product_id in ("BTC-USDC", "ETH-USDC"):
        assert (
            post(
                client, "/pairs/candidates", product_id=str(env.product_uuid(product_id))
            ).status_code
            == 303
        )
    refused = post(client, "/pairs/candidates", product_id=str(env.product_uuid("SOL-USDC")))
    assert refused.status_code == 409 and "maximum number of open pairs" in refused.text
    assert denials(sql) == ["PAIR_CAP_REACHED"]
    # archiving one frees a slot
    first = sql("SELECT id FROM pairs ORDER BY proposed_at LIMIT 1")[0]["id"]
    env.act(first, PairAction.ARCHIVE)
    assert (
        post(client, "/pairs/candidates", product_id=str(env.product_uuid("SOL-USDC"))).status_code
        == 303
    )


# ------------------------------------------------------------------ queue validation
def test_queueing_validation_is_a_csrf_only_action_and_creates_no_evidence(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    response = post(admin_client, f"/pairs/{pair_id}/validate", version=1)
    assert response.status_code == 303 and "pair_validation_queued" in response.headers["location"]
    assert env.state(pair_id) is PairState.VALIDATING
    assert sql("SELECT count(*) AS n FROM pair_validation_runs")[0]["n"] == 0
    assert audit(sql, "pair.validation_started")[0]["reason_code"] == "VALIDATE"


def test_a_stale_page_cannot_act(admin_client: TestClient, env: Any, sql: Sql) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    response = post(admin_client, f"/pairs/{pair_id}/validate", version=7)
    assert response.status_code == 409 and "out of date" in response.text
    assert env.state(pair_id) is PairState.PROPOSED and denials(sql) == ["STALE_VERSION"]


def test_actions_refused_from_the_wrong_state(admin_client: TestClient, env: Any, sql: Sql) -> None:
    pair_id = env.eligible("BTC-USDC")
    version = env.get(pair_id).version
    for action in ("validate", "pause", "deactivate"):
        response = post(admin_client, f"/pairs/{pair_id}/{action}", version=version)
        assert response.status_code == 409, action
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE
    assert denials(sql) == ["ILLEGAL_TRANSITION"] * 3


def test_unknown_pairs_are_404(admin_client: TestClient) -> None:
    ghost = uuid4()
    assert admin_client.get(f"/pairs/{ghost}").status_code == 404
    assert post(admin_client, f"/pairs/{ghost}/validate", version=1).status_code == 404
    assert admin_client.get(f"/pairs/{ghost}/disable/request").status_code == 404
    assert confirm(admin_client, ghost, "disable", 1, "DISABLE PAIR X").status_code == 404


def test_bad_path_parameters_are_rejected(admin_client: TestClient, env: Any) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    assert admin_client.get("/pairs/not-a-uuid").status_code == 400
    assert admin_client.get(f"/pairs/{pair_id}/delete/request").status_code == 400
    assert (
        admin_client.get(f"/pairs/{pair_id}/validate/request").status_code == 404
    )  # not a full-chain action
    assert (
        post(
            admin_client, f"/pairs/{pair_id}/validate/confirm", version=1, confirmation="x"
        ).status_code
        == 404
    )


# ------------------------------------------------------------------ detail page (requirement 13)
def test_the_detail_page_shows_lifecycle_validation_metadata_history_reasons_transitions_and_audit(
    admin_client: TestClient, env: Any
) -> None:
    pair_id = env.eligible("BTC-USDC")
    body = admin_client.get(f"/pairs/{pair_id}").text
    for name in (
        "DISCOVERED",
        "PROPOSED",
        "VALIDATING",
        "RESEARCH_ONLY",
        "PAPER_ELIGIBLE",
        "PAPER_ACTIVE",
        "LIVE_ELIGIBLE",
        "LIVE_ACTIVE",
    ):
        assert name in body
    assert body.count("blocked in this build") == 2
    assert 'aria-current="step"' in body
    assert (
        "Metadata age" in body
        and "UNIFIED_USD_BOOK" in body
        and "priced from the unified USD book" in body
    )
    assert "Daily history" in body and "Five-minute candles" in body
    assert "Validation results" in body and body.count('class="badge check-pass"') == 14
    for label in (
        "Product status and flags",
        "Quote currency",
        "Fee viability",
        "50 / 15 / 35 USDC feasibility",
        "Liquidity and spread",
        "OHLCV quality",
        "Metadata freshness",
        "Precision",
    ):
        assert label in body
    assert "<code>OK</code>" in body and "VALID (passing, unexpired, current metadata)" in body
    assert (
        "PROPOSED -&gt; VALIDATING" in body.replace("→", "->") or "PROPOSED -> VALIDATING" in body
    )
    assert "pair.proposed" in body and "pair.paper_eligible" in body and "host CLI" in body
    assert (
        "Currently refused" in body and "MODE_NOT_PAPER" in body
    )  # activation is shown as refused


def test_metadata_staleness_is_shown(admin_client: TestClient, env: Any, sql: Sql) -> None:
    pair_id = env.eligible("BTC-USDC")
    # age the metadata (advancing the clock would also end the 30-minute idle session)
    sql(
        "UPDATE product_metadata_current SET last_verified_at = last_verified_at - interval '2 hours'"
    )
    body = admin_client.get(f"/pairs/{pair_id}").text
    assert "(stale)" in body and "2 h" in body
    assert "not valid" in body or "METADATA_STALE" in body


def test_a_viewer_gets_the_history_but_a_redacted_audit_timeline(
    viewer_client: TestClient, admin: Account, env: Any
) -> None:
    pair_id = env.eligible("BTC-USDC")
    body = viewer_client.get(f"/pairs/{pair_id}").text
    assert "State transitions" in body and "PAPER_ELIGIBLE" in body and "VALIDATION_PASS" in body
    assert "shown to ADMIN users" in body
    assert "pair.proposed" not in body
    assert admin.username not in body  # a VIEWER never learns which ADMIN acted
    assert "t" * 12 not in body  # no client tag
    assert 'action="/pairs' not in body


def test_failed_validation_reasons_are_visible(
    admin_client: TestClient, env: Any, coinbase: Any
) -> None:
    coinbase.drop_daily = 30
    env.discover()
    pair_id = env.propose("BTC-USDC")
    env.queue(pair_id)
    env.validate()
    body = admin_client.get(f"/pairs/{pair_id}").text
    assert "RESEARCH_ONLY" in body and "HISTORY_TOO_SHORT" in body
    assert 'class="badge check-fail"' in body and "Fewer daily candles than required" in body
    assert "not valid" in body


# ------------------------------------------------------------------ escaping (requirement 14)
HOSTILE = "<script>alert('x')</script><img src=x onerror=alert(2)>"


def plant_hostile_content(sql: Sql, pair_id: UUID) -> None:
    """Content only an attacker-controlled exchange (or a compromised runner) could produce."""
    product = sql("SELECT product_uuid FROM pairs WHERE id = %s", (pair_id,))[0]["product_uuid"]
    old = sql(
        "SELECT snapshot_id FROM product_metadata_current WHERE product_uuid = %s", (product,)
    )[0]["snapshot_id"]
    new = uuid4()
    sql(
        "INSERT INTO product_metadata_snapshots (id, product_uuid, sha256, status, alias_to, malformed, "
        "first_seen_at, base_increment, quote_increment, price_increment, base_min_size) "
        "SELECT %s, product_uuid, repeat('a', 64), %s, %s, %s, first_seen_at, base_increment, "
        "quote_increment, price_increment, base_min_size FROM product_metadata_snapshots WHERE id = %s",
        (new, HOSTILE, [HOSTILE], [HOSTILE], old),
    )
    sql(
        "UPDATE product_metadata_current SET snapshot_id = %s WHERE product_uuid = %s",
        (new, product),
    )
    checks = [
        {
            "code": "PRODUCT_STATUS",
            "status": "FAIL",
            "reason": HOSTILE,
            "message": HOSTILE,
            "observed": {HOSTILE: HOSTILE},
        }
    ]
    sql(
        "INSERT INTO pair_validation_runs (id, pair_id, pair_version, snapshot_id, started_at, "
        "finished_at, outcome, checks, thresholds_sha256, expires_at) VALUES (%s, %s, 1, %s, now(), now(), "
        "'FAIL', %s::jsonb, repeat('b', 64), now() + interval '1 day')",
        (uuid4(), pair_id, new, json.dumps(checks)),
    )


def assert_escaped(body: str) -> None:
    assert "<script>alert" not in body and "<img" not in body
    assert "&lt;script&gt;" in body and "&lt;img" in body


def test_exchange_and_stored_content_is_escaped_everywhere(
    admin_client: TestClient, viewer_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    plant_hostile_content(sql, pair_id)
    for client in (admin_client, viewer_client):
        for path in (f"/pairs/{pair_id}", "/pairs/products?show=all", "/pairs"):
            response = client.get(path)
            assert response.status_code == 200, path
            if path != "/pairs":
                assert_escaped(response.text)
            assert "<script>alert" not in response.text and "<img src=x" not in response.text
            assert response.headers["content-security-policy"].startswith("default-src 'none'")
            assert not re.search(r"<script(?![^>]*\ssrc=)", response.text)


def test_the_confirmation_page_escapes_and_shows_the_exact_phrase(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    plant_hostile_content(sql, pair_id)
    body = admin_client.get(f"/pairs/{pair_id}/archive/request").text
    assert "<code>ARCHIVE PAIR BTC-USDC</code>" in body
    assert "<script>alert" not in body


def test_flash_codes_never_reflect_input(admin_client: TestClient, env: Any) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    for msg in ("<script>alert(1)</script>", "r:<x>", "r:NOPE", "pair_proposed" * 5, "%00"):
        response = admin_client.get(f"/pairs/{pair_id}", params={"msg": msg})
        if response.status_code == 200:
            assert "<script>alert" not in response.text and "&lt;x&gt;" not in response.text


# ------------------------------------------------------------------ the full chain: disable and archive
def request_page(client: TestClient, pair_id: UUID, action: str) -> str:
    response = client.get(f"/pairs/{pair_id}/{action}/request")
    assert response.status_code == 200, response.text
    body: str = response.text
    return body


def test_get_requests_never_change_anything(admin_client: TestClient, env: Any, sql: Sql) -> None:
    pair_id = env.make("BTC-USDC", PairState.PAPER_ELIGIBLE)
    before = sql("SELECT count(*) AS n FROM audit_events")[0]["n"], env.get(pair_id).version
    for action in ("activate", "disable", "archive"):
        assert admin_client.get(f"/pairs/{pair_id}/{action}/request").status_code == 200
    admin_client.get(f"/pairs/{pair_id}")
    admin_client.get("/pairs")
    admin_client.get("/pairs/products")
    assert (
        sql("SELECT count(*) AS n FROM audit_events")[0]["n"],
        env.get(pair_id).version,
    ) == before


def test_the_confirmation_page_teaches_the_two_steps(admin_client: TestClient, env: Any) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    body = request_page(admin_client, pair_id, "disable")
    assert "Step 1: confirm your password" in body and "Step 2: type the phrase" in body
    assert "<code>DISABLE PAIR BTC-USDC</code>" in body and "not confirmed" in body
    assert f'action="/pairs/{pair_id}/disable/reauth"' in body
    assert f'action="/pairs/{pair_id}/disable/confirm"' in body
    assert f'name="version" value="{env.get(pair_id).version}"' in body


def test_disabling_needs_password_phrase_and_csrf_and_writes_history_and_audit(
    admin_client: TestClient, admin: Account, env: Any, sql: Sql
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    version = env.get(pair_id).version
    assert reauth(admin_client, pair_id, "disable", version).status_code == 303
    assert "confirmed (single use)" in request_page(admin_client, pair_id, "disable")
    done = confirm(admin_client, pair_id, "disable", version, "DISABLE PAIR BTC-USDC")
    assert (
        done.status_code == 303
        and done.headers["location"] == f"/pairs/{pair_id}?msg=pair_disabled"
    )
    pair = env.get(pair_id)
    assert pair.state is PairState.DISABLED and pair.version == version + 1
    event = audit(sql, "pair.disabled")[0]
    assert event["actor_role"] == "ADMIN" and str(event["actor_user_id"]) == str(admin.user.id)
    assert (
        event["target_type"] == "pair"
        and event["target_id"] == str(pair_id)
        and event["result"] == "SUCCESS"
    )
    assert json.loads(event["detail"]) == {
        "product": "BTC-USDC",
        "from": "PROPOSED",
        "to": "DISABLED",
        "version": version + 1,
    }
    row = sql(
        "SELECT * FROM pair_state_history WHERE pair_id = %s ORDER BY version_after DESC",
        (pair_id,),
    )[0]
    assert (row["state_after"], row["actor_class"], row["transition_no"]) == ("DISABLED", "WEB", 12)
    assert row["audit_seq"] == event["seq"]
    assert "was disabled" in admin_client.get(done.headers["location"]).text


def test_without_a_fresh_password_the_action_is_refused_and_audited(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    version = env.get(pair_id).version
    response = confirm(admin_client, pair_id, "disable", version, "DISABLE PAIR BTC-USDC")
    assert response.status_code == 400 and "Confirm your password first" in response.text
    assert env.state(pair_id) is PairState.PROPOSED
    assert denials(sql) == ["REAUTH_REQUIRED"] and audit(sql, "pair.disabled") == []


def test_a_wrong_phrase_is_refused_and_does_not_burn_the_password_confirmation(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    version = env.get(pair_id).version
    reauth(admin_client, pair_id, "disable", version)
    wrong = confirm(admin_client, pair_id, "disable", version, "DISABLE PAIR ETH-USDC")
    assert wrong.status_code == 400 and "did not match" in wrong.text
    assert env.state(pair_id) is PairState.PROPOSED
    ok = confirm(
        admin_client, pair_id, "disable", version, "DISABLE PAIR BTC-USDC"
    )  # same confirmation
    assert ok.status_code == 303 and env.state(pair_id) is PairState.DISABLED
    assert denials(sql) == ["PHRASE_MISMATCH"]


@pytest.mark.parametrize(
    "phrase",
    [
        "disable pair btc-usdc",
        "Disable Pair BTC-USDC",
        " DISABLE PAIR BTC-USDC",
        "DISABLE PAIR BTC-USDC ",
        "DISABLE PAIR BTC-USDC\n",
        "DISABLE  PAIR BTC-USDC",
        "DISABLE PAIR BTC‑USDC",
        "DISABLE PAIR ВTC-USDC",
        "ARCHIVE PAIR BTC-USDC",
        "DISABLE PAIR ETH-USDC",
        "DISABLE PAIR",
        "",
        "DISABLE PAIR BTC-USDC" * 2,
    ],
)
def test_phrase_must_match_byte_for_byte(admin_client: TestClient, env: Any, phrase: str) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    version = env.get(pair_id).version
    reauth(admin_client, pair_id, "disable", version)
    response = confirm(admin_client, pair_id, "disable", version, phrase)
    assert response.status_code in (400, 200) and env.state(pair_id) is PairState.PROPOSED


def test_the_password_confirmation_is_single_use(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    first = env.make("BTC-USDC", PairState.PROPOSED)
    second = env.make("ETH-USDC", PairState.PROPOSED)
    reauth(admin_client, first, "disable", env.get(first).version)
    assert (
        confirm(
            admin_client, first, "disable", env.get(first).version, "DISABLE PAIR BTC-USDC"
        ).status_code
        == 303
    )
    again = confirm(
        admin_client, second, "disable", env.get(second).version, "DISABLE PAIR ETH-USDC"
    )
    assert again.status_code == 400 and env.state(second) is PairState.PROPOSED
    assert denials(sql) == ["REAUTH_REQUIRED"]


def test_a_wrong_password_grants_no_confirmation(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    version = env.get(pair_id).version
    bad = reauth(admin_client, pair_id, "disable", version, password="wrong password entirely")
    assert bad.status_code == 400 and "password is incorrect" in bad.text
    response = confirm(admin_client, pair_id, "disable", version, "DISABLE PAIR BTC-USDC")
    assert response.status_code == 400 and env.state(pair_id) is PairState.PROPOSED
    assert [r["reason_code"] for r in audit(sql, "auth.reauth.failure")] == ["BAD_PASSWORD"]


def test_the_password_confirmation_expires(
    admin_client: TestClient, env: Any, clock: FakeClock
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    version = env.get(pair_id).version
    reauth(admin_client, pair_id, "disable", version)
    clock.advance(121)
    response = confirm(admin_client, pair_id, "disable", version, "DISABLE PAIR BTC-USDC")
    assert response.status_code == 400 and env.state(pair_id) is PairState.PROPOSED


def test_a_stale_confirmation_page_cannot_disable_a_changed_pair(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    stale_version = env.get(pair_id).version
    env.queue(pair_id)  # the pair moved on after the page was rendered
    reauth(admin_client, pair_id, "disable", stale_version)
    response = confirm(admin_client, pair_id, "disable", stale_version, "DISABLE PAIR BTC-USDC")
    assert response.status_code == 409 and env.state(pair_id) is PairState.VALIDATING
    assert denials(sql) == ["STALE_VERSION"]


def test_archiving_keeps_everything_and_is_terminal(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.eligible("BTC-USDC")
    runs_before = sql("SELECT count(*) AS n FROM pair_validation_runs")[0]["n"]
    version = env.get(pair_id).version
    reauth(admin_client, pair_id, "archive", version)
    done = confirm(admin_client, pair_id, "archive", version, "ARCHIVE PAIR BTC-USDC")
    assert done.status_code == 303 and env.state(pair_id) is PairState.ARCHIVED
    # nothing was deleted
    assert sql("SELECT count(*) AS n FROM pairs")[0]["n"] == 1
    assert sql("SELECT count(*) AS n FROM pair_validation_runs")[0]["n"] == runs_before
    assert (
        sql("SELECT count(*) AS n FROM pair_state_history WHERE pair_id = %s", (pair_id,))[0]["n"]
        == 4
    )
    assert audit(sql, "pair.archived")[0]["target_id"] == str(pair_id)
    body = admin_client.get(f"/pairs/{pair_id}").text
    assert "ARCHIVED" in body and "Validation results" in body and "No action is available" in body
    # terminal: no action works, and every attempt is refused and audited
    assert admin_client.get(f"/pairs/{pair_id}/disable/request").status_code == 404
    reauth_after = env.get(pair_id).version
    for action, phrase in (
        ("disable", "DISABLE PAIR BTC-USDC"),
        ("archive", "ARCHIVE PAIR BTC-USDC"),
        ("reenable", "REENABLE PAIR BTC-USDC"),
    ):
        response = confirm(admin_client, pair_id, action, reauth_after, phrase)
        assert response.status_code == 409, action
    assert (
        set(denials(sql)) == {"ARCHIVED_IS_TERMINAL"} and env.state(pair_id) is PairState.ARCHIVED
    )
    for action in ("validate", "pause", "deactivate"):
        assert (
            post(admin_client, f"/pairs/{pair_id}/{action}", version=reauth_after).status_code
            == 409
        )


def test_re_adding_an_archived_product_creates_a_new_linked_candidate(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    old = env.make("BTC-USDC", PairState.PROPOSED)
    reauth(admin_client, old, "archive", 1)
    confirm(admin_client, old, "archive", 1, "ARCHIVE PAIR BTC-USDC")
    response = post(admin_client, "/pairs/candidates", product_id=str(env.product_uuid("BTC-USDC")))
    assert response.status_code == 303
    rows = sql("SELECT id, state, successor_of FROM pairs ORDER BY proposed_at, state")
    new = next(r for r in rows if r["state"] == "PROPOSED")
    assert new["id"] != old and str(new["successor_of"]) == str(old)
    assert env.state(old) is PairState.ARCHIVED


def test_an_active_pair_cannot_be_archived_or_disabled(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PAPER_ACTIVE)
    version = env.get(pair_id).version
    for action, phrase in (
        ("archive", "ARCHIVE PAIR BTC-USDC"),
        ("disable", "DISABLE PAIR BTC-USDC"),
    ):
        reauth(admin_client, pair_id, action, version)
        response = confirm(admin_client, pair_id, action, version, phrase)
        assert response.status_code == 409
        assert (
            f"An active pair cannot be {'archived' if action == 'archive' else 'disabled'}"
            in response.text
        )
        assert admin_client.get(f"/pairs/{pair_id}/{action}/request").status_code == 404
    assert env.state(pair_id) is PairState.PAPER_ACTIVE
    assert denials(sql) == ["ACTIVE_PAIR_CANNOT_ARCHIVE", "ACTIVE_PAIR_CANNOT_DISABLE"]
    assert audit(sql, "pair.archived") == [] and audit(sql, "pair.disabled") == []


def test_reenabling_needs_its_own_phrase_and_revalidates(
    admin_client: TestClient, env: Any
) -> None:
    pair_id = env.make("BTC-USDC", PairState.DISABLED)
    version = env.get(pair_id).version
    reauth(admin_client, pair_id, "reenable", version)
    assert (
        confirm(admin_client, pair_id, "reenable", version, "DISABLE PAIR BTC-USDC").status_code
        == 400
    )
    ok = confirm(admin_client, pair_id, "reenable", version, "REENABLE PAIR BTC-USDC")
    assert ok.status_code == 303 and env.state(pair_id) is PairState.VALIDATING


def test_a_disabled_pair_can_be_archived(admin_client: TestClient, env: Any, sql: Sql) -> None:
    disabled = env.make("BTC-USDC", PairState.DISABLED)
    version = env.get(disabled).version
    reauth(admin_client, disabled, "archive", version)
    assert (
        confirm(admin_client, disabled, "archive", version, "ARCHIVE PAIR BTC-USDC").status_code
        == 303
    )
    assert env.state(disabled) is PairState.ARCHIVED


def test_a_previously_active_pair_cannot_be_archived_while_it_holds_paper_state(
    admin_client: TestClient, env: Any, sql: Sql
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PAUSED)  # ever active
    sql(
        "INSERT INTO paper_positions (pair_id, base_qty, cost_basis, updated_at) "
        "VALUES (%s, 1, 10, now())",
        (pair_id,),
    )  # paper inventory left over: not clean
    version = env.get(pair_id).version
    reauth(admin_client, pair_id, "archive", version)
    response = confirm(admin_client, pair_id, "archive", version, "ARCHIVE PAIR BTC-USDC")
    assert response.status_code == 409 and env.state(pair_id) is PairState.PAUSED
    assert denials(sql) == ["GUARD_FAILED"]
    assert "PAPER_INVENTORY" in admin_client.get(f"/pairs/{pair_id}/archive/request").text


def test_there_is_no_way_to_hard_delete_a_pair(
    admin_client: TestClient, app: Any, env: Any, sql: Sql
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    methods = {m for r in walk_routes(app) if r.path.startswith("/pairs") for m in r.methods}
    assert not methods & {"DELETE", "PUT", "PATCH"}
    for method in ("DELETE", "PUT", "PATCH"):
        response = admin_client.request(
            method, f"/pairs/{pair_id}", headers={"x-csrf-token": token(admin_client)}
        )
        assert response.status_code in (404, 405)
    assert sql("SELECT count(*) AS n FROM pairs")[0]["n"] == 1


def test_csrf_and_origin_protect_every_pair_write(
    admin_client: TestClient,
    make_client: Callable[..., TestClient],
    admin: Account,
    login: Callable[..., Any],
    env: Any,
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PROPOSED)
    for path, data in (
        ("/pairs/candidates", {"product_id": str(env.product_uuid("ETH-USDC"))}),
        (f"/pairs/{pair_id}/validate", {"version": 1}),
        (f"/pairs/{pair_id}/disable/reauth", {"version": 1, "password": GOOD_PASSWORD}),
        (
            f"/pairs/{pair_id}/disable/confirm",
            {"version": 1, "confirmation": "DISABLE PAIR BTC-USDC"},
        ),
    ):
        assert admin_client.post(path, data=data).status_code == 403, path  # no token
        assert admin_client.post(path, data={**data, "csrf_token": "x" * 43}).status_code == 403
        evil = admin_client.post(
            path,
            data={**data, "csrf_token": token(admin_client)},
            headers={"origin": "https://evil.example"},
        )
        assert evil.status_code == 403, path
    assert env.state(pair_id) is PairState.PROPOSED


def test_no_pair_page_or_action_can_reach_orders_or_the_exchange(
    admin_client: TestClient, env: Any
) -> None:
    pair_id = env.eligible("BTC-USDC")
    pages = "\n".join(
        admin_client.get(p).text.lower()
        for p in (
            "/pairs",
            "/pairs/products",
            f"/pairs/{pair_id}",
            f"/pairs/{pair_id}/archive/request",
        )
    )
    for word in (
        "place order",
        "cancel order",
        "submit order",
        "create order",
        "enable live",
        "resume bot",
        "start bot",
        "kill switch",
        "sell",
        "buy now",
    ):
        assert word not in pages, word
    assert not re.search(
        r'action="/(orders?|exchange|bot|config|kill|mode|reports?|review|proposals?)', pages
    )
