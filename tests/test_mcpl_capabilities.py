"""Phase 1 regression tests for MCPL capability advertisement."""

from mcp.server.fastmcp import FastMCP

from telegram_mcp.mcpl import MCPL_VERSION
from telegram_mcp.mcpl.policy import FS_NAME, FS_USES
from telegram_mcp.mcpl.capabilities import (
    build_experimental_capabilities,
    build_mcpl_capabilities,
)


def test_mcpl_version_pinned():
    assert MCPL_VERSION == "0.5"


def test_capabilities_shape():
    caps = build_mcpl_capabilities()
    assert caps["version"] == "0.5"
    # never emitted → not advertised (§6.4 honesty)
    assert caps["pushEvents"] is False
    assert caps["channels"] == {
        "register": True,
        "publish": True,
        "incoming": True,
        "lifecycle": True,
        "typing": True,
        "streaming": False,
        "acknowledge": False,
    }
    # §6.1 Record form; `uses` names exactly the capabilities we exercise
    fs = caps["featureSets"][FS_NAME]
    assert fs["description"]
    assert set(fs["uses"]) == set(FS_USES)
    assert "channels.incoming" in fs["uses"] and "channels.register" in fs["uses"]
    # every `uses` entry is advertised, so the host's §6.4 derivation can pass
    for cap in FS_USES:
        if cap == "tools":
            continue  # outer MCP capability, not part of the manifest
        head, leaf = cap.split(".")
        assert caps[head][leaf] is True, cap


def test_experimental_wrapper_namespaces_under_mcpl():
    wrapped = build_experimental_capabilities()
    assert set(wrapped.keys()) == {"mcpl"}
    assert wrapped["mcpl"] == build_mcpl_capabilities()


def test_capabilities_flow_through_create_initialization_options():
    """Smoke test: capabilities reach the actual initialize-response surface."""
    mcp = FastMCP("telegram-test")
    opts = mcp._mcp_server.create_initialization_options(
        experimental_capabilities=build_experimental_capabilities(),
    )
    assert opts.capabilities.experimental is not None
    assert "mcpl" in opts.capabilities.experimental
    assert opts.capabilities.experimental["mcpl"]["version"] == "0.5"
    assert FS_NAME in opts.capabilities.experimental["mcpl"]["featureSets"]
