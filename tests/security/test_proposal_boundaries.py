"""Imported proposals are UNTRUSTED ADVISORY INPUT: never executed, rendered raw or applied."""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import ProposalSettings
from app.domain.enums import AuditEventType, Role
from app.domain.permissions import Permission, has_permission
from app.proposals import policy, risk, schema
from tests.security.test_pair_no_exchange_access import APP, imports_of

PROPOSALS = APP / "proposals"
TEMPLATES = APP / "web" / "templates"
PROPOSAL_TEMPLATES = ("proposals.html", "proposal_confirm.html", "proposal_detail.html")
NETWORK = (
    "httpx",
    "requests",
    "urllib",
    "socket",
    "http.client",
    "aiohttp",
    "app.adapters",
    "anthropic",
    "openai",
    "websockets",
    "smtplib",
    "subprocess",
    "ftplib",
)
UNSAFE = (
    "yaml",
    "pickle",
    "marshal",
    "shelve",
    "dill",
    "jinja2",
    "zipfile",
    "tarfile",
    "importlib",
)


def proposal_sources() -> dict[Path, str]:
    files = [*sorted(PROPOSALS.glob("*.py")), APP / "web" / "proposal_views.py"]
    files += [APP / "api" / "proposals.py", APP / "storage" / "proposal_repositories.py"]
    return {p: p.read_text() for p in files}


def test_proposal_code_imports_no_network_process_or_unsafe_deserialisation() -> None:
    for path, source in proposal_sources().items():
        mods = imports_of(source)
        assert not [m for m in mods if m.startswith(NETWORK)], path
        assert not [m for m in mods if m.split(".")[0] in UNSAFE], path


def test_proposal_code_never_evals_execs_compiles_or_builds_templates() -> None:
    for path, source in proposal_sources().items():
        tree = ast.parse(source)
        bare = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        attrs = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        for name in ("eval", "exec", "compile", "__import__"):
            assert name not in bare, (path, name)  # re.compile on a fixed pattern is not this
        for name in ("from_string", "system", "popen", "spawn", "run", "Popen"):
            assert name not in attrs, (path, name)
        for name in ("load", "loads"):  # only json.loads on the host validator is allowed
            hits = [
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == name
                and not (isinstance(n.func.value, ast.Name) and n.func.value.id == "json")
            ]
            assert not hits, (path, name)


def test_only_the_host_validator_parses_proposal_json() -> None:
    parsers = {p.name for p, s in proposal_sources().items() if re.search(r"\bjson\.loads?\(", s)}
    assert parsers <= {"schema.py", "proposal_repositories.py", "proposal_views.py"}, parsers
    for name in ("service.py", "proposals.py"):
        text = next(s for p, s in proposal_sources().items() if p.name == name)
        assert "parse_text" not in text and "schema.validate" not in text, name


def test_proposal_code_never_reaches_bot_pair_exchange_or_paper_modules() -> None:
    banned = (
        "app.paper",
        "app.backtest",
        "app.strategy",
        "app.market",
        "app.adapters",
        "app.pairs.service",
    )
    for path, source in proposal_sources().items():
        assert not [m for m in imports_of(source) if m.startswith(banned)], path


def test_no_proposal_route_or_service_writes_bot_pair_or_config_state() -> None:
    text = "".join(
        s for p, s in proposal_sources().items() if p.name in ("proposals.py", "service.py")
    )
    for word in (
        "pairs.simple_action",
        "pairs.confirm",
        "paper.",
        "PairService",
        "apply_transition",
        "create_order",
        "place_order",
        "cancel_order",
        "os.system",
        "write_env",
        "settings_file",
    ):
        assert word not in text, word


