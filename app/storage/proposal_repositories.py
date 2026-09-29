"""SQL for imported proposals: settings, proposals, history, change requests, attestations.

Proposal text is untrusted. It is stored and returned as data (parameterised SQL only) and is never
interpreted here. Nothing in this module references a pair, order, ledger, strategy or config table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.storage.repositories import Conn


@dataclass(frozen=True)
class ProposalSettingsRow:
    import_enabled: bool
    updated_at: datetime


@dataclass(frozen=True)
class ProposalRow:
    id: UUID
    state: str
    storage_name: str
    size_bytes: int
    sha256: str
    declared_mime: str
    imported_by: UUID
    imported_at: datetime
    proposal_ref: str | None
    category: str | None
    linked_package_id: UUID | None
    linked_package_sha256: str | None
    parsed: dict[str, Any] | None
    findings: list[dict[str, Any]] | None
    risk_assessment: dict[str, Any] | None
    reject_rules: tuple[str, ...] | None
    validated_at: datetime | None
    review_notes: str | None
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    closed_reason: str | None
    closed_at: datetime | None
    content_removed_at: datetime | None


@dataclass(frozen=True)
class HistoryRow:
    state_before: str | None
    state_after: str
    actor_class: str
    reason_code: str | None
    occurred_at: datetime


@dataclass(frozen=True)
class ChangeRequestRow:
    id: UUID
    seq: int
    proposal_id: UUID
    change_type: str
    impact_assessment: str
    ceilings_unaffected: bool
    created_at: datetime


@dataclass(frozen=True)
class AttestationRow:
    kind: str
    reference: str | None
    report_ids: tuple[UUID, ...] | None
    attested_at: datetime


def _proposal(row: dict[str, Any]) -> ProposalRow:
    data = dict(row)
    if data["reject_rules"] is not None:
        data["reject_rules"] = tuple(data["reject_rules"])
    return ProposalRow(**data)


class ProposalRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ settings
    def settings(self, *, for_update: bool = False) -> ProposalSettingsRow:
        row = self._conn.execute(
            "SELECT import_enabled, updated_at FROM proposal_settings WHERE id"  # noqa: S608
            + (" FOR UPDATE" if for_update else "")
        ).fetchone()
        if row is None:  # pragma: no cover  (the migration inserts the single row)
            raise RuntimeError("proposal settings row is missing")
        return ProposalSettingsRow(**row)

    def set_import_enabled(self, enabled: bool, now: datetime) -> None:
        self._conn.execute(
            "UPDATE proposal_settings SET import_enabled = %s, updated_at = %s WHERE id",
            (enabled, now),
        )

    # ------------------------------------------------------------------ proposals
    def insert(
        self,
        *,
        proposal_id: UUID,
        storage_name: str,
        size_bytes: int,
        sha256: str,
        declared_mime: str,
        imported_by: UUID,
        now: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO proposals (id, state, storage_name, size_bytes, sha256, declared_mime, "
            "imported_by, imported_at) VALUES (%s, 'IMPORTED', %s, %s, %s, %s, %s, %s)",
            (proposal_id, storage_name, size_bytes, sha256, declared_mime, imported_by, now),
        )

    def get(self, proposal_id: UUID, *, for_update: bool = False) -> ProposalRow | None:
        row = self._conn.execute(
            "SELECT * FROM proposals WHERE id = %s"  # noqa: S608
            + (" FOR UPDATE" if for_update else ""),
            (proposal_id,),
        ).fetchone()
        return _proposal(row) if row else None

    def recent(self, limit: int = 50) -> list[ProposalRow]:
        rows = self._conn.execute(
            "SELECT * FROM proposals ORDER BY imported_at DESC, id LIMIT %s", (limit,)
        ).fetchall()
        return [_proposal(r) for r in rows]

    def in_state(self, *states: str) -> list[ProposalRow]:
        rows = self._conn.execute(
            "SELECT * FROM proposals WHERE state = ANY(%s) ORDER BY imported_at, id",
            (list(states),),
        ).fetchall()
        return [_proposal(r) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT state, count(*) AS n FROM proposals GROUP BY state"
        ).fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    def policy_rejections(self) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS n FROM proposals WHERE findings IS NOT NULL "
            "AND jsonb_array_length(findings) > 0"
        ).fetchone()
        return int(row["n"]) if row else 0

    def total(self) -> int:
        row = self._conn.execute("SELECT count(*) AS n FROM proposals").fetchone()
        return int(row["n"]) if row else 0

    def storage_names(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT storage_name FROM proposals WHERE content_removed_at IS NULL"
        ).fetchall()
        return {r["storage_name"] for r in rows}

    def purgeable(self, older_than: datetime) -> list[ProposalRow]:
        rows = self._conn.execute(
            "SELECT * FROM proposals WHERE content_removed_at IS NULL "
            "AND ((state = 'CLOSED' AND closed_at <= %s) "
            "  OR (state = 'REJECTED' AND validated_at <= %s)) ORDER BY imported_at, id",
            (older_than, older_than),
        ).fetchall()
        return [_proposal(r) for r in rows]

    def imports_since(self, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS n FROM proposals WHERE imported_at > %s", (since,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def stored_bytes(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(sum(size_bytes), 0) AS n FROM proposals "
            "WHERE content_removed_at IS NULL"
        ).fetchone()
        return int(row["n"]) if row else 0

    def sha_is_live(self, sha256: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 AS x FROM proposals WHERE sha256 = %s "
            "AND state NOT IN ('REJECTED', 'CLOSED')",
            (sha256,),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ transitions
    def history(self, proposal_id: UUID) -> list[HistoryRow]:
        rows = self._conn.execute(
            "SELECT state_before, state_after, actor_class, reason_code, occurred_at "
            "FROM proposal_state_history WHERE proposal_id = %s ORDER BY id",
            (proposal_id,),
        ).fetchall()
        return [HistoryRow(**r) for r in rows]

    def add_history(
        self,
        proposal_id: UUID,
        before: str | None,
        after: str,
        actor_class: str,
        user_id: UUID | None,
        reason: str | None,
        now: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO proposal_state_history (proposal_id, state_before, state_after, "
            "actor_class, actor_user_id, reason_code, occurred_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (proposal_id, before, after, actor_class, user_id, reason, now),
        )

    def mark_validating(self, proposal_id: UUID) -> None:
        self._conn.execute(
            "UPDATE proposals SET state = 'VALIDATING' WHERE id = %s", (proposal_id,)
        )

    def finish_validation(
        self,
        proposal_id: UUID,
        *,
        state: str,
        proposal_ref: str | None,
        category: str | None,
        linked_package_id: UUID | None,
        linked_package_sha256: str | None,
        parsed: dict[str, Any] | None,
        findings: list[dict[str, Any]],
        risk: dict[str, Any] | None,
        reject_rules: list[str],
        now: datetime,
    ) -> None:
        self._conn.execute(
            "UPDATE proposals SET state = %s, proposal_ref = %s, category = %s, "
            "linked_package_id = %s, linked_package_sha256 = %s, parsed = %s::jsonb, "
            "findings = %s::jsonb, risk_assessment = %s::jsonb, reject_rules = %s, "
            "validated_at = %s WHERE id = %s",
            (
                state,
                proposal_ref,
                category,
                linked_package_id,
                linked_package_sha256,
                json.dumps(parsed, sort_keys=True) if parsed is not None else None,
                json.dumps(findings, sort_keys=True),
                json.dumps(risk, sort_keys=True) if risk is not None else None,
                reject_rules,
                now,
                proposal_id,
            ),
        )

    def mark_reviewed(self, proposal_id: UUID, notes: str, user_id: UUID, now: datetime) -> None:
        self._conn.execute(
            "UPDATE proposals SET state = 'REVIEWED', review_notes = %s, reviewed_by = %s, "
            "reviewed_at = %s WHERE id = %s",
            (notes, user_id, now, proposal_id),
        )

    def set_state(self, proposal_id: UUID, state: str) -> None:
        self._conn.execute("UPDATE proposals SET state = %s WHERE id = %s", (state, proposal_id))

    def mark_closed(self, proposal_id: UUID, reason: str, now: datetime) -> None:
        self._conn.execute(
            "UPDATE proposals SET state = 'CLOSED', closed_reason = %s, closed_at = %s "
            "WHERE id = %s",
            (reason, now, proposal_id),
        )

    def mark_content_removed(self, proposal_id: UUID, now: datetime) -> None:
        self._conn.execute(
            "UPDATE proposals SET content_removed_at = %s WHERE id = %s", (now, proposal_id)
        )

    # ------------------------------------------------------------------ change requests
    def add_change_request(
        self,
        cr_id: UUID,
        proposal_id: UUID,
        change_type: str,
        impact: str,
        ceilings_unaffected: bool,
        user_id: UUID,
        now: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO change_requests (id, proposal_id, change_type, impact_assessment, "
            "ceilings_unaffected, created_by, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (cr_id, proposal_id, change_type, impact, ceilings_unaffected, user_id, now),
        )

    def change_request(self, proposal_id: UUID) -> ChangeRequestRow | None:
        row = self._conn.execute(
            "SELECT id, seq, proposal_id, change_type, impact_assessment, ceilings_unaffected, "
            "created_at FROM change_requests WHERE proposal_id = %s",
            (proposal_id,),
        ).fetchone()
        return ChangeRequestRow(**row) if row else None

    # ------------------------------------------------------------------ attestations
    def add_attestation(
        self,
        proposal_id: UUID,
        kind: str,
        reference: str | None,
        report_ids: list[UUID] | None,
        user_id: UUID,
        now: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO proposal_attestations (proposal_id, kind, reference, report_ids, "
            "attested_by, attested_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (proposal_id, kind, reference, report_ids, user_id, now),
        )

    def attestations(self, proposal_id: UUID) -> dict[str, AttestationRow]:
        rows = self._conn.execute(
            "SELECT kind, reference, report_ids, attested_at FROM proposal_attestations "
            "WHERE proposal_id = %s",
            (proposal_id,),
        ).fetchall()
        out: dict[str, AttestationRow] = {}
        for r in rows:
            ids = tuple(r["report_ids"]) if r["report_ids"] is not None else None
            out[r["kind"]] = AttestationRow(r["kind"], r["reference"], ids, r["attested_at"])
        return out

    # ------------------------------------------------------------------ evidence lookups
    def report_kinds(self, ids: list[UUID]) -> dict[UUID, tuple[str, datetime]]:
        rows = self._conn.execute(
            "SELECT id, kind, created_at FROM reports WHERE id = ANY(%s)", (ids,)
        ).fetchall()
        return {r["id"]: (r["kind"], r["created_at"]) for r in rows}

    _EXISTS = {
        "reports": "SELECT 1 AS x FROM reports WHERE id = %s",
        "dataset_snapshots": "SELECT 1 AS x FROM dataset_snapshots WHERE id = %s",
        "backtest_runs": "SELECT 1 AS x FROM backtest_runs WHERE id = %s",
    }

    def exists(self, table: str, row_id: UUID) -> bool:
        """Does a report, snapshot or backtest run with this id exist? (fixed queries only)"""
        query = self._EXISTS.get(table)
        if query is None:
            raise ValueError("table not allowed")
        return self._conn.execute(query, (row_id,)).fetchone() is not None
