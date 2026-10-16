from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from uuid import uuid4
from typing import Any, Callable

SpanSink = Callable[[str, dict[str, Any]], None]
_sink: ContextVar[SpanSink | None] = ContextVar("km_span_sink", default=None)
_parent: ContextVar[str | None] = ContextVar("km_parent_span", default=None)


@contextmanager
def bind_span_sink(sink: SpanSink | None):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


class Span:
    def __init__(self, prefix: str, operation: str, attributes: dict[str, Any]):
        self.prefix = prefix
        self.started = perf_counter()
        self.attributes = {"span_id": str(uuid4()), "parent_span_id": _parent.get(),
                           "operation": operation, **attributes}
        self.finished = False
        self.emit("started")

    def emit(self, event: str, **attributes: Any):
        sink = _sink.get()
        if sink:
            try:
                sink(f"{self.prefix}_{event}", {**self.attributes, **attributes})
            except Exception:
                pass

    def finish(self, **attributes: Any):
        if not self.finished:
            self.finished = True
            self.emit("finished", duration_ms=(perf_counter() - self.started) * 1000,
                      status="success", **attributes)


@contextmanager
def observed_span(prefix: str, operation: str, **attributes: Any):
    span = Span(prefix, operation, attributes)
    token = _parent.set(span.attributes["span_id"])
    try:
        yield span
    except BaseException as error:
        span.finished = True
        span.emit("failed", duration_ms=(perf_counter() - span.started) * 1000,
                  status="failed", error_type=type(error).__name__)
        raise
    finally:
        span.finish()
        _parent.reset(token)


@contextmanager
def llm_operation(operation: str, **attributes: Any):
    with observed_span("llm_span", operation, **attributes) as span:
        yield span
