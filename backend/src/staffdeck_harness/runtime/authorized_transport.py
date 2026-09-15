"""Adapt existing HTTP clients to a selected credential-owning transport port."""
import httpx
from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.manifest import SlotName


def selected_transport(db, registry=None):
    from staffdeck_harness.modules.registry import peek_registry
    registry = registry or getattr(db, "info", {}).get("staffdeck_registry") or peek_registry()
    item = registry.provider(SlotName.RUNTIME_TRANSPORT) if registry else None
    return item.provider.build(db) if item else None


class AuthorizedClientTransport(httpx.BaseTransport):
    def __init__(self, port, identity, *, service, operation):
        if port is None:
            raise ModuleSdkError("授权通信模块未装配", code="AUTHORIZED_TRANSPORT_UNAVAILABLE")
        self.port, self.identity, self.service, self.operation = port, identity, service, operation

    def handle_request(self, request):
        response = self.port.forward(self.identity, service=self.service, operation=self.operation,
            method=request.method, path=request.url.path, params=request.url.params.multi_items(),
            body=request.read(), resource_headers=dict(request.headers))
        return httpx.Response(response.status, headers=response.headers, content=response.body, request=request)
