"""Minimal egress allowlist proxy: HTTP CONNECT to api.coinbase.com:443 and nothing else.

This is the only component that may reach the internet on behalf of the pair runner. It refuses
every other method, host and port, refuses hosts that resolve to non-public addresses (DNS
rebinding, SSRF), bounds idle time and bytes per tunnel, and logs nothing about destinations.
It never sees plaintext: tunnels carry TLS end to end.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Callable
from typing import Final

ALLOWED: Final = frozenset({("api.coinbase.com", 443)})
MAX_HEADER_BYTES: Final = 8192
CONNECT_TIMEOUT: Final = 10.0
IDLE_TIMEOUT: Final = 30.0
MAX_TUNNEL_BYTES: Final = 32 * 1024 * 1024
MAX_TUNNELS: Final = 8
_REQUEST_LINE: Final = re.compile(rb"^CONNECT ([A-Za-z0-9.-]{1,253}):([0-9]{1,5}) HTTP/1\.[01]$")

logger = logging.getLogger("app")
Resolver = Callable[[str, int], list[str]]


def system_resolver(host: str, port: int) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def parse_connect(head: bytes) -> tuple[str, int] | None:
    """(host, port) for a well-formed CONNECT request line, else None."""
    line = head.split(b"\r\n", 1)[0]
    match = _REQUEST_LINE.fullmatch(line)
    if not match:
        return None
    return match.group(1).decode("ascii").lower(), int(match.group(2))


class EgressProxy:
    def __init__(self, *, resolver: Resolver = system_resolver) -> None:
        self._resolver = resolver
        self._slots = asyncio.Semaphore(MAX_TUNNELS)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def _refuse(self, writer: asyncio.StreamWriter, status: bytes) -> None:
        writer.write(b"HTTP/1.1 " + status + b"\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=IDLE_TIMEOUT)
        if len(head) > MAX_HEADER_BYTES:
            return await self._refuse(writer, b"431 Header Too Large")
        target = parse_connect(head)
        if target is None:
            return await self._refuse(writer, b"405 Method Not Allowed")
        if target not in ALLOWED:
            return await self._refuse(writer, b"403 Forbidden")
        loop = asyncio.get_running_loop()
        addresses = await loop.run_in_executor(None, self._resolver, *target)
        public = [a for a in addresses if is_public(a)]
        if not public or len(public) != len(addresses):
            return await self._refuse(writer, b"403 Forbidden")
        async with self._slots:
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(public[0], target[1], server_hostname=None),
                    timeout=CONNECT_TIMEOUT,
                )
            except (TimeoutError, OSError):
                return await self._refuse(writer, b"502 Bad Gateway")
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            budget = [MAX_TUNNEL_BYTES]
            await asyncio.gather(
                self._pipe(reader, upstream_writer, budget),
                self._pipe(upstream_reader, writer, budget),
            )

    async def _pipe(
        self, source: asyncio.StreamReader, sink: asyncio.StreamWriter, budget: list[int]
    ) -> None:
        try:
            while True:
                chunk = await asyncio.wait_for(source.read(16384), timeout=IDLE_TIMEOUT)
                if not chunk:
                    break
                budget[0] -= len(chunk)
                if budget[0] < 0:
                    break
                sink.write(chunk)
                await sink.drain()
        except (TimeoutError, ConnectionError, OSError):
            pass
        finally:
            sink.close()


async def serve(host: str, port: int) -> None:  # pragma: no cover  (exercised by the container)
    proxy = EgressProxy()
    server = await asyncio.start_server(proxy.handle, host, port, limit=MAX_HEADER_BYTES)
    async with server:
        await server.serve_forever()


def main() -> None:  # pragma: no cover
    asyncio.run(serve("0.0.0.0", 3128))  # noqa: S104  container-internal, never published


if __name__ == "__main__":  # pragma: no cover
    main()
