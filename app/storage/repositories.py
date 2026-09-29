"""SQL repositories. Every statement is parameterised; every method works on one transaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import psycopg

from app.domain.enums import (
    ActorRole,
    AuditEventType,
    AuditResult,
    LoginScope,
    SessionEndReason,
)
from app.domain.models import AuditRecord, ChainStatus, SessionRecord, User
from app.storage.models import (
    SESSION_COLUMNS,
    USER_COLUMNS,
    audit_from_row,
    session_from_row,
    user_from_row,
)

if TYPE_CHECKING:
    from app.storage.market_repositories import MarketRepository, ResultRepository
    from app.storage.pair_repositories import PairRepository, ProductRepository
    from app.storage.paper_repositories import PaperRepository
    from app.storage.proposal_repositories import ProposalRepository
    from app.storage.review_repositories import ExportRepository, ReviewRepository
    from app.storage.safety_repositories import SafetyRepository

GENESIS_HASH = "0" * 64
MAX_DETAIL_BYTES = 4096

Conn = psycopg.Connection[dict[str, Any]]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def compute_event_hash(prev_hash: str, fields: dict[str, Any]) -> str:
    return hashlib.sha256(f"{prev_hash}|{canonical_json(fields)}".encode("ascii")).hexdigest()


def _hash_fields(row: dict[str, Any]) -> dict[str, Any]:
    """The exact set of columns covered by the chain (same for append and verify)."""
    return {
        "seq": row["seq"],
        "event_id": str(row["event_id"]),
        "occurred_at": iso_utc(row["occurred_at"]),
        "event_code": row["event_code"],
        "result": row["result"],
        "actor_user_id": str(row["actor_user_id"]) if row["actor_user_id"] else None,
        "actor_role": row["actor_role"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "reason_code": row["reason_code"],
        "client_tag": row["client_tag"],
        "request_id": row["request_id"],
        "detail": row["detail"],
    }


class UserRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def get_by_username(self, username: str) -> User | None:
        row = self._conn.execute(
            f"SELECT {USER_COLUMNS} FROM users WHERE username = %s", (username,)
        ).fetchone()
        return user_from_row(row) if row else None

    def get_by_id(self, user_id: UUID) -> User | None:
        row = self._conn.execute(
            f"SELECT {USER_COLUMNS} FROM users WHERE id = %s", (user_id,)
        ).fetchone()
        return user_from_row(row) if row else None

    def list_all(self) -> list[User]:
        rows = self._conn.execute(f"SELECT {USER_COLUMNS} FROM users ORDER BY username").fetchall()
        return [user_from_row(r) for r in rows]

    def any_admin(self) -> bool:
        return (
            self._conn.execute("SELECT 1 FROM users WHERE role = 'ADMIN' LIMIT 1").fetchone()
            is not None
        )

    def lock_bootstrap(self) -> None:
        """Serialise concurrent first-admin creation for the rest of the transaction."""
        self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (7_272_001,))

    def insert(self, user: User) -> None:
        self._conn.execute(
            f"INSERT INTO users ({USER_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                user.id,
                user.username,
                user.role.value,
                user.password_hash,
                user.password_changed_at,
                user.created_at,
                user.disabled_at,
            ),
        )

    def update_password(self, user_id: UUID, password_hash: str, now: datetime) -> None:
        self._conn.execute(
            "UPDATE users SET password_hash = %s, password_changed_at = %s WHERE id = %s",
            (password_hash, now, user_id),
        )


class SessionRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def create(self, record: SessionRecord) -> None:
        self._conn.execute(
            f"INSERT INTO sessions ({SESSION_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record.id,
                record.user_id,
                record.token_hash,
                record.created_at,
                record.last_seen_at,
                record.absolute_expires_at,
                record.revoked_at,
                record.revoked_reason,
                record.reauth_at,
            ),
        )

    def get_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        row = self._conn.execute(
            f"SELECT {SESSION_COLUMNS} FROM sessions WHERE token_hash = %s", (token_hash,)
        ).fetchone()
        return session_from_row(row) if row else None

    def touch(self, session_id: UUID, now: datetime) -> None:
        self._conn.execute("UPDATE sessions SET last_seen_at = %s WHERE id = %s", (now, session_id))

    def revoke(self, session_id: UUID, reason: SessionEndReason, now: datetime) -> bool:
        cur = self._conn.execute(
            "UPDATE sessions SET revoked_at = %s, revoked_reason = %s "
            "WHERE id = %s AND revoked_at IS NULL",
            (now, reason.value, session_id),
        )
        return cur.rowcount == 1

    def revoke_all_for_user(
        self,
        user_id: UUID,
        reason: SessionEndReason,
        now: datetime,
        *,
        keep: UUID | None = None,
    ) -> int:
        cur = self._conn.execute(
            "UPDATE sessions SET revoked_at = %s, revoked_reason = %s "
            "WHERE user_id = %s AND revoked_at IS NULL AND (%s::uuid IS NULL OR id <> %s::uuid)",
            (now, reason.value, user_id, keep, keep),
        )
        return cur.rowcount

    def list_active(
        self, user_id: UUID, now: datetime, idle_cutoff: datetime
    ) -> list[SessionRecord]:
        rows = self._conn.execute(
            f"SELECT {SESSION_COLUMNS} FROM sessions "
            "WHERE user_id = %s AND revoked_at IS NULL AND absolute_expires_at > %s "
            "AND last_seen_at > %s ORDER BY last_seen_at DESC, created_at DESC",
            (user_id, now, idle_cutoff),
        ).fetchall()
        return [session_from_row(r) for r in rows]

    def active_counts(self, now: datetime, idle_cutoff: datetime) -> dict[UUID, int]:
        rows = self._conn.execute(
            "SELECT user_id, count(*) AS n FROM sessions "
            "WHERE revoked_at IS NULL AND absolute_expires_at > %s AND last_seen_at > %s "
            "GROUP BY user_id",
            (now, idle_cutoff),
        ).fetchall()
        return {r["user_id"]: int(r["n"]) for r in rows}

    def set_reauth(self, session_id: UUID, now: datetime) -> None:
        self._conn.execute("UPDATE sessions SET reauth_at = %s WHERE id = %s", (now, session_id))

    def consume_reauth(self, session_id: UUID, earliest: datetime) -> bool:
        """Atomically clear a fresh reauthentication stamp. True only once per stamp."""
        cur = self._conn.execute(
            "UPDATE sessions SET reauth_at = NULL "
            "WHERE id = %s AND reauth_at IS NOT NULL AND reauth_at >= %s",
            (session_id, earliest),
        )
        return cur.rowcount == 1


class LoginAttemptRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def add(self, scope: LoginScope, key_hmac: str, success: bool, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO login_attempts (scope, key_hmac, success, attempted_at) "
            "VALUES (%s, %s, %s, %s)",
            (scope.value, key_hmac, success, now),
        )

    def failure_window(
        self,
        scope: LoginScope,
        key_hmac: str,
        since: datetime,
        *,
        reset_on_success: bool,
    ) -> tuple[int, datetime | None]:
        row = self._conn.execute(
            "SELECT count(*) AS n, min(attempted_at) AS oldest FROM login_attempts "
            "WHERE scope = %(scope)s AND key_hmac = %(key)s AND success = false "
            "AND attempted_at > %(since)s "
            "AND (NOT %(reset)s OR id > COALESCE("
            "  (SELECT max(id) FROM login_attempts "
            "   WHERE scope = %(scope)s AND key_hmac = %(key)s AND success), 0))",
            {"scope": scope.value, "key": key_hmac, "since": since, "reset": reset_on_success},
        ).fetchone()
        if row is None:
            raise RuntimeError("aggregate query returned no row")
        return int(row["n"]), row["oldest"]

    def purge_before(self, cutoff: datetime) -> int:
        return self._conn.execute(
            "DELETE FROM login_attempts WHERE attempted_at < %s", (cutoff,)
        ).rowcount


class AuditRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def append(
        self,
        *,
        occurred_at: datetime,
        event_code: AuditEventType,
        result: AuditResult,
        actor_user_id: UUID | None,
        actor_role: ActorRole | None,
        target_type: str | None,
        target_id: str | None,
        reason_code: str | None,
        client_tag: str | None,
        request_id: str | None,
        detail: dict[str, Any],
    ) -> int:
        detail_text = canonical_json(detail)
        if len(detail_text.encode("ascii")) > MAX_DETAIL_BYTES:
            raise ValueError("audit detail exceeds the size limit")
        head = self._conn.execute(
            "SELECT last_seq, last_hash FROM audit_head WHERE id FOR UPDATE"
        ).fetchone()
        if head is None:
            raise RuntimeError("audit_head row is missing")
        row: dict[str, Any] = {
            "seq": head["last_seq"] + 1,
            "event_id": uuid4(),
            "occurred_at": occurred_at,
            "event_code": event_code.value,
            "result": result.value,
            "actor_user_id": actor_user_id,
            "actor_role": actor_role.value if actor_role else None,
            "target_type": target_type,
            "target_id": target_id,
            "reason_code": reason_code,
            "client_tag": client_tag,
            "request_id": request_id,
            "detail": detail_text,
        }
        event_hash = compute_event_hash(head["last_hash"], _hash_fields(row))
        self._conn.execute(
            "INSERT INTO audit_events (seq, event_id, occurred_at, event_code, result, "
            "actor_user_id, actor_role, target_type, target_id, reason_code, client_tag, "
            "request_id, detail, prev_hash, event_hash) VALUES (%(seq)s, %(event_id)s, "
            "%(occurred_at)s, %(event_code)s, %(result)s, %(actor_user_id)s, %(actor_role)s, "
            "%(target_type)s, %(target_id)s, %(reason_code)s, %(client_tag)s, %(request_id)s, "
            "%(detail)s, %(prev_hash)s, %(event_hash)s)",
            {**row, "prev_hash": head["last_hash"], "event_hash": event_hash},
        )
        self._conn.execute(
            "UPDATE audit_head SET last_seq = %s, last_hash = %s WHERE id",
            (row["seq"], event_hash),
        )
        return int(row["seq"])

    def exists_recent(
        self,
        event_code: AuditEventType,
        client_tag: str,
        since: datetime,
        target_id: str | None = None,
    ) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM audit_events WHERE event_code = %s AND client_tag = %s "
                "AND occurred_at > %s AND target_id IS NOT DISTINCT FROM %s LIMIT 1",
                (event_code.value, client_tag, since, target_id),
            ).fetchone()
            is not None
        )

    def list(
        self, *, code: AuditEventType | None, before_seq: int | None, limit: int
    ) -> list[AuditRecord]:
        rows = self._conn.execute(
            "SELECT e.*, u.username AS actor_username FROM audit_events e "
            "LEFT JOIN users u ON u.id = e.actor_user_id "
            "WHERE (%(code)s::text IS NULL OR e.event_code = %(code)s::text) "
            "AND (%(before)s::bigint IS NULL OR e.seq < %(before)s::bigint) "
            "ORDER BY e.seq DESC LIMIT %(limit)s",
            {"code": code.value if code else None, "before": before_seq, "limit": limit},
        ).fetchall()
        return [audit_from_row(r) for r in rows]

    def verify_chain(self, *, max_events: int = 100_000) -> ChainStatus:
        head = self._conn.execute("SELECT last_seq, last_hash FROM audit_head WHERE id").fetchone()
        if head is None:
            raise RuntimeError("audit_head row is missing")
        rows = self._conn.execute(
            "SELECT * FROM audit_events ORDER BY seq LIMIT %s", (max_events,)
        ).fetchall()
        prev, expected_seq = GENESIS_HASH, 1
        for row in rows:
            if row["seq"] != expected_seq or row["prev_hash"] != prev:
                return ChainStatus(False, expected_seq - 1, True, broken_at=expected_seq)
            if compute_event_hash(prev, _hash_fields(row)) != row["event_hash"]:
                return ChainStatus(False, expected_seq - 1, True, broken_at=expected_seq)
            prev, expected_seq = row["event_hash"], expected_seq + 1
        checked = expected_seq - 1
        complete = checked == head["last_seq"] or len(rows) < max_events
        if complete and (checked != head["last_seq"] or prev != head["last_hash"]):
            return ChainStatus(False, checked, True, broken_at=checked + 1)
        return ChainStatus(True, checked, complete)


class MonitoringRepository:
    """Read-only aggregate queries for metrics and the monitoring summary."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def probe(self) -> None:
        self._conn.execute("SELECT 1").fetchone()

    def schema_version(self) -> int | None:
        row = self._conn.execute("SELECT version FROM schema_meta WHERE id").fetchone()
        return int(row["version"]) if row else None

    def audit_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT event_code, count(*) AS n FROM audit_events GROUP BY event_code"
        ).fetchall()
        return {r["event_code"]: int(r["n"]) for r in rows}

    def audit_last_seq(self) -> int:
        row = self._conn.execute("SELECT last_seq FROM audit_head WHERE id").fetchone()
        return int(row["last_seq"]) if row else 0

    def audit_last_event_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT occurred_at FROM audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row["occurred_at"] if row else None

    def active_sessions(self, now: datetime, idle_cutoff: datetime) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS n FROM sessions WHERE revoked_at IS NULL "
            "AND absolute_expires_at > %s AND last_seen_at > %s",
            (now, idle_cutoff),
        ).fetchone()
        return int(row["n"]) if row else 0

    def users_by_role(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT role, count(*) AS n FROM users WHERE disabled_at IS NULL GROUP BY role"
        ).fetchall()
        return {r["role"]: int(r["n"]) for r in rows}


