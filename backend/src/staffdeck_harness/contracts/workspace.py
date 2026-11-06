"""Trusted workspace activation; no database or whole CapabilityHost crosses this port."""
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from .invocation import InvocationContext


@dataclass(frozen=True)
class WorkspaceActivation:
    context: InvocationContext
    local_root: str
    policy: Mapping[str, Any]
    require: Callable = field(repr=False)
    emit: Callable = field(repr=False)
    remaining_seconds: Callable = field(repr=False)
