"""Channel Host: Receive/Send PEP around the existing durable Inbox/Outbox.

The legacy channel layer is already the target shape — ``ChannelAdapter``
Protocol with five implementations, a durable ``ChannelInboundEvent`` inbox,
a ``ChannelDelivery`` outbox, and ``process_inbound`` → ``AgentLoop.handle_turn``
(so channel turns ride the Harness v3 engine automatically once ``harness_v3_enabled``).

What was missing versus the architecture is a *single* enforcement point:

- **Receive PEP** — before an inbound event may create a turn, the acting
  channel identity must be allowed to ``receive`` on the binding and to ``use``
  the target Staff. Unmapped identities are refused (never silently created).
- **Send PEP** — before a reply is staged to the outbox, the platform principal
  must be allowed to ``send`` on the binding, and the text must have passed the
  downstream supervisor (raw engine text never goes out unreviewed).

``ChannelHost`` wraps those two decisions and delegates everything else to the
legacy services; adapters are registered through the same
``register_channel_adapter`` registry, so they stay pluggable modules.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session

from app.channels.adapters import ChannelAdapter, ChannelInbound, get_channel_adapter, register_channel_adapter
from app.channels.service_outbox import stage_channel_delivery
from app.db.models import AgentProfile, ChannelBinding, ChatSession, Message, User
from staffdeck_harness.composition import projection
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.security import SecurityContext, SecurityProfile
from staffdeck_harness.security.profile import Guard


@dataclass(frozen=True)
class ReceiveDecision:
    allowed: bool
    reason: str
    security_context: SecurityContext | None = None


class ChannelHost:
    def __init__(self, db: Session, profile: SecurityProfile):
        self.db = db
        self.profile = profile
        self.guard = Guard("channel", profile)

    # -- modules --------------------------------------------------------------------

    @staticmethod
    def register_adapter(channel: str, adapter: ChannelAdapter) -> None:
        register_channel_adapter(channel, adapter)

    @staticmethod
    def adapter(channel: str) -> ChannelAdapter:
        return get_channel_adapter(channel)

    # -- receive --------------------------------------------------------------------

    def authorize_receive(self, binding: ChannelBinding, inbound: ChannelInbound, user: User | None) -> ReceiveDecision:
        """Channel Receive PEP + Staff PEP. ``user`` is the mapped StaffDeck identity (None = unmapped)."""

        if user is None:
            return ReceiveDecision(False, "unmapped channel identity")
        ctx = self.profile.identity.from_user(user, channel=binding.channel)
        try:
            self.guard.require(ctx, "channel.receive/v1", projection.channel_ref(binding))
        except PermissionDenied as exc:
            return ReceiveDecision(False, exc.message)
        agent_id = binding.agent_id
        if agent_id:
            agent = self.db.get(AgentProfile, agent_id)
            if agent is None or agent.tenant_id != binding.tenant_id or agent.status != "active":
                return ReceiveDecision(False, "target staff unavailable")
            try:
                Guard("staffdeck.runtime", self.profile).require(ctx, "staff.use/v1", projection.agent_ref(agent))
            except PermissionDenied as exc:
                return ReceiveDecision(False, exc.message)
        return ReceiveDecision(True, "allowed", ctx)

    # -- send -----------------------------------------------------------------------

    def stage_send(self, session: ChatSession, message: Message, *, supervised: bool = True) -> None:
        """Channel Send PEP, then the legacy outbox staging (same transaction)."""

        if not supervised:
            raise PermissionDenied("unsupervised text may not be sent to a channel", details={"session_id": session.id})
        if not session.channel_binding_id:
            return
        binding = self.db.get(ChannelBinding, session.channel_binding_id)
        if binding is None:
            return
        ctx = self.profile.identity.from_service("staffdeck.runtime", session.tenant_id)
        self.guard.require(ctx, "channel.send/v1", projection.channel_ref(binding))
        stage_channel_delivery(self.db, session, message)


def bound_channels(db: Session, tenant_id: str, agent_id: str) -> list[ChannelBinding]:
    return projection.agent_channels(db, tenant_id, agent_id)
