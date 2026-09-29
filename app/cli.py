"""Command line: `validate-config`, `serve`, `pairs`, `market`, `backtest` and `paper`.

`pairs` and `market` read public Coinbase data only; `backtest` reads frozen snapshots; `paper`
uses the local paper database. No live trading, exchange order or bot-control command exists.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from app import constants
from app.config import ConfigError, load_settings


def main(argv: Sequence[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if args_in and args_in[0] == "pairs":
        from app.pairs.cli import main as pairs_main

        return pairs_main(args_in[1:])
    if args_in and args_in[0] in ("market", "backtest", "paper", "review", "proposal", "safety"):
        from app.batch_cli import main as batch_main

        return batch_main(args_in)

    parser = argparse.ArgumentParser(prog="tradingdots")
    parser.add_argument("command", choices=["validate-config", "serve", "pairs"])
    args = parser.parse_args(args_in)

    if args.command == "serve":
        from app.main import main as serve

        serve()
        return 0

    if args.command == "pairs":  # `pairs` without a subcommand
        from app.pairs.cli import main as pairs_main

        return pairs_main([])

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"CONFIG INVALID: {exc}", file=sys.stderr)
        return 1
    print(
        f"configuration valid: environment={settings.environment} mode={settings.mode} "
        f"LIVE TRADING: {constants.LIVE_TRADING_STATUS}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
