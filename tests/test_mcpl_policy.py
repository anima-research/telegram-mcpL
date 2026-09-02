"""MCPL 0.5 §5.3/§5.4/§6.7 — negotiated policy state."""

import asyncio

import pytest

from telegram_mcp.mcpl.policy import (
    CAP_INCOMING,
    CAP_REGISTER,
    FS_NAME,
    FS_USES,
    PolicyState,
    host_speaks_0_5,
)

FULL = {"effectiveCapabilities": list(FS_USES), "enabled": [FS_NAME]}


def test_fresh_state_is_fail_closed():
    p = PolicyState()
    assert not p.policy_received
    assert not p.granted(CAP_INCOMING)
    assert not p.can_register
    assert not p.can_push_incoming


def test_request_with_full_grant_enables_feature_set_and_returns_receipt():
    p = PolicyState()
    receipt = p.apply(FULL, is_request=True)
    assert p.policy_received and p.fs_enabled
    assert p.can_register and p.can_push_incoming
    assert receipt["accepted"] is True
    assert "mode" not in receipt  # nothing degraded → omit, don't invent (§6.7)
    assert receipt["unavailableFeatures"] == []


def test_request_missing_capability_degrades_and_names_it():
    p = PolicyState()
    caps = [c for c in FS_USES if c != CAP_INCOMING]
    receipt = p.apply({"effectiveCapabilities": caps}, is_request=True)
    assert p.policy_received
    assert not p.fs_enabled  # §6.4: a missing `uses` capability disables the set
    assert receipt["accepted"] is True and receipt["mode"] == "degraded"
    uf = receipt["unavailableFeatures"][0]
    assert uf["featureSet"] == FS_NAME
    assert uf["missingCapabilities"] == [CAP_INCOMING]
    assert uf["effect"] == "disabled"
    assert any("channels.incoming" in n for n in receipt["notes"])
    # registration capability itself is still granted — grant and set differ
    assert p.granted(CAP_REGISTER)
    assert not p.can_register  # ...but the set is disabled, so nothing runs


def test_request_without_effective_capabilities_refuses_mcp_only():
    p = PolicyState()
    p.apply(FULL, is_request=True)
    receipt = p.apply({"enabled": [FS_NAME]}, is_request=True)  # absent → grant of nothing
    assert receipt["accepted"] is False and receipt["fallback"] == "mcp-only"
    assert not p.policy_received and p.effective_capabilities == set()
    assert not p.can_push_incoming  # previous wider grant did NOT survive


def test_request_with_path_in_both_lists_is_malformed_and_fails_closed():
    p = PolicyState()
    receipt = p.apply(
        {"effectiveCapabilities": list(FS_USES), "deniedCapabilities": [CAP_INCOMING]},
        is_request=True,
    )
    assert receipt["accepted"] is False and receipt["fallback"] == "mcp-only"
    assert not p.policy_received


def test_enabled_allowlist_not_naming_us_disables():
    p = PolicyState()
    receipt = p.apply(
        {"effectiveCapabilities": list(FS_USES), "enabled": ["other.set"]}, is_request=True
    )
    assert not p.fs_enabled and receipt["mode"] == "degraded"


def test_disabled_always_subtracts():
    p = PolicyState()
    p.apply({"effectiveCapabilities": list(FS_USES), "disabled": [FS_NAME]}, is_request=True)
    assert p.policy_received and not p.fs_enabled


def test_notification_before_request_cannot_establish_ready():
    p = PolicyState()
    assert p.apply(FULL, is_request=False) is None
    assert not p.policy_received and not p.fs_enabled


def test_notification_after_request_narrows_only():
    p = PolicyState()
    p.apply(FULL, is_request=True)
    # a widening notification is ignored...
    p.fs_enabled = False
    p.apply({"effectiveCapabilities": list(FS_USES), "enabled": [FS_NAME]}, is_request=False)
    assert not p.fs_enabled
    # ...a narrowing one is honoured
    p.fs_enabled = True
    p.apply({"disabled": [FS_NAME]}, is_request=False)
    assert not p.fs_enabled


def test_reduction_request_takes_effect_immediately():
    p = PolicyState()
    p.apply(FULL, is_request=True)
    assert p.can_push_incoming
    p.apply({"effectiveCapabilities": [c for c in FS_USES if c != CAP_INCOMING]}, is_request=True)
    assert not p.can_push_incoming


@pytest.mark.asyncio
async def test_wait_ready_resolves_on_request_and_times_out_otherwise():
    p = PolicyState()
    assert await p.wait_ready(0.05) is False

    async def later():
        await asyncio.sleep(0.02)
        p.apply(FULL, is_request=True)

    asyncio.get_running_loop().create_task(later())
    assert await p.wait_ready(1.0) is True


@pytest.mark.asyncio
async def test_wait_ready_also_wakes_on_refusal_so_startup_never_hangs():
    p = PolicyState()

    async def later():
        await asyncio.sleep(0.02)
        p.apply({}, is_request=True)

    asyncio.get_running_loop().create_task(later())
    assert await p.wait_ready(1.0) is True
    assert not p.can_register


@pytest.mark.parametrize(
    "caps,expected",
    [
        (None, False),
        ({}, False),
        ({"version": "0.4"}, False),
        ({"version": "0.5"}, True),
        ({"version": "0.5.0-draft"}, True),
        ({"version": "0.6"}, True),
        ({"version": "1.0"}, True),
        ({"version": "garbage"}, False),
        ({"pushEvents": True}, False),
    ],
)
def test_host_speaks_0_5(caps, expected):
    assert host_speaks_0_5(caps) is expected
