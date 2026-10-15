"""What a send tool tells the agent about the message(s) it created.

The agent needs the new message's id to edit, delete, pin or reply to it later,
so every send tool ends its result with e.g. "(message_id 312, chat_id 74631161)".
"""

from __future__ import annotations

from typing import Any, Iterable

from telethon import utils
from telethon.tl import types

_NEW_MESSAGE_UPDATES = (
    types.UpdateNewMessage,
    types.UpdateNewChannelMessage,
    types.UpdateNewScheduledMessage,
)


def sent_ids(result: Any, random_ids: Iterable[int] | None = None) -> list[int]:
    """Ids of the messages a send call created.

    `result` is what the call returned: a Message, a list of them (albums,
    forwards), or the raw Updates of a `functions.messages.*` request. For raw
    Updates pass the request's `random_id`(s): Telegram maps each one to the
    new message id (UpdateMessageID), which is the only reliable link between
    a request and its message.
    """
    if result is None:
        return []
    if isinstance(result, (list, tuple)):
        return [i for item in result for i in sent_ids(item)]
    if isinstance(result, types.UpdateShortSentMessage):
        return [result.id]
    updates = getattr(result, "updates", None)
    if isinstance(updates, list):
        by_random = {u.random_id: u.id for u in updates if isinstance(u, types.UpdateMessageID)}
        if random_ids is not None:
            wanted = [random_ids] if isinstance(random_ids, int) else list(random_ids)
            # No fallback: another update's id would point the agent at someone
            # else's message.
            return [by_random[r] for r in wanted if r in by_random]
        if by_random:
            return list(by_random.values())
        return [
            u.message.id
            for u in updates
            if isinstance(u, _NEW_MESSAGE_UPDATES) and isinstance(u.message, types.Message)
        ]
    message_id = getattr(result, "id", None)
    return [message_id] if isinstance(message_id, int) else []


def chat_id_of(entity: Any, fallback: Any = None) -> Any:
    """The marked id (-100… for channels) the tools accept as chat_id."""
    try:
        return utils.get_peer_id(entity)
    except Exception:  # noqa: BLE001 — a fake or unexpected entity: keep the caller's id
        return fallback


def sent_note(ids: list[int], chat_id: Any = None) -> str:
    """'message_id 312, chat_id 74631161' (or 'message_ids 312, 313, …')."""
    if not ids:
        parts = ["message_id not returned by Telegram"]
    elif len(ids) == 1:
        parts = [f"message_id {ids[0]}"]
    else:
        parts = ["message_ids " + ", ".join(str(i) for i in ids)]
    if chat_id is not None:
        parts.append(f"chat_id {chat_id}")
    return ", ".join(parts)
