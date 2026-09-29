"""Review packages at service level: chain, limits, build, verify, download, retention."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from datetime import date
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from app.review import package as pkg
from app.review import sanitizer as sz
from app.review.service import PHRASES
from app.review.store import NAME
from tests.conftest import TestDb
from tests.integration.conftest import Market
from tests.integration.review_env import TABLES, ReviewEnv
from tests.integration.test_market_ingest import role_conn

Sql = Callable[..., list[dict[str, Any]]]
START, END = date(2026, 9, 1), date(2026, 9, 28)


def enabled(review: ReviewEnv, days: int = 14) -> ReviewEnv:
    assert review.chain("enable", days=days).kind == "ok"
    return review


def requested(review: ReviewEnv, **kw: Any) -> Any:
    outcome = review.chain("create", start=START, end=END, **kw)
    assert outcome.kind == "ok" and outcome.package_id, outcome
    return outcome.package_id


def ready(review: ReviewEnv, **kw: Any) -> Any:
    pid = requested(review, **kw)
    results = review.builder.build_pending()
    assert [r.state for r in results] == ["READY"], results
    return pid


def codes(sql: Sql) -> list[str]:
    return [r["event_code"] for r in sql("SELECT event_code FROM audit_events ORDER BY seq")]


def fingerprint(sql: Sql) -> dict[str, str]:
    return {
        t: sql(
            f"SELECT md5(COALESCE(string_agg(x::text, '|' ORDER BY x::text), '')) AS h FROM {t} x"
        )[  # noqa: S608
            0
        ]["h"]
        for t in TABLES
    }


# ------------------------------------------------------------------ disabled by default
def test_the_feature_is_disabled_by_default(review: ReviewEnv, sql: Sql) -> None:
    row = sql("SELECT enabled, retention_days FROM review_settings")[0]
    assert row["enabled"] is False
    overview = review.service.overview(review.ctx)
    assert overview.settings.enabled is False and overview.create_blockers == ("FEATURE_DISABLED",)


def test_requests_are_refused_and_audited_while_disabled(review: ReviewEnv, sql: Sql) -> None:
    outcome = review.chain("create", start=START, end=END)
    assert outcome.kind == "disabled"
    assert sql("SELECT count(*) AS n FROM review_packages")[0]["n"] == 0
    denied = sql("SELECT * FROM audit_events WHERE event_code = 'review.denied'")
    assert len(denied) == 1 and denied[0]["reason_code"] == "FEATURE_DISABLED"


def test_the_database_itself_refuses_a_request_while_disabled(
    review: ReviewEnv, db: TestDb
) -> None:
    with role_conn(db, "td_app") as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="disabled"):
            conn.execute(
                "INSERT INTO review_packages (id, state, period_start, period_end, scope, "
                "retention_days, requested_by, requested_at) VALUES (%s, 'REQUESTED', %s, %s, "
                "ARRAY['backtests'], 14, %s, now())",
                (uuid4(), START, END, review.admin.user.id),
            )


# ------------------------------------------------------------------ enable / disable chain
def test_enable_needs_the_exact_phrase_and_a_fresh_reauth(review: ReviewEnv, sql: Sql) -> None:
    for typed in (
        "enable read-only review packages",
        "ENABLE READ-ONLY REVIEW PACKAGES ",
        "ENABLE READ-ONLY REVIEW PACKAGE",
        "ENABLE READ‐ONLY REVIEW PACKAGES",
        "",
    ):
        review.reauth.available = True
        got = review.service.enable(review.ctx, review.actor, typed, 14)
        assert got.kind == "phrase_mismatch", typed
    assert review.reauth.consumed == 0  # a wrong phrase never spends the reauthentication
    review.reauth.available = False
    assert review.service.enable(review.ctx, review.actor, PHRASES["enable"], 14).kind == (
        "reauth_required"
    )
    assert sql("SELECT enabled FROM review_settings")[0]["enabled"] is False
    assert codes(sql).count("review.denied") == 6


def test_enable_with_the_phrase_and_reauth_records_retention_and_audits(
    review: ReviewEnv, sql: Sql
) -> None:
    assert review.chain("enable", days=30).kind == "ok"
    assert review.reauth.consumed == 1
    row = sql("SELECT enabled, retention_days FROM review_settings")[0]
    assert row == {"enabled": True, "retention_days": 30}
    event = sql("SELECT * FROM audit_events WHERE event_code = 'review.enabled'")[0]
    assert event["actor_role"] == "ADMIN" and event["result"] == "SUCCESS"
    assert review.chain("enable").kind == "conflict"  # already enabled


@pytest.mark.parametrize("days", [0, 91, -1])
def test_retention_must_be_between_one_and_ninety_days(review: ReviewEnv, days: int) -> None:
    assert review.chain("enable", days=days).kind == "invalid"


def test_the_reauth_is_single_use(review: ReviewEnv) -> None:
    assert review.chain("enable").kind == "ok"
    # no fresh reauth: the disable is refused although the phrase is right
    assert review.service.disable(review.ctx, review.actor, PHRASES["disable"]).kind == (
        "reauth_required"
    )


def test_disable_needs_its_own_phrase_and_reauth(review: ReviewEnv, sql: Sql) -> None:
    enabled(review)
    assert review.chain("disable", typed=PHRASES["enable"]).kind == "phrase_mismatch"
    assert review.chain("disable", typed="DISABLE READ-ONLY REVIEW PACKAGES").kind == "ok"
    assert sql("SELECT enabled FROM review_settings")[0]["enabled"] is False
    assert "review.disabled" in codes(sql)
    assert review.chain("disable").kind == "conflict"


def test_disabling_blocks_requests_and_downloads(review: ReviewEnv) -> None:
    enabled(review)
    pid = ready(review)
    assert review.chain("disable").kind == "ok"
    assert review.chain("create", start=START, end=END).kind == "disabled"
    assert review.service.download(review.actor, pid).outcome.kind == "disabled"
    assert review.service.verify(review.actor, pid).outcome.kind == "disabled"


# ------------------------------------------------------------------ create
def test_create_needs_its_own_exact_phrase(review: ReviewEnv, sql: Sql) -> None:
    enabled(review)
    for typed in (PHRASES["enable"], "create read-only review package", "CREATE REVIEW PACKAGE"):
        assert review.chain("create", start=START, end=END, typed=typed).kind == "phrase_mismatch"
    review.reauth.available = False
    got = review.service.request(
        review.ctx, review.actor, PHRASES["create"], START, END, ["backtests"]
    )
    assert got.kind == "reauth_required"
    assert sql("SELECT count(*) AS n FROM review_packages")[0]["n"] == 0


def test_create_records_a_request_only_and_audits_it(review: ReviewEnv, sql: Sql) -> None:
    enabled(review, days=7)
    pid = requested(review)
    row = sql("SELECT * FROM review_packages WHERE id = %s", (pid,))[0]
    assert row["state"] == "REQUESTED" and row["retention_days"] == 7
    assert row["requested_by"] == review.admin.user.id
    assert row["storage_name"] is None and row["package_sha256"] is None
    assert list(review.review_dir.glob("*")) == []  # nothing built yet
    event = sql("SELECT * FROM audit_events WHERE event_code = 'review.requested'")[0]
    assert str(pid) == event["target_id"] and event["actor_role"] == "ADMIN"


@pytest.mark.parametrize(
    ("start", "end", "scope"),
    [
        (date(2026, 9, 10), date(2026, 9, 1), ["backtests"]),
        (date(2026, 9, 1), date(2026, 10, 30), ["backtests"]),
        (date(2026, 1, 1), date(2026, 9, 28), ["backtests"]),
        (date(2026, 9, 1), date(2026, 9, 2), []),
        (date(2026, 9, 1), date(2026, 9, 2), ["everything"]),
        (date(2026, 9, 1), date(2026, 9, 2), ["backtests", "backtests"]),
    ],
)
def test_invalid_periods_and_scopes_are_refused_before_the_chain(
    review: ReviewEnv, start: date, end: date, scope: list[str], sql: Sql
) -> None:
    enabled(review)
    review.reauth.available = True
    got = review.service.request(review.ctx, review.actor, PHRASES["create"], start, end, scope)
    assert got.kind == "invalid" and review.reauth.consumed == 1  # only the enable used it
    assert sql("SELECT count(*) AS n FROM review_packages")[0]["n"] == 0


def test_only_one_package_can_wait_or_build_at_a_time(review: ReviewEnv, sql: Sql) -> None:
    enabled(review)
    requested(review)
    again = review.chain("create", start=START, end=END)
    assert again.kind == "limit" and "PACKAGE_BEING_BUILT" in again.reasons
    assert "review.denied" in codes(sql)


def test_at_most_three_requests_per_hour(review: ReviewEnv) -> None:
    enabled(review)
    for _ in range(3):
        ready(review)
    fourth = review.chain("create", start=START, end=END)
    assert fourth.kind == "limit" and "RATE_LIMITED" in fourth.reasons
    review.clock.advance(3700)
    assert review.chain("create", start=START, end=END).kind == "ok"


def test_at_most_ten_packages_are_retained(review: ReviewEnv) -> None:
    enabled(review)
    for _ in range(10):
        ready(review)
        review.clock.advance(3700)
    eleventh = review.chain("create", start=START, end=END)
    assert eleventh.kind == "limit" and "TOO_MANY_RETAINED" in eleventh.reasons


def test_the_database_enforces_the_hourly_and_retained_limits_too(
    review: ReviewEnv, db: TestDb, sql: Sql
) -> None:
    enabled(review)
    for _ in range(3):
        ready(review)
    with role_conn(db, "td_app") as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="per hour"):
            conn.execute(
                "INSERT INTO review_packages (id, state, period_start, period_end, scope, "
                "retention_days, requested_by, requested_at) VALUES (%s, 'REQUESTED', %s, %s, "
                "ARRAY['backtests'], 14, %s, %s)",
                (uuid4(), START, END, review.admin.user.id, review.clock.now()),
            )


# ------------------------------------------------------------------ build
def test_a_built_package_is_stored_outside_the_web_root_under_a_generated_name(
    review: ReviewEnv, sql: Sql
) -> None:
    enabled(review)
    pid = ready(review)
    row = sql("SELECT * FROM review_packages WHERE id = %s", (pid,))[0]
    assert row["state"] == "READY" and NAME.match(row["storage_name"])
    assert row["storage_name"] != f"{pid}.zip"  # not derivable from the package id
    path = review.review_dir / row["storage_name"]
    assert path.is_file() and stat.S_IMODE(os.stat(path).st_mode) == 0o440
    assert "static" not in path.parts and "web" not in path.parts
    blob = path.read_bytes()
    assert row["package_sha256"] == pkg.sha256_hex(blob) and row["size_bytes"] == len(blob)
    assert row["expires_at"] is not None and row["file_count"] >= 8
    assert pkg.verify_zip(blob, max_bytes=20 * 1024 * 1024) == []
    assert {"review.requested", "review.generating", "review.ready"} <= set(codes(sql))
    ready_event = sql("SELECT * FROM audit_events WHERE event_code = 'review.ready'")[0]
    assert ready_event["actor_role"] == "HOST_CLI"


def test_building_twice_builds_nothing_new(review: ReviewEnv) -> None:
    enabled(review)
    ready(review)
    assert review.builder.build_pending() == []
    assert len(list(review.review_dir.glob("*.zip"))) == 1


def test_a_request_is_failed_if_the_feature_was_disabled_before_the_build(
    review: ReviewEnv, sql: Sql
) -> None:
    enabled(review)
    pid = requested(review)
    assert review.chain("disable").kind == "ok"
    (result,) = review.builder.build_pending()
    assert (result.state, result.code) == ("FAILED", "FEATURE_DISABLED")
    assert (
        sql("SELECT failure_code FROM review_packages WHERE id = %s", (pid,))[0]["failure_code"]
        == "FEATURE_DISABLED"
    )
    assert list(review.review_dir.glob("*")) == []


def test_a_failed_export_leaves_no_partial_output(
    review: ReviewEnv, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled(review)
    pid = requested(review)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise sz.Rejected("BAD_DECIMAL")

    monkeypatch.setattr("app.review.exporter.export", boom)
    (result,) = review.builder.build_pending()
    assert (result.state, result.code) == ("FAILED", "BAD_DECIMAL")
    assert list(review.review_dir.glob("*")) == []
    assert "review.failed" in codes(sql)
    assert sql("SELECT state FROM review_packages WHERE id = %s", (pid,))[0]["state"] == "FAILED"


def test_the_scanner_fails_a_package_and_purges_it(
    review: ReviewEnv, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled(review)
    requested(review)
    real = pkg.efficiency_summary

    def leaky(*a: Any, **k: Any) -> dict[str, Any]:
        return {**real(*a, **k), "note": "password=hunter2"}

    monkeypatch.setattr("app.review.package.efficiency_summary", leaky)
    (result,) = review.builder.build_pending()
    assert (result.state, result.code) == ("FAILED", "SCANNER_HIT")
    assert list(review.review_dir.glob("*")) == []


# ------------------------------------------------------------------ verify and download
def test_verify_and_download_serve_exactly_the_stored_bytes_and_are_audited(
    review: ReviewEnv, sql: Sql
) -> None:
    enabled(review)
    pid = ready(review)
    stored = sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    assert review.service.verify(review.actor, pid).outcome.kind == "ok"
    got = review.service.download(review.actor, pid)
    assert got.outcome.kind == "ok" and got.filename == f"review-package-{pid}.zip"
    assert got.data == (review.review_dir / stored).read_bytes()
    assert {"review.verified", "review.downloaded"} <= set(codes(sql))
    event = sql("SELECT * FROM audit_events WHERE event_code = 'review.downloaded'")[0]
    assert event["actor_role"] == "ADMIN" and str(pid) == event["target_id"]


def test_a_tampered_file_is_never_served_and_marks_the_package_corrupt(
    review: ReviewEnv, sql: Sql
) -> None:
    enabled(review)
    pid = ready(review)
    path = review.review_dir / sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    path.chmod(0o644)
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    got = review.service.download(review.actor, pid)
    assert got.outcome.kind == "corrupt" and got.data == b"" and got.problems
    assert sql("SELECT state FROM review_packages")[0]["state"] == "CORRUPT"
    assert "review.corrupt" in codes(sql)
    assert review.service.download(review.actor, pid).outcome.kind == "not_ready"


def test_a_missing_file_or_a_symlink_is_treated_as_corrupt(review: ReviewEnv, sql: Sql) -> None:
    enabled(review)
    pid = ready(review)
    path = review.review_dir / sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    target = review.review_dir / "elsewhere.bin"
    target.write_bytes(path.read_bytes())
    path.chmod(0o644)
    path.unlink()
    path.symlink_to(target)
    got = review.service.verify(review.actor, pid)
    assert got.outcome.kind == "corrupt"
    assert sql("SELECT state FROM review_packages")[0]["state"] == "CORRUPT"


def test_only_ready_packages_can_be_verified_or_downloaded(review: ReviewEnv) -> None:
    enabled(review)
    pid = requested(review)
    assert review.service.download(review.actor, pid).outcome.kind == "not_ready"
    assert review.service.download(review.actor, uuid4()).outcome.kind == "not_found"


def test_host_verify_marks_corruption_and_reports_problems(review: ReviewEnv, sql: Sql) -> None:
    enabled(review)
    pid = ready(review)
    assert review.builder.verify(pid) == []
    path = review.review_dir / sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    path.chmod(0o644)
    path.write_bytes(path.read_bytes()[:-30])
    assert review.builder.verify(pid)
    assert sql("SELECT state FROM review_packages")[0]["state"] == "CORRUPT"


# ------------------------------------------------------------------ retention
def test_expired_packages_lose_their_content_but_keep_a_record(review: ReviewEnv, sql: Sql) -> None:
    enabled(review, days=2)
    pid = ready(review)
    name = sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    assert review.builder.cleanup().expired == 0
    review.clock.advance(3 * 86400)
    result = review.builder.cleanup()
    assert (result.expired, result.removed) == (1, 1)
    row = sql("SELECT * FROM review_packages WHERE id = %s", (pid,))[0]
    assert row["state"] == "EXPIRED" and row["content_removed_at"] is not None
    assert not (review.review_dir / name).exists()
    assert {"review.expired", "review.cleanup"} <= set(codes(sql))
    assert review.builder.cleanup() == type(result)(0, 0, 0)  # idempotent
    assert review.service.download(review.actor, pid).outcome.kind == "not_ready"


def test_cleanup_removes_orphans_and_temp_files_but_never_a_referenced_package(
    review: ReviewEnv, sql: Sql
) -> None:
    enabled(review)
    ready(review)
    kept = sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    orphan = review.review_dir / f"{uuid4()}.zip"
    orphan.write_bytes(b"left over")
    tmp = review.review_dir / f".{'a' * 32}.tmp"
    tmp.write_bytes(b"partial")
    note = review.review_dir / "notes.txt"
    note.write_bytes(b"not ours")
    result = review.builder.cleanup()
    assert result.orphans == 2
    assert not orphan.exists() and not tmp.exists()
    assert (review.review_dir / kept).exists() and note.exists()
    event = sql(
        "SELECT * FROM audit_events WHERE event_code = 'review.cleanup' "
        "AND reason_code = 'ORPHANS_REMOVED'"
    )
    assert len(event) == 1


def test_corrupt_packages_expire_and_are_cleaned_too(review: ReviewEnv, sql: Sql) -> None:
    enabled(review, days=1)
    pid = ready(review)
    path = review.review_dir / sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    path.chmod(0o644)
    path.write_bytes(b"broken")
    assert review.builder.verify(pid)
    review.clock.advance(2 * 86400)
    review.builder.cleanup()
    row = sql("SELECT state, content_removed_at FROM review_packages")[0]
    assert row["state"] == "EXPIRED" and row["content_removed_at"] is not None
    assert not path.exists()


# ------------------------------------------------------------------ database roles and guards
def test_the_host_role_cannot_request_or_change_settings(review: ReviewEnv, db: TestDb) -> None:
    enabled(review)
    with role_conn(db, "td_ctl") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE review_settings SET enabled = false")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO review_packages (id, state, period_start, period_end, scope, "
                "retention_days, requested_by, requested_at) VALUES (gen_random_uuid(), "
                "'REQUESTED', %s, %s, ARRAY['backtests'], 14, %s, now())",
                (START, END, review.admin.user.id),
            )


def test_the_web_role_cannot_build_or_expire_or_delete(review: ReviewEnv, db: TestDb) -> None:
    enabled(review)
    pid = ready(review)
    with role_conn(db, "td_app") as conn:
        for statement in (
            "UPDATE review_packages SET state = 'EXPIRED'",
            "UPDATE review_packages SET storage_name = 'x'",
            "UPDATE review_packages SET package_sha256 = repeat('0', 64)",
            "DELETE FROM review_packages",
            "TRUNCATE review_packages",
        ):
            with pytest.raises(psycopg.Error):
                conn.execute(statement)
            conn.rollback()
        # the one thing the web tier may do to a READY package: mark it CORRUPT after verification
        conn.execute("UPDATE review_packages SET state = 'CORRUPT' WHERE id = %s", (pid,))
        conn.commit()


def test_illegal_transitions_and_request_edits_are_refused_by_the_database(
    review: ReviewEnv, db: TestDb
) -> None:
    enabled(review)
    pid = requested(review)
    with role_conn(db, "td_ctl") as conn:
        for statement in (
            "UPDATE review_packages SET state = 'READY'",  # skips GENERATING
            "UPDATE review_packages SET state = 'EXPIRED'",
        ):
            with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
                conn.execute(statement)
            conn.rollback()
        conn.execute("UPDATE review_packages SET state = 'GENERATING', started_at = now()")
        conn.commit()
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE review_packages SET scope = ARRAY['paper']")
        conn.rollback()
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE review_packages SET state = 'REQUESTED'")
        conn.rollback()
    assert pid


def test_the_web_role_cannot_change_settings_beyond_the_three_columns(
    review: ReviewEnv, db: TestDb
) -> None:
    with role_conn(db, "td_app") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE review_settings SET id = id")
        conn.rollback()
        with pytest.raises(psycopg.Error):
            conn.execute("DELETE FROM review_settings")


def test_scope_and_period_constraints_are_enforced_in_the_database(
    review: ReviewEnv, db: TestDb
) -> None:
    enabled(review)
    with role_conn(db, "td_app") as conn:
        for scope, start, end in (
            ("ARRAY['secrets']", START, END),
            ("ARRAY[]::text[]", START, END),
            ("ARRAY['backtests']", date(2026, 1, 1), date(2026, 9, 28)),
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO review_packages (id, state, period_start, period_end, scope, "  # noqa: S608
                    f"retention_days, requested_by, requested_at) VALUES (gen_random_uuid(), "
                    f"'REQUESTED', %s, %s, {scope}, 14, %s, now())",
                    (start, end, review.admin.user.id),
                )
            conn.rollback()


# ------------------------------------------------------------------ nothing else changes
def test_no_bot_pair_order_config_or_gate_state_changes_through_the_whole_lifecycle(
    review: ReviewEnv, mkt: Market, sql: Sql
) -> None:
    mkt.imported()
    before = fingerprint(sql)
    before_policy = review.settings.pair_policy.model_dump_json()
    enabled(review, days=1)
    pid = ready(review)
    assert review.service.verify(review.actor, pid).outcome.kind == "ok"
    assert review.service.download(review.actor, pid).outcome.kind == "ok"
    review.clock.advance(2 * 86400)
    review.builder.cleanup()
    assert review.chain("disable").kind == "ok"
    assert fingerprint(sql) == before
    assert review.settings.pair_policy.model_dump_json() == before_policy
