"""The single place that decides whether an exchange reader or gateway exists.

Both exist only when `TD_COINBASE_KEY_FILE` names a credential file that passes the guards in
`app.exchange.credentials`; with no key file configured (the default) both are None and every host
command that needs one says so and does nothing. The gateway is additionally useless without a
host arming: the database refuses any COINBASE order attempt that is not armed. Tests inject the
scripted fake instead.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.config import Settings
from app.exchange.cdp_signer import CdpSigner
from app.exchange.coinbase_live import CoinbaseLiveGateway
from app.exchange.coinbase_private import CoinbasePrivateReader
from app.exchange.credentials import key_file_from_env, load_credentials
from app.exchange.gateway import ExecutionGateway
from app.exchange.reader import ExchangeReader


def build_reader(settings: Settings, env: Mapping[str, str] | None = None) -> ExchangeReader | None:
    """The authenticated read adapter, or None when no credential is configured.

    Raises `CredentialError` when a key file is configured but unusable: a broken credential is an
    error to show, never a silent fallback.
    """
    path = key_file_from_env(env)
    if path is None:
        return None
    return CoinbasePrivateReader(settings.exchange, CdpSigner(load_credentials(path)))


def build_gateway(
    settings: Settings, env: Mapping[str, str] | None = None
) -> ExecutionGateway | None:
    """The order gateway, or None when no credential is configured (see `build_reader`)."""
    path = key_file_from_env(env)
    if path is None:
        return None
    return CoinbaseLiveGateway(settings.exchange, CdpSigner(load_credentials(path)))
