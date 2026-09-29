"""FastAPI application factory: server-rendered Jinja2 + HTMX shell. No data, no controls."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import constants
from app.api.health import router as health_router
from app.config import Settings, load_settings

logger = logging.getLogger("app")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

NAV_ITEMS = ("Bot", "Pairs", "Reports", "LLM Review", "Audit", "Security")

# Every value is "Unknown" or "Not available": Phase 1 has no bot, exchange or data source.
UNAVAILABLE_TILES: tuple[tuple[str, str], ...] = (
    ("Bot state", "Unknown"),
    ("Active pair", "Not available"),
    ("Protected reserve", "Not available"),
    ("Deployed capital", "Not available"),
    ("Deployment cap", "Not available"),
    ("Grid status", "Not available"),
    ("Data freshness", "Not available"),
    ("Product metadata freshness", "Not available"),
    ("Reconciliation status", "Unknown"),
    ("Circuit breaker state", "Unknown"),
    ("Kill switch state", "Unknown"),
    ("Recent risk decisions", "Not available"),
    ("Alerts", "Not available"),
    ("Latest reports", "Not available"),
    ("LLM Review Package state", "Not available"),
)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

_STATUS_PARTIAL = '{% from "base.html" import status_tile %}{{ status_tile(now) }}'


def _build_environment(settings: Settings) -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(WEB_DIR / "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    env.globals.update(
        app_name=constants.APP_NAME,
        mode=settings.mode,
        live_status=constants.LIVE_TRADING_STATUS,
        nav_items=NAV_ITEMS,
        grafana_url=f"https://{settings.grafana_hostname}/" if settings.grafana_hostname else None,
    )
    return env


def _dashboard_context() -> dict[str, Any]:
    return {"tiles": UNAVAILABLE_TILES, "now": datetime.now(UTC)}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    env = _build_environment(settings)

    def render(template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        return HTMLResponse(env.get_template(template).render(**context), status_code=status_code)

    app = FastAPI(
        title=constants.APP_NAME,
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    allowed_hosts = ["localhost", "127.0.0.1"]
    if settings.app_hostname:
        allowed_hosts.append(settings.app_hostname)
    if settings.environment == "test":
        allowed_hosts.append("testserver")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        message = (
            "Page not found." if exc.status_code == 404 else "The request could not be completed."
        )
        return render(
            "error.html", exc.status_code, status_code_text=exc.status_code, message=message
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> Response:
        logger.error("unhandled application error", exc_info=exc)
        return render("error.html", 500, status_code_text=500, message="Something went wrong.")

    app.include_router(health_router)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return render("dashboard.html", **_dashboard_context())

    @app.get("/login", response_class=HTMLResponse)
    def login() -> HTMLResponse:
        return render("login.html")

    @app.get("/partials/status", response_class=HTMLResponse)
    def status_partial() -> HTMLResponse:
        html = env.from_string(_STATUS_PARTIAL).render(now=datetime.now(UTC))
        return HTMLResponse(html)

    return app
