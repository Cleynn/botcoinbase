"""Proposals at service level: import chain, storage, validation, lifecycle, and no side effects."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from app.proposals.service import (
    PHRASE_ENABLE,
    PHRASE_IMPORT,
    phrase_change_request,
)
from app.proposals.store import NAME
from tests.conftest import TestDb
from tests.integration.proposal_env import UNTOUCHED, ProposalEnv
from tests.integration.test_market_ingest import role_conn

Sql = Callable[..., list[dict[str, Any]]]


def codes(sql: Sql) -> list[str]:
    return [r["event_code"] for r in sql("SELECT event_code FROM audit_events ORDER BY seq")]


def fingerprint(sql: Sql) -> dict[str, str]:
    return {
        t: sql(
            f"SELECT md5(COALESCE(string_agg(x::text, '|' ORDER BY x::text), '')) AS h FROM {t} x"
        )[  # noqa: S608
            0
        ]["h"]
        for t in UNTOUCHED
    }


def add_report(sql: Sql, kind: str, created: datetime) -> UUID:
    rid = uuid4()
    label = "PAPER" if kind == "PAPER_DAILY" else "BACKTEST"
    sql(
        "INSERT INTO reports (id, kind, mode_label, title, body_json, body_md, sha256, created_at) "
        "VALUES (%s, %s, %s, 'r', '{}', 'm', %s, %s)",
        (rid, kind, label, uuid4().hex + uuid4().hex, created),
    )
    return rid


# ------------------------------------------------------------------ disabled by default
def test_import_is_disabled_by_default_and_nothing_is_stored(prop: ProposalEnv, sql: Sql) -> None:
    assert sql("SELECT import_enabled FROM proposal_settings")[0]["import_enabled"] is False
    outcome = prop.import_bytes(json.dumps(prop.document()).encode())
    assert outcome.kind == "disabled"
    assert sql("SELECT count(*) AS n FROM proposals")[0]["n"] == 0
    assert not prop.directory.exists() or list(prop.directory.iterdir()) == []
    denied = sql("SELECT * FROM audit_events WHERE event_code = 'proposal.denied'")
    assert len(denied) == 1 and denied[0]["reason_code"] == "IMPORT_DISABLED"


def test_the_database_itself_refuses_an_import_while_disabled(
    prop: ProposalEnv, db: TestDb
) -> None:
    with role_conn(db, "td_app") as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="disabled"):
            conn.execute(
                "INSERT INTO proposals (id, state, storage_name, size_bytes, sha256, "
                "declared_mime, "
                "imported_by, imported_at) VALUES "
                "(%s, 'IMPORTED', %s, 10, %s, 'text/plain', %s, now())",
                (uuid4(), f"{uuid4()}.proposal", "a" * 64, prop.admin.user.id),
            )


# ------------------------------------------------------------------ enabling
def test_enabling_needs_the_exact_phrase_and_a_fresh_reauth(prop: ProposalEnv, sql: Sql) -> None:
    for typed in (
        "enable untrusted proposal import",
        PHRASE_ENABLE + " ",
        "ENABLE PROPOSAL IMPORT",
        "",
        "ENABLE UNTRUSTED PROPOSAL IMPОRT",
    ):
        prop.reauth.available = True
        assert prop.service.enable_import(prop.ctx, prop.actor, typed).kind == "phrase_mismatch"
    assert prop.reauth.consumed == 0  # a wrong phrase never spends the reauthentication
    prop.reauth.available = False
    assert prop.service.enable_import(prop.ctx, prop.actor, PHRASE_ENABLE).kind == "reauth_required"
    assert sql("SELECT import_enabled FROM proposal_settings")[0]["import_enabled"] is False
    assert prop.enable().kind == "ok" and prop.reauth.consumed == 1
    assert sql("SELECT import_enabled FROM proposal_settings")[0]["import_enabled"] is True
    assert "proposal.import_enabled" in codes(sql)
    assert prop.enable().kind == "conflict"


def test_disabling_is_the_restrictive_direction_and_needs_no_chain(
    prop: ProposalEnv, sql: Sql
) -> None:
    assert prop.service.disable_import(prop.actor).kind == "conflict"
    prop.enable()
    prop.reauth.available = False  # no reauthentication, no phrase
    assert prop.service.disable_import(prop.actor).kind == "ok"
    assert sql("SELECT import_enabled FROM proposal_settings")[0]["import_enabled"] is False
    assert "proposal.import_disabled" in codes(sql)
    assert prop.import_bytes(json.dumps(prop.document()).encode()).kind == "disabled"


# ------------------------------------------------------------------ the import chain
def test_import_needs_its_own_exact_phrase_and_a_fresh_reauth(prop: ProposalEnv, sql: Sql) -> None:
    prop.enable()
    data = json.dumps(prop.document()).encode()
    consumed = prop.reauth.consumed
    for typed in (
        "import untrusted llm proposal",
        PHRASE_IMPORT + ".",
        PHRASE_ENABLE,
        "IMPORT LLM PROPOSAL",
    ):
        assert prop.import_bytes(data, typed=typed).kind == "phrase_mismatch"
    assert prop.reauth.consumed == consumed
    assert prop.import_bytes(data, fresh=False).kind == "reauth_required"
    assert sql("SELECT count(*) AS n FROM proposals")[0]["n"] == 0
    assert not list(prop.directory.glob("*.proposal")) if prop.directory.exists() else True


def test_a_successful_import_stores_opaque_bytes_under_a_generated_name(
    prop: ProposalEnv, sql: Sql
) -> None:
    prop.enable()
    data = json.dumps(prop.document()).encode()
    pid = prop.import_doc()
    row = sql("SELECT * FROM proposals WHERE id = %s", (pid,))[0]
    assert row["state"] == "IMPORTED" and row["imported_by"] == prop.admin.user.id
    assert NAME.match(row["storage_name"]) and row["storage_name"] != f"{pid}.proposal"
    path = prop.directory / row["storage_name"]
    assert path.read_bytes() == data and stat.S_IMODE(os.stat(path).st_mode) == 0o440
    assert "static" not in path.parts and "web" not in path.parts
    assert row["size_bytes"] == len(data) and len(row["sha256"]) == 64
    assert row["parsed"] is None and row["category"] is None  # nothing is parsed at import
    hist = sql("SELECT state_after, actor_class FROM proposal_state_history")
    assert hist == [{"state_after": "IMPORTED", "actor_class": "WEB"}]
    event = sql("SELECT * FROM audit_events WHERE event_code = 'proposal.imported'")[0]
    assert event["actor_role"] == "ADMIN" and event["target_id"] == str(pid)


def test_the_client_file_name_is_never_used_or_stored(prop: ProposalEnv) -> None:
    prop.enable()
    prop.import_doc(filename="../../etc/cron.d/evil.json")
    names = [p.name for p in prop.directory.iterdir()]
    assert len(names) == 1 and NAME.match(names[0])


@pytest.mark.parametrize(
    "mime", ["text/plain", "application/json", "application/json; charset=utf-8"]
)
def test_both_allowed_types_import(prop: ProposalEnv, mime: str) -> None:
    prop.enable()
    assert (
        prop.import_bytes(json.dumps(prop.document()).encode(), mime=mime, filename=None).kind
        == "ok"
    )


HOSTILE_UPLOADS = [
    ("zip", "application/json", "a.json", b"PK\x03\x04" + b"\x00" * 30),
    ("zip-as-text", "text/plain", "a.txt", b"PK\x03\x04" + b'{"a":1}'),
    ("pdf", "application/json", "a.json", b"%PDF-1.4\n{}"),
    ("docx", "application/json", "a.json", b"PK\x03\x04\x14\x00\x06\x00word/document.xml"),
    ("xlsx", "text/plain", "a.txt", b"PK\x03\x04\x14\x00\x06\x00xl/workbook.xml"),
    ("csv", "text/plain", "a.txt", b"a,b\n1,2\n"),
    ("png", "application/json", "a.json", b"\x89PNG\r\n\x1a\n\x00\x00"),
    ("jpg", "application/json", "a.json", b"\xff\xd8\xff\xe0\x00\x10JFIF"),
    ("html", "text/plain", "a.txt", b"<html><script>alert(1)</script></html>"),
    ("js", "application/json", "a.json", b"function f(){return {}}"),
    ("yaml", "text/plain", "a.txt", b"---\nkey: value\n"),
    ("xml", "text/plain", "a.txt", b'<?xml version="1.0"?><a/>'),
    ("shell", "text/plain", "a.txt", b"#!/bin/sh\nrm -rf /\n"),
    ("elf", "application/json", "a.json", b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 30),
    ("exe", "application/json", "a.json", b"MZ\x90\x00" + b"\x00" * 30),
    ("gzip", "application/json", "a.json", b"\x1f\x8b\x08\x00" + b"\x00" * 30),
    ("array", "application/json", "a.json", b'[{"a": 1}]'),
    ("latin1", "application/json", "a.json", '{"a": "café"}'.encode("latin-1")),
    ("html-type", "text/html", "a.json", b'{"a": 1}'),
    ("zip-type", "application/zip", "a.json", b'{"a": 1}'),
    ("zip-name", "application/json", "a.zip", b'{"a": 1}'),
    ("exe-name", "text/plain", "a.json.exe", b'{"a": 1}'),
    ("oversize", "application/json", "a.json", b'{"a": "' + b"x" * (128 * 1024) + b'"}'),
]


@pytest.mark.parametrize(
    ("label", "mime", "name", "data"), HOSTILE_UPLOADS, ids=[h[0] for h in HOSTILE_UPLOADS]
)
def test_hostile_uploads_are_refused_stored_nowhere_and_do_not_spend_the_reauth(
    prop: ProposalEnv, sql: Sql, label: str, mime: str, name: str, data: bytes
) -> None:
    prop.enable()
    consumed = prop.reauth.consumed
    outcome = prop.import_bytes(data, mime=mime, filename=name)
    assert outcome.kind == "rejected_input" and len(outcome.reasons) == 1, label
    assert prop.reauth.consumed == consumed
    assert sql("SELECT count(*) AS n FROM proposals")[0]["n"] == 0
    assert not prop.directory.exists() or list(prop.directory.iterdir()) == []
    denied = sql("SELECT reason_code FROM audit_events WHERE event_code = 'proposal.denied'")
    assert denied and denied[-1]["reason_code"] == outcome.reasons[0]


def test_the_same_bytes_cannot_be_imported_twice_while_live(prop: ProposalEnv, sql: Sql) -> None:
    prop.enable()
    prop.import_doc()
    again = prop.import_bytes(json.dumps(prop.document()).encode())
    assert again.kind == "conflict" and again.reasons == ("DUPLICATE",)
    assert sql("SELECT count(*) AS n FROM proposals")[0]["n"] == 1


def test_a_rejected_proposal_can_be_resubmitted_as_a_new_import(
    prop: ProposalEnv, sql: Sql
) -> None:
    prop.enable()
    bad = prop.document(linked_review_package_sha256="0" * 64)
    first = prop.import_doc(bad)
    (result,) = prop.validate()
    assert result.state == "REJECTED"
    second = prop.import_doc(bad)
    assert second != first and sql("SELECT count(*) AS n FROM proposals")[0]["n"] == 2


# ------------------------------------------------------------------ limits
def test_at_most_ten_imports_per_hour_and_twenty_per_day(prop: ProposalEnv) -> None:
    prop.enable()
    for i in range(10):
        assert (
            prop.import_bytes(
                json.dumps(prop.document(proposal_id=f"prop-h1-{i:02d}")).encode()
            ).kind
            == "ok"
        )
    eleventh = prop.import_bytes(json.dumps(prop.document(proposal_id="prop-h1-99")).encode())
    assert eleventh.kind == "limit" and "RATE_LIMIT_HOUR" in eleventh.reasons
    prop.clock.advance(3700)
    for i in range(10):
        assert (
            prop.import_bytes(
                json.dumps(prop.document(proposal_id=f"prop-h2-{i:02d}")).encode()
            ).kind
            == "ok"
        )
    prop.clock.advance(3700)
    twenty_first = prop.import_bytes(json.dumps(prop.document(proposal_id="prop-h3-00")).encode())
    assert twenty_first.kind == "limit" and "RATE_LIMIT_DAY" in twenty_first.reasons
    prop.clock.advance(86400)
    assert (
        prop.import_bytes(json.dumps(prop.document(proposal_id="prop-d2-00")).encode()).kind == "ok"
    )


def test_the_database_enforces_the_rate_limit_too(prop: ProposalEnv, db: TestDb) -> None:
    prop.enable()
    for i in range(10):
        prop.import_bytes(json.dumps(prop.document(proposal_id=f"prop-db-{i:02d}")).encode())
    with role_conn(db, "td_app") as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="rate limit"):
            conn.execute(
                "INSERT INTO proposals (id, state, storage_name, size_bytes, sha256, "
                "declared_mime, "
                "imported_by, imported_at) VALUES "
                "(%s, 'IMPORTED', %s, 10, %s, 'text/plain', %s, %s)",
                (uuid4(), f"{uuid4()}.proposal", "b" * 64, prop.admin.user.id, prop.clock.now()),
            )


def test_the_total_storage_limit_blocks_further_imports(
    prop: ProposalEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    prop.enable()
    monkeypatch.setattr("app.proposals.service.MAX_STORED_BYTES", 100)
    prop.import_doc()
    blocked = prop.import_bytes(json.dumps(prop.document(proposal_id="prop-big-02")).encode())
    assert blocked.kind == "limit" and "STORAGE_LIMIT" in blocked.reasons


def test_a_failed_database_insert_leaves_no_file_behind(
    prop: ProposalEnv, monkeypatch: pytest.MonkeyPatch, sql: Sql
) -> None:
    prop.enable()

    def refuse(*_a: Any, **_k: Any) -> None:
        raise psycopg.errors.IntegrityConstraintViolation("refused")

    monkeypatch.setattr("app.storage.proposal_repositories.ProposalRepository.insert", refuse)
    outcome = prop.import_bytes(json.dumps(prop.document()).encode())
    assert outcome.kind == "limit" and outcome.reasons == ("DATABASE_REFUSED",)
    assert list(prop.directory.glob("*")) == []
    assert sql("SELECT count(*) AS n FROM proposals")[0]["n"] == 0


# ------------------------------------------------------------------ database roles and guards
def test_the_web_role_cannot_validate_and_the_host_role_cannot_review(
    prop: ProposalEnv, db: TestDb
) -> None:
    prop.enable()
    pid = prop.import_doc()
    with role_conn(db, "td_app") as conn:
        for statement in (
            "UPDATE proposals SET state = 'VALIDATING'",
            "UPDATE proposals SET state = 'VALIDATED'",
            "UPDATE proposals SET parsed = '{}'::jsonb",
            "UPDATE proposals SET findings = '[]'::jsonb",
            "UPDATE proposals SET risk_assessment = '{}'::jsonb",
            "UPDATE proposals SET category = 'risk'",
            "UPDATE proposals SET sha256 = repeat('0', 64)",
            "UPDATE proposals SET storage_name = 'x'",
            "UPDATE proposals SET content_removed_at = now()",
            "DELETE FROM proposals",
            "TRUNCATE proposals",
        ):
            with pytest.raises(psycopg.Error):
                conn.execute(statement)
            conn.rollback()
    with role_conn(db, "td_ctl") as conn:
        for statement in (
            "UPDATE proposals SET state = 'REVIEWED'",
            "UPDATE proposals SET review_notes = 'x'",
            "UPDATE proposals SET closed_reason = 'OTHER'",
            "UPDATE proposals SET imported_by = imported_by",
            "DELETE FROM proposals",
        ):
            with pytest.raises(psycopg.Error):
                conn.execute(statement)
            conn.rollback()
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO proposals (id, state, storage_name, size_bytes, sha256, "
                "declared_mime, "
                "imported_by, imported_at) VALUES "
                "(%s, 'IMPORTED', %s, 10, %s, 'text/plain', %s, now())",
                (uuid4(), f"{uuid4()}.proposal", "c" * 64, prop.admin.user.id),
            )
    assert pid


def test_illegal_transitions_are_refused_by_the_database_whoever_asks(
    prop: ProposalEnv, db: TestDb
) -> None:
    prop.enable()
    prop.import_doc()
    with role_conn(db, "td_ctl") as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute("UPDATE proposals SET state = 'VALIDATED'")  # skips VALIDATING
        conn.rollback()
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute("UPDATE proposals SET state = 'CLOSED'")
        conn.rollback()
    with role_conn(db, "td_app") as conn:
        for target in (
            "REVIEWED",
            "CHANGE_REQUEST_CREATED",
            "IMPLEMENTED",
            "BACKTESTED",
            "PAPER_VALIDATED",
            "CLOSED",
        ):
            with pytest.raises(psycopg.Error):
                conn.execute("UPDATE proposals SET state = %s", (target,))
            conn.rollback()


def test_records_are_append_only_and_history_is_required(
    prop: ProposalEnv, sql: Sql, db: TestDb
) -> None:
    prop.enable()
    pid = prop.validated()
    prop.review(pid)
    prop.change_request(pid)
    for statement in (
        "UPDATE change_requests SET impact_assessment = 'edited afterwards, quietly'",
        "DELETE FROM change_requests",
        "UPDATE proposal_state_history SET state_after = 'CLOSED'",
        "DELETE FROM proposal_state_history",
        "TRUNCATE proposal_state_history",
        "TRUNCATE change_requests",
    ):
        with pytest.raises(psycopg.Error):
            sql(statement)
    with role_conn(db, "td_app") as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="history"):
            conn.execute("UPDATE proposals SET state = 'CLOSED', closed_reason = 'OTHER'")
            conn.commit()


def test_the_change_request_state_needs_its_record_and_the_ceiling_attestation(
    prop: ProposalEnv, db: TestDb
) -> None:
    prop.enable()
    pid = prop.validated()
    prop.review(pid)
    history_sql = (
        "INSERT INTO proposal_state_history (proposal_id, state_before, state_after, "
        "actor_class, occurred_at) VALUES (%s, 'REVIEWED', 'CHANGE_REQUEST_CREATED', 'WEB', now())"
    )
    with role_conn(db, "td_app") as conn:
        conn.execute("UPDATE proposals SET state = 'CHANGE_REQUEST_CREATED' WHERE id = %s", (pid,))
        conn.execute(history_sql, (pid,))
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="change request"):
            conn.commit()
    with role_conn(db, "td_app") as conn:
        conn.execute("UPDATE proposals SET state = 'CHANGE_REQUEST_CREATED' WHERE id = %s", (pid,))
        conn.execute(
            "INSERT INTO proposal_state_history (proposal_id, state_before, state_after, "
            "actor_class, occurred_at) VALUES "
            "(%s, 'REVIEWED', 'CHANGE_REQUEST_CREATED', 'WEB', now())",
            (pid,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO change_requests (id, proposal_id, change_type, impact_assessment, "
                "ceilings_unaffected, created_by, created_at) VALUES (%s, %s, 'PARAMETER_CHANGE', "
                "'twenty characters of text here', false, %s, now())",
                (uuid4(), pid, prop.admin.user.id),
            )


# ------------------------------------------------------------------ validation (host)
def only(prop: ProposalEnv) -> Any:
    (result,) = prop.validate()
    return result


def test_a_good_proposal_becomes_validated_with_parsed_fields_and_a_risk_assessment(
    prop: ProposalEnv, sql: Sql
) -> None:
    prop.enable()
    pid = prop.import_doc()
    result = only(prop)
    assert (result.state, result.rules) == ("VALIDATED", ())
    row = sql("SELECT * FROM proposals WHERE id = %s", (pid,))[0]
    assert row["proposal_ref"] == "prop-2026-001" and row["category"] == "strategy"
    assert row["linked_package_id"] == prop.package.id
    assert row["parsed"]["suggested_change"].startswith("Raise the minimum band ratio")
    assert row["findings"] == [] and row["reject_rules"] == []
    assert row["risk_assessment"]["level"] in {"LOW", "MEDIUM", "HIGH"}
    assert row["risk_assessment"]["advisory_only"] is True
    assert [
        h["state_after"] for h in sql("SELECT state_after FROM proposal_state_history ORDER BY id")
    ] == ["IMPORTED", "VALIDATING", "VALIDATED"]
    assert {"proposal.validating", "proposal.validated"} <= set(codes(sql))
    assert prop.validate() == []  # nothing left to do


REJECTIONS = [
    ("unknown-package", {"linked_review_package_id": str(uuid4())}, "LINK_PACKAGE_UNKNOWN"),
    ("wrong-hash", {"linked_review_package_sha256": "0" * 64}, "LINK_PACKAGE_HASH_MISMATCH"),
    (
        "evidence-not-in-package",
        {"evidence_references": ["data/paper_orders.jsonl"]},
        "EVIDENCE_NOT_IN_PACKAGE",
    ),
    (
        "evidence-unknown-report",
        {"evidence_references": ["efficiency_summary.json", f"report:{uuid4()}"]},
        "EVIDENCE_UNKNOWN_ID",
    ),
    (
        "evidence-unknown-snapshot",
        {"evidence_references": [f"snapshot:{uuid4()}"]},
        "EVIDENCE_UNKNOWN_ID",
    ),
    ("unknown-field", {"apply_now": True}, "UNKNOWN_FIELD"),
    ("bad-category", {"category": "trading"}, "BAD_VALUE"),
    ("bad-version", {"proposal_version": 2}, "BAD_VERSION"),
    (
        "risk-bypass",
        {"suggested_change": "Disable the reserve check so more capital can trade."},
        "RISK_BYPASS",
    ),
    (
        "secret",
        {"suggested_change": "Rotate the API key and paste it into the settings file."},
        "SECRET_CHANGE",
    ),
    (
        "live",
        {"suggested_change": "Then go live with real money after the paper run."},
        "LIVE_ACTIVATION",
    ),
    (
        "pair",
        {"suggested_change": "Activate the ETH-USDC pair immediately after the run."},
        "PAIR_STATE_CHANGE",
    ),
    (
        "capital",
        {"suggested_change": "Double the deployment cap when profits rise each week."},
        "CAPITAL_INCREASE",
    ),
    (
        "orders",
        {"suggested_change": "Place market orders automatically on each breakout signal."},
        "AUTOMATED_ORDER",
    ),
    (
        "auto-apply",
        {"suggested_change": "Apply this change automatically without review once tests pass."},
        "AUTO_APPLICATION",
    ),
    (
        "api",
        {"suggested_change": "Grant the bot private API access to the exchange account."},
        "API_ACCESS_CHANGE",
    ),
    (
        "security",
        {"suggested_change": "Disable CSRF checks for the import form to save time."},
        "SECURITY_CONTROL_CHANGE",
    ),
    (
        "guarantee",
        {"expected_benefit": "This guarantees profit in every market condition."},
        "PROFIT_GUARANTEE",
    ),
    (
        "markup",
        {"suggested_change": "<script>alert(1)</script> and then narrow the band a bit."},
        "MARKUP_OR_TEMPLATE",
    ),
    (
        "code",
        {"suggested_change": "Run sudo systemctl restart the bot after the change."},
        "EXECUTABLE_CONTENT",
    ),
    (
        "link",
        {"suggested_change": "See https://example.com/notes for the full parameters."},
        "EXTERNAL_REFERENCE",
    ),
    ("no-trade-false", {"should_remain_no_trade_until_validated": False}, "NO_TRADE_NOT_PRESERVED"),
    ("no-declaration", {"no_profit_guarantee": False}, "PROFIT_DECLARATION_MISSING"),
]


@pytest.mark.parametrize(("label", "changes", "rule"), REJECTIONS, ids=[r[0] for r in REJECTIONS])
def test_bad_or_forbidden_proposals_are_rejected_with_the_rule_recorded(
    prop: ProposalEnv, sql: Sql, label: str, changes: dict[str, Any], rule: str
) -> None:
    prop.enable()
    pid = prop.import_doc(prop.document(**changes))
    result = only(prop)
    assert result.state == "REJECTED" and any(r.split(":")[0] == rule for r in result.rules), (
        label,
        result,
    )
    row = sql("SELECT state, reject_rules FROM proposals WHERE id = %s", (pid,))[0]
    assert row["state"] == "REJECTED" and row["reject_rules"]
    assert "proposal.rejected" in codes(sql)


def test_policy_findings_and_a_blocked_risk_level_are_stored_with_the_parsed_text(
    prop: ProposalEnv, sql: Sql
) -> None:
    prop.enable()
    pid = prop.import_doc(
        prop.document(suggested_change="Disable the reserve check so more capital can trade.")
    )
    only(prop)
    row = sql("SELECT * FROM proposals WHERE id = %s", (pid,))[0]
    assert {f["rule"] for f in row["findings"]} >= {"RISK_BYPASS"}
    assert all(f["severity"] == "BLOCK" for f in row["findings"])
    assert row["risk_assessment"]["level"] == "BLOCKED"
    assert row["parsed"] is not None  # kept, escaped on display, so the reviewer can see why


def test_a_corrupt_review_package_is_not_a_valid_link(prop: ProposalEnv, review: Any) -> None:
    prop.enable()
    path = review.review_dir / prop.package.storage_name
    path.chmod(0o644)
    path.write_bytes(path.read_bytes()[:-20])
    assert review.builder.verify(prop.package.id)
    prop.import_doc()
    assert "LINK_PACKAGE_STATE" in only(prop).rules


def test_an_expired_package_still_links_because_its_hash_and_file_list_are_kept(
    prop: ProposalEnv, review: Any
) -> None:
    prop.enable()
    prop.clock.advance(30 * 86400)
    review.builder.cleanup()
    prop.import_doc()
    assert only(prop).state == "VALIDATED"


def test_existing_report_ids_are_valid_evidence(prop: ProposalEnv, sql: Sql) -> None:
    prop.enable()
    rid = add_report(sql, "BACKTEST", prop.clock.now())
    prop.import_doc(prop.document(evidence_references=["efficiency_summary.json", f"report:{rid}"]))
    assert only(prop).state == "VALIDATED"


def test_a_tampered_stored_file_is_rejected_not_parsed(prop: ProposalEnv, sql: Sql) -> None:
    prop.enable()
    pid = prop.import_doc()
    name = sql("SELECT storage_name FROM proposals WHERE id = %s", (pid,))[0]["storage_name"]
    path = prop.directory / name
    path.chmod(0o644)
    path.write_bytes(
        json.dumps(
            prop.document(suggested_change="A different, sneaky, replacement text.")
        ).encode()
    )
    assert only(prop).rules == ("FILE_CHECKSUM_MISMATCH",)


def test_a_missing_or_symlinked_stored_file_is_rejected(prop: ProposalEnv, sql: Sql) -> None:
    prop.enable()
    a = prop.import_doc()
    b = prop.import_doc(prop.document(proposal_id="prop-2026-002"))
    name_a = sql("SELECT storage_name FROM proposals WHERE id = %s", (a,))[0]["storage_name"]
    name_b = sql("SELECT storage_name FROM proposals WHERE id = %s", (b,))[0]["storage_name"]
    (prop.directory / name_a).chmod(0o644)
    (prop.directory / name_a).unlink()
    target = prop.directory.parent / "elsewhere.txt"
    target.write_text(json.dumps(prop.document(proposal_id="prop-2026-002")))
    (prop.directory / name_b).chmod(0o644)
    (prop.directory / name_b).unlink()
    (prop.directory / name_b).symlink_to(target)
    results = {r.proposal_id: r for r in prop.validate()}
    assert results[a].rules == ("FILE_MISSING",) and results[b].rules == ("FILE_UNREADABLE",)


def test_the_validator_rechecks_the_declared_type_and_bytes(prop: ProposalEnv, sql: Sql) -> None:
    prop.enable()
    pid = prop.import_doc()
    name = sql("SELECT storage_name FROM proposals WHERE id = %s", (pid,))[0]["storage_name"]
    path = prop.directory / name
    # same bytes on disk, but a row whose checksum was forged to match a zip: refused by content
    zipish = b"PK\x03\x04" + b'{"a": 1}'
    import hashlib

    path.chmod(0o644)
    path.write_bytes(zipish)
    sql("ALTER TABLE proposals DISABLE TRIGGER USER")
    sql(
        "UPDATE proposals SET sha256 = %s, size_bytes = %s WHERE id = %s",
        (hashlib.sha256(zipish).hexdigest(), len(zipish), pid),
    )
    sql("ALTER TABLE proposals ENABLE TRIGGER USER")
    assert only(prop).rules == ("ARCHIVE_OR_OFFICE_FILE",)


def test_proposal_text_is_stored_verbatim_and_never_evaluated(prop: ProposalEnv, sql: Sql) -> None:
    prop.enable()
    text = "Insert {{ 7 * 7 }} and ${HOME} and <b>bold</b> into the description of the change."
    pid = prop.import_doc(prop.document(suggested_change=text))
    only(prop)
    row = sql("SELECT parsed FROM proposals WHERE id = %s", (pid,))[0]
    assert row["parsed"]["suggested_change"] == text  # literal, not "49", not expanded


def test_an_unexpected_validator_error_rejects_instead_of_hanging(
    prop: ProposalEnv, monkeypatch: pytest.MonkeyPatch, sql: Sql
) -> None:
    prop.enable()
    pid = prop.import_doc()

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr("app.proposals.validator.policy.evaluate", boom)
    assert only(prop).rules == ("VALIDATOR_ERROR",)
    assert prop.state(sql, pid) == "REJECTED"


# ------------------------------------------------------------------ review and change request
def test_review_needs_a_validated_proposal_and_plain_notes(prop: ProposalEnv, sql: Sql) -> None:
    prop.enable()
    pid = prop.import_doc()
    assert prop.review(pid).kind == "not_allowed"  # still IMPORTED
    prop.validate()
    for bad in ("", "   ", "x" * 1001, "hidden‮notes", "bell\x07 notes"):
        assert prop.review(pid, bad).kind == "invalid"
    assert prop.review(pid, "Worth a careful manual look at the fee model.").kind == "ok"
    assert prop.state(sql, pid) == "REVIEWED"
    assert sql("SELECT review_notes, reviewed_by FROM proposals")[0] == {
        "review_notes": "Worth a careful manual look at the fee model.",
        "reviewed_by": prop.admin.user.id,
    }
    assert prop.review(pid).kind == "not_allowed"
    assert "proposal.reviewed" in codes(sql)


def test_a_rejected_proposal_cannot_be_reviewed_or_approved(prop: ProposalEnv, sql: Sql) -> None:
    prop.enable()
    pid = prop.import_doc(
        prop.document(suggested_change="Disable the reserve check so more capital can trade.")
    )
    prop.validate()
    assert prop.review(pid).kind == "not_allowed"
    assert prop.change_request(pid).kind == "not_allowed"
    assert prop.state(sql, pid) == "REJECTED"


def test_the_change_request_needs_its_own_phrase_with_the_proposal_id_and_a_fresh_reauth(
    prop: ProposalEnv, sql: Sql
) -> None:
    prop.enable()
    pid = prop.validated()
    prop.review(pid)
    other = uuid4()
    consumed = prop.reauth.consumed
    for typed in (
        "CREATE MANUAL CHANGE REQUEST",
        phrase_change_request(other),
        phrase_change_request(pid).lower(),
        phrase_change_request(pid) + " ",
        phrase_change_request(str(pid)[:8]),
        PHRASE_IMPORT,
        "",
    ):
        assert prop.change_request(pid, typed=typed).kind == "phrase_mismatch", typed
    assert prop.reauth.consumed == consumed
    assert prop.change_request(pid, fresh=False).kind == "reauth_required"
    assert (
        prop.state(sql, pid) == "REVIEWED"
        and sql("SELECT count(*) AS n FROM change_requests")[0]["n"] == 0
    )
    assert prop.change_request(pid).kind == "ok"
    assert prop.state(sql, pid) == "CHANGE_REQUEST_CREATED"


def test_the_change_request_is_a_record_with_a_number_and_nothing_else(
    prop: ProposalEnv, sql: Sql
) -> None:
    prop.enable()
    pid = prop.validated()
    prop.review(pid)
    assert prop.change_request(pid, change_type="CODE_CHANGE", ceilings=False).kind == "ok"
    cr = sql("SELECT * FROM change_requests")[0]
    assert cr["proposal_id"] == pid and cr["change_type"] == "CODE_CHANGE" and cr["seq"] >= 1
    assert cr["created_by"] == prop.admin.user.id
    assert "proposal.change_request_created" in codes(sql)
    assert prop.change_request(pid).kind == "not_allowed"  # only one per proposal


@pytest.mark.parametrize(
    "kwargs",
    [
        {"change_type": "DELETE_EVERYTHING"},
        {"change_type": ""},
        {"impact": "too short"},
        {"impact": "x" * 2001},
        {"impact": "hidden‮ text that is long enough to pass the length check"},
        {"change_type": "PARAMETER_CHANGE", "ceilings": False},
    ],
)
def test_invalid_change_requests_are_refused_before_the_chain(
    prop: ProposalEnv, sql: Sql, kwargs: dict[str, Any]
) -> None:
    prop.enable()
    pid = prop.validated()
    prop.review(pid)
    consumed = prop.reauth.consumed
    assert prop.change_request(pid, **kwargs).kind == "invalid"
    assert prop.reauth.consumed == consumed and prop.state(sql, pid) == "REVIEWED"


# ------------------------------------------------- attestations and the rest of the lifecycle
def to_change_request(prop: ProposalEnv) -> UUID:
    prop.enable()
    pid = prop.validated()
    prop.review(pid)
    assert prop.change_request(pid).kind == "ok"
    return pid


def test_steps_cannot_be_skipped_or_reordered(prop: ProposalEnv, sql: Sql) -> None:
    pid = to_change_request(prop)
    later = prop.clock.now() + timedelta(hours=1)
    bt = add_report(sql, "BACKTEST", later)
    assert prop.attest(pid, "BACKTESTED", report_ids=[str(bt)]).kind == "not_allowed"
    assert prop.attest(pid, "PAPER_VALIDATED", report_ids=[str(bt)]).kind == "not_allowed"
    assert (
        prop.service.close(prop.actor, pid, "COMPLETED").kind == "ok"
    )  # CR states may close early
    assert prop.attest(pid, "IMPLEMENTED", reference="v0.7.1").kind == "not_allowed"


@pytest.mark.parametrize(
    "ref", ["", "ab", "x" * 65, "has space", "../../x", "v1;rm -rf", "<b>v1</b>", "v1\nv2"]
)
def test_the_release_reference_is_pattern_limited(prop: ProposalEnv, ref: str) -> None:
    pid = to_change_request(prop)
    consumed = prop.reauth.consumed
    assert prop.attest(pid, "IMPLEMENTED", reference=ref or None).kind == "invalid"
    assert prop.reauth.consumed == consumed


def test_the_whole_lifecycle_to_closed_with_every_gate(prop: ProposalEnv, sql: Sql) -> None:
    pid = to_change_request(prop)
    base = prop.clock.now()
    assert (
        prop.attest(pid, "IMPLEMENTED", reference="v0.7.1", fresh=False).kind == "reauth_required"
    )
    assert prop.attest(pid, "IMPLEMENTED", reference="v0.7.1").kind == "ok"
    # backtest evidence: must exist, be the right kind, and post-date the change request
    old = add_report(sql, "BACKTEST", base - timedelta(days=1))
    assert prop.attest(pid, "BACKTESTED", report_ids=[str(old)]).reasons == ("REPORT_TOO_OLD",)
    assert prop.attest(pid, "BACKTESTED", report_ids=[str(uuid4())]).reasons == ("REPORT_UNKNOWN",)
    paper_kind = add_report(sql, "PAPER_DAILY", base + timedelta(hours=2))
    assert prop.attest(pid, "BACKTESTED", report_ids=[str(paper_kind)]).reasons == ("REPORT_KIND",)
    assert prop.attest(pid, "BACKTESTED", report_ids=["not-a-uuid"]).reasons == ("REPORT_IDS",)
    assert prop.attest(pid, "BACKTESTED", report_ids=[]).reasons == ("REPORT_IDS",)
    fresh_bt = add_report(sql, "WALK_FORWARD", base + timedelta(hours=3))
    assert prop.attest(pid, "BACKTESTED", report_ids=[str(fresh_bt), str(fresh_bt)]).reasons == (
        "REPORT_IDS",
    )
    assert prop.attest(pid, "BACKTESTED", report_ids=[str(fresh_bt)]).kind == "ok"
    # paper evidence: right kind, after the backtest step, and spanning the minimum duration
    p1 = add_report(sql, "PAPER_DAILY", base + timedelta(days=1))
    p2 = add_report(sql, "PAPER_DAILY", base + timedelta(days=3))
    assert prop.attest(pid, "PAPER_VALIDATED", report_ids=[str(p1)]).reasons == ("PAPER_DURATION",)
    assert prop.attest(pid, "PAPER_VALIDATED", report_ids=[str(p1), str(p2)]).reasons == (
        "PAPER_DURATION",
    )
    p3 = add_report(sql, "PAPER_DAILY", base + timedelta(days=9))
    assert prop.attest(pid, "PAPER_VALIDATED", report_ids=[str(fresh_bt), str(p3)]).reasons == (
        "REPORT_KIND",
    )
    assert prop.attest(pid, "PAPER_VALIDATED", report_ids=[str(p1), str(p3)]).kind == "ok"
    assert prop.service.close(prop.actor, pid, "COMPLETED").kind == "ok"
    assert prop.state(sql, pid) == "CLOSED"
    order = [
        h["state_after"] for h in sql("SELECT state_after FROM proposal_state_history ORDER BY id")
    ]
    assert order == [
        "IMPORTED",
        "VALIDATING",
        "VALIDATED",
        "REVIEWED",
        "CHANGE_REQUEST_CREATED",
        "IMPLEMENTED",
        "BACKTESTED",
        "PAPER_VALIDATED",
        "CLOSED",
    ]
    assert {
        "proposal.implemented",
        "proposal.backtested",
        "proposal.paper_validated",
        "proposal.closed",
    } <= set(codes(sql))
    kinds = [a["kind"] for a in sql("SELECT kind FROM proposal_attestations ORDER BY id")]
    assert kinds == ["IMPLEMENTED", "BACKTESTED", "PAPER_VALIDATED"]
    assert prop.service.close(prop.actor, pid, "COMPLETED").kind == "not_allowed"  # terminal


@pytest.mark.parametrize("reason", ["BOGUS", "", "completed", "COMPLETED "])
def test_close_needs_a_known_reason(prop: ProposalEnv, reason: str) -> None:
    pid = to_change_request(prop)
    assert prop.service.close(prop.actor, pid, reason).kind == "invalid"


@pytest.mark.parametrize("stage", ["IMPORTED", "VALIDATED", "IMPLEMENTED"])
def test_close_is_only_available_from_the_documented_states(
    prop: ProposalEnv, sql: Sql, stage: str
) -> None:
    prop.enable()
    pid = prop.import_doc()
    if stage != "IMPORTED":
        prop.validate()
    if stage == "IMPLEMENTED":
        prop.review(pid)
        prop.change_request(pid)
        prop.attest(pid, "IMPLEMENTED", reference="v0.7.1")
    assert prop.service.close(prop.actor, pid, "NOT_PURSUED").kind == "not_allowed"
    assert prop.state(sql, pid) == stage


def test_rejected_reviewed_and_change_request_proposals_can_be_closed(
    prop: ProposalEnv, sql: Sql
) -> None:
    prop.enable()
    bad = prop.import_doc(
        prop.document(linked_review_package_sha256="0" * 64, proposal_id="prop-x-001")
    )
    prop.validate()
    assert prop.service.close(prop.actor, bad, "UNSAFE").kind == "ok"
    reviewed = prop.validated(prop.document(proposal_id="prop-x-002"))
    prop.review(reviewed)
    assert prop.service.close(prop.actor, reviewed, "NOT_PURSUED").kind == "ok"
    row = sql("SELECT closed_reason, closed_at FROM proposals WHERE id = %s", (reviewed,))[0]
    assert row["closed_reason"] == "NOT_PURSUED" and row["closed_at"] is not None


def test_unknown_proposals_are_not_found(prop: ProposalEnv) -> None:
    ghost = uuid4()
    assert prop.review(ghost).kind == "not_found"
    assert prop.change_request(ghost).kind == "not_found"
    assert prop.attest(ghost, "IMPLEMENTED", reference="v0.7.1").kind == "not_found"
    assert prop.service.close(prop.actor, ghost, "OTHER").kind == "not_found"


def test_every_refusal_is_audited_without_echoing_input(prop: ProposalEnv, sql: Sql) -> None:
    prop.enable()
    pid = prop.validated()
    prop.review(pid)
    prop.change_request(pid, typed="wrong phrase <script>alert(1)</script>")
    rows = sql("SELECT * FROM audit_events WHERE event_code = 'proposal.denied'")
    assert rows and rows[-1]["reason_code"] == "PHRASE_MISMATCH"
    blob = json.dumps([{k: str(v) for k, v in r.items()} for r in rows])
    assert "<script>" not in blob and "wrong phrase" not in blob


# ------------------------------------------------------------------ approval changes nothing else
def test_the_whole_lifecycle_changes_no_pair_order_ledger_config_or_review_state(
    prop: ProposalEnv, sql: Sql
) -> None:
    before = fingerprint(sql)
    policy_before = prop.settings.pair_policy.model_dump_json()
    pid = to_change_request(prop)
    base = prop.clock.now()
    prop.attest(pid, "IMPLEMENTED", reference="v0.7.1")
    bt = add_report(sql, "BACKTEST", base + timedelta(hours=1))
    before_reports = sql("SELECT count(*) AS n FROM reports")[0]["n"]
    prop.attest(pid, "BACKTESTED", report_ids=[str(bt)])
    p1 = add_report(sql, "PAPER_DAILY", base + timedelta(days=1))
    p2 = add_report(sql, "PAPER_DAILY", base + timedelta(days=9))
    prop.attest(pid, "PAPER_VALIDATED", report_ids=[str(p1), str(p2)])
    prop.service.close(prop.actor, pid, "COMPLETED")
    after = fingerprint(sql)
    changed = {t for t in UNTOUCHED if before[t] != after[t]}
    assert changed == {"reports"}, changed  # only the reports this test itself inserted
    assert sql("SELECT count(*) AS n FROM reports")[0]["n"] == before_reports + 2
    assert prop.settings.pair_policy.model_dump_json() == policy_before
    assert sql("SELECT count(*) AS n FROM change_requests")[0]["n"] == 1


def test_approval_writes_only_proposal_records_and_the_audit_log(
    prop: ProposalEnv, sql: Sql
) -> None:
    pid = prop.validated() if prop.enable().kind == "ok" else None
    prop.review(pid)  # type: ignore[arg-type]
    before = fingerprint(sql)
    tables = sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
    counts_before = {
        t["tablename"]: sql(f"SELECT count(*) AS n FROM {t['tablename']}")[0]["n"] for t in tables
    }  # noqa: S608
    prop.change_request(pid)  # type: ignore[arg-type]
    counts_after = {
        t["tablename"]: sql(f"SELECT count(*) AS n FROM {t['tablename']}")[0]["n"] for t in tables
    }  # noqa: S608
    grew = {t for t in counts_after if counts_after[t] != counts_before[t]}
    assert grew <= {"change_requests", "proposal_state_history", "audit_events", "audit_head"}, grew
    assert fingerprint(sql) == before


# ------------------------------------------------------------------ retention
def test_old_closed_and_rejected_content_is_purged_but_the_record_stays(
    prop: ProposalEnv, sql: Sql
) -> None:
    prop.enable()
    rejected = prop.import_doc(prop.document(linked_review_package_sha256="0" * 64))
    live = prop.import_doc(prop.document(proposal_id="prop-live-01"))
    prop.validate()
    names = {r["id"]: r["storage_name"] for r in sql("SELECT id, storage_name FROM proposals")}
    assert prop.validator.cleanup().purged == 0  # not old enough
    prop.clock.advance(91 * 86400)
    result = prop.validator.cleanup()
    assert result.purged == 1
    assert (
        not (prop.directory / names[rejected]).exists() and (prop.directory / names[live]).exists()
    )
    row = sql("SELECT state, content_removed_at, sha256 FROM proposals WHERE id = %s", (rejected,))[
        0
    ]
    assert row["state"] == "REJECTED" and row["content_removed_at"] is not None and row["sha256"]
    assert "proposal.cleanup" in codes(sql)
    assert prop.validator.cleanup().purged == 0  # idempotent


def test_orphans_and_temp_files_are_removed_but_referenced_files_never(
    prop: ProposalEnv, sql: Sql
) -> None:
    prop.enable()
    prop.import_doc()
    kept = sql("SELECT storage_name FROM proposals")[0]["storage_name"]
    orphan = prop.directory / f"{uuid4()}.proposal"
    orphan.write_bytes(b"left over")
    temp = prop.directory / f".{'a' * 32}.tmp"
    temp.write_bytes(b"partial")
    note = prop.directory / "notes.txt"
    note.write_bytes(b"not ours")
    assert prop.validator.cleanup().orphans == 2
    assert not orphan.exists() and not temp.exists()
    assert (prop.directory / kept).exists() and note.exists()


def test_only_the_host_removes_content_and_only_from_closed_or_rejected(
    prop: ProposalEnv, db: TestDb
) -> None:
    prop.enable()
    prop.import_doc()
    with role_conn(db, "td_ctl") as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="CLOSED or REJECTED"):
            conn.execute("UPDATE proposals SET content_removed_at = now()")
