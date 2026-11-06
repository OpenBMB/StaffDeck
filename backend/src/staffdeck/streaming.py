from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator

from .errors import ProtocolError
from .models import RunEvent

_MAX_EVENT_CHARS = 1_048_576


class IncompleteEvent(Exception):
    """A connection ended mid-frame; resume from the previous delivered ID."""


def is_sequence_id(value: str | None) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= 19
        and value.isascii() and value.isdecimal() and int(value) <= 2**63 - 1
    )


def iter_sse_lines(
    chunks: Iterable[str], *, max_line_chars: int = _MAX_EVENT_CHARS,
) -> Iterator[str]:
    """Split only CR, LF and CRLF, leaving Unicode separators inside JSON intact."""
    parts: list[str] = []
    size = 0
    skip_lf = False
    for chunk in chunks:
        if not chunk:
            continue
        if skip_lf:
            chunk = chunk.removeprefix("\n")
        skip_lf = chunk.endswith("\r")
        start = 0
        for match in re.finditer(r"\r\n|\r|\n", chunk):
            size += match.start() - start
            if size > max_line_chars:
                raise ProtocolError("StaffDeck event line exceeded the size limit.")
            parts.append(chunk[start:match.start()])
            yield "".join(parts)
            parts.clear()
            size = 0
            start = match.end()
        if start < len(chunk):
            size += len(chunk) - start
            if size > max_line_chars:
                raise ProtocolError("StaffDeck event line exceeded the size limit.")
            parts.append(chunk[start:])
    if parts:
        yield "".join(parts)


def parse_events(
    lines: Iterable[str], *, max_event_chars: int = _MAX_EVENT_CHARS,
) -> Iterator[RunEvent]:
    """Parse complete SSE frames, ignoring comments and detecting truncated frames.

    StaffDeck persists numeric sequence IDs on every public event. Requiring them
    lets the consumer resume safely rather than silently skipping untracked data.
    """
    data: list[str] = []
    event = "message"
    event_id: str | None = None
    size = 0
    for index, line in enumerate(lines):
        if index == 0:
            line = line.removeprefix("\ufeff")
        if not line:
            if data:
                if not is_sequence_id(event_id):
                    raise ProtocolError("StaffDeck event is missing a numeric sequence ID.")
                try:
                    value = json.loads("\n".join(data))
                except ValueError:
                    raise ProtocolError("StaffDeck event data is not JSON.") from None
                yield RunEvent(id=event_id, event=event, data=value)
            data, event, event_id, size = [], "message", None, 0
            continue
        size += len(line)
        if size > max_event_chars:
            raise ProtocolError("StaffDeck event exceeded the size limit.")
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "data":
            data.append(value)
        elif field == "event":
            event = value or "message"
        elif field == "id" and "\0" not in value:
            event_id = value
    if data or event_id:
        raise IncompleteEvent()
