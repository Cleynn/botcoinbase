"""The review package pages and downloads over HTTP (ADMIN only, full chain, protected)."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import Settings
from app.review.builder import ReviewBuilder
from app.review.service import PHRASES
from app.storage.database import Storage
from tests.conftest import GOOD_PASSWORD, ORIGIN, Account, FakeClock, csrf_from
from tests.integration.test_review_service import fingerprint

Sql = Callable[..., list[dict[str, Any]]]
DAY = date(2026, 9, 29)  # the fake clock's "today"


@pytest.fixture
def rsettings(settings: Settings, tmp_path: Path) -> Settings:
    review = settings.review.model_copy(update={"dir": str(tmp_path / "review")})
    return settings.model_copy(update={"review": review})


@pytest.fixture
def rapp(rsettings: Settings, storage: Storage, clock: FakeClock) -> Any:
    return create_app(rsettings, storage=storage, clock=clock)


@pytest.fixture
def rbuilder(rsettings: Settings, ctl_storage: Storage, clock: FakeClock) -> ReviewBuilder:
    return ReviewBuilder(storage=ctl_storage, clock=clock, settings=rsettings)


def session(app: Any, login: Callable[..., Any], account: Account, peer: str) -> TestClient:
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
def ac(rapp: Any, login: Callable[..., Any], admin: Account) -> TestClient:
    return session(rapp, login, admin, "10.0.0.5")


@pytest.fixture
def vc(rapp: Any, login: Callable[..., Any], viewer: Account) -> TestClient:
    return session(rapp, login, viewer, "10.0.0.6")


def token(client: TestClient) -> str:
    return csrf_from(client.get("/review/disable/request").text)


def reauth(client: TestClient, prefix: str, fields: dict[str, Any], pw: str = GOOD_PASSWORD) -> Any:
    return client.post(
        f"{prefix}/reauth",
        data={"csrf_token": token(client), "password": pw, **fields},
        follow_redirects=False,
    )


def confirm(client: TestClient, prefix: str, fields: dict[str, Any], phrase: str) -> Any:
    return client.post(
        f"{prefix}/confirm",
        data={"csrf_token": token(client), "confirmation": phrase, **fields},
        follow_redirects=False,
    )


def enable(client: TestClient, days: int = 14) -> None:
    assert reauth(client, "/review/enable", {"retention_days": days}).status_code == 303
    got = confirm(client, "/review/enable", {"retention_days": days}, PHRASES["enable"])
    assert got.status_code == 303 and got.headers["location"] == "/review?msg=review_enabled"


CREATE_FIELDS = {"period_start": "2026-09-01", "period_end": "2026-09-29", "scope": ["backtests"]}


def create(client: TestClient) -> str:
    assert reauth(client, "/review/packages/create", CREATE_FIELDS).status_code == 303
    got = confirm(client, "/review/packages/create", CREATE_FIELDS, PHRASES["create"])
    assert got.status_code == 303, got.text
    return re.search(r"/review/packages/([0-9a-f-]{36})", got.headers["location"]).group(1)  # type: ignore[union-attr]


ROUTES = (
    ("GET", "/review"),
    ("GET", "/review/enable/request"),
    ("GET", "/review/disable/request"),
    ("GET", f"/review/packages/create/request?period_start={DAY}&period_end={DAY}&scope=paper"),
    ("GET", f"/review/packages/{uuid4()}"),
    ("POST", "/review/enable/reauth"),
    ("POST", "/review/enable/confirm"),
    ("POST", "/review/disable/reauth"),
    ("POST", "/review/disable/confirm"),
    ("POST", "/review/packages/create/reauth"),
    ("POST", "/review/packages/create/confirm"),
    ("POST", f"/review/packages/{uuid4()}/verify"),
    ("POST", f"/review/packages/{uuid4()}/download"),
)


# ------------------------------------------------------------------ access
@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_viewers_are_refused_everywhere(vc: TestClient, method: str, path: str) -> None:
    resp = vc.request(method, path, data={"csrf_token": "x"} if method == "POST" else None)
    assert (
        resp.status_code in (403, 404) and "review" not in resp.text.lower().split("<main")[-1][:0]
    )


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_anonymous_visitors_get_nothing(rapp: Any, method: str, path: str) -> None:
    anon = TestClient(
        rapp,
        base_url="https://testserver",
        raise_server_exceptions=False,
        client=("10.0.0.9", 50000),
        headers={"origin": ORIGIN},
    )
    resp = anon.request(method, path, follow_redirects=False)
    assert resp.status_code in (303, 401, 403)
    assert "Review package" not in resp.text


def test_the_menu_shows_review_packages_only_to_admins(ac: TestClient, vc: TestClient) -> None:
    assert 'href="/review"' in ac.get("/").text
    assert 'href="/review"' not in vc.get("/").text


def test_denials_are_audited(vc: TestClient, sql: Sql) -> None:
    vc.get("/review")
    denied = sql("SELECT * FROM audit_events WHERE event_code = 'authz.denied'")
    assert denied and denied[-1]["actor_role"] == "VIEWER"


# ------------------------------------------------------------------ the pages say what they are
def test_disabled_by_default_and_the_page_says_it_cannot_trade(ac: TestClient) -> None:
    page = ac.get("/review").text
    assert "DISABLED" in page
    assert "cannot trade" in page and "cannot change the bot" in page
    assert "Request a package" not in page  # no create form while disabled
    assert "Enable review packages" in page


def test_every_review_page_states_the_package_cannot_trade(ac: TestClient) -> None:
    for path in ("/review", "/review/enable/request", "/review/disable/request"):
        assert "cannot trade" in ac.get(path).text, path


# ------------------------------------------------------------------ enable / disable chain
def test_enable_needs_password_then_the_exact_phrase(ac: TestClient, sql: Sql) -> None:
    fields = {"retention_days": 7}
    assert reauth(ac, "/review/enable", fields, pw="wrong password").status_code == 400
    early = confirm(ac, "/review/enable", fields, PHRASES["enable"])
    assert early.status_code == 400 and "Confirm your password first" in early.text
    assert reauth(ac, "/review/enable", fields).status_code == 303
    bad = confirm(ac, "/review/enable", fields, "enable read-only review packages")
    assert bad.status_code == 400 and "did not match exactly" in bad.text
    assert sql("SELECT enabled FROM review_settings")[0]["enabled"] is False
    ok = confirm(ac, "/review/enable", fields, PHRASES["enable"])
    assert ok.status_code == 303
    assert sql("SELECT enabled, retention_days FROM review_settings")[0] == {
        "enabled": True,
        "retention_days": 7,
    }
    assert "ENABLED" in ac.get("/review").text


def test_the_password_confirmation_is_single_use(ac: TestClient) -> None:
    enable(ac)
    again = confirm(ac, "/review/disable", {}, PHRASES["disable"])
    assert again.status_code == 400 and "Confirm your password first" in again.text


def test_disable_has_its_own_chain_and_phrase(ac: TestClient, sql: Sql) -> None:
    enable(ac)
    assert reauth(ac, "/review/disable", {}).status_code == 303
    wrong = confirm(ac, "/review/disable", {}, PHRASES["enable"])
    assert wrong.status_code == 400
    ok = confirm(ac, "/review/disable", {}, PHRASES["disable"])
    assert ok.status_code == 303 and ok.headers["location"] == "/review?msg=review_disabled"
    assert sql("SELECT enabled FROM review_settings")[0]["enabled"] is False


def test_retention_is_bounded(ac: TestClient) -> None:
    for days in (0, 91, "x"):
        got = ac.post(
            "/review/enable/confirm",
            data={
                "csrf_token": token(ac),
                "confirmation": PHRASES["enable"],
                "retention_days": days,
            },
        )
        assert got.status_code == 422 or got.status_code == 400


# ------------------------------------------------------------------ CSRF and origin
@pytest.mark.parametrize(
    "path",
    [
        "/review/enable/reauth",
        "/review/enable/confirm",
        "/review/disable/reauth",
        "/review/disable/confirm",
        "/review/packages/create/reauth",
        "/review/packages/create/confirm",
        f"/review/packages/{uuid4()}/verify",
        f"/review/packages/{uuid4()}/download",
    ],
)
def test_every_review_post_needs_csrf_and_a_same_origin_request(
    ac: TestClient, sql: Sql, path: str
) -> None:
    before = fingerprint(sql), sql("SELECT enabled FROM review_settings")
    no_token = ac.post(path, data={"confirmation": PHRASES["enable"], "password": "x"})
    assert no_token.status_code == 403
    bad_token = ac.post(path, data={"csrf_token": "forged", "password": "x"})
    assert bad_token.status_code == 403
    cross = ac.post(
        path,
        data={"csrf_token": token(ac), "password": "x"},
        headers={"origin": "https://evil.example"},
    )
    assert cross.status_code == 403
    assert (fingerprint(sql), sql("SELECT enabled FROM review_settings")) == before


def test_get_requests_never_write(ac: TestClient, sql: Sql) -> None:
    before = sql("SELECT count(*) AS n FROM audit_events")[0]["n"], fingerprint(sql)
    for path in ("/review", "/review/enable/request", "/review/disable/request"):
        assert ac.get(path).status_code == 200
    assert (sql("SELECT count(*) AS n FROM audit_events")[0]["n"], fingerprint(sql)) == before


# ------------------------------------------------------------------ create, build, download
def test_create_then_build_then_protected_download(
    ac: TestClient, vc: TestClient, rbuilder: ReviewBuilder, sql: Sql, rsettings: Settings
) -> None:
    enable(ac)
    assert "Request a package" in ac.get("/review").text
    pid = create(ac)
    page = ac.get(f"/review/packages/{pid}").text
    assert "REQUESTED" in page and "Download" not in page
    assert (
        ac.post(f"/review/packages/{pid}/download", data={"csrf_token": token(ac)}).status_code
        == 409
    )  # not built yet
    (result,) = rbuilder.build_pending()
    assert result.state == "READY"
    detail = ac.get(f"/review/packages/{pid}").text
    assert "READY" in detail and "Download (ZIP)" in detail and "efficiency_summary.json" in detail
    assert "cannot trade" in detail
    got = ac.post(f"/review/packages/{pid}/download", data={"csrf_token": token(ac)})
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"] == f'attachment; filename="review-package-{pid}.zip"'
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["cache-control"] == "no-store"
    assert "sandbox" in got.headers["content-security-policy"]
    row = sql("SELECT package_sha256 FROM review_packages")[0]
    import hashlib

    assert hashlib.sha256(got.content).hexdigest() == row["package_sha256"]
    assert got.content[:2] == b"PK"
    assert (
        sql("SELECT count(*) AS n FROM audit_events WHERE event_code = 'review.downloaded'")[0]["n"]
        == 1
    )
    # a viewer can neither see nor fetch it
    assert vc.get(f"/review/packages/{pid}").status_code == 403
    assert vc.post(f"/review/packages/{pid}/download", data={"csrf_token": "x"}).status_code == 403


def test_the_package_route_has_no_get_download_and_no_static_route(
    ac: TestClient, rbuilder: ReviewBuilder, rsettings: Settings, sql: Sql
) -> None:
    enable(ac)
    pid = create(ac)
    rbuilder.build_pending()
    name = sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    assert ac.get(f"/review/packages/{pid}/download").status_code in (404, 405)
    for path in (f"/static/{name}", f"/review/{name}", f"/{name}", f"/static/../{name}"):
        assert ac.get(path).status_code == 404
    assert not (Path(__file__).parents[2] / "app" / "web" / "static" / name).exists()


def test_a_tampered_package_is_refused_and_marked_corrupt(
    ac: TestClient, rbuilder: ReviewBuilder, rsettings: Settings, sql: Sql
) -> None:
    enable(ac)
    pid = create(ac)
    rbuilder.build_pending()
    name = sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    path = Path(rsettings.review.dir) / name
    path.chmod(0o644)
    path.write_bytes(path.read_bytes()[:-40])
    got = ac.post(f"/review/packages/{pid}/download", data={"csrf_token": token(ac)})
    assert got.status_code == 409 and "CORRUPT" in got.text and got.content[:2] != b"PK"
    assert sql("SELECT state FROM review_packages")[0]["state"] == "CORRUPT"
    assert "Download (ZIP)" not in ac.get(f"/review/packages/{pid}").text


def test_verify_reports_intact_packages(ac: TestClient, rbuilder: ReviewBuilder, sql: Sql) -> None:
    enable(ac)
    pid = create(ac)
    rbuilder.build_pending()
    got = ac.post(
        f"/review/packages/{pid}/verify", data={"csrf_token": token(ac)}, follow_redirects=True
    )
    assert got.status_code == 200 and "intact and clean" in got.text


def test_downloads_stop_when_the_feature_is_disabled(
    ac: TestClient, rbuilder: ReviewBuilder
) -> None:
    enable(ac)
    pid = create(ac)
    rbuilder.build_pending()
    assert reauth(ac, "/review/disable", {}).status_code == 303
    assert confirm(ac, "/review/disable", {}, PHRASES["disable"]).status_code == 303
    got = ac.post(f"/review/packages/{pid}/download", data={"csrf_token": token(ac)})
    assert got.status_code == 409 and got.content[:2] != b"PK"
    assert "Downloads are blocked" in ac.get(f"/review/packages/{pid}").text


def test_an_unknown_package_is_a_404(ac: TestClient) -> None:
    assert ac.get(f"/review/packages/{uuid4()}").status_code == 404
    assert (
        ac.post(f"/review/packages/{uuid4()}/download", data={"csrf_token": token(ac)}).status_code
        == 404
    )
    assert ac.get("/review/packages/not-a-uuid").status_code in (400, 404, 422)


# ------------------------------------------------------------------ hostile input
def test_query_and_form_input_is_validated_and_never_reflected(ac: TestClient) -> None:
    enable(ac)
    hostile = "<script>alert(1)</script>"
    for query in (
        f"period_start={hostile}&period_end=2026-09-29&scope=paper",
        "period_start=2026-09-01&period_end=2026-09-29&scope=" + hostile,
        "period_start=2026-09-01&period_end=2026-09-29",
    ):
        got = ac.get("/review/packages/create/request?" + query)
        assert got.status_code in (400, 422) and hostile not in got.text
    flash = ac.get(f"/review?msg={hostile}")
    assert flash.status_code in (200, 422) and hostile not in flash.text


def test_invalid_periods_do_not_reach_the_confirmation_step(ac: TestClient) -> None:
    enable(ac)
    got = ac.get(
        "/review/packages/create/request?period_start=2026-09-29&period_end=2026-09-01&scope=paper"
    )
    assert got.status_code == 400 and "not valid" in got.text
    future = ac.get(
        "/review/packages/create/request?period_start=2026-09-29&period_end=2027-01-01&scope=paper"
    )
    assert future.status_code == 400


def test_create_without_the_feature_enabled_is_refused(ac: TestClient, sql: Sql) -> None:
    assert reauth(ac, "/review/packages/create", CREATE_FIELDS).status_code in (303, 400)
    got = confirm(ac, "/review/packages/create", CREATE_FIELDS, PHRASES["create"])
    assert got.status_code == 409 and sql("SELECT count(*) AS n FROM review_packages")[0]["n"] == 0


def test_the_package_pages_never_render_package_content(
    ac: TestClient, rbuilder: ReviewBuilder, sql: Sql
) -> None:
    enable(ac)
    pid = create(ac)
    rbuilder.build_pending()
    page = ac.get(f"/review/packages/{pid}").text
    assert not re.search(r"<script(?![^>]*\bsrc=)", page.lower())  # no inline script
    assert "<iframe" not in page.lower()
    # only metadata: file names, sizes and digest prefixes, no row content
    assert "backtest_runs" in page and '"run_id"' not in page and "fee_scenario" not in page
