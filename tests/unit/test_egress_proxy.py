"""The egress proxy allows one CONNECT target and refuses everything else."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.adapters import egress_proxy as ep


async def exchange(proxy: ep.EgressProxy, request: bytes) -> bytes:
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()
        return data
    finally:
        server.close()
        await server.wait_closed()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "line",
    [
        b"GET http://api.coinbase.com/ HTTP/1.1",
        b"POST / HTTP/1.1",
        b"CONNECT api.coinbase.com:80 HTTP/1.1",
        b"CONNECT api.coinbase.com:8443 HTTP/1.1",
        b"CONNECT evil.example:443 HTTP/1.1",
        b"CONNECT api.coinbase.com.evil.example:443 HTTP/1.1",
        b"CONNECT 127.0.0.1:443 HTTP/1.1",
        b"CONNECT 169.254.169.254:80 HTTP/1.1",
        b"CONNECT drb.coinbase.com:443 HTTP/1.1",
        b"CONNECT api-sandbox.coinbase.com:443 HTTP/1.1",
        b"CONNECT api.coinbase.com:443 HTTP/2",
        b"CONNECT [::1]:443 HTTP/1.1",
        b"",
    ],
)
def test_everything_but_the_one_target_is_refused(line: bytes) -> None:
    proxy = ep.EgressProxy(resolver=lambda h, p: ["93.184.216.34"])
    reply = run(exchange(proxy, line + b"\r\n\r\n"))
    assert reply.startswith((b"HTTP/1.1 403", b"HTTP/1.1 405")) or reply == b""


def test_parse_connect_is_strict() -> None:
    assert ep.parse_connect(b"CONNECT API.Coinbase.com:443 HTTP/1.1\r\n\r\n") == (
        "api.coinbase.com",
        443,
    )
    for bad in (
        b"CONNECT a b:443 HTTP/1.1",
        b"connect a:1 HTTP/1.1",
        b"CONNECT a:99999999 HTTP/1.1",
        b"CONNECT a:443\r\nHost: x HTTP/1.1",
    ):
        assert ep.parse_connect(bad + b"\r\n\r\n") is None


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fd00::1", "0.0.0.0"],
)
def test_a_host_that_resolves_to_a_private_address_is_refused(address: str) -> None:
    assert not ep.is_public(address)
    proxy = ep.EgressProxy(resolver=lambda h, p: [address])
    reply = run(exchange(proxy, b"CONNECT api.coinbase.com:443 HTTP/1.1\r\n\r\n"))
    assert reply.startswith(b"HTTP/1.1 403")


def test_a_mixed_public_and_private_answer_is_refused() -> None:
    proxy = ep.EgressProxy(resolver=lambda h, p: ["93.184.216.34", "10.0.0.1"])
    reply = run(exchange(proxy, b"CONNECT api.coinbase.com:443 HTTP/1.1\r\n\r\n"))
    assert reply.startswith(b"HTTP/1.1 403")


def test_the_allowed_target_is_tunnelled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> bytes:
        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"pong:" + await reader.read(100))
            await writer.drain()
            writer.close()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]
        real = asyncio.open_connection

        async def fake_open(host: str, port: int, **kw: Any) -> Any:
            assert host == "93.184.216.34" and port == 443
            return await real("127.0.0.1", up_port)

        monkeypatch.setattr(asyncio, "open_connection", fake_open)
        proxy = ep.EgressProxy(resolver=lambda h, p: ["93.184.216.34"])
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(asyncio, "open_connection", real)  # client side uses the real one
        reader, writer = await real("127.0.0.1", port)
        monkeypatch.setattr(asyncio, "open_connection", fake_open)
        writer.write(b"CONNECT api.coinbase.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(b"ping")
        await writer.drain()
        body = await asyncio.wait_for(reader.read(100), timeout=5)
        writer.close()
        server.close()
        upstream.close()
        return body

    assert run(scenario()) == b"pong:ping"


def test_limits_are_bounded() -> None:
    assert ep.ALLOWED == frozenset({("api.coinbase.com", 443)})
    assert ep.MAX_TUNNELS <= 8 and ep.MAX_TUNNEL_BYTES <= 64 * 1024 * 1024 and ep.IDLE_TIMEOUT <= 60
