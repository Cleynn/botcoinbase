"""Process entrypoint: validate configuration (fail closed), configure logging, serve."""

from __future__ import annotations

import logging
from typing import Any

import uvicorn

from app import constants
from app.api.app import create_app
from app.config import load_settings
from app.logging import setup_logging


def _start_metrics_listener(app: Any, settings: Any) -> Any:
    """Start the internal metrics listener. A failure is reported, never fatal to the web app."""
    if not settings.monitoring.enabled:
        return None
    server = app.state.monitoring.make_server()
    try:
        server.start()
    except OSError:
        app.state.monitoring.service.listener_failed = True
        logging.getLogger("app").error("metrics listener could not start; continuing without it")
        return None
    return server


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    app = create_app(settings)
    listener = _start_metrics_listener(app, settings)
    try:
        _serve(app)
    finally:
        if listener is not None:
            listener.stop()


def _serve(app: Any) -> None:
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104  container-internal; the port is never host-published
        port=constants.INTERNAL_APP_PORT,
        server_header=False,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
