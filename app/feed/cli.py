"""`feed` host commands: `feed once` reads the Coinbase account one time, `feed run` keeps reading.

Only GET requests are sent (the authenticated reader has no other verb). Run as `td_ctl` from the
`feed` container: docker compose --profile discovery up -d feed
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

import psycopg

from app.config import Settings
from app.domain.models import Clock
from app.exchange.factory import build_reader
from app.exchange.reader import ExchangeReader
from app.feed.service import ExchangeFeed, FeedOutcome
from app.storage.database import Storage, StorageUnavailable

MIN_INTERVAL = 15  # seconds; four GET requests per refresh
DEFAULT_INTERVAL = 60


def _interval(text: str) -> int:
    value = int(text)
    if not MIN_INTERVAL <= value <= 3600:
        raise argparse.ArgumentTypeError(f"must be between {MIN_INTERVAL} and 3600 seconds")
    return value


def add_parser(top: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    feed = top.add_parser("feed").add_subparsers(dest="command", required=True)
    feed.add_parser("once", help="read the Coinbase account once (GET only) and store it")
    run = feed.add_parser("run", help="keep the stored Coinbase account data fresh (GET only)")
    run.add_argument("--interval", type=_interval, default=DEFAULT_INTERVAL)
    run.add_argument("--max-runs", type=int, default=0, help=argparse.SUPPRESS)


def _line(outcome: FeedOutcome) -> str:
    if not outcome.ok:
        return f"feed: FAILED {outcome.error_code} (the previous data is kept)"
    return f"feed: OK balances={outcome.balances} orders={outcome.orders} fills={outcome.fills}"


def run_feed(
    args: argparse.Namespace,
    settings: Settings,
    storage: Storage,
    clock: Clock,
    out: Callable[[str], None],
    reader: ExchangeReader | None,
    sleep: Callable[[float], None],
) -> int:
    reader = reader or build_reader(settings)
    if reader is None:
        out("NO_CREDENTIALS: set TD_COINBASE_KEY_FILE (see docs/vps-deployment.md, step 7)")
        return 1
    feed = ExchangeFeed(storage=storage, reader=reader, clock=clock)
    if args.command == "once":
        outcome = feed.refresh()
        out(_line(outcome))
        return 0 if outcome.ok else 1
    runs, last = 0, ""
    while True:
        try:
            line = _line(feed.refresh())
        except StorageUnavailable:
            line = "feed: FAILED DATABASE_UNAVAILABLE"
        except psycopg.Error as exc:  # e.g. a value the feed tables refuse: keep running
            line = f"feed: FAILED DATABASE_REFUSED ({type(exc).__name__}; the data is kept)"
        if line != last:  # say it once, not every minute
            out(line)
            last = line
        runs += 1
        if args.max_runs and runs >= args.max_runs:
            return 0
        sleep(args.interval)
