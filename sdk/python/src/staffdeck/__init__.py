"""Client for StaffDeck's remote Open API, independent of the backend package."""

from .client import StaffDeck
from .errors import APIError, ProtocolError, StaffDeckError, TransportError
from .models import APIResponse

__all__ = [
    "APIError",
    "APIResponse",
    "ProtocolError",
    "StaffDeck",
    "StaffDeckError",
    "TransportError",
]
