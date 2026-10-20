from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

try:  # pragma: no cover - exercised through parse_swagger_text fallback
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


HTTP_METHODS = ("get", "post", "put", "patch", "delete")
DRAFT_NAME_MAX_LENGTH = 64
SECRET_PLACEHOLDER = "${secret.API_TOKEN}"
API_KEY_PLACEHOLDER = "${secret.API_KEY}"


class SwaggerParseError(ValueError):
    """Raised when the uploaded document is not a usable OpenAPI/Swagger spec."""


@dataclass
class SwaggerEndpoint:
    name: str
    display_name: str
    description: str
    method: str
    path: str
    url: str
    group: str
    headers: dict[str, str] = field(default_factory=dict)
    auth: dict[str, Any] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    request_options: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    deprecated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "method": self.method,
            "path": self.path,
            "url": self.url,
            "group": self.group,
            "headers": self.headers,
            "auth": self.auth,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "request_options": self.request_options,
            "tags": self.tags,
            "deprecated": self.deprecated,
        }


def parse_swagger_text(
    source_text: str,
    *,
    default_group: str | None = None,
    base_url_override: str | None = None,
) -> dict[str, Any]:
    """Parse an OpenAPI 3 / Swagger 2 document into batch-importable tool drafts.

    The parser deliberately keeps the result plain dictionaries: the HTTP API
    layer only needs to validate/forward them, and the frontend can let the user
    edit every draft before importing.
    """

    document = _load_document(source_text)
    if not isinstance(document, dict):
        raise SwaggerParseError("Swagger 文件必须是一个 JSON/YAML 对象。")
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise SwaggerParseError("Swagger 文件中没有找到 paths，无法导入接口。")

    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    title = str(info.get("title") or "Swagger 导入").strip() or "Swagger 导入"
    version = str(info.get("version") or "").strip()
    group = (default_group or "").strip() or title
    base_url = (base_url_override or "").strip() or _document_base_url(document)

    endpoints: list[SwaggerEndpoint] = []
    warnings: list[str] = []
    used_names: set[str] = set()
    for path, raw_path_item in paths.items():
        if not isinstance(raw_path_item, dict):
            continue
        path_item = _resolve_ref(document, raw_path_item)
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            operation = _resolve_ref(document, operation)
            if not isinstance(operation, dict):
                continue
            path_parameters = path_item.get("parameters")
            if isinstance(path_parameters, list):
                operation = {
                    **operation,
                    "parameters": [
                        *(path_parameters if isinstance(path_parameters, list) else []),
                        *(operation.get("parameters") or []),
                    ],
                }
            endpoint = _build_endpoint(
                document,
                path=str(path),
                method=method.upper(),
                operation=operation,
                base_url=base_url,
                default_group=group,
            )
            if not endpoint:
                continue
            endpoint.name = _unique_name(endpoint.name, used_names)
            if endpoint.deprecated:
                warnings.append(f"{endpoint.method} {endpoint.path} 已标记为 deprecated。")
            endpoints.append(endpoint)

    if not endpoints:
        raise SwaggerParseError("Swagger 文件中没有可导入的 HTTP 接口。")

    return {
        "title": title,
        "version": version,
        "base_url": base_url,
        "group": group,
        "endpoints": [endpoint.to_dict() for endpoint in endpoints],
        "warnings": warnings,
    }


def _load_document(source_text: str) -> Any:
    text = (source_text or "").strip()
    if not text:
        raise SwaggerParseError("Swagger 文件内容为空。")
    if text.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SwaggerParseError(f"JSON 解析失败：{exc}") from exc
    if yaml is None:
        raise SwaggerParseError("当前环境缺少 PyYAML，无法解析 YAML 格式的 Swagger 文件。")
    try:
        return yaml.safe_load(text)
    except Exception as exc:
        raise SwaggerParseError(f"YAML 解析失败：{exc}") from exc


