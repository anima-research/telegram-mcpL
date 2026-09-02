"""End-to-end 0.5 handshake through the real intercepting transport.

Feeds initialize → initialized → featureSets/update (Request) into
`mcpl_stdio_server` with the runner's real dispatcher wiring (policy handler,
manifest handler) and the ready-gated on_ready. Asserts the order the host
depends on: the receipt is answered, and channels/register is sent only
after it. No Telegram client is involved.
"""

import asyncio
import json

import anyio
import pytest

from telegram_mcp.mcpl.capabilities import build_mcpl_capabilities
from telegram_mcp.mcpl.dispatcher import McplDispatcher
from telegram_mcp.mcpl.policy import FS_NAME, FS_USES, PolicyState
from telegram_mcp.mcpl.ready import make_on_ready
from telegram_mcp.mcpl.transport import McplTransport, mcpl_stdio_server
from tests.test_mcpl_transport import FakeAsyncFile


def _frames(*objs):
    return "".join(json.dumps(o) + "\n" for o in objs)


def _wire(policy):
    dispatcher = McplDispatcher()

    async def fsu(params, is_request):
        return policy.apply(params, is_request=is_request)

    dispatcher.register_frame_aware("featureSets/update", fsu)

    async def manifest(params):
        return build_mcpl_capabilities()

    dispatcher.register("mcpl/manifest", manifest)
    return dispatcher


async def _run(stdin_text, policy, on_ready, wait_for):
    transport = McplTransport(_wire(policy))
    stdin = FakeAsyncFile(stdin_text)
    stdout = FakeAsyncFile()
    forwarded = []

    async def consume(read_stream):
        async for item in read_stream:
            forwarded.append(item)

    with anyio.fail_after(3.0):
        async with mcpl_stdio_server(transport, stdin=stdin, stdout=stdout, on_ready=on_ready) as (
            rs,
            ws,
        ):
            consumer = asyncio.create_task(consume(rs))
            for _ in range(200):
                if wait_for(stdout):
                    break
                await asyncio.sleep(0.01)
            await ws.aclose()
            await rs.aclose()
            consumer.cancel()
            try:
                await consumer
            except (asyncio.CancelledError, anyio.ClosedResourceError):
                pass
    return [json.loads(l) for l in stdout.written if l.strip()], forwarded


INIT_0_5 = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {"experimental": {"mcpl": {"version": "0.5", "channels": True}}},
        "clientInfo": {"name": "agent-framework", "version": "0.11.0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
POLICY_REQ = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "featureSets/update",
    "params": {"effectiveCapabilities": list(FS_USES), "enabled": [FS_NAME]},
}
MANIFEST_REQ = {"jsonrpc": "2.0", "id": 3, "method": "mcpl/manifest", "params": {}}


@pytest.mark.asyncio
async def test_0_5_handshake_receipt_then_register():
    policy = PolicyState()

    async def enumerate(client, label):
        return [{"id": f"telegram:{label}:dm:5", "type": "telegram"}]

    attached = []

    async def attach(client, *, account_label, transport, policy):
        attached.append(account_label)

    on_ready = make_on_ready(
        {"default": object()},
        policy=policy,
        enumerate_channels=enumerate,
        attach_event_handlers=attach,
        policy_wait_seconds=2.0,
    )
    out, forwarded = await _run(
        _frames(INIT_0_5, INITIALIZED, POLICY_REQ, MANIFEST_REQ),
        policy,
        on_ready,
        wait_for=lambda so: sum(1 for l in so.written if l.strip()) >= 3,
    )
    methods_or_ids = [(f.get("method"), f.get("id")) for f in out]
    # receipt (id 2) precedes channels/register; manifest (id 3) answered too
    assert methods_or_ids[0] == (None, 2)
    assert out[0]["result"]["accepted"] is True and "mode" not in out[0]["result"]
    assert ("channels/register", None) in methods_or_ids
    assert methods_or_ids.index(("channels/register", None)) > 0
    manifest_resp = next(f for f in out if f.get("id") == 3)
    assert manifest_resp["result"]["version"] == "0.5"
    assert FS_NAME in manifest_resp["result"]["featureSets"]
    assert attached == ["default"]
    # initialize/initialized were forwarded to FastMCP, MCPL frames were not
    fwd_methods = [m.message.root.method for m in forwarded if hasattr(m, "message")]
    assert "initialize" in fwd_methods and "featureSets/update" not in fwd_methods


@pytest.mark.asyncio
async def test_0_5_handshake_register_is_rejected_policy_sends_refusal_and_stays_dark():
    policy = PolicyState()
    called = []

    async def enumerate(client, label):
        called.append(label)
        return []

    async def attach(client, **kw):
        called.append("attach")

    on_ready = make_on_ready(
        {"default": object()},
        policy=policy,
        enumerate_channels=enumerate,
        attach_event_handlers=attach,
        policy_wait_seconds=2.0,
    )
    bad_policy = {"jsonrpc": "2.0", "id": 2, "method": "featureSets/update", "params": {}}
    out, _ = await _run(
        _frames(INIT_0_5, INITIALIZED, bad_policy),
        policy,
        on_ready,
        wait_for=lambda so: any(l.strip() for l in so.written),
    )
    await asyncio.sleep(0.05)
    assert out[0]["id"] == 2
    assert out[0]["result"] == {
        "accepted": False,
        "fallback": "mcp-only",
        "reason": out[0]["result"]["reason"],
        "missingCapabilities": list(FS_USES),
    }
    assert all(f.get("method") != "channels/register" for f in out)
    # nothing privileged ran: no enumeration/registration. Handlers ARE attached —
    # they gate on the live grant, so a later widening Request activates pushes
    # without a restart.
    assert called == ["attach"]
