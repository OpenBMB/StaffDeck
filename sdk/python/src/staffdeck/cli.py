"""Machine-readable CLI; all protocol behavior lives in the SDK resources."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .client import StaffDeck
from .errors import (
    APIError,
    ProtocolError,
    RunFailedError,
    StreamError,
    TransportError,
    WaitTimeout,
)

# SDK method -> keyword arguments. A trailing '?' denotes an optional argument.
# There are intentionally no HTTP paths or automatic write sequences here.
_COMMANDS = {
    "agents": {
        "list": "limit?", "get": "agent_id",
        "create": "body idempotency_key?", "update": "agent_id body if_match",
        "capabilities": "agent_id", "resources": "agent_id",
        "set_resources": "agent_id resources",
    },
    "sessions": {
        "list": "agent_id limit?", "get": "agent_id session_id",
        "create": "agent_id body? idempotency_key?",
        "update": "agent_id session_id body if_match",
    },
    "tools": {
        "list": "agent_id", "create": "agent_id body",
        "update": "agent_id tool_id body", "test": "agent_id tool_id body",
    },
    "mcp_servers": {
        "list": "agent_id", "create": "agent_id body", "update": "agent_id server_id body",
        "discover": "agent_id server_id", "sync": "agent_id server_id body",
    },
    "sops": {
        "list": "agent_id", "create": "agent_id content idempotency_key?",
        "get_draft": "agent_id sop_id draft_id",
        "replace": "agent_id sop_id content draft_id if_match",
        "patch": "agent_id sop_id operations draft_id if_match",
        "validate": "agent_id sop_id draft_id", "publish": "agent_id sop_id draft_id",
        "versions": "agent_id sop_id", "rollback": "agent_id sop_id version",
    },
    "runs": {
        "create": "agent_id body idempotency_key?", "get": "run_id", "result": "run_id",
        "cancel": "run_id", "wait": "run_id timeout? poll_interval?",
        "events": "run_id last_event_id? max_reconnects?",
    },
}
_JSON_TYPES = {"body": dict, "content": dict, "operations": list, "resources": list}
_NUMERIC_TYPES = {
    "limit": int, "timeout": float, "poll_interval": float, "max_reconnects": int,
}


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default includes raw user input, which may contain secrets.
        raise UsageError("Invalid arguments; use staffdeck-api [resource] [command] --help.")


def _parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="staffdeck-api", description="StaffDeck Open API v1 client (JSON / NDJSON output).",
    )
    parser.add_argument("--base-url", default=os.environ.get("STAFFDECK_BASE_URL"))
    parser.add_argument("--http-timeout", type=float, default=30.0, help="Per-request timeout seconds")
    parser.add_argument("--max-retries", type=int, default=2, help="GET retries only; writes are once")
    groups = parser.add_subparsers(dest="resource", required=True)
    for resource, commands in _COMMANDS.items():
        group = groups.add_parser(resource.replace("_", "-"))
        actions = group.add_subparsers(dest="action", required=True)
        for method, fields in commands.items():
            action = actions.add_parser(method.replace("_", "-"))
            action.set_defaults(sdk_resource=resource, sdk_method=method, sdk_fields=fields.split())
            for field in fields.split():
                name = field.rstrip("?")
                flag = "--json" if name in _JSON_TYPES else "--" + name.replace("_", "-")
                options: dict[str, Any] = {"dest": name, "required": not field.endswith("?")}
                if name in _JSON_TYPES:
                    options.update(metavar="FILE|-", help=f"JSON {name} from file or stdin (-)")
                elif name in _NUMERIC_TYPES:
                    options["type"] = _NUMERIC_TYPES[name]
                action.add_argument(flag, **options)
    return parser


def _load_json(source: str, name: str) -> Any:
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, ValueError):
        raise UsageError("Cannot read valid UTF-8 JSON from the supplied input.") from None
    expected = _JSON_TYPES[name]
    if not isinstance(value, expected) or (
        expected is list and any(not isinstance(item, dict) for item in value)
    ):
        raise UsageError(f"JSON {name} must be an object or an array of objects as documented.")
    return value


def _error(kind: str, message: str, **metadata: Any) -> None:
    payload = json.dumps({"error": {"kind": kind, "message": message, **metadata}})
    key = os.environ.get("STAFFDECK_API_KEY", "").strip()
    if key:
        payload = payload.replace(json.dumps(key)[1:-1], "[REDACTED]")
    print(payload, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        key = os.environ.get("STAFFDECK_API_KEY", "").strip()
        if not key or not args.base_url:
            raise UsageError("Set STAFFDECK_API_KEY and STAFFDECK_BASE_URL (or --base-url).")
        kwargs = {}
        for field in args.sdk_fields:
            name = field.rstrip("?")
            value = getattr(args, name)
            if value is not None:
                kwargs[name] = _load_json(value, name) if name in _JSON_TYPES else value
        with StaffDeck(
            base_url=args.base_url, api_key=key,
            timeout=args.http_timeout, max_retries=args.max_retries,
        ) as client:
            method = getattr(getattr(client, args.sdk_resource), args.sdk_method)
            result = method(**kwargs)
            if args.sdk_resource == "runs" and args.sdk_method == "events":
                with closing(result):
                    for event in result:
                        print(json.dumps(asdict(event)), flush=True)
            else:
                print(json.dumps(asdict(result)), flush=True)
        return 0
    except UsageError as exc:
        _error("usage", str(exc))
        return 2
    except ValueError:
        _error("usage", "Invalid client option or resource identifier.")
        return 2
    except APIError as exc:
        code = exc.code if isinstance(exc.code, str) else "HTTP_ERROR"
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            code = "HTTP_ERROR"
        _error("api", str(exc), status_code=exc.status_code, code=code, request_id=exc.request_id)
        return 1
    except TransportError as exc:
        _error("transport", str(exc))
        return 3
    except RunFailedError as exc:
        _error("run_failed", str(exc), run_id=exc.run_id, status=exc.status)
        return 4
    except WaitTimeout as exc:
        _error("wait_timeout", str(exc), run_id=exc.run_id)
        return 5
    except StreamError as exc:
        _error("stream", str(exc), run_id=exc.run_id, last_event_id=exc.last_event_id)
        return 6
    except ProtocolError as exc:
        _error("protocol", str(exc))
        return 6
    except BrokenPipeError:
        # Avoid a second error from Python's final stdout flush (e.g. piped to head).
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        _error("interrupted", "Interrupted locally; no remote run was cancelled.")
        return 130
