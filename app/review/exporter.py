"""Typed export views: what a review package may contain, and nothing else.

Each dataset is a schema of typed fields (see `app.review.sanitizer`). The queries behind them
select allow-listed columns only. Backtest summaries are read through explicit key paths, so an
unexpected key in a stored summary is ignored, never copied.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any, Final

from app.review import sanitizer as sz
from app.review.sanitizer import Validator, optional
from app.storage.repositories import Repos

_STATE = sz.enum("OPEN", "FILLED", "CANCELLED", "REJECTED")
_D = sz.decimal_str
_OD = optional(sz.decimal_str)
_N = sz.integer(0, 2**31 - 1)
_SIDE = sz.enum("BUY", "SELL")

SCHEMAS: Final[dict[str, dict[str, Validator]]] = {
    "data/backtest_runs.jsonl": {
        "run_id": sz.uuid_str,
        "snapshot_id": sz.uuid_str,
        "product": sz.product,
        "kind": sz.enum("BACKTEST", "WALK_FORWARD"),
        "fee_scenario": sz.enum("OPERATOR", "STRESS"),
        "config_sha256": sz.sha256,
        "engine_version": sz.version,
        "created_at": sz.timestamp,
        "net_pnl": _OD,
        "realized_pnl": _OD,
        "unrealized_pnl": _OD,
        "fees_paid": _OD,
        "max_drawdown": _OD,
        "buy_and_hold_return": _OD,
        "fills": optional(_N),
        "partial_fills": optional(_N),
        "touched_no_fill": optional(_N),
        "cycles_completed": optional(_N),
        "orders_placed": optional(_N),
        "stale_age_cancels": optional(_N),
        "stale_gap_cancels": optional(_N),
        "breakout_stops": optional(_N),
        "halts": optional(_N),
        "decisions_grid": optional(_N),
        "decisions_no_trade": optional(_N),
        "fold_count": optional(_N),
        "out_of_sample_net_pnl": _OD,
        "positive_folds": optional(_N),
        "negative_folds": optional(_N),
        "flat_folds": optional(_N),
        "worst_fold_pnl": _OD,
        "worst_drawdown": _OD,
    },
    "data/reports_index.jsonl": {
        "report_id": sz.uuid_str,
        "kind": sz.enum("BACKTEST", "WALK_FORWARD", "PAPER_DAILY", "DATA_QUALITY"),
        "mode_label": sz.enum("BACKTEST", "PAPER"),
        "sha256": sz.sha256,
        "created_at": sz.timestamp,
    },
    "data/dataset_snapshots.jsonl": {
        "snapshot_id": sz.uuid_str,
        "product": sz.product,
        "range_start": sz.epoch_ts,
        "range_end": sz.epoch_ts,
        "candle_count": _N,
        "gap_count": _N,
        "missing_count": _N,
        "file_sha256": sz.sha256,
        "content_sha256": sz.sha256,
        "engine_version": sz.version,
    },
    "data/paper_orders.jsonl": {
        "order_id": sz.uuid_str,
        "seq": _N,
        "level_index": sz.integer(0, 4),
        "cycle_no": _N,
        "side": _SIDE,
        "price": _D,
        "base_qty": _D,
        "filled_qty": _D,
        "state": _STATE,
        "placed_candle": sz.epoch_ts,
    },
    "data/paper_fills.jsonl": {
        "order_id": sz.uuid_str,
        "candle_start": sz.epoch_ts,
        "price": _D,
        "base_qty": _D,
        "notional": _D,
        "fee": _D,
    },
    "data/paper_summary.jsonl": {
        "state": sz.enum("PAUSED", "RUNNING"),
        "phase": sz.enum("IDLE", "ACTIVE", "STOPPED", "HALTED"),
        "cash": _D,
        "reserved": _D,
        "inventory": _D,
        "cost_basis": _D,
        "fills": _N,
        "fees_paid": _D,
    },
    "data/pair_lifecycle.jsonl": {
        "product": sz.product,
        "version_after": _N,
        "state_before": optional(sz.code),
        "state_after": sz.code,
        "actor_class": sz.enum("WEB", "HOST"),
        "transition_no": _N,
        "reason_code": sz.code_or_none,
        "occurred_at": sz.timestamp,
    },
    "data/ingest_runs.jsonl": {
        "run_id": _N,
        "product": sz.product,
        "status": sz.enum("COMPLETE", "PARTIAL", "FAILED"),
        "window_start": sz.epoch_ts,
        "window_end": sz.epoch_ts,
        "requests": _N,
        "retries": _N,
        "fetched": _N,
        "inserted": _N,
        "duplicates": _N,
        "conflicts": _N,
        "malformed": _N,
        "gaps": _N,
        "missing": _N,
    },
    "data/data_quality_events.jsonl": {
        "run_id": _N,
        "code": sz.code,
        "severity": sz.enum("INFO", "WARN", "ERROR"),
        "count": _N,
    },
    "data/grid_plans.jsonl": {
        "plan_id": sz.uuid_str,
        "product": sz.product,
        "levels": sz.integer(3, 5),
        "lower_price": _D,
        "upper_price": _D,
        "prices": sz.decimal_list,
        "cell_budget": _D,
        "config_sha256": sz.sha256,
        "created_at": sz.timestamp,
    },
    "data/audit_summary.jsonl": {
        "day": sz.day,
        "event_code": sz.event_code,
        "result": sz.enum("SUCCESS", "FAILURE", "DENIED"),
        "count": _N,
    },
}

SCOPE_FILES: Final[dict[str, tuple[str, ...]]] = {
    "backtests": (
        "data/backtest_runs.jsonl",
        "data/reports_index.jsonl",
        "data/dataset_snapshots.jsonl",
    ),
    "paper": ("data/paper_orders.jsonl", "data/paper_fills.jsonl", "data/paper_summary.jsonl"),
    "pairs": ("data/pair_lifecycle.jsonl",),
    "data_quality": ("data/ingest_runs.jsonl", "data/data_quality_events.jsonl"),
    "grid_plans": ("data/grid_plans.jsonl",),
    "audit_summary": ("data/audit_summary.jsonl",),
}
NOT_AVAILABLE: Final = ("risk_events", "order_intents", "reconciliation_summaries")


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return None
        d = d[key]
    return d


_BACKTEST_PATHS: Final[dict[str, tuple[str, ...]]] = {
    "net_pnl": ("performance", "net_pnl"),
    "realized_pnl": ("performance", "realized_pnl"),
    "unrealized_pnl": ("performance", "unrealized_pnl"),
    "fees_paid": ("performance", "fees_paid"),
    "max_drawdown": ("performance", "max_drawdown"),
    "buy_and_hold_return": ("performance", "buy_and_hold_return"),
    "fills": ("activity", "fills"),
    "partial_fills": ("activity", "partial_fills"),
    "touched_no_fill": ("activity", "touched_no_fill"),
    "cycles_completed": ("activity", "cycles_completed"),
    "orders_placed": ("activity", "orders_placed"),
    "stale_age_cancels": ("activity", "stale_age_cancels"),
    "stale_gap_cancels": ("activity", "stale_gap_cancels"),
    "breakout_stops": ("activity", "breakout_stops"),
    "halts": ("activity", "halts"),
    "decisions_grid": ("activity", "decisions_grid"),
    "decisions_no_trade": ("activity", "decisions_no_trade"),
    "fold_count": ("summary", "fold_count"),
    "out_of_sample_net_pnl": ("summary", "out_of_sample_net_pnl"),
    "positive_folds": ("summary", "positive_folds"),
    "negative_folds": ("summary", "negative_folds"),
    "flat_folds": ("summary", "flat_folds"),
    "worst_fold_pnl": ("summary", "worst_fold_pnl"),
    "worst_drawdown": ("summary", "worst_drawdown"),
}


def _backtest_row(raw: dict[str, Any]) -> dict[str, Any]:
    summary = raw.pop("summary")
    for name, path in _BACKTEST_PATHS.items():
        raw[name] = _get(summary, *path)
    return raw


def _plain(raw: dict[str, Any]) -> dict[str, Any]:
    return raw


_LOADERS: Final[dict[str, Callable[[Repos, date, date], list[dict[str, Any]]]]] = {
    "data/backtest_runs.jsonl": lambda r, a, b: [
        _backtest_row(x) for x in r.export.backtests(a, b)
    ],
    "data/reports_index.jsonl": lambda r, a, b: r.export.reports(a, b),
    "data/dataset_snapshots.jsonl": lambda r, a, b: r.export.snapshots(a, b),
    "data/paper_orders.jsonl": lambda r, a, b: r.export.paper_orders(a, b),
    "data/paper_fills.jsonl": lambda r, a, b: r.export.paper_fills(a, b),
    "data/paper_summary.jsonl": lambda r, a, b: [r.export.paper_summary()],
    "data/pair_lifecycle.jsonl": lambda r, a, b: r.export.pair_lifecycle(a, b),
    "data/ingest_runs.jsonl": lambda r, a, b: r.export.data_quality_runs(a, b),
    "data/data_quality_events.jsonl": lambda r, a, b: r.export.data_quality_events(a, b),
    "data/grid_plans.jsonl": lambda r, a, b: r.export.grid_plans(a, b),
    "data/audit_summary.jsonl": lambda r, a, b: r.export.audit_summary(a, b),
}


def export(
    repos: Repos, scope: tuple[str, ...], start: date, end: date, max_rows: int
) -> dict[str, list[dict[str, Any]]]:
    """Sanitized rows per data file, in a deterministic order. Raises `Rejected` on any problem."""
    out: dict[str, list[dict[str, Any]]] = {}
    for name in scope:
        if name not in SCOPE_FILES:
            raise sz.Rejected("BAD_SCOPE")
        for path in SCOPE_FILES[name]:
            raw = _LOADERS[path](repos, start, end)
            if len(raw) > max_rows:
                raise sz.Rejected("TOO_MANY_ROWS")
            out[path] = [sz.sanitize_row(SCHEMAS[path], row) for row in raw]
    return out
