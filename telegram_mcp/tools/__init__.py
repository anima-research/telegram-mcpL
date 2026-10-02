"""Import tool modules so their MCP decorators register with the shared server."""

from telegram_mcp.runtime import mcp as _mcp
from telegram_mcp.tool_classes import apply_tool_classes as _apply_tool_classes
from telegram_mcp.tools.accounts import *
from telegram_mcp.tools.contacts import *
from telegram_mcp.tools.chats import *
from telegram_mcp.tools.messages import *
from telegram_mcp.tools.groups import *
from telegram_mcp.tools.media import *
from telegram_mcp.tools.profile import *
from telegram_mcp.tools.folders import *

# MCPL RFC-008: tag every registered tool with its class (see tool_classes.py).
_apply_tool_classes(_mcp)

__all__ = [name for name in globals() if not name.startswith("_")]
