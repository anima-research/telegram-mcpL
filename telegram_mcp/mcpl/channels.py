"""Telegram entity ↔ MCPL ChannelDescriptor mapping.

Channel ID scheme (account label is part of the ID so distinct accounts
sharing the same chat are still distinct channels — multi-account safe):

  - User (private chat)            → telegram:{label}:dm:{user_id}
  - Chat (basic group)             → telegram:{label}:group:{chat_id}
  - Channel.megagroup (supergroup) → telegram:{label}:supergroup:{chat_id}
  - Channel.broadcast              → telegram:{label}:channel:{channel_id}
  - Saved Messages (own user)      → telegram:{label}:saved

Direction:
  - bidirectional everywhere we can post (DMs, groups, supergroups,
    broadcast channels where we're creator or have admin rights)
  - inbound for broadcast channels we just subscribe to

One channel per supergroup; forum topics ride on `threadId` of incoming
messages, not as separate channels.
"""

from __future__ import annotations

import logging
from typing import Any

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat, User

from .types import ChannelDescriptor

log = logging.getLogger("telegram_mcp.mcpl")


# ---------------------------------------------------------------------------
# Entity classification
# ---------------------------------------------------------------------------


def _kind_of(entity: Any) -> str | None:
    """Return one of: 'dm', 'group', 'supergroup', 'channel'.

    Returns None for entities we shouldn't expose (forbidden chats, etc.).
    Saved Messages ('saved') is detected by the caller using self_id.
    """
    if isinstance(entity, User):
        return "dm"
    if isinstance(entity, Chat):
        # Skip ChatForbidden, ChatEmpty by checking for required attrs
        if getattr(entity, "deactivated", False):
            return None
        return "group"
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            return "supergroup"
        if getattr(entity, "broadcast", False):
            return "channel"
        # Default to supergroup-shaped if neither flag is set (unusual)
        return "supergroup"
    return None


def _can_post(entity: Any, kind: str) -> bool:
    """True when our user can send messages to this entity."""
    if kind == "channel":
        # Broadcast channels: only writable if we're creator or admin.
        return bool(getattr(entity, "creator", False)) or bool(
            getattr(entity, "admin_rights", None)
        )
    if kind == "supergroup":
        # Supergroups can be locked down for non-admins, but for a user
        # account in normal groups posting is the default. If banned_rights
        # explicitly forbids messaging, downgrade.
        banned = getattr(entity, "banned_rights", None)
        if banned is not None and getattr(banned, "send_messages", False):
            return False
        return True
    # dm and group are always writable from a user account
    return True


def _label_for(entity: Any, kind: str) -> str:
    """Best-effort human-readable label."""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    if isinstance(entity, User):
        first = getattr(entity, "first_name", "") or ""
        last = getattr(entity, "last_name", "") or ""
        full = f"{first} {last}".strip()
        if full:
            return full
        username = getattr(entity, "username", None)
        if username:
            return f"@{username}"
        return f"user:{entity.id}"
    return f"{kind}:{getattr(entity, 'id', '?')}"


# ---------------------------------------------------------------------------
# Public mapping
# ---------------------------------------------------------------------------


def channel_id_for(account_label: str, kind: str, peer_id: int | str) -> str:
    """Build the canonical MCPL channel id for a Telegram entity.

    `kind` is one of 'dm', 'group', 'supergroup', 'channel', 'saved'.
    For 'saved', `peer_id` is conventionally the self user_id but is not
    encoded in the id; use a constant suffix.
    """
    if kind == "saved":
        return f"telegram:{account_label}:saved"
    return f"telegram:{account_label}:{kind}:{peer_id}"


