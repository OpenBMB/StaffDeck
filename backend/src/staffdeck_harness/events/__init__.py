"""Event relay: Harness v3 SessionEvent → StaffDeck RuntimeEvent, and event.observer slot."""

from staffdeck_harness.events.relay import OBSERVER_SLOT, EventObserver, SessionEventRelay, register_observer, relay_event

__all__ = ["OBSERVER_SLOT", "EventObserver", "SessionEventRelay", "register_observer", "relay_event"]
