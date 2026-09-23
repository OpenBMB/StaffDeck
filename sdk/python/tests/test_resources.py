from __future__ import annotations

import json

import httpx
import pytest

from staffdeck import StaffDeck

# One wire assertion for every named resource method; schema is owned by the server.
CASES = [
    ("agents", "list", [], {}, "GET", "agents", {"limit": "50"}, None),
    ("agents", "get", ["a"], {}, "GET", "agents/a", {}, None),
    ("agents", "create", [{"name": "x"}], {"idempotency_key": "key"},
     "POST", "agents", {}, {"name": "x"}),
    ("agents", "update", ["a", {"name": "y"}], {"if_match": '"etag"'},
     "PATCH", "agents/a", {}, {"name": "y"}),
    ("agents", "capabilities", ["a"], {}, "GET", "agents/a/capabilities", {}, None),
    ("agents", "resources", ["a"], {}, "GET", "agents/a/resources", {}, None),
    ("agents", "set_resources", ["a", []], {}, "PUT", "agents/a/resources", {}, {"resources": []}),
    ("sessions", "list", ["a"], {}, "GET", "agents/a/sessions", {"limit": "50"}, None),
    ("sessions", "create", ["a"], {}, "POST", "agents/a/sessions", {}, {}),
    ("sessions", "get", ["a", "session"], {}, "GET", "agents/a/sessions/session", {}, None),
    ("sessions", "update", ["a", "session", {"title": "new"}], {"if_match": '"etag"'},
     "PATCH", "agents/a/sessions/session", {}, {"title": "new"}),
    ("tools", "list", ["a"], {}, "GET", "agents/a/tools", {}, None),
    ("tools", "create", ["a", {"name": "tool"}], {}, "POST", "agents/a/tools", {}, {"name": "tool"}),
    ("tools", "update", ["a", "t", {}], {}, "PUT", "agents/a/tools/t", {}, {}),
    ("tools", "test", ["a", "t", {"args": {}}], {}, "POST", "agents/a/tools/t:test", {}, {"args": {}}),
    ("mcp_servers", "list", ["a"], {}, "GET", "agents/a/mcp-servers", {}, None),
    ("mcp_servers", "create", ["a", {}], {}, "POST", "agents/a/mcp-servers", {}, {}),
    ("mcp_servers", "update", ["a", "m", {}], {}, "PUT", "agents/a/mcp-servers/m", {}, {}),
    ("mcp_servers", "discover", ["a", "m"], {}, "POST", "agents/a/mcp-servers/m:discover", {}, None),
    ("mcp_servers", "sync", ["a", "m", {}], {}, "POST", "agents/a/mcp-servers/m:sync", {}, {}),
    ("sops", "list", ["a"], {}, "GET", "agents/a/sops", {}, None),
    ("sops", "generate", ["a", {"title": "流程", "raw_content": "回答问题"}],
     {"idempotency_key": "key"}, "POST", "agents/a/sops:generate", {},
     {"title": "流程", "raw_content": "回答问题"}),
    ("sops", "rewrite", ["a", "s", {"instruction": "改写", "draft_id": "d"}],
     {"idempotency_key": "key"}, "POST", "agents/a/sops/s:rewrite", {},
     {"instruction": "改写", "draft_id": "d"}),
    ("sops", "get_version", ["a", "s", "1.0"], {}, "GET", "sops/s/versions/1.0",
     {"agent_id": "a"}, None),
    ("sops", "diff", ["a", "s", "2.0"], {"compare_to": "1.0"}, "GET", "sops/s/versions/2.0/diff",
     {"agent_id": "a", "compare_to": "1.0"}, None),
    ("sops", "archive", ["a", "s"], {}, "POST", "sops/s:archive", {"agent_id": "a"}, None),
    ("knowledge_bases", "list", ["a"], {}, "GET", "agents/a/knowledge-bases", {}, None),
    ("knowledge_bases", "create", ["a", {"name": "制度"}], {}, "POST", "agents/a/knowledge-bases",
     {}, {"name": "制度"}),
    ("knowledge_bases", "update", ["a", "k", {"name": "新制度"}], {}, "PATCH",
     "agents/a/knowledge-bases/k", {}, {"name": "新制度"}),
    ("knowledge_bases", "archive", ["a", "k"], {}, "POST",
     "agents/a/knowledge-bases/k:archive", {}, None),
    ("knowledge_bases", "search", ["a", "k", {"query": "报销"}], {}, "POST",
     "agents/a/knowledge-bases/k:search", {}, {"query": "报销"}),
    ("knowledge_bases", "upsert_entries", ["a", "k", {"entries": [{"content": "文本"}]}],
     {"idempotency_key": "key"}, "POST", "agents/a/knowledge-bases/k/entries", {},
     {"entries": [{"content": "文本"}]}),
    ("knowledge_bases", "documents", ["a", "k"], {}, "GET",
     "agents/a/knowledge-bases/k/documents", {}, None),
    ("knowledge_bases", "update_document", ["a", "k", "d", {"content_md": "新正文"}], {}, "PATCH",
     "agents/a/knowledge-bases/k/documents/d", {}, {"content_md": "新正文"}),
    ("knowledge_bases", "archive_document", ["a", "k", "d"], {}, "POST",
     "agents/a/knowledge-bases/k/documents/d:archive", {}, None),
    ("knowledge_bases", "versions", ["a", "k"], {}, "GET",
     "agents/a/knowledge-bases/k/versions", {}, None),
    ("knowledge_bases", "rollback", ["a", "k", "1.0"], {}, "POST",
     "agents/a/knowledge-bases/k:rollback", {}, {"version": "1.0"}),
    ("knowledge_bases", "concepts", ["a", "k"], {}, "GET",
     "agents/a/knowledge-bases/k/concepts", {}, None),
    ("sops", "create", ["a", {"skill_id": "s"}], {"idempotency_key": "key"},
     "POST", "agents/a/sops", {}, {"content": {"skill_id": "s"}}),
    ("sops", "get_draft", ["a", "s", "d"], {}, "GET", "agents/a/sops/s/drafts/d", {}, None),
    ("sops", "replace", ["a", "s", {}], {"draft_id": "d", "if_match": '"etag"'},
     "PUT", "agents/a/sops/s", {"draft_id": "d"}, {"content": {}}),
    ("sops", "patch", ["a", "s", []], {"draft_id": "d", "if_match": '"etag"'},
     "PATCH", "agents/a/sops/s", {"draft_id": "d"}, []),
    ("sops", "validate", ["a", "s", "d"], {}, "POST", "sops/s:validate",
     {"agent_id": "a", "draft_id": "d"}, None),
    ("sops", "publish", ["a", "s", "d"], {}, "POST", "sops/s:publish",
     {"agent_id": "a"}, {"draft_id": "d"}),
    ("sops", "versions", ["a", "s"], {}, "GET", "sops/s/versions", {"agent_id": "a"}, None),
    ("sops", "rollback", ["a", "s", "1.0"], {}, "POST", "sops/s/versions/1.0:rollback",
     {"agent_id": "a"}, None),
    ("runs", "create", ["a", {"input": "hi"}], {"idempotency_key": "key"},
     "POST", "agents/a/runs", {}, {"input": "hi"}),
    ("runs", "get", ["r"], {}, "GET", "runs/r", {}, None),
    ("runs", "result", ["r"], {}, "GET", "runs/r/result", {}, None),
    ("runs", "cancel", ["r"], {}, "POST", "runs/r:cancel", {}, None),
    ("jobs", "get", ["j"], {}, "GET", "jobs/j", {}, None),
    ("jobs", "result", ["j"], {}, "GET", "jobs/j/result", {}, None),
    ("jobs", "cancel", ["j"], {}, "POST", "jobs/j:cancel", {}, None),
]


