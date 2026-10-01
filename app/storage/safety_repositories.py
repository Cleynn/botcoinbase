"""SQL for the safety machinery: bot control, intents, decisions, attempts, reconciliation, hints,
API events and control commands. Values are always bound parameters; only fixed column names from
allowlists are ever interpolated. The database (guards in migration 0006) is the authority on who
may write what; this module never decides that."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.capital.trading import TradingConfig
from app.storage.repositories import Conn

_CONTROL_COLUMNS = frozenset(
    {
        "bot_state",
        "kill_switch",
        "kill_reason",
        "kill_activated_at",
        "paper_profile",
        "live_profile",
        "breaker_state",
        "breaker_reason",
        "breaker_opened_at",
        "breaker_cooldown_until",
        "recovery_state",
        "recovery_completed_at",
        "boot_id",
        "last_change_reason",
    }
)
LIVE_ATTEMPT_STATES = ("AUTHORIZED", "SUBMITTING", "WORKING", "CANCEL_REQUESTED", "UNKNOWN")
TERMINAL_ATTEMPT_STATES = ("FILLED", "CANCELLED", "EXPIRED", "REJECTED", "ABSENT")


@dataclass(frozen=True)
class ControlRow:
    bot_state: str
    kill_switch: str
    kill_reason: str | None
    kill_activated_at: datetime | None
    breaker_state: str
    breaker_reason: str | None
    breaker_opened_at: datetime | None
    breaker_cooldown_until: datetime | None
    recovery_state: str
    recovery_completed_at: datetime | None
    boot_id: str | None
    last_change_reason: str
    version: int
    updated_at: datetime
    paper_profile: str = "pilot"
    live_profile: str = "pilot"


@dataclass(frozen=True)
class RunRow:
    id: UUID
    venue: str
    trigger: str
    started_at: datetime
    finished_at: datetime
    outcome: str
    orders_seen: int
    fills_seen: int
    balances_seen: int
    findings_count: int
    failure_code: str | None


@dataclass(frozen=True)
class FindingRow:
    code: str
    subject: str | None
    detail: str | None


@dataclass(frozen=True)
class IntentRow:
    id: UUID
    intent_key: str
    venue: str
    pair_id: UUID
    product_id: str
    side: str
    order_type: str
    post_only: bool
    price: Decimal
    base_qty: Decimal
    expected_cycle_return: Decimal | None
    source: str
    created_at: datetime


@dataclass(frozen=True)
class DecisionRow:
    id: UUID
    intent_id: UUID
    decision: str
    reasons: tuple[str, ...]
    inputs_hash: str
    decided_at: datetime
    expires_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True)
class AttemptRow:
    id: UUID
    intent_id: UUID
    attempt_no: int
    client_order_id: UUID
    decision_id: UUID
    boot_id: str
    state: str
    exchange_order_id: str | None
    filled_qty: Decimal
    failure_code: str | None
    created_at: datetime
    submitting_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True)
class FillRow:
    attempt_id: UUID
    venue: str
    exchange_fill_id: str
    side: str
    price: Decimal
    size: Decimal
    fee: Decimal
    liquidity: str
    occurred_at: datetime


@dataclass(frozen=True)
class HintRow:
    venue: str
    client_order_id: str
    order_id: str
    status: str
    filled_qty: Decimal
    sequence: int
    received_at: datetime


@dataclass(frozen=True)
class CommandRow:
    id: UUID
    kind: str
    origin: str
    state: str
    requested_by: UUID
    requested_at: datetime
    finished_at: datetime | None
    result: dict[str, Any] | None
    failure_code: str | None


def _attempt(row: dict[str, Any]) -> AttemptRow:
    return AttemptRow(**row)


@dataclass(frozen=True)
class SafetyStats:
    """Aggregates for metrics: counts only, never ids, amounts or free text."""

    reasons: dict[str, int]
    decisions: dict[str, int]
    attempts: dict[str, int]
    api: dict[tuple[str, bool, str], int]  # (operation, ok, code) -> count
    findings: int
    fill_anomalies: int
    events: dict[str, int]


class SafetyRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def stats(self) -> SafetyStats:
        reasons = {
            r["reason"]: int(r["n"])
            for r in self._conn.execute(
                "SELECT r AS reason, count(*) AS n FROM risk_decisions, unnest(reasons) AS r "
                "GROUP BY r"
            ).fetchall()
        }
        decisions = {
            r["decision"]: int(r["n"])
            for r in self._conn.execute(
                "SELECT decision, count(*) AS n FROM risk_decisions GROUP BY decision"
            ).fetchall()
        }
        api = {
            (r["operation"], r["ok"], r["code"] or ""): int(r["n"])
            for r in self._conn.execute(
                "SELECT operation, ok, code, count(*) AS n FROM api_events GROUP BY operation, ok, code"
            ).fetchall()
        }
        findings = self._conn.execute(
            "SELECT count(*) AS n FROM reconciliation_findings"
        ).fetchone()
        anomalies = self._conn.execute(
            "SELECT count(*) AS n FROM reconciliation_findings WHERE code = 'FILL_ANOMALY'"
        ).fetchone()
        events = {
            r["event_code"]: int(r["n"])
            for r in self._conn.execute(
                "SELECT event_code, count(*) AS n FROM audit_events WHERE event_code LIKE 'order.%%' "
                "GROUP BY event_code"
            ).fetchall()
        }
        return SafetyStats(
            reasons,
            decisions,
            self.counts_by_state(),
            api,
            int(findings["n"]) if findings else 0,
            int(anomalies["n"]) if anomalies else 0,
            events,
        )

    # ------------------------------------------------------------------ control
    def control(self, *, for_update: bool = False) -> ControlRow:
        row = self._conn.execute(
            "SELECT bot_state, kill_switch, kill_reason, kill_activated_at, breaker_state, "
            "breaker_reason, breaker_opened_at, breaker_cooldown_until, recovery_state, "
            "recovery_completed_at, boot_id, last_change_reason, version, updated_at, "
            "paper_profile, live_profile "
            "FROM bot_control WHERE id" + (" FOR UPDATE" if for_update else "")
        ).fetchone()
        if row is None:  # pragma: no cover  (the migration inserts the single row)
            raise RuntimeError("bot control row is missing")
        return ControlRow(**row)

    def update_control(self, expected_version: int, now: datetime, **changes: Any) -> bool:
        """Compare-and-set on the version. False means someone else changed it first."""
        if not changes or set(changes) - _CONTROL_COLUMNS:
            raise ValueError("unknown bot control column")
        columns = sorted(changes)
        assignments = ", ".join(f"{c} = %s" for c in columns)
        cur = self._conn.execute(
            f"UPDATE bot_control SET {assignments}, version = version + 1, updated_at = %s "  # noqa: S608
            "WHERE id AND version = %s",
            (*[changes[c] for c in columns], now, expected_version),
        )
        return cur.rowcount == 1

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(
            self._conn.execute(
                "SELECT occurred_at, actor_class, event, bot_state, kill_switch, breaker_state, "
                "recovery_state, reason, version FROM bot_control_history ORDER BY id DESC LIMIT %s",
                (limit,),
            ).fetchall()
        )

    # ------------------------------------------------------------------ reconciliation
    def add_run(
        self,
        *,
        run_id: UUID,
        venue: str,
        trigger: str,
        started_at: datetime,
        finished_at: datetime,
        outcome: str,
        orders_seen: int,
        fills_seen: int,
        balances_seen: int,
        findings: list[FindingRow],
        failure_code: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO reconciliation_runs (id, venue, trigger, started_at, finished_at, outcome, "
            "orders_seen, fills_seen, balances_seen, findings_count, failure_code) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run_id, venue, trigger, started_at, finished_at, outcome, orders_seen,
                fills_seen, balances_seen, len(findings), failure_code,
            ),
        )  # fmt: skip
        for f in findings:
            self._conn.execute(
                "INSERT INTO reconciliation_findings (run_id, code, subject, detail) "
                "VALUES (%s, %s, %s, %s)",
                (run_id, f.code, f.subject, f.detail),
            )

    def latest_run(self, venue: str | None = None) -> RunRow | None:
        row = self._conn.execute(
            "SELECT id, venue, trigger, started_at, finished_at, outcome, orders_seen, fills_seen, "
            "balances_seen, findings_count, failure_code FROM reconciliation_runs "
            "WHERE (%s::text IS NULL OR venue = %s) ORDER BY seq DESC LIMIT 1",
            (venue, venue),
        ).fetchone()
        return RunRow(**row) if row else None

    def recent_runs(self, limit: int = 10) -> list[RunRow]:
        rows = self._conn.execute(
            "SELECT id, venue, trigger, started_at, finished_at, outcome, orders_seen, fills_seen, "
            "balances_seen, findings_count, failure_code FROM reconciliation_runs "
            "ORDER BY seq DESC LIMIT %s",
            (limit,),
        ).fetchall()
        return [RunRow(**r) for r in rows]

    def findings(self, run_id: UUID) -> list[FindingRow]:
        rows = self._conn.execute(
            "SELECT code, subject, detail FROM reconciliation_findings WHERE run_id = %s ORDER BY id",
            (run_id,),
        ).fetchall()
        return [FindingRow(**r) for r in rows]

    def absence_proof(self, moment: datetime, venue: str) -> tuple[int, timedelta]:
        """(OK runs of this venue started at or after `moment`, time between the first and last)."""
        row = self._conn.execute(
            "SELECT count(*) AS n, max(started_at) - min(started_at) AS spread "
            "FROM reconciliation_runs WHERE venue = %s AND outcome = 'OK' AND started_at >= %s",
            (venue, moment),
        ).fetchone()
        if row is None or not row["n"]:
            return 0, timedelta(0)
        return int(row["n"]), row["spread"] or timedelta(0)

    def client_id_ever_named(self, client_order_id: str | UUID) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM reconciliation_findings WHERE subject = %s LIMIT 1",
            (str(client_order_id),),
        ).fetchone()
        return row is not None

    def consecutive_bad_runs(self, venue: str, limit: int = 10) -> int:
        n = 0
        for row in self._conn.execute(
            "SELECT outcome FROM reconciliation_runs WHERE venue = %s ORDER BY seq DESC LIMIT %s",
            (venue, limit),
        ).fetchall():
            if row["outcome"] == "OK":
                break
            n += 1
        return n

    # ------------------------------------------------------------------ baselines
    def add_baseline(self, venue: str, currency: str, amount: Decimal, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO venue_baselines (venue, currency, amount, recorded_at) VALUES (%s, %s, %s, %s)",
            (venue, currency, amount, now),
        )

    # ------------------------------------------------------------------ trading configuration
    def trading_config(self, mode: str, *, for_update: bool = False) -> TradingConfig:
        row = self._conn.execute(
            "SELECT mode, max_pairs, levels_per_grid, quote_per_grid, invested_cap, reserve, "
            "per_order_cap, version FROM trading_config WHERE mode = %s"
            + (" FOR UPDATE" if for_update else ""),
            (mode,),
        ).fetchone()
        if row is None:
            raise RuntimeError("trading configuration row is missing")
        return TradingConfig(**row)

    def trading_state(self, *, for_update: bool = False) -> tuple[str, int]:
        """(active mode, version)."""
        row = self._conn.execute(
            "SELECT active_mode, version FROM trading_state WHERE id"
            + (" FOR UPDATE" if for_update else "")
        ).fetchone()
        if row is None:
            raise RuntimeError("trading state row is missing")
        return str(row["active_mode"]), int(row["version"])

    def active_trading_config(self) -> TradingConfig:
        """The configuration of the active mode (BACKTEST and PAPER share the PAPER row)."""
        return self.trading_config("LIVE" if self.trading_state()[0] == "LIVE" else "PAPER")

    def update_trading_config(self, cfg: TradingConfig, now: datetime) -> bool:
        """Compare-and-set on the version; False means someone else changed it first."""
        cur = self._conn.execute(
            "UPDATE trading_config SET max_pairs = %s, levels_per_grid = %s, quote_per_grid = %s, "
            "invested_cap = %s, reserve = %s, per_order_cap = %s, version = version + 1, "
            "updated_at = %s WHERE mode = %s AND version = %s",
            (
                cfg.max_pairs,
                cfg.levels_per_grid,
                cfg.quote_per_grid,
                cfg.invested_cap,
                cfg.reserve,
                cfg.per_order_cap,
                now,
                cfg.mode,
                cfg.version,
            ),
        )
        return cur.rowcount == 1

    def set_active_mode(self, mode: str, expected_version: int, now: datetime) -> bool:
        cur = self._conn.execute(
            "UPDATE trading_state SET active_mode = %s, version = version + 1, updated_at = %s "
            "WHERE id AND version = %s",
            (mode, now, expected_version),
        )
        return cur.rowcount == 1

    # ------------------------------------------------------------------ live arming (host writes)
    def live_armed(self, now: datetime) -> bool:
        """The database's own answer: an unrevoked, unexpired arming not undone by a later kill
        switch, breaker trip, recovery reset or LIVE profile change."""
        row = self._conn.execute("SELECT td_live_armed(%s) AS armed", (now,)).fetchone()
        return bool(row and row["armed"])

    def active_arming(self, now: datetime) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id, armed_at, expires_at, armed_by, key_hint FROM live_arming "
            "WHERE revoked_at IS NULL AND expires_at > %s ORDER BY armed_at DESC LIMIT 1",
            (now,),
        ).fetchone()
        return dict(row) if row else None

    def latest_attestations(self) -> dict[str, tuple[datetime, str]]:
        rows = self._conn.execute(
            "SELECT DISTINCT ON (code) code, attested_at, attested_by FROM live_attestations "
            "ORDER BY code, attested_at DESC"
        ).fetchall()
        return {r["code"]: (r["attested_at"], r["attested_by"]) for r in rows}

    def add_attestation(self, code: str, by: str, note: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO live_attestations (code, attested_at, attested_by, note) "
            "VALUES (%s, %s, %s, %s)",
            (code, now, by, note),
        )

    def add_arming(
        self,
        arming_id: UUID,
        *,
        armed_at: datetime,
        expires_at: datetime,
        armed_by: str,
        key_hint: str,
        checks: dict[str, Any],
    ) -> None:
        self._conn.execute(
            "INSERT INTO live_arming (id, armed_at, expires_at, armed_by, key_hint, checks) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
            (arming_id, armed_at, expires_at, armed_by, key_hint, json.dumps(checks)),
        )

    def revoke_arming(self, now: datetime) -> int:
        cur = self._conn.execute(
            "UPDATE live_arming SET revoked_at = %s WHERE revoked_at IS NULL AND expires_at > %s",
            (now, now),
        )
        return cur.rowcount

    def baselines(self, venue: str) -> dict[str, Decimal]:
        rows = self._conn.execute(
            "SELECT currency, amount FROM venue_baselines WHERE venue = %s", (venue,)
        ).fetchall()
        return {r["currency"]: r["amount"] for r in rows}

    # ------------------------------------------------------------------ intents and decisions
    def add_intent(self, row: IntentRow) -> None:
        self._conn.execute(
            "INSERT INTO order_intents (id, intent_key, venue, pair_id, product_id, side, order_type, "
            "post_only, price, base_qty, expected_cycle_return, source, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                row.id, row.intent_key, row.venue, row.pair_id, row.product_id, row.side,
                row.order_type, row.post_only, row.price, row.base_qty, row.expected_cycle_return,
                row.source, row.created_at,
            ),
        )  # fmt: skip

    def intent(self, intent_id: UUID) -> IntentRow | None:
        row = self._conn.execute(
            "SELECT * FROM order_intents WHERE id = %s", (intent_id,)
        ).fetchone()
        return IntentRow(**row) if row else None

    def intent_by_key(self, key: str) -> IntentRow | None:
        row = self._conn.execute(
            "SELECT * FROM order_intents WHERE intent_key = %s", (key,)
        ).fetchone()
        return IntentRow(**row) if row else None

    def intents_since(self, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS n FROM order_intents WHERE created_at > %s", (since,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def add_decision(self, row: DecisionRow) -> None:
        self._conn.execute(
            "INSERT INTO risk_decisions (id, intent_id, decision, reasons, inputs_hash, decided_at, "
            "expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                row.id, row.intent_id, row.decision, list(row.reasons), row.inputs_hash,
                row.decided_at, row.expires_at,
            ),
        )  # fmt: skip

    def decision(self, decision_id: UUID) -> DecisionRow | None:
        row = self._conn.execute(
            "SELECT * FROM risk_decisions WHERE id = %s", (decision_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["reasons"] = tuple(data["reasons"])
        return DecisionRow(**data)

    def decisions_for(self, intent_id: UUID) -> list[DecisionRow]:
        rows = self._conn.execute(
            "SELECT * FROM risk_decisions WHERE intent_id = %s ORDER BY decided_at", (intent_id,)
        ).fetchall()
        return [DecisionRow(**{**r, "reasons": tuple(r["reasons"])}) for r in rows]

    def recent_block_reasons(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(
            self._conn.execute(
                "SELECT decided_at, decision, reasons FROM risk_decisions ORDER BY decided_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
        )

    # ------------------------------------------------------------------ attempts
    def add_attempt(
        self,
        *,
        attempt_id: UUID,
        intent_id: UUID,
        attempt_no: int,
        client_order_id: UUID,
        decision_id: UUID,
        boot_id: str,
        now: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO order_attempts (id, intent_id, attempt_no, client_order_id, decision_id, "
            "boot_id, state, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, 'AUTHORIZED', %s, %s)",
            (attempt_id, intent_id, attempt_no, client_order_id, decision_id, boot_id, now, now),
        )

    def attempt(self, attempt_id: UUID, *, for_update: bool = False) -> AttemptRow | None:
        row = self._conn.execute(
            "SELECT * FROM order_attempts WHERE id = %s" + (" FOR UPDATE" if for_update else ""),
            (attempt_id,),
        ).fetchone()
        return _attempt(row) if row else None

    def attempt_by_client_id(self, client_order_id: str | UUID) -> AttemptRow | None:
        row = self._conn.execute(
            "SELECT * FROM order_attempts WHERE client_order_id::text = %s", (str(client_order_id),)
        ).fetchone()
        return _attempt(row) if row else None

    def attempt_by_exchange_id(self, exchange_order_id: str) -> AttemptRow | None:
        row = self._conn.execute(
            "SELECT * FROM order_attempts WHERE exchange_order_id = %s", (exchange_order_id,)
        ).fetchone()
        return _attempt(row) if row else None

    def attempts_for_intent(self, intent_id: UUID) -> list[AttemptRow]:
        rows = self._conn.execute(
            "SELECT * FROM order_attempts WHERE intent_id = %s ORDER BY attempt_no", (intent_id,)
        ).fetchall()
        return [_attempt(r) for r in rows]

    def attempts_in(self, states: tuple[str, ...], venue: str | None = None) -> list[AttemptRow]:
        rows = self._conn.execute(
            "SELECT a.* FROM order_attempts a JOIN order_intents i ON i.id = a.intent_id "
            "WHERE a.state = ANY(%s) AND (%s::text IS NULL OR i.venue = %s) ORDER BY a.created_at",
            (list(states), venue, venue),
        ).fetchall()
        return [_attempt(r) for r in rows]

    def attempts_all(self, venue: str) -> list[AttemptRow]:
        rows = self._conn.execute(
            "SELECT a.* FROM order_attempts a JOIN order_intents i ON i.id = a.intent_id "
            "WHERE i.venue = %s ORDER BY a.created_at",
            (venue,),
        ).fetchall()
        return [_attempt(r) for r in rows]

    def count_state(self, state: str) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS n FROM order_attempts WHERE state = %s", (state,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def counts_by_state(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT state, count(*) AS n FROM order_attempts GROUP BY state"
        ).fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    def recent_attempt_states(self, limit: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT state FROM order_attempts ORDER BY created_at DESC, attempt_no DESC LIMIT %s",
            (limit,),
        ).fetchall()
        return [r["state"] for r in rows]

    def mark_submitting(self, attempt_id: UUID, now: datetime) -> bool:
        """Compare-and-set AUTHORIZED -> SUBMITTING, committed BEFORE any exchange I/O."""
        cur = self._conn.execute(
            "UPDATE order_attempts SET state = 'SUBMITTING', submitting_at = %s, updated_at = %s "
            "WHERE id = %s AND state = 'AUTHORIZED'",
            (now, now, attempt_id),
        )
        return cur.rowcount == 1

    def transition(
        self,
        attempt_id: UUID,
        from_states: tuple[str, ...],
        to_state: str,
        now: datetime,
        *,
        exchange_order_id: str | None = None,
        filled_qty: Decimal | None = None,
        failure_code: str | None = None,
    ) -> bool:
        cur = self._conn.execute(
            "UPDATE order_attempts SET state = %s, updated_at = %s, "
            "exchange_order_id = COALESCE(exchange_order_id, %s), "
            "filled_qty = GREATEST(filled_qty, COALESCE(%s, filled_qty)), "
            "failure_code = COALESCE(%s, failure_code) "
            "WHERE id = %s AND state = ANY(%s)",
            (
                to_state,
                now,
                exchange_order_id,
                filled_qty,
                failure_code,
                attempt_id,
                list(from_states),
            ),
        )
        return cur.rowcount == 1

    # ------------------------------------------------------------------ fills
    def add_fill(self, row: FillRow) -> bool:
        cur = self._conn.execute(
            "INSERT INTO attempt_fills (attempt_id, venue, exchange_fill_id, side, price, size, fee, "
            "liquidity, occurred_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (venue, exchange_fill_id) DO NOTHING",
            (
                row.attempt_id, row.venue, row.exchange_fill_id, row.side, row.price, row.size,
                row.fee, row.liquidity, row.occurred_at,
            ),
        )  # fmt: skip
        return cur.rowcount == 1

    def fills(self, venue: str) -> list[FillRow]:
        rows = self._conn.execute(
            "SELECT attempt_id, venue, exchange_fill_id, side, price, size, fee, liquidity, occurred_at "
            "FROM attempt_fills WHERE venue = %s ORDER BY id",
            (venue,),
        ).fetchall()
        return [FillRow(**r) for r in rows]

    def fills_for_attempt(self, attempt_id: UUID) -> list[FillRow]:
        rows = self._conn.execute(
            "SELECT attempt_id, venue, exchange_fill_id, side, price, size, fee, liquidity, occurred_at "
            "FROM attempt_fills WHERE attempt_id = %s ORDER BY id",
            (attempt_id,),
        ).fetchall()
        return [FillRow(**r) for r in rows]

    # ------------------------------------------------------------------ hints and API events
    def add_hint(self, row: HintRow) -> None:
        self._conn.execute(
            "INSERT INTO order_hints (venue, client_order_id, order_id, status, filled_qty, sequence, "
            "received_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                row.venue, row.client_order_id, row.order_id, row.status, row.filled_qty,
                row.sequence, row.received_at,
            ),
        )  # fmt: skip

    def hints_since(self, venue: str, since: datetime) -> list[HintRow]:
        rows = self._conn.execute(
            "SELECT venue, client_order_id, order_id, status, filled_qty, sequence, received_at "
            "FROM order_hints WHERE venue = %s AND received_at >= %s ORDER BY id",
            (venue, since),
        ).fetchall()
        return [HintRow(**r) for r in rows]

    def add_api_event(
        self, venue: str, operation: str, ok: bool, code: str | None, now: datetime
    ) -> None:
        self._conn.execute(
            "INSERT INTO api_events (venue, operation, ok, code, occurred_at) VALUES (%s, %s, %s, %s, %s)",
            (venue, operation, ok, code, now),
        )

    def api_failures_since(self, venue: str, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS n FROM api_events WHERE venue = %s AND NOT ok AND occurred_at > %s",
            (venue, since),
        ).fetchone()
        return int(row["n"]) if row else 0

    def api_calls_since(self, since: datetime) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT count(*) AS n, count(*) FILTER (WHERE NOT ok) AS bad FROM api_events WHERE occurred_at > %s",
            (since,),
        ).fetchone()
        return (int(row["n"]), int(row["bad"])) if row else (0, 0)

    def prune_api_events(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM api_events WHERE occurred_at < clock_timestamp() - interval '7 days'"
        )
        return cur.rowcount

    # ------------------------------------------------------------------ commands
    def add_command(self, command_id: UUID, origin: str, requested_by: UUID, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO control_commands (id, kind, origin, requested_by, requested_at) "
            "VALUES (%s, 'CANCEL_KNOWN', %s, %s, %s)",
            (command_id, origin, requested_by, now),
        )

    def open_command(self) -> CommandRow | None:
        row = self._conn.execute(
            "SELECT * FROM control_commands WHERE state IN ('PENDING', 'RUNNING') LIMIT 1"
        ).fetchone()
        return CommandRow(**row) if row else None

    def pending_command(self) -> CommandRow | None:
        row = self._conn.execute(
            "SELECT * FROM control_commands WHERE state = 'PENDING' ORDER BY requested_at LIMIT 1"
        ).fetchone()
        return CommandRow(**row) if row else None

    def recent_commands(self, limit: int = 10) -> list[CommandRow]:
        rows = self._conn.execute(
            "SELECT * FROM control_commands ORDER BY requested_at DESC LIMIT %s", (limit,)
        ).fetchall()
        return [CommandRow(**r) for r in rows]

    def finish_command(
        self,
        command_id: UUID,
        state: str,
        now: datetime,
        result: dict[str, Any] | None,
        failure_code: str | None,
    ) -> None:
        from psycopg.types.json import Jsonb

        self._conn.execute(
            "UPDATE control_commands SET state = %s, finished_at = %s, result = %s, failure_code = %s "
            "WHERE id = %s",
            (state, now, Jsonb(result) if result is not None else None, failure_code, command_id),
        )

    def start_command(self, command_id: UUID) -> None:
        self._conn.execute(
            "UPDATE control_commands SET state = 'RUNNING' WHERE id = %s AND state = 'PENDING'",
            (command_id,),
        )
