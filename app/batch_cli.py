"""Host CLI for market data, backtests and paper trading (`market`, `backtest`, `paper`).

Runs as `td_ctl` from the `batch` container, never in the web process. Importing is a dry run unless
`--commit` is given. Nothing here places an order anywhere: the only "exchange" is the local paper
database, and the only network access is read-only public market data through the egress proxy.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID

from app.adapters.coinbase_public import CoinbasePublicClient
from app.backtest.service import BacktestError, BacktestService
from app.config import ConfigError, Settings, load_settings
from app.domain.models import Clock, SystemClock
from app.market.ingest import GRANULARITY, STEP, Importer
from app.market.snapshots import SnapshotBuilder, SnapshotError
from app.pairs.runner import RunnerError
from app.paper.exchange import PaperError, PaperExchange
from app.storage.database import SchemaError, Storage, StorageUnavailable

HOST_ROLE = "td_ctl"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradingdots")
    top = parser.add_subparsers(dest="group", required=True)

    market = top.add_parser("market").add_subparsers(dest="command", required=True)
    imp = market.add_parser("import", help="import closed candles (DRY RUN unless --commit)")
    imp.add_argument("--product", required=True)
    imp.add_argument("--days", type=int, default=None)
    imp.add_argument("--commit", action="store_true", help="write candles (default: dry run)")
    snap = market.add_parser("snapshot", help="freeze ingested candles into a Parquet snapshot")
    snap.add_argument("--product", required=True)
    snap.add_argument("--days", type=int, default=None)
    market.add_parser("snapshots", help="list dataset snapshots")
    quality = market.add_parser("quality", help="store a data-quality report")
    quality.add_argument("--product", required=True)

    backtest = top.add_parser("backtest").add_subparsers(dest="command", required=True)
    run = backtest.add_parser("run", help="backtest a snapshot (operator and stress fees)")
    run.add_argument("--snapshot", type=UUID, required=True)
    run.add_argument("--walk-forward", action="store_true")
    run.add_argument("--levels", type=int, default=None)

    paper = top.add_parser("paper").add_subparsers(dest="command", required=True)
    paper.add_parser("status")
    start = paper.add_parser("start")
    start.add_argument("--acknowledge-halt", action="store_true")
    paper.add_parser("stop")
    paper.add_parser("step")
    paper.add_parser("report")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    storage: Storage | None = None,
    client: CoinbasePublicClient | None = None,
    clock: Clock | None = None,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = settings or load_settings()
        if settings.database.user != HOST_ROLE:
            print(f"error: run this as the host CLI role ({HOST_ROLE})", file=sys.stderr)
            return 2
        storage = storage or Storage(settings.database)
        storage.check_schema()
        clock = clock or SystemClock()
        owned: CoinbasePublicClient | None = None
        if args.group == "market" and args.command == "import" and client is None:
            owned = client = CoinbasePublicClient(settings.exchange)
        try:
            return _dispatch(args, settings, storage, clock, client, sleep, out)
        finally:
            if owned is not None:
                owned.close()
    except (ConfigError, SchemaError, StorageUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (RunnerError, PaperError, BacktestError, SnapshotError) as exc:
        print(f"error: {exc.code}", file=sys.stderr)
        return 1


def _dispatch(
    args: argparse.Namespace,
    settings: Settings,
    storage: Storage,
    clock: Clock,
    client: CoinbasePublicClient | None,
    sleep: Callable[[float], None],
    out: Callable[[str], None],
) -> int:
    if args.group == "market":
        return _market(args, settings, storage, clock, client, sleep, out)
    if args.group == "backtest":
        service = BacktestService(storage=storage, clock=clock, settings=settings)
        report, created = service.run(args.snapshot, walk=args.walk_forward, levels=args.levels)
        out(
            f"report {report.id} {report.kind} sha256={report.sha256} {'created' if created else 'already existed'}"
        )
        return 0
    exchange = PaperExchange(storage=storage, clock=clock, settings=settings)
    if args.command == "status":
        s = exchange.status()
        out(f"MODE: PAPER | session={s.state} phase={s.phase} product={s.product_id}")
        out(
            f"cash={s.cash} reserved={s.reserved} free={s.free_cash} inventory={s.inventory} cost={s.cost_basis} deployed={s.deployed}"
        )
        out(
            f"orders={dict(sorted(s.orders_by_state.items()))} fills={s.fills} fees={s.fees_paid} data_stale={s.data_stale}"
        )
    elif args.command == "start":
        exchange.start(acknowledge_halt=args.acknowledge_halt)
        out("paper session RUNNING (local paper exchange only; nothing is sent to any exchange)")
    elif args.command == "stop":
        out(
            f"paper session PAUSED; cancelled {exchange.stop()} open paper orders; no inventory was sold"
        )
    elif args.command == "step":
        r = exchange.step()
        out(
            f"candles={r.candles_processed} placed={r.placed} fills={r.fills} cancelled={r.cancelled} phase={r.phase}"
        )
    else:
        report, created = BacktestService(
            storage=storage, clock=clock, settings=settings
        ).paper_report(exchange)
        out(
            f"report {report.id} {report.kind} sha256={report.sha256} {'created' if created else 'already existed'}"
        )
    return 0


def _market(
    args: argparse.Namespace,
    settings: Settings,
    storage: Storage,
    clock: Clock,
    client: CoinbasePublicClient | None,
    sleep: Callable[[float], None],
    out: Callable[[str], None],
) -> int:
    if args.command == "import":
        if client is None:
            raise RunnerError("NO_CLIENT")
        result = Importer(
            storage=storage, clock=clock, settings=settings, client=client, sleep=sleep
        ).run(args.product, commit=args.commit, days=args.days)
        out(
            f"{'COMMIT' if args.commit else 'DRY RUN (nothing written; pass --commit to write)'}: {result.status}"
        )
        out(
            f"window {_iso(result.window_start)} .. {_iso(result.window_end)} requests={result.requests} retries={result.retries}"
        )
        out(
            f"fetched={result.fetched} to_insert={result.to_insert} inserted={result.inserted} duplicates={result.duplicates} "
            f"conflicts={result.conflicts} malformed={result.malformed} gaps={result.gaps} missing={result.missing}"
        )
        for event in result.events[:20]:
            out(f"  {event.severity} {event.code} x{event.count} {event.detail}")
        return 0 if result.status in ("COMPLETE", "NOTHING_TO_DO") else 1
    if args.command == "snapshot":
        with storage.tx() as repos:
            product = repos.products.get_by_product_id(args.product)
            if product is None:
                raise RunnerError("UNKNOWN_PRODUCT")
            cursor = repos.market.cursor(product.id, GRANULARITY)
        if cursor is None:
            raise SnapshotError("RANGE_NOT_INGESTED")
        days = settings.data.history_days if args.days is None else args.days
        if not 1 <= days <= 365:
            raise RunnerError("BAD_DAYS")
        row, created = SnapshotBuilder(storage=storage, clock=clock, settings=settings).build(
            args.product, start=(cursor - days * 86400) // STEP * STEP, end=cursor
        )
        out(
            f"snapshot {row.id} {'created' if created else 'already existed'} candles={row.candle_count} "
            f"gaps={row.gap_count} sha256={row.file_sha256}"
        )
        return 0
    if args.command == "snapshots":
        with storage.tx() as repos:
            for row in repos.results.snapshots():
                out(
                    f"{row.id} candles={row.candle_count} {_iso(row.range_start)}..{_iso(row.range_end)} sha256={row.file_sha256[:16]}"
                )
        return 0
    report, created = BacktestService(
        storage=storage, clock=clock, settings=settings
    ).quality_report(args.product)
    out(
        f"report {report.id} {report.kind} sha256={report.sha256} {'created' if created else 'already existed'}"
    )
    return 0


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M")
