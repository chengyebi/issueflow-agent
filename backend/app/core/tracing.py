from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Protocol

from app.core.sanitization import sanitize_error_message


class TraceRecorder(Protocol):
    def start_node(self, node_name: str, input_summary: dict) -> int | None: ...

    def finish_node(
        self,
        span_id: int | None,
        duration_ms: int,
        output_summary: dict,
        input_tokens: int,
        output_tokens: int,
    ) -> None: ...

    def fail_node(
        self,
        span_id: int | None,
        duration_ms: int,
        error_type: str,
        error_message: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None: ...


class NullTraceRecorder:
    def start_node(self, node_name: str, input_summary: dict) -> None:
        return None

    def finish_node(self, *args, **kwargs) -> None:
        return None

    def fail_node(self, *args, **kwargs) -> None:
        return None


@dataclass
class TraceSession:
    trace_id: str
    recorder: TraceRecorder = field(default_factory=NullTraceRecorder)
    input_tokens: int = 0
    output_tokens: int = 0
    structured_output_success: bool = True
    _current_node_usage: list[int] | None = None

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        safe_input = max(0, int(input_tokens or 0))
        safe_output = max(0, int(output_tokens or 0))
        self.input_tokens += safe_input
        self.output_tokens += safe_output
        if self._current_node_usage is not None:
            self._current_node_usage[0] += safe_input
            self._current_node_usage[1] += safe_output


_current_trace: ContextVar[TraceSession | None] = ContextVar(
    "issueflow_current_trace", default=None
)


@contextmanager
def use_trace(trace: TraceSession | None):
    token = _current_trace.set(trace)
    try:
        yield trace
    finally:
        _current_trace.reset(token)


def current_trace() -> TraceSession | None:
    return _current_trace.get()


def summarize_input(state: dict) -> dict:
    summary: dict[str, Any] = {"fields": sorted(state.keys())}
    for key in ("title", "body"):
        value = state.get(key)
        if isinstance(value, str):
            summary[f"{key}_chars"] = len(value)
    if "repo" in state:
        summary["repo_present"] = bool(state.get("repo"))
    return summary


def summarize_output(output: dict) -> dict:
    summary: dict[str, Any] = {"fields": sorted(output.keys())}
    for key in ("category", "priority", "risk_level", "status", "confidence"):
        if key in output:
            summary[key] = output[key]
    if isinstance(output.get("missing_repro_fields"), list):
        summary["missing_repro_field_count"] = len(output["missing_repro_fields"])
    if isinstance(output.get("proposed_actions"), list):
        summary["action_types"] = [
            item.get("type") for item in output["proposed_actions"] if isinstance(item, dict)
        ]
    for key in ("summary", "suggested_reply"):
        if isinstance(output.get(key), str):
            summary[f"{key}_chars"] = len(output[key])
    return summary


def trace_node(node_name: str):
    def decorator(function):
        @wraps(function)
        def wrapped(state: dict, *args, **kwargs):
            trace = current_trace()
            if trace is None:
                return function(state, *args, **kwargs)
            usage = [0, 0]
            previous_usage = trace._current_node_usage
            trace._current_node_usage = usage
            started = time.perf_counter()
            span_id = trace.recorder.start_node(node_name, summarize_input(state))
            try:
                output = function(state, *args, **kwargs)
                duration_ms = round((time.perf_counter() - started) * 1000)
                trace.recorder.finish_node(
                    span_id,
                    duration_ms,
                    summarize_output(output),
                    usage[0],
                    usage[1],
                )
                return output
            except Exception as exc:
                duration_ms = round((time.perf_counter() - started) * 1000)
                trace.recorder.fail_node(
                    span_id,
                    duration_ms,
                    type(exc).__name__,
                    sanitize_error_message(exc),
                    usage[0],
                    usage[1],
                )
                raise
            finally:
                trace._current_node_usage = previous_usage

        return wrapped

    return decorator
