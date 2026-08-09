#!/usr/bin/env python3
"""Egress broker for the codex-modal Docker sandbox.

The sandbox container has no route to the internet and no DNS. Everything it
reaches goes through this process, which runs in a separate container with no
access to the sandbox's filesystem. Two listeners:

  * ``:3128``  HTTP forward proxy (CONNECT and absolute-URI) that only allows
    globally routable destinations on allow-listed ports. Private, loopback,
    link-local, and carrier-grade-NAT addresses are refused, so the sandbox can
    reach the public internet but never the host, the LAN, or its own Docker
    networks. Destination hostnames are resolved here and the connection is made
    to the validated address, so a rebinding answer cannot smuggle a private IP.

  * ``:8081``  reverse proxy for the model API. The sandbox speaks plain HTTP
    with no credential; this process attaches the real ``Authorization`` header
    and forwards to the configured upstream. The sandbox therefore never holds a
    Modal proxy token or any other upstream secret.

Every decision is written to stdout as JSON lines so the host can export the
complete egress record after a run.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit

MAX_HEAD_BYTES = 64 * 1024
IDLE_TIMEOUT_SECONDS = 900.0
CONNECT_TIMEOUT_SECONDS = 30.0

# Hop-by-hop headers must not be forwarded, and the sandbox must not be able to
# influence the credential we attach upstream.
STRIPPED_REQUEST_HEADERS = frozenset(
    {
        "proxy-authorization",
        "proxy-connection",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "authorization",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
    }
)


def log_event(**fields: object) -> None:
    fields["time"] = round(time.time(), 3)
    sys.stdout.write(json.dumps(fields, sort_keys=True) + "\n")
    sys.stdout.flush()


def parse_ports(raw: str) -> frozenset[int]:
    ports: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        port = int(piece)
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid egress port {piece!r}.")
        ports.add(port)
    return frozenset(ports)


def parse_hosts(raw: str) -> tuple[str, ...]:
    return tuple(piece.strip().lower() for piece in raw.split(",") if piece.strip())


class Policy:
    """Destination policy for the forward proxy."""

    def __init__(self, allowed_ports: frozenset[int], allowed_hosts: tuple[str, ...]):
        self.allowed_ports = allowed_ports
        self.allowed_hosts = allowed_hosts

    def host_allowed(self, host: str) -> bool:
        if not self.allowed_hosts:
            return True
        host = host.lower().rstrip(".")
        for pattern in self.allowed_hosts:
            if pattern.startswith("."):
                if host == pattern[1:] or host.endswith(pattern):
                    return True
            elif host == pattern:
                return True
        return False

    @staticmethod
    def address_is_public(address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        for tunnelled in (getattr(ip, "sixtofour", None), getattr(ip, "teredo", None)):
            if tunnelled is not None:
                candidates = tunnelled if isinstance(tunnelled, tuple) else (tunnelled,)
                if any(not Policy.address_is_public(str(part)) for part in candidates):
                    return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
        if getattr(ip, "is_site_local", False):
            return False
        if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
            return False
        return True

    async def resolve(self, host: str, port: int) -> tuple[str, int]:
        """Resolve to a single validated public address, or raise ValueError."""

        if port not in self.allowed_ports:
            raise ValueError(f"port {port} is not in the sandbox egress allow-list")
        if not self.host_allowed(host):
            raise ValueError(f"host {host!r} is not in the sandbox egress allow-list")
        literal = host.strip("[]")
        try:
            ipaddress.ip_address(literal)
        except ValueError:
            pass
        else:
            if not self.address_is_public(literal):
                raise ValueError(f"address {literal} is not a public internet address")
            return literal, port
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise ValueError(f"could not resolve {host!r}: {error}") from error
        for info in infos:
            address = str(info[4][0])
            if self.address_is_public(address):
                return address, port
        raise ValueError(f"{host!r} only resolves to non-public addresses")


class RequestHead:
    __slots__ = ("method", "target", "version", "headers", "raw_headers", "body_prefix")

    def __init__(
        self,
        method: str,
        target: str,
        version: str,
        headers: dict[str, str],
        raw_headers: list[tuple[str, str]],
        body_prefix: bytes,
    ):
        self.method = method
        self.target = target
        self.version = version
        self.headers = headers
        self.raw_headers = raw_headers
        self.body_prefix = body_prefix


async def read_request_head(reader: asyncio.StreamReader) -> RequestHead | None:
    try:
        blob = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=IDLE_TIMEOUT_SECONDS
        )
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        return None
    except asyncio.LimitOverrunError as error:
        raise ValueError("request head exceeded the proxy buffer") from error
    if len(blob) > MAX_HEAD_BYTES:
        raise ValueError("request head is too large")
    text = blob.decode("latin-1")
    lines = text.split("\r\n")
    request_line = lines[0]
    parts = request_line.split(" ")
    if len(parts) != 3:
        raise ValueError(f"malformed request line {request_line!r}")
    method, target, version = parts
    headers: dict[str, str] = {}
    raw_headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"malformed header line {line!r}")
        name = name.strip()
        value = value.strip()
        raw_headers.append((name, value))
        headers[name.lower()] = value
    return RequestHead(method, target, version, headers, raw_headers, b"")


def split_authority(authority: str, default_port: int) -> tuple[str, int]:
    authority = authority.strip()
    if authority.startswith("["):
        host, _, remainder = authority.partition("]")
        host = host[1:]
        if remainder.startswith(":"):
            return host, int(remainder[1:])
        return host, default_port
    host, separator, port_text = authority.rpartition(":")
    if separator and port_text.isdigit():
        return host, int(port_text)
    return authority, default_port


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    total = 0
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=IDLE_TIMEOUT_SECONDS)
            if not chunk:
                break
            total += len(chunk)
            writer.write(chunk)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.write_eof()
        except (OSError, RuntimeError):
            pass
    return total


async def splice(
    client: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter],
) -> None:
    client_reader, client_writer = client
    upstream_reader, upstream_writer = upstream
    await asyncio.gather(
        pipe(client_reader, upstream_writer),
        pipe(upstream_reader, client_writer),
        return_exceptions=True,
    )


async def close_writer(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, ConnectionError, RuntimeError):
        pass


def rebuild_request(
    head: RequestHead,
    *,
    target: str,
    host_header: str,
    extra_headers: tuple[tuple[str, str], ...] = (),
    drop: frozenset[str] = STRIPPED_REQUEST_HEADERS,
) -> bytes:
    lines = [f"{head.method} {target} HTTP/1.1"]
    seen_host = False
    for name, value in head.raw_headers:
        lowered = name.lower()
        if lowered in drop:
            continue
        if lowered == "host":
            if seen_host:
                continue
            seen_host = True
            value = host_header
        lines.append(f"{name}: {value}")
    if not seen_host:
        lines.insert(1, f"Host: {host_header}")
    for name, value in extra_headers:
        lines.append(f"{name}: {value}")
    # One request per upstream connection keeps header injection correct and
    # makes streaming responses terminate cleanly.
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


async def send_simple_response(
    writer: asyncio.StreamWriter, status: str, message: str
) -> None:
    body = message.encode("utf-8")
    head = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("latin-1")
    try:
        writer.write(head + body)
        await writer.drain()
    except (OSError, ConnectionError):
        pass


class ForwardProxy:
    def __init__(self, policy: Policy):
        self.policy = policy

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await read_request_head(reader)
            if head is None:
                return
            if head.method.upper() == "CONNECT":
                await self._connect(head, reader, writer)
            else:
                await self._absolute(head, reader, writer)
        except ValueError as error:
            log_event(listener="forward", event="reject", reason=str(error))
            await send_simple_response(writer, "400 Bad Request", f"{error}\n")
        except Exception as error:  # pragma: no cover - defensive
            log_event(listener="forward", event="error", reason=repr(error))
        finally:
            await close_writer(writer)

    async def _open(self, host: str, port: int) -> tuple[
        asyncio.StreamReader, asyncio.StreamWriter, str
    ]:
        address, port = await self.policy.resolve(host, port)
        upstream = await asyncio.wait_for(
            asyncio.open_connection(address, port), timeout=CONNECT_TIMEOUT_SECONDS
        )
        return upstream[0], upstream[1], address

    async def _connect(
        self,
        head: RequestHead,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        host, port = split_authority(head.target, 443)
        try:
            upstream_reader, upstream_writer, address = await self._open(host, port)
        except ValueError as error:
            log_event(
                listener="forward",
                event="deny",
                method="CONNECT",
                host=host,
                port=port,
                reason=str(error),
            )
            await send_simple_response(writer, "403 Forbidden", f"{error}\n")
            return
        except (OSError, asyncio.TimeoutError) as error:
            log_event(
                listener="forward",
                event="unreachable",
                method="CONNECT",
                host=host,
                port=port,
                reason=str(error),
            )
            await send_simple_response(writer, "502 Bad Gateway", f"{error}\n")
            return
        log_event(
            listener="forward",
            event="allow",
            method="CONNECT",
            host=host,
            port=port,
            address=address,
        )
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        try:
            await splice((reader, writer), (upstream_reader, upstream_writer))
        finally:
            await close_writer(upstream_writer)

    async def _absolute(
        self,
        head: RequestHead,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if not head.target.lower().startswith("http://"):
            raise ValueError(
                "only CONNECT and absolute http:// targets are proxied; "
                f"got {head.target!r}"
            )
        parts = urlsplit(head.target)
        host, port = split_authority(parts.netloc, 80)
        try:
            upstream_reader, upstream_writer, address = await self._open(host, port)
        except ValueError as error:
            log_event(
                listener="forward",
                event="deny",
                method=head.method,
                host=host,
                port=port,
                reason=str(error),
            )
            await send_simple_response(writer, "403 Forbidden", f"{error}\n")
            return
        except (OSError, asyncio.TimeoutError) as error:
            log_event(
                listener="forward",
                event="unreachable",
                method=head.method,
                host=host,
                port=port,
                reason=str(error),
            )
            await send_simple_response(writer, "502 Bad Gateway", f"{error}\n")
            return
        origin_target = parts.path or "/"
        if parts.query:
            origin_target += f"?{parts.query}"
        log_event(
            listener="forward",
            event="allow",
            method=head.method,
            host=host,
            port=port,
            address=address,
            path=parts.path,
        )
        upstream_writer.write(
            rebuild_request(head, target=origin_target, host_header=parts.netloc)
        )
        await upstream_writer.drain()
        try:
            await splice((reader, writer), (upstream_reader, upstream_writer))
        finally:
            await close_writer(upstream_writer)


class ModelProxy:
    """Reverse proxy that attaches the upstream credential the sandbox lacks."""

    def __init__(self, upstream: str, authorization: str | None, local_prefix: str):
        parts = urlsplit(upstream)
        if parts.scheme not in {"http", "https"}:
            raise ValueError(f"Unsupported model upstream scheme in {upstream!r}.")
        self.scheme = parts.scheme
        self.host, self.port = split_authority(
            parts.netloc, 443 if parts.scheme == "https" else 80
        )
        self.base_path = parts.path.rstrip("/")
        self.authorization = authorization
        self.local_prefix = local_prefix.rstrip("/")
        self.host_header = parts.netloc

    def upstream_target(self, target: str) -> str:
        path, separator, query = target.partition("?")
        if self.local_prefix and path.startswith(self.local_prefix):
            path = path[len(self.local_prefix) :]
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_path}{path}" + (f"{separator}{query}" if separator else "")

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            head = await read_request_head(reader)
            if head is None:
                return
            target = self.upstream_target(head.target)
            context = ssl.create_default_context() if self.scheme == "https" else None
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host,
                    self.port,
                    ssl=context,
                    server_hostname=self.host if context else None,
                ),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
            extra: tuple[tuple[str, str], ...] = ()
            if self.authorization:
                extra = (("Authorization", self.authorization),)
            log_event(
                listener="model",
                event="forward",
                method=head.method,
                path=head.target,
                upstream=f"{self.scheme}://{self.host_header}{target}",
                credential="injected" if self.authorization else "none",
            )
            upstream_writer.write(
                rebuild_request(
                    head,
                    target=target,
                    host_header=self.host_header,
                    extra_headers=extra,
                )
            )
            await upstream_writer.drain()
            await splice((reader, writer), (upstream_reader, upstream_writer))
        except ValueError as error:
            log_event(listener="model", event="reject", reason=str(error))
            await send_simple_response(writer, "400 Bad Request", f"{error}\n")
        except (OSError, asyncio.TimeoutError, ssl.SSLError) as error:
            log_event(listener="model", event="error", reason=str(error))
            await send_simple_response(writer, "502 Bad Gateway", f"{error}\n")
        finally:
            if upstream_writer is not None:
                await close_writer(upstream_writer)
            await close_writer(writer)


async def main() -> int:
    policy = Policy(
        parse_ports(os.environ.get("EGRESS_ALLOW_PORTS", "80,443")),
        parse_hosts(os.environ.get("EGRESS_ALLOW_HOSTS", "")),
    )
    forward_port = int(os.environ.get("EGRESS_PORT", "3128"))
    model_port = int(os.environ.get("MODEL_PORT", "8081"))
    upstream = os.environ.get("MODEL_UPSTREAM", "").strip()
    token = os.environ.get("MODEL_AUTHORIZATION", "").strip()

    servers = []
    forward = ForwardProxy(policy)
    servers.append(await asyncio.start_server(forward.handle, "0.0.0.0", forward_port))
    log_event(
        listener="forward",
        event="listening",
        port=forward_port,
        allowed_ports=sorted(policy.allowed_ports),
        allowed_hosts=list(policy.allowed_hosts) or ["<any public address>"],
    )
    if upstream:
        model = ModelProxy(upstream, token or None, os.environ.get("MODEL_PREFIX", "/v1"))
        servers.append(await asyncio.start_server(model.handle, "0.0.0.0", model_port))
        log_event(
            listener="model",
            event="listening",
            port=model_port,
            upstream=f"{model.scheme}://{model.host_header}{model.base_path}",
            credential="injected" if model.authorization else "none",
        )
    async with servers[0]:
        await asyncio.gather(*(server.serve_forever() for server in servers))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
