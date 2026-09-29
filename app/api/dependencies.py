"""Request-scoped services: client identity, session resolution, access declarations, CSRF guard.

`access_guard` and `csrf_guard` are registered app-wide, so a route added later is default-deny
and CSRF-protected without anyone remembering to opt in.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import Request
from fastapi.responses import Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from app.api.limits import IMPORT_CONFIRM_PATH
from app.auth import csrf
from app.auth.audit import AuditWriter, actor_for
from app.auth.authorization import (
    PUBLIC,
    AccessDeclaration,
    CsrfRejected,
    Forbidden,
    LoginRequired,
    UndeclaredAccess,
    authorize,
    declare,
    declared_access,
)
from app.auth.rate_limit import LoginRateLimiter
from app.auth.session import AuthService, hash_token, valid_token_format
from app.config import CookieSettings, Settings
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import AuthContext, ClientIdentity, Clock
from app.domain.permissions import Permission
from app.pairs.service import PairService
from app.proposals.service import ProposalService
from app.review.service import ReviewService
from app.storage.database import Storage
from app.web.view_models import Renderer

logger = logging.getLogger("app")

LOGIN_PATH = "/login"
MAX_FORM_FIELDS = 20


@dataclass(frozen=True)
class Services:
    settings: Settings
    storage: Storage
    clock: Clock
    auth: AuthService
    pairs: PairService
    review: ReviewService
    proposals: ProposalService
    audit: AuditWriter
    limiter: LoginRateLimiter
    csrf_key: bytes
    renderer: Renderer
    cookie: CookieSettings
    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(default=())


def get_services(request: Request) -> Services:
    return cast(Services, request.app.state.services)


def request_id(request: Request) -> str | None:
    return cast("str | None", getattr(request.state, "request_id", None))


# ---------------------------------------------------------------- client identity
def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def resolve_client(request: Request) -> ClientIdentity:
    """Client identity as a keyed hash. X-Forwarded-For is honoured only from trusted proxies."""
    cached = getattr(request.state, "client_identity", None)
    if cached is not None:
        return cast(ClientIdentity, cached)
    services = get_services(request)
    peer = request.client.host if request.client else ""
    address = peer
    peer_ip = _parse_ip(peer)
    if peer_ip is not None and any(peer_ip in net for net in services.trusted_networks):
        forwarded = request.headers.get("x-forwarded-for", "")
        candidate = _parse_ip(forwarded.split(",")[-1]) if forwarded else None
        if candidate is not None:
            address = str(candidate)
    identity = services.limiter.client_identity(address or "unknown")
    request.state.client_identity = identity
    return identity


# ---------------------------------------------------------------- session resolution
def resolve_auth(request: Request, *, touch: bool) -> AuthContext | None:
    if getattr(request.state, "auth_resolved", False):
        return cast("AuthContext | None", request.state.auth)
    services = get_services(request)
    token = request.cookies.get(services.cookie.name)
    ctx = services.auth.validate(
        token, touch=touch, client=resolve_client(request), request_id=request_id(request)
    )
    request.state.auth_resolved = True
    request.state.auth = ctx
    return ctx


def public() -> None:
    """Marks a route as reachable without a session."""


declare(public, PUBLIC)


def require_permission(permission: Permission, *, touch: bool = True) -> Callable[..., AuthContext]:
    """Dependency factory: valid session and an explicit permission, otherwise refuse."""

    def dependency(request: Request) -> AuthContext:
        ctx = resolve_auth(request, touch=touch)
        if ctx is None:
            raise LoginRequired
        if not authorize(ctx.user.role, permission):
            services = get_services(request)
            route = request.scope.get("route")
            try:
                with services.storage.tx() as repos:
                    services.audit.record(
                        repos,
                        AuditEventType.AUTHZ_DENIED,
                        AuditResult.DENIED,
                        actor=actor_for(ctx.user),
                        target_type="route",
                        target_id=getattr(route, "path", "unknown"),
                        reason="INSUFFICIENT_ROLE",
                        client_tag=resolve_client(request).tag,
                        request_id=request_id(request),
                        detail={"permission": permission.value},
                        throttle_seconds=60,
                    )
            except Exception:  # noqa: BLE001  denial must still be enforced if auditing fails
                logger.error("could not record an authorization denial in the audit log")
            raise Forbidden
        return ctx

    declare(dependency, AccessDeclaration(public=False, permission=permission))
    return dependency


def access_guard(request: Request) -> None:
    """Default deny: refuse any route that did not declare `public` or a permission."""
    route = request.scope.get("route")
    if not isinstance(route, APIRoute) or declared_access(route) is None:
        raise UndeclaredAccess


# ---------------------------------------------------------------- CSRF
def _expected_csrf(request: Request, services: Services) -> str | None:
    if request.url.path == LOGIN_PATH:
        nonce = request.cookies.get(services.cookie.login_name)
        if nonce is None or not csrf.valid_login_nonce(nonce):
            return None
        return csrf.login_token(services.csrf_key, nonce)
    token = request.cookies.get(services.cookie.name)
    if not valid_token_format(token):
        return None
    return csrf.session_token(services.csrf_key, hash_token(token or ""))


def _audit_csrf_rejection(request: Request, services: Services, reason: str) -> None:
    try:
        with services.storage.tx() as repos:
            services.audit.record(
                repos,
                AuditEventType.CSRF_REJECTED,
                AuditResult.DENIED,
                reason=reason,
                target_type="route",
                target_id=getattr(request.scope.get("route"), "path", "unknown"),
                client_tag=resolve_client(request).tag,
                request_id=request_id(request),
                throttle_seconds=60,
            )
    except Exception:  # noqa: BLE001  the request is refused regardless of audit availability
        logger.error("could not record a CSRF rejection in the audit log")


async def csrf_guard(request: Request) -> None:
    """Every POST/PUT/PATCH/DELETE needs a same-origin signal and a valid CSRF token."""
    if request.method in csrf.SAFE_METHODS:
        return
    services = get_services(request)
    host = request.headers.get("host", "")
    require_https = services.settings.environment == "production"

    async def reject(reason: str) -> None:
        await run_in_threadpool(_audit_csrf_rejection, request, services, reason)
        raise CsrfRejected

    if not csrf.origin_allowed(request.headers, host, require_https=require_https):
        await reject("ORIGIN")
    supplied = request.headers.get("x-csrf-token")
    upload = request.url.path == IMPORT_CONFIRM_PATH  # the one route that accepts a single file
    if upload and _expected_csrf(request, services) is None:
        await reject("TOKEN")  # never buffer a large body for a request without a session
    if supplied is None:
        try:
            form = await request.form(max_fields=MAX_FORM_FIELDS, max_files=1 if upload else 0)
        except Exception:  # noqa: BLE001  malformed bodies are simply rejected
            await reject("BAD_BODY")
            return
        value: Any = form.get("csrf_token")
        supplied = value if isinstance(value, str) else None
    expected = _expected_csrf(request, services)
    if expected is None or not csrf.tokens_match(expected, supplied):
        await reject("TOKEN")


# ---------------------------------------------------------------- cookies
def set_session_cookie(response: Response, cookie: CookieSettings, token: str) -> None:
    response.set_cookie(
        cookie.name,
        token,
        path=cookie.path,
        secure=cookie.secure,
        httponly=cookie.httponly,
        samesite=cookie.samesite,
    )


def set_login_cookie(response: Response, cookie: CookieSettings, nonce: str) -> None:
    response.set_cookie(
        cookie.login_name,
        nonce,
        max_age=3600,
        path=cookie.path,
        secure=cookie.secure,
        httponly=True,
        samesite=cookie.samesite,
    )


def clear_cookie(response: Response, cookie: CookieSettings, name: str) -> None:
    response.delete_cookie(
        name, path=cookie.path, secure=cookie.secure, httponly=True, samesite=cookie.samesite
    )
