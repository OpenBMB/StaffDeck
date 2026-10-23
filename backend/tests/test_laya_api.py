from __future__ import annotations

import json

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import laya


def _question_payload() -> dict:
    return {
        "state": {"background": "客户来电说账单多扣了钱，要求退款"},
        "questions": {
            "q_abc123": {"type": "noul", "instructions": "用户是否明确要求退款？"},
            "q_def456": {
                "type": "choice",
                "instructions": "这条工单应由哪个部门处理？",
                "criteria": {"billing": "账单、扣款、发票", "technical": "Bug、报错"},
            },
            "q_ghi789": {
                "type": "score",
                "instructions": "这条工单的紧急程度是多少？",
                "criteria": ["不急，普通咨询", "很急，涉及钱或违约"],
            },
        },
    }


def _patch_transport(monkeypatch, handler) -> list[httpx.Request]:
    """用 MockTransport 替掉真实出站，同时记录发往上游的请求。

    注意 `laya.httpx` 就是 httpx 模块本身，patch 它的 Client 会全局生效——
    必须先抓住原始类，否则替身会调用自己导致无限递归。
    """
    seen: list[httpx.Request] = []
    real_client = httpx.Client

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        laya.httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(wrapped), timeout=kwargs.get("timeout")
        ),
    )
    return seen


# ---------------------------------------------------------------------------
# 请求校验：宁可 422 也不要让非法结构打到上游
# ---------------------------------------------------------------------------


def test_accepts_free_form_question_keys_and_noul_without_criteria() -> None:
    request = laya.LayaPredictRequest.model_validate(_question_payload())
    payload = laya._upstream_payload(request)
    # key 原样透传，回包才能按同一 key 对齐
    assert set(payload["questions"]) == {"q_abc123", "q_def456", "q_ghi789"}
    assert payload["questions"]["q_abc123"] == {
        "type": "noul",
        "instructions": "用户是否明确要求退款？",
    }
    # 未指定 model 时不上送空字段，交给上游按语种路由
    assert "model" not in payload


def test_noul_ignores_supplied_criteria() -> None:
    request = laya.LayaPredictRequest.model_validate(
        {
            "state": {"background": "背景"},
            "questions": {"k": {"type": "noul", "instructions": "是否？", "criteria": ["是", "否"]}},
        }
    )
    assert request.questions["k"].criteria is None


@pytest.mark.parametrize(
    "payload",
    [
        {"questions": {"k": {"type": "noul", "instructions": "是否？"}}},  # 缺 state
        {"state": {"background": "   "}, "questions": {"k": {"type": "noul", "instructions": "x"}}},
        {"state": {"background": "b"}, "questions": {}},  # 没有问题
        {
            "state": {"background": "b"},
            "questions": {"k": {"type": "choice", "instructions": "选哪个？", "criteria": {"only": "仅一个"}}},
        },
        {
            "state": {"background": "b"},
            "questions": {"k": {"type": "choice", "instructions": "选哪个？", "criteria": ["甲", "乙"]}},
        },
        {
            "state": {"background": "b"},
            "questions": {"k": {"type": "score", "instructions": "多重？", "criteria": ["仅一个"]}},
        },
        {"state": {"background": "b"}, "questions": {"k": {"type": "unknown", "instructions": "x"}}},
        {"state": {"background": "b"}, "questions": {"k": {"type": "noul", "instructions": ""}}},
        {
            "state": {"background": "b"},
            "questions": {"k": {"type": "choice", "instructions": "选哪个？", "criteria": {"a": "甲", "b": " "}}},
        },
    ],
)
def test_rejects_malformed_payloads(payload: dict) -> None:
    with pytest.raises(ValidationError):
        laya.LayaPredictRequest.model_validate(payload)


def test_rejects_too_many_questions() -> None:
    questions = {
        f"k{index}": {"type": "noul", "instructions": "是否？"}
        for index in range(laya.MAX_QUESTIONS + 1)
    }
    with pytest.raises(ValidationError):
        laya.LayaPredictRequest.model_validate({"state": {"background": "b"}, "questions": questions})


# ---------------------------------------------------------------------------
# 上游交互
# ---------------------------------------------------------------------------


def test_health_url_derives_from_predict_url() -> None:
    from app.core.knowledge_flow.laya_client import health_url

    assert health_url("http://8.153.146.109:8080/predict") == "http://8.153.146.109:8080/health"
    assert health_url("https://laya.example.com/api/predict") == "https://laya.example.com/api/health"
    # 没有 /predict 后缀时也不误删路径
    assert health_url("http://host:8080/v1") == "http://host:8080/v1/health"


def test_predict_forwards_payload_and_returns_answers(monkeypatch) -> None:
    upstream = {
        "answers": {
            "q_abc123": {"type": "noul", "noul": 0.9961, "confidence": 0.9961},
            "q_def456": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.9949, "technical": 0.0051},
                "confidence": 0.9738,
            },
        },
        "routing": {"model": "multilingual", "repo": "convaiinnovations/laya/multilingual"},
    }
    seen = _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=upstream, request=request),
    )

    result = laya.predict(laya.LayaPredictRequest.model_validate(_question_payload()))

    assert result.answers == upstream["answers"]
    assert (result.routing or {}).get("model") == "multilingual"
    assert result.elapsed_ms >= 0
    assert len(seen) == 1
    sent = json.loads(seen[0].content.decode("utf-8"))
    assert sent["state"]["background"].startswith("客户来电")
    assert sent["questions"]["q_def456"]["criteria"]["billing"] == "账单、扣款、发票"


