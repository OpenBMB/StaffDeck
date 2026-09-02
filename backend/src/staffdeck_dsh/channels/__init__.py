"""Channel Host: Receive/Send PEP around the legacy durable inbox/outbox and adapter registry."""

from staffdeck_dsh.channels.host import ChannelHost, ReceiveDecision, bound_channels

__all__ = ["ChannelHost", "ReceiveDecision", "bound_channels"]