def entity_to_descriptor(
    entity: Any,
    *,
    account_label: str,
    self_id: int | None = None,
) -> ChannelDescriptor | None:
    """Convert a Telethon entity to an MCPL ChannelDescriptor.

    Returns None for entities we can't or shouldn't expose (forbidden,
    empty, unknown kind).
    """
    kind = _kind_of(entity)
    if kind is None:
        return None

    if kind == "dm" and self_id is not None and getattr(entity, "id", None) == self_id:
        kind = "saved"

    peer_id = getattr(entity, "id", None)
    if peer_id is None:
        return None

    direction = "bidirectional" if _can_post(entity, kind) else "inbound"
    label = _label_for(entity, kind)

    descriptor: ChannelDescriptor = {
        "id": channel_id_for(account_label, kind, peer_id),
        "type": "telegram",
        "label": label,
        "direction": direction,
        "address": {
            "account": account_label,
            "kind": kind,
            "peerId": peer_id,
        },
        "metadata": {
            "account": account_label,
            "kind": kind,
        },
    }

    # Extra metadata that helps the agent and host disambiguate.
    username = getattr(entity, "username", None)
    if username:
        descriptor["metadata"]["username"] = username

    forum = getattr(entity, "forum", False)
    if kind == "supergroup" and forum:
        descriptor["metadata"]["forum"] = True

    descriptor["capabilities"] = publish_capabilities(kind == "supergroup" and bool(forum))
    return descriptor


class PublishDeclarations:
    """What the host was last told about where a publish lands in each
    registered chat (RFC-011 `capabilities.publish.target`).

    A supergroup can turn its forum on or off after registration. A topic
    message from a chat still declared `root` would contradict the
    declaration (RFC-011 §3), so the changed descriptor is re-declared,
    through `channels/changed`, before anything that depends on it.
    """

    def __init__(self) -> None:
        self._targets: dict[str, str | None] = {}

    @staticmethod
    def _target(descriptor: dict[str, Any]) -> str | None:
        return descriptor.get("capabilities", {}).get("publish", {}).get("target")

    def record(self, descriptors: list[ChannelDescriptor]) -> None:
        for d in descriptors:
            self._targets[d["id"]] = self._target(d)

    def forget(self, channel_ids: list[str]) -> None:
        for channel_id in channel_ids:
            self._targets.pop(channel_id, None)

    def is_stale(self, descriptor: ChannelDescriptor) -> bool:
        """True when this registered chat's declaration no longer matches."""
        channel_id = descriptor["id"]
        return channel_id in self._targets and self._targets[channel_id] != self._target(descriptor)

    def reset(self) -> None:
        self._targets.clear()


# One per process, like the event handlers it serves (see events.py).
publish_declarations = PublishDeclarations()


def publish_capabilities(forum: bool) -> dict[str, Any]:
    """Where a channels/publish lands (MCPL RFC-011 `capabilities.publish`).

    A forum supergroup holds topics, and a publish naming one lands exactly
    in that topic, or in the chat itself (General) for None, or fails with
    nothing posted: `exact`. Every other chat has no threads inside it, so a
    publish lands in the chat itself: `root`. A chat that turns its forum on
    or off is re-declared the next time its descriptor is sent.
    """
    return {"publish": {"target": "exact" if forum else "root"}}


async def enumerate_channels(
    client: TelegramClient,
    account_label: str,
) -> list[ChannelDescriptor]:
    """Walk the client's dialogs and emit ChannelDescriptors for each.

    Caller has already invoked `client.start()` and `client.get_dialogs()`
    (entity cache warmed) per the standing pattern in `runner._main`.
    """
    me = await client.get_me()
    self_id = getattr(me, "id", None)

    descriptors: list[ChannelDescriptor] = []
    saw_saved = False

    async for dialog in client.iter_dialogs():
        descriptor = entity_to_descriptor(
            dialog.entity, account_label=account_label, self_id=self_id
        )
        if descriptor is None:
            continue
        if descriptor["metadata"]["kind"] == "saved":
            saw_saved = True
        descriptors.append(descriptor)

    # Saved Messages may not always appear as a dialog. If we missed it,
    # synthesize a descriptor since `client.send_message('me', ...)` works
    # regardless.
    if not saw_saved and self_id is not None:
        descriptors.append(
            {
                "id": channel_id_for(account_label, "saved", self_id),
                "type": "telegram",
                "label": "Saved Messages",
                "direction": "bidirectional",
                "address": {
                    "account": account_label,
                    "kind": "saved",
                    "peerId": self_id,
                },
                "metadata": {
                    "account": account_label,
                    "kind": "saved",
                },
                "capabilities": publish_capabilities(False),
            }
        )

    log.info(
        "Enumerated %d channels for account '%s'",
        len(descriptors),
        account_label,
    )
    return descriptors
