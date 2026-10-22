"""Laya 决策助手（Convai Laya Decision API）的后端代理。

为什么不直连：目标服务是独立部署的 FastAPI，未挂 CORS 中间件（OPTIONS 预检直接
405），浏览器 fetch 会被拦；且地址属服务端内网，写进前端产物也不合适。

上游契约（来自 /openapi.json，别被文档里的 `question` 误导——真实字段是复数）：

    POST /predict
    {
      "state": {"background": "..."},            # 任意键值，模型据此推理
      "questions": {                              # key 可随机，回包按同一 key 返回
        "<key>": {"type": "noul",   "instructions": "用户是否明确要求退款？"},
        "<key>": {"type": "choice", "instructions": "...",
                  "criteria": {"billing": "账单、扣款、发票", "technical": "Bug、报错"}},
        "<key>": {"type": "score",  "instructions": "...",
                  "criteria": ["不急，普通咨询", "很急，涉及钱或违约"]}
      },
      "model": "multilingual"                     # 可选，缺省由服务端按语种路由
    }

回包：{"answers": {"<key>": {...}}, "routing": {...}}
"""

from __future__ import annotations

import time
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.core.knowledge_flow.laya_client import LayaError, health_url, predict_raw
from app.security.auth import get_current_user

router = APIRouter(
    prefix="/api/enterprise/laya",
    tags=["enterprise:laya"],
    dependencies=[Depends(get_current_user)],
)

QUESTION_TYPES = ("noul", "choice", "score")
MAX_QUESTIONS = 30
MAX_CRITERIA = 30

# 开放 API（`app/public_api/decisions.py`）对外暴露的路径与所需 scope。
# 单点维护：路径改了这里一处，前端「API 接入」示例跟着变。
OPEN_API_ENDPOINT_PATH = "/api/v1/decisions/predict"
OPEN_API_DOCS_PATH = "/api/v1/docs"
OPEN_API_SCOPE = "decisions:run"


class LayaQuestion(BaseModel):
    """单个决策问题。除 `type` 与 `criteria` 的形态外不再加限制，与上游保持一致。"""

    type: Literal["noul", "choice", "score"]
    instructions: str = Field(min_length=1, max_length=2000)
    criteria: dict[str, str] | list[str] | None = None

    @model_validator(mode="after")
    def validate_criteria(self) -> "LayaQuestion":
        if self.type == "noul":
            # 是非题固定二分类，上游也只认 type + instructions
            self.criteria = None
            return self
        if self.type == "choice":
            if not isinstance(self.criteria, dict):
                raise ValueError("choice 类型的 criteria 必须是「选项名 -> 说明」的对象")
            if len(self.criteria) < 2:
                raise ValueError("choice 类型至少需要 2 个选项")
            if len(self.criteria) > MAX_CRITERIA:
                raise ValueError(f"选项数量不能超过 {MAX_CRITERIA} 个")
            for key, label in self.criteria.items():
                if not str(key).strip():
                    raise ValueError("choice 类型的选项名不能为空")
                if not str(label or "").strip():
                    raise ValueError(f"choice 类型选项「{key}」的说明不能为空")
            return self
        if not isinstance(self.criteria, list):
            raise ValueError("score 类型的 criteria 必须是选项文案数组")
        if len(self.criteria) < 2:
            raise ValueError("score 类型至少需要 2 个选项")
        if len(self.criteria) > MAX_CRITERIA:
            raise ValueError(f"选项数量不能超过 {MAX_CRITERIA} 个")
        if any(not str(item or "").strip() for item in self.criteria):
            raise ValueError("score 类型的选项文案不能为空")
        return self


