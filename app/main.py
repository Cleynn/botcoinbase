"""Process entrypoint: validate configuration (fail closed), configure logging, serve."""

from __future__ import annotations

import uvicorn

from app import constants
from app.api.app import create_app
from app.config import load_settings
from app.logging import setup_logging


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",  # noqa: S104  container-internal; the port is never host-published
        port=constants.INTERNAL_APP_PORT,
        server_header=False,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
