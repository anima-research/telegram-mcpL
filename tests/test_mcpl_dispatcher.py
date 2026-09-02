"""Phase 2 tests — dispatcher routing logic, decoupled from real I/O."""

import asyncio
import json
from typing import Any

import anyio
import pytest

from telegram_mcp.mcpl.dispatcher import McplDispatcher, McplError
from telegram_mcp.mcpl.transport import McplTransport


class FakeTransport:
    """Captures outbound calls without serialising over a real stream."""

    def __init__(self) -> None:
        self.errors: list[tuple[Any, int, str]] = []
        self.responses: list[tuple[Any, Any]] = []
        self.requests: list[tuple[int, str, dict]] = []
        self.notifications: list[tuple[str, dict | None]] = []

    async def send_error(self, msg_id: Any, code: int, message: str, data: Any = None) -> None:
        self.errors.append((msg_id, code, message))

    async def send_response(self, msg_id: Any, result: Any) -> None:
        self.responses.append((msg_id, result))

    async def send_request(self, msg_id: int, method: str, params: dict) -> None:
        self.requests.append((msg_id, method, params))

    async def send_notification(self, method: str, params: dict | None) -> None:
        self.notifications.append((method, params))


@pytest.mark.asyncio
async def test_request_with_handler_returns_result():
    d = McplDispatcher()
    d.register(
        "channels/publish", lambda params: _async_result({"delivered": True, "messageId": "42"})
    )
    t = FakeTransport()

    await d.handle_inbound(
        {"jsonrpc": "2.0", "id": 1, "method": "channels/publish", "params": {}},
        t,
    )

    assert t.responses == [(1, {"delivered": True, "messageId": "42"})]
    assert not t.errors


@pytest.mark.asyncio
async def test_request_without_handler_returns_method_not_found():
    d = McplDispatcher()
    t = FakeTransport()

    await d.handle_inbound(
        {"jsonrpc": "2.0", "id": 7, "method": "channels/publish", "params": {}},
        t,
    )

    assert len(t.errors) == 1
    msg_id, code, message = t.errors[0]
    assert msg_id == 7
    assert code == -32601
    assert "channels/publish" in message


@pytest.mark.asyncio
async def test_notification_without_handler_is_dropped_silently():
    d = McplDispatcher()
    t = FakeTransport()

    await d.handle_inbound(
        {"jsonrpc": "2.0", "method": "channels/changed", "params": {"added": []}},
        t,
    )

    # No response, no error — notification is fire-and-forget.
    assert not t.errors
    assert not t.responses


@pytest.mark.asyncio
async def test_handler_exception_yields_internal_error_for_request():
    d = McplDispatcher()

    async def boom(params):
        raise RuntimeError("kaboom")

    d.register("channels/publish", boom)
    t = FakeTransport()

    await d.handle_inbound(
        {"jsonrpc": "2.0", "id": 9, "method": "channels/publish", "params": {}},
        t,
    )

    assert len(t.errors) == 1
    msg_id, code, message = t.errors[0]
    assert msg_id == 9
    assert code == -32603
    assert "kaboom" in message


@pytest.mark.asyncio
async def test_outbound_request_correlates_response_by_id():
    d = McplDispatcher()
    t = FakeTransport()

    async def driver():
        # Wait until send_request has emitted on the wire and registered its future.
        while not t.requests:
            await asyncio.sleep(0)
        msg_id = t.requests[-1][0]
        d.resolve_outbound(msg_id, {"jsonrpc": "2.0", "id": msg_id, "result": {"ok": True}})

    request_task = asyncio.create_task(d.send_request("channels/incoming", {"messages": []}, t))
    driver_task = asyncio.create_task(driver())
    result, _ = await asyncio.gather(request_task, driver_task)

    assert result == {"ok": True}
    assert len(t.requests) == 1
    assert t.requests[0][1] == "channels/incoming"


@pytest.mark.asyncio
async def test_outbound_request_raises_on_error_response():
    d = McplDispatcher()
    t = FakeTransport()

    async def driver():
        while not t.requests:
            await asyncio.sleep(0)
        msg_id = t.requests[-1][0]
        d.resolve_outbound(
            msg_id,
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32000, "message": "no such channel"},
            },
        )

    request_task = asyncio.create_task(d.send_request("channels/incoming", {"messages": []}, t))
    driver_task = asyncio.create_task(driver())

    with pytest.raises(McplError) as exc_info:
        await asyncio.gather(request_task, driver_task)

    assert exc_info.value.code == -32000
    assert "no such channel" in exc_info.value.message


# -- helpers -----------------------------------------------------------------


async def _async_result(value):
    return value


# ---------------------------------------------------------------------------
# Frame-aware handlers (featureSets/update is dual-mode, §6.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frame_aware_handler_sees_request_vs_notification():
    dispatcher = McplDispatcher()
    transport = McplTransport(dispatcher)
    send_stream, receive_stream = anyio.create_memory_object_stream(4)
    transport._bind_write_stream(send_stream)
    seen = []

    async def handler(params, is_request):
        seen.append((params, is_request))
        return {"accepted": True} if is_request else None

    dispatcher.register_frame_aware("featureSets/update", handler)

    await dispatcher.handle_inbound(
        {"jsonrpc": "2.0", "id": 3, "method": "featureSets/update", "params": {"a": 1}}, transport
    )
    await dispatcher.handle_inbound(
        {"jsonrpc": "2.0", "method": "featureSets/update", "params": {"b": 2}}, transport
    )
    assert seen == [({"a": 1}, True), ({"b": 2}, False)]
    payload = json.loads(
        receive_stream.receive_nowait().message.model_dump_json(by_alias=True, exclude_none=True)
    )
    assert payload == {"jsonrpc": "2.0", "id": 3, "result": {"accepted": True}}
    with pytest.raises(anyio.WouldBlock):
        receive_stream.receive_nowait()  # notification produced no response
