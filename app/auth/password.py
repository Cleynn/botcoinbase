"""Argon2id hashing and password policy and policy (length + denylist, no composition rules)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import AuthSettings

_COMMON = frozenset(
    {
        "passwordpassword",
        "password123456",
        "1234567890123",
        "12345678901234",
        "123456789012345",
        "qwertyuiopasdf",
        "qwertyuiopasdfgh",
        "iloveyouiloveyou",
        "administrator1234",
        "letmeinletmein",
        "welcomewelcome",
        "trustno1trustno1",
    }
)
_PRODUCT_TERMS = ("tradingdots", "coinbase", "onthewall")

POLICY_MESSAGES: dict[str, str] = {
    "TOO_SHORT": "The password is too short.",
    "TOO_LONG": "The password is too long.",
    "COMMON": "That password is too common.",
    "CONTAINS_USERNAME": "The password must not contain the username.",
    "PRODUCT_TERM": "The password must not contain the product or exchange name.",
    "LOW_VARIETY": "The password uses too few distinct characters.",
    "CONTROL_CHARS": "The password contains control characters.",
}


class PasswordPolicyError(ValueError):
    """Raised with machine-readable rule codes (never the password itself)."""

    def __init__(self, codes: tuple[str, ...]) -> None:
        super().__init__(", ".join(codes))
        self.codes = codes


def policy_violations(password: str, username: str, settings: AuthSettings) -> tuple[str, ...]:
    codes: list[str] = []
    if len(password) < settings.password_min_length:
        codes.append("TOO_SHORT")
    if len(password) > settings.password_max_length:
        codes.append("TOO_LONG")
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        codes.append("CONTROL_CHARS")
    lowered = password.lower()
    if lowered in _COMMON or lowered.strip() == "":
        codes.append("COMMON")
    if username and username.lower() in lowered:
        codes.append("CONTAINS_USERNAME")
    if any(term in lowered for term in _PRODUCT_TERMS):
        codes.append("PRODUCT_TERM")
    if len(set(password)) < 6:
        codes.append("LOW_VARIETY")
    return tuple(codes)


def validate_new_password(password: str, username: str, settings: AuthSettings) -> None:
    codes = policy_violations(password, username, settings)
    if codes:
        raise PasswordPolicyError(codes)


@dataclass(frozen=True)
class PasswordService:
    """Argon2id with parameters taken from settings (production minimums enforced elsewhere)."""

    hasher: PasswordHasher
    _dummy_hash: str

    @classmethod
    def create(cls, settings: AuthSettings) -> PasswordService:
        hasher = PasswordHasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_kib,
            parallelism=settings.argon2_parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        return cls(hasher, hasher.hash(secrets.token_urlsafe(24)))

    def hash(self, password: str) -> str:
        return self.hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self.hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self.hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True

    def burn(self, password: str) -> None:
        """Do equivalent work for unknown accounts so timing does not reveal existence."""
        self.verify(self._dummy_hash, password)