class LayaPredictRequest(BaseModel):
    state: dict[str, Any]
    questions: dict[str, LayaQuestion]
    model: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_shape(self) -> "LayaPredictRequest":
        if not self.state:
            raise ValueError("state 不能为空")
        background = self.state.get("background")
        if not isinstance(background, str) or not background.strip():
            raise ValueError("决策背景（state.background）不能为空")
        if not self.questions:
            raise ValueError("至少需要一个决策问题")
        if len(self.questions) > MAX_QUESTIONS:
            raise ValueError(f"决策问题数量不能超过 {MAX_QUESTIONS} 个")
        for key in self.questions:
            if not str(key).strip():
                raise ValueError("决策问题的 key 不能为空")
        return self


class LayaPredictResponse(BaseModel):
    answers: dict[str, Any]
    routing: dict[str, Any] | None = None
    elapsed_ms: float
    # 上游地址回显，便于前端 / 排障确认这次请求到底打到了哪里
    upstream_url: str | None = None


class LayaHealthResponse(BaseModel):
    reachable: bool
    status: str | None = None
    message: str | None = None
    upstream_url: str | None = None


class LayaApiAccessResponse(BaseModel):
    """「API 接入」弹窗要展示的接入信息（路径与对外基址都由后端给，前端不再自己拼）。"""

    required_scope: str
    endpoint_path: str
    docs_path: str
    # 来自 PUBLIC_BASE_URL；为空表示未配置，前端回退到当前 origin
    public_base_url: str


def _upstream_payload(request: LayaPredictRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": request.state,
        # exclude_none 顺手丢掉 noul 的空 criteria，避免给上游塞无意义字段
        "questions": {
            key: question.model_dump(exclude_none=True)
            for key, question in request.questions.items()
        },
    }
    if request.model:
        payload["model"] = request.model
    return payload


@router.post("/predict", response_model=LayaPredictResponse)
def predict(request: LayaPredictRequest) -> LayaPredictResponse:
    return execute_predict(request)


def execute_predict(request: LayaPredictRequest) -> LayaPredictResponse:
    """调用 Laya 上游并把失败翻译成 HTTPException。

    企业端（登录态）与开放 API（`sd_live_*` 密钥）共用这一份实现：
    开放 API 侧的 HTTPException 会由 `public_http_error_handler` 转成
    RFC7807 problem+json，前端企业端则拿到既有格式。
    真实出站实现收敛在 `app/core/laya_client.predict_raw`，与 SOP 走向旁路
    (`app/core/laya_router.py`) 共享同一份网络层。
    """
    settings = get_settings()
    payload = _upstream_payload(request)
    started = time.perf_counter()
    try:
        body = predict_raw(payload)
    except LayaError as exc:
        status_code = 504 if exc.kind == "timeout" else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    routing = body.get("routing")
    return LayaPredictResponse(
        answers=body["answers"],
        routing=routing if isinstance(routing, dict) else None,
        elapsed_ms=body.get("elapsed_ms") or elapsed_ms,
        upstream_url=settings.laya_predict_url,
    )


@router.get("/health", response_model=LayaHealthResponse)
def health() -> LayaHealthResponse:
    settings = get_settings()
    url = health_url(settings.laya_predict_url)
    try:
        with httpx.Client(timeout=min(settings.laya_timeout_seconds, 10.0)) as client:
            response = client.get(url)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        return LayaHealthResponse(
            reachable=False, message=str(exc), upstream_url=url
        )
    return LayaHealthResponse(
        reachable=True,
        status=str(body.get("status")) if isinstance(body, dict) else "ok",
        upstream_url=url,
    )


@router.get("/api-access", response_model=LayaApiAccessResponse)
def api_access() -> LayaApiAccessResponse:
    """给前端「API 接入」弹窗用的接入信息：对外基址取自 PUBLIC_BASE_URL。"""
    settings = get_settings()
    return LayaApiAccessResponse(
        required_scope=OPEN_API_SCOPE,
        endpoint_path=OPEN_API_ENDPOINT_PATH,
        docs_path=OPEN_API_DOCS_PATH,
        public_base_url=(settings.public_base_url or "").rstrip("/"),
    )