def _build_endpoint(
    document: dict[str, Any],
    *,
    path: str,
    method: str,
    operation: dict[str, Any],
    base_url: str,
    default_group: str,
) -> SwaggerEndpoint | None:
    path_params, query_params, header_params, body_schema, body_params = _operation_parameters(
        document, operation
    )
    input_schema = _object_schema(
        [*path_params, *query_params, *header_params, *body_params]
    )
    if body_schema:
        input_schema = _merge_object_schemas(input_schema, body_schema)

    output_schema = _operation_output_schema(document, operation)
    auth = _operation_auth(document, operation)

    headers: dict[str, str] = {}
    for item in header_params:
        schema = item.get("schema") if isinstance(item.get("schema"), dict) else {}
        default = schema.get("default")
        if default is not None:
            headers[str(item["name"])] = str(default)

    operation_id = str(operation.get("operationId") or "").strip()
    fallback_name = f"{method.lower()}_{path.strip('/') or 'root'}"
    name = _sanitize_name(operation_id or fallback_name) or _sanitize_name(fallback_name)
    display_name = str(
        operation.get("summary")
        or operation.get("title")
        or operation_id
        or f"{method} {path}"
    ).strip()
    description = str(operation.get("description") or operation.get("summary") or "").strip()
    tags = [str(item) for item in operation.get("tags") or [] if str(item).strip()]

    return SwaggerEndpoint(
        name=name,
        display_name=display_name or name,
        description=description,
        method=method,
        path=path,
        url=_join_url(base_url, path),
        group=default_group,
        headers=headers,
        auth=auth,
        input_schema=input_schema,
        output_schema=output_schema,
        request_options={
            "path_params": [item["name"] for item in path_params],
            "query_params": [item["name"] for item in query_params],
            "header_params": [item["name"] for item in header_params],
            "body_params": [item["name"] for item in body_params],
        },
        tags=tags,
        deprecated=bool(operation.get("deprecated")),
    )


def _operation_parameters(
    document: dict[str, Any], operation: dict[str, Any]
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
]:
    path_params: list[dict[str, Any]] = []
    query_params: list[dict[str, Any]] = []
    header_params: list[dict[str, Any]] = []
    body_params: list[dict[str, Any]] = []
    body_schema: dict[str, Any] = {}

    raw_params = operation.get("parameters")
    if isinstance(raw_params, list):
        for raw_param in raw_params:
            param = _resolve_ref(document, raw_param)
            if not isinstance(param, dict):
                continue
            location = str(param.get("in") or "").lower()
            name = str(param.get("name") or "").strip()
            if not name:
                continue
            schema = _parameter_schema(document, param)
            if location == "path":
                path_params.append({"name": name, "required": True, "schema": schema})
            elif location == "query":
                query_params.append(
                    {
                        "name": name,
                        "required": bool(param.get("required")),
                        "schema": schema,
                    }
                )
            elif location == "header":
                header_params.append(
                    {
                        "name": name,
                        "required": bool(param.get("required")),
                        "schema": schema,
                    }
                )
            elif location in {"body"}:
                raw_schema = param.get("schema")
                body_schema = _schema(document, raw_schema)
            elif location in {"formdata", "formData"}:
                body_params.append(
                    {
                        "name": name,
                        "required": bool(param.get("required")),
                        "schema": schema,
                    }
                )

    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        request_body = _resolve_ref(document, request_body)
        content = request_body.get("content") if isinstance(request_body, dict) else None
        if isinstance(content, dict) and content:
            media = content.get("application/json")
            if not isinstance(media, dict):
                media = next(
                    (
                        item
                        for key, item in content.items()
                        if "json" in str(key).lower() and isinstance(item, dict)
                    ),
                    next((item for item in content.values() if isinstance(item, dict)), None),
                )
            if isinstance(media, dict):
                body_schema = _schema(document, media.get("schema"))

    return path_params, query_params, header_params, body_schema, body_params


