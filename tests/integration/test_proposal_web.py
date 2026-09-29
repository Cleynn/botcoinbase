"""Proposal pages over HTTP: ADMIN only, full chain, multipart upload safety, XSS, no auto-apply.

All proposal content here is SYNTHETIC. Nothing is sent anywhere and nothing is applied.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.proposals.service import PHRASE_ENABLE, PHRASE_IMPORT, phrase_change_request
from tests.conftest import GOOD_PASSWORD, ORIGIN, Account, csrf_from
from tests.integration.proposal_env import UNTOUCHED, ProposalEnv

Sql = Callable[..., list[dict[str, Any]]]
LABEL = "UNTRUSTED ADVISORY INPUT"
BASE = "/review/proposals"


@pytest.fixture
def papp(prop: ProposalEnv, storage: Any, clock: Any) -> Any:
    return create_app(prop.settings, storage=storage, clock=clock)


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
def ac(papp: Any, login: Callable[..., Any], admin: Account) -> TestClient:
    return _client(papp, login, admin, "10.0.0.5")


@pytest.fixture
def vc(papp: Any, login: Callable[..., Any], viewer: Account) -> TestClient:
    return _client(papp, login, viewer, "10.0.0.6")


def token(client: TestClient) -> str:
    return csrf_from(client.get(f"{BASE}/import/request").text)


def reauth(client: TestClient, prefix: str, pw: str = GOOD_PASSWORD) -> Any:
    return client.post(
        f"{prefix}/reauth",
        data={"csrf_token": token(client), "password": pw},
        follow_redirects=False,
    )


def enable(client: TestClient) -> None:
    assert reauth(client, f"{BASE}/import-enable").status_code == 303
    got = client.post(
        f"{BASE}/import-enable/confirm",
        data={"csrf_token": token(client), "confirmation": PHRASE_ENABLE},
        follow_redirects=False,
    )
    assert got.status_code == 303, got.text


def upload(
    client: TestClient,
    body: bytes,
    *,
    name: str = "proposal.json",
    mime: str = "application/json",
    phrase: str = PHRASE_IMPORT,
    fresh: bool = True,
    extra: dict[str, Any] | None = None,
) -> Any:
    if fresh:
        assert reauth(client, f"{BASE}/import").status_code == 303
    data = {"csrf_token": token(client), "confirmation": phrase, **(extra or {})}
    return client.post(
        f"{BASE}/import/confirm",
        data=data,
        files={"file": (name, io.BytesIO(body), mime)},
        follow_redirects=False,
    )


def pid_of(resp: Any) -> UUID:
    assert resp.status_code == 303, resp.text
    match = re.search(r"/proposals/([0-9a-f-]{36})", resp.headers["location"])
    assert match
    return UUID(match.group(1))


def tables(sql: Sql) -> dict[str, list[dict[str, Any]]]:
    return {name: sql(f"SELECT * FROM {name} ORDER BY 1") for name in UNTOUCHED}  # noqa: S608


def doc_bytes(prop: ProposalEnv, **changes: Any) -> bytes:
    return json.dumps(prop.document(**changes)).encode()


ROUTES = (
    ("GET", BASE),
    ("GET", f"{BASE}/import-enable/request"),
    ("GET", f"{BASE}/import/request"),
    ("GET", f"{BASE}/{uuid4()}"),
    ("GET", f"{BASE}/{uuid4()}/change-request/request"),
    ("GET", f"{BASE}/{uuid4()}/attest/implemented/request"),
    ("POST", f"{BASE}/import-enable/reauth"),
    ("POST", f"{BASE}/import-enable/confirm"),
    ("POST", f"{BASE}/import-disable"),
    ("POST", f"{BASE}/import/reauth"),
    ("POST", f"{BASE}/import/confirm"),
    ("POST", f"{BASE}/{uuid4()}/review"),
    ("POST", f"{BASE}/{uuid4()}/close"),
    ("POST", f"{BASE}/{uuid4()}/change-request/reauth"),
    ("POST", f"{BASE}/{uuid4()}/change-request/confirm"),
    ("POST", f"{BASE}/{uuid4()}/attest/implemented/reauth"),
    ("POST", f"{BASE}/{uuid4()}/attest/implemented/confirm"),
)


# ------------------------------------------------------------------ access
@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_viewers_are_refused_everywhere(vc: TestClient, method: str, path: str) -> None:
    resp = vc.request(method, path, data={"csrf_token": "x"} if method == "POST" else None)
    assert resp.status_code in (403, 404)
    assert LABEL not in resp.text


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_anonymous_visitors_get_nothing(papp: Any, method: str, path: str) -> None:
    anon = TestClient(
        papp,
        base_url="https://testserver",
        raise_server_exceptions=False,
        client=("10.0.0.9", 50000),
        headers={"origin": ORIGIN},
    )
    resp = anon.request(method, path, follow_redirects=False)
    assert resp.status_code in (303, 401, 403)
    assert LABEL not in resp.text


def test_the_menu_shows_proposals_only_to_admins(ac: TestClient, vc: TestClient) -> None:
    assert f'href="{BASE}"' in ac.get("/").text
    assert f'href="{BASE}"' not in vc.get("/").text


def test_denials_are_audited(vc: TestClient, sql: Sql) -> None:
    vc.get(BASE)
    denied = sql("SELECT * FROM audit_events WHERE event_code = 'authz.denied'")
    assert denied and denied[-1]["actor_role"] == "VIEWER"


# ------------------------------------------------------------------ the pages say what they are
def test_disabled_by_default_and_every_page_carries_the_label(ac: TestClient) -> None:
    page = ac.get(BASE).text
    assert "DISABLED" in page and LABEL in page
    for path in (BASE, f"{BASE}/import-enable/request", f"{BASE}/import/request"):
        assert LABEL in ac.get(path).text, path


def test_a_disabled_import_stores_nothing(ac: TestClient, prop: ProposalEnv, sql: Sql) -> None:
    got = upload(ac, doc_bytes(prop), fresh=False)
    assert got.status_code in (400, 403, 409)
    assert sql("SELECT count(*) AS n FROM proposals")[0]["n"] == 0
    assert not list(prop.directory.glob("*")) if prop.directory.exists() else True


def test_get_requests_never_write(ac: TestClient, sql: Sql) -> None:
    before = sql("SELECT count(*) AS n FROM audit_events")[0]["n"]
    for path in (BASE, f"{BASE}/import-enable/request", f"{BASE}/import/request"):
        assert ac.get(path).status_code == 200
    assert sql("SELECT count(*) AS n FROM audit_events")[0]["n"] == before


# ------------------------------------------------------------------ the whole chain
def test_full_chain_creates_only_a_manual_change_request(
    ac: TestClient, prop: ProposalEnv, sql: Sql
) -> None:
    enable(ac)
    before = tables(sql)
    pid = pid_of(upload(ac, doc_bytes(prop)))
    assert sql("SELECT state FROM proposals")[0]["state"] == "IMPORTED"
    (result,) = prop.validate()
    assert result.state == "VALIDATED"
    page = ac.get(f"{BASE}/{pid}").text
    assert LABEL in page and "VALIDATED" in page
    review = ac.post(
        f"{BASE}/{pid}/review",
        data={"csrf_token": token(ac), "notes": "Worth a manual look."},
        follow_redirects=False,
    )
    assert review.status_code == 303
    assert reauth(ac, f"{BASE}/{pid}/change-request").status_code == 303
    cr = ac.post(
        f"{BASE}/{pid}/change-request/confirm",
        data={
            "csrf_token": token(ac),
            "confirmation": phrase_change_request(pid),
            "change_type": "PARAMETER_CHANGE",
            "impact_assessment": "Band filter only; fees and ceilings are unchanged.",
            "ceilings_unaffected": "true",
        },
        follow_redirects=False,
    )
    assert cr.status_code == 303, cr.text
    assert sql("SELECT state FROM proposals")[0]["state"] == "CHANGE_REQUEST_CREATED"
    assert len(sql("SELECT * FROM change_requests")) == 1
    assert tables(sql) == before  # nothing else moved: no pair, order, config or user change
    detail = ac.get(f"{BASE}/{pid}").text
    assert "CHANGE_REQUEST_CREATED" in detail and "manual" in detail.lower()


def test_the_wrong_phrase_never_spends_the_reauth(
    ac: TestClient, prop: ProposalEnv, sql: Sql
) -> None:
    enable(ac)
    assert reauth(ac, f"{BASE}/import").status_code == 303
    bad = upload(ac, doc_bytes(prop), phrase="import untrusted llm proposal", fresh=False)
    assert bad.status_code == 400 and not sql("SELECT 1 FROM proposals")
    ok = upload(ac, doc_bytes(prop), fresh=False)
    assert ok.status_code == 303


def test_import_without_fresh_reauth_is_refused(
    ac: TestClient, prop: ProposalEnv, sql: Sql
) -> None:
    enable(ac)
    got = upload(ac, doc_bytes(prop), fresh=False)
    assert got.status_code == 400 and "Confirm your password first" in got.text
    assert not sql("SELECT 1 FROM proposals")


def test_the_reauth_is_single_use(ac: TestClient, prop: ProposalEnv) -> None:
    enable(ac)
    upload(ac, doc_bytes(prop))
    again = upload(ac, doc_bytes(prop, proposal_id="prop-2026-002"), fresh=False)
    assert again.status_code == 400


def test_the_change_request_phrase_names_this_proposal(
    ac: TestClient, prop: ProposalEnv, sql: Sql
) -> None:
    enable(ac)
    first = pid_of(upload(ac, doc_bytes(prop)))
    other = uuid4()
    prop.validate()
    ac.post(
        f"{BASE}/{first}/review",
        data={"csrf_token": token(ac), "notes": "ok"},
        follow_redirects=False,
    )
    assert reauth(ac, f"{BASE}/{first}/change-request").status_code == 303
    bad = ac.post(
        f"{BASE}/{first}/change-request/confirm",
        data={
            "csrf_token": token(ac),
            "confirmation": phrase_change_request(other),
            "change_type": "PARAMETER_CHANGE",
            "impact_assessment": "Band filter only; fees and ceilings are unchanged.",
            "ceilings_unaffected": "true",
        },
    )
    assert bad.status_code == 400
    assert not sql("SELECT 1 FROM change_requests")


# ------------------------------------------------------------------ upload safety
def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("proposal.json", "{}")
    return buf.getvalue()


HOSTILE = (
    ("zip as json", "proposal.json", "application/json", _zip()),
    ("zip by name", "proposal.zip", "application/zip", _zip()),
    ("pdf", "proposal.pdf", "application/pdf", b"%PDF-1.7\n{}"),
    ("html", "proposal.html", "text/html", b"<html><script>alert(1)</script></html>"),
    ("html as json", "proposal.json", "application/json", b"<script>alert(1)</script>"),
    ("csv", "proposal.csv", "text/csv", b"a,b\n1,2\n"),
    ("png", "proposal.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16),
    ("js", "proposal.js", "application/javascript", b"alert(1)"),
    ("yaml", "proposal.yaml", "application/x-yaml", b"a: 1\n"),
    ("xml", "proposal.xml", "application/xml", b"<a/>"),
    ("shell", "proposal.sh", "application/x-sh", b"#!/bin/sh\nid\n"),
    ("elf", "proposal.bin", "application/octet-stream", b"\x7fELF" + b"\x00" * 16),
    ("json with nul", "proposal.json", "application/json", b'{"a":"\x00"}'),
    ("latin-1", "proposal.json", "application/json", b'{"a":"\xe9"}'),
    ("mime mismatch", "proposal.json", "text/html", b"{}"),
    ("double extension", "proposal.json.exe", "application/json", b"{}"),
)


@pytest.mark.parametrize(("label", "name", "mime", "body"), HOSTILE, ids=[h[0] for h in HOSTILE])
def test_hostile_uploads_are_refused_and_store_nothing(
    ac: TestClient, prop: ProposalEnv, sql: Sql, label: str, name: str, mime: str, body: bytes
) -> None:
    enable(ac)
    got = upload(ac, body, name=name, mime=mime)
    assert got.status_code in (400, 413, 415, 422), (label, got.status_code)
    assert not sql("SELECT 1 FROM proposals")
    assert not list(prop.directory.glob("*.proposal"))


def test_a_bad_file_does_not_spend_the_reauth(ac: TestClient, prop: ProposalEnv, sql: Sql) -> None:
    enable(ac)
    assert reauth(ac, f"{BASE}/import").status_code == 303
    bad = upload(ac, _zip(), fresh=False)
    assert bad.status_code == 400
    ok = upload(ac, doc_bytes(prop), fresh=False)
    assert ok.status_code == 303


def test_oversize_upload_is_refused(ac: TestClient, prop: ProposalEnv, sql: Sql) -> None:
    enable(ac)
    big = b'{"x":"' + b"a" * (prop.settings.proposals.max_bytes + 10) + b'"}'
    got = upload(ac, big)
    assert got.status_code in (400, 413)
    huge = b"a" * (1024 * 1024)
    assert upload(ac, huge).status_code in (400, 413)
    assert not sql("SELECT 1 FROM proposals")


def test_file_and_text_together_or_neither_are_refused(
    ac: TestClient, prop: ProposalEnv, sql: Sql
) -> None:
    enable(ac)
    both = upload(ac, doc_bytes(prop), extra={"text": doc_bytes(prop).decode()})
    assert both.status_code == 400
    assert reauth(ac, f"{BASE}/import").status_code == 303
    neither = ac.post(
        f"{BASE}/import/confirm",
        data={"csrf_token": token(ac), "confirmation": PHRASE_IMPORT},
    )
    assert neither.status_code == 400
    assert not sql("SELECT 1 FROM proposals")


def test_pasted_text_is_accepted_as_plain_text(ac: TestClient, prop: ProposalEnv, sql: Sql) -> None:
    enable(ac)
    assert reauth(ac, f"{BASE}/import").status_code == 303
    got = ac.post(
        f"{BASE}/import/confirm",
        data={
            "csrf_token": token(ac),
            "confirmation": PHRASE_IMPORT,
            "text": doc_bytes(prop).decode(),
        },
        follow_redirects=False,
    )
    assert pid_of(got)
    assert sql("SELECT declared_mime FROM proposals")[0]["declared_mime"] == "text/plain"


def test_unknown_form_fields_and_two_files_are_refused(
    ac: TestClient, prop: ProposalEnv, sql: Sql
) -> None:
    enable(ac)
    extra = upload(ac, doc_bytes(prop), extra={"apply": "yes"})
    assert extra.status_code == 400
    assert reauth(ac, f"{BASE}/import").status_code == 303
    two = ac.post(
        f"{BASE}/import/confirm",
        data={"csrf_token": token(ac), "confirmation": PHRASE_IMPORT},
        files=[
            ("file", ("a.json", io.BytesIO(doc_bytes(prop)), "application/json")),
            ("file", ("b.json", io.BytesIO(doc_bytes(prop)), "application/json")),
        ],
    )
    assert two.status_code in (400, 403, 413)
    assert not sql("SELECT 1 FROM proposals")


def test_the_stored_name_is_generated_and_outside_any_web_root(
    ac: TestClient, prop: ProposalEnv, sql: Sql
) -> None:
    enable(ac)
    pid_of(upload(ac, doc_bytes(prop), name="../../etc/evil name.json"))
    stored = sql("SELECT storage_name FROM proposals")[0]["storage_name"]
    assert re.fullmatch(r"[0-9a-f-]{36}\.proposal", stored)
    assert "evil" not in stored
    assert (prop.directory / stored).is_file()
    assert "static" not in str(prop.directory) and "templates" not in str(prop.directory)


def test_no_upload_path_is_served_statically(ac: TestClient, prop: ProposalEnv) -> None:
    enable(ac)
    pid_of(upload(ac, doc_bytes(prop)))
    stored = next(prop.directory.glob("*.proposal")).name
    for path in (f"/static/{stored}", f"/proposals/{stored}", f"{BASE}/{stored}"):
        assert ac.get(path).status_code in (400, 404, 422)


# ------------------------------------------------------------------ CSRF and origin
POSTS = [(m, p) for m, p in ROUTES if m == "POST"]


@pytest.mark.parametrize(("method", "path"), POSTS)
def test_every_post_needs_csrf_and_a_same_origin_request(
    ac: TestClient, sql: Sql, method: str, path: str
) -> None:
    before = tables(sql)
    assert ac.post(path, data={"confirmation": PHRASE_IMPORT, "password": "x"}).status_code == 403
    assert ac.post(path, data={"csrf_token": "forged", "password": "x"}).status_code == 403
    cross = ac.post(
        path,
        data={"csrf_token": token(ac), "password": "x"},
        headers={"origin": "https://evil.example"},
    )
    assert cross.status_code == 403
    assert tables(sql) == before
    assert not sql("SELECT 1 FROM proposals")


def test_multipart_upload_needs_csrf_and_same_origin(
    ac: TestClient, prop: ProposalEnv, sql: Sql
) -> None:
    enable(ac)
    assert reauth(ac, f"{BASE}/import").status_code == 303
    files = {"file": ("p.json", io.BytesIO(doc_bytes(prop)), "application/json")}
    no_token = ac.post(f"{BASE}/import/confirm", data={"confirmation": PHRASE_IMPORT}, files=files)
    assert no_token.status_code == 403
    files = {"file": ("p.json", io.BytesIO(doc_bytes(prop)), "application/json")}
    cross = ac.post(
        f"{BASE}/import/confirm",
        data={"csrf_token": token(ac), "confirmation": PHRASE_IMPORT},
        files=files,
        headers={"origin": "https://evil.example"},
    )
    assert cross.status_code == 403
    assert not sql("SELECT 1 FROM proposals")


def test_multipart_is_refused_on_every_other_route(ac: TestClient, sql: Sql) -> None:
    before = tables(sql)
    for _method, path in POSTS:
        if path.endswith("/import/confirm"):
            continue
        got = ac.post(
            path,
            data={"csrf_token": token(ac), "password": "x"},
            files={"file": ("p.json", io.BytesIO(b"{}"), "application/json")},
        )
        assert got.status_code in (400, 403, 413, 415, 422), path
    assert tables(sql) == before


def test_a_big_upload_without_a_session_is_rejected_early(papp: Any) -> None:
    anon = TestClient(
        papp,
        base_url="https://testserver",
        raise_server_exceptions=False,
        client=("10.0.0.9", 50000),
        headers={"origin": ORIGIN},
    )
    got = anon.post(
        f"{BASE}/import/confirm",
        data={"csrf_token": "x", "confirmation": PHRASE_IMPORT},
        files={"file": ("p.json", io.BytesIO(b"a" * 300_000), "application/json")},
        follow_redirects=False,
    )
    assert got.status_code in (303, 401, 403, 413)


# ------------------------------------------------------------------ XSS and raw rendering
XSS = "<script>alert(1)</script><img src=x onerror=alert(2)>\"'{{7*7}}${7*7}"


def test_hostile_text_is_escaped_in_every_field(
    ac: TestClient, prop: ProposalEnv, sql: Sql
) -> None:
    enable(ac)
    doc = prop.document(
        summary="Wider band " + XSS,
        suggested_change="Raise the band ratio " + XSS,
        expected_benefit="Fewer cycles " + XSS,
        risk_tradeoffs="Less activity " + XSS,
        required_validation="Backtest then paper " + XSS,
        rollback_plan="Revert by release " + XSS,
        assumptions=["Fees hold " + XSS],
        proposal_id="prop-<b>x</b>",
    )
    resp = upload(ac, json.dumps(doc).encode())
    if resp.status_code != 303:  # a refusal page must not echo the hostile text either
        assert "<script>" not in resp.text and "<img" not in resp.text
        return
    pid = pid_of(resp)
    prop.validate()
    for path in (f"{BASE}/{pid}", BASE):
        page = ac.get(path).text
        assert "<script>alert" not in page and "<img src=x" not in page, path
        assert not re.search(r"\son[a-z]+\s*=\s*alert", page), path


def test_template_syntax_stays_literal(ac: TestClient, prop: ProposalEnv) -> None:
    enable(ac)
    doc = prop.document(summary="Use {{7*7}} and {% raw %} and ${7*7} for the band")
    pid = pid_of(upload(ac, json.dumps(doc).encode()))
    prop.validate()
    page = ac.get(f"{BASE}/{pid}").text
    assert not re.search(r">\s*49\s*<", page)


def test_review_notes_and_impact_are_escaped(ac: TestClient, prop: ProposalEnv) -> None:
    enable(ac)
    pid = pid_of(upload(ac, doc_bytes(prop)))
    prop.validate()
    ac.post(
        f"{BASE}/{pid}/review",
        data={"csrf_token": token(ac), "notes": "<script>alert(3)</script>"},
        follow_redirects=False,
    )
    assert reauth(ac, f"{BASE}/{pid}/change-request").status_code == 303
    ac.post(
        f"{BASE}/{pid}/change-request/confirm",
        data={
            "csrf_token": token(ac),
            "confirmation": phrase_change_request(pid),
            "change_type": "PARAMETER_CHANGE",
            "impact_assessment": "<img src=x onerror=alert(4)> band filter only, fees unchanged.",
            "ceilings_unaffected": "true",
        },
        follow_redirects=False,
    )
    page = ac.get(f"{BASE}/{pid}").text
    assert "<script>alert(3)" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page and "&lt;img" in page


def test_no_page_has_inline_script_or_event_handlers(ac: TestClient, prop: ProposalEnv) -> None:
    enable(ac)
    pid = pid_of(upload(ac, doc_bytes(prop)))
    prop.validate()
    for path in (BASE, f"{BASE}/{pid}", f"{BASE}/import/request", f"{BASE}/import-enable/request"):
        page = ac.get(path).text
        assert not re.search(r"<script(?![^>]*\bsrc=)", page), path
        assert not re.search(r"\son[a-z]+\s*=", page), path
        assert "javascript:" not in page.lower(), path


def test_the_raw_file_is_never_rendered_or_downloadable(ac: TestClient, prop: ProposalEnv) -> None:
    enable(ac)
    pid = pid_of(upload(ac, doc_bytes(prop)))
    prop.validate()
    page = ac.get(f"{BASE}/{pid}").text
    assert '"proposal_version"' not in page
    for suffix in ("download", "raw", "file", "content"):
        assert ac.get(f"{BASE}/{pid}/{suffix}").status_code in (404, 405, 422)


# ------------------------------------------------------------------ policy over HTTP
@pytest.mark.parametrize(
    "text",
    [
        "Disable the risk limits so the grid can run wider.",
        "Rotate the API key and store it in the config.",
        "Enable live trading once the paper run looks fine.",
        "Automatically increase capital after each winning day.",
        "Automatically apply this change on the next release.",
    ],
)
def test_forbidden_proposals_are_rejected_and_never_reviewable(
    ac: TestClient, prop: ProposalEnv, sql: Sql, text: str
) -> None:
    enable(ac)
    pid = pid_of(upload(ac, doc_bytes(prop, suggested_change=text)))
    (result,) = prop.validate()
    assert result.state == "REJECTED"
    page = ac.get(f"{BASE}/{pid}").text
    assert "REJECTED" in page and LABEL in page
    denied = ac.post(
        f"{BASE}/{pid}/review",
        data={"csrf_token": token(ac), "notes": "try anyway"},
        follow_redirects=False,
    )
    assert denied.status_code in (400, 409)
    assert sql("SELECT state FROM proposals")[0]["state"] == "REJECTED"


def test_unknown_fields_and_a_missing_package_link_are_rejected(
    ac: TestClient, prop: ProposalEnv, sql: Sql
) -> None:
    enable(ac)
    pid_of(upload(ac, doc_bytes(prop, apply_now=True)))
    pid_of(
        upload(
            ac, doc_bytes(prop, proposal_id="prop-2026-002", linked_review_package_id=str(uuid4()))
        )
    )
    results = prop.validate()
    assert {r.state for r in results} == {"REJECTED"}


# ------------------------------------------------------------------ disable
def test_disable_is_csrf_only_and_stops_new_imports(
    ac: TestClient, prop: ProposalEnv, sql: Sql
) -> None:
    enable(ac)
    off = ac.post(f"{BASE}/import-disable", data={"csrf_token": token(ac)}, follow_redirects=False)
    assert off.status_code == 303
    assert sql("SELECT import_enabled FROM proposal_settings")[0]["import_enabled"] is False
    assert upload(ac, doc_bytes(prop), fresh=False).status_code in (400, 403, 409)
