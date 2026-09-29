"""The single place that decides whether an exchange reader or gateway exists.

In every deployment of this build there is neither: no signer is configured, no credential is
loaded, and no live gateway class exists. `build_reader` therefore returns None, and every host
command that needs one says so and does nothing. Tests inject the scripted fake instead.
"""

from __future__ import annotations

from app.config import Settings
from app.exchange.gateway import ExecutionGateway
from app.exchange.reader import ExchangeReader


def build_reader(settings: Settings) -> ExchangeReader | None:
    """No credential source exists in this build, so there is no private reader."""
    return None


def build_gateway(settings: Settings) -> ExecutionGateway | None:
    """There is no live gateway class in this build, so there is never a gateway."""
    return None
