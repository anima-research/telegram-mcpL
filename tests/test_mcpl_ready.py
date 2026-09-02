"""on_ready ordering under MCPL 0.5: registration waits for the policy receipt."""

import asyncio
import json

import anyio
import pytest

from telegram_mcp.mcpl.dispatcher import McplDispatcher
from telegram_mcp.mcpl.policy import FS_NAME, FS_USES, PolicyState
from telegram_mcp.mcpl.ready import make_on_ready
from telegram_mcp.mcpl.transport import McplTransport

FULL = {"effectiveCapabilities": list(FS_USES), "enabled": [FS_NAME]}


def _transport(host_version):
    t = McplTransport(McplDispatcher())
    send, recv = anyio.create_memory_object_stream(8)
    t._bind_write_stream(send)
    t.host_capabilities = {"version": host_version} if host_version else None
    return t, recv


def _drain(recv):
    out = []
    while True:
        try:
            m = recv.receive_nowait()
        except anyio.WouldBlock:
            return out
        out.append(json.loads(m.message.model_dump_json(by_alias=True, exclude_none=True)))


class Recorder:
    def __init__(self):
        self.enumerated = []
        self.attached = []

    async def enumerate(self, client, label):
        self.enumerated.append(label)
        return [{"id": f"telegram:{label}:dm:1", "type": "telegram"}]

    async def attach(self, client, *, account_label, transport, policy):
        self.attached.append((account_label, policy))


@pytest.mark.asyncio
async def test_0_5_host_registration_waits_for_policy_then_registers():
    policy = PolicyState()
    rec = Recorder()
    t, recv = _transport("0.5")
    on_ready = make_on_ready(
        {"default": object()},
        policy=policy,
        enumerate_channels=rec.enumerate,
        attach_event_handlers=rec.attach,
        policy_wait_seconds=1.0,
    )
    task = asyncio.get_running_loop().create_task(on_ready(t))
    await asyncio.sleep(0.05)
    assert rec.enumerated == [] and _drain(recv) == []  # nothing before the receipt
    policy.apply(FULL, is_request=True)
    await asyncio.wait_for(task, 1.0)
    frames = _drain(recv)
    assert [f["method"] for f in frames] == ["channels/register"]
    assert frames[0]["params"]["channels"][0]["id"] == "telegram:default:dm:1"
    assert rec.attached == [("default", policy)]


@pytest.mark.asyncio
async def test_0_5_host_that_never_answers_leaves_surface_dark():
    policy = PolicyState()
    rec = Recorder()
    t, recv = _transport("0.5")
    on_ready = make_on_ready(
        {"default": object()},
        policy=policy,
        enumerate_channels=rec.enumerate,
        attach_event_handlers=rec.attach,
        policy_wait_seconds=0.05,
    )
    await asyncio.wait_for(on_ready(t), 1.0)
    assert _drain(recv) == [] and rec.enumerated == [] and rec.attached == []


@pytest.mark.asyncio
async def test_0_5_host_denying_register_skips_registration_but_attaches_handlers():
    policy = PolicyState()
    policy.apply(
        {"effectiveCapabilities": [c for c in FS_USES if c != "channels.register"]},
        is_request=True,
    )
    rec = Recorder()
    t, recv = _transport("0.5")
    on_ready = make_on_ready(
        {"default": object()},
        policy=policy,
        enumerate_channels=rec.enumerate,
        attach_event_handlers=rec.attach,
        policy_wait_seconds=1.0,
    )
    await asyncio.wait_for(on_ready(t), 1.0)
    assert _drain(recv) == [] and rec.enumerated == []
    assert rec.attached == [("default", policy)]  # a later widening activates without restart


@pytest.mark.asyncio
async def test_pre_0_5_host_registers_immediately_without_policy():
    policy = PolicyState()
    rec = Recorder()
    t, recv = _transport("0.4")
    on_ready = make_on_ready(
        {"a": object(), "b": object()},
        policy=policy,
        enumerate_channels=rec.enumerate,
        attach_event_handlers=rec.attach,
        policy_wait_seconds=5.0,
    )
    await asyncio.wait_for(on_ready(t), 1.0)
    frames = _drain(recv)
    assert [f["method"] for f in frames] == ["channels/register"]
    assert len(frames[0]["params"]["channels"]) == 2
    assert [a for a, _ in rec.attached] == ["a", "b"]


@pytest.mark.asyncio
async def test_enumeration_failure_for_one_account_does_not_block_others():
    policy = PolicyState()
    policy.apply(FULL, is_request=True)

    async def flaky(client, label):
        if label == "bad":
            raise RuntimeError("boom")
        return [{"id": f"telegram:{label}:saved"}]

    attached = []

    async def attach(client, *, account_label, transport, policy):
        attached.append(account_label)

    t, recv = _transport("0.5")
    on_ready = make_on_ready(
        {"bad": object(), "good": object()},
        policy=policy,
        enumerate_channels=flaky,
        attach_event_handlers=attach,
    )
    await asyncio.wait_for(on_ready(t), 1.0)
    frames = _drain(recv)
    assert frames[0]["params"]["channels"] == [{"id": "telegram:good:saved"}]
    assert attached == ["bad", "good"]
