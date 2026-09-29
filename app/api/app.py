"""FastAPI application factory: server-rendered Jinja2 + HTMX, authenticated, default deny."""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import constants
from app.api import admin, auth, dashboard, health, pairs, proposals, reports, review
from app.api.dependencies import Services, access_guard, csrf_guard
from app.api.errors import BodySizeLimitMiddleware, register_error_handlers
from app.api.limits import BODY_LIMIT_OVERRIDES
from app.auth.audit import AuditWriter
from app.auth.password import PasswordService
from app.auth.rate_limit import LoginRateLimiter
from app.auth.session import AuthService, derive_key
from app.config import ConfigError, Settings, load_settings
from app.domain.models import Clock, SystemClock
from app.monitoring.collectors import build_monitoring
from app.pairs.service import PairService, RuntimeGate
from app.paper.gate import PaperRuntimeGate
from app.proposals.service import ProposalService
from app.review.service import ReviewService
from app.storage.database import Storage
from app.web.pair_views import reason_text
from app.web.view_models import WEB_DIR, Renderer

logger = logging.getLogger("app")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # NOT "no-referrer": with that policy browsers serialise the Origin of same-origin POSTs as
    # "null", which the CSRF check must (and does) reject. "same-origin" leaks nothing to other
    # sites while keeping a real Origin on our own form submissions.
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def build_services(
    settings: Settings,
    storage: Storage,
    clock: Clock,
    pair_gate: RuntimeGate | None = None,
) -> Services:
    secret = settings.secret_key
    if secret is None or len(secret.get_secret_value()) < constants.MIN_SECRET_LENGTH:
        raise ConfigError(
            f"TD_SECRET_KEY is required (at least {constants.MIN_SECRET_LENGTH} characters)"
        )
    if settings.cookie is None:
        raise ConfigError("cookie settings are required")
    csrf_key = derive_key(secret, "csrf")
    limiter = LoginRateLimiter(settings.auth, derive_key(secret, "identity"))
    audit = AuditWriter(clock)
    auth_service = AuthService(
        storage=storage,
        settings=settings.auth,
        clock=clock,
        passwords=PasswordService.create(settings.auth),
        limiter=limiter,
        audit=audit,
        csrf_key=csrf_key,
    )
    renderer = Renderer(settings)
    renderer.add_global("pair_reason", reason_text)
    pair_service = PairService(
        storage=storage,
        clock=clock,
        policy=settings.pair_policy,
        audit=audit,
        gate=pair_gate or PaperRuntimeGate(settings.mode),
        consume_reauth=auth_service.consume_reauth,
        reauth_active=auth_service.reauth_is_fresh,
    )
    review_service = ReviewService(
        storage=storage,
        clock=clock,
        settings=settings,
        audit=audit,
        consume_reauth=auth_service.consume_reauth,
        reauth_active=auth_service.reauth_is_fresh,
    )
    proposal_service = ProposalService(
        storage=storage,
        clock=clock,
        settings=settings,
        audit=audit,
        consume_reauth=auth_service.consume_reauth,
        reauth_active=auth_service.reauth_is_fresh,
    )
    return Services(
        settings=settings,
        storage=storage,
        clock=clock,
        auth=auth_service,
        pairs=pair_service,
        review=review_service,
        proposals=proposal_service,
        audit=audit,
        limiter=limiter,
        csrf_key=csrf_key,
        renderer=renderer,
        cookie=settings.cookie,
        trusted_networks=tuple(
            ipaddress.ip_network(n, strict=False) for n in settings.trusted_proxies
        ),
    )


def create_app(
    settings: Settings | None = None,
    *,
    storage: Storage | None = None,
    clock: Clock | None = None,
    pair_gate: RuntimeGate | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    storage = storage or Storage(settings.database)
    storage.check_schema()  # refuse to start against an older or newer schema
    clock = clock or SystemClock()
    services = build_services(settings, storage, clock, pair_gate)
    monitoring = build_monitoring(storage=storage, clock=clock, settings=settings)

    app = FastAPI(
        title=constants.APP_NAME,
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # Registered app-wide: no route can opt out of default-deny or CSRF enforcement.
        dependencies=[Depends(access_guard), Depends(csrf_guard)],
    )
    app.state.services = services
    app.state.monitoring = monitoring  # metrics are served by a separate internal listener
    register_error_handlers(app)

    allowed_hosts = ["localhost", "127.0.0.1"]
    if settings.app_hostname:
        allowed_hosts.append(settings.app_hostname)
    if settings.environment == "test":
        allowed_hosts.append("testserver")
    app.add_middleware(BodySizeLimitMiddleware, overrides=BODY_LIMIT_OVERRIDES)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        request.state.request_id = uuid4().hex
        started = time.perf_counter()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
        finally:
            try:  # observing must never affect the response
                template = getattr(request.scope.get("route"), "path", None)
                monitoring.metrics.observe_request(template, status, time.perf_counter() - started)
            except Exception:  # noqa: BLE001
                logger.error("could not record request metrics")
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers["X-Request-ID"] = request.state.request_id
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault("Vary", "Cookie")
        return response

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(admin.router)
    app.include_router(pairs.router)
    app.include_router(reports.router)
    app.include_router(review.router)
    app.include_router(proposals.router)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    return app
