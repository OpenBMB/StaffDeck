"""Handoff Core: the state machine nobody may replace, plus the three pluggable slots.

State machine (durable on ``HumanHandoffRequest.status``):

    pending ──assign──► assigned ──answer──► answered ──resume──► resumed ──close──► closed
       │                  │                                                    ▲
       └──────────────────┴───────────── cancel ───────────────────────────────┘

Legacy uses ``pending`` → ``answered`` → (``cancelled``); those remain valid
so the existing web/feishu reply paths keep working. ``assigned``/``resumed``/
``closed`` are additive.

Pluggable slots (``handoff.assignment``, ``handoff.notifier``,
``handoff.reply_endpoint``) only *propose*: an ``AssignmentStrategy`` returns a
candidate assignee that the Core validates against the tenant; a ``Notifier``
delivers a notice and may record ``notify_message_id``; a ``ReplyResolver``
turns an inbound message into ``(handoff_id, reply_text, replier)``. The Core
runs the PEP (``handoff.create/assign/reply``) before every mutation.

Every human reply creates a **new turn**: the Core never mutates engine history.
It reuses the legacy resume path (``_apply_handoff_reply`` → async resume
worker) so the resumed turn starts from Channel Receive PEP like any other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from sqlmodel import Session, select

from app.db.models import AgentEvent, AgentProfile, ChatSession, HumanHandoffRequest, User, utc_now
from staffdeck_harness.composition import projection
from staffdeck_harness.contracts.security import SecurityContext
from staffdeck_harness.security.profile import Guard

TERMINAL = {"closed", "cancelled"}
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"assigned", "answered", "cancelled"},
    "assigned": {"answered", "assigned", "cancelled"},
    "answered": {"resumed", "cancelled", "closed"},
    "resumed": {"closed", "cancelled"},
    "closed": set(),
    "cancelled": set(),
}


class HandoffTransitionError(ValueError):
    pass


@runtime_checkable
class AssignmentStrategy(Protocol):
    slot = "handoff.assignment"

    def propose(self, db: Session, handoff: HumanHandoffRequest, *, agent: AgentProfile | None) -> str | None: ...


@runtime_checkable
class Notifier(Protocol):
    slot = "handoff.notifier"
    name: str

    def notify(self, db: Session, handoff: HumanHandoffRequest, *, pending_question: str, context_summary: str) -> str | None: ...


@runtime_checkable
class ReplyResolver(Protocol):
    slot = "handoff.reply_endpoint"
    name: str

    def resolve(self, db: Session, tenant_id: str, inbound: Mapping[str, Any]) -> tuple[str, str] | None: ...


# --------------------------------------------------------------------------- default modules

class DefaultAssignment(AssignmentStrategy):
    """SOP step assignee → channel default → agent owner → tenant admin (legacy order)."""

    def propose(self, db: Session, handoff: HumanHandoffRequest, *, agent: AgentProfile | None) -> str | None:
        meta = dict(handoff.metadata_json or {})
        for key in ("step_assignee_user_id", "binding_default_assignee_user_id"):
            uid = meta.get(key)
            if uid and self._internal(db, handoff.tenant_id, str(uid)):
                return str(uid)
        owner = (agent.metadata_json or {}).get("owner_user_id") if agent is not None else None
        if owner and self._internal(db, handoff.tenant_id, str(owner)):
            return str(owner)
        admin = db.exec(select(User).where(User.tenant_id == handoff.tenant_id, User.role == "admin").order_by(User.created_at)).first()
        return admin.id if admin else None

    @staticmethod
    def _internal(db: Session, tenant_id: str, user_id: str) -> bool:
        row = db.get(User, user_id)
        return bool(row and row.tenant_id == tenant_id and getattr(row, "source", "web") == "web")


class WebInboxNotifier(Notifier):
    name = "web"

    def notify(self, db: Session, handoff: HumanHandoffRequest, *, pending_question: str, context_summary: str) -> str | None:
        db.add(AgentEvent(tenant_id=handoff.tenant_id, session_id=handoff.session_id, event_type="human_handoff_notified", payload_json={"handoff_id": handoff.id, "assignee_user_id": handoff.assignee_user_id, "channel": "web"}))
        return None


class ChannelNotifier(Notifier):
    """Delegates to the legacy channel outbox (feishu/dingtalk/wecom) notice path."""

    def __init__(self, channel: str):
        self.name = channel

    def notify(self, db: Session, handoff: HumanHandoffRequest, *, pending_question: str, context_summary: str) -> str | None:
        from app.channels.service_outbox import notify_handoff_assignee, resolve_handoff_notify_binding

        binding = resolve_handoff_notify_binding(db, handoff.tenant_id, self.name)
        if binding is None:
            return None
        notify_handoff_assignee(db, binding, handoff, pending_question, context_summary)
        return handoff.notify_message_id


class WebReplyResolver(ReplyResolver):
    name = "web"

    def resolve(self, db: Session, tenant_id: str, inbound: Mapping[str, Any]) -> tuple[str, str] | None:
        hid = str(inbound.get("handoff_id") or "")
        text = str(inbound.get("reply") or "").strip()
        return (hid, text) if hid and text else None


class ChannelCommandReplyResolver(ReplyResolver):
    """``/回复反馈 <handoff_id> <text>`` and quoted-reply correlation via notify_message_id."""

    name = "channel_command"

    def resolve(self, db: Session, tenant_id: str, inbound: Mapping[str, Any]) -> tuple[str, str] | None:
        quoted = str(inbound.get("quoted_message_id") or "")
        text = str(inbound.get("text") or "").strip()
        if quoted:
            row = db.exec(select(HumanHandoffRequest).where(HumanHandoffRequest.tenant_id == tenant_id, HumanHandoffRequest.notify_message_id == quoted)).first()
            if row is not None and text:
                return row.id, text
        from app.channels.service_routing import parse_command

        cmd = parse_command(text)
        if cmd is not None and cmd.kind == "handoff_reply" and cmd.query:
            hid, _, body = cmd.query.partition(" ")
            if hid and body.strip():
                return hid, body.strip()
        return None


# --------------------------------------------------------------------------- core

@dataclass
class HandoffCore:
    db: Session
    guard: Guard
    assignment: AssignmentStrategy
    notifiers: Mapping[str, Notifier]
    resolvers: Mapping[str, ReplyResolver]
    apply_reply: Callable[..., None] | None = None   # legacy `_apply_handoff_reply`; injected to avoid api import at module load

    def _transition(self, row: HumanHandoffRequest, to: str) -> None:
        if to not in TRANSITIONS.get(row.status, set()):
            raise HandoffTransitionError(f"handoff {row.id}: {row.status} → {to} is not allowed")
        row.status = to
        row.updated_at = utc_now()
        self.db.add(row)

    def _ref(self, row: HumanHandoffRequest):
        agent = self.db.get(AgentProfile, row.agent_id) if row.agent_id else None
        owner = (agent.metadata_json or {}).get("owner_user_id") if agent is not None else None
        return projection.handoff_ref(row, agent_owner_user_id=owner)

    # -- create --------------------------------------------------------------------

    def create(self, ctx: SecurityContext, session: ChatSession, *, pending_question: str, context_summary: str, trigger_skill_id: str | None = None, trigger_step_id: str | None = None, metadata: Mapping[str, Any] | None = None, notify_channel: str | None = None) -> HumanHandoffRequest:
        existing = self.db.exec(select(HumanHandoffRequest).where(HumanHandoffRequest.tenant_id == session.tenant_id, HumanHandoffRequest.session_id == session.id, HumanHandoffRequest.status.in_(["pending", "assigned"]))).first()  # type: ignore[attr-defined]
        if existing is not None:
            existing.metadata_json = {**dict(existing.metadata_json or {}), **dict(metadata or {})}
            existing.updated_at = utc_now()
            self.db.add(existing)
            return existing
        row = HumanHandoffRequest(
            tenant_id=session.tenant_id, session_id=session.id, agent_id=session.agent_id, requester_user_id=session.user_id,
            trigger_skill_id=trigger_skill_id, trigger_step_id=trigger_step_id, context_summary=context_summary, pending_question=pending_question,
            status="pending", metadata_json={**dict(metadata or {}), **({"assignee_notify_channel": notify_channel} if notify_channel else {})},
        )
        self.guard.require(ctx, "handoff.request/v1", projection.ResourceRef(type="handoff", id="new", tenant_id=session.tenant_id, attributes={"requester_user_id": session.user_id}))
        self.db.add(row)
        session.status = "handoff"
        session.awaiting_input_json = {"type": "human_handoff", "handoff_id": None, "pending_question": pending_question}
        self.db.commit()
        self.db.refresh(row)
        session.awaiting_input_json = {**(session.awaiting_input_json or {}), "handoff_id": row.id}
        self.db.add(session)
        self.db.add(AgentEvent(tenant_id=row.tenant_id, session_id=row.session_id, event_type="human_handoff_created", payload_json={"handoff_id": row.id, "trigger_skill_id": trigger_skill_id, "trigger_step_id": trigger_step_id, "engine": "harness_v3"}))
        self.db.commit()
        self.assign(ctx, row, actor_is_system=True)
        self.notify(row, pending_question=pending_question, context_summary=context_summary)
        return row

    # -- assign --------------------------------------------------------------------

    def assign(self, ctx: SecurityContext, row: HumanHandoffRequest, *, assignee_user_id: str | None = None, actor_is_system: bool = False) -> HumanHandoffRequest:
        if not actor_is_system:
            self.guard.require(ctx, "handoff.assign/v1", self._ref(row))
        agent = self.db.get(AgentProfile, row.agent_id) if row.agent_id else None
        candidate = assignee_user_id or self.assignment.propose(self.db, row, agent=agent)
        if candidate:
            user = self.db.get(User, candidate)
            if user is None or user.tenant_id != row.tenant_id:
                raise HandoffTransitionError(f"assignee {candidate} is not a member of tenant {row.tenant_id}")
            row.assignee_user_id = candidate
            if row.status == "pending":
                self._transition(row, "assigned")
            self.db.add(AgentEvent(tenant_id=row.tenant_id, session_id=row.session_id, event_type="human_handoff_assigned", payload_json={"handoff_id": row.id, "assignee_user_id": candidate}))
            self.db.commit()
        return row

    # -- notify --------------------------------------------------------------------

    def notify(self, row: HumanHandoffRequest, *, pending_question: str, context_summary: str) -> None:
        preferred = str((row.metadata_json or {}).get("assignee_notify_channel") or "").strip()
        order = [preferred] if preferred and preferred in self.notifiers else []
        if "web" in self.notifiers and "web" not in order:
            order.append("web")
        for name in order:
            try:
                mid = self.notifiers[name].notify(self.db, row, pending_question=pending_question, context_summary=context_summary)
                if mid:
                    row.notify_message_id = mid
                    self.db.add(row)
            except Exception as exc:  # a notifier must never break the handoff
                self.db.add(AgentEvent(tenant_id=row.tenant_id, session_id=row.session_id, event_type="human_handoff_notify_failed", payload_json={"handoff_id": row.id, "notifier": name, "error": str(exc)}))
        self.db.commit()

    # -- reply ---------------------------------------------------------------------

    def reply(self, ctx: SecurityContext, resolver: str, inbound: Mapping[str, Any], *, source: str) -> HumanHandoffRequest | None:
        res = self.resolvers.get(resolver)
        if res is None:
            return None
        resolved = res.resolve(self.db, ctx.tenant_id, inbound)
        if resolved is None:
            return None
        handoff_id, text = resolved
        row = self.db.get(HumanHandoffRequest, handoff_id)
        if row is None or row.tenant_id != ctx.tenant_id:
            return None
        self.guard.require(ctx, "handoff.reply/v1", self._ref(row))
        if row.status not in {"pending", "assigned"}:
            raise HandoffTransitionError(f"handoff {row.id} is {row.status}; replies are accepted only while pending/assigned")
        if self.apply_reply is None:
            from app.api.chat import _apply_handoff_reply as legacy_apply

            self.apply_reply = legacy_apply
        # Legacy path: status→answered, session→active, event, async resume as a NEW turn.
        self.apply_reply(self.db, row, text, answered_by_user_id=ctx.principal_id, source=source)
        return row

    def mark_resumed(self, row: HumanHandoffRequest) -> None:
        self._transition(row, "resumed")
        self.db.commit()

    def close(self, ctx: SecurityContext, row: HumanHandoffRequest, *, reason: str = "closed") -> None:
        self.guard.require(ctx, "handoff.assign/v1", self._ref(row))
        self._transition(row, "closed")
        self.db.add(AgentEvent(tenant_id=row.tenant_id, session_id=row.session_id, event_type="human_handoff_closed", payload_json={"handoff_id": row.id, "reason": reason}))
        self.db.commit()

    def cancel(self, ctx: SecurityContext, row: HumanHandoffRequest, *, reason: str = "cancelled") -> None:
        self.guard.require(ctx, "handoff.assign/v1", self._ref(row))
        self._transition(row, "cancelled")
        session = self.db.get(ChatSession, row.session_id)
        if session is not None and session.status == "handoff":
            session.status = "active"
            session.awaiting_input_json = None
            self.db.add(session)
        self.db.add(AgentEvent(tenant_id=row.tenant_id, session_id=row.session_id, event_type="human_handoff_cancelled", payload_json={"handoff_id": row.id, "reason": reason}))
        self.db.commit()


def build_handoff_core(db: Session, guard: Guard, *, channels: tuple[str, ...] = ("feishu", "dingtalk", "wecom", "wechat"), use_registry: bool = True) -> HandoffCore:
    """Assemble the Core from the Module Registry (``handoff.*`` slots); falls back to shipped defaults."""

    assignment: AssignmentStrategy | None = None
    notifiers: dict[str, Notifier] = {}
    resolvers: dict[str, ReplyResolver] = {}
    if use_registry:
        try:
            from staffdeck_harness.contracts.manifest import SlotName
            from staffdeck_harness.modules.registry import peek_registry

            reg = peek_registry()
            if reg is not None:
                # The slot also hosts kernel entries (handoff.core, runtime.cancellation); pick a real strategy.
                assignment = next((i.provider for i in reg.providers(SlotName.HANDOFF_ASSIGNMENT) if callable(getattr(i.provider, "propose", None))), None)
                for item in reg.providers(SlotName.HANDOFF_NOTIFIER):
                    if callable(getattr(item.provider, "notify", None)):
                        notifiers[getattr(item.provider, "name", item.manifest.module_id)] = item.provider
                for item in reg.providers(SlotName.HANDOFF_REPLY_ENDPOINT):
                    if callable(getattr(item.provider, "resolve", None)):
                        resolvers[getattr(item.provider, "name", item.manifest.module_id)] = item.provider
        except Exception:
            assignment, notifiers, resolvers = None, {}, {}
    if assignment is None:
        assignment = DefaultAssignment()
    if not notifiers:
        notifiers = {"web": WebInboxNotifier(), **{ch: ChannelNotifier(ch) for ch in channels}}
    if not resolvers:
        resolvers = {"web": WebReplyResolver(), "channel_command": ChannelCommandReplyResolver()}
    return HandoffCore(db=db, guard=guard, assignment=assignment, notifiers=notifiers, resolvers=resolvers)
