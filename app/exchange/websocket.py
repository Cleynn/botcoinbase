"""WebSocket user-channel updates: SUPPLEMENTAL hints, never authoritative.

REST reconciliation is the authority for order and fill state. A message from this feed can only
(a) be recorded as a hint, and (b) ask for an early reconciliation. It never changes an order
attempt, a ledger row, a balance or any bot state, and a hint that disagrees with REST loses.

This module parses and tracks messages. It contains no socket, no client and no connection code:
the transport is a protocol (`Transport`), and this build ships none (the egress proxy carries
REST only, and CB-16 excluded a WebSocket client), so in every deployment of this build the feed is
simply absent and REST reconciliation runs alone. Tests drive it with scripted messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol

from app.adapters.coinbase_parse import ParseError, load_json
from app.exchange.models import ORDER_ID_RE, STATUS_MAP

MAX_MESSAGE_BYTES: Final = 256 * 1024
MAX_ORDERS_PER_MESSAGE: Final = 500
CHANNELS: Final = ("user", "heartbeats")


class Transport(Protocol):
    def recv(self) -> bytes | None: ...


@dataclass(frozen=True)
class OrderHint:
    client_order_id: str
    order_id: str
    status: str  # local vocabulary (WORKING, FILLED, ...), UNKNOWN for anything unmapped
    filled_qty: str  # canonical decimal text as received (validated to be a plain number)
    sequence: int
    received_at: datetime


@dataclass
class FeedState:
    last_sequence: int | None = None
    last_message_at: datetime | None = None
    hints_seen: int = 0
    malformed: int = 0
    gaps: int = 0
    reconcile_requested: list[str] = field(default_factory=list)


def _plain(value: object) -> str | None:
    from app.adapters.coinbase_parse import parse_decimal

    parsed = parse_decimal(value)
    return None if parsed is None else format(parsed, "f")


class UserFeed:
    def __init__(self) -> None:
        self.state = FeedState()

    def reset_connection(self) -> None:
        """A new connection restarts sequence numbers; missed events are covered by a reconcile."""
        self.state.last_sequence = None
        self._ask("WS_RECONNECT")

    def _ask(self, reason: str) -> None:
        if reason not in self.state.reconcile_requested:
            self.state.reconcile_requested.append(reason)

    def take_reconcile_requests(self) -> list[str]:
        out, self.state.reconcile_requested = self.state.reconcile_requested, []
        return out

    def silence_seconds(self, now: datetime) -> int | None:
        last = self.state.last_message_at
        return None if last is None else int((now - last).total_seconds())

    def handle(self, raw: bytes, received_at: datetime) -> list[OrderHint]:
        """Parse one message. Never raises: anything wrong is counted and asks for a reconcile."""
        hints: list[OrderHint] = []
        try:
            if len(raw) > MAX_MESSAGE_BYTES:
                raise ParseError("TOO_LARGE")
            data = load_json(raw)
            if not isinstance(data, dict) or data.get("channel") not in CHANNELS:
                raise ParseError("BAD_MESSAGE")
            seq = data.get("sequence_num")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise ParseError("BAD_SEQUENCE")
            events = data.get("events")
            if not isinstance(events, list):
                raise ParseError("BAD_MESSAGE")
            if data["channel"] == "user":
                hints = self._orders(events, seq, received_at)
        except ParseError:
            self.state.malformed += 1
            self._ask("WS_MALFORMED")
            return []
        last = self.state.last_sequence
        if last is not None and seq != last + 1:
            self.state.gaps += 1
            self._ask("WS_SEQUENCE_GAP")
        self.state.last_sequence = seq
        self.state.last_message_at = received_at
        self.state.hints_seen += len(hints)
        return hints

    def _orders(self, events: list[object], seq: int, at: datetime) -> list[OrderHint]:
        out: list[OrderHint] = []
        for event in events:
            if not isinstance(event, dict):
                raise ParseError("BAD_EVENT")
            orders = event.get("orders", [])
            if not isinstance(orders, list) or len(orders) > MAX_ORDERS_PER_MESSAGE:
                raise ParseError("BAD_EVENT")
            for raw in orders:
                if not isinstance(raw, dict):
                    raise ParseError("BAD_ORDER")
                client = raw.get("client_order_id")
                oid = raw.get("order_id")
                if not (isinstance(client, str) and ORDER_ID_RE.fullmatch(client)):
                    raise ParseError("BAD_ORDER")
                if not (isinstance(oid, str) and ORDER_ID_RE.fullmatch(oid)):
                    raise ParseError("BAD_ORDER")
                status = STATUS_MAP.get(str(raw.get("status", "")), "UNKNOWN")
                filled = _plain(raw.get("cumulative_quantity", "0"))
                if filled is None:
                    raise ParseError("BAD_ORDER")
                out.append(OrderHint(client, oid, status, filled, seq, at))
        return out


def utc_now() -> datetime:  # pragma: no cover  (tests inject their own times)
    return datetime.now(UTC)
