"""Inbound MCPL handlers — methods the host calls on us.

  - channels/publish — send agent's content via the right Telethon client.
    Honors MCPL RFC-011 `threadId` (a forum topic id, or None for the chat
    itself), posting exactly there or failing with nothing posted. Without
    it, honors a custom `replyToMessageId` extension (not in the base MCPL
    spec) which also auto-threads into the right forum topic when the
    original message lived in one.
  - channels/typing — fire a brief typing indicator on the target chat.
  - channels/list — re-enumerate live dialogs and return the current set.
  - channels/open / channels/close — minimal acknowledgements; channel
    subscription is 'auto' on the host side, so these are mostly informational.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl import functions, types

from .channels import enumerate_channels
from .types import ChannelsPublishParams, ChannelsPublishResult, McplContentBlock

log = logging.getLogger("telegram_mcp.mcpl")


# ---------------------------------------------------------------------------
# Channel id parsing
# ---------------------------------------------------------------------------


def parse_channel_id(channel_id: str) -> tuple[str, str, int | None]:
    """Parse `telegram:{account}:{kind}[:{peer_id}]` into its components.

    Saved Messages has no peer_id segment (kind='saved').
    Raises ValueError on malformed input.
    """
    parts = channel_id.split(":")
    if not parts or parts[0] != "telegram":
        raise ValueError(f"Not a telegram channel id: {channel_id!r}")
    if len(parts) == 3 and parts[2] == "saved":
        return parts[1], "saved", None
    if len(parts) == 4:
        try:
            peer_id = int(parts[3])
        except ValueError as exc:
            raise ValueError(f"Invalid peer id in {channel_id!r}") from exc
        kind = parts[2]
        if kind not in {"dm", "group", "supergroup", "channel"}:
            raise ValueError(f"Unknown channel kind {kind!r} in {channel_id!r}")
        return parts[1], kind, peer_id
    raise ValueError(f"Malformed telegram channel id: {channel_id!r}")


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


def extract_text(content: list[McplContentBlock]) -> str:
    """Concatenate text blocks. Non-text blocks are dropped with a warning.

    Phase 5 supports text only. Image / audio / resource blocks are noted
    in the log but not sent — sending media as the agent requires fetching
    base64 data and is deferred.
    """
    text_parts: list[str] = []
    skipped: list[str] = []
    for block in content or []:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        else:
            skipped.append(btype or "<unknown>")
    if skipped:
        log.warning(
            "channels/publish dropped %d non-text content block(s): %s",
            len(skipped),
            skipped,
        )
    return "\n".join(p for p in text_parts if p)


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


def make_publish_handler(
    clients: dict[str, TelegramClient],
    *,
    resolve_entity_fn,
    ensure_connected_fn,
):
    """Build the channels/publish handler bound to this server's clients.

    `resolve_entity_fn` and `ensure_connected_fn` are the existing helpers
    in `telegram_mcp.runtime`. Passed as parameters to keep this module
    decoupled from the runtime singleton — it makes testing tractable.
    """

    async def handle_publish(params: ChannelsPublishParams) -> ChannelsPublishResult:
        channel_id = params.get("channelId")
        if not channel_id:
            raise ValueError("channels/publish missing channelId")
        content = params.get("content") or []

        account_label, kind, peer_id = parse_channel_id(channel_id)
        client = clients.get(account_label)
        if client is None:
            raise ValueError(
                f"Unknown account '{account_label}' "
                f"(known: {', '.join(sorted(clients))})"
            )
        await ensure_connected_fn(client)

        if kind == "saved":
            peer: Any = "me"
        else:
            assert peer_id is not None  # parse_channel_id guarantees this
            peer = await resolve_entity_fn(peer_id, client)

        text = extract_text(content)
        if not text:
            if "threadId" in params:
                # A targeted publish answers a definite no-post as a result.
                return {"delivered": False, "reason": "no text content to send; nothing was posted"}
            raise ValueError("channels/publish has no text content to send")

        if "threadId" in params:
            return await publish_targeted(client, peer, kind, text, params)

        kwargs: dict[str, Any] = {}
        reply_to_id = params.get("replyToMessageId")
        if reply_to_id:
            try:
                kwargs["reply_to"] = int(reply_to_id)
            except (TypeError, ValueError):
                log.warning("Ignoring non-numeric replyToMessageId: %r", reply_to_id)

        sent = await client.send_message(peer, text, **kwargs)
        return {"delivered": True, "messageId": str(sent.id)}

    return handle_publish


def publish_frame_handler(handle_publish):
    """Wrap a channels/publish handler for frame-aware dispatch.

    MCPL RFC-011: a publish carrying `threadId` must be a Request, so where it
    landed can be answered; a Notification naming a place is dropped, never
    posted. A Notification without it keeps the legacy behavior.
    """

    async def handle(params: ChannelsPublishParams, is_request: bool) -> Any:
        if not is_request and "threadId" in params:
            log.warning(
                "channels/publish Notification carrying threadId dropped: a targeted publish "
                "must be a Request (MCPL RFC-011)"
            )
            return None
        return await handle_publish(params)

    return handle


async def publish_targeted(
    client: TelegramClient,
    peer: Any,
    kind: str,
    text: str,
    params: ChannelsPublishParams,
) -> ChannelsPublishResult:
    """A channels/publish naming its place (MCPL RFC-011 `threadId`).

    A forum topic id posts exactly into that topic; None posts in the chat
    itself (a forum's General). It lands there or fails with nothing posted,
    as `{"delivered": False, "reason": ...}` with no message id, and the
    result echoes where Telegram's own answer says it landed.
    """
    target = params.get("threadId")

    def refuse(reason: str) -> ChannelsPublishResult:
        log.info("channels/publish refused target %r: %s", target, reason)
        return {"delivered": False, "reason": reason}

    if target is not None and not (
        isinstance(target, str) and target.isdigit() and int(target) > 0
    ):
        return refuse(
            f"invalid threadId {target!r}: expected a forum topic id, or null for the chat itself"
        )
    if target == "1":
        # Topic 1 is a forum's General: the chat itself, which is null.
        return refuse("topic 1 is General, the chat itself: publish with threadId null")
    if params.get("replyToMessageId"):
        # The replied-to message's own topic would decide where the post
        # lands, whatever threadId says.
        return refuse("replyToMessageId can't be combined with threadId; nothing was posted")

    forum = kind == "supergroup" and bool(getattr(peer, "forum", False))
    kwargs: dict[str, Any] = {}
    if target is not None:
        if not forum:
            return refuse(
                f"this chat has no forum topics (thread {target} requested); nothing was posted"
            )
        topic_id = int(target)
        try:
            found = await client(
                functions.messages.GetForumTopicsByIDRequest(peer=peer, topics=[topic_id])
            )
        except Exception as err:  # noqa: BLE001 — any failure means we can't confirm
            return refuse(
                f"could not confirm topic {target} in this forum ({err}); nothing was posted"
            )
        topic = next(
            (t for t in getattr(found, "topics", []) if getattr(t, "id", None) == topic_id), None
        )
        if topic is None or isinstance(topic, types.ForumTopicDeleted):
            return refuse(f"no topic {target} in this forum; nothing was posted")
        kwargs["reply_to"] = topic_id

    try:
        sent = await client.send_message(peer, text, **kwargs)
    except RPCError as err:
        # Telegram's own refusal of the request (400/403: a closed topic, no
        # rights to post) posted nothing. Anything else stays an error, which
        # the host treats as unconfirmed.
        if getattr(err, "code", None) in (400, 403):
            return refuse(f"Telegram refused the post: {err.__class__.__name__}")
        raise

    result: ChannelsPublishResult = {"delivered": True, "messageId": str(sent.id)}
    landed = _landed_topic(sent)
    if landed is not _UNKNOWN:
        result["threadId"] = landed
    return result


_UNKNOWN = object()


def _landed_topic(sent: Any) -> Any:
    """Where Telegram says a sent message landed: its forum topic id, None
    for the chat itself, or _UNKNOWN when its reply header can't tell."""
    header = getattr(sent, "reply_to", None)
    if header is None:
        return None
    if getattr(header, "forum_topic", False):
        top = getattr(header, "reply_to_top_id", None) or getattr(header, "reply_to_msg_id", None)
        return str(top) if top is not None else _UNKNOWN
    return _UNKNOWN


# ---------------------------------------------------------------------------
# channels/list
# ---------------------------------------------------------------------------


def make_list_handler(clients: dict[str, TelegramClient]):
    """Re-enumerate dialogs across all configured accounts."""

    async def handle_list(params: dict[str, Any]) -> dict[str, Any]:
        channels = []
        for label, cl in clients.items():
            try:
                channels.extend(await enumerate_channels(cl, label))
            except Exception:
                log.exception("channels/list — enumeration failed for %r", label)
        return {"channels": channels}

    return handle_list


# ---------------------------------------------------------------------------
# channels/open and channels/close — informational under 'auto' subscription
# ---------------------------------------------------------------------------


def make_open_handler():
    """Acknowledge channels/open. We auto-subscribe everywhere; nothing to do."""

    async def handle_open(params: dict[str, Any]) -> dict[str, Any]:
        # The spec returns a ChannelDescriptor on success. The host already
        # has it from the original channels/register; echo back what they
        # asked about so the contract is satisfied.
        return {
            "channel": {
                "id": params.get("address", {}).get("id")
                or f"telegram:{params.get('type', 'unknown')}",
                "type": params.get("type", "telegram"),
                "label": "open-acknowledged",
                "direction": "bidirectional",
            }
        }

    return handle_open


def make_close_handler():
    """Acknowledge channels/close. No-op under auto subscription."""

    async def handle_close(params: dict[str, Any]) -> dict[str, Any]:
        return {"closed": True}

    return handle_close


# ---------------------------------------------------------------------------
# channels/typing
# ---------------------------------------------------------------------------


def make_typing_handler(
    clients: dict[str, TelegramClient],
    *,
    resolve_entity_fn,
    ensure_connected_fn,
    refresh_window: float = 10.0,
    max_typing: float = 300.0,
):
    """Drive a continuous Telegram typing indicator from channels/typing.

    The host refreshes typing roughly every 7s while inference is active
    (TYPING_INTERVAL_MS in the host's channel-registry) — with ``op`` absent
    — and sends one final notification with ``op="stop"`` when the turn ends.
    So we keep a SINGLE background task per channel that holds Telethon's
    typing action open (it auto-resends every ~4s) and:

      * start / refresh (``op`` != ``"stop"``) — (re)arm a deadline
        ``refresh_window`` seconds out and, if no task is running, start one;
      * stop (``op`` == ``"stop"``) — cancel the task so typing clears at once.

    If the host stops refreshing without a stop (e.g. a dropped notification),
    the deadline lapses and typing clears on its own; ``max_typing`` is an
    absolute safety cap so a stuck refresher can't pin "typing…" forever.
    channels/typing is a notification, so the handler returns immediately and
    does the actual typing in the background. Best-effort throughout — a
    typing failure must never break the agent.
    """

    tasks: dict[str, asyncio.Task[None]] = {}
    deadlines: dict[str, float] = {}

    async def _keep_typing(channel_id: str, client: TelegramClient, peer: Any) -> None:
        loop = asyncio.get_running_loop()
        hard_stop = loop.time() + max_typing
        try:
            async with client.action(peer, "typing"):
                while True:
                    now = loop.time()
                    if now >= deadlines.get(channel_id, 0.0) or now >= hard_stop:
                        break
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass  # stop() cancelled us — let the action context clear typing
        except Exception:
            log.exception("channels/typing — action failed")
        finally:
            tasks.pop(channel_id, None)
            deadlines.pop(channel_id, None)

    async def handle_typing(params: dict[str, Any]) -> None:
        channel_id = params.get("channelId")
        if not channel_id:
            return

        # Stop — cancel any active typing for this channel.
        if params.get("op") == "stop":
            deadlines.pop(channel_id, None)
            task = tasks.pop(channel_id, None)
            if task is not None:
                task.cancel()
            return

        # Start / refresh.
        try:
            account_label, kind, peer_id = parse_channel_id(channel_id)
        except ValueError:
            log.warning("channels/typing — bad channelId: %r", channel_id)
            return
        client = clients.get(account_label)
        if client is None:
            return

        # Arm the deadline first so an already-running loop picks it up.
        deadlines[channel_id] = asyncio.get_running_loop().time() + refresh_window
        if channel_id in tasks:
            return  # already typing — the bumped deadline keeps it alive

        await ensure_connected_fn(client)
        peer: Any = "me" if kind == "saved" else await resolve_entity_fn(peer_id, client)
        if peer is None:
            deadlines.pop(channel_id, None)  # entity didn't resolve — undo the arm
            return
        tasks[channel_id] = asyncio.create_task(_keep_typing(channel_id, client, peer))

    return handle_typing
