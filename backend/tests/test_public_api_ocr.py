"""开放 API：OCR 助手端点的认证、授权、multipart 透传与接入信息。

与企业端 `ocr:run` 共用 `execute_ocr_upload`，所以这里主要守三件事：

1. 没 `ocr:run` scope 的密钥必须在打到上游之前就被拒；
2. multipart 的字段名与文件要原样转发给 Indra（`/v1/ocr`）；
3. 账号级全量密钥默认带 `ocr:run`，员工级密钥不授予。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api import agent_apps
from app.core.artifacts import ocr as ocr_module
from app.db import get_session
from app.db.models import Tenant, User
from app.public_api.app import create_public_api_app
from app.public_api.credential_profiles import (
    AGENT_RUNTIME_SCOPES,
    USER_FULL_ACCESS_SCOPES,
)
from app.security.auth import create_access_token

OCR_PATH = "/ocr"

SAMPLE_RESPONSE = {
    "model": "indra-ocr",
    "results": [
        {
            "filename": "scan.pdf",
            "n_pages": 1,
            "elapsed_ms": 120.0,
            "error": None,
            "pages": [
                {
                    "index": 0,
                    "source": "ocr",
                    "n_lines": 1,
                    "markdown": "# 标题",
                    "text": "标题",
                    "lines": [{"box": [1, 2, 3, 4], "text": "标题", "conf": 0.9}],
                    "blocks": [{"label": "Section-Header", "bbox": [1, 2, 3, 4], "text": "标题"}],
                }
            ],
        }
    ],
}

_REAL_CLIENT = httpx.Client


class _FakeSettings:
    indra_ocr_base_url = "http://ocr.local:8000"
    indra_ocr_api_key = "k-1"
    indra_ocr_timeout_seconds = 30.0
    indra_ocr_max_files = 10
    indra_ocr_max_file_mb = 5
    public_base_url = "https://staffdeck.example.com/"


def _settings(monkeypatch) -> None:
    fake = _FakeSettings()
    monkeypatch.setattr(agent_apps, "get_settings", lambda: fake)
    monkeypatch.setattr(ocr_module, "get_settings", lambda: fake)


def _patch_upstream(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        ocr_module.httpx,
        "Client",
        lambda **kwargs: _REAL_CLIENT(
            transport=httpx.MockTransport(wrapped), timeout=kwargs.get("timeout")
        ),
    )
    return seen


def _client(monkeypatch) -> tuple[TestClient, str]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_ocr", name="OCR Tenant"))
        admin = User(
            id="user_ocr_admin",
            tenant_id="tenant_ocr",
            username="ocr_admin",
            role="admin",
            password_hash="x",
        )
        db.add(admin)
        db.commit()
        token = create_access_token(admin)

    app = create_public_api_app()

    def session_override():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = session_override
    monkeypatch.setattr("app.public_api.app.engine", engine)
    return TestClient(app), token


def _api_key(client: TestClient, admin_token: str, scopes: list[str]) -> str:
    created = client.post(
        "/api-clients",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "ocr", "scopes": ["*"]},
    )
    assert created.status_code == 201, created.text
    credential = client.post(
        f"/api-clients/{created.json()['id']}/credentials",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "ocr caller", "scopes": scopes},
    )
    assert credential.status_code == 201, credential.text
    return credential.json()["api_key"]


def test_ocr_scope_is_granted_by_account_level_keys() -> None:
    assert "ocr:run" in USER_FULL_ACCESS_SCOPES
    assert "ocr:run" not in AGENT_RUNTIME_SCOPES


def test_ocr_requires_the_scope_before_reaching_upstream(monkeypatch) -> None:
    _settings(monkeypatch)
    client, admin_token = _client(monkeypatch)
    key = _api_key(client, admin_token, ["agents:read"])
    seen = _patch_upstream(
        monkeypatch, lambda request: httpx.Response(200, json=SAMPLE_RESPONSE, request=request)
    )

    response = client.post(
        OCR_PATH,
        headers={"Authorization": f"Bearer {key}"},
        files={"files": ("scan.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_SCOPE"
    assert seen == []


def test_ocr_rejects_login_token(monkeypatch) -> None:
    _settings(monkeypatch)
    client, admin_token = _client(monkeypatch)

    response = client.post(
        OCR_PATH,
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"files": ("scan.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "API_KEY_REQUIRED"


def test_ocr_forwards_multipart_fields_and_returns_pages(monkeypatch) -> None:
    _settings(monkeypatch)
    client, admin_token = _client(monkeypatch)
    key = _api_key(client, admin_token, ["ocr:run"])
    seen = _patch_upstream(
        monkeypatch, lambda request: httpx.Response(200, json=SAMPLE_RESPONSE, request=request)
    )

    response = client.post(
        OCR_PATH,
        headers={"Authorization": f"Bearer {key}"},
        files={"files": ("scan.pdf", b"%PDF-1.4", "application/pdf")},
        data={"format": "md", "max_pages": "3", "math": "true", "include_timings": "false"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "indra-ocr"
    assert body["results"][0]["pages"][0]["markdown"] == "# 标题"
    assert body["summary"]["page_count"] == 1

    assert len(seen) == 1
    forwarded = seen[0]
    assert str(forwarded.url) == "http://ocr.local:8000/v1/ocr"
    raw = forwarded.read()
    assert b'name="format"' in raw and b"md" in raw
    assert b'name="max_pages"' in raw and b"3" in raw
    assert b'name="math"' in raw and b"true" in raw
    assert b'name="include_timings"' in raw and b"false" in raw
    assert b'name="files"' in raw and b"%PDF-1.4" in raw
    # 没给的项不能凭空补上（会覆盖上游默认值）
    assert b'name="dpi"' not in raw
    assert b'name="furniture"' not in raw


def test_ocr_maps_upstream_failure_to_problem_details(monkeypatch) -> None:
    _settings(monkeypatch)
    client, admin_token = _client(monkeypatch)
    key = _api_key(client, admin_token, ["ocr:run"])
    _patch_upstream(
        monkeypatch,
        lambda request: httpx.Response(401, json={"detail": "invalid api key"}, request=request),
    )

    response = client.post(
        OCR_PATH,
        headers={"Authorization": f"Bearer {key}"},
        files={"files": ("scan.pdf", b"%PDF-1.4", "application/pdf")},
    )

    # 上游 4xx（密钥错等）按「服务侧配置问题」收敛成 400，不把 401 原样透出去
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "invalid api key" in response.json()["detail"]


def test_ocr_rejects_too_many_files(monkeypatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(_FakeSettings, "indra_ocr_max_files", 1)
    client, admin_token = _client(monkeypatch)
    key = _api_key(client, admin_token, ["ocr:run"])
    seen = _patch_upstream(
        monkeypatch, lambda request: httpx.Response(200, json=SAMPLE_RESPONSE, request=request)
    )

    response = client.post(
        OCR_PATH,
        headers={"Authorization": f"Bearer {key}"},
        files=[
            ("files", ("a.pdf", b"%PDF-1.4", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-1.4", "application/pdf")),
        ],
    )

    assert response.status_code == 400
    assert seen == []


def test_ocr_endpoint_is_published_in_openapi(monkeypatch) -> None:
    client, _token = _client(monkeypatch)
    schema = client.get("/openapi.json").json()
    assert "post" in schema["paths"]["/ocr"]


def test_ocr_api_access_points_at_the_public_endpoint() -> None:
    access = agent_apps.ocr_api_access(current_user=None)  # type: ignore[arg-type]
    assert access.required_scope == "ocr:run"
    assert access.endpoint_path == "/api/v1/ocr"
    assert access.docs_path == "/api/v1/docs"


@pytest.mark.parametrize(("dpi", "ok"), [(71, False), (200, True), (601, False)])
def test_ocr_dpi_range_is_shared_by_both_surfaces(monkeypatch, dpi: int, ok: bool) -> None:
    _settings(monkeypatch)
    from fastapi import HTTPException, UploadFile

    import io

    upload = UploadFile(filename="a.png", file=io.BytesIO(b"x"))
    _patch_upstream(
        monkeypatch, lambda request: httpx.Response(200, json=SAMPLE_RESPONSE, request=request)
    )
    if ok:
        result = agent_apps.execute_ocr_upload([upload], dpi=dpi)
        assert result["model"] == "indra-ocr"
    else:
        with pytest.raises(HTTPException) as excinfo:
            agent_apps.execute_ocr_upload([upload], dpi=dpi)
        assert excinfo.value.status_code == 400