@dataclass
class Repos:
    """All repositories bound to one connection/transaction."""

    users: UserRepository
    sessions: SessionRepository
    attempts: LoginAttemptRepository
    audit: AuditRepository
    monitoring: MonitoringRepository
    products: ProductRepository
    pairs: PairRepository
    market: MarketRepository
    results: ResultRepository
    paper: PaperRepository
    review: ReviewRepository
    export: ExportRepository
    proposals: ProposalRepository
    safety: SafetyRepository

    @classmethod
    def bind(cls, conn: Conn) -> Repos:
        from app.storage.market_repositories import MarketRepository, ResultRepository
        from app.storage.pair_repositories import PairRepository, ProductRepository
        from app.storage.paper_repositories import PaperRepository
        from app.storage.proposal_repositories import ProposalRepository
        from app.storage.review_repositories import ExportRepository, ReviewRepository
        from app.storage.safety_repositories import SafetyRepository

        return cls(
            UserRepository(conn),
            SessionRepository(conn),
            LoginAttemptRepository(conn),
            AuditRepository(conn),
            MonitoringRepository(conn),
            ProductRepository(conn),
            PairRepository(conn),
            MarketRepository(conn),
            ResultRepository(conn),
            PaperRepository(conn),
            ReviewRepository(conn),
            ExportRepository(conn),
            ProposalRepository(conn),
            SafetyRepository(conn),
        )
