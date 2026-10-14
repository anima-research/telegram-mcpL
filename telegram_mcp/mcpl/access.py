"""TELEGRAM_ALLOWED_CHAT_IDS on the MCPL path, and bot-token sign-in.

The allowlist itself is runtime.is_chat_allowed (it gates the MCP tools).
Here it also gates what MCPL exposes: which chats register as channels,
which incoming messages are pushed, and where channels/publish and
channels/typing may go. Saved Messages (the account's own space) is not
gated.

A bot can't list dialogs, so with the allowlist set its channels are the
allowlisted chats themselves; without it a bot registers none.

TELEGRAM_BOT_TOKEN (or TELEGRAM_BOT_TOKEN_<LABEL>) signs a session in as a
bot when it isn't authorized yet. Pair it with a file session
(TELEGRAM_SESSION_NAME) so the login is kept across restarts.
"""

from __future__ import annotations

import os
from typing import Any, Union


def bot_token_for(label: str) -> str | None:
    """TELEGRAM_BOT_TOKEN_<LABEL>, else TELEGRAM_BOT_TOKEN for the default account."""
    token = os.getenv(f"TELEGRAM_BOT_TOKEN_{label.upper()}")
    if not token and label == "default":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
    return token or None


def allowlist_enabled() -> bool:
    from telegram_mcp.runtime import is_chat_allowlist_enabled

    return is_chat_allowlist_enabled()


def chat_allowed(chat_identifier: Any, entity: Any = None) -> bool:
    from telegram_mcp.runtime import is_chat_allowed

    return is_chat_allowed(chat_identifier, entity)


def allowlist_entries() -> list[Union[int, str]]:
    """The configured entries, in order, as written: ints, or handles without '@'."""
    entries: list[Union[int, str]] = []
    for token in (os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            entry: Union[int, str] = int(token)
        except ValueError:
            entry = token.lstrip("@")
        if entry not in entries:
            entries.append(entry)
    return entries
