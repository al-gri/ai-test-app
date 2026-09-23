"""Pinned-endpoint HTTP/SSE transport; HTTPX is an optional protocol dependency.

Each operation owns and closes its client. Instances may therefore be used from
different worker event loops. No shared authentication cookies or mutable session
headers cross workers. There are no redirects, inherited proxies or auto retries.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
from urllib.parse import urlsplit

from ..models import PolicyError
from .jsonrpc import ProtocolError, RequestTimeout, TransportError, HTTPStatusError, WireResponse, decode, encode, finite_timeout

HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def endpoint_url(value: str, *, allow_loopback_http=False) -> str:
    if (not isinstance(value, str) or len(value) > 4096 or not value.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value):
        raise PolicyError("Invalid endpoint URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PolicyError("Invalid endpoint authority") from exc
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise PolicyError("Endpoint requires a host and cannot contain credentials, query or fragment")
    loopback = False
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (allow_loopback_http and parsed.scheme == "http" and loopback):
        raise PolicyError("HTTPS is required; explicit numeric loopback HTTP is for tests only")
    if port is not None and not 1 <= port <= 65535:
        raise PolicyError("Invalid endpoint port")
    return value


def _headers(value: dict) -> dict:
    result = {}
    for name, content in value.items():
        if (not isinstance(name, str) or not HEADER.fullmatch(name) or not isinstance(content, str)
                or len(content) > 16384 or not content.isascii()
                or any(ord(c) < 32 or ord(c) == 127 for c in content)):
            raise PolicyError("Unsafe HTTP header")
        key = name.lower()
        if key in result:
            raise PolicyError("Duplicate case-insensitive HTTP header")
        result[key] = content
    return result


class SSEDecoder:
    """Incremental UTF-8 SSE framing with bounded bytes and event count.

    CR, LF and CRLF are valid line delimiters. Comments and unrelated field names
    do not become instructions. The caller validates every emitted JSON envelope.
    """
    def __init__(self, *, limit=2 * 1024 * 1024):
        self.limit, self.total, self.events = limit, 0, 0
        self.buffer, self.data, self.skip_lf = bytearray(), [], False

    def feed(self, chunk: bytes) -> list[dict]:
        self.total += len(chunk)
        if self.total > self.limit:
            raise ProtocolError("SSE response limit exceeded")
        emitted = []
        for byte in chunk:
            if self.skip_lf:
                self.skip_lf = False
                if byte == 10:
                    continue
            if byte not in (10, 13):
                self.buffer.append(byte)
                if len(self.buffer) > 256 * 1024:
                    raise ProtocolError("SSE line limit exceeded")
                continue
            line = bytes(self.buffer)
            self.buffer.clear()
            self.skip_lf = byte == 13
            if not line:
                if self.data:
                    self.events += 1
                    if self.events > 1000:
                        raise ProtocolError("SSE event limit exceeded")
                    payload = b"\n".join(self.data)
                    # MCP legacy streams may start with an ID-only/empty-data
                    # priming event. It carries no JSON-RPC message to execute.
                    if payload:
                        emitted.append(decode(payload, limit=self.limit))
                    self.data.clear()
            elif not line.startswith(b":"):
                name, _, content = line.partition(b":")
                if name == b"data":
                    self.data.append(content[1:] if content.startswith(b" ") else content)
        return emitted


class HttpTransport:
    handles_action_admission = True
    def __init__(self, endpoint: str, *, allowed_endpoints: frozenset[str],
                 credential_headers=None, allow_loopback_http=False, max_bytes=2 * 1024 * 1024,
                 httpx_transport=None):
        self.endpoint = endpoint_url(endpoint, allow_loopback_http=allow_loopback_http)
        if self.endpoint not in allowed_endpoints:
            raise PolicyError("Endpoint is not explicitly admitted by the owner")
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 16 * 1024 * 1024:
            raise PolicyError("Invalid HTTP byte limit")
        self.max_bytes, self.credentials = max_bytes, credential_headers
        self._test_transport = httpx_transport

    async def exchange(self, message: dict, *, headers: dict, timeout: float) -> WireResponse:
        return await self._request("POST", encode(message), headers, timeout,
                                   expected_id=message.get("id"), notification="id" not in message)

    async def get_json(self, *, timeout=30.0, headers: dict | None = None) -> dict:
        response = await self._request("GET", None, headers or {}, timeout)
        if response.status != 200 or response.message is None:
            raise TransportError(f"Discovery HTTP error ({response.status})")
        return response.message

    async def _request(self, method, body, headers, timeout, *, expected_id=None, notification=False):
        import httpx  # Install adaptive-ai-team[protocols]; the core stays dependency-free.
        finite_timeout(timeout)
        if body is not None and len(body) > self.max_bytes:
            raise ProtocolError("HTTP request limit exceeded")
        supplied = _headers(headers)
        if any(key in supplied for key in ("authorization", "host", "cookie", "content-length", "transfer-encoding")):
            raise PolicyError("Protocol code cannot override authentication or HTTP framing")
        credentials = _headers(self.credentials() if self.credentials else {})
        if set(credentials) - {"authorization"}:
            raise PolicyError("Only owner-supplied Authorization headers are supported")
        outgoing = {"accept": "application/json, text/event-stream", "content-type": "application/json",
                    "accept-encoding": "identity", **supplied, **credentials}
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=timeout,
                    transport=self._test_transport, limits=httpx.Limits(max_connections=4)) as client:
                    from ..security.action_admission import admit_current
                    # Credential resolution and client setup precede admission.
                    # From here the request is in flight, including HTTP pool/DNS.
                    admit_current(self.endpoint, decode(body).get('method', method) if body else method)
                    async with client.stream(method, self.endpoint, content=body, headers=outgoing) as response:
                        status, returned = response.status_code, dict(response.headers)
                        if 300 <= status < 400:
                            raise TransportError("Redirect rejected; endpoint changes need owner approval")
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise ProtocolError("Compressed responses are not admitted")
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        parser, data = SSEDecoder(limit=self.max_bytes), bytearray()
                        async for chunk in response.aiter_raw():
                            if content_type == "text/event-stream" and expected_id:
                                for value in parser.feed(chunk):
                                    if "id" in value:
                                        # Includes wrong IDs: RpcClient must reject them, not wait forever.
                                        return WireResponse(value, returned, status)
                                    if (value.get("jsonrpc") != "2.0" or not isinstance(value.get("method"), str)
                                            or not value["method"].startswith("notifications/")):
                                        raise ProtocolError("Unsupported server-initiated request/event")
                                    # Notifications are informational, never executed as local tools.
                            else:
                                data.extend(chunk)
                                if len(data) > self.max_bytes:
                                    raise ProtocolError("HTTP response limit exceeded")
                        if notification and status == 202 and not data:
                            return WireResponse(None, returned, status)
                        if not 200 <= status < 300 and (not data or content_type != "application/json"):
                            raise HTTPStatusError(status)
                        if content_type != "application/json":
                            raise ProtocolError("Expected a complete JSON response or bounded SSE result")
                        try:
                            value = decode(bytes(data), limit=self.max_bytes)
                        except ProtocolError:
                            if not 200 <= status < 300:
                                raise HTTPStatusError(status) from None
                            raise
                        return WireResponse(value, returned, status)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise RequestTimeout("HTTP deadline exceeded; remote outcome is unknown") from exc
        except httpx.HTTPError as exc:
            raise TransportError("HTTP transport failed; remote outcome is unknown") from exc
