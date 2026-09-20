"""Stateless Protocol v2 HTTP facade for StaffDeck's portable SOP runtime."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from .original_runtime import SopRuntimeError, prepare, submit


PROTOCOL_VERSION = "2.0"
MODULE_ID = "sop.runtime"
CONTRACT = "sop.lifecycle/v2"
OPERATIONS = ("prepare", "submit")
DESCRIPTOR_VERSION = "1.0"
IMPLEMENTATION_ID = "staffdeck.portable-sop"
TRANSPORT = "sop-http-v2"


class SopRequestEnvelope(BaseModel):
    protocolVersion: Literal["2.0"]
    runId: str
    operationId: str
    requestId: str
    sessionId: str
    turnId: str
    idempotencyKey: str | None = None
    deadlineAt: str | None = None
    expectedRevision: int | None = Field(default=None, ge=0)
    payload: dict[str, Any]


class PreparePayload(BaseModel):
    bundle: dict[str, Any]
    state: dict[str, Any]


class SubmitPayload(PreparePayload):
    proposal: dict[str, Any]
    successfulToolNames: list[str] = Field(default_factory=list)


class SopProtocolError(ValueError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


def create_app() -> FastAPI:
    app = FastAPI(title="StaffDeck SOP Runtime", version=PROTOCOL_VERSION)

    @app.exception_handler(RequestValidationError)
    async def malformed_request(_request, error: RequestValidationError) -> JSONResponse:
        body = error.body if isinstance(error.body, dict) else {}
        request_id = body.get("requestId") if isinstance(body.get("requestId"), str) else "unknown"
        return _failed(
            request_id,
            "SOP_RUNTIME_PROTOCOL",
            "StaffDeck SOP runtime received an invalid protocol envelope.",
            "unsafe",
            {"validation": error.errors()},
            status_code=400,
        )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "protocolVersion": PROTOCOL_VERSION,
            "moduleId": MODULE_ID,
            "contract": CONTRACT,
            "operations": list(OPERATIONS),
            "descriptorVersion": DESCRIPTOR_VERSION,
            "implementationId": IMPLEMENTATION_ID,
            "implementationVersion": "0.1.0",
            "transport": TRANSPORT,
            "capabilities": ["handoff", "external_wait"],
            "state": {"ownership": "host", "schema": CONTRACT, "scope": "session"},
            "requires": {"hostCapabilities": ["sop.host-resume/v1"]},
        }

    @app.post("/v1/sop/prepare")
    def prepare_sop(request: SopRequestEnvelope) -> JSONResponse:
        payload = _parse_or_error(PreparePayload, request)
        if isinstance(payload, JSONResponse):
            return payload
        return _invoke(request, prepare, payload.bundle, payload.state)

    @app.post("/v1/sop/submit")
    def submit_sop(request: SopRequestEnvelope) -> JSONResponse:
        payload = _parse_or_error(SubmitPayload, request)
        if isinstance(payload, JSONResponse):
            return payload
        return _invoke(
            request,
            submit,
            payload.bundle,
            payload.state,
            payload.proposal,
            payload.successfulToolNames,
        )

    return app


def _parse_payload(model: type[BaseModel], request: SopRequestEnvelope) -> BaseModel:
    try:
        return model.model_validate(request.payload)
    except ValidationError as error:
        raise SopProtocolError("StaffDeck SOP runtime received an invalid operation payload.", {"validation": error.errors()}) from error


def _parse_or_error(model: type[BaseModel], request: SopRequestEnvelope) -> BaseModel | JSONResponse:
    try:
        return _parse_payload(model, request)
    except SopProtocolError as error:
        return _failed(request.requestId, "SOP_RUNTIME_PROTOCOL", str(error), "unsafe", error.details, status_code=400)


def _invoke(request: SopRequestEnvelope, handler, *args: Any) -> JSONResponse:
    try:
        payload = handler(*args)
    except SopProtocolError as error:
        return _failed(request.requestId, "SOP_RUNTIME_PROTOCOL", str(error), "unsafe", error.details, status_code=400)
    except SopRuntimeError as error:
        return _failed(request.requestId, error.code, str(error), "unsafe", error.details, status_code=422)
    except ValueError as error:
        return _failed(request.requestId, "SOP_RUNTIME_REJECTED", str(error), "unsafe", status_code=422)
    return JSONResponse({
        "protocolVersion": PROTOCOL_VERSION,
        "requestId": request.requestId,
        "ok": True,
        "outcome": "completed",
        "payload": payload,
    })


def _failed(
    request_id: str,
    code: str,
    message: str,
    retryability: Literal["safe", "unsafe", "retry_after_status"],
    details: dict[str, Any] | None = None,
    *,
    status_code: int,
) -> JSONResponse:
    return JSONResponse({
        "protocolVersion": PROTOCOL_VERSION,
        "requestId": request_id,
        "ok": False,
        "outcome": "failed",
        "error": {
            "code": code,
            "message": message,
            "retryability": retryability,
            "details": details or {},
        },
    }, status_code=status_code)


app = create_app()
