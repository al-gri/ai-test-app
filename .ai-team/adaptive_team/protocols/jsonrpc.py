"""Strict JSON-RPC 2.0 envelopes shared by MCP and A2A clients."""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from dataclasses import dataclass
from typing import Protocol, Callable

from ..models import PolicyError
from ..observability.otel import instrument, current_context


class ProtocolError(RuntimeError):
    """Invalid/unexpected wire data; callers must not treat it as success."""


class TransportError(RuntimeError):
    """Connection/HTTP failure. A side-effecting request may already have run."""


class RequestTimeout(TransportError):
    """Deadline elapsed. This is NOT evidence of remote termination."""


class HTTPStatusError(TransportError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"HTTP request rejected ({status})")


class RemoteError(ProtocolError):
    def __init__(self, code: int, message: str, data=None):
        self.code, self.remote_message, self.data = code, message, data
        # Do not accidentally copy credentials or server-controlled text into logs.
        super().__init__(f"Remote JSON-RPC error {code}")


def encode(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Value is not bounded interoperable JSON") from exc


def decode(data: bytes, *, limit: int = 2 * 1024 * 1024) -> dict:
    if not isinstance(data, bytes) or len(data) > limit:
        raise ProtocolError("JSON payload exceeds its byte limit")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError("Duplicate JSON property")
            result[key] = value
        return result
    def constant(_):
        raise ProtocolError("Non-finite JSON number")
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)
        if not isinstance(result, dict):
            raise ProtocolError("A single JSON object is required; JSON-RPC batches are unsupported")
        encode(result)  # Also reject float overflow (e.g. 1e999) and invalid Unicode.
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid UTF-8 JSON object") from exc


def finite_timeout(value: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
        raise PolicyError("Request timeout must be finite and between 0 and 300 seconds")
    return float(value)


def text(value, name: str, *, limit=4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise ProtocolError(f"Invalid {name}")
    return value


@dataclass(frozen=True)
class WireResponse:
    message: dict | None
    headers: dict
    status: int = 200


class Transport(Protocol):
    async def exchange(self, message: dict, *, headers: dict, timeout: float) -> WireResponse: ...


def response_result(message: dict | None, request_id: str):
    if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
            or type(message.get("id")) is not str or message["id"] != request_id
            or ("result" in message) == ("error" in message)
            or "method" in message):
        raise ProtocolError("JSON-RPC version, ID or response envelope mismatch")
    if "error" in message:
        error = message["error"]
        if not isinstance(error, dict) or type(error.get("code")) is not int:
            raise ProtocolError("Malformed JSON-RPC error")
        raise RemoteError(error["code"], text(error.get("message"), "error message"), error.get("data"))
    if not isinstance(message["result"], dict):
        raise ProtocolError("This protocol operation requires an object result")
    return message["result"]


class RpcClient:
    def __init__(self, transport: Transport, *, timeout=30.0, trace_id: str = "unscoped",
                 audit: Callable[[dict], None] | None = None, trace_context=None):
        self.transport, self.timeout = transport, finite_timeout(timeout)
        self.trace_id = text(trace_id, "trace ID", limit=200)
        self.audit = audit
        self.audit_failures = 0
        if trace_context is not None and trace_context.trace_id != self.trace_id:
            raise PolicyError("Trace context differs from RPC task identity")
        self.trace_context = trace_context

    @instrument('rpc.request', lambda self,method,params,**kw: dict(
        trace_id=self.trace_id if len(self.trace_id)==32 else None,
        parent=self.trace_context,attributes={'method':method}))
    async def request(self, method: str, params: dict, *, headers: dict | None = None) -> WireResponse:
        request_id = uuid.uuid4().hex
        message = {"jsonrpc": "2.0", "id": request_id, "method": text(method, "method"), "params": params}
        started, outcome = time.monotonic(), "error"
        context = current_context() or (self.trace_context.child() if self.trace_context is not None else None)
        headers = dict(headers or {})
        if context is not None:
            if any(key.lower() == 'traceparent' for key in headers):
                raise PolicyError("Protocol caller cannot override the trusted trace context")
            headers['traceparent'] = context.traceparent
        try:
            # The outer deadline includes pool wait, DNS, connect and the complete
            # response. No transport error retries a possibly side-effecting call.
            async with asyncio.timeout(self.timeout):
                if not getattr(self.transport, 'handles_action_admission', False):
                    from ..security.action_admission import admit_current
                    admit_current(getattr(self.transport, 'endpoint', 'owner-adapter'), method)
                wire = await self.transport.exchange(message, headers=headers or {}, timeout=self.timeout)
            if wire.message is None and not 200 <= wire.status < 300:
                raise HTTPStatusError(wire.status)
            try:
                response_result(wire.message, request_id)
            except RemoteError as exc:
                exc.status = wire.status
                raise
            except ProtocolError:
                if not 200 <= wire.status < 300:
                    raise HTTPStatusError(wire.status) from None
                raise
            if not 200 <= wire.status < 300:
                raise HTTPStatusError(wire.status)
            outcome = "success"
            return wire
        except TimeoutError as exc:
            outcome = "timeout_unknown_remote_outcome"
            raise RequestTimeout("Request deadline exceeded; reconcile remote outcome") from exc
        finally:
            from ..observability.rpc_outbox import record
            record(self, {"trace_id": self.trace_id, "request_id": request_id, "method": method,
                            "span_id": context.span_id if context else None,
                            "parent_span_id": context.parent_span_id if context else None,
                            "outcome": outcome, "elapsed_ms": round((time.monotonic() - started) * 1000)})

    @instrument('rpc.notification', lambda self,method,**kw: dict(
        trace_id=self.trace_id if len(self.trace_id)==32 else None,
        parent=self.trace_context,attributes={'method':method}))
    async def notify(self, method: str, *, headers: dict | None = None) -> None:
        method = text(method, "method")
        context = current_context() or (self.trace_context.child() if self.trace_context is not None else None)
        headers = dict(headers or {})
        if context is not None:
            if any(key.lower() == 'traceparent' for key in headers):
                raise PolicyError("Protocol caller cannot override the trusted trace context")
            headers['traceparent'] = context.traceparent
        started, outcome = time.monotonic(), 'error'
        try:
            async with asyncio.timeout(self.timeout):
                if not getattr(self.transport, 'handles_action_admission', False):
                    from ..security.action_admission import admit_current
                    admit_current(getattr(self.transport, 'endpoint', 'owner-adapter'), method)
                wire = await self.transport.exchange({"jsonrpc": "2.0", "method": method},
                                                      headers=headers or {}, timeout=self.timeout)
            if wire.status != 202 or wire.message is not None:
                raise ProtocolError("Notification requires an empty HTTP 202 acknowledgment")
            outcome = 'success'
        except TimeoutError as exc:
            outcome = 'timeout_unknown_remote_outcome'
            raise RequestTimeout("Notification deadline exceeded") from exc
        finally:
            from ..observability.rpc_outbox import record
            record(self, {'trace_id': self.trace_id, 'method': method,
                            'span_id': context.span_id if context else None,
                            'parent_span_id': context.parent_span_id if context else None,
                            'outcome': outcome, 'notification': True,
                            'elapsed_ms': round((time.monotonic() - started) * 1000)})
