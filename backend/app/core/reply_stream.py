"""Public reply projection, separate from model traces and runtime control messages."""

from collections.abc import Callable
from typing import Any


class ReplyStream:
    """A turn-local append/replace stream; the transport owns persistence and delivery."""

    def __init__(self, publish: Callable[[str, dict[str, Any]], None]):
        self.publish = publish
        self.text = ""

    def on_delta(self, text: str) -> None:
        if text:
            self.publish("stream_delta", {"content": text})
            self.text += text

    def on_replace(self, text: str) -> None:
        if text == self.text:
            return
        if not self.text:
            self.on_delta(text)
        else:
            self.publish("stream_replace", {"content": text})
            self.text = text

    def finish(self) -> bool:
        # The coordinator still has to persist the authoritative assistant message.
        # stream_end is emitted by the transport only after that persistence succeeds.
        return True


class PublicTextProjection:
    """Stream plain text, but hold possible native-result envelopes until final validation.

    A JSON/code-prefixed answer is intentionally buffered, not discarded. Its final public
    representation is reconciled by the coordinator, including legitimate user-requested JSON.
    No partial JSON, escape sequence or internal status field is exposed while classifying it.
    """

    def __init__(self, emit: Callable[[str], None]):
        self.emit = emit
        self.pending = ""
        self.plain = False

    def feed(self, text: str) -> None:
        if self.plain:
            self.emit(text)
            return
        self.pending += text
        prefix = self.pending.lstrip()
        if not prefix or prefix.startswith(("{", "`")):
            return
        self.plain = True
        self.emit(self.pending)
        self.pending = ""


def reconcile_reply(sink: Any, reply: str, streamed: str = "") -> None:
    """Commit a final public representation without appending a second whole answer."""
    replace = getattr(sink, "on_replace", None)
    if callable(replace):
        replace(reply)
    elif reply != streamed:
        # Older append-only consumers can safely receive a missing suffix, not a second reply.
        if not streamed or reply.startswith(streamed):
            sink.on_delta(reply[len(streamed):])
