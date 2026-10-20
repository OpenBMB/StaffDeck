from __future__ import annotations

from app.tools.http_request import render_path_params, split_http_arguments
from app.tools.swagger_import import SwaggerParseError, parse_swagger_text

OPENAPI_SPEC = """
openapi: 3.0.3
info:
  title: 订单服务
  version: 1.0.0
servers:
  - url: https://api.example.com/v1
security:
  - bearerAuth: []
paths:
  /orders/{orderId}:
    get:
      operationId: getOrder
      summary: 查询订单
      parameters:
        - name: orderId
          in: path
          required: true
          schema: {type: string}
        - name: expand
          in: query
          schema: {type: string}
        - name: X-Trace-Id
          in: header
          schema: {type: string}
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Order'
  /orders:
    post:
      operationId: createOrder
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [amount]
              properties:
                amount: {type: number}
                note: {type: string}
      responses:
        '201':
          description: created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Order'
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
  schemas:
    Order:
      type: object
      properties:
        id: {type: string}
        amount: {type: number}
"""


def test_parse_openapi_extracts_schemas_locations_and_auth() -> None:
    parsed = parse_swagger_text(OPENAPI_SPEC)

    assert parsed["title"] == "订单服务"
    assert parsed["base_url"] == "https://api.example.com/v1"
    assert [endpoint["name"] for endpoint in parsed["endpoints"]] == [
        "getOrder",
        "createOrder",
    ]

    get_order = parsed["endpoints"][0]
    assert get_order["method"] == "GET"
    assert get_order["url"] == "https://api.example.com/v1/orders/{orderId}"
    assert get_order["request_options"] == {
        "path_params": ["orderId"],
        "query_params": ["expand"],
        "header_params": ["X-Trace-Id"],
        "body_params": [],
    }
    assert list(get_order["input_schema"]["properties"]) == [
        "orderId",
        "expand",
        "X-Trace-Id",
    ]
    assert list(get_order["output_schema"]["properties"]) == ["id", "amount"]
    assert get_order["auth"] == {"type": "bearer", "token": "${secret.API_TOKEN}"}

    create_order = parsed["endpoints"][1]
    assert create_order["method"] == "POST"
    assert create_order["input_schema"]["required"] == ["amount"]
    assert set(create_order["input_schema"]["properties"]) == {"amount", "note"}


def test_parse_swagger_2_uses_host_base_path_and_unique_names() -> None:
    parsed = parse_swagger_text(
        """
swagger: '2.0'
info: {title: Legacy, version: '2'}
host: legacy.example.com
basePath: /api
schemes: [https]
paths:
  /ping:
    get:
      summary: Ping once
      responses:
        '200': {description: ok}
    post:
      summary: Ping twice
      responses:
        '200': {description: ok}
"""
    )

    assert parsed["base_url"] == "https://legacy.example.com/api"
    names = [endpoint["name"] for endpoint in parsed["endpoints"]]
    assert names == ["get_ping", "post_ping"]


def test_parse_rejects_documents_without_paths() -> None:
    try:
        parse_swagger_text("openapi: 3.0.3\ninfo: {title: Empty}")
    except SwaggerParseError as exc:
        assert "paths" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected SwaggerParseError")


def test_split_http_arguments_routes_swagger_parameters() -> None:
    url, query, body, headers = split_http_arguments(
        "https://example.test/orders/{orderId}",
        "POST",
        {"orderId": "o-1", "dryRun": True, "X-Trace-Id": "trace", "amount": 3},
        {
            "path_params": ["orderId"],
            "query_params": ["dryRun"],
            "header_params": ["X-Trace-Id"],
            "body_params": ["amount"],
        },
    )

    assert url == "https://example.test/orders/o-1"
    assert query == {"dryRun": True}
    assert body == {"amount": 3}
    assert headers == {"X-Trace-Id": "trace"}


def test_render_path_params_keeps_unmatched_placeholders() -> None:
    url, remaining = render_path_params("/orders/{orderId}/{missing}", {"orderId": "o-1"})
    assert url == "/orders/o-1/{missing}"
    assert remaining == {}
