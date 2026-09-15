"""Deployment-owned services. The public runtime never imports an enterprise package."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, ContextManager, Iterator, Mapping, Protocol


@dataclass(frozen=True)
class ChannelExecutionScope:
    tenant_id: str
    actor_id: str
    agent_id: str
    session_id: str
    binding_id: str
    binding_revision: int
    account_key: str
    inbound_event_id: str
    namespace: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for key, value in vars(self).items() if key != "binding_revision"):
            raise ValueError("channel scope requires complete identity and data realm")
        if type(self.binding_revision) is not int or self.binding_revision < 0:
            raise ValueError("invalid channel binding revision")


@dataclass(frozen=True)
class ActorIdentity:
    """Verified interactive identity, without a bearer token or execution authority."""
    user_id: str
    tenant_id: str
    username: str
    display_name: str
    role: str
    provider: str
    channel_scope: ChannelExecutionScope | None = None

    @property
    def subject_id(self):
        return self.user_id


@dataclass(frozen=True)
class ExecutionIdentity:
    tenant_id: str
    actor_user_id: str
    staff_id: str
    session_id: str
    outer_run_id: str
    outer_attempt: int
    trace_id: str
    deadline_at: datetime | None = None
    workload_session_id: str | None = None
    channel_scope: ChannelExecutionScope | None = None

    def __post_init__(self):
        if any(not isinstance(getattr(self, key), str) or not getattr(self, key).strip()
               for key in ("tenant_id", "actor_user_id", "staff_id", "session_id", "outer_run_id", "trace_id")):
            raise ValueError("execution identity requires complete trusted correlation")
        if not isinstance(self.outer_attempt, int) or isinstance(self.outer_attempt, bool) or self.outer_attempt < 1:
            raise ValueError("outer_attempt must be positive")


class RuntimeServicesPort(Protocol):
    namespace: str
    models: Mapping[str, type]

    def session(self, identity: ExecutionIdentity, *, purpose: str) -> ContextManager[Any]: ...


@dataclass(frozen=True)
class MaintenanceIdentity:
    """Deployment-owned data maintenance only; never a user or workload credential."""
    tenant_id: str


class AuthorizedTransportPort(Protocol):
    """Only configured destinations and declared operations may receive credentials."""

    def request(self, identity: ExecutionIdentity, *, service: str, operation: str,
                method: str, path: str, body: Mapping[str, Any] | None = None) -> Any: ...

    def forward(self, identity: ExecutionIdentity, *, service: str, operation: str, method: str,
                path: str, body: bytes = b"", params: Any = None, resource_headers: Any = None) -> Any: ...


@dataclass(frozen=True)
class ServiceResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class StreamingServiceResponse:
    """Module-owned response stream. close must be idempotent and safe on disconnect."""
    status: int
    headers: Mapping[str, str]
    body: Iterator[bytes]
    close: Callable[[], None]


class WorkspacePort(Protocol):
    def schemas(self) -> list[dict[str, Any]]: ...
    def execute(self, invocation: Any) -> Any: ...
    def materialize_package(self, context: Any, package: Any) -> tuple[str, list[str]]: ...
    def discover_artifacts(self, task_frame_id: str) -> list[Mapping[str, Any]]: ...
