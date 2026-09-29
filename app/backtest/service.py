"""Runs backtests on frozen snapshots and stores results and reports. Host CLI only (td_ctl).

The only data source is a dataset snapshot: the Parquet file is re-verified against the checksums in
its immutable database row before a single candle is used, and the frozen product-metadata snapshot
supplies the exchange rules. Results and reports are immutable and idempotent: the same snapshot,
config and engine version return the stored row instead of writing a second one.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.backtest.engine import SCENARIOS, config_payload, config_sha256, run_backtest
from app.backtest.trader import SimConfig
from app.backtest.walkforward import walk_forward
from app.config import Settings
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import Clock
from app.market.ingest import GRANULARITY
from app.market.snapshots import ENGINE_VERSION, SnapshotError, load_snapshot
from app.pairs import policy as pair_policy
from app.pairs.runner import HOST_ACTOR
from app.reports import builder
from app.storage.database import Storage
from app.storage.market_repositories import BacktestRow, ReportRow
from app.strategy import decision as strat
from app.strategy.grid import GridRejected, rules_from_metadata


class BacktestError(Exception):
    """A backtest could not be run. `code` is a fixed reason; nothing was stored."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BacktestService:
    def __init__(self, *, storage: Storage, clock: Clock, settings: Settings) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._policy = settings.pair_policy
        self._audit = AuditWriter(clock)

    def run(
        self, snapshot_id: UUID, *, walk: bool = False, levels: int | None = None
    ) -> tuple[ReportRow, bool]:
        """Backtest (or walk-forward) both fee scenarios. Returns (report, newly_created)."""
        now = self._clock.now()
        try:
            operator = strat.operator_fee(self._policy, now.date())
        except strat.FeeNotAttested as exc:
            raise BacktestError(exc.code) from exc
        with self._storage.tx() as repos:
            snap = repos.results.snapshot(snapshot_id)
            if snap is None:
                raise BacktestError("UNKNOWN_SNAPSHOT")
            if snap.metadata_snapshot_id is None:
                raise BacktestError("SNAPSHOT_HAS_NO_METADATA")
            meta = repos.products.metadata_snapshot(snap.metadata_snapshot_id)
            product = repos.products.get(snap.product_uuid)
        if meta is None or product is None:
            raise BacktestError("METADATA_MISSING")
        try:
            candles = load_snapshot(snap, self._settings)
            rules = rules_from_metadata(meta)
        except SnapshotError as exc:
            raise BacktestError(f"SNAPSHOT_{exc.code}") from exc
        except GridRejected as exc:
            raise BacktestError(exc.code) from exc

        levels = levels or self._policy.grid_levels
        fees = {"OPERATOR": operator, "STRESS": self._policy.fees.stress_maker_rate}
        scenarios: dict[str, dict[str, Any]] = {}
        wf: dict[str, dict[str, Any]] = {}
        hashes: dict[str, str] = {}
        rows: list[BacktestRow] = []
        for scenario in SCENARIOS:
            cfg = SimConfig(self._policy, rules, fees[scenario], operator, levels)
            hashes[scenario] = config_sha256(cfg, scenario)
            if walk:
                try:
                    data = walk_forward(candles, cfg, scenario=scenario)
                except ValueError as exc:  # too few candles for a single fold
                    raise BacktestError("TOO_SHORT_FOR_WALK_FORWARD") from exc
                wf[scenario] = data
            else:
                data = run_backtest(candles, cfg, scenario=scenario).data
                scenarios[scenario] = data
            rows.append(
                BacktestRow(
                    uuid4(),
                    snap.id,
                    "WALK_FORWARD" if walk else "BACKTEST",
                    scenario,
                    hashes[scenario],
                    ENGINE_VERSION,
                    config_payload(cfg, scenario),
                    data,
                    now,
                )
            )
        snapshot_info = {
            "id": str(snap.id),
            "candles": snap.candle_count,
            "gap_count": snap.gap_count,
            "missing_intervals": snap.missing_count,
            "range_start_iso": builder.iso(snap.range_start),
            "range_end_iso": builder.iso(snap.range_end),
            "file_sha256": snap.file_sha256,
            "content_sha256": snap.content_sha256,
            "max_ingest_run_id": snap.max_ingest_run_id,
            "metadata_snapshot_id": str(snap.metadata_snapshot_id),
            "engine_version_at_freeze": snap.engine_version,
        }
        kind = "WALK_FORWARD" if walk else "BACKTEST"
        body = builder.backtest_body(
            kind=kind,
            product_id=product.metadata.product_id,
            data_basis=pair_policy.derive_data_basis(meta)[1],
            snapshot=snapshot_info,
            scenarios=scenarios or {k: {"see": "walk_forward"} for k in wf},
            config_hashes=hashes,
            walk_forward=wf or None,
        )
        return self._store(kind, body, rows, snap.id, product.metadata.product_id)

    def _store(
        self,
        kind: str,
        body: dict[str, Any],
        rows: list[BacktestRow],
        snapshot_id: UUID,
        product_id: str,
    ) -> tuple[ReportRow, bool]:
        sha = builder.sha256_of(body)
        with self._storage.tx() as repos:
            source: UUID | None = None
            for row in rows:
                repos.results.insert_backtest(row)  # an identical earlier row is kept as it is
                stored_row = repos.results.backtest_by_identity(
                    row.snapshot_id,
                    row.kind,
                    row.fee_scenario,
                    row.config_sha256,
                    row.engine_version,
                )
                source = source or (stored_row.id if stored_row else None)
            report = ReportRow(
                uuid4(),
                kind,
                "BACKTEST",
                source,
                f"{kind.replace('_', ' ').title()} {product_id}"[:120],
                builder.canonical_json(body),
                builder.backtest_markdown(body, sha),
                sha,
                self._clock.now(),
            )
            created = repos.results.insert_report(report)
            stored = repos.results.report_by_hash(kind, sha)
            if stored is None:  # pragma: no cover  (inserted or already present)
                raise BacktestError("REPORT_NOT_RECORDED")
            self._audit.record(
                repos,
                AuditEventType.BACKTEST_COMPLETED,
                AuditResult.SUCCESS,
                actor=HOST_CLI_ACTOR,
                target_type="dataset",
                target_id=str(snapshot_id),
                reason=kind,
                client_tag=HOST_ACTOR.client_tag,
                request_id=HOST_ACTOR.request_id,
                detail={"product": product_id, "new_report": created},
            )
            if created:
                self._audit.record(
                    repos,
                    AuditEventType.REPORT_CREATED,
                    AuditResult.SUCCESS,
                    actor=HOST_CLI_ACTOR,
                    target_type="report",
                    target_id=str(stored.id),
                    reason=kind,
                    client_tag=HOST_ACTOR.client_tag,
                    request_id=HOST_ACTOR.request_id,
                    detail={"label": "BACKTEST"},
                )
        return stored, created

    # ------------------------------------------------------------------ other reports
    def store_report(
        self, kind: str, label: str, title: str, body: dict[str, Any], md: str, sha: str
    ) -> tuple[ReportRow, bool]:
        report = ReportRow(
            uuid4(),
            kind,
            label,
            None,
            title[:120],
            builder.canonical_json(body),
            md,
            sha,
            self._clock.now(),
        )
        with self._storage.tx() as repos:
            created = repos.results.insert_report(report)
            stored = repos.results.report_by_hash(kind, sha)
            if stored is None:  # pragma: no cover
                raise BacktestError("REPORT_NOT_RECORDED")
            if created:
                self._audit.record(
                    repos,
                    AuditEventType.REPORT_CREATED,
                    AuditResult.SUCCESS,
                    actor=HOST_CLI_ACTOR,
                    target_type="report",
                    target_id=str(stored.id),
                    reason=kind,
                    client_tag=HOST_ACTOR.client_tag,
                    request_id=HOST_ACTOR.request_id,
                    detail={"label": label},
                )
        return stored, created

    def paper_report(self, exchange: Any) -> tuple[ReportRow, bool]:
        status = exchange.status()
        with self._storage.tx() as repos:
            orders = [
                {
                    "seq": o.seq,
                    "cell": o.level_index,
                    "side": o.side,
                    "price": _c(o.price),
                    "qty": _c(o.base_qty),
                    "filled": _c(o.filled_qty),
                    "state": o.state,
                }
                for o in reversed(repos.paper.orders(30))
            ]
        status_dict = {
            "state": status.state,
            "phase": status.phase,
            "cash": _c(status.cash),
            "reserved": _c(status.reserved),
            "free_cash": _c(status.free_cash),
            "inventory": _c(status.inventory),
            "cost_basis": _c(status.cost_basis),
            "deployed": _c(status.deployed),
            "orders_by_state": dict(sorted(status.orders_by_state.items())),
            "fills": status.fills,
            "fees_paid": _c(status.fees_paid),
            "data_stale": status.data_stale,
        }
        body = builder.paper_body(
            product_id=status.product_id,
            status=status_dict,
            orders=orders,
            as_of=status.last_candle_start,
        )
        sha = builder.sha256_of(body)
        return self.store_report(
            "PAPER_DAILY",
            "PAPER",
            f"Paper report {status.product_id or ''}",
            body,
            builder.paper_markdown(body, sha),
            sha,
        )

    def quality_report(self, product_id: str) -> tuple[ReportRow, bool]:
        with self._storage.tx() as repos:
            product = repos.products.get_by_product_id(product_id)
            if product is None:
                raise BacktestError("UNKNOWN_PRODUCT")
            candles = repos.market.candle_count(product.id, GRANULARITY)
            cursor = repos.market.cursor(product.id, GRANULARITY)
            events = repos.market.event_counts()
            run = repos.market.last_run(product.id, GRANULARITY)
        runs = (
            [
                {
                    "id": run.id,
                    "status": run.status,
                    "inserted": run.inserted,
                    "duplicates": run.duplicates,
                    "conflicts": run.conflicts,
                    "gaps": run.gaps,
                    "missing": run.missing,
                    "retries": run.retries,
                }
            ]
            if run
            else []
        )
        body = builder.quality_body(
            product_id=product_id, runs=runs, events=events, candles=candles, cursor=cursor
        )
        sha = builder.sha256_of(body)
        return self.store_report(
            "DATA_QUALITY",
            "BACKTEST",
            f"Data quality {product_id}",
            body,
            builder.quality_markdown(body, sha),
            sha,
        )


def _c(value: Any) -> str:
    from app.domain.money import canonical

    return canonical(value)
