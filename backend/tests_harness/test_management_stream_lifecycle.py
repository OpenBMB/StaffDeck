import asyncio
import threading
import pytest
from staffdeck_harness.modules.registry import ModuleRegistry
from staffdeck_harness.runtime.management import ManagedStream
from staffdeck_harness.contracts.runtime_services import StreamingServiceResponse


@pytest.mark.asyncio
async def test_disconnect_closes_upstream_and_releases_generation():
    registry=ModuleRegistry()
    lease=registry.turn_lease()
    lease.__enter__()
    closed=threading.Event()
    incoming=asyncio.Queue()
    sent=[]
    def chunks():
        yield b"data: first\n\n"
        closed.wait(2)
    async def send(message):
        sent.append(message)
        if message["type"]=="http.response.body" and message.get("body"):
            assert registry.live_turns==1
            registry.begin_drain()
            await incoming.put({"type":"http.disconnect"})
    response=ManagedStream(StreamingServiceResponse(200,{"content-type":"text/event-stream"},chunks(),closed.set),
                           lambda:lease.__exit__(None,None,None))
    await asyncio.wait_for(response({"type":"http","asgi":{"spec_version":"2.0"}},incoming.get,send),3)
    assert closed.is_set() and registry.live_turns==0
    assert any(message.get("body")==b"data: first\n\n" for message in sent)
