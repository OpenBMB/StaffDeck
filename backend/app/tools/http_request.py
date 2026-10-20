from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_PATH_PARAM = re.compile(r"\{([A-Za-z0-9_.-]+)\}")


def prepare_get_request(url: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    clean_params = {str(key): value for key, value in (params or {}).items() if value is not None}
    if not clean_params:
        return url, {}

    parsed = urlsplit(url)
    if not parsed.query:
        return url, {"params": clean_params}

    merged_params: dict[str, Any] = dict(parse_qsl(parsed.query, keep_blank_values=True))
    merged_params.update(clean_params)
    merged_url = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(merged_params, doseq=True),
            parsed.fragment,
        )
    )
    return merged_url, {}


def render_path_params(url: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Substitute ``{param}`` placeholders and remove those values from arguments."""

    remaining = dict(arguments or {})
    matched = _PATH_PARAM.findall(url)
    if not matched:
        return url, remaining

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in remaining:
            return match.group(0)
        value = remaining.pop(key)
        return str(value)

    return _PATH_PARAM.sub(replace, url), remaining


def split_http_arguments(
    url: str,
    method: str,
    arguments: dict[str, Any],
    request_options: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, str]]:
    """Split model arguments into path/query/body/header parts.

    Legacy tools (no ``request_options``) keep the historical behaviour: path
    placeholders are substituted when present and every remaining argument is
    sent as query (GET) or JSON body (other methods). Swagger-imported tools
    carry parameter locations so query/header values are not mixed into body.
    """

    rendered_url, remaining = render_path_params(url, arguments)
    options = request_options if isinstance(request_options, dict) else None
    if not options:
        if method.upper() == "GET":
            return rendered_url, remaining, {}, {}
        return rendered_url, {}, remaining, {}

    query_keys = {str(item) for item in options.get("query_params") or []}
    header_keys = {str(item) for item in options.get("header_params") or []}
    query = {key: remaining.pop(key) for key in list(remaining) if key in query_keys}
    headers = {key: remaining.pop(key) for key in list(remaining) if key in header_keys}
    body = remaining
    if method.upper() == "GET":
        query.update(body)
        body = {}
    return (
        rendered_url,
        query,
        body,
        {key: str(value) for key, value in headers.items() if value is not None},
    )
