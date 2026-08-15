"""Standalone customer-like HTTP service for exercising tool authentication."""

from __future__ import annotations

import argparse
import base64
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="StaffDeck Tool Auth Matrix")


def _require(condition: bool, scheme: str) -> None:
    if not condition:
        raise HTTPException(status_code=401, detail=f"invalid {scheme} authentication")


@app.post("/basic")
async def basic(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    expected = "Basic " + base64.b64encode(b"demo:secret").decode("ascii")
    _require(authorization == expected, "basic")
    return {"ok": True, "scheme": "basic", "body": await request.json()}


@app.post("/bearer")
async def bearer(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require(authorization == "Bearer bearer-secret", "bearer")
    return {"ok": True, "scheme": "bearer", "body": await request.json()}


@app.post("/api-key")
async def api_key(request: Request, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    _require(x_api_key == "api-key-secret", "api-key")
    return {"ok": True, "scheme": "api-key", "body": await request.json()}


@app.post("/custom")
async def custom(
    request: Request,
    x_customer_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
) -> dict[str, Any]:
    _require(x_customer_token == "customer-secret" and x_tenant_id == "tenant_demo", "custom")
    return {"ok": True, "scheme": "custom", "body": await request.json()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