@pytest.mark.parametrize("resource,operation,args,kwargs,method,path,query,body", CASES)
def test_resource_wire_contract(resource, operation, args, kwargs, method, path, query, body):
    def handle(request):
        assert request.method == method
        assert request.url.path == "/api/v1/" + path
        assert dict(request.url.params) == query
        assert (json.loads(request.content) if request.content else None) == body
        if "idempotency_key" in kwargs:
            assert request.headers["idempotency-key"] == "key"
        if "if_match" in kwargs:
            assert request.headers["if-match"] == '"etag"'
        if operation == "patch":
            assert request.headers["content-type"] == "application/json-patch+json"
        return httpx.Response(200, json={"ok": True})

    with StaffDeck(base_url="https://test", api_key="key", transport=httpx.MockTransport(handle)) as sdk:
        assert getattr(getattr(sdk, resource), operation)(*args, **kwargs).data == {"ok": True}


@pytest.mark.parametrize("value", ["", "..", "a/b", "a\\b", "%2e%2e", "a\nb"])
def test_invalid_resource_id_never_sends(value):
    transport = httpx.MockTransport(lambda r: pytest.fail("must not send"))
    with (
        StaffDeck(base_url="https://test", api_key="key", transport=transport) as sdk,
        pytest.raises(ValueError),
    ):
        sdk.agents.get(value)
