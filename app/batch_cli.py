"""Host CLI for market data, backtests, paper trading, review packages and proposals.

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
from app.auth.audit import AuditWriter
from app.backtest.service import BacktestError, BacktestService
from app.config import ConfigError, Settings, load_settings
from app.domain.models import Clock, SystemClock
from app.exchange.factory import build_gateway, build_reader
from app.exchange.gateway import ExecutionGateway
from app.exchange.reader import ExchangeReader, RecordingReader, RetryingReader
from app.market.ingest import GRANULARITY, STEP, Importer
from app.market.snapshots import SnapshotBuilder, SnapshotError
from app.pairs.runner import RunnerError
from app.paper.exchange import PaperError, PaperExchange
from app.proposals.validator import ProposalValidator
from app.review.builder import ReviewBuilder
from app.safety.commands import CommandRunner
from app.safety.control import ControlService
from app.safety.host_control import HostControl
from app.safety.monitor import SafetyMonitor
from app.safety.reconciler import Reconciler
from app.safety.recovery import RecoveryService
from app.safety.retry import RetryPolicy
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

    review = top.add_parser("review").add_subparsers(dest="command", required=True)
    review.add_parser("build", help="build every requested review package")
    verify = review.add_parser("verify", help="verify READY packages on disk")
    which = verify.add_mutually_exclusive_group(required=True)
    which.add_argument("--id", type=UUID)
    which.add_argument("--all", action="store_true")
    review.add_parser("cleanup", help="expire packages past retention and remove their files")
    review.add_parser("list", help="list review packages")

    proposal = top.add_parser("proposal").add_subparsers(dest="command", required=True)
    proposal.add_parser("validate", help="validate every imported proposal")
    proposal.add_parser("cleanup", help="remove old proposal content and orphan files")
    proposal.add_parser("list", help="list proposals")

    safety = top.add_parser("safety").add_subparsers(dest="command", required=True)
    safety.add_parser("status", help="bot control state, reconciliation and the live gate")
    safety.add_parser("recover", help="startup recovery: reconcile before any action")
    safety.add_parser("reconcile", help="run one REST reconciliation")
    safety.add_parser("monitor", help="evaluate breaker signals and open the breaker if needed")
    safety.add_parser("commands", help="run the queued cancel command for known bot orders")
    safety.add_parser("pause", help="pause the bot")
    kill = safety.add_parser("kill", help="activate the kill switch (never sells)")
    kill.add_argument("--confirm", required=True, help="the exact phrase ACTIVATE KILL SWITCH")
    release = safety.add_parser("kill-release", help="release the kill switch (host only)")
    release.add_argument("--confirm", required=True, help="the exact phrase RELEASE KILL SWITCH")
    safety.add_parser("prune", help="delete exchange call records older than seven days")

    from app.live.cli import add_parser as add_live_parser

    add_live_parser(top)

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
    reader: ExchangeReader | None = None,
    gateway: ExecutionGateway | None = None,
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
        if (
            args.group == "market"
            and args.command == "import"
            or args.group == "live"
            and args.command == "run"
        ) and client is None:
            owned = client = CoinbasePublicClient(settings.exchange)
        try:
            return _dispatch(args, settings, storage, clock, client, sleep, out, reader, gateway)
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
    reader: ExchangeReader | None = None,
    gateway: ExecutionGateway | None = None,
) -> int:
    if args.group == "live":
        from app.live.cli import run_live

        return run_live(args, settings, storage, clock, out, reader, gateway, client, sleep)
    if args.group == "safety":
        return _safety(args, settings, storage, clock, out, reader, gateway)
    if args.group == "market":
        return _market(args, settings, storage, clock, client, sleep, out)
    if args.group == "backtest":
        service = BacktestService(storage=storage, clock=clock, settings=settings)
        report, created = service.run(args.snapshot, walk=args.walk_forward, levels=args.levels)
        out(
            f"report {report.id} {report.kind} sha256={report.sha256} {'created' if created else 'already existed'}"
        )
        return 0
    if args.group == "review":
        return _review(args, settings, storage, clock, out)
    if args.group == "proposal":
        return _proposal(args, settings, storage, clock, out)
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


def _review(
    args: argparse.Namespace,
    settings: Settings,
    storage: Storage,
    clock: Clock,
    out: Callable[[str], None],
) -> int:
    builder = ReviewBuilder(storage=storage, clock=clock, settings=settings)
    if args.command == "build":
        results = builder.build_pending()
        for r in results:
            out(f"package {r.package_id} {r.state}{' ' + r.code if r.code else ''}")
        out(f"{len(results)} package(s) processed")
        return 0 if all(r.state == "READY" for r in results) else 1
    if args.command == "verify":
        checked = builder.verify_all() if args.all else {args.id: builder.verify(args.id)}
        for pid, problems in checked.items():
            out(f"package {pid} {'OK' if not problems else 'CORRUPT ' + ','.join(problems)}")
        return 0 if all(not p for p in checked.values()) else 1
    if args.command == "cleanup":
        c = builder.cleanup()
        out(f"expired={c.expired} content_removed={c.removed} orphan_files_removed={c.orphans}")
        return 0
    with storage.tx() as repos:
        for row in repos.review.recent(50):
            out(f"{row.id} {row.state} {row.period_start}..{row.period_end} {','.join(row.scope)}")
    return 0


def _proposal(
    args: argparse.Namespace,
    settings: Settings,
    storage: Storage,
    clock: Clock,
    out: Callable[[str], None],
) -> int:
    validator = ProposalValidator(storage=storage, clock=clock, settings=settings)
    if args.command == "validate":
        results = validator.run()
        for r in results:
            codes = ",".join(sorted({x.split(":")[0] for x in r.rules})[:6])
            out(f"proposal {r.proposal_id} {r.state}{' ' + codes if codes else ''}")
        out(f"{len(results)} proposal(s) processed")
        return 0
    if args.command == "cleanup":
        c = validator.cleanup()
        out(f"content_removed={c.purged} orphan_files_removed={c.orphans}")
        return 0
    with storage.tx() as repos:
        for row in repos.proposals.recent(50):
            out(f"{row.id} {row.state} {row.category or '-'}")
    return 0


def _reconciler(
    settings: Settings,
    storage: Storage,
    clock: Clock,
    reader: ExchangeReader | None,
) -> Reconciler | None:
    reader = reader or build_reader(settings)
    if reader is None:
        return None
    venue = reader.venue if reader.venue in ("PAPER", "FAKE", "COINBASE") else "FAKE"

    def sink(operation: str, ok: bool, code: str | None) -> None:
        with storage.tx() as repos:
            repos.safety.add_api_event(venue, operation, ok, code, clock.now())

    wrapped = RetryingReader(
        RecordingReader(reader, sink), RetryPolicy.from_settings(settings.safety)
    )
    return Reconciler(storage=storage, clock=clock, settings=settings, reader=wrapped, venue=venue)


def _safety(
    args: argparse.Namespace,
    settings: Settings,
    storage: Storage,
    clock: Clock,
    out: Callable[[str], None],
    reader: ExchangeReader | None,
    gateway: ExecutionGateway | None,
) -> int:
    host = HostControl(storage=storage, clock=clock, settings=settings)
    if args.command == "status":
        view = ControlService(
            storage=storage,
            clock=clock,
            settings=settings,
            audit=AuditWriter(clock),
            consume_reauth=lambda _r, _c: False,
            reauth_active=lambda _c: False,
        ).overview()
        c = view.control
        out(f"LIVE TRADING: BLOCKED ({len(view.gate.reasons)} unmet conditions)")
        out(
            f"bot={c.bot_state} kill_switch={c.kill_switch} breaker={c.breaker_state} "
            f"recovery={c.recovery_state}"
        )
        run = view.run
        out(
            "reconciliation: none"
            if run is None
            else f"reconciliation: {run.outcome} at {run.finished_at.isoformat()} ({run.trigger})"
        )
        out(
            "attempts: " + (", ".join(f"{k}={v}" for k, v in sorted(view.counts.items())) or "none")
        )
        out("resume blockers: " + (", ".join(view.resume_blockers) or "none"))
        return 0
    if args.command in ("recover", "reconcile"):
        reconciler = _reconciler(settings, storage, clock, reader)
        if args.command == "reconcile":
            if reconciler is None:
                out(
                    "NO_EXCHANGE_READER: no exchange reader exists in this deployment; nothing to do"
                )
                return 1
            run_result = reconciler.run("MANUAL")
            out(
                f"reconciliation {run_result.outcome} "
                f"findings={','.join(run_result.codes) or 'none'}"
            )
            return 0 if run_result.outcome == "OK" else 1
        recovery = RecoveryService(
            storage=storage, clock=clock, settings=settings, reconciler=reconciler
        ).run()
        out(
            f"recovery complete={recovery.complete} run={recovery.run_outcome} "
            f"blockers={','.join(recovery.blockers) or 'none'}; the bot stays PAUSED"
        )
        return 0 if recovery.complete else 1
    if args.command == "monitor":
        tick = SafetyMonitor(storage=storage, clock=clock, settings=settings, host=host).tick()
        out(f"breaker {'OPENED: ' + tick.tripped if tick.tripped else 'unchanged'}")
        return 0
    if args.command == "commands":
        exchange = PaperExchange(storage=storage, clock=clock, settings=settings)
        gateways = {}
        active_gateway = gateway or build_gateway(settings)
        if active_gateway is not None:
            gateways[active_gateway.venue] = active_gateway
        outcome = CommandRunner(
            storage=storage,
            clock=clock,
            settings=settings,
            gateways=gateways,
            paper_cancel=exchange.cancel_for_safety,
        ).run_pending()
        out(
            "no pending command" if outcome is None else f"command {outcome.state} {outcome.counts}"
        )
        return 0 if outcome is None or outcome.state == "DONE" else 1
    if args.command == "pause":
        out("paused" if host.pause() else "already paused")
        return 0
    if args.command == "kill":
        killed = host.activate_kill(args.confirm)
        out(f"kill switch: {killed.kind}")
        return 0 if killed.kind == "ok" else 1
    if args.command == "kill-release":
        released = host.release_kill(args.confirm)
        out(
            f"kill switch release: {released.kind}; the bot stays PAUSED and recovery must be redone"
        )
        return 0 if released.kind == "ok" else 1
    with storage.tx() as repos:
        out(f"pruned {repos.safety.prune_api_events()} call records")
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
