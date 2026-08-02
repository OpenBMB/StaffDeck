from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.db.models import AgentEvent


@dataclass(frozen=True)
class _ModelSpan:
    operation: str
    started_ms: float
    finished_ms: float
    duration_ms: float


@dataclass
class _LineObservation:
    running_ms: list[float]
    terminal_ms: list[float]

    @property
    def all_ms(self) -> list[float]:
        return [*self.running_ms, *self.terminal_ms]


def enrich_turn_traces_with_timings(
    traces: list[dict[str, Any]],
    events: list[AgentEvent],
) -> list[dict[str, Any]]:
    """Attach durable total/model timings to enterprise conversation traces."""
    aliases = _turn_aliases(events)
    spans_by_turn = _model_spans_by_turn(events, aliases)
    observations_by_turn = _trace_observations_by_turn(events, aliases)
    windows_by_turn = _resolve_trace_windows(traces, observations_by_turn)
    _extend_action_windows_with_model_decisions(events, aliases, spans_by_turn, windows_by_turn)
    _extend_router_windows_with_planner(
        spans_by_turn,
        windows_by_turn,
        observations_by_turn,
    )

    for trace in traces:
        turn_id = str(trace.get("turn_id") or "").strip()
        spans = spans_by_turn.get(turn_id, [])
        windows = windows_by_turn.get(turn_id, {})
        lines = trace.get("lines") if isinstance(trace.get("lines"), list) else []

        response_spans = [
            span
            for span in spans
            if span.operation in {"response.generate", "response.generate_stream"}
        ]
        if response_spans and not any(line.get("id") == "response_generation" for line in lines):
            response_started = min(span.started_ms for span in response_spans)
            response_finished = max(span.finished_ms for span in response_spans)
            windows["response_generation"] = (response_started, response_finished)
            lines.append(
                {
                    "id": "response_generation",
                    "kind": "decision",
                    "text": "生成最终回复",
                    "detail": f"模型调用 {len(response_spans)} 次",
                    "state": "completed",
                }
            )

        for line in lines:
            line_id = str(line.get("id") or "")
            window = windows.get(line_id)
            if not window:
                continue
            started_ms, finished_ms = window
            if finished_ms < started_ms:
                continue
            line["duration_ms"] = round(max(0.0, finished_ms - started_ms), 3)
            line["model_duration_ms"] = _model_duration_in_window(
                spans,
                started_ms,
                finished_ms,
            )

        trace_started = _iso_ms(trace.get("started_at"))
        trace_finished = _iso_ms(trace.get("completed_at"))
        if trace_started is not None and trace_finished is not None:
            trace["duration_ms"] = round(max(0.0, trace_finished - trace_started), 3)
            trace["model_duration_ms"] = _model_duration_in_window(
                spans,
                trace_started,
                trace_finished,
            )
            trace["model_call_count"] = sum(
                1
                for span in spans
                if span.finished_ms >= trace_started - 1
                and span.started_ms <= trace_finished + 1
            )
    return traces


