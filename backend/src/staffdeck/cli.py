"""Machine-readable CLI; all protocol behavior lives in the SDK resources."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from contextlib import closing
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
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
from .profiles import ProfileStore, connection_url, local_login, revoke_profile

# SDK method -> keyword arguments. A trailing '?' denotes an optional argument.
# There are intentionally no HTTP paths or automatic write sequences here.
_COMMANDS = {
    "system": {"health": "", "schema": "", "whoami": ""},
    "api": {"request": "method path body? idempotency_key? if_match?"},
    "gallery": {
        "list": "limit? cursor? query?", "get": "agent_id", "add": "agent_id idempotency_key?",
    },
    "skills": {
        "list": "agent_id", "create": "agent_id body", "publish": "agent_id slug",
        "archive": "agent_id slug", "test": "agent_id slug body",
    },
    "scheduled_tasks": {
        "list": "agent_id", "create": "agent_id body", "update": "agent_id task_id body",
        "run": "agent_id task_id", "runs": "agent_id task_id", "pause": "agent_id task_id",
        "resume": "agent_id task_id", "archive": "agent_id task_id",
    },
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
        "generate": "agent_id body idempotency_key?",
        "rewrite": "agent_id sop_id body idempotency_key?",
        "get_draft": "agent_id sop_id draft_id",
        "replace": "agent_id sop_id content draft_id if_match",
        "patch": "agent_id sop_id operations draft_id if_match",
        "validate": "agent_id sop_id draft_id", "publish": "agent_id sop_id draft_id",
        "versions": "agent_id sop_id", "rollback": "agent_id sop_id version",
        "get_version": "agent_id sop_id version", "diff": "agent_id sop_id version compare_to",
        "archive": "agent_id sop_id",
    },
    "knowledge_bases": {
        "list": "agent_id", "create": "agent_id body",
        "update": "agent_id knowledge_base_id body", "archive": "agent_id knowledge_base_id",
        "search": "agent_id knowledge_base_id body",
        "upsert_entries": "agent_id knowledge_base_id body idempotency_key?",
        "upload_document": "agent_id knowledge_base_id file_path title?",
        "documents": "agent_id knowledge_base_id",
        "update_document": "agent_id knowledge_base_id document_id body",
        "archive_document": "agent_id knowledge_base_id document_id",
        "versions": "agent_id knowledge_base_id",
        "rollback": "agent_id knowledge_base_id version",
        "concepts": "agent_id knowledge_base_id",
    },
    "runs": {
        "create": "agent_id body idempotency_key?", "get": "run_id", "result": "run_id",
        "artifacts": "run_id",
        "download_artifact": "run_id task_frame_id output_path artifact_path",
        "cancel": "run_id", "wait": "run_id timeout? poll_interval?",
        "events": "run_id last_event_id? max_reconnects?",
    },
    "jobs": {
        "get": "job_id", "result": "job_id", "cancel": "job_id",
        "wait": "job_id timeout? poll_interval?",
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
        raise UsageError("参数不完整或命令不正确；运行 staffdeck --help 查看示例，或使用对应子命令 --help。")


def _parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="staffdeck-cli", description="StaffDeck framework CLI (JSON / NDJSON output).",
    )
    try:
        release = version("skill-agent-loop-backend")
    except PackageNotFoundError:
        release = "0.1.0"
    parser.add_argument("--version", action="version", version=f"StaffDeck CLI {release}")
    parser.add_argument("--base-url", default=os.environ.get("STAFFDECK_BASE_URL"))
    parser.add_argument("--profile", default=os.environ.get("STAFFDECK_PROFILE", "local"))
    parser.add_argument("--http-timeout", type=float, default=30.0, help="Per-request timeout seconds")
    parser.add_argument("--max-retries", type=int, default=2, help="GET retries only; writes are once")
    groups = parser.add_subparsers(dest="resource", required=True)
    groups.add_parser("guide", help="Print the bundled SOP/knowledge coding-agent guide (Markdown)")
    auth = groups.add_parser("auth", help="Connect an OSS account; credentials stay on this device")
    actions = auth.add_subparsers(dest="action", required=True)
    login = actions.add_parser("login")
    login.add_argument("--tenant-id", default=os.environ.get("STAFFDECK_TENANT_ID"))
    login.add_argument("--username", required=True)
    login.add_argument("--password-stdin", action="store_true")
    actions.add_parser("status")
    actions.add_parser("logout")
    chat = groups.add_parser("chat", help="Start/continue a conversation; prints durable receipt first")
    chat.add_argument("--agent-id", required=True)
    chat.add_argument("--message", required=True, help="Text, or - to read UTF-8 text from stdin")
    chat.add_argument("--session-id")
    chat.add_argument("--stream", action="store_true", help="NDJSON events instead of final result")
    chat.add_argument("--timeout", type=float, default=300.0)
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
        if source == "-":
            buffer = getattr(sys.stdin, "buffer", None)
            # Real pipes must not use the locale encoding. Text-only replacements
            # (e.g. StringIO in embedded callers) already contain decoded Unicode.
            text = buffer.read().decode("utf-8") if buffer is not None else sys.stdin.read()
        else:
            text = Path(source).read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, ValueError):
        raise UsageError("Cannot read valid UTF-8 JSON from the supplied input.") from None
    expected = _JSON_TYPES[name]
    if not isinstance(value, expected) or (
        expected is list and any(not isinstance(item, dict) for item in value)
    ):
        raise UsageError(f"JSON {name} must be an object or an array of objects as documented.")
    return value


def _error(kind: str, message: str, *, _secret: str = "", **metadata: Any) -> None:
    secrets = {_secret, os.environ.get("STAFFDECK_API_KEY", "").strip()} - {""}

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {name: redact(item) for name, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    # Protocol keys and the fixed error kind are not credential-bearing data.
    # Redact values before encoding so placeholders like "null" cannot corrupt JSON.
    payload = json.dumps({"error": {"kind": kind, "message": redact(message), **redact(metadata)}})
    print(payload, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    key = ""
    try:
        args = _parser().parse_args(argv)
        if args.resource == "guide":
            print(files("staffdeck").joinpath("skills/staffdeck-cli/SKILL.md").read_text("utf-8"))
            return 0
        key = os.environ.get("STAFFDECK_API_KEY", "").strip()
        if args.resource == "auth":
            return _auth(args)
        public_read = args.resource == "system" and args.sdk_method in {"health", "schema"}
        if not key:
            profile = ProfileStore(args.profile).load()
            if profile:
                if args.base_url and connection_url(args.base_url) != profile["base_url"]:
                    raise UsageError("Profile is bound to another server; select its profile or API key.")
                args.base_url, key = profile["base_url"], profile["api_key"]
        if public_read and not key:
            args.base_url = args.base_url or "http://127.0.0.1:5173"
            key = "anonymous"
        if not key or not args.base_url:
            raise UsageError("Set STAFFDECK_API_KEY and STAFFDECK_BASE_URL (or --base-url).")
        kwargs = {}
        for field in getattr(args, "sdk_fields", []):
            name = field.rstrip("?")
            value = getattr(args, name)
            if value is not None:
                kwargs[name] = _load_json(value, name) if name in _JSON_TYPES else value
        with StaffDeck(
            base_url=args.base_url, api_key=key,
            timeout=args.http_timeout, max_retries=args.max_retries,
        ) as client:
            if args.resource == "chat":
                return _chat(client, args)
            method = getattr(getattr(client, args.sdk_resource), args.sdk_method)
            result = method(**kwargs)
            if args.sdk_resource == "runs" and args.sdk_method == "events":
                with closing(result):
                    for event in result:
                        print(json.dumps(asdict(event)), flush=True)
            else:
                print(json.dumps(asdict(result)), flush=True)
        return 0
    except BrokenPipeError:
        # Avoid another exception from the interpreter's final stdout flush.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 0
    except OSError:
        _error("local_io", "Cannot access CLI profile or input file securely.")
        return 2
    except UsageError as exc:
        _error("usage", str(exc))
        return 2
    except EOFError:
        _error("usage", "No password received; use --password-stdin for a pipe.")
        return 2
    except ValueError:
        _error("usage", "Invalid client option or resource identifier.")
        return 2
    except APIError as exc:
        code = exc.code if isinstance(exc.code, str) else "HTTP_ERROR"
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            code = "HTTP_ERROR"
        _error("api", str(exc), _secret=key, status_code=exc.status_code,
               code=code, request_id=exc.request_id)
        return 1
    except TransportError as exc:
        _error("transport", str(exc))
        return 3
    except RunFailedError as exc:
        if getattr(args, "sdk_resource", None) == "jobs":
            _error("job_failed", "StaffDeck job did not succeed.",
                   job_id=exc.run_id, status=exc.status)
        else:
            _error("run_failed", str(exc), run_id=exc.run_id, status=exc.status)
        return 4
    except WaitTimeout as exc:
        identifier = {"job_id" if getattr(args, "sdk_resource", None) == "jobs" else "run_id": exc.run_id}
        message = (
            "Timed out waiting for StaffDeck; the job was not cancelled."
            if getattr(args, "sdk_resource", None) == "jobs" else str(exc)
        )
        _error("wait_timeout", message, **identifier)
        return 5
    except StreamError as exc:
        _error("stream", str(exc), run_id=exc.run_id, last_event_id=exc.last_event_id)
        return 6
    except ProtocolError as exc:
        _error("protocol", str(exc))
        return 6
    except KeyboardInterrupt:
        _error("interrupted", "Interrupted locally; no remote run was cancelled.")
        return 130


def _auth(args) -> int:
    store = ProfileStore(args.profile)
    profile = store.load()
    if args.action == "login":
        if profile:
            raise UsageError("This profile is already connected; logout first or use another profile.")
        if not args.tenant_id:
            raise UsageError("Set --tenant-id or STAFFDECK_TENANT_ID to this installation's tenant.")
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass()
        profile = local_login(args.base_url or "http://127.0.0.1:5173", args.tenant_id,
                              args.username, password, timeout=args.http_timeout)
        try:
            store.save_new(profile)
        except (OSError, ValueError):
            revoke_profile(profile)
            raise
        print(json.dumps({"connected": True, "profile": args.profile,
                          "base_url": profile["base_url"], "user": profile["user"],
                          "expires_at": profile["expires_at"]}), flush=True)
        return 0
    if not profile:
        raise UsageError("No saved connection for this profile; run auth login first.")
    if args.base_url and connection_url(args.base_url) != profile["base_url"]:
        raise UsageError("Profile is bound to another server.")
    if args.action == "logout":
        revoke_profile(profile)
        store.remove()
        print(json.dumps({"connected": False, "profile": args.profile}), flush=True)
    else:
        with StaffDeck(base_url=profile["base_url"], api_key=profile["api_key"]) as client:
            try:
                result = client.system.whoami()
            except APIError as exc:
                # Do not include a potentially echoed saved token in error output.
                raise APIError(exc.status_code, request_id=exc.request_id) from None
            print(json.dumps(asdict(result)), flush=True)
    return 0


def _chat(client: StaffDeck, args) -> int:
    message = args.message
    if message == "-":
        buffer = getattr(sys.stdin, "buffer", None)
        message = buffer.read().decode("utf-8") if buffer is not None else sys.stdin.read()
    if not message.strip():
        raise UsageError("Chat message cannot be empty.")
    body = {"input": message, "session_mode": "stateful"}
    if args.session_id:
        body.update(session_id=args.session_id)
    receipt = client.runs.create(args.agent_id, body)
    print(json.dumps({"type": "receipt", **asdict(receipt)}), flush=True)
    run_id = receipt.data.get("run_id") or receipt.data.get("id")
    if not run_id:
        raise ProtocolError("Run receipt has no run_id; submission will not be repeated.")
    if args.stream:
        with closing(client.runs.events(run_id)) as events:
            for event in events:
                print(json.dumps(asdict(event)), flush=True)
        state = client.runs.get(run_id).data
        if state.get("status") in {"failed", "cancelled"}:
            raise RunFailedError(run_id, state)
    else:
        print(json.dumps(asdict(client.runs.wait(run_id, timeout=args.timeout))), flush=True)
    return 0