def _parameter_schema(document: dict[str, Any], param: dict[str, Any]) -> dict[str, Any]:
    raw_schema = param.get("schema")
    if isinstance(raw_schema, dict):
        schema = _schema(document, raw_schema)
    else:
        schema = {
            key: param.get(key)
            for key in (
                "type",
                "format",
                "enum",
                "default",
                "items",
                "minimum",
                "maximum",
                "minLength",
                "maxLength",
                "pattern",
                "description",
            )
            if param.get(key) is not None
        }
    if param.get("description") and not schema.get("description"):
        schema["description"] = str(param["description"])
    if not schema.get("type"):
        schema["type"] = "string"
    return schema


def _operation_output_schema(
    document: dict[str, Any], operation: dict[str, Any]
) -> dict[str, Any]:
    responses = operation.get("responses")
    if not isinstance(responses, dict) or not responses:
        return {}
    success_keys = sorted(
        (str(key) for key in responses if str(key).startswith("2")),
        key=lambda value: (value != "200", value),
    )
    if not success_keys:
        success_keys = ["default"] if "default" in responses else []
    for key in success_keys:
        response = _resolve_ref(document, responses.get(key))
        if not isinstance(response, dict):
            continue
        schema = _response_schema(document, response)
        if schema:
            return schema
    return {}


def _response_schema(document: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    if isinstance(response.get("schema"), dict):
        return _schema(document, response["schema"])
    content = response.get("content")
    if not isinstance(content, dict) or not content:
        return {}
    media = content.get("application/json")
    if not isinstance(media, dict):
        media = next(
            (
                item
                for key, item in content.items()
                if "json" in str(key).lower() and isinstance(item, dict)
            ),
            None,
        )
    if not isinstance(media, dict):
        return {}
    return _schema(document, media.get("schema"))


def _operation_auth(
    document: dict[str, Any], operation: dict[str, Any]
) -> dict[str, Any]:
    security = operation.get("security")
    if not isinstance(security, list):
        security = document.get("security")
    if not isinstance(security, list) or not security:
        return {}

    components = document.get("components") if isinstance(document.get("components"), dict) else {}
    schemes = components.get("securitySchemes")
    if not isinstance(schemes, dict):
        schemes = document.get("securityDefinitions")
    if not isinstance(schemes, dict):
        return {}

    requirement = next((item for item in security if isinstance(item, dict)), None)
    if not requirement:
        return {}

    auth: dict[str, Any] = {}
    for scheme_name in requirement:
        scheme = _resolve_ref(document, schemes.get(scheme_name))
        if not isinstance(scheme, dict):
            continue
        scheme_type = str(scheme.get("type") or "").lower()
        if scheme_type == "http":
            http_scheme = str(scheme.get("scheme") or "").lower()
            if http_scheme == "basic":
                auth = {
                    "type": "basic",
                    "basic": {"username": "${secret.API_USERNAME}", "password": "${secret.API_PASSWORD}"},
                }
            elif http_scheme == "bearer":
                auth = {"type": "bearer", "token": SECRET_PLACEHOLDER}
        elif scheme_type == "basic":
            auth = {
                "type": "basic",
                "basic": {"username": "${secret.API_USERNAME}", "password": "${secret.API_PASSWORD}"},
            }
        elif scheme_type == "apikey":
            if str(scheme.get("in") or "").lower() == "header":
                auth[str(scheme.get("name") or "X-API-Key")] = API_KEY_PLACEHOLDER
        elif scheme_type in {"oauth2", "openidconnect"}:
            auth = {"type": "bearer", "token": SECRET_PLACEHOLDER}
    return auth


def _document_base_url(document: dict[str, Any]) -> str:
    servers = document.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict) and first.get("url"):
            url = str(first["url"]).strip()
            variables = first.get("variables")
            if isinstance(variables, dict):
                for key, variable in variables.items():
                    if isinstance(variable, dict) and variable.get("default") is not None:
                        url = url.replace(
                            "{" + str(key) + "}", str(variable.get("default"))
                        )
            return url
    host = str(document.get("host") or "").strip()
    if not host:
        return ""
    schemes = document.get("schemes")
    scheme = (
        str(schemes[0])
        if isinstance(schemes, list) and schemes
        else "https"
    )
    base_path = str(document.get("basePath") or "").strip()
    return _join_url(f"{scheme}://{host}", base_path)


