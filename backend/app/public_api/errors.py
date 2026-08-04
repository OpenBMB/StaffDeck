from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class PublicAPIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.errors = errors or []
        self.headers = headers or {}


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    detail: str,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = str(getattr(request.state, "request_id", ""))
    payload: dict[str, Any] = {
        "type": f"urn:staffdeck:error:{code.lower()}",
        "title": code,
        "status": status_code,
        "code": code,
        "detail": detail,
        "request_id": request_id,
    }
    if errors:
        payload["errors"] = errors
    response_headers = {"X-Request-ID": request_id, **(headers or {})}
    return JSONResponse(
        payload,
        status_code=status_code,
        media_type="application/problem+json",
        headers=response_headers,
    )


async def public_api_error_handler(request: Request, exc: PublicAPIError) -> JSONResponse:
    return problem_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        detail=exc.detail,
        errors=exc.errors,
        headers=exc.headers,
    )


async def public_http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = "HTTP_ERROR"
    detail = str(exc.detail)
    if isinstance(exc.detail, str) and exc.detail.isupper():
        code = exc.detail
    return problem_response(
        request,
        status_code=exc.status_code,
        code=code,
        detail=detail,
        headers=dict(exc.headers or {}),
    )


async def public_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = [
        {
            "path": ".".join(str(item) for item in error.get("loc", [])),
            "message": error.get("msg", "Invalid value"),
            "type": error.get("type", "validation_error"),
        }
        for error in exc.errors()
    ]
    return problem_response(
        request,
        status_code=422,
        code="VALIDATION_ERROR",
        detail="The request payload is invalid.",
        errors=errors,
    )
