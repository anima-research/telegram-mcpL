"""MCPL RFC-008: every registered tool declares a valid class in tools/list."""

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ListToolsRequest

import telegram_mcp.tools  # noqa: F401 - registers the tools and applies their classes
from telegram_mcp.runtime import mcp
from telegram_mcp.tool_classes import (
    MCPL_CLASS_KEY,
    TOOL_CLASS_VOCABULARY,
    TOOL_CLASSES,
    UNCLASSED,
)

# Name fragments that mean a tool carries or reads people's messages or details.
# Such a tool must be comms whatever else it is (see the comms rule in tool_classes.py).
_PEOPLE_FRAGMENTS = (
    "message",
    "draft",
    "contact",
    "user",
    "participant",
    "admin",
    "reaction",
    "read",
    "send_",
    "reply",
    "forward",
    "history",
    "invite",
    "poll",
)


def _registered_names():
    return {tool.name for tool in mcp._tool_manager.list_tools()}


def test_every_registered_tool_is_classed_or_explicitly_unclassed():
    missing = _registered_names() - set(TOOL_CLASSES) - UNCLASSED
    assert not missing, f"add to telegram_mcp/tool_classes.py: {sorted(missing)}"


def test_class_map_names_only_registered_tools():
    stale = (set(TOOL_CLASSES) | UNCLASSED) - _registered_names()
    assert not stale, f"not registered: {sorted(stale)}"


def test_classed_and_unclassed_are_disjoint():
    assert not set(TOOL_CLASSES) & UNCLASSED


def test_every_class_is_in_the_rfc_vocabulary():
    for name, classes in TOOL_CLASSES.items():
        assert classes, name
        assert len(set(classes)) == len(classes), name
        assert set(classes) <= TOOL_CLASS_VOCABULARY, (name, classes)


def test_tools_touching_people_are_comms():
    for name, classes in TOOL_CLASSES.items():
        if any(fragment in name for fragment in _PEOPLE_FRAGMENTS):
            assert "comms" in classes, (name, classes)


def _assert_listed_classes(listed_meta):
    assert set(listed_meta) == _registered_names()
    for name, meta in listed_meta.items():
        if name in UNCLASSED:
            assert MCPL_CLASS_KEY not in (meta or {}), name
        else:
            assert meta[MCPL_CLASS_KEY] == list(TOOL_CLASSES[name]), name


@pytest.mark.asyncio
async def test_serialized_tools_list_carries_class_in_meta():
    handler = mcp._mcp_server.request_handlers[ListToolsRequest]
    result = await handler(ListToolsRequest(method="tools/list"))
    # The same serialization the server session applies before writing to the wire.
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    _assert_listed_classes({t["name"]: t.get("_meta") for t in payload["tools"]})


@pytest.mark.asyncio
async def test_client_sees_class_in_tools_list():
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        result = await client.list_tools()
    _assert_listed_classes({tool.name: tool.meta for tool in result.tools})
