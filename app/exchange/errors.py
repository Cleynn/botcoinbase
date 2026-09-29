"""Exchange errors with a fixed vocabulary. Messages never carry response data or credentials."""

from __future__ import annotations


class ExchangeError(Exception):
    """A call to the exchange boundary failed.

    `ambiguous` is True when the request may have been processed (timeout, connection loss, 5xx
    after the request was sent). An ambiguous failure of an order-creating call must be followed by
    reconciliation, never by a blind retry.
    """

    def __init__(self, code: str, *, status: int | None = None, ambiguous: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.ambiguous = ambiguous

    @property
    def transient(self) -> bool:
        """A read may be repeated. Never used to repeat an order-creating call."""
        return self.code in {"TIMEOUT", "NETWORK", "RATE_LIMITED", "SERVER_ERROR"}


class NoCredentials(ExchangeError):
    """No signer is configured. This is the state of every deployment of this build."""

    def __init__(self) -> None:
        super().__init__("NO_CREDENTIALS")
