"""Client for StaffDeck's remote Open API, independent of the backend package."""

from .client import StaffDeck
from .errors import (
    APIError,
    ProtocolError,
    RunFailedError,
    StaffDeckError,
    StreamError,
    TransportError,
    WaitTimeout,
)
from .models import APIResponse, RunEvent

__all__ = [
    "APIError",
    "APIResponse",
    "ProtocolError",
    "RunEvent",
    "RunFailedError",
    "StaffDeck",
    "StaffDeckError",
    "StreamError",
    "TransportError",
    "WaitTimeout",
]
