"""Public build request and first-install defaults. Credentials are never request fields."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Platform = Literal['linux-x64', 'windows-x64', 'macos-arm64', 'macos-x64']
PackageKind = Literal['source', 'docker', 'installer']


class DefaultStaff(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=2000)
    persona_prompt: str = Field(default='', max_length=30000)
    harness_max_actions: int = Field(default=32, ge=1, le=256)


class DeploymentNode(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(pattern=r'^[a-z][a-z0-9-]{0,39}$')
    label: str = Field(min_length=1, max_length=100)
    address: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9.:-]{0,200}$')


class ServiceEndpoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(pattern=r'^[a-z][a-z0-9-]{0,39}$')
    node: str = Field(pattern=r'^[a-z][a-z0-9-]{0,39}$')
    kind: Literal['app', 'edge', 'external']
    url: str = Field(max_length=1000)
    # JSON pointers into the sanitized configuration, not arbitrary executable env.
    configuration_paths: list[str] = Field(default_factory=list, max_length=30)
    image: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,250}$')
    published_port: int | None = Field(default=None, ge=1, le=65535)
    container_port: int | None = Field(default=None, ge=1, le=65535)
    environment: dict[str, str] = Field(default_factory=dict)
    secret_environment: list[str] = Field(default_factory=list, max_length=50)
    depends_on: list[str] = Field(default_factory=list, max_length=30)
    data_paths: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode='after')
    def endpoint(self):
        import re
        from .manifest import SECRET
        from urllib.parse import urlsplit
        parsed = urlsplit(self.url)
        if any(char.isspace() or ord(char) < 32 or char in ';{}$' or ord(char) in (34, 39, 92) for char in self.url):
            raise ValueError('Unsafe characters in service URL')
        if parsed.scheme not in {'http', 'https', 'postgresql', 'redis'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Service URL must be an explicit endpoint without embedded credentials')
        if any(not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', key) for key in [*self.environment, *self.secret_environment]):
            raise ValueError('Invalid service environment variable name')
        if any(SECRET.search(key) for key in self.environment):
            raise ValueError('Use secret_environment names, never literal credential values')
        from .manifest import portable
        hidden = []
        portable(self.environment, 'service.environment', hidden)
        if hidden:
            raise ValueError('Service environment contains credentials; use secret_environment')
        if (self.image or self.kind in {'app', 'edge'}) and (not self.published_port or not self.container_port):
            raise ValueError('Managed containers need explicit host and container ports')
        if any(not path.startswith('/') or path == '/' or '..' in path.split('/') or ':' in path for path in self.data_paths):
            raise ValueError('Persistent data paths must be explicit container directories')
        if self.image and parsed.scheme in {'postgresql', 'redis'} and not self.data_paths:
            raise ValueError('Managed database services require persistent data_paths')
        return self


class PackageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tenant_id: str = Field(min_length=1, max_length=100)
    name: str = Field(default='StaffDeck', pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')
    version: str = Field(default='0.1.0', pattern=r'^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$')
    kind: PackageKind
    platform: Platform | None = None
    configuration: Literal['applied', 'saved'] = 'applied'
    default_staff: list[DefaultStaff] = Field(default_factory=list, max_length=100)
    employee_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=100)
    deployment_nodes: list[DeploymentNode] = Field(default_factory=list, max_length=20)
    services: list[ServiceEndpoint] = Field(default_factory=list, max_length=50)

    @model_validator(mode='after')
    def validate_request(self):
        if (self.kind == 'installer') != (self.platform is not None):
            raise ValueError('Only installer packages require a target platform')
        if len(self.employee_ids) != len(set(self.employee_ids)):
            raise ValueError('Employee IDs must be unique')
        nodes = {node.key for node in self.deployment_nodes}
        if len(nodes) != len(self.deployment_nodes) or len({s.key for s in self.services}) != len(self.services):
            raise ValueError('Deployment node and service keys must be unique')
        if any(service.node not in nodes for service in self.services):
            raise ValueError('Every service must reference a declared node')
        if self.services and len([s for s in self.services if s.kind == 'app']) != 1:
            raise ValueError('Exactly one application service is required')
        graph = {service.key: service.depends_on for service in self.services}
        if any(dep not in graph for deps in graph.values() for dep in deps):
            raise ValueError('Service dependency is not declared')
        resolved = set()
        while len(resolved) < len(graph):
            ready = {key for key, deps in graph.items() if key not in resolved and set(deps) <= resolved}
            if not ready:
                raise ValueError('Cyclic service startup dependencies')
            resolved |= ready
        for field in ('key', 'name'):
            values = [getattr(staff, field).strip() for staff in self.default_staff]
            if any(not value for value in values) or len(values) != len(set(values)):
                raise ValueError(f'Default employee {field} must be nonempty and unique')
        return self
