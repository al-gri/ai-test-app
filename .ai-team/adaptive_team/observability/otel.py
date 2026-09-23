"""Optional OpenTelemetry SDK bridge with bounded, asynchronous OTLP exporters.

Only sanitized allowlisted metadata enters the SDK. No global logging handler or
automatic exception recording can accidentally export prompts, bodies or keys.
Owner startup installs one runtime per process. No telemetry failure authorizes
a retry of an external operation or changes its result.
"""
from __future__ import annotations
import contextvars
import functools
import inspect
import os
import re
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit
from .redaction import Redactor
from ..models import PolicyError, digest
from .. import __version__

_runtime=None
_forced_trace=contextvars.ContextVar('ai_otel_root_trace',default=None)
_current=contextvars.ContextVar('ai_otel_context',default=None)
ALLOWED=frozenset({'task','role','phase','lease_id','generation','prompt_digest','input_digest',
    'subject_digest','artifact_digest','commit','method','outcome','error_type','exit_code',
    'timed_out','output_limited','elapsed_ms','summary_digest','summary_chars','request_id',
    'operation','run_id','telemetry_id'})


def current_context():
    return _current.get()


def extract_parent(carrier,*,expected_trace_id):
    """Trusted worker ingress only. W3C context is correlation, never permission.

    Authenticate the task/lease separately and compare its assigned trace ID.
    Deliberately do not propagate arbitrary baggage or remote response headers.
    """
    from .tracing import TraceContext
    parsed=TraceContext.from_traceparent(carrier.get('traceparent'))
    if parsed.trace_id!=expected_trace_id:raise PolicyError('Trace differs from assigned task')
    return TraceContext(parsed.trace_id,parsed.parent_span_id)


def configure(runtime):
    """Trusted startup/shutdown configuration, not per-request model input."""
    global _runtime
    previous,_runtime=_runtime,runtime
    return previous


