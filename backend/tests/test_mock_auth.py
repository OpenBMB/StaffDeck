from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mock import router
from app.security.internal_service import INTERNAL_SERVICE_HEADER, internal_service_token


def test_mock_api_rejects_anonymous_and_invalid_internal_requests() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    payload = {"order_id": "ARCHIVE-1001"}

    assert client.post("/api/mock/order/archive-query", json=payload).status_code == 401
    assert client.post(
        "/api/mock/order/archive-query",
        json=payload,
        headers={INTERNAL_SERVICE_HEADER: "invalid"},
    ).status_code == 401


def test_mock_api_accepts_internal_service_token() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/mock/order/archive-query",
        json={"order_id": "ARCHIVE-1001"},
        headers={INTERNAL_SERVICE_HEADER: internal_service_token()},
    )

    assert response.status_code == 200
    assert response.json()["found"] is True


def test_mock_expense_quota_query_requires_internal_token() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    payload = {"employee_id": "E1001", "month": "2026-09"}

    assert client.post("/api/mock/expense/quota_query", json=payload).status_code == 401
    response = client.post(
        "/api/mock/expense/quota_query",
        json=payload,
        headers={INTERNAL_SERVICE_HEADER: internal_service_token()},
    )
    assert response.status_code == 200
    assert response.json()["found"] is True


def test_mock_expense_quota_query_unknown_employee_via_http() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/mock/expense/quota_query",
        json={"employee_id": "666", "month": "2026-09"},
        headers={INTERNAL_SERVICE_HEADER: internal_service_token()},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["miss_reason"] == "employee_not_found"
    assert "total_quota" not in body
