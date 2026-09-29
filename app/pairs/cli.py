"""Host CLI for pair discovery and validation: `python -m app.cli pairs <command>`.

Runs as the `td_ctl` role from the `ctl` container/profile, never in the web process. Every command
reads public Coinbase market data only; none activates a pair, places or cancels an order, or
touches an account. Output uses fixed vocabulary and pattern-checked product ids only.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from uuid import UUID

from app.adapters.coinbase_public import CoinbasePublicClient
from app.config import ConfigError, Settings, load_settings
from app.domain.models import Clock, SystemClock
from app.pairs.runner import PairRunner, RunnerError
from app.storage.database import SchemaError, Storage, StorageUnavailable

HOST_ROLE = "td_ctl"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradingdots pairs")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="refresh the USDC spot product catalogue from public data")
    seed = sub.add_parser("seed", help="propose the configured watchlist as candidates only")
    seed.add_argument("--queue-validation", action="store_true", help="also queue validation")
    validate = sub.add_parser(
        "validate", help="expire stale eligibility, then validate queued pairs"
    )
    validate.add_argument("--pair", type=UUID, default=None, help="validate only this pair")
    sub.add_parser("list", help="show pairs and their states (read-only)")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    storage: Storage | None = None,
    client: CoinbasePublicClient | None = None,
    clock: Clock | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = settings or load_settings()
        if settings.database.user != HOST_ROLE:
            print(
                f"error: run this as the host CLI role ({HOST_ROLE}); the web role cannot write "
                "products or validation evidence",
                file=sys.stderr,
            )
            return 2
        storage = storage or Storage(settings.database)
        storage.check_schema()
        owned = client is None
        client = client or CoinbasePublicClient(settings.exchange)
        try:
            runner = PairRunner(
                storage=storage, clock=clock or SystemClock(), settings=settings, client=client
            )
            return _dispatch(args, runner, storage)
        finally:
            if owned:
                client.close()
    except (ConfigError, SchemaError, StorageUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RunnerError as exc:
        print(f"error: {exc.code}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace, runner: PairRunner, storage: Storage) -> int:
    if args.command == "discover":
        s = runner.discover()
        print(
            f"discovery: seen={s.seen} usdc_spot={s.candidates} stored={s.stored} "
            f"new={s.new} changed={s.changed} capped={s.capped}"
        )
        return 0
    if args.command == "seed":
        for result in runner.seed_watchlist(queue_validation=args.queue_validation):
            print(f"{result.product_id}: {result.outcome}")
        return 0
    if args.command == "validate":
        expired = runner.expire_eligibility()
        print(f"expired eligibility: {expired}")
        reports = runner.validate_pending(only=args.pair)
        for report in reports:
            detail = ""
            if report.failed:
                detail += f" failed={','.join(report.failed)}"
            if report.inconclusive:
                detail += f" inconclusive={','.join(report.inconclusive)}"
            print(f"{report.product_id}: {report.result} ({report.outcome}){detail}")
        if not reports:
            print("nothing queued for validation")
        return 0
    with storage.tx() as repos:  # list
        pairs = repos.pairs.list_all()
    for pair in pairs:
        print(f"{pair.id} {pair.product_id} {pair.state.value} v{pair.version}")
    if not pairs:
        print("no pairs")
    return 0
