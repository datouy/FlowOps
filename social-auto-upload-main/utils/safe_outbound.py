# -*- coding: utf-8 -*-
"""Outbound HTTP validation with DNS pinning for user-supplied URLs."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit


MAX_URL_LENGTH = 4096
MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_ALLOWED_PORTS = frozenset({80, 443})


class OutboundURLError(ValueError):
    """The destination is malformed or resolves to a forbidden network."""


@dataclass(frozen=True)
class OutboundTarget:
    url: str
    scheme: str
    hostname: str
    port: int
    address: str
    request_target: str

    @property
    def host_header(self) -> str:
        default_port = 443 if self.scheme == "https" else 80
        return self.hostname if self.port == default_port else f"{self.hostname}:{self.port}"


def _allowed_ports() -> set[int]:
    raw = str(os.environ.get("FLOWOPS_OUTBOUND_ALLOWED_PORTS") or "").strip()
    if not raw:
        return set(DEFAULT_ALLOWED_PORTS)
    result: set[int] = set()
    for item in raw.split(","):
        try:
            port = int(item.strip())
        except (TypeError, ValueError):
            continue
        if 0 < port <= 65535:
            result.add(port)
    return result or set(DEFAULT_ALLOWED_PORTS)


def _private_addresses_allowed() -> bool:
    return str(os.environ.get("FLOWOPS_OUTBOUND_ALLOW_PRIVATE") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def validate_outbound_url(url: str) -> OutboundTarget:
    """Validate a URL and pin it to one checked public address.

    User supplied callbacks and media URLs must not reach loopback, private,
    link-local, multicast or reserved networks.  Redirects are intentionally
    not followed by :func:`post_json`; callers that use another client should
    disable redirects as well.
    """
    raw = str(url or "").strip()
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise OutboundURLError("出站地址为空或过长")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise OutboundURLError("出站地址包含控制字符")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise OutboundURLError("出站地址端口不合法") from exc
    scheme = str(parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise OutboundURLError("出站地址只允许 http/https")
    if parsed.username is not None or parsed.password is not None:
        raise OutboundURLError("出站地址不能包含用户名或密码")
    hostname = str(parsed.hostname or "").strip().rstrip(".").lower()
    if not hostname:
        raise OutboundURLError("出站地址缺少主机名")
    port = int(port or (443 if scheme == "https" else 80))
    if port not in _allowed_ports():
        raise OutboundURLError("出站地址使用了未授权端口")

    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise OutboundURLError("出站地址无法解析") from exc
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        if address and address not in addresses:
            addresses.append(address)
    if not addresses:
        raise OutboundURLError("出站地址没有可用 IP")
    allow_private = _private_addresses_allowed()
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise OutboundURLError("出站地址解析结果不合法") from exc
        if not allow_private and not ip.is_global:
            raise OutboundURLError("出站地址指向内网、回环或保留网络")

    path = parsed.path or "/"
    request_target = path + (("?" + parsed.query) if parsed.query else "")
    return OutboundTarget(
        url=raw,
        scheme=scheme,
        hostname=hostname,
        port=port,
        address=addresses[0],
        request_target=request_target,
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a checked IP while keeping the original TLS hostname."""

    def __init__(self, target: OutboundTarget, timeout: float):
        super().__init__(target.address, target.port, timeout=timeout, context=ssl.create_default_context())
        self._flowops_target = target

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._flowops_target.address, self._flowops_target.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(
            sock,
            server_hostname=self._flowops_target.hostname,
        )


def post_body(
    url: str,
    body: bytes,
    *,
    timeout: float = 10.0,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, bytes]:
    """POST a bounded body without redirects or a second DNS lookup."""
    target = validate_outbound_url(url)
    body = bytes(body)
    request_headers = {
        "Content-Type": "application/json",
        "User-Agent": "FlowOps-Outbound/1.0",
        "Content-Length": str(len(body)),
        "Host": target.host_header,
    }
    request_headers.update({str(k): str(v) for k, v in (headers or {}).items()})
    timeout_value = max(0.5, min(float(timeout), 60.0))
    if target.scheme == "https":
        connection: http.client.HTTPConnection = _PinnedHTTPSConnection(target, timeout_value)
    else:
        connection = http.client.HTTPConnection(target.address, target.port, timeout=timeout_value)
    try:
        connection.request("POST", target.request_target, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise RuntimeError("出站响应体过大")
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}")
        return response.status, data
    finally:
        connection.close()


def post_json(
    url: str,
    payload: Mapping[str, Any] | list[Any],
    *,
    timeout: float = 10.0,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, bytes]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return post_body(url, body, timeout=timeout, headers=headers)
