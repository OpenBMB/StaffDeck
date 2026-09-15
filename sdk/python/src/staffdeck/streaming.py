from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from .errors import ProtocolError
from .models import RunEvent


class IncompleteEvent(Exception):
    """A connection ended mid-frame; resume from the previous delivered ID."""


def parse_events(lines: Iterable[str], *, max_event_chars: int = 1_048_576) -> Iterator[RunEvent]:
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
                if not event_id or not event_id.isascii() or not event_id.isdecimal():
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

