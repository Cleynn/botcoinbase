from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.domain.enums import AuditEventType, AuditResult
from tests.conftest import Account

ROOT = Path(__file__).resolve().parents[2]
PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "'-alert(1)-'",
    "{{7*7}}",
    "${7*7}",
    "{% raw %}x{% endraw %}",
    "javascript:alert(1)",
    "</td><td>injected</td>",
    "&lt;already&gt;",
]


LEGIT_SCRIPTS = (
    '<script src="/static/js/htmx.min.js" defer></script>',
    '<script src="/static/js/app.js" defer></script>',
)


def without_own_scripts(body: str) -> str:
    """The page's own two static script tags are expected; anything else would be an injection."""
    for tag in LEGIT_SCRIPTS:
        assert body.count(tag) == 1
        body = body.replace(tag, "")
    return body


def hostile_event(services: Any, payload: str) -> None:
    with services.storage.tx() as repos:
        services.audit.record(
            repos,
            AuditEventType.AUTHZ_DENIED,
            AuditResult.DENIED,
            reason=payload[:60],
            target_type="route",
            target_id=payload,
            client_tag="attacker",
            request_id=payload[:60],
            detail={"note": payload, "other": payload[::-1]},
        )


@pytest.mark.parametrize("payload", PAYLOADS)
def test_hostile_audit_values_are_escaped_on_the_audit_page(
    admin_client: TestClient, services: Any, payload: str
) -> None:
    hostile_event(services, payload)
    body = admin_client.get("/audit").text
    if any(ch in payload for ch in "<>\"'&"):
        assert payload not in body  # never rendered raw
    # Inert text such as "&lt;img src=x onerror=alert(1)&gt;" is fine; a raw tag or handler is not.
    stripped = without_own_scripts(body)
    assert not re.search(r"<(script|img|svg|iframe)\b", stripped, re.IGNORECASE)
    assert not re.search(r"<[^>]*\sonerror\s*=", stripped, re.IGNORECASE)
    assert "<td>injected</td>" not in body
    assert "49" not in re.sub(
        r"\d{2,}", "", body.split("<tbody>")[1].split("</tbody>")[0]
    )  # {{7*7}} not evaluated


def test_escaped_forms_appear_in_the_output(admin_client: TestClient, services: Any) -> None:
    hostile_event(services, "<script>alert(1)</script>")
    body = admin_client.get("/audit").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    hostile_event(services, "{{7*7}}")
    assert "{{7*7}}" in admin_client.get("/audit").text  # rendered literally, not evaluated


def test_control_characters_in_audit_values_are_neutralised(
    admin_client: TestClient, services: Any
) -> None:
    hostile_event(services, "line1\nline2\r\x00\x1b[31mred")
    body = admin_client.get("/audit").text
    assert "\x00" not in body and "\x1b" not in body


def test_usernames_cannot_contain_markup(db: Any) -> None:
    for name in ("<b>x</b>", "a b", "x'--", "ab", "A" * 65, "x/../y"):
        with (
            psycopg.connect(db.owner_conninfo()) as conn,
            pytest.raises(psycopg.errors.CheckViolation),
        ):
            conn.execute(
                "INSERT INTO users (id, username, role, password_hash, "
                "password_changed_at, created_at) "
                "VALUES (gen_random_uuid(), %s, 'VIEWER', '$argon2id$x', now(), now())",
                (name,),
            )


@pytest.mark.parametrize("payload", PAYLOADS)
def test_reflected_parameters_are_never_echoed(
    admin_client: TestClient, client: TestClient, payload: str
) -> None:
    responses = [
        admin_client.get("/security", params={"msg": payload}),
        admin_client.get("/audit", params={"code": payload}),
        admin_client.get("/audit", params={"before": payload}),
        client.get(f"/{payload}"),
        client.get("/login", params={"next": payload, "error": payload}),
    ]
    for response in responses:
        assert payload not in response.text or not any(ch in payload for ch in "<>\"'&")
        assert not re.search(r"<script>alert", response.text)


def test_login_page_never_reflects_submitted_credentials(
    client: TestClient, login: Callable[..., Any]
) -> None:
    response = login(client, '"><script>alert(1)</script>', "<img src=x onerror=alert(1)>")
    stripped = without_own_scripts(response.text)
    assert not re.search(r"<(script|img)\b[^>]*>", stripped.split("<main")[1], re.IGNORECASE)
    assert not re.search(r"<[^>]*\sonerror", stripped, re.IGNORECASE)


def test_csp_forbids_inline_and_external_content(admin_client: TestClient) -> None:
    csp = admin_client.get("/").headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "*" not in csp
    for directive in (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ):
        assert directive in csp


def test_rendered_pages_use_no_inline_styles_scripts_or_event_handlers(
    admin_client: TestClient, client: TestClient
) -> None:
    for c, path in (
        (admin_client, "/"),
        (admin_client, "/security"),
        (admin_client, "/audit"),
        (client, "/login"),
    ):
        body = c.get(path).text
        assert "<style" not in body and not re.search(r'\sstyle="', body)
        assert not re.search(r"\son[a-z]+\s*=", body, re.IGNORECASE)
        assert "javascript:" not in body.lower()
        assert not re.search(r"<script(?![^>]*\ssrc=)", body)


def test_templates_never_bypass_autoescaping() -> None:
    for template in (ROOT / "app/web/templates").glob("*.html"):
        text = template.read_text()
        for forbidden in (
            "|safe",
            "Markup",
            "autoescape false",
            "|striptags",
            "hx-vals",
            "hx-headers",
            "hx-on",
            "hx-swap-oob",
        ):
            assert forbidden not in text, (template.name, forbidden)
        assert "{%- autoescape" not in text and "{% autoescape" not in text


def test_the_template_environment_is_strict(services: Any) -> None:
    env = services.renderer._env
    assert env.autoescape is True
    import jinja2

    assert env.undefined is jinja2.StrictUndefined


def test_html_responses_declare_charset_and_nosniff(admin_client: TestClient) -> None:
    response = admin_client.get("/audit")
    assert response.headers["content-type"].startswith("text/html; charset=utf-8")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_security_page_shows_only_fixed_message_texts(
    admin_client: TestClient, admin: Account
) -> None:
    body = admin_client.get("/security?msg=password_changed").text
    assert "Password changed. Your other sessions were signed out." in body
    assert "<script>" not in without_own_scripts(
        admin_client.get("/security?msg=%3Cscript%3E").text
    )