def _join_url(base_url: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    if not base_url:
        return path
    if not path or path == "/":
        return base_url.rstrip("/")
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _object_schema(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for item in parameters:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        schema = item.get("schema")
        properties[name] = schema if isinstance(schema, dict) else {"type": "string"}
        if item.get("required"):
            required.append(name)
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _merge_object_schemas(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    if not base:
        return extra
    if not extra or extra.get("type") not in {None, "object"}:
        return base
    properties = dict(base.get("properties") or {})
    properties.update(extra.get("properties") or {})
    required = list(dict.fromkeys([*(base.get("required") or []), *(extra.get("required") or [])]))
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


_SCHEMA_KEYS = {
    "type",
    "title",
    "description",
    "format",
    "enum",
    "const",
    "default",
    "example",
    "examples",
    "nullable",
    "readOnly",
    "writeOnly",
    "deprecated",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "additionalProperties",
    "required",
    "discriminator",
    "xml",
}


def _schema(
    document: dict[str, Any],
    raw: Any,
    *,
    seen_refs: tuple[str, ...] = (),
    depth: int = 0,
) -> dict[str, Any]:
    if depth > 24 or not isinstance(raw, dict):
        return {}
    if "$ref" in raw:
        ref = str(raw["$ref"])
        target = _resolve_pointer(document, ref)
        if target is None:
            return {}
        if ref in seen_refs:
            return {"type": "object", "description": "递归引用"}
        resolved = _schema(
            document,
            target,
            seen_refs=(*seen_refs, ref),
            depth=depth + 1,
        )
        siblings = {key: value for key, value in raw.items() if key != "$ref"}
        if siblings:
            resolved = {**resolved, **{key: siblings[key] for key in siblings if key in _SCHEMA_KEYS}}
        return resolved

    result: dict[str, Any] = {}
    for key in _SCHEMA_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key in {"default", "example", "examples", "discriminator", "xml", "additionalProperties"}:
            if key == "additionalProperties" and isinstance(value, dict):
                result[key] = _schema(
                    document, value, seen_refs=seen_refs, depth=depth + 1
                )
            else:
                result[key] = value
            continue
        result[key] = value

    properties = raw.get("properties")
    if isinstance(properties, dict):
        result["properties"] = {
            str(name): _schema(
                document, schema, seen_refs=seen_refs, depth=depth + 1
            )
            for name, schema in properties.items()
        }
    items = raw.get("items")
    if isinstance(items, dict):
        result["items"] = _schema(document, items, seen_refs=seen_refs, depth=depth + 1)
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        value = raw.get(key)
        if isinstance(value, list):
            result[key] = [
                _schema(document, item, seen_refs=seen_refs, depth=depth + 1)
                for item in value
            ]
    return result


def _resolve_ref(document: dict[str, Any], node: Any) -> Any:
    if not isinstance(node, dict) or "$ref" not in node:
        return node
    target = _resolve_pointer(document, str(node["$ref"]))
    if target is None:
        return node
    siblings = {key: value for key, value in node.items() if key != "$ref"}
    if siblings and isinstance(target, dict):
        return {**target, **siblings}
    return target


def _resolve_pointer(document: dict[str, Any], pointer: str) -> Any:
    if not pointer.startswith("#/"):
        return None
    node: Any = document
    for raw_part in pointer[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _sanitize_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:DRAFT_NAME_MAX_LENGTH]


def _unique_name(base: str, used: set[str]) -> str:
    candidate = base or "tool"
    suffix = 2
    while candidate in used:
        tail = f"_{suffix}"
        candidate = f"{base[: DRAFT_NAME_MAX_LENGTH - len(tail)]}{tail}"
        suffix += 1
    used.add(candidate)
    return candidate

