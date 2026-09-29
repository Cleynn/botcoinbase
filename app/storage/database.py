"""Connections, transactions, schema guard and the SQL migration runner.

Runtime code connects as the least-privileged role (td_app, or td_ctl for the host CLI). Only the
migrate command uses the database owner, and it is never run by the web process.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app.config import ConfigError, DatabaseSettings, load_database_target, secret_problem
from app.storage.repositories import Conn, Repos

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_MIGRATION_LOCK = 7_272_002
_SESSION_OPTIONS = (
    "-c statement_timeout=10000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=15000"
)


class SchemaError(RuntimeError):
    """The database schema does not match what this code expects. Refuse to run."""


class StorageUnavailable(Exception):
    """The database could not be reached or timed out. Callers show a generic 503."""


def migration_files() -> dict[int, Path]:
    found: dict[int, Path] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_RE.match(path.name)
        if match:
            found[int(match.group(1))] = path
    expected = list(range(1, len(found) + 1))
    if list(found) != expected:
        raise SchemaError("migration files must be numbered contiguously from 0001")
    return found


def head_version() -> int:
    return max(migration_files())


class Storage:
    """Creates short-lived connections; one `tx()` block is one database transaction."""

    def __init__(self, settings: DatabaseSettings) -> None:
        password = settings.password.get_secret_value() if settings.password else None
        self._conninfo = make_conninfo(
            host=settings.host,
            port=settings.port,
            dbname=settings.name,
            user=settings.user,
            password=password,
            connect_timeout=5,
            application_name="tradingdots",
            options=_SESSION_OPTIONS,
        )

    def _connect(self) -> Conn:
        try:
            return psycopg.connect(self._conninfo, row_factory=dict_row, autocommit=False)
        except psycopg.OperationalError as exc:
            raise StorageUnavailable("database unavailable") from exc

    @contextmanager
    def tx(self) -> Iterator[Repos]:
        conn = self._connect()
        try:
            yield Repos.bind(conn)
            conn.commit()
        except psycopg.OperationalError as exc:
            conn.rollback()
            raise StorageUnavailable("database unavailable") from exc
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def check_schema(self) -> None:
        """Refuse to run against an older or newer schema than this code was written for."""
        conn = self._connect()
        try:
            try:
                row = conn.execute("SELECT version FROM schema_meta WHERE id").fetchone()
            except psycopg.errors.UndefinedTable as exc:
                raise SchemaError(
                    "database schema is not initialised; run the migrate command"
                ) from exc
        finally:
            conn.close()
        version = row["version"] if row else None
        expected = head_version()
        if version != expected:
            direction = "older" if (version or 0) < expected else "newer"
            raise SchemaError(
                f"database schema is {direction} than this code (expected {expected})"
            )


@dataclass(frozen=True)
class OwnerTarget:
    host: str
    port: int
    dbname: str
    user: str
    password: str | None = None

    def conninfo(self) -> str:
        return make_conninfo(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.user,
            password=self.password,
            connect_timeout=5,
            options="-c lock_timeout=10000",
        )


def _ensure_role(conn: psycopg.Connection[Any], role: str, password: str, limit: int) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    verb = "ALTER" if exists else "CREATE"
    conn.execute(
        sql.SQL(
            "{} ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            "NOINHERIT CONNECTION LIMIT {} PASSWORD {}"
        ).format(sql.SQL(verb), sql.Identifier(role), sql.Literal(limit), sql.Literal(password))
    )


def current_version(conn: psycopg.Connection[Any]) -> int | None:
    if conn.execute("SELECT to_regclass('public.schema_meta')").fetchone()[0] is None:  # type: ignore[index]
        return None
    row = conn.execute("SELECT version FROM schema_meta WHERE id").fetchone()
    return int(row[0]) if row else None


def migrate(
    target: OwnerTarget,
    *,
    app_password: str,
    ctl_password: str,
    app_role: str = "td_app",
    ctl_role: str = "td_ctl",
) -> int:
    """Create/refresh the runtime roles and apply pending migrations. Returns the schema version."""
    files = migration_files()
    with psycopg.connect(target.conninfo(), autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
        try:
            _ensure_role(conn, app_role, app_password, 20)
            _ensure_role(conn, ctl_role, ctl_password, 5)
            db = sql.Identifier(target.dbname)
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db))
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                    db, sql.Identifier(app_role), sql.Identifier(ctl_role)
                )
            )
            version = current_version(conn) or 0
            if version > max(files):
                raise SchemaError("database schema is newer than this code; refusing to migrate")
            for number, path in files.items():
                if number <= version:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute(
                        "INSERT INTO schema_meta (id, version) VALUES (true, %s) "
                        "ON CONFLICT (id) DO UPDATE "
                        "SET version = EXCLUDED.version, applied_at = now()",
                        (number,),
                    )
                version = number
            return version
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))


def rollback(target: OwnerTarget, *, to_version: int) -> int:
    """Development/test only: apply .down.sql files. Destroys data created by later migrations."""
    files = migration_files()
    with psycopg.connect(target.conninfo(), autocommit=True) as conn:
        version = current_version(conn) or 0
        while version > to_version:
            down = files[version].with_suffix("").with_suffix(".down.sql")
            with conn.transaction():
                conn.execute(down.read_text(encoding="utf-8"))
                if version - 1 >= 1:
                    conn.execute("UPDATE schema_meta SET version = %s WHERE id", (version - 1,))
            version -= 1
        return version


def _owner_target_from_env(env: Mapping[str, str]) -> tuple[OwnerTarget, str, str]:
    target = load_database_target(env)
    production = env.get("TD_ENVIRONMENT") == "production"
    owner_password = env.get("TD_DB_OWNER_PASSWORD")
    app_password = env.get("TD_DB_APP_PASSWORD", "")
    ctl_password = env.get("TD_DB_CTL_PASSWORD", "")
    if not owner_password or not app_password or not ctl_password:
        raise ConfigError(
            "TD_DB_OWNER_PASSWORD, TD_DB_APP_PASSWORD and TD_DB_CTL_PASSWORD are required"
        )
    if production:
        for name, value in (
            ("TD_DB_APP_PASSWORD", app_password),
            ("TD_DB_CTL_PASSWORD", ctl_password),
        ):
            problem = secret_problem(name, value)
            if problem:
                raise ConfigError(problem)
    owner = OwnerTarget(target.host, target.port, target.name, target.owner_user, owner_password)
    return owner, app_password, ctl_password


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.storage.database")
    parser.add_argument("command", choices=["migrate", "rollback"])
    parser.add_argument("--to", type=int, default=0, help="rollback target version")
    parser.add_argument("--i-understand-data-loss", action="store_true")
    args = parser.parse_args(argv)
    try:
        owner, app_password, ctl_password = _owner_target_from_env(os.environ)
        if args.command == "migrate":
            version = migrate(owner, app_password=app_password, ctl_password=ctl_password)
            print(f"schema at version {version}")
        else:
            if not args.i_understand_data_loss:
                print(
                    "refusing: rollback destroys data; pass --i-understand-data-loss",
                    file=sys.stderr,
                )
                return 2
            print(f"schema rolled back to version {rollback(owner, to_version=args.to)}")
    except (ConfigError, SchemaError) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)  # messages never contain secrets
        return 1
    except psycopg.Error as exc:
        print(f"migration failed: database error ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
