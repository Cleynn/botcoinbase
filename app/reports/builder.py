"""Report builders: canonical JSON plus a Markdown rendering of the same data.

A report contains only values the system computed (Decimals as canonical strings, fixed vocabulary,
pattern-checked product ids). Nothing an exchange or a user typed is copied in. The body carries no
generation time, so identical inputs give identical bytes and an identical SHA-256; the row stores
the creation time separately. Every report states its mode label (BACKTEST or PAPER), that live
trading is blocked, and its limitations.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Final

from app import constants
from app.market.snapshots import ENGINE_VERSION

REPORT_VERSION: Final = 1

LIMITATIONS: Final = (
    "Simulated with closed five-minute candles only: no order book, queue position or intra-candle order is modelled.",
    "Fills are pessimistic by construction (price must trade through the limit, volume-capped, delayed one candle) but real fills can differ in either direction.",
    "Fees are operator-attested, not read from Coinbase; the stress scenario uses the configured stress maker fee.",
    "Synthetic or historical results do not predict future results and are not a profit guarantee.",
    "The strategy never sells holdings at market; an unfinished position is marked to the last close, not realised.",
    "Capital growth and regridding are disabled; the deployment cap is a fixed 35 USDC and the protected reserve at least 15 USDC.",
)


def canonical_json(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_of(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(body).encode("ascii")).hexdigest()


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def banner(label: str) -> str:
    return f"MODE: {label} | LIVE TRADING: {constants.LIVE_TRADING_STATUS}"


# ---------------------------------------------------------------------------- backtest
def backtest_body(
    *,
    kind: str,
    product_id: str,
    data_basis: str,
    snapshot: dict[str, Any],
    scenarios: dict[str, dict[str, Any]],
    config_hashes: dict[str, str],
    walk_forward: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "kind": kind,
        "label": "BACKTEST",
        "banner": banner("BACKTEST"),
        "engine_version": ENGINE_VERSION,
        "product": product_id,
        "priced_from": "unified USD book"
        if data_basis == "UNIFIED_USD_BOOK"
        else data_basis.lower().replace("_", " "),
        "snapshot": snapshot,
        "config_sha256": dict(sorted(config_hashes.items())),
        "scenarios": scenarios,
        "limitations": list(LIMITATIONS),
        "growth_policy": "disabled",
    }
    if walk_forward is not None:
        body["walk_forward"] = walk_forward
    return body


def backtest_markdown(body: dict[str, Any], sha: str) -> str:
    snap = body["snapshot"]
    lines = [
        f"# {body['kind'].replace('_', ' ').title()} report: {body['product']}",
        "",
        f"**{body['banner']}**",
        "",
        "This is a simulation on a frozen dataset. It is not real trading and not a promise of profit.",
        "",
        "## Provenance",
        f"- Report SHA-256 (of the JSON body): `{sha}`",
        f"- Dataset snapshot: `{snap['id']}` ({snap['candles']} candles, {snap['gap_count']} gaps, "
        f"{snap['missing_intervals']} missing intervals)",
        f"- Range: {snap['range_start_iso']} to {snap['range_end_iso']} (UTC)",
        f"- Dataset file SHA-256: `{snap['file_sha256']}`",
        f"- Dataset content SHA-256: `{snap['content_sha256']}`",
        f"- Engine version: {body['engine_version']}; priced from: {body['priced_from']}",
        "",
    ]
    for scenario, hash_ in body["config_sha256"].items():
        lines.append(f"- Config SHA-256 ({scenario}): `{hash_}`")
    if body["kind"] == "BACKTEST":
        lines += [
            "",
            "## Results",
            "",
            "| Metric | " + " | ".join(body["scenarios"]) + " |",
            "|---|" + "---|" * len(body["scenarios"]),
        ]
        rows = (
            ("Fee rate", lambda r: r["fee_rate"]),
            ("Net P&L (USDC)", lambda r: r["performance"]["net_pnl"]),
            ("Return", lambda r: r["performance"]["return_display"]),
            ("Realised P&L", lambda r: r["performance"]["realized_pnl"]),
            ("Unrealised P&L", lambda r: r["performance"]["unrealized_pnl"]),
            ("Fees paid", lambda r: r["performance"]["fees_paid"]),
            ("Max drawdown", lambda r: r["performance"]["max_drawdown_display"]),
            ("Buy and hold (context)", lambda r: r["performance"]["buy_and_hold_return"][:8]),
            ("Fills", lambda r: str(r["activity"]["fills"])),
            ("Partial fills", lambda r: str(r["activity"]["partial_fills"])),
            ("Touched, not filled", lambda r: str(r["activity"]["touched_no_fill"])),
            ("Cycles completed", lambda r: str(r["activity"]["cycles_completed"])),
            (
                "Stale cancels (age / gap)",
                lambda r: (
                    f"{r['activity']['stale_age_cancels']} / {r['activity']['stale_gap_cancels']}"
                ),
            ),
            ("Post-only rejects", lambda r: str(r["activity"]["post_only_rejects"])),
            ("Capital-limit blocks", lambda r: str(r["activity"]["capital_blocked"])),
            (
                "Breakout stops / halts",
                lambda r: f"{r['activity']['breakout_stops']} / {r['activity']['halts']}",
            ),
            (
                "Decisions GRID / NO_TRADE",
                lambda r: (
                    f"{r['activity']['decisions_grid']} / {r['activity']['decisions_no_trade']}"
                ),
            ),
            ("End phase", lambda r: r["end_state"]["phase"]),
            (
                "Open inventory / cost",
                lambda r: f"{r['end_state']['inventory']} / {r['end_state']['cost_basis']}",
            ),
        )
        for name, get in rows:
            lines.append(
                f"| {name} | " + " | ".join(get(r) for r in body["scenarios"].values()) + " |"
            )
        reasons: dict[str, int] = {}
        for r in body["scenarios"].values():
            for k, v in r["activity"]["no_trade_reasons"].items():
                reasons[k] = max(reasons.get(k, 0), v)
        lines += ["", "## Why NO_TRADE", ""]
        lines += [f"- {k}: {v} evaluations" for k, v in sorted(reasons.items())] or [
            "- none recorded"
        ]
    else:
        for scenario, wf in (body.get("walk_forward") or {}).items():
            s = wf["summary"]
            lines += [
                "",
                f"## Walk-forward ({scenario})",
                "",
                f"- Folds: {s['fold_count']} (train {wf['train_candles']} candles, test {wf['test_candles']} candles)",
                f"- Out-of-sample net P&L: {s['out_of_sample_net_pnl']} USDC; mean fold return {s['mean_fold_return_display']}",
                f"- Positive / negative / flat folds: {s['positive_folds']} / {s['negative_folds']} / {s['flat_folds']}",
                f"- Worst fold P&L {s['worst_fold_pnl']}; worst drawdown {s['worst_drawdown']}",
                "",
                "| Fold | Test start | Levels | Buffer | Net P&L | Drawdown | Fills |",
                "|---|---|---|---|---|---|---|",
            ]
            for f in wf["folds"]:
                t = f["test"]
                lines.append(
                    f"| {f['fold']} | {iso(f['test_first_candle'])} | {f['chosen_levels']} | "
                    f"{f['chosen_breakout_buffer']} | {t['net_pnl']} | {t['max_drawdown']} | {t['fills']} |"
                )
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in body["limitations"]]
    lines += ["", f"Growth policy: {body['growth_policy']}.", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------- paper
def paper_body(
    *,
    product_id: str | None,
    status: dict[str, Any],
    orders: list[dict[str, Any]],
    as_of: int | None,
) -> dict[str, Any]:
    return {
        "report_version": REPORT_VERSION,
        "kind": "PAPER_DAILY",
        "label": "PAPER",
        "banner": banner("PAPER"),
        "engine_version": ENGINE_VERSION,
        "product": product_id,
        "as_of_candle": as_of,
        "as_of_iso": iso(as_of) if as_of else None,
        "status": status,
        "recent_orders": orders,
        "limitations": list(LIMITATIONS),
        "growth_policy": "disabled",
    }


def paper_markdown(body: dict[str, Any], sha: str) -> str:
    s = body["status"]
    lines = [
        f"# Paper trading report: {body['product'] or 'no pair'}",
        "",
        f"**{body['banner']}**",
        "",
        "Local paper exchange only: no order was sent to any exchange.",
        "",
        f"- Report SHA-256 (of the JSON body): `{sha}`",
        f"- As of candle: {body['as_of_iso'] or 'none'}",
        f"- Session: {s['state']} (phase {s['phase']}); data stale: {s['data_stale']}",
        "",
        "## Ledger",
        f"- Cash: {s['cash']} USDC; reserved for open buys: {s['reserved']}; free cash: {s['free_cash']} (protected reserve floor 15)",
        f"- Inventory: {s['inventory']} base at cost {s['cost_basis']} USDC; deployed {s['deployed']} of 35 USDC cap",
        f"- Orders: {s['orders_by_state']}; fills {s['fills']}; fees {s['fees_paid']} USDC",
        "",
        "## Recent orders",
        "",
        "| Seq | Cell | Side | Price | Qty | Filled | State |",
        "|---|---|---|---|---|---|---|",
    ]
    for o in body["recent_orders"]:
        lines.append(
            f"| {o['seq']} | {o['cell']} | {o['side']} | {o['price']} | {o['qty']} | {o['filled']} | {o['state']} |"
        )
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in body["limitations"]] + [""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------- data quality
def quality_body(
    *,
    product_id: str,
    runs: list[dict[str, Any]],
    events: dict[str, int],
    candles: int,
    cursor: int | None,
) -> dict[str, Any]:
    return {
        "report_version": REPORT_VERSION,
        "kind": "DATA_QUALITY",
        "label": "BACKTEST",
        "banner": banner("BACKTEST"),
        "engine_version": ENGINE_VERSION,
        "product": product_id,
        "stored_candles": candles,
        "cursor_iso": iso(cursor) if cursor else None,
        "recent_runs": runs,
        "event_totals": dict(sorted(events.items())),
        "limitations": [
            "Gaps are reported, never filled: a missing interval means no candle was accepted for it."
        ],
    }


def quality_markdown(body: dict[str, Any], sha: str) -> str:
    lines = [
        f"# Data-quality report: {body['product']}",
        "",
        f"**{body['banner']}**",
        "",
        f"- Report SHA-256 (of the JSON body): `{sha}`",
        f"- Stored candles: {body['stored_candles']}; ingestion cursor: {body['cursor_iso'] or 'none'}",
        "",
        "## Event totals (all runs, all products)",
    ]
    lines += [f"- {k}: {v}" for k, v in body["event_totals"].items()] or ["- none"]
    lines += [
        "",
        "## Recent import runs",
        "",
        "| Run | Status | Inserted | Duplicates | Conflicts | Gaps | Missing | Retries |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in body["recent_runs"]:
        lines.append(
            f"| {r['id']} | {r['status']} | {r['inserted']} | {r['duplicates']} | {r['conflicts']} | {r['gaps']} | {r['missing']} | {r['retries']} |"
        )
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in body["limitations"]] + [""]
    return "\n".join(lines)
