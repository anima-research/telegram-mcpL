"""Send tools report the id(s) of the message(s) they created.

The agent can only edit, delete, pin or reply to its own message when it knows
the id, so every send result ends with "(message_id N, chat_id C)".
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types

from telegram_mcp import runtime
from telegram_mcp.sent import chat_id_of, sent_ids, sent_note
from telegram_mcp.tools import contacts, media, messages

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _updates(*updates):
    return types.Updates(list(updates), [], [], NOW, 1)


def _new_channel_message(mid):
    msg = types.Message(id=mid, peer_id=types.PeerChannel(5), date=NOW, message="x")
    return types.UpdateNewChannelMessage(msg, pts=1, pts_count=1)


# ---------------------------------------------------------------------------
# sent_ids / sent_note
# ---------------------------------------------------------------------------


def test_sent_ids_from_a_message_and_an_album():
    assert sent_ids(SimpleNamespace(id=312)) == [312]
    assert sent_ids([SimpleNamespace(id=312), SimpleNamespace(id=313)]) == [312, 313]
    assert sent_ids(None) == []


def test_sent_ids_from_update_short_sent_message():
    short = types.UpdateShortSentMessage(id=77, pts=1, pts_count=1, date=NOW)
    assert sent_ids(short) == [77]


def test_sent_ids_follows_the_request_random_id():
    """Only the UpdateMessageID for this request's random_id counts."""
    result = _updates(
        types.UpdateMessageID(id=900, random_id=111),
        types.UpdateMessageID(id=312, random_id=222),
        _new_channel_message(312),
    )
    assert sent_ids(result, 222) == [312]
    assert sent_ids(result, [222, 111]) == [312, 900]


def test_sent_ids_never_guesses_for_an_unmatched_random_id():
    """An unrelated update must not be reported as this request's message."""
    result = _updates(types.UpdateMessageID(id=900, random_id=111), _new_channel_message(900))
    assert sent_ids(result, 222) == []
    assert sent_ids(_updates(_new_channel_message(41)), 123) == []


def test_sent_ids_without_random_id_reads_the_new_message_update():
    assert sent_ids(_updates(_new_channel_message(41))) == [41]


def test_sent_ids_ignores_unknown_results():
    assert sent_ids(AsyncMock()) == []
    assert sent_ids("ok") == []


def test_sent_note_formats():
    assert sent_note([312], 74631161) == "message_id 312, chat_id 74631161"
    assert sent_note([1, 2, 3], -4756081720) == "message_ids 1, 2, 3, chat_id -4756081720"
    assert sent_note([]) == "message_id not returned by Telegram"


