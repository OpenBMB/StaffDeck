import httpx
import pytest
from app.tools.mcp_client import MCPClientError, _HttpSession, _SseSession


@pytest.mark.parametrize("session_type", [_HttpSession, _SseSession])
def test_failed_mcp_request_identifies_endpoint_without_credentials(session_type):
    url = "https://fake-user:fake-password@mcp.example.test/messages?token=fake-token#fake-fragment"
    session = session_type({"url": url}, timeout_seconds=1)
    if session_type is _SseSession:
        session._message_url = url
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401))) as client:
        session._client = client
        with pytest.raises(MCPClientError) as error:
            session._request("tools/list", {})
    message = str(error.value)
    assert "401" in message and "https://mcp.example.test/messages" in message
    assert all(secret not in message for secret in ("fake-user", "fake-password", "fake-token", "fake-fragment"))
