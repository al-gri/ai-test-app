"""Defer optional telemetry until the external result is durably recorded.

Only metadata created by RpcClient is buffered, never arguments/response bodies.
Outbox delivery is at least once: exporters deduplicate telemetry_id if needed.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import time
from .background import submit
from .redaction import Redactor

_buffer = ContextVar('adaptive_team_rpc_telemetry', default=None)


@contextmanager
def deferred_telemetry():
    events = []
    token = _buffer.set(events)
    try:
        yield events
    finally:
        _buffer.reset(token)


def record(client, event):
    # Snapshot metadata and occurrence time before handing it to another thread.
    try:
        event = Redactor().clean(copy.deepcopy(event))
    except Exception:
        client.audit_failures += 1
        return
    event.setdefault('at', time.time())
    events = _buffer.get()
    if events is not None:
        # MCP has bounded discovery pages; still bound this independent queue.
        if len(events) < 256:
            events.append(event)
        else:
            client.audit_failures += 1
        return
    # Bare RpcClient has no durable operation journal. Its optional exporter is
    # best effort; callers can inspect this counter, never raw exception secrets.
    if client.audit is not None:
        callback = client.audit
        def deliver():
            try:
                callback(event)
            except Exception:
                client.audit_failures += 1
        if not submit(deliver):
            client.audit_failures += 1
