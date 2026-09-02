"""Event relay: DSH SessionEvent → StaffDeck RuntimeEvent, and event.observer slot."""

from staffdeck_dsh.events.relay import OBSERVER_SLOT, EventObserver, SessionEventRelay, register_observer, relay_event

__all__ = ["OBSERVER_SLOT", "EventObserver", "SessionEventRelay", "register_observer", "relay_event"]
