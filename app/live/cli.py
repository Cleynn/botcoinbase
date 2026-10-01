"""`live` commands (host only, run as td_ctl from the batch container).

    live status                          mode, arming, configuration, grids
    live check                           read-only: key permissions and USDC balance (no order)
    live baseline --confirm PHRASE       record today's exchange balances as the starting point
    live arm --hours N --confirm PHRASE  arm live trading for at most 24 hours
    live disarm                          revoke the arming
    live config [--pairs N --levels N --per-grid X --invested X --reserve X --per-order X]
    live run [--interval S] [--max-ticks N]   recover, then run the grid runner

Secrets are never printed: only counts, flags and a six-character key hint.
"""

from __future__ import annotations

import argparse
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Final

import psycopg

from app.adapters import coinbase_parse as parse
from app.adapters.coinbase_public import CoinbasePublicClient
from app.capital.trading import TradingConfig, problems
from app.config import Settings
from app.domain.models import Clock
from app.exchange.credentials import CredentialError, key_file_from_env, load_credentials
from app.exchange.errors import ExchangeError
from app.exchange.gateway import ExecutionGateway
from app.exchange.reader import ExchangeReader, RecordingReader, RetryingReader
from app.live.runner import LiveRunner
from app.market.ingest import Importer
from app.safety.commands import CommandRunner
from app.safety.context import BookFacts
from app.safety.host_control import HostControl
from app.safety.monitor import SafetyMonitor
from app.safety.pipeline import OrderPipeline
from app.safety.reconciler import Reconciler
from app.safety.recovery import RecoveryService
from app.safety.retry import RetryPolicy
from app.storage.database import Storage

BASELINE_PHRASE: Final = "RECORD LIVE BASELINE"
ARM_PHRASE: Final = "ARM LIVE TRADING"
VENUE: Final = "COINBASE"
ARMED_BY: Final = "host_cli"


