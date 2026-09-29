"""Command line: `validate-config` and `serve`. No trading, pair or bot commands exist."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from app import constants
from app.config import ConfigError, load_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradingdots")
    parser.add_argument("command", choices=["validate-config", "serve"])
    args = parser.parse_args(argv)

    if args.command == "serve":
        from app.main import main as serve

        serve()
        return 0

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
