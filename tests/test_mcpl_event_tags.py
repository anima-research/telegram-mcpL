"""MCPL chat tags describe Telegram addressing, senders, topics, and attachments."""

import logging
from types import SimpleNamespace

import pytest
from telethon.tl.types import (
    Channel,
    Chat,
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    GeoPointEmpty,
    InputStickerSetEmpty,
    MessageEntityMention,
    MessageEntityMentionName,
    MessageMediaContact,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    MessageMediaVenue,
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
async def test_a_mention_after_an_emoji_is_found_at_its_utf16_offset():
    # Telegram counts the emoji as two UTF-16 code units, so the mention
    # starts at offset 3, where Python's index is 2.
    message = FakeMessage(
        id=100,
        text="\U0001f600 @MyBot hello",
        entities=[MessageEntityMention(offset=3, length=6)],
    )
    result = await incoming(message, sender=make_user(42))
    assert set(result["tags"]) == {"chat:from-human", "chat:mention", "chat:addressed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,offset,length",
    [
        pytest.param("\U0001f600@mybot hello", 1, 6, id="starts-inside-the-emoji"),
        pytest.param("@mybot\U0001f600 hello", 0, 7, id="ends-inside-the-emoji"),
    ],
)
async def test_a_mention_entity_that_splits_an_emoji_is_no_mention(text, offset, length):
    message = FakeMessage(
        id=100, text=text, entities=[MessageEntityMention(offset=offset, length=length)]
    )
    result = await incoming(message, sender=make_user(42))
    assert set(result["tags"]) == {"chat:from-human", "chat:ambient"}


# Telegram sets a message's mentioned flag on a reply to the account's own
# message, so each reply here carries the flag as Telegram would send it.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply_author,mentioned,expected",
    [
        pytest.param(99, True, {"chat:reply", "chat:addressed"}, id="to-us"),
        pytest.param(7, False, {"chat:ambient"}, id="to-another"),
        pytest.param(None, False, {"chat:ambient"}, id="to-another-gone"),
        pytest.param(None, True, {"chat:addressed"}, id="to-us-gone"),
    ],
)
async def test_reply_tags_follow_the_resolved_author_and_telegrams_flag(
    reply_author, mentioned, expected
):
    replied = SimpleNamespace(sender_id=reply_author, message="earlier") if reply_author else None
    message = FakeMessage(
        id=100,
        text="reply",
        is_reply=True,
        reply_to=SimpleNamespace(reply_to_msg_id=10, forum_topic=False),
        replied=replied,
        mentioned=mentioned,
    )
    result = await incoming(message, sender=make_user(42))
    assert set(result["tags"]) == {"chat:from-human"} | expected


@pytest.mark.asyncio
async def test_a_bots_reply_to_us_is_a_reply_not_a_mention():
    # A host can let a bot wake the agent only by an explicit @-mention, which
    # breaks reply loops between bots, so Telegram's flag on a bot's reply to
    # us mustn't read as one.
    bot = make_user(42)
    bot.bot = True
    message = FakeMessage(
        id=100,
        text="reply",
        is_reply=True,
        reply_to=SimpleNamespace(reply_to_msg_id=10, forum_topic=False),
        replied=SimpleNamespace(sender_id=99, message="earlier"),
        mentioned=True,
    )
    result = await incoming(message, sender=bot)
    assert set(result["tags"]) == {"chat:from-bot", "chat:reply", "chat:addressed"}


@pytest.mark.asyncio
async def test_an_at_mention_in_a_reply_to_another_is_a_mention():
    message = FakeMessage(
        id=100,
        text="@mybot see this",
        entities=[MessageEntityMention(offset=0, length=6)],
        is_reply=True,
        reply_to=SimpleNamespace(reply_to_msg_id=10, forum_topic=False),
        replied=SimpleNamespace(sender_id=7, message="earlier"),
        mentioned=True,
    )
    result = await incoming(message, sender=make_user(42))
    assert set(result["tags"]) == {"chat:from-human", "chat:mention", "chat:addressed"}


class FailingReplyLookup(FakeMessage):
    async def get_reply_message(self):
        raise ConnectionError("lookup failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mentioned,expected",
    [
        # A reply to the account's message carries Telegram's flag, which
        # addresses it even though the reply can't be confirmed.
        pytest.param(True, "chat:addressed", id="to-us"),
        pytest.param(False, "chat:ambient", id="to-another"),
    ],
)
async def test_a_failed_reply_lookup_is_logged_and_the_message_still_delivered(
    caplog, mentioned, expected
):
    # Production's logging configuration, kept as it is: runtime sets the
    # package logger, and its handlers, to ERROR.
    from telegram_mcp import runtime

    assert runtime.logger.level == logging.ERROR
    message = FailingReplyLookup(
        id=100,
        text="reply",
        is_reply=True,
        reply_to=SimpleNamespace(reply_to_msg_id=10, forum_topic=False),
        mentioned=mentioned,
    )
    result = await incoming(message, sender=make_user(42))
    assert set(result["tags"]) == {"chat:from-human", expected}
    failures = [r.getMessage() for r in caplog.records if "Reply lookup failed" in r.getMessage()]
    assert len(failures) == 1
    assert f"message 100 in {result['channelId']}:" in failures[0]


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


