import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from telegram_mcp.runtime import mcp

_PROBE = "audience_probe_for_tests"


@pytest.fixture
def probe_tool():
    @mcp.tool(name=_PROBE)
    def probe(text: str) -> str:
        return text

    try:
        yield _PROBE
    finally:
        mcp.remove_tool(_PROBE)


@pytest.mark.asyncio
async def test_text_result_carries_user_audience(probe_tool):
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        result = await client.call_tool(probe_tool, {"text": "a message someone sent"})

    assert not result.isError
    texts = [block for block in result.content if isinstance(block, TextContent)]
    assert [block.text for block in texts] == ["a message someone sent"]
    for block in texts:
        assert block.annotations is not None
        assert block.annotations.audience == ["user"]
