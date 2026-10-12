"""Inbound MCPL handlers — methods the host calls on us.

  - channels/publish — send agent's content via the right Telethon client.
    Honors MCPL RFC-011 `threadId` (a forum topic id, or None for the chat
    itself): it posts exactly there, or refuses with nothing posted. Once
    the send is dispatched, an error other than Telegram's own refusal stays
    an error, which the host treats as unconfirmed. Without `threadId`, it
    honors a custom `replyToMessageId` extension (not in the base MCPL spec)
    which also auto-threads into the right forum topic when the original
    message lived in one.
  - channels/typing — fire a brief typing indicator on the target chat.
  - channels/list — re-enumerate live dialogs and return the current set.
  - channels/open / channels/close — minimal acknowledgements; channel
    subscription is 'auto' on the host side, so these are mostly informational.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl import functions, types

from .channels import GENERAL_TOPIC_ID, enumerate_channels, forum_topic_of, publish_declarations
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
        if "threadId" in params:
            return await handle_targeted(channel_id, params)
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
            raise ValueError("channels/publish has no text content to send")

        kwargs: dict[str, Any] = {}
        reply_to_id = params.get("replyToMessageId")
        if reply_to_id:
            try:
                kwargs["reply_to"] = int(reply_to_id)
            except (TypeError, ValueError):
                log.warning("Ignoring non-numeric replyToMessageId: %r", reply_to_id)

        sent = await client.send_message(peer, text, **kwargs)
        return {"delivered": True, "messageId": str(sent.id)}

    async def handle_targeted(
        channel_id: str, params: ChannelsPublishParams
    ) -> ChannelsPublishResult:
        # RFC-011 §4: a target this server can't honor is refused as a
        # result, with nothing posted. So the target's form is checked first,
        # and every failure before the send answers the same way.
        target = params.get("threadId")
        refusal = _refuse_target_form(target, params)
        if refusal is not None:
            return refusal
        text = extract_text(params.get("content") or [])
        if not text:
            return _refusal(target, "no text content to send; nothing was posted")
        try:
            account_label, kind, peer_id = parse_channel_id(channel_id)
            client = clients.get(account_label)
            if client is None:
                return _refusal(
                    target, f"unknown account {_brief(repr(account_label))}; nothing was posted"
                )
            await ensure_connected_fn(client)
            if kind == "saved":
                peer: Any = "me"
            else:
                assert peer_id is not None  # parse_channel_id guarantees this
                peer = await resolve_entity_fn(peer_id, client)
        except Exception as err:  # noqa: BLE001 — nothing was sent
            return _refusal(
                target, f"could not reach this chat ({_error_text(err)}); nothing was posted"
            )
        return await publish_targeted(client, peer, kind, text, params)

    return handle_publish


def publish_frame_handler(handle_publish):
    """Wrap a channels/publish handler for frame-aware dispatch.

    MCPL RFC-011: a publish carrying `threadId` must be a Request, so where it
    landed can be answered; a Notification naming a place is dropped, never
    posted. A Notification without it keeps the legacy behavior.
    """

    async def handle(params: ChannelsPublishParams, is_request: bool) -> Any:
        if not is_request and "threadId" in params:
            # The host broke RFC-011 §4's Request rule and loses this publish,
            # so say so where an operator looks: the package logs at ERROR.
            log.error(
                "channels/publish Notification carrying threadId dropped for %s: a targeted "
                "publish must be a Request (MCPL RFC-011 §4); nothing was posted",
                _brief(repr(params.get("channelId")), 120),
            )
            return None
        return await handle_publish(params)

    return handle


# A topic id in exactly the form this server sends one: incoming `threadId`
# and the echo are both `str(int)` of a message id, at most ten digits.
# `str.isdigit()` and `int()` are looser. They would read "01" and "١" as
# topic 1, General, and post there, though only the string "1" is refused by
# name; they would read "05" as topic 5, whose echo "5" the host can't match;
# and `int()` raises on "¹", which `isdigit()` passes. `[0-9]`, unlike `\d`,
# is ASCII.
_TOPIC_ID = re.compile(r"[1-9][0-9]{0,9}")


def _brief(text: str, limit: int = 60) -> str:
    """A host-supplied value or an error, bounded for a log line or a reason:
    a host can send a target of any size or type."""
    return text if len(text) <= limit else f"{text[:limit]}… ({len(text)} characters)"


def _error_text(err: BaseException) -> str:
    return _brief(f"{type(err).__name__}: {err}", 120)


def _refusal(target: Any, reason: str) -> ChannelsPublishResult:
    log.info("channels/publish refused target %s: %s", _brief(repr(target)), reason)
    return {"delivered": False, "reason": reason}


def _refuse_target_form(
    target: Any, params: ChannelsPublishParams
) -> ChannelsPublishResult | None:
    """Refuse a `threadId` this server can never honor, before anything else."""
    if target is not None and not (isinstance(target, str) and _TOPIC_ID.fullmatch(target)):
        return _refusal(
            target,
            f"invalid threadId {_brief(repr(target))}: expected a forum topic id (ASCII digits,"
            " no leading zero), or null for the chat itself",
        )
    if target == str(GENERAL_TOPIC_ID):
        return _refusal(target, "topic 1 is General, the chat itself: publish with threadId null")
    if params.get("replyToMessageId"):
        # The replied-to message's own topic would decide where the post
        # lands, whatever threadId says.
        return _refusal(
            target, "replyToMessageId can't be combined with threadId; nothing was posted"
        )
    return None


async def publish_targeted(
    client: TelegramClient,
    peer: Any,
    kind: str,
    text: str,
    params: ChannelsPublishParams,
) -> ChannelsPublishResult:
    """A channels/publish naming its place (MCPL RFC-011 `threadId`), whose
    target has the right form and whose chat was reached.

    A forum topic id posts exactly into that topic, on a chat that is a forum
    now, unless the host was last told the chat is `root`; None posts in the
    chat itself (a forum's General). Anything else is refused with nothing
    posted, as `{"delivered": False, "reason": ...}` with no message id. Once
    the send is dispatched, Telegram's own refusal (400/403) is a refusal
    too, and any other error stays an error, which the host treats as
    unconfirmed. The result echoes where Telegram's own answer says the post
    landed.
    """
    target = params.get("threadId")
    kwargs: dict[str, Any] = {}
    if target is not None:
        if publish_declarations.target_of(params["channelId"]) == "root":
            # The host was told this chat has no threads (RFC-011 §3), even
            # if its forum has come on since: it hasn't been re-declared.
            return _refusal(
                target,
                f"this chat is declared root, with no threads (thread {target} requested);"
                " nothing was posted",
            )
        if not (kind == "supergroup" and bool(getattr(peer, "forum", False))):
            return _refusal(
                target,
                f"this chat has no forum topics (thread {target} requested); nothing was posted",
            )
        topic_id = int(target)
        try:
            found = await client(
                functions.messages.GetForumTopicsByIDRequest(peer=peer, topics=[topic_id])
            )
        except Exception as err:  # noqa: BLE001 — unconfirmed, and nothing was sent
            return _refusal(
                target,
                f"could not confirm topic {target} in this forum ({_error_text(err)});"
                " nothing was posted",
            )
        topic = next(
            (t for t in getattr(found, "topics", []) if getattr(t, "id", None) == topic_id), None
        )
        if topic is None or isinstance(topic, types.ForumTopicDeleted):
            return _refusal(target, f"no topic {target} in this forum; nothing was posted")
        kwargs["reply_to"] = topic_id

    try:
        sent = await client.send_message(peer, text, **kwargs)
    except RPCError as err:
        # Telegram's own refusal of the request (400/403: a closed topic, no
        # rights to post) posted nothing. Anything else stays an error, which
        # the host treats as unconfirmed.
        if getattr(err, "code", None) in (400, 403):
            return _refusal(target, f"Telegram refused the post: {err.__class__.__name__}")
        raise

    result: ChannelsPublishResult = {"delivered": True, "messageId": str(sent.id)}
    landed = _landed_topic(sent)
    if landed is not _UNKNOWN:
        result["threadId"] = landed
    return result


_UNKNOWN = object()


def _landed_topic(sent: Any) -> Any:
    """Where Telegram says a sent message landed: a forum topic id, None for
    the chat itself, or _UNKNOWN when Telegram didn't say.

    Telegram's own answer carries the message's reply header, read exactly as
    incoming threadId is (forum_topic_of). No header means the message is in
    the chat itself: Telegram reports it outside any reply, or, on its short
    answer, the send named none. On that short answer Telethon builds the
    message from our request, so a header there is the InputReplyToMessage we
    sent, which reports nothing about where the post landed: no echo, and the
    host counts the post unconfirmed (RFC-011 §5).
    """
    header = getattr(sent, "reply_to", None)
    if header is None:
        return None
    if not isinstance(header, types.MessageReplyHeader):
        return _UNKNOWN
    return forum_topic_of(header)


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
        # The host may keep these descriptors, declarations included, so a
        # later change to one is re-declared (RFC-011 §3).
        publish_declarations.record(channels)
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
        channel: dict[str, Any] = {
            "id": params.get("address", {}).get("id")
            or f"telegram:{params.get('type', 'unknown')}",
            "type": params.get("type", "telegram"),
            "label": "open-acknowledged",
            "direction": "bidirectional",
        }
        # A host that keeps this answer keeps the publish target it was last
        # told for the channel (RFC-011 §3), not a descriptor without one.
        target = publish_declarations.target_of(channel["id"])
        if target is not None:
            channel["capabilities"] = {"publish": {"target": target}}
        return {"channel": channel}

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