class CountingReplyLookup(FakeMessage):
    lookups = 0

    async def get_reply_message(self):
        self.lookups += 1
        return await super().get_reply_message()


@pytest.mark.asyncio
@pytest.mark.parametrize("mentioned", [False, True], ids=["unflagged", "flagged"])
async def test_a_post_in_a_topic_we_opened_is_not_a_reply_to_us(mentioned):
    # A plain topic post's reply header names the topic's root, which the
    # account wrote, and has no reply_to_top_id. Whether Telegram flags such
    # a post as mentioning the root's author, it doesn't address the account.
    message = CountingReplyLookup(
        id=100,
        text="topic post",
        is_reply=True,
        reply_to=SimpleNamespace(forum_topic=True, reply_to_msg_id=7, reply_to_top_id=None),
        replied=SimpleNamespace(sender_id=99, message="topic opened"),
        mentioned=mentioned,
    )
    result = await incoming(message, kind="supergroup", sender=make_user(42))
    assert set(result["tags"]) == {"chat:from-human", "chat:thread", "chat:ambient"}
    assert result["threadId"] == "7"
    assert message.lookups == 0
    metadata = result["metadata"]
    assert metadata["isReply"] is False
    assert metadata["isReplyToBot"] is False
    assert [key for key in metadata if key.startswith("replyTo")] == []


@pytest.mark.asyncio
async def test_a_reply_to_our_message_inside_a_topic_is_addressed():
    message = FakeMessage(
        id=100,
        text="reply in topic",
        is_reply=True,
        reply_to=SimpleNamespace(forum_topic=True, reply_to_msg_id=12, reply_to_top_id=7),
        replied=SimpleNamespace(sender_id=99, message="ours"),
        mentioned=True,
    )
    result = await incoming(message, kind="supergroup", sender=make_user(42))
    assert set(result["tags"]) == {
        "chat:from-human",
        "chat:thread",
        "chat:reply",
        "chat:addressed",
    }
    assert result["threadId"] == "7"
    assert result["metadata"]["replyToMessageId"] == "12"


def document(mime, *attributes):
    return MessageMediaDocument(
        document=SimpleNamespace(mime_type=mime, size=10, attributes=list(attributes))
    )


def sticker():
    return DocumentAttributeSticker(alt="", stickerset=InputStickerSetEmpty())


def video(**flags):
    return DocumentAttributeVideo(duration=1, w=1, h=1, **flags)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media,expected",
    [
        pytest.param(MessageMediaPhoto(), "chat:has-image", id="photo"),
        pytest.param(document("image/png"), "chat:has-image", id="image"),
        pytest.param(document("IMAGE/PNG"), "chat:has-image", id="image-uppercase-mime"),
        pytest.param(document("audio/ogg"), "chat:has-audio", id="audio-mime"),
        pytest.param(
            document("application/octet-stream", DocumentAttributeAudio(duration=1, voice=True)),
            "chat:has-audio",
            id="voice-note",
        ),
        pytest.param(
            document("application/octet-stream", DocumentAttributeAudio(duration=1, voice=False)),
            "chat:has-audio",
            id="music-under-another-mime",
        ),
        pytest.param(document("text/plain"), "chat:has-file", id="text"),
        pytest.param(
            document("application/pdf", DocumentAttributeFilename(file_name="report.pdf")),
            "chat:has-file",
            id="named-upload",
        ),
        pytest.param(document("video/mp4"), "chat:has-file", id="video"),
        pytest.param(
            document("video/mp4", video(), DocumentAttributeAnimated()),
            "chat:has-file",
            id="gif",
        ),
        pytest.param(
            document("application/x-tgsticker", sticker()), "chat:has-file", id="animated-sticker"
        ),
        pytest.param(
            document("video/webm", sticker(), video()), "chat:has-file", id="video-sticker"
        ),
        pytest.param(
            document("video/mp4", video(round_message=True)),
            "chat:has-file",
            id="round-video-note",
        ),
        pytest.param(document(None), "chat:has-file", id="no-mime"),
        pytest.param(MessageMediaDocument(document=None), "chat:has-file", id="absent-document"),
        pytest.param(MessageMediaWebPage(webpage=None), None, id="link-preview"),
        pytest.param(MessageMediaPoll(poll=None, results=None), None, id="poll"),
        pytest.param(
            MessageMediaContact(phone_number="", first_name="", last_name="", vcard="", user_id=0),
            None,
            id="contact",
        ),
        pytest.param(MessageMediaGeo(geo=GeoPointEmpty()), None, id="location"),
        pytest.param(
            MessageMediaVenue(
                geo=GeoPointEmpty(),
                title="",
                address="",
                provider="",
                venue_id="",
                venue_type="",
            ),
            None,
            id="venue",
        ),
    ],
)
async def test_media_tags_describe_attachments_even_when_not_inlined(media, expected):
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
