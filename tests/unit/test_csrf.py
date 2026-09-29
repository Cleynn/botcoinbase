from __future__ import annotations

import pytest

from app.auth import csrf

KEY = b"k" * 32


def test_session_token_is_deterministic_and_session_bound() -> None:
    a1 = csrf.session_token(KEY, "a" * 64)
    assert a1 == csrf.session_token(KEY, "a" * 64)
    assert a1 != csrf.session_token(KEY, "b" * 64)
    assert a1 != csrf.session_token(b"z" * 32, "a" * 64)
    assert len(a1) == 43


def test_login_and_session_tokens_are_domain_separated() -> None:
    value = csrf.new_login_nonce()
    assert csrf.login_token(KEY, value) != csrf.session_token(KEY, value)


def test_nonce_shape() -> None:
    nonce = csrf.new_login_nonce()
    assert csrf.valid_login_nonce(nonce)
    assert nonce != csrf.new_login_nonce()
    for bad in (None, "", "short", "x" * 44, "!" * 43, nonce + "="):
        assert not csrf.valid_login_nonce(bad)


def test_tokens_match_is_exact() -> None:
    token = csrf.session_token(KEY, "a" * 64)
    assert csrf.tokens_match(token, token)
    assert not csrf.tokens_match(token, None)
    assert not csrf.tokens_match(token, "")
    assert not csrf.tokens_match(token, token[:-1])
    assert not csrf.tokens_match(token, token + "x")
    assert not csrf.tokens_match(token, "é" * 43)


@pytest.mark.parametrize(
    ("headers", "https", "expected"),
    [
        ({"origin": "https://app.example.com"}, True, True),
        ({"origin": "https://evil.example.com"}, True, False),
        ({"origin": "http://app.example.com"}, True, False),  # downgrade refused in production
        ({"origin": "http://app.example.com"}, False, True),  # allowed for local development
        ({"origin": "null"}, True, False),
        ({"origin": "https://app.example.com.evil.com"}, True, False),
        (
            {"origin": "https://evil.com", "sec-fetch-site": "same-origin"},
            True,
            False,
        ),  # Origin wins
        ({"sec-fetch-site": "same-origin"}, True, True),
        ({"sec-fetch-site": "none"}, True, True),
        ({"sec-fetch-site": "cross-site"}, True, False),
        ({"sec-fetch-site": "same-site"}, True, False),
        ({}, True, False),  # no signal at all: fail closed
    ],
)
def test_origin_matrix(headers: dict[str, str], https: bool, expected: bool) -> None:
    assert csrf.origin_allowed(headers, "app.example.com", require_https=https) is expected


def test_origin_with_port_must_match_exactly() -> None:
    assert csrf.origin_allowed(
        {"origin": "http://localhost:8000"}, "localhost:8000", require_https=False
    )
    assert not csrf.origin_allowed(
        {"origin": "http://localhost:9999"}, "localhost:8000", require_https=False
    )


def test_safe_methods() -> None:
    assert {"GET", "HEAD", "OPTIONS"} <= csrf.SAFE_METHODS
    assert not {"POST", "PUT", "PATCH", "DELETE"} & csrf.SAFE_METHODS
