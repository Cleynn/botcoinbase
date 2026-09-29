from __future__ import annotations

import pytest

from app.auth.password import (
    PasswordPolicyError,
    PasswordService,
    policy_violations,
    validate_new_password,
)
from app.config import AuthSettings

FAST = AuthSettings(argon2_memory_kib=8, argon2_time_cost=1, argon2_parallelism=1)


@pytest.fixture
def service() -> PasswordService:
    return PasswordService.create(FAST)


def test_hash_is_argon2id_with_configured_parameters(service: PasswordService) -> None:
    hashed = service.hash("a perfectly fine passphrase")
    assert hashed.startswith("$argon2id$v=19$m=8,t=1,p=1$")


def test_default_parameters_meet_the_production_minimum() -> None:
    defaults = AuthSettings()
    assert defaults.argon2_memory_kib >= 19456
    assert defaults.argon2_time_cost >= 2


def test_hashes_are_salted_and_never_contain_the_plaintext(service: PasswordService) -> None:
    password = "another passphrase entirely"
    first, second = service.hash(password), service.hash(password)
    assert first != second
    assert password not in first


def test_verify_accepts_only_the_right_password(service: PasswordService) -> None:
    hashed = service.hash("right passphrase here")
    assert service.verify(hashed, "right passphrase here")
    assert not service.verify(hashed, "wrong passphrase here")
    assert not service.verify(hashed, "")


@pytest.mark.parametrize(
    "bad_hash", ["", "plaintext", "$argon2id$garbage", "$2b$12$abcdefghijklmnopqrstuv"]
)
def test_verify_rejects_malformed_or_foreign_hashes_without_raising(
    service: PasswordService, bad_hash: str
) -> None:
    assert service.verify(bad_hash, "whatever password") is False


def test_rehash_is_requested_when_parameters_are_weaker_than_current() -> None:
    old = PasswordService.create(AuthSettings(argon2_memory_kib=8, argon2_time_cost=1))
    new = PasswordService.create(AuthSettings(argon2_memory_kib=16, argon2_time_cost=2))
    hashed = old.hash("some passphrase 12345")
    assert new.needs_rehash(hashed)
    assert not old.needs_rehash(hashed)
    assert new.needs_rehash("not-a-hash")


def test_burn_does_equivalent_work_and_never_raises(service: PasswordService) -> None:
    service.burn("anything at all")
    service.burn("")


def test_valid_passphrase_passes_policy() -> None:
    validate_new_password("correct horse battery staple", "alice", FAST)


def test_no_composition_rules_long_lowercase_is_fine() -> None:
    assert policy_violations("abcdefghijklmnopqrstuvwxyz", "alice", FAST) == ()


@pytest.mark.parametrize(
    ("password", "code"),
    [
        ("short1!", "TOO_SHORT"),
        ("x" * 200, "TOO_LONG"),
        ("passwordpassword", "COMMON"),
        ("PASSWORDPASSWORD", "COMMON"),
        ("my-alice-passphrase", "CONTAINS_USERNAME"),
        ("tradingdots is my passphrase", "PRODUCT_TERM"),
        ("my coinbase passphrase 1", "PRODUCT_TERM"),
        ("aaaaaaaaaaaaaaaaaaaa", "LOW_VARIETY"),
        ("a valid looking\x00passphrase", "CONTROL_CHARS"),
    ],
)
def test_policy_rules(password: str, code: str) -> None:
    assert code in policy_violations(password, "alice", FAST)
    with pytest.raises(PasswordPolicyError) as info:
        validate_new_password(password, "alice", FAST)
    assert code in info.value.codes
    assert password not in str(info.value)


def test_maximum_length_boundary() -> None:
    at_limit = "ab1" * 42 + "xy"  # 128 characters
    assert len(at_limit) == FAST.password_max_length
    assert "TOO_LONG" not in policy_violations(at_limit, "alice", FAST)
    assert "TOO_LONG" in policy_violations(at_limit + "z", "alice", FAST)
