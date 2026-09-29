"""Login, logout, password change and reauthentication routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.api.admin import render_security
from app.api.dependencies import (
    clear_cookie,
    get_services,
    public,
    request_id,
    require_permission,
    resolve_auth,
    resolve_client,
    set_login_cookie,
    set_session_cookie,
)
from app.api.schemas import LoginForm, LogoutForm, PasswordChangeForm, ReauthForm
from app.auth import csrf
from app.auth.password import POLICY_MESSAGES
from app.domain.models import AuthContext
from app.domain.permissions import Permission

router = APIRouter()

_logout_access = require_permission(Permission.LOGOUT)
_password_access = require_permission(Permission.CHANGE_OWN_PASSWORD)
_reauth_access = require_permission(Permission.REAUTHENTICATE)

GENERIC_LOGIN_ERROR = "Invalid username or password."
THROTTLED_LOGIN_ERROR = "Too many sign-in attempts. Try again later."


def _login_page(
    request: Request, nonce: str, status: int = 200, error: str | None = None
) -> Response:
    services = get_services(request)
    response = services.renderer.html(
        "login.html", status, csrf_token=csrf.login_token(services.csrf_key, nonce), error=error
    )
    set_login_cookie(response, services.cookie, nonce)
    return response


@router.get("/login", dependencies=[Depends(public)])
def login_page(request: Request) -> Response:
    services = get_services(request)
    if resolve_auth(request, touch=False) is not None:
        return RedirectResponse("/", status_code=303)
    nonce = request.cookies.get(services.cookie.login_name)
    if not csrf.valid_login_nonce(nonce):
        nonce = csrf.new_login_nonce()
    return _login_page(request, nonce or "")


@router.post("/login", dependencies=[Depends(public)])
def login_submit(request: Request, form: Annotated[LoginForm, Form()]) -> Response:
    services = get_services(request)
    nonce = request.cookies.get(services.cookie.login_name) or ""
    result = services.auth.login(
        form.username,
        form.password,
        resolve_client(request),
        request_id(request),
        previous_token=request.cookies.get(services.cookie.name),
    )
    if result.status == "invalid":
        return _login_page(request, nonce, 401, GENERIC_LOGIN_ERROR)
    if result.status == "throttled":
        response = _login_page(request, nonce, 429, THROTTLED_LOGIN_ERROR)
        response.headers["Retry-After"] = str(max(result.retry_after, 1))
        return response
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, services.cookie, result.token or "")
    clear_cookie(response, services.cookie, services.cookie.login_name)
    return response


@router.post("/logout")
def logout(
    request: Request,
    form: Annotated[LogoutForm, Form()],
    ctx: Annotated[AuthContext, Depends(_logout_access)],
) -> Response:
    services = get_services(request)
    services.auth.logout(ctx, resolve_client(request), request_id(request))
    response = RedirectResponse("/login", status_code=303)
    clear_cookie(response, services.cookie, services.cookie.name)
    # Ask the browser to drop cookies, caches and any web storage for this origin.
    response.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
    return response


@router.post("/security/password")
def change_password(
    request: Request,
    form: Annotated[PasswordChangeForm, Form()],
    ctx: Annotated[AuthContext, Depends(_password_access)],
) -> Response:
    services = get_services(request)
    result = services.auth.change_password(
        ctx,
        form.current_password,
        form.new_password,
        form.confirm_password,
        resolve_client(request),
        request_id(request),
    )
    if result.status == "ok":
        response = RedirectResponse("/security?msg=password_changed", status_code=303)
        set_session_cookie(response, services.cookie, result.token or "")
        return response
    status = 429 if result.status == "throttled" else 400
    return render_security(
        request,
        ctx,
        status=status,
        message=result.status,
        rules=tuple(POLICY_MESSAGES.get(code, code) for code in result.codes),
    )


@router.post("/security/reauth")
def reauthenticate(
    request: Request,
    form: Annotated[ReauthForm, Form()],
    ctx: Annotated[AuthContext, Depends(_reauth_access)],
) -> Response:
    services = get_services(request)
    outcome = services.auth.reauthenticate(
        ctx, form.password, resolve_client(request), request_id(request)
    )
    if outcome == "ok":
        return RedirectResponse("/security?msg=reauth_ok", status_code=303)
    if outcome == "throttled":
        return render_security(request, ctx, status=429, message="throttled")
    return render_security(request, ctx, status=400, message="reauth_invalid")