def _turn_aliases(events: list[AgentEvent]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for event in events:
        payload = event.payload_json or {}
        canonical = str(payload.get("user_message_id") or "").strip()
        if event.event_type == "user_message_received":
            canonical = str(payload.get("message_id") or canonical).strip()
        if not canonical:
            continue
        aliases[canonical] = canonical
        client_turn_id = str(payload.get("client_turn_id") or "").strip()
        if client_turn_id:
            aliases[client_turn_id] = canonical
    return aliases


def _event_turn_id(event: AgentEvent, aliases: dict[str, str]) -> str:
    payload = event.payload_json or {}
    raw = str(
        payload.get("user_message_id")
        or payload.get("turn_id")
        or (payload.get("message_id") if event.event_type == "user_message_received" else "")
        or ""
    ).strip()
    return aliases.get(raw, raw)


def _model_spans_by_turn(
    events: list[AgentEvent],
    aliases: dict[str, str],
) -> dict[str, list[_ModelSpan]]:
    spans_by_turn: dict[str, list[_ModelSpan]] = {}
    for event in events:
        if event.event_type != "llm_call_finished":
            continue
        turn_id = _event_turn_id(event, aliases)
        if not turn_id:
            continue
        payload = event.payload_json or {}
        finished_ms = _event_ms(event)
        duration_ms = _float_ms(payload.get("duration_ms"))
        started_ms = _iso_ms(payload.get("started_at"))
        if started_ms is None:
            started_ms = finished_ms - duration_ms
        spans_by_turn.setdefault(turn_id, []).append(
            _ModelSpan(
                operation=str(payload.get("operation") or "llm.request"),
                started_ms=started_ms,
                finished_ms=finished_ms,
                duration_ms=duration_ms or max(0.0, finished_ms - started_ms),
            )
        )
    for spans in spans_by_turn.values():
        spans.sort(key=lambda span: (span.finished_ms, span.started_ms))
    return spans_by_turn


def _trace_observations_by_turn(
    events: list[AgentEvent],
    aliases: dict[str, str],
) -> dict[str, dict[str, _LineObservation]]:
    # Keep the timing projection coupled to the same trace-line formatter that
    # produces the visible log. This avoids a second, inevitably drifting list
    # of event-type-to-line-id mappings.
    from app.api.chat import _event_trace_lines

    observations_by_turn: dict[str, dict[str, _LineObservation]] = {}
    skill_hints_by_turn: dict[str, str | None] = {}
    for event in events:
        turn_id = _event_turn_id(event, aliases)
        if not turn_id:
            continue
        payload = event.payload_json or {}
        if event.event_type == "router_decision_created":
            target_skill_id = str(payload.get("target_skill_id") or "").strip()
            if target_skill_id:
                skill_hints_by_turn[turn_id] = target_skill_id
        lines = _event_trace_lines(
            event,
            {},
            skill_hints_by_turn.get(turn_id),
        )
        if not lines:
            continue
        event_ms = _event_ms(event)
        turn_observations = observations_by_turn.setdefault(turn_id, {})
        for line in lines:
            line_id = str(line.get("id") or "").strip()
            if not line_id:
                continue
            observation = turn_observations.setdefault(
                line_id,
                _LineObservation(running_ms=[], terminal_ms=[]),
            )
            if line.get("state") == "running":
                observation.running_ms.append(event_ms)
            else:
                observation.terminal_ms.append(event_ms)
    return observations_by_turn


def _resolve_trace_windows(
    traces: list[dict[str, Any]],
    observations_by_turn: dict[str, dict[str, _LineObservation]],
) -> dict[str, dict[str, tuple[float, float]]]:
    windows_by_turn: dict[str, dict[str, tuple[float, float]]] = {}
    for trace in traces:
        turn_id = str(trace.get("turn_id") or "").strip()
        observations = observations_by_turn.get(turn_id, {})
        lines = trace.get("lines") if isinstance(trace.get("lines"), list) else []
        trace_started = _iso_ms(trace.get("started_at"))
        trace_finished = _iso_ms(trace.get("completed_at"))
        line_observations = [observations.get(str(line.get("id") or "")) for line in lines]

        windows: dict[str, tuple[float, float]] = {}
        for index, line in enumerate(lines):
            line_id = str(line.get("id") or "").strip()
            observation = line_observations[index]
            if not line_id or not observation or not observation.all_ms:
                continue

            if observation.running_ms:
                started_ms = min(observation.running_ms)
            else:
                started_ms = _previous_observation_ms(
                    line_observations,
                    index,
                    before_ms=max(observation.terminal_ms),
                )
                if started_ms is None:
                    started_ms = trace_started

            if observation.terminal_ms:
                finished_ms = max(observation.terminal_ms)
            else:
                finished_ms = _next_observation_ms(
                    line_observations,
                    index,
                    after_ms=min(observation.running_ms),
                )
                if finished_ms is None:
                    finished_ms = trace_finished

            if started_ms is None or finished_ms is None:
                continue
            windows[line_id] = (min(started_ms, finished_ms), max(started_ms, finished_ms))
        windows_by_turn[turn_id] = windows
    return windows_by_turn


def _previous_observation_ms(
    observations: list[_LineObservation | None],
    index: int,
    *,
    before_ms: float,
) -> float | None:
    for candidate in reversed(observations[:index]):
        if not candidate or not candidate.all_ms:
            continue
        eligible = [value for value in candidate.all_ms if value <= before_ms]
        if eligible:
            return max(eligible)
    return None


def _next_observation_ms(
    observations: list[_LineObservation | None],
    index: int,
    *,
    after_ms: float,
) -> float | None:
    for candidate in observations[index + 1 :]:
        if not candidate or not candidate.all_ms:
            continue
        eligible = [value for value in candidate.all_ms if value >= after_ms]
        if eligible:
            return min(eligible)
    return None


def _extend_action_windows_with_model_decisions(
    events: list[AgentEvent],
    aliases: dict[str, str],
    spans_by_turn: dict[str, list[_ModelSpan]],
    windows_by_turn: dict[str, dict[str, tuple[float, float]]],
) -> None:
    actions_by_turn: dict[str, list[tuple[float, str]]] = {}
    for event in events:
        if event.event_type != "harness_action_created":
            continue
        turn_id = _event_turn_id(event, aliases)
        line_id = _trace_line_id(event)
        if turn_id and line_id:
            actions_by_turn.setdefault(turn_id, []).append((_event_ms(event), line_id))

    for turn_id, actions in actions_by_turn.items():
        available = [
            span
            for span in spans_by_turn.get(turn_id, [])
            if span.operation == "harness.task_action"
        ]
        used: set[int] = set()
        for action_ms, line_id in sorted(actions):
            candidates = [
                (index, span)
                for index, span in enumerate(available)
                if index not in used and span.finished_ms <= action_ms + 250
            ]
            if not candidates:
                continue
            index, span = max(candidates, key=lambda item: item[1].finished_ms)
            used.add(index)
            current = windows_by_turn.setdefault(turn_id, {}).get(line_id)
            if current:
                windows_by_turn[turn_id][line_id] = (
                    span.started_ms
                    if line_id.startswith("harness_finish_")
                    else min(span.started_ms, current[0]),
                    current[1],
                )


def _extend_router_windows_with_planner(
    spans_by_turn: dict[str, list[_ModelSpan]],
    windows_by_turn: dict[str, dict[str, tuple[float, float]]],
    observations_by_turn: dict[str, dict[str, _LineObservation]],
) -> None:
    for turn_id, windows in windows_by_turn.items():
        current = windows.get("decision_router")
        if not current:
            continue
        planner_spans = [
            span
            for span in spans_by_turn.get(turn_id, [])
            if span.operation in {"turn_planner.plan", "router.scene"}
            and span.finished_ms <= current[1] + 250
        ]
        if planner_spans:
            planner = max(planner_spans, key=lambda span: span.finished_ms)
            observation = observations_by_turn.get(turn_id, {}).get("decision_router")
            started_ms = (
                min(planner.started_ms, current[0])
                if observation and observation.running_ms
                else planner.started_ms
            )
            windows["decision_router"] = (started_ms, current[1])


def _trace_line_id(event: AgentEvent) -> str:
    payload = event.payload_json or {}
    event_type = event.event_type
    frame_id = str(payload.get("task_frame_id") or event.id).strip()
    iteration = str(payload.get("iteration") or "").strip()
    if event_type in {"task_frame_started", "task_frame_finished"}:
        return f"harness_frame_{frame_id}"
    if event_type == "harness_action_created":
        action = str(payload.get("action") or "").strip()
        if action == "tool":
            return f"harness_action_{frame_id}_{iteration or event.id}"
        if action == "finish":
            return f"harness_finish_{frame_id}_{iteration or event.id}"
    if event_type == "harness_tool_completed":
        return f"harness_action_{frame_id}_{iteration or event.id}"
    if event_type == "router_decision_created":
        return "decision_router"
    if event_type in {"knowledge_query_started", "knowledge_query_finished", "knowledge_result"}:
        query = payload.get("query")
        if isinstance(query, dict):
            query = query.get("query")
        query_text = " ".join(str(query or payload.get("text") or "").split())
        return f"knowledge_lookup_{query_text}" if query_text else "knowledge_lookup"
    if event_type in {"tool_call_started", "tool_call_finished"}:
        tool_name = str(payload.get("tool_name") or payload.get("name") or "").strip()
        call_id = str(payload.get("tool_call_id") or tool_name or event.id).strip()
        return f"tool_{call_id}"
    if event_type == "tool_result":
        raw_name = str(payload.get("rawToolName") or payload.get("toolId") or "").strip()
        call_id = str(payload.get("toolCallId") or raw_name or event.id).strip()
        return f"tool_{call_id}"
    return ""


def _model_duration_in_window(
    spans: list[_ModelSpan],
    started_ms: float,
    finished_ms: float,
) -> float:
    intervals = sorted(
        (
            max(started_ms, span.started_ms),
            min(finished_ms, span.finished_ms),
        )
        for span in spans
        if span.finished_ms >= started_ms and span.started_ms <= finished_ms
    )
    if not intervals:
        return 0.0

    merged: list[tuple[float, float]] = []
    for interval_started, interval_finished in intervals:
        if interval_finished < interval_started:
            continue
        if not merged or interval_started > merged[-1][1]:
            merged.append((interval_started, interval_finished))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], interval_finished))
    return round(sum(end - start for start, end in merged), 3)


def _event_ms(event: AgentEvent) -> float:
    value = event.created_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp() * 1000


def _iso_ms(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp() * 1000


def _float_ms(value: object) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0
