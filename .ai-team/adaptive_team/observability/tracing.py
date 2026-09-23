"""W3C trace context and an append-only, sanitized local SQLite event sink."""
from __future__ import annotations
import json
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from ..models import PolicyError, canonical
from ..security._store import SecurityStore
from .redaction import Redactor


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None

    def __post_init__(self):
        for value, length in ((self.trace_id, 32), (self.span_id, 16)) + (() if self.parent_span_id is None else ((self.parent_span_id, 16),)):
            if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{' + str(length) + '}', value) or set(value) == {'0'}:
                raise PolicyError("Invalid trace/span identity")

    @classmethod
    def task(cls, trace_id):
        return cls(trace_id, uuid.uuid4().hex[:16])

    def child(self):
        return TraceContext(self.trace_id, uuid.uuid4().hex[:16], self.span_id)

    @property
    def traceparent(self):
        return '00-' + self.trace_id + '-' + self.span_id + '-01'

    @classmethod
    def from_traceparent(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r'00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]', value):
            raise PolicyError("Unsupported traceparent")
        _, trace_id, parent, _ = value.split('-')
        return cls(trace_id, uuid.uuid4().hex[:16], parent)


class TraceStore:
    def __init__(self, database, redactor: Redactor | None = None):
        self.store, self.redactor = SecurityStore(database, timeout=0.05), redactor or Redactor()
        self.failures = 0
        with self.store.edit() as db:
            db.execute("CREATE TABLE IF NOT EXISTS trace_events(id TEXT PRIMARY KEY, trace_id TEXT, span_id TEXT, parent TEXT, at REAL, payload TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS trace_lookup ON trace_events(trace_id,at)")

    def emit(self, context: TraceContext, action: str, attributes=None):
        # Optional local/remote telemetry must never replace a successful outcome
        # or prevent sandbox cleanup. Admission/financial journals remain strict.
        try:
            safe = self.redactor.clean({'action':action,'attributes':attributes or {}})
        except Exception:
            self.failures += 1
            return  # Never fall back to unsanitized data after a sanitizer error.
        try:
            self._emit(context, safe['action'], safe['attributes'])
        except Exception:
            self.failures += 1
        from .otel import event
        event(safe['action'], safe['attributes'], context)

    def _emit(self, context: TraceContext, action: str, attributes=None):
        # No raw payload, exception text or label reaches sqlite parameters.
        safe = self.redactor.clean({'action': action, 'attributes': attributes or {}})
        encoded = canonical(safe)
        if len(encoded) > 64000:
            encoded = canonical({'action': self.redactor.text(action), 'attributes': '***'})
        with self.store.edit() as db:
            db.execute("INSERT INTO trace_events VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, context.trace_id,
                context.span_id, context.parent_span_id, time.time(), encoded))

    @contextmanager
    def span(self, context, action, attributes=None):
        from .otel import operation
        try:
            safe = self.redactor.clean({'action':action,'attributes':attributes or {}})
        except Exception:
            self.failures += 1
            safe = {'action':'telemetry.redaction_failed','attributes':{}}
        with operation(safe['action'],context.trace_id,safe['attributes'],parent=context) as exported:
            child = exported or context.child()
            self.emit(child, action + '.start', attributes)
            try:
                yield child
            except BaseException as exc:
                self.emit(child, action + '.error', {'error_type': type(exc).__name__})
                raise
            else:
                self.emit(child, action + '.complete')

    def rpc_audit(self, context):
        def callback(event):
            if event.get('trace_id') != context.trace_id:
                raise PolicyError("RPC audit trace differs from task")
            rpc_context = TraceContext(context.trace_id, event['span_id'], event.get('parent_span_id')) if event.get('span_id') else context
            self.emit(rpc_context, 'rpc.response', event)
        return callback

    def events(self, trace_id, *, limit=1000):
        TraceContext.task(trace_id)
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise PolicyError("Invalid trace query limit")
        with self.store.edit() as db:
            return [dict(row, payload=json.loads(row['payload'])) for row in db.execute(
                "SELECT * FROM trace_events WHERE trace_id=? ORDER BY at,id LIMIT ?", (trace_id, limit))]
