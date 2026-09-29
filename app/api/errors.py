"""Error handling: generic pages only, never internals. Also the request-size guard."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.dependencies import clear_cookie, get_services
from app.auth.authorization import CsrfRejected, Forbidden, LoginRequired, UndeclaredAccess
from app.storage.database import StorageUnavailable

logger = logging.getLogger("app")

MAX_BODY_BYTES = 16 * 1024


def _page(request: Request, status: int, message: str) -> Response:
    return get_services(request).renderer.html(
        "error.html", status, status_code_text=status, message=message
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> Response:
        services = get_services(request)
        if request.headers.get("hx-request") == "true":
            response: Response = Response(status_code=200, headers={"HX-Redirect": "/login"})
        else:
            response = RedirectResponse("/login", status_code=303)
        if request.cookies.get(services.cookie.name):
            clear_cookie(response, services.cookie, services.cookie.name)
        return response

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden) -> Response:
        return _page(request, 403, "You do not have permission to view this page.")

    @app.exception_handler(CsrfRejected)
    async def csrf_rejected(request: Request, exc: CsrfRejected) -> Response:
        return _page(
            request, 403, "The request could not be verified. Reload the page and try again."
        )

    @app.exception_handler(UndeclaredAccess)
    async def undeclared(request: Request, exc: UndeclaredAccess) -> Response:
        logger.error("route reached the access guard without an access declaration")
        return _page(request, 403, "You do not have permission to view this page.")

    @app.exception_handler(StorageUnavailable)
    async def unavailable(request: Request, exc: StorageUnavailable) -> Response:
        logger.error("database unavailable")
        return _page(request, 503, "The service is temporarily unavailable.")

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> Response:
        return _page(request, 400, "The request was invalid.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        message = (
            "Page not found." if exc.status_code == 404 else "The request could not be completed."
        )
        return _page(request, exc.status_code, message)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> Response:
        logger.error("unhandled application error", exc_info=exc)
        return _page(request, 500, "Something went wrong.")


class _TooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Pure ASGI cap on request bodies (Content-Length and streamed), before any parsing."""

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int = MAX_BODY_BYTES,
        overrides: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.overrides = overrides or {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.overrides.get(scope.get("path", ""), self.max_bytes)
        declared = dict(scope["headers"]).get(b"content-length", b"")
        started = False
        exceeded = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            if exceeded and message["type"] == "http.response.start":
                # A downstream parser may have swallowed our exception and answered 400.
                message = {**message, "status": 413}
            await send(message)

        async def too_large() -> None:
            if started:
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"Request too large."})

        if declared.isdigit() and int(declared) > limit:
            await too_large()
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _TooLarge
            return message

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _TooLarge:
            await too_large()
