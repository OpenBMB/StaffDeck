from __future__ import annotations

from email import policy
from email.parser import BytesParser

import httpx
import pytest

from staffdeck import APIError, StaffDeck, TransportError, cli
from staffdeck.knowledge import MAX_DOCUMENT_BYTES


def test_upload_multipart(tmp_path):
    source = tmp_path / "policy.md"
    source.write_text("# 差旅制度\n允许报销", encoding="utf-8")

    def handle(request):
        assert request.method == "POST"
        assert request.url.path == "/proxy/api/v1/agents/a/knowledge-bases/k/documents"
        assert "idempotency-key" not in request.headers
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
        )
        parts = {p.get_param("name", header="content-disposition"): p for p in message.iter_parts()}
        assert parts["file"].get_filename() == "policy.md"
        assert parts["file"].get_payload(decode=True) == source.read_bytes()
        assert parts["title"].get_payload(decode=True).decode() == "制度"
        return httpx.Response(202, json={"id": "job"}, headers={"X-Request-ID": "req"})

    with StaffDeck(
        base_url="https://example.test/proxy/api/v1", api_key="test",
        transport=httpx.MockTransport(handle),
    ) as sdk:
        result = sdk.knowledge_bases.upload_document("a", "k", source, title="制度")
        assert result.status_code == 202 and result.request_id == "req"


@pytest.mark.parametrize("failure", ["transport", "503"])
def test_upload_never_retried(tmp_path, failure):
    source = tmp_path / "policy.md"
    source.write_bytes(b"policy")
    calls = []

    def handle(request):
        calls.append(request)
        if failure == "transport":
            raise httpx.ReadError("connection lost")
        return httpx.Response(503)

    with (
        StaffDeck(base_url="https://test", api_key="key", transport=httpx.MockTransport(handle)) as sdk,
        pytest.raises(TransportError if failure == "transport" else APIError),
    ):
        sdk.knowledge_bases.upload_document("a", "k", source)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["missing", "directory", "oversized", "unsafe-id"])
def test_invalid_upload_never_sends(tmp_path, kind):
    source = tmp_path / "doc.md"
    if kind == "directory":
        source.mkdir()
    elif kind == "oversized":
        with source.open("wb") as stream:
            stream.truncate(MAX_DOCUMENT_BYTES + 1)
    elif kind == "unsafe-id":
        source.write_bytes(b"text")
    transport = httpx.MockTransport(lambda _: pytest.fail("must not send"))
    with (
        StaffDeck(base_url="https://test", api_key="key", transport=transport) as sdk,
        pytest.raises(ValueError),
    ):
        sdk.knowledge_bases.upload_document("a", "../bad" if kind == "unsafe-id" else "k", source)


def test_cli_missing_upload_is_safe(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("STAFFDECK_BASE_URL", "https://example.test")
    monkeypatch.setenv("STAFFDECK_API_KEY", "secret-key")
    assert cli.main([
        "knowledge-bases", "upload-document", "--agent-id", "a", "--knowledge-base-id", "k",
        "--file-path", str(tmp_path / "sensitive-missing-file"),
    ]) == 2
    out = capsys.readouterr()
    assert not out.out
    assert "sensitive-missing-file" not in out.err and "secret-key" not in out.err


@pytest.mark.parametrize("kwargs", [
    {"method": "GET", "files": {"file": ("f", b"data", "text/plain")}},
    {"method": "POST", "body": {}, "files": {"file": ("f", b"data", "text/plain")}},
    {"method": "POST", "form": {"title": "x"}},
    {"method": "POST", "form": {"tenant_id": "x"},
     "files": {"file": ("f", b"data", "text/plain")}},
])
def test_invalid_multipart_never_sends(kwargs):
    transport = httpx.MockTransport(lambda _: pytest.fail("must not send"))
    with (
        StaffDeck(base_url="https://test", api_key="key", transport=transport) as sdk,
        pytest.raises(ValueError),
    ):
        sdk.request(path="agents", **kwargs)