def test_chat_id_of_marks_groups_and_channels():
    assert chat_id_of(types.PeerUser(42)) == 42
    assert chat_id_of(types.PeerChat(4756081720)) == -4756081720
    assert chat_id_of(types.PeerChannel(123)) == -1000000000123
    assert chat_id_of("not an entity", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# Text tools
# ---------------------------------------------------------------------------


@pytest.fixture
def text_client(monkeypatch):
    cl = AsyncMock()
    monkeypatch.setattr(messages, "get_client", lambda account=None: cl)
    monkeypatch.setattr(messages, "resolve_entity", AsyncMock(return_value=types.PeerUser(42)))
    monkeypatch.setattr(messages, "is_chat_allowlist_enabled", lambda: False)
    return cl


def _answer_with_id(mid):
    """cl(request) side effect: the Updates Telegram sends for that request."""

    async def call(request):
        return _updates(
            types.UpdateMessageID(id=999, random_id=request.random_id + 1),
            types.UpdateMessageID(id=mid, random_id=request.random_id),
        )

    return call


@pytest.mark.asyncio
async def test_send_message_returns_the_id(text_client):
    text_client.send_message.return_value = SimpleNamespace(id=312)
    result = await messages.send_message(chat_id=42, message="hi")
    assert result == "Message sent (message_id 312, chat_id 42)."


@pytest.mark.asyncio
async def test_send_message_raw_request_returns_the_id(text_client):
    """format_date goes through a raw SendMessageRequest, which returns Updates."""
    text_client.side_effect = _answer_with_id(314)
    result = await messages.send_message(chat_id=42, message="on 13/09", format_date="13/09")
    assert result == "Message sent (message_id 314, chat_id 42)."


@pytest.mark.asyncio
async def test_reply_to_message_returns_the_new_id(text_client):
    text_client.send_message.return_value = SimpleNamespace(id=313)
    result = await messages.reply_to_message(chat_id=42, message_id=311, text="yes")
    assert result == "Reply to message 311 sent (message_id 313, chat_id 42)."


@pytest.mark.asyncio
async def test_group_chat_id_is_the_marked_one(text_client, monkeypatch):
    monkeypatch.setattr(
        messages, "resolve_entity", AsyncMock(return_value=types.PeerChat(4756081720))
    )
    text_client.send_message.return_value = SimpleNamespace(id=5)
    result = await messages.send_message(chat_id=-4756081720, message="hi")
    assert result == "Message sent (message_id 5, chat_id -4756081720)."


@pytest.mark.asyncio
async def test_rich_message_json_has_the_id(text_client, monkeypatch):
    monkeypatch.setattr(messages, "account_is_premium", AsyncMock(return_value=True))
    monkeypatch.setattr(messages, "make_rich_input", lambda mode, text: None)
    text_client.side_effect = _answer_with_id(400)
    result = await messages.send_message(chat_id=42, message="# hi", parse_mode="rich")
    assert json.loads(result) == {"sent": True, "rich": True, "message_id": 400, "chat_id": 42}


@pytest.mark.asyncio
async def test_create_poll_returns_the_id(text_client, monkeypatch):
    monkeypatch.setattr(messages, "ensure_connected", AsyncMock())
    text_client.side_effect = _answer_with_id(500)
    result = await messages.create_poll(chat_id=42, question="Lunch?", options=["Yes", "No"])
    assert result == "Poll created (message_id 500, chat_id 42)."


@pytest.mark.asyncio
async def test_forward_messages_returns_destination_ids(text_client):
    text_client.forward_messages.return_value = [SimpleNamespace(id=61), SimpleNamespace(id=62)]
    result = await messages.forward_messages(from_chat_id=1, message_ids=[10, 11], to_chat_id=42)
    assert result == "2 messages forwarded from 1 to 42 (message_ids 61, 62, chat_id 42)."


@pytest.mark.asyncio
async def test_send_contact_returns_the_id(monkeypatch):
    cl = AsyncMock(side_effect=_answer_with_id(70))
    monkeypatch.setattr(contacts, "get_client", lambda account=None: cl)
    monkeypatch.setattr(contacts, "ensure_connected", AsyncMock())
    monkeypatch.setattr(contacts, "resolve_entity", AsyncMock(return_value=types.PeerUser(42)))
    result = await contacts.send_contact(chat_id=42, phone_number="+100", first_name="Ann")
    assert result == "Contact sent (message_id 70, chat_id 42)."


# ---------------------------------------------------------------------------
# Media tools
# ---------------------------------------------------------------------------


@pytest.fixture
def media_client(tmp_path, monkeypatch):
    root = (tmp_path / "root").resolve()
    root.mkdir()
    cl = AsyncMock()
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [root])
    monkeypatch.setattr(media, "clients", {"default": cl})
    monkeypatch.setattr(media, "get_client", lambda account=None: cl)
    monkeypatch.setattr(media, "resolve_entity", AsyncMock(return_value=types.PeerUser(42)))
    return cl, root


@pytest.mark.asyncio
async def test_send_voice_returns_the_id(media_client):
    cl, root = media_client
    (root / "reply.ogg").write_bytes(b"OggS")
    cl.send_file.return_value = SimpleNamespace(id=320)
    result = await media.send_voice(chat_id=42, file_path="reply.ogg")
    assert result == f"Voice message sent from {root / 'reply.ogg'} (message_id 320, chat_id 42)."


@pytest.mark.asyncio
async def test_send_album_returns_every_id(media_client):
    cl, root = media_client
    for name in ("a.png", "b.png", "c.png"):
        (root / name).write_bytes(b"png")
    cl.send_file.return_value = [SimpleNamespace(id=i) for i in (330, 331, 332)]
    result = await media.send_album(chat_id=42, file_paths=["a.png", "b.png", "c.png"])
    assert result == "Album of 3 files sent (message_ids 330, 331, 332, chat_id 42)."