def test_proposal_sql_writes_only_proposal_tables() -> None:
    source = (APP / "storage" / "proposal_repositories.py").read_text()
    writes = re.findall(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([a-z_]+)", source)
    assert writes and set(writes) <= {
        "proposals",
        "proposal_settings",
        "proposal_state_history",
        "change_requests",
        "proposal_attestations",
    }, set(writes)
    assert "DELETE FROM" not in source and "TRUNCATE" not in source


@pytest.mark.parametrize("name", PROPOSAL_TEMPLATES)
def test_proposal_templates_never_disable_escaping_or_run_scripts(name: str) -> None:
    text = (TEMPLATES / name).read_text()
    assert "|safe" not in text and "| safe" not in text
    assert "autoescape false" not in text and "{% autoescape" not in text
    assert "Markup(" not in text and "|markdown" not in text
    assert not re.search(r"<script(?![^>]*\bsrc=)", text)
    assert not re.search(r"\son[a-z]+\s*=", text)
    assert "javascript:" not in text.lower()


def test_view_code_does_not_mark_text_safe() -> None:
    source = (APP / "web" / "proposal_views.py").read_text()
    for word in ("Markup", "mark_safe", "escape(", "|safe"):
        assert word not in source, word


def test_the_label_is_defined_once_and_used_on_every_proposal_page() -> None:
    from app.web import proposal_views

    assert proposal_views.LABEL == "UNTRUSTED ADVISORY INPUT"
    for name in ("proposals.html", "proposal_detail.html", "proposal_confirm.html"):
        assert "view.label" in (TEMPLATES / name).read_text(), name


def test_only_admins_can_manage_proposals() -> None:
    for role in Role:
        assert has_permission(role, Permission.MANAGE_PROPOSALS) == (role is Role.ADMIN), role
    assert not has_permission(None, Permission.MANAGE_PROPOSALS)


def test_every_policy_rule_has_plain_language_text_and_a_view_message() -> None:
    from app.web import proposal_views

    rules = set(policy.NORMALISED_RULES) | set(policy.RAW_RULES)
    assert rules <= set(policy.RULE_TEXT)
    assert rules <= set(proposal_views.RULE_TEXT)


def test_every_forbidden_area_in_the_brief_has_a_rule() -> None:
    have = set(policy.NORMALISED_RULES) | set(policy.RAW_RULES)
    assert {
        "RISK_BYPASS",
        "SECRET_CHANGE",
        "SECURITY_CONTROL_CHANGE",
        "API_ACCESS_CHANGE",
        "LIVE_ACTIVATION",
        "PAIR_STATE_CHANGE",
        "CAPITAL_INCREASE",
        "AUTOMATED_ORDER",
        "AUTO_APPLICATION",
    } <= have


def test_the_schema_is_a_closed_allowlist_with_no_free_form_extension_point() -> None:
    assert len(schema.FIELDS) == 15
    assert "proposal_version" in schema.FIELDS and "linked_review_package_sha256" in schema.FIELDS
    assert not {"extra", "metadata", "config", "code", "command", "script", "apply"} & set(
        schema.FIELDS
    )


def test_risk_assessment_is_advisory_and_uses_a_fixed_vocabulary() -> None:
    assert set(risk.FACTOR_TEXT) and all(len(v) < 200 for v in risk.FACTOR_TEXT.values())


def test_every_proposal_audit_event_is_in_the_catalogue() -> None:
    wanted = {
        "import_enabled",
        "import_disabled",
        "imported",
        "validating",
        "validated",
        "rejected",
        "reviewed",
        "change_request_created",
        "implemented",
        "backtested",
        "paper_validated",
        "closed",
        "cleanup",
        "denied",
    }
    have = {e.value.split(".", 1)[1] for e in AuditEventType if e.value.startswith("proposal.")}
    assert have == wanted


def test_the_proposal_directory_cannot_be_configured_inside_the_web_root() -> None:
    for bad in (
        "/app/web/static/x",
        "/srv/static/x",
        "/var/public/x",
        "relative/x",
        "/a/../b",
        "/app/proposals",
    ):
        with pytest.raises(ValidationError):
            ProposalSettings(dir=bad)
    default = ProposalSettings()
    assert default.dir == "/proposals" and default.max_bytes == 128 * 1024


def test_import_is_off_in_the_migration_by_default() -> None:
    sql = (APP / "storage" / "migrations" / "0005_proposals.sql").read_text()
    assert re.search(r"import_enabled\s+boolean\s+NOT NULL\s+DEFAULT\s+false", sql)


def test_compose_mounts_the_proposals_volume_only_on_app_and_batch(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert "proposals:/proposals" in services["app"]["volumes"]
    assert "proposals:/proposals" in services["batch"]["volumes"]
    others = [
        n
        for n, s in services.items()
        if n not in ("app", "batch") and any("proposals" in str(v) for v in s.get("volumes") or [])
    ]
    assert others == []
    assert not any("datasets" in str(v) for v in services["app"].get("volumes") or [])


def test_verify_security_config_rejects_a_stray_or_read_only_proposal_mount(
    verify: Any, compose: dict[str, Any]
) -> None:
    check = verify.check_compose
    assert check(compose) == []
    stray = copy.deepcopy(compose)
    volumes = stray["services"]["grafana"].get("volumes", [])
    stray["services"]["grafana"]["volumes"] = [*volumes, "proposals:/x"]
    assert any("must not mount proposals" in p for p in check(stray))
    ro = copy.deepcopy(compose)
    ro["services"]["batch"]["volumes"] = [
        v + ":ro" if str(v).startswith("proposals:") else v
        for v in ro["services"]["batch"]["volumes"]
    ]
    assert any("read-write" in p for p in check(ro))