def add_parser(top: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    live = top.add_parser("live").add_subparsers(dest="command", required=True)
    live.add_parser("status")
    live.add_parser("check")
    base = live.add_parser("baseline")
    base.add_argument("--confirm", required=True, help=f"the exact phrase {BASELINE_PHRASE}")
    arm = live.add_parser("arm")
    arm.add_argument("--hours", type=int, default=8)
    arm.add_argument("--confirm", required=True, help=f"the exact phrase {ARM_PHRASE}")
    live.add_parser("disarm")
    cfg = live.add_parser("config")
    cfg.add_argument("--pairs", type=int)
    cfg.add_argument("--levels", type=int)
    cfg.add_argument("--per-grid")
    cfg.add_argument("--invested")
    cfg.add_argument("--reserve")
    cfg.add_argument("--per-order")
    run = live.add_parser("run")
    run.add_argument("--interval", type=int, default=30)
    run.add_argument("--max-ticks", type=int, default=0, help="0 = run until stopped")


def _amount(text: str | None, current: Decimal) -> Decimal:
    if text is None:
        return current
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("AMOUNTS") from exc
    if not value.is_finite():
        raise ValueError("AMOUNTS")
    return value


def _line(cfg: TradingConfig) -> str:
    return (
        f"pairs={cfg.max_pairs} levels={cfg.levels_per_grid} per_grid={cfg.quote_per_grid} "
        f"invested_cap={cfg.invested_cap} reserve={cfg.reserve} per_order={cfg.per_order_cap} "
        f"(version {cfg.version})"
    )


def run_live(
    args: argparse.Namespace,
    settings: Settings,
    storage: Storage,
    clock: Clock,
    out: Callable[[str], None],
    reader: ExchangeReader | None,
    gateway: ExecutionGateway | None,
    client: CoinbasePublicClient | None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    from app.exchange.factory import build_gateway, build_reader

    cmd = args.command
    if cmd == "status":
        now = clock.now()
        with storage.tx() as repos:
            mode, _ = repos.safety.trading_state()
            cfg = repos.safety.trading_config("LIVE")
            armed = repos.safety.live_armed(now)
            control = repos.safety.control()
            grids = repos.safety.live_grids()
        out(f"mode={mode} live_armed={armed} bot={control.bot_state} kill={control.kill_switch}")
        out("LIVE configuration: " + _line(cfg))
        bad = problems(cfg)
        if bad:
            out("configuration problems: " + ",".join(bad))
        out(f"active grids: {len(grids)}")
        for g in grids:
            out(
                f"  {g.product_id} levels={g.levels} band={g.lower}..{g.upper} "
                f"commitment={g.commitment}"
            )
        return 0
    if cmd == "config":
        with storage.tx() as repos:
            cur = repos.safety.trading_config("LIVE", for_update=True)
            changed = any(
                v is not None
                for v in (
                    args.pairs,
                    args.levels,
                    args.per_grid,
                    args.invested,
                    args.reserve,
                    args.per_order,
                )
            )
            if not changed:
                out(_line(cur))
                return 0
            try:
                new = TradingConfig(
                    "LIVE",
                    args.pairs if args.pairs is not None else cur.max_pairs,
                    args.levels if args.levels is not None else cur.levels_per_grid,
                    _amount(args.per_grid, cur.quote_per_grid),
                    _amount(args.invested, cur.invested_cap),
                    _amount(args.reserve, cur.reserve),
                    _amount(args.per_order, cur.per_order_cap),
                    cur.version,
                )
            except ValueError:
                out("refused: AMOUNTS")
                return 1
            bad = problems(new)
            if bad:
                out("refused: " + ",".join(bad))
                return 1
            try:
                ok = repos.safety.update_trading_config(new, clock.now())
            except psycopg.errors.IntegrityConstraintViolation:
                out("refused: the database only accepts this while the bot is PAUSED")
                return 1
        out(("updated: " if ok else "conflict, nothing changed: ") + _line(new))
        return 0 if ok else 1
    if cmd == "disarm":
        with storage.tx() as repos:
            n = repos.safety.revoke_arming(clock.now())
        out(f"revoked {n} arming(s)")
        return 0

    reader = reader or build_reader(settings)
    if reader is None:
        out("NO_CREDENTIALS: set TD_COINBASE_KEY_FILE (see docs/vps-deployment.md, step 7)")
        return 1
    if cmd == "check":
        perms = reader.key_permissions()
        out(f"key: view={perms.can_view} trade={perms.can_trade} transfer={perms.can_transfer}")
        if perms.can_transfer:
            out("WARNING: the key can transfer funds. Create a key without Transfer permission.")
        accounts = reader.list_accounts()
        usdc = [a for a in accounts if a.currency == "USDC"]
        if len(usdc) == 1:
            out(f"USDC available={usdc[0].available} hold={usdc[0].hold}")
        else:
            out(f"USDC account problem: found {len(usdc)}")
        held = [a.currency for a in accounts if a.currency != "USDC" and (a.available + a.hold) > 0]
        out(f"other currencies with a balance: {len(held)}")
        return 0 if perms.can_view and not perms.can_transfer and len(usdc) == 1 else 1
    if cmd == "baseline":
        if args.confirm != BASELINE_PHRASE:
            out("refused: PHRASE_MISMATCH")
            return 1
        accounts = reader.list_accounts()
        if not any(a.currency == "USDC" for a in accounts):
            out("refused: NO_QUOTE_ACCOUNT")
            return 1
        try:
            with storage.tx() as repos:
                for a in accounts:
                    if a.currency == "USDC" or (a.available + a.hold) > 0:
                        repos.safety.add_baseline(
                            VENUE, a.currency, a.available + a.hold, clock.now()
                        )
        except psycopg.errors.IntegrityError:
            out("refused: BASELINE_EXISTS (a baseline is recorded once)")
            return 1
        out("baseline recorded for COINBASE")
        return 0
    if cmd == "arm":
        if args.confirm != ARM_PHRASE:
            out("refused: PHRASE_MISMATCH")
            return 1
        if not 1 <= args.hours <= 24:
            out("refused: HOURS_OUT_OF_RANGE (1 to 24)")
            return 1
        perms = reader.key_permissions()
        if not perms.can_trade or perms.can_transfer or not perms.can_view:
            out("refused: KEY_PERMISSIONS (needs view and trade, never transfer)")
            return 1
        hint = "...000000"
        try:
            path = key_file_from_env()
            if path:
                hint = load_credentials(path).key_hint
        except CredentialError as exc:
            out(f"refused: {exc.code}")
            return 1
        now = clock.now()
        try:
            with storage.tx() as repos:
                repos.safety.add_arming(
                    uuid.uuid4(),
                    armed_at=now,
                    expires_at=now + timedelta(hours=args.hours),
                    armed_by=ARMED_BY,
                    key_hint=hint,
                    checks={"can_trade": True, "can_transfer": False},
                )
        except psycopg.errors.IntegrityConstraintViolation:
            out(
                "refused: the database needs recovery complete, kill switch off, breaker closed, "
                "a COINBASE baseline and a current reconciliation, and no active arming"
            )
            return 1
        out(f"LIVE TRADING ARMED until {(now + timedelta(hours=args.hours)).isoformat()}")
        return 0
    return _run(args, settings, storage, clock, out, reader, gateway, client, sleep, build_gateway)


def _run(
    args: argparse.Namespace,
    settings: Settings,
    storage: Storage,
    clock: Clock,
    out: Callable[[str], None],
    reader: ExchangeReader,
    gateway: ExecutionGateway | None,
    client: CoinbasePublicClient | None,
    sleep: Callable[[float], None],
    build_gateway: Callable[[Settings], ExecutionGateway | None],
) -> int:
    gateway = gateway or build_gateway(settings)
    if gateway is None:
        out("NO_CREDENTIALS: no order gateway")
        return 1
    venue = gateway.venue

    def sink(operation: str, ok: bool, code: str | None) -> None:
        with storage.tx() as repos:
            repos.safety.add_api_event(venue, operation, ok, code, clock.now())

    wrapped = RetryingReader(
        RecordingReader(reader, sink), RetryPolicy.from_settings(settings.safety)
    )
    reconciler = Reconciler(
        storage=storage, clock=clock, settings=settings, reader=wrapped, venue=venue
    )
    boot_id = str(uuid.uuid4())
    recovery = RecoveryService(
        storage=storage, clock=clock, settings=settings, reconciler=reconciler
    ).run(boot_id)
    out(
        f"recovery complete={recovery.complete} run={recovery.run_outcome}; the bot is PAUSED. "
        "Resume it on the Bot page, then arm live trading (live arm)."
    )
    pipeline = OrderPipeline(
        storage=storage,
        clock=clock,
        settings=settings,
        gateway=gateway,
        boot_id=boot_id,
        reconcile=reconciler.run,
        list_accounts=wrapped.list_accounts,
    )

    def book(product_id: str) -> BookFacts | None:
        if client is None:
            return None
        try:
            ob = parse.parse_product_book(client.get_product_book(product_id, 5))
        except Exception:  # noqa: BLE001
            return None
        if not ob.bids or not ob.asks or ob.time is None:
            return None
        bid, ask = ob.bids[0].price, ob.asks[0].price
        mid = (bid + ask) / 2
        if mid <= 0 or ask < bid:
            return None
        age = int((clock.now() - ob.time).total_seconds())
        return BookFacts((ask - bid) / mid * 10000, max(age, 0))

    importer = (
        Importer(storage=storage, clock=clock, settings=settings, client=client, sleep=sleep)
        if client is not None
        else None
    )

    def refresh(product_id: str) -> None:
        if importer is not None:
            importer.run(product_id, commit=True)

    runner = LiveRunner(
        storage=storage,
        clock=clock,
        settings=settings,
        pipeline=pipeline,
        gateway=gateway,
        list_accounts=wrapped.list_accounts,
        reconcile=reconciler.run,
        refresh=refresh,
        book=book,
    )
    host = HostControl(storage=storage, clock=clock, settings=settings)
    monitor = SafetyMonitor(storage=storage, clock=clock, settings=settings, host=host)
    commands = CommandRunner(
        storage=storage, clock=clock, settings=settings, gateways={venue: gateway}
    )
    ticks = 0
    try:
        while True:
            ticks += 1
            monitor.tick()
            commands.run_pending()  # a kill switch or cancel request is served first
            try:
                result = runner.tick()
            except ExchangeError as exc:
                out(f"tick {ticks}: exchange error {exc.code}")
            else:
                if not result.ran:
                    out(f"tick {ticks}: idle ({result.reason})")
                for p in result.pairs:
                    out(
                        f"tick {ticks}: {p.product_id} {p.action} placed={p.placed} "
                        f"cancelled={p.cancelled} {','.join(p.notes)}"
                    )
            if args.max_ticks and ticks >= args.max_ticks:
                return 0
            sleep(args.interval)
    except KeyboardInterrupt:
        out("stopped; open orders stay on the exchange until cancelled (kill switch or Bot page)")
        return 0
