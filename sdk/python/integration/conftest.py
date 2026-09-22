from __future__ import annotations

import httpx
import pytest

from staffdeck import StaffDeck


@pytest.fixture
def api(monkeypatch, tmp_path):
    # Never read deployment configuration or connect to a user's database.
    monkeypatch.setenv("ULTRARAG_DOTENV", str(tmp_path / "no-dotenv"))
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("APP_SECRET", "sdk-integration-test-only")
    from backend.tests.test_public_api_v1 import _client, _tenant_key

    # HTTP responses can arrive before the server's dependency cleanup finishes.
    # A file-backed database lets overlapping sessions use separate connections;
    # StaticPool's single in-memory connection would share their transactions.
    server, engine, token = _client(
        monkeypatch, database_url=f"sqlite:///{(tmp_path / 'api.sqlite').as_posix()}"
    )
    key = _tenant_key(server, token, ["*"])

    def transport(request):
        # Exercise real FastAPI routing/auth/schema/storage, not canned responses.
        path = request.url.raw_path.decode().removeprefix("/api/v1")
        response = server.request(
            request.method, path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    def sdk(api_key=key):
        return StaffDeck(
            base_url="http://testserver/api/v1", api_key=api_key,
            transport=httpx.MockTransport(transport),
        )

    with server:
        yield server, engine, token, sdk
    engine.dispose()


@pytest.fixture
def skill_card(api):
    from backend.tests.test_public_api_v1 import _skill_card

    return _skill_card()
