"""MCPL chat tags describe Telegram addressing, senders, topics, and attachments."""

from types import SimpleNamespace

import pytest
from telethon.tl.types import (
    Channel,
    Chat,
    DocumentAttributeAudio,
    MessageEntityMention,
    MessageEntityMentionName,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
)

from telegram_mcp.mcpl.events import (
    attach_event_handlers,
    build_incoming_message,
    reset_attached_clients,
)
from tests.test_mcpl_events import FakeEvent, FakeMessage, make_user


def make_chat(kind):
    if kind == "dm":
        return make_user(42, first="Alice")
    if kind == "saved":
        return make_user(99, first="Self")
    if kind == "group":
        return Chat(id=100, title="Group", photo=None, participants_count=2, date=None, version=1)
    return Channel(
        id=100,
        title="Channel",
        photo=None,
        date=None,
        megagroup=kind == "supergroup",
        broadcast=kind == "channel",
        forum=kind == "supergroup",
    )


async def incoming(message, *, kind="group", sender=None):
    return await build_incoming_message(
        FakeEvent(message, make_chat(kind), sender),
        account_label="default",
        self_id=99,
        self_username="mybot",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dm", "saved", "group", "supergroup", "channel"])
async def test_addressing_tags_follow_chat_kind(kind):
    result = await incoming(FakeMessage(id=100, text="hello"), kind=kind, sender=make_user(42))
    expected = {"chat:from-human"}
    if kind in ("dm", "saved"):
        expected |= {"chat:dm", "chat:private", "chat:addressed"}
    else:
        expected.add("chat:ambient")
    assert set(result["tags"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["username", "id", "server-flag", "other-user"])
async def test_mention_tags_use_account_mention_facts(signal):
    message = FakeMessage(
        id=100,
        text="@MyBot hello",
        entities=(
            [MessageEntityMention(offset=0, length=6)]
            if signal == "username"
            else (
                [MessageEntityMentionName(offset=0, length=6, user_id=99 if signal == "id" else 7)]
                if signal in ("id", "other-user")
                else []
            )
        ),
        mentioned=signal == "server-flag",
    )
    result = await incoming(message, sender=make_user(42))
    expected = {"chat:from-human", "chat:ambient"}
    if signal != "other-user":
        expected = {"chat:from-human", "chat:mention", "chat:addressed"}
    assert set(result["tags"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_author", [99, 7, None])
async def test_only_a_resolved_reply_to_our_account_is_addressed(reply_author):
    replied = SimpleNamespace(sender_id=reply_author, message="earlier") if reply_author else None
    message = FakeMessage(
        id=100,
        text="reply",
        is_reply=True,
        reply_to=SimpleNamespace(reply_to_msg_id=10, forum_topic=False),
        replied=replied,
    )
    result = await incoming(message, sender=make_user(42))
    expected = {"chat:from-human", "chat:ambient"}
    if reply_author == 99:
        expected = {"chat:from-human", "chat:reply", "chat:addressed"}
    assert set(result["tags"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("sender_kind", ["human", "bot", "channel", "unknown"])
async def test_sender_tags_require_a_resolved_user(sender_kind):
    sender = None
    if sender_kind in ("human", "bot"):
        sender = make_user(42)
        sender.bot = sender_kind == "bot"
    elif sender_kind == "channel":
        sender = make_chat("channel")
    result = await incoming(FakeMessage(id=100, text="hello"), sender=sender)
    expected = {"chat:ambient"}
    if sender_kind in ("human", "bot"):
        expected.add(f"chat:from-{sender_kind}")
    assert set(result["tags"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", [True, False])
async def test_forum_topic_tag_follows_thread_id(topic):
    result = await incoming(
        FakeMessage(
            id=100,
            text="topic",
            reply_to=SimpleNamespace(forum_topic=topic, reply_to_top_id=7, reply_to_msg_id=10),
        ),
        kind="supergroup",
        sender=make_user(42),
    )
    assert ("chat:thread" in result["tags"]) is topic
    assert result.get("threadId") == ("7" if topic else None)
    assert "chat:ambient" in result["tags"]
    assert "chat:reply" not in result["tags"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media_kind,mime,voice,expected",
    [
        ("photo", None, False, "chat:has-image"),
        ("document", "image/png", False, "chat:has-image"),
        ("document", "IMAGE/PNG", False, "chat:has-image"),
        ("document", "audio/ogg", False, "chat:has-audio"),
        ("document", "application/octet-stream", True, "chat:has-audio"),
        ("document", "text/plain", False, "chat:has-file"),
        ("document", "video/mp4", False, "chat:has-file"),
        ("document", None, False, "chat:has-file"),
        ("webpage", None, False, None),
    ],
)
async def test_media_tags_describe_attachments_even_when_not_inlined(
    media_kind, mime, voice, expected
):
    if media_kind == "photo":
        media = MessageMediaPhoto()
    elif media_kind == "webpage":
        media = MessageMediaWebPage(webpage=None)
    else:
        media = MessageMediaDocument(
            document=SimpleNamespace(
                mime_type=mime,
                size=10,
                attributes=[DocumentAttributeAudio(duration=1, voice=True)] if voice else [],
            )
        )
    # No download method: images and text files take the placeholder path.
    result = await incoming(FakeMessage(id=100, text="", media=media), sender=make_user(42))
    tags = {tag for tag in result["tags"] if tag.startswith("chat:has-")}
    assert tags == ({expected} if expected else set())


@pytest.mark.asyncio
async def test_tagged_message_reaches_the_incoming_notification():
    handlers = {}
    sent = []

    class Client:
        async def get_me(self):
            return make_user(99, username="mybot")

        def on(self, event_filter):
            def register(handler):
                name = getattr(event_filter, "__name__", type(event_filter).__name__)
                handlers[name] = handler
                return handler

            return register

    class Transport:
        async def send_notification(self, method, params):
            sent.append((method, params))

    reset_attached_clients()
    try:
        await attach_event_handlers(Client(), account_label="default", transport=Transport())
        event = FakeEvent(FakeMessage(id=100, text="hello"), make_chat("dm"), make_user(42))
        event.out = False
        await handlers["NewMessage"](event)
        assert len(sent) == 1
        method, params = sent[0]
        assert method == "channels/incoming"
        assert set(params["messages"][0]["tags"]) == {
            "chat:dm",
            "chat:private",
            "chat:addressed",
            "chat:from-human",
        }
    finally:
        reset_attached_clients()
