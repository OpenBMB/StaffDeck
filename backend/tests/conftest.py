"""Each API/service test owns its process-level module assembly."""
import pytest


@pytest.fixture
def independent_knowledge_service(tmp_path, monkeypatch):
    from contextlib import ExitStack
    from fastapi.testclient import TestClient
    from knowledge_core.api import create_app
    from knowledge_core.runtime import Runtime
    from knowledge_core.configuration import Configuration
    from knowledge_core.identity import Principal, StaticBearerIdentity
    from app.knowledge import http_adapter

    with ExitStack() as stack:
        def start(tenant_id):
            runtime = Runtime(f"sqlite:///{tmp_path / 'km.sqlite'}",
                configuration=Configuration('full', {'authentication': True}),
                identity=StaticBearerIdentity({'host': Principal('host', tenant_id, spaces=frozenset({'*'}))}))
            client = stack.enter_context(TestClient(create_app(runtime)))
            monkeypatch.setenv('STAFFDECK_KM_URL', 'http://knowledge')
            monkeypatch.setenv('STAFFDECK_KM_BEARER', 'host')
            monkeypatch.setattr(http_adapter.httpx, 'request', lambda method, url, **kwargs: client.request(
                method, url.removeprefix('http://knowledge'),
                **{key: value for key, value in kwargs.items() if key != 'timeout'}))
            return client
        yield start


@pytest.fixture(autouse=True)
def isolated_module_assembly():
    from staffdeck_harness.modules.registry import reset_registry
    from staffdeck_harness.modules.config import reset_env_snapshot
    from staffdeck_harness.security.profile import reset_profile
    reset_registry()
    reset_profile()
    reset_env_snapshot()
    yield
    reset_registry()
    reset_profile()
    reset_env_snapshot()
