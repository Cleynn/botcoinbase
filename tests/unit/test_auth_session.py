"""Session lifecycle against a real database with an injectable clock."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from app.auth.session import (
    derive_key,
    hash_token,
    new_token,
    valid_token_format,
)
from app.domain.models import ClientIdentity
from tests.conftest import GOOD_PASSWORD, Account, FakeClock

CLIENT = ClientIdentity.from_key("c" * 64)


def _login(services: Any, account: Account, password: str | None = None) -> str:
    result = services.auth.login(account.username, password or account.password, CLIENT, "rid")
    assert result.status == "ok", result
    assert result.token
    return str(result.token)


def _ctx(services: Any, token: str | None, *, touch: bool = True) -> Any:
    return services.auth.validate(token, touch=touch, client=CLIENT, request_id="rid")


def test_token_shape_and_hashing() -> None:
    token = new_token()
    assert valid_token_format(token)
    assert token != new_token()
    digest = hash_token(token)
    assert re.fullmatch(r"[0-9a-f]{64}", digest) and token not in digest
    for bad in (None, "", "x", token + "x", token[:-1], "!" * 43):
        assert not valid_token_format(bad)


def test_derived_keys_are_purpose_separated() -> None:
    assert derive_key("s" * 40, "csrf") != derive_key("s" * 40, "identity")
    assert derive_key("s" * 40, "csrf") != derive_key("t" * 40, "csrf")


def test_valid_session_yields_context_with_bound_csrf_token(services: Any, admin: Account) -> None:
    token = _login(services, admin)
    ctx = _ctx(services, token)
    assert ctx.user.username == admin.username
    assert ctx.csrf_token == services.auth.csrf_token(hash_token(token))
    assert token not in repr(ctx) and ctx.csrf_token not in repr(ctx)


def test_unknown_or_malformed_tokens_are_rejected(services: Any) -> None:
    assert _ctx(services, None) is None
    assert _ctx(services, "garbage") is None
    assert _ctx(services, new_token()) is None


def test_idle_timeout(services: Any, admin: Account, clock: FakeClock) -> None:
    token = _login(services, admin)
    clock.advance(1799)
    assert _ctx(services, token) is not None  # activity resets the idle timer
    clock.advance(1800)
    assert _ctx(services, token) is None
    clock.advance(1)
    assert _ctx(services, token) is None  # stays dead


def test_idle_timeout_is_not_reset_by_untouched_reads(
    services: Any, admin: Account, clock: FakeClock
) -> None:
    token = _login(services, admin)
    for _ in range(2):  # 700 s and 1400 s since the last real activity: still inside 1800 s
        clock.advance(700)
        assert _ctx(services, token, touch=False) is not None
    clock.advance(500)  # 1900 s: polling did not extend the session
    assert _ctx(services, token, touch=False) is None


def test_absolute_expiry_even_with_constant_activity(
    services: Any, admin: Account, clock: FakeClock
) -> None:
    token = _login(services, admin)
    for _ in range(23):  # 23 x 1799 s < 12 h
        clock.advance(1799)
        assert _ctx(services, token) is not None
    clock.advance(1799 * 2)
    assert _ctx(services, token) is None


def test_touch_is_throttled(
    services: Any, admin: Account, clock: FakeClock, sql: Callable[..., Any]
) -> None:
    token = _login(services, admin)
    before = sql("SELECT last_seen_at FROM sessions")[0]["last_seen_at"]
    clock.advance(10)
    _ctx(services, token)
    assert sql("SELECT last_seen_at FROM sessions")[0]["last_seen_at"] == before
    clock.advance(60)
    _ctx(services, token)
    assert sql("SELECT last_seen_at FROM sessions")[0]["last_seen_at"] > before


def test_logout_invalidates_the_session(services: Any, admin: Account) -> None:
    token = _login(services, admin)
    ctx = _ctx(services, token)
    services.auth.logout(ctx, CLIENT, "rid")
    assert _ctx(services, token) is None


def test_session_cap_evicts_the_oldest(services: Any, admin: Account, clock: FakeClock) -> None:
    tokens = []
    for _ in range(6):
        tokens.append(_login(services, admin))
        clock.advance(5)
    assert _ctx(services, tokens[0]) is None
    assert all(_ctx(services, t) is not None for t in tokens[1:])


def test_disabled_user_loses_access(
    services: Any, admin: Account, sql: Callable[..., Any], clock: FakeClock
) -> None:
    token = _login(services, admin)
    sql("UPDATE users SET disabled_at = now()")
    assert _ctx(services, token) is None
    assert services.auth.login(admin.username, admin.password, CLIENT, "rid").status == "invalid"


def test_password_change_rotates_and_invalidates_everything_else(
    services: Any, admin: Account
) -> None:
    first, second = _login(services, admin), _login(services, admin)
    ctx = _ctx(services, first)
    result = services.auth.change_password(
        ctx, admin.password, "a brand new passphrase 99", "a brand new passphrase 99", CLIENT, "rid"
    )
    assert result.status == "ok" and result.token and result.token != first
    assert _ctx(services, first) is None
    assert _ctx(services, second) is None
    assert _ctx(services, result.token) is not None
    assert services.auth.login(admin.username, admin.password, CLIENT, "rid").status == "invalid"
    assert (
        services.auth.login(admin.username, "a brand new passphrase 99", CLIENT, "rid").status
        == "ok"
    )


@pytest.mark.parametrize(
    ("current", "new", "confirm", "status"),
    [
        (
            "wrong current password 1",
            "a brand new passphrase 99",
            "a brand new passphrase 99",
            "bad_current",
        ),
        (GOOD_PASSWORD, "a brand new passphrase 99", "different confirmation 99", "mismatch"),
        (GOOD_PASSWORD, GOOD_PASSWORD, GOOD_PASSWORD, "same"),
        (GOOD_PASSWORD, "short", "short", "policy"),
    ],
)
def test_password_change_refusals_leave_the_session_intact(
    services: Any, admin: Account, current: str, new: str, confirm: str, status: str
) -> None:
    token = _login(services, admin)
    result = services.auth.change_password(
        _ctx(services, token), current, new, confirm, CLIENT, "rid"
    )
    assert result.status == status and result.token is None
    assert _ctx(services, token) is not None
    assert services.auth.login(admin.username, GOOD_PASSWORD, CLIENT, "rid").status == "ok"


def test_reauthentication_is_fresh_and_single_use(
    services: Any, admin: Account, clock: FakeClock
) -> None:
    ctx = _ctx(services, _login(services, admin))

    def consume() -> bool:
        with services.storage.tx() as repos:
            return bool(services.auth.consume_reauth(repos, ctx))

    assert not consume()  # never reauthenticated
    assert services.auth.reauthenticate(ctx, "wrong password at all", CLIENT, "rid") == "invalid"
    assert not consume()
    assert services.auth.reauthenticate(ctx, admin.password, CLIENT, "rid") == "ok"
    assert consume()
    assert not consume()  # single use
    assert services.auth.reauthenticate(ctx, admin.password, CLIENT, "rid") == "ok"
    clock.advance(121)
    assert not consume()  # window elapsed


def test_reauth_state_never_leaks_between_sessions(services: Any, admin: Account) -> None:
    ctx_a = _ctx(services, _login(services, admin))
    ctx_b = _ctx(services, _login(services, admin))
    services.auth.reauthenticate(ctx_a, admin.password, CLIENT, "rid")
    with services.storage.tx() as repos:
        assert not services.auth.consume_reauth(repos, ctx_b)
        assert services.auth.consume_reauth(repos, ctx_a)