def _endpoint(value):
    parts=urlsplit(value)
    if (parts.scheme not in {'http','https'} or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise PolicyError('Use an explicit OTLP endpoint without URL credentials')
    if parts.scheme=='http' and parts.hostname not in {'127.0.0.1','localhost','::1'}:
        raise PolicyError('Remote OTLP export requires HTTPS')
    return value


class Telemetry:
    def __init__(self, *, service_name='adaptive-ai-team', traces_endpoint=None,
                 logs_endpoint=None, redactor=None, span_exporter=None, log_exporter=None):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        self.redactor=redactor or Redactor()
        self.failures=0;self._closed=False
        if not re.fullmatch('[a-zA-Z0-9._-]{1,80}',service_name):raise PolicyError('Invalid telemetry service name')
        class BoundIds(RandomIdGenerator):
            def generate_trace_id(self):
                return _forced_trace.get() or super().generate_trace_id()
        # Do not ingest arbitrary OTEL_RESOURCE_ATTRIBUTES or process environment.
        resource=Resource({'service.name':service_name,'service.version':__version__})
        # Do not register global providers: other embedded applications keep theirs.
        self.provider=TracerProvider(resource=resource,id_generator=BoundIds(),shutdown_on_exit=False)
        self.logger_provider=LoggerProvider(resource=resource,shutdown_on_exit=False)
        if span_exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            span_exporter=OTLPSpanExporter(endpoint=_endpoint(traces_endpoint or 'http://127.0.0.1:4318/v1/traces'),timeout=2)
        self.provider.add_span_processor(BatchSpanProcessor(span_exporter,max_queue_size=2048,
            max_export_batch_size=128,schedule_delay_millis=500,export_timeout_millis=2000))
        if logs_endpoint or log_exporter is not None:
            if log_exporter is None:
                from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
                log_exporter=OTLPLogExporter(endpoint=_endpoint(logs_endpoint),timeout=2)
            self.logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter,
                max_queue_size=2048,max_export_batch_size=128,schedule_delay_millis=500,
                export_timeout_millis=2000))
        self.tracer=self.provider.get_tracer('adaptive_team', __version__)
        self.logger=self.logger_provider.get_logger('adaptive_team',__version__)

    @classmethod
    def from_environment(cls):
        base=os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT','http://127.0.0.1:4318').rstrip('/')
        return cls(service_name=os.environ.get('OTEL_SERVICE_NAME','adaptive-ai-team'),
            traces_endpoint=os.environ.get('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT',base+'/v1/traces'),
            logs_endpoint=os.environ.get('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT'))

    def safe(self,attributes):
        # Unknown metadata is excluded, rather than trusting heuristic recognition
        # of arbitrary secrets. Known values are still redacted before any processor.
        data=self.redactor.clean({k:v for k,v in (attributes or {}).items() if k in ALLOWED})
        return {k:v for k,v in data.items() if type(v) in (str,int,float,bool)}

    @contextmanager
    def span(self,name,trace_id=None,attributes=None,parent=None):
        from opentelemetry import trace, context as sdk_context
        from opentelemetry.context import Context
        from .tracing import TraceContext
        span=None;attached=None;token=None;forced=None;child=None
        try:
            if self._closed:raise RuntimeError('Telemetry closed')
            # Names are fixed by trusted integration code, never a raw prompt/URL.
            name=self.redactor.text(name)[:120]
            if trace_id is not None:TraceContext.task(trace_id)
            existing=current_context()
            parent=existing if existing and (trace_id is None or existing.trace_id==trace_id) else parent
            ctx=Context()
            if parent is not None:
                parent_span=trace.NonRecordingSpan(trace.SpanContext(int(parent.trace_id,16),int(parent.span_id,16),
                    is_remote=True,trace_flags=trace.TraceFlags(1)))
                ctx=trace.set_span_in_context(parent_span,ctx)
            forced=_forced_trace.set(int(trace_id,16) if trace_id else None)
            span=self.tracer.start_span(name,context=ctx,attributes=self.safe(attributes),
                record_exception=False,set_status_on_exception=False)
            identity=span.get_span_context()
            child=TraceContext(f'{identity.trace_id:032x}',f'{identity.span_id:016x}',parent.span_id if parent else None)
            token=_current.set(child)
            attached=sdk_context.attach(trace.set_span_in_context(span,ctx))
        except Exception:
            self.failures+=1
        try:
            yield child
        except BaseException:
            if span is not None:
                try:span.set_status(trace.StatusCode.ERROR)  # No message or stack.
                except Exception:self.failures+=1
            raise
        finally:
            if attached is not None:
                try:sdk_context.detach(attached)
                except Exception:self.failures+=1
            if token is not None:_current.reset(token)
            if forced is not None:_forced_trace.reset(forced)
            if span is not None:
                try:span.end()
                except Exception:self.failures+=1

    def event(self,name,attributes=None,context=None):
        from opentelemetry import trace
        from opentelemetry._logs import LogRecord
        try:
            name=self.redactor.text(name)[:120];safe=self.safe(attributes)
            active=current_context()
            if (active is not None and trace.get_current_span().is_recording()
                    and (context is None or active.trace_id==context.trace_id)):
                trace.get_current_span().add_event(name,safe)
                self.logger.emit(LogRecord(body=name,attributes=safe))
            else:
                # A queued callback may run after its captured parent ended.
                # Start a correlated child; adding an event to an ended span
                # would silently discard it in the SDK.
                parent=context or active
                with self.span(name,parent.trace_id if parent else None,safe,parent=parent):
                    self.logger.emit(LogRecord(body=name,attributes=safe))
        except Exception:self.failures+=1

    def summary(self,summary,*,role=None):
        """Record evidence that a public decision summary exists, not hidden thought.

        Full prompts, private chain-of-thought and model bodies are never exported.
        Store approved human-readable artifacts in the encrypted journal instead.
        """
        if not isinstance(summary,str):raise PolicyError('Summary must be text')
        self.event('agent.decision_summary',{'summary_digest':digest(summary),
            'summary_chars':len(summary),'role':role})

    def flush(self,timeout_ms=2000):
        try:
            return self.provider.force_flush(timeout_millis=timeout_ms) and self.logger_provider.force_flush(timeout_millis=timeout_ms)
        except Exception:
            self.failures+=1;return False

    def close(self,timeout=3):
        """Bound owner shutdown; an unresponsive exporter never holds work forever."""
        self._closed=True
        def shutdown():
            try:self.provider.shutdown();self.logger_provider.shutdown()
            except Exception:self.failures+=1
        thread=threading.Thread(target=shutdown,daemon=True,name='telemetry-shutdown')
        thread.start();thread.join(timeout)
        return not thread.is_alive()


@contextmanager
def operation(name,trace_id=None,attributes=None,parent=None):
    runtime=_runtime
    if runtime is None:
        yield None
    else:
        with runtime.span(name,trace_id,attributes,parent) as context:yield context


def event(name,attributes=None,context=None):
    if _runtime is not None:_runtime.event(name,attributes,context)


def instrument(name,selector):
    """Selectors use authenticated task metadata; ContextVars isolate async calls."""
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def wrapped(*args,**kwargs):
                with operation(name,**selector(*args,**kwargs)):
                    return await function(*args,**kwargs)
        else:
            @functools.wraps(function)
            def wrapped(*args,**kwargs):
                with operation(name,**selector(*args,**kwargs)):
                    return function(*args,**kwargs)
        return wrapped
    return decorate
