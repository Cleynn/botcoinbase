"""Account- and client-aware login throttling stored in PostgreSQL (no Redis).

Three counters, all keyed by HMACs so neither usernames nor addresses are stored:
  PAIR    account + client, reset by a successful login from that pair
  CLIENT  one client across accounts (credential stuffing)
  ACCOUNT one account from anywhere (distributed guessing)
Keys are derived from the *submitted* name whether or not the account exists, so throttling
never reveals which accounts exist.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import AuthSettings
from app.domain.enums import LoginScope
from app.domain.models import ClientIdentity
from app.storage.repositories import Repos


@dataclass(frozen=True)
class LimitKeys:
    pair: str
    client: str
    account: str


@dataclass(frozen=True)
class Throttle:
    blocked: bool
    retry_after: int = 0


def keyed_hash(key: bytes, *parts: str) -> str:
    return hmac.new(key, "|".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


class LoginRateLimiter:
    def __init__(self, settings: AuthSettings, identity_key: bytes) -> None:
        self._settings = settings
        self._key = identity_key

    def client_identity(self, address: str) -> ClientIdentity:
        return ClientIdentity.from_key(keyed_hash(self._key, "client", address))

    def keys(self, namespace: str, account: str, client: ClientIdentity) -> LimitKeys:
        return LimitKeys(
            pair=keyed_hash(self._key, namespace, "pair", account, client.key),
            client=keyed_hash(self._key, namespace, "client", client.key),
            account=keyed_hash(self._key, namespace, "account", account),
        )

    def check(self, repos: Repos, keys: LimitKeys, now: datetime) -> Throttle:
        s = self._settings
        since = now - timedelta(seconds=s.login_window_seconds)
        limits = (
            (LoginScope.PAIR, keys.pair, s.login_pair_max_failures, True),
            (LoginScope.CLIENT, keys.client, s.login_client_max_failures, False),
            (LoginScope.ACCOUNT, keys.account, s.login_account_max_failures, False),
        )
        retry = 0
        for scope, key, maximum, reset in limits:
            count, oldest = repos.attempts.failure_window(scope, key, since, reset_on_success=reset)
            if count >= maximum and oldest is not None:
                wait = (oldest + timedelta(seconds=s.login_window_seconds) - now).total_seconds()
                retry = max(retry, math.ceil(max(wait, 1)))
        return Throttle(blocked=retry > 0, retry_after=retry)

    def record_failure(self, repos: Repos, keys: LimitKeys, now: datetime) -> None:
        repos.attempts.add(LoginScope.PAIR, keys.pair, False, now)
        repos.attempts.add(LoginScope.CLIENT, keys.client, False, now)
        repos.attempts.add(LoginScope.ACCOUNT, keys.account, False, now)

    def record_success(self, repos: Repos, keys: LimitKeys, now: datetime) -> None:
        repos.attempts.add(LoginScope.PAIR, keys.pair, True, now)
