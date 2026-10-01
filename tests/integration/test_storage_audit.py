"""Migrations, schema guard, role privileges and the tamper-evident audit log (real PostgreSQL)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from app.auth.audit import AuditWriter, sanitise_detail
from app.domain.enums import AuditEventType, AuditResult
from app.storage import database
from app.storage.database import SchemaError, Storage, head_version, migrate, rollback
from tests.conftest import FakeClock, TestDb


def connect(db: TestDb, role: str) -> psycopg.Connection[Any]:
    settings = db.settings_for(role)
    return psycopg.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.name,
        user=role,
        password=settings.password.get_secret_value() if settings.password else None,
        autocommit=True,
    )


# ------------------------------------------------------------------ migrations and schema guard
def test_head_version_and_meta_agree(sql: Callable[..., Any]) -> None:
    assert sql("SELECT version FROM schema_meta")[0]["version"] == head_version() == 14


def test_migrate_is_idempotent(db: TestDb, role_passwords: tuple[str, str]) -> None:
    assert (
        migrate(db.owner_target(), app_password=role_passwords[0], ctl_password=role_passwords[1])
        == 14
    )


def test_rollback_and_remigrate_round_trip(
    db: TestDb, role_passwords: tuple[str, str], sql: Callable[..., Any]
) -> None:
    assert rollback(db.owner_target(), to_version=0) == 0
    assert sql("SELECT to_regclass('public.users') AS t")[0]["t"] is None
    assert sql("SELECT to_regclass('public.pairs') AS t")[0]["t"] is None
    assert (
        migrate(db.owner_target(), app_password=role_passwords[0], ctl_password=role_passwords[1])
        == 14
    )
    assert sql("SELECT count(*) AS n FROM allowed_transitions")[0]["n"] > 0
    assert sql("SELECT count(*) AS n FROM audit_head")[0]["n"] == 1


def test_schema_guard_refuses_an_older_schema(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.check_schema()
    monkeypatch.setattr(database, "head_version", lambda: 15)
    with pytest.raises(SchemaError, match="older"):
        storage.check_schema()


def test_schema_guard_refuses_a_newer_schema(storage: Storage, sql: Callable[..., Any]) -> None:
    sql("UPDATE schema_meta SET version = 15")
    with pytest.raises(SchemaError, match="newer"):
        storage.check_schema()


def test_migrate_refuses_to_run_against_a_newer_schema(
    db: TestDb, role_passwords: tuple[str, str], sql: Callable[..., Any]
) -> None:
    sql("UPDATE schema_meta SET version = 16")
    with pytest.raises(SchemaError):
        migrate(db.owner_target(), app_password=role_passwords[0], ctl_password=role_passwords[1])


def test_migration_files_are_contiguous() -> None:
    assert list(database.migration_files()) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]


def test_cli_rollback_requires_explicit_acknowledgement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("TD_DB_OWNER_PASSWORD", "TD_DB_APP_PASSWORD", "TD_DB_CTL_PASSWORD"):
        monkeypatch.setenv(name, "x" * 40)
    assert database.main(["rollback"]) == 2
    assert "--i-understand-data-loss" in capsys.readouterr().err


# ------------------------------------------------------------------ least-privilege roles
def test_the_web_role_cannot_create_users_or_change_roles(db: TestDb, admin: Any) -> None:
    with connect(db, "td_app") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO users (id, username, role, password_hash, "
                "password_changed_at, created_at) "
                "VALUES (gen_random_uuid(), 'mallory', 'ADMIN', '$argon2id$x', now(), now())"
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE users SET role = 'ADMIN'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE users SET disabled_at = NULL")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM users")
        conn.execute(
            "UPDATE users SET password_changed_at = now()"
        )  # allowed: password change flow


def test_the_web_role_cannot_modify_or_delete_audit_rows(db: TestDb, admin: Any) -> None:
    with connect(db, "td_app") as conn:
        for statement in (
            "UPDATE audit_events SET result = 'SUCCESS'",
            "DELETE FROM audit_events",
            "TRUNCATE audit_events",
            "DELETE FROM audit_head",
            "TRUNCATE audit_head",
            "DROP TABLE audit_events",
            "CREATE TABLE evil (x int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
        # UPDATE on audit_head is granted (the chain head advances), but only by exactly one.
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute("UPDATE audit_head SET last_hash = repeat('a', 64)")
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute("UPDATE audit_head SET last_seq = last_seq + 5")


def test_the_owner_is_also_blocked_by_the_append_only_triggers(
    services: Any, sql: Callable[..., Any]
) -> None:
    _write(services)
    for statement in (
        "UPDATE audit_events SET result = 'SUCCESS'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
        "UPDATE audit_head SET last_seq = 7",
        "TRUNCATE audit_head",
    ):
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            sql(statement)


def test_the_host_cli_role_can_create_users_but_not_touch_audit_history(db: TestDb) -> None:
    with connect(db, "td_ctl") as conn:
        conn.execute(
            "INSERT INTO users (id, username, role, password_hash, "
            "password_changed_at, created_at) "
            "VALUES (gen_random_uuid(), 'root-admin', 'ADMIN', '$argon2id$x', now(), now())"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE users SET role = 'VIEWER'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM audit_events")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM login_attempts")


def test_runtime_roles_have_no_superuser_ddl_or_replication_rights(sql: Callable[..., Any]) -> None:
    rows = sql(
        "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolinherit "
        "FROM pg_roles WHERE rolname IN ('td_app', 'td_ctl')"
    )
    assert {r["rolname"] for r in rows} == {"td_app", "td_ctl"}
    assert not any(
        r["rolsuper"]
        or r["rolcreatedb"]
        or r["rolcreaterole"]
        or r["rolreplication"]
        or r["rolinherit"]
        for r in rows
    )


def test_public_has_no_access_to_the_schema_or_database(
    db: TestDb, sql: Callable[..., Any]
) -> None:
    sql("CREATE ROLE td_probe LOGIN PASSWORD 'probe' NOSUPERUSER")
    try:
        with pytest.raises(psycopg.Error):
            with psycopg.connect(
                host=db.target.host,
                port=db.target.port,
                dbname=db.name,
                user="td_probe",
                password="probe",
                autocommit=True,
            ) as conn:
                conn.execute("SELECT * FROM users")
    finally:
        sql("DROP ROLE td_probe")


def test_table_constraints_reject_bad_rows(sql: Callable[..., Any]) -> None:
    bad_rows = [
        ("role", "'SUPERADMIN'", "'$argon2id$x'"),
        ("role", "'ADMIN'", "'plaintext-password'"),
    ]
    for _, role, password_hash in bad_rows:
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(
                f"INSERT INTO users (id, username, role, password_hash, "
                "password_changed_at, created_at) "
                f"VALUES (gen_random_uuid(), 'someone', {role}, {password_hash}, now(), now())"
            )


# ------------------------------------------------------------------ audit chain
def _write(services: Any, event: AuditEventType = AuditEventType.LOGIN_FAILURE, **kw: Any) -> None:
    with services.storage.tx() as repos:
        services.audit.record(repos, event, AuditResult.FAILURE, **kw)


def test_chain_verifies_after_normal_use(services: Any, admin: Any) -> None:
    for _ in range(5):
        _write(services)
    with services.storage.tx() as repos:
        status = repos.audit.verify_chain()
    assert status.ok and status.complete and status.events_checked == 5 and status.broken_at is None


def test_empty_chain_is_valid(services: Any) -> None:
    with services.storage.tx() as repos:
        assert repos.audit.verify_chain().ok


def _tamper(sql: Callable[..., Any], statement: str, params: Any = None) -> None:
    sql("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete")
    try:
        sql(statement, params)
    finally:
        sql("ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update_delete")


def test_editing_a_row_breaks_the_chain_at_that_row(services: Any, sql: Callable[..., Any]) -> None:
    for _ in range(4):
        _write(services)
    _tamper(sql, "UPDATE audit_events SET result = 'SUCCESS' WHERE seq = 3")
    with services.storage.tx() as repos:
        status = repos.audit.verify_chain()
    assert not status.ok and status.broken_at == 3


def test_deleting_a_row_is_detected(services: Any, sql: Callable[..., Any]) -> None:
    for _ in range(4):
        _write(services)
    _tamper(sql, "DELETE FROM audit_events WHERE seq = 2")
    with services.storage.tx() as repos:
        status = repos.audit.verify_chain()
    assert not status.ok and status.broken_at == 2


def test_deleting_the_newest_row_is_detected_via_the_head(
    services: Any, sql: Callable[..., Any]
) -> None:
    for _ in range(3):
        _write(services)
    _tamper(sql, "DELETE FROM audit_events WHERE seq = 3")
    with services.storage.tx() as repos:
        assert not repos.audit.verify_chain().ok


def test_rewriting_the_detail_text_is_detected(services: Any, sql: Callable[..., Any]) -> None:
    _write(services, detail={"note": "original"})
    _tamper(sql, 'UPDATE audit_events SET detail = \'{"note":"forged"}\' WHERE seq = 1')
    with services.storage.tx() as repos:
        assert repos.audit.verify_chain().broken_at == 1


def test_concurrent_writers_produce_a_gapless_verified_chain(services: Any) -> None:
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(5):
                _write(services)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    with services.storage.tx() as repos:
        status = repos.audit.verify_chain()
        seqs = [r.seq for r in repos.audit.list(code=None, before_seq=None, limit=100)]
    assert status.ok and status.events_checked == 30
    assert sorted(seqs) == list(range(1, 31))


def test_state_change_and_audit_event_commit_or_roll_back_together(
    services: Any, admin: Any, sql: Callable[..., Any]
) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom), services.storage.tx() as repos:
        repos.users.update_password(admin.user.id, "$argon2id$replaced", services.clock.now())
        services.audit.record(repos, AuditEventType.PASSWORD_CHANGED, AuditResult.SUCCESS)
        raise Boom
    assert sql("SELECT password_hash FROM users")[0]["password_hash"] == admin.user.password_hash
    assert sql("SELECT count(*) AS n FROM audit_events")[0]["n"] == 0


def test_a_failed_audit_write_aborts_the_login(
    services: Any, admin: Any, monkeypatch: pytest.MonkeyPatch, sql: Callable[..., Any]
) -> None:
    from app.domain.models import ClientIdentity

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit store down")

    monkeypatch.setattr(services.audit, "record", broken)
    with pytest.raises(RuntimeError):
        services.auth.login(
            admin.username, admin.password, ClientIdentity.from_key("e" * 64), "rid"
        )
    assert (
        sql("SELECT count(*) AS n FROM sessions")[0]["n"] == 0
    )  # no session without its audit record


# ------------------------------------------------------------------ writer rules
def test_the_writer_rejects_secret_looking_detail_keys() -> None:
    for key in (
        "password",
        "new_password",
        "token",
        "session_token",
        "cookie",
        "password_hash",
        "api_key",
        "authorization",
    ):
        with pytest.raises(ValueError):
            sanitise_detail({key: "x"})
    assert sanitise_detail({"count": 3, "reason": "ok", "flag": True, "none": None}) == {
        "count": 3,
        "reason": "ok",
        "flag": True,
        "none": None,
    }


def test_the_writer_rejects_non_scalar_values_and_bounds_strings() -> None:
    with pytest.raises(ValueError):
        sanitise_detail({"nested": {"a": 1}})
    assert len(sanitise_detail({"note": "x" * 5000})["note"]) == 200
    assert sanitise_detail({"note": "a\nb\x00c"})["note"] == "a b c"


def test_oversized_detail_is_refused_by_the_repository(services: Any) -> None:
    with pytest.raises(ValueError), services.storage.tx() as repos:
        repos.audit.append(
            occurred_at=services.clock.now(),
            event_code=AuditEventType.LOGIN_FAILURE,
            result=AuditResult.FAILURE,
            actor_user_id=None,
            actor_role=None,
            target_type=None,
            target_id=None,
            reason_code=None,
            client_tag=None,
            request_id=None,
            detail={f"k{i}": "v" * 100 for i in range(60)},
        )


def test_every_catalogue_code_is_storable_and_unique(services: Any) -> None:
    codes = [e.value for e in AuditEventType]
    assert len(codes) == len(set(codes))
    for event in AuditEventType:
        _write(services, event)
    with services.storage.tx() as repos:
        stored = {r.event_code for r in repos.audit.list(code=None, before_seq=None, limit=100)}
        assert stored == set(AuditEventType) and repos.audit.verify_chain().ok


def test_throttle_is_per_event_client_and_target(services: Any, clock: FakeClock) -> None:
    writer = AuditWriter(clock)
    with services.storage.tx() as repos:
        first = writer.record(
            repos,
            AuditEventType.AUTHZ_DENIED,
            AuditResult.DENIED,
            client_tag="c1",
            target_id="/a",
            throttle_seconds=60,
        )
        dup = writer.record(
            repos,
            AuditEventType.AUTHZ_DENIED,
            AuditResult.DENIED,
            client_tag="c1",
            target_id="/a",
            throttle_seconds=60,
        )
        other_target = writer.record(
            repos,
            AuditEventType.AUTHZ_DENIED,
            AuditResult.DENIED,
            client_tag="c1",
            target_id="/b",
            throttle_seconds=60,
        )
        other_client = writer.record(
            repos,
            AuditEventType.AUTHZ_DENIED,
            AuditResult.DENIED,
            client_tag="c2",
            target_id="/a",
            throttle_seconds=60,
        )
    assert (first, dup, other_target, other_client) == (True, False, True, True)
    clock.advance(61)
    with services.storage.tx() as repos:
        assert writer.record(
            repos,
            AuditEventType.AUTHZ_DENIED,
            AuditResult.DENIED,
            client_tag="c1",
            target_id="/a",
            throttle_seconds=60,
        )


def test_hostile_strings_in_every_text_column_are_sanitised(services: Any) -> None:
    evil = "x\x00\n\x1b" + "y" * 500
    with services.storage.tx() as repos:
        services.audit.record(
            repos,
            AuditEventType.AUTHZ_DENIED,
            AuditResult.DENIED,
            reason=evil,
            target_type=evil,
            target_id=evil,
            client_tag=evil,
            request_id=evil,
            detail={"a": evil},
        )
        record = repos.audit.list(code=None, before_seq=None, limit=1)[0]
        assert (
            "\x00" not in repr(record)
            and len(record.reason_code or "") <= 64
            and len(record.target_id or "") <= 128
        )
        assert repos.audit.verify_chain().ok


def test_unknown_user_ids_cannot_be_recorded_as_actors(services: Any) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation), services.storage.tx() as repos:
        repos.audit.append(
            occurred_at=services.clock.now(),
            event_code=AuditEventType.LOGIN_SUCCESS,
            result=AuditResult.SUCCESS,
            actor_user_id=uuid4(),
            actor_role=None,
            target_type=None,
            target_id=None,
            reason_code=None,
            client_tag=None,
            request_id=None,
            detail={},
        )


def test_an_applied_migration_that_was_edited_is_refused(
    db: TestDb,
    role_passwords: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    copy = tmp_path / "migrations"
    shutil.copytree(database.MIGRATIONS_DIR, copy)
    monkeypatch.setattr(database, "MIGRATIONS_DIR", copy)
    args = {"app_password": role_passwords[0], "ctl_password": role_passwords[1]}
    assert migrate(db.owner_target(), **args) == 14  # records (or trusts once) every checksum
    assert migrate(db.owner_target(), **args) == 14  # unchanged files: fine
    with (copy / "0006_safety.sql").open("a", encoding="utf-8") as handle:
        handle.write("\n-- an edit after the migration was applied\n")
    with pytest.raises(SchemaError, match="0006 was modified after it was applied"):
        migrate(db.owner_target(), **args)


def test_the_checksums_are_owner_only_and_follow_a_rollback(
    db: TestDb, role_passwords: tuple[str, str], sql: Callable[..., Any]
) -> None:
    assert sql("SELECT count(*) AS n FROM schema_migrations")[0]["n"] == 14
    assert rollback(db.owner_target(), to_version=6) == 6
    assert sql("SELECT max(version) AS v FROM schema_migrations")[0]["v"] == 6
    migrate(db.owner_target(), app_password=role_passwords[0], ctl_password=role_passwords[1])
    assert sql("SELECT count(*) AS n FROM schema_migrations")[0]["n"] == 14
    for role in ("td_app", "td_ctl"):
        with connect(db, role) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM schema_migrations")
