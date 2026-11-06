"""Private host/provider HTTP definition; never a model tool or assembly snapshot."""
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class HttpTaskDefinition:
    name: str
    url: str
    method: str
    headers: Mapping[str, Any] = field(default_factory=dict, repr=False)
    auth: Mapping[str, Any] = field(default_factory=dict, repr=False)
    execution_policy: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorizedHttpTask:
    """Freshly authorized definition, not permission to reuse it in another activation."""
    definition: HttpTaskDefinition
    module_id: str
    module_version: str
    digest: str


@dataclass(frozen=True)
class ResolvedHttpTool:
    """Execution DTO consumed by the shared HTTP executor; never an ORM resource row."""
    id: str
    tenant_id: str
    name: str
    url: str
    method: str
    headers_json: Mapping[str, Any] = field(repr=False)
    auth_json: Mapping[str, Any] = field(repr=False)
    config_json: Mapping[str, Any]