def test_predict_maps_timeout_to_504(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(HTTPException) as exc:
        laya.predict(laya.LayaPredictRequest.model_validate(_question_payload()))
    assert exc.value.status_code == 504
    assert "超时" in str(exc.value.detail)


def test_predict_maps_upstream_error_to_502(monkeypatch) -> None:
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(500, json={"detail": "模型未加载"}, request=request),
    )
    with pytest.raises(HTTPException) as exc:
        laya.predict(laya.LayaPredictRequest.model_validate(_question_payload()))
    assert exc.value.status_code == 502
    assert "模型未加载" in str(exc.value.detail)


def test_predict_maps_connection_error_to_502(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(HTTPException) as exc:
        laya.predict(laya.LayaPredictRequest.model_validate(_question_payload()))
    assert exc.value.status_code == 502
    assert "无法连接" in str(exc.value.detail)


def test_predict_rejects_unexpected_upstream_shape(monkeypatch) -> None:
    _patch_transport(monkeypatch, lambda request: httpx.Response(200, json={"ok": True}, request=request))
    with pytest.raises(HTTPException) as exc:
        laya.predict(laya.LayaPredictRequest.model_validate(_question_payload()))
    assert exc.value.status_code == 502


def test_health_reports_unreachable_without_raising(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, handler)
    result = laya.health()
    assert result.reachable is False
    assert result.upstream_url.endswith("/health")


# ---------------------------------------------------------------------------
# 路由层：确认鉴权依赖生效、代理链贯通（不走 lifespan，避免连库）
# ---------------------------------------------------------------------------


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app), app


def test_predict_route_is_registered_under_enterprise_laya() -> None:
    _client_instance, app = _client()
    paths = app.openapi()["paths"]
    assert "/api/enterprise/laya/predict" in paths
    assert "/api/enterprise/laya/health" in paths
    assert "/api/enterprise/laya/api-access" in paths


def test_api_access_route_rejects_anonymous_callers() -> None:
    client, _app = _client()
    response = client.get("/api/enterprise/laya/api-access")
    assert response.status_code == 401


def test_api_access_reports_scope_and_paths_from_single_source() -> None:
    """前端示例里公布的 scope / 路径必须真的可用：路径可达、scope 与开放 API 同源。"""
    client, app = _client()
    app.dependency_overrides[laya.get_current_user] = lambda: object()
    try:
        body = client.get("/api/enterprise/laya/api-access").json()
    finally:
        app.dependency_overrides.clear()

    assert body["required_scope"] == laya.OPEN_API_SCOPE
    assert body["endpoint_path"] == laya.OPEN_API_ENDPOINT_PATH
    assert body["docs_path"] == laya.OPEN_API_DOCS_PATH

    from app.public_api import decisions

    assert decisions.OPEN_API_SCOPE == laya.OPEN_API_SCOPE

    # 公布的路径确实挂载在 public_api 上：匿名调用返回 401 而不是 404
    response = client.post(
        laya.OPEN_API_ENDPOINT_PATH,
        json={"state": {"background": "客户要求退款"}, "questions": {}},
    )
    assert response.status_code == 401


def test_api_access_reads_public_base_url_from_settings(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        laya,
        "get_settings",
        lambda: SimpleNamespace(public_base_url="https://staffdeck.example.com/"),
    )

    client, app = _client()
    app.dependency_overrides[laya.get_current_user] = lambda: object()
    try:
        body = client.get("/api/enterprise/laya/api-access").json()
    finally:
        app.dependency_overrides.clear()

    # 末尾斜杠已被去掉，前端可安全拼接路径
    assert body["public_base_url"] == "https://staffdeck.example.com"


def test_api_access_returns_empty_base_url_when_unset(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(laya, "get_settings", lambda: SimpleNamespace(public_base_url=""))

    client, app = _client()
    app.dependency_overrides[laya.get_current_user] = lambda: object()
    try:
        body = client.get("/api/enterprise/laya/api-access").json()
    finally:
        app.dependency_overrides.clear()

    # 未配置时前端回退到浏览器当前 origin
    assert body["public_base_url"] == ""


def test_predict_route_rejects_anonymous_callers() -> None:
    client, _app = _client()
    response = client.post("/api/enterprise/laya/predict", json=_question_payload())
    assert response.status_code == 401


def test_predict_route_returns_answers_for_authenticated_callers(monkeypatch) -> None:
    from app.security.auth import get_current_user

    upstream = {"answers": {"q_abc123": {"type": "noul", "noul": 0.99, "confidence": 0.99}}}
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=upstream, request=request),
    )

    client, app = _client()
    app.dependency_overrides[get_current_user] = lambda: object()
    try:
        response = client.post("/api/enterprise/laya/predict", json=_question_payload())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["answers"] == upstream["answers"]
    assert body["upstream_url"].endswith("/predict")


def test_predict_route_validates_before_calling_upstream(monkeypatch) -> None:
    from app.security.auth import get_current_user

    seen = _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"answers": {}}, request=request),
    )

    client, app = _client()
    app.dependency_overrides[get_current_user] = lambda: object()
    try:
        response = client.post(
            "/api/enterprise/laya/predict",
            json={"state": {"background": ""}, "questions": {}},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert seen == []
