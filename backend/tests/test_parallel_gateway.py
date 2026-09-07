import httpx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from scripts.deploy.parallel_gateway import ParallelGateway


def test_parallel_prefix_keeps_legacy_routes_and_stream_headers_separate():
    seen = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: stream_delta\ndata: {"content":"hello"}\n\n'
            yield b'event: stream_end\ndata: {}\n\n'

    async def upstream(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

    legacy = Starlette(routes=[Route('/{path:path}', lambda request: PlainTextResponse('legacy'))])
    gateway = ParallelGateway(legacy, 'http://127.0.0.1:10186', client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(upstream), **kw))
    with TestClient(gateway) as client:
        assert client.get('/').text == 'legacy'
        assert client.get('/dsh-other').text == 'legacy'
        assert not seen
        assert client.get('/dsh', follow_redirects=False).headers['location'] == '/dsh/'
        result = client.post('/dsh/api/chat/stream?tenant_id=t1', json={"message": "test"})
        assert result.headers['x-accel-buffering'] == 'no'
        assert 'event: stream_delta' in result.text
        assert str(seen[0].url) == 'http://127.0.0.1:10186/api/chat/stream?tenant_id=t1'
        assert seen[0].headers['x-forwarded-prefix'] == '/dsh'
