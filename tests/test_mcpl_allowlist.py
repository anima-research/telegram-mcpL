"""TELEGRAM_ALLOWED_CHAT_IDS on the MCPL path, and bot-token sign-in."""

from types import SimpleNamespace

import pytest
from telethon.errors import BotMethodInvalidError

from telegram_mcp.mcpl import access
from telegram_mcp.mcpl.channels import enumerate_channels
from telegram_mcp.mcpl.handlers import make_publish_handler


def make_user(user_id, **kw):
    from tests.test_mcpl_channels import make_user as _make_user

    return _make_user(user_id, **kw)


class DialogClient:
    """A user account: lists dialogs."""

    def __init__(self, entities):
        self._entities = entities

    async def get_me(self):
        return SimpleNamespace(id=1, username="me")

    async def iter_dialogs(self):
        for entity in self._entities:
            yield SimpleNamespace(entity=entity)


class BotClient:
    """A bot: can't list dialogs; knows some entities."""

    def __init__(self, known=None):
        self._known = known or {}

    async def get_me(self):
        return SimpleNamespace(id=999, username="a_bot")

    async def iter_dialogs(self):
        raise BotMethodInvalidError(request=None)
        yield  # pragma: no cover

    async def get_entity(self, entry):
        if entry in self._known:
            return self._known[entry]
        raise ValueError(f"unknown {entry!r}")

    async def sign_in(self, *, bot_token):
        self.signed_in_with = bot_token


def test_bot_token_for(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_WORK", raising=False)
    assert access.bot_token_for("default") is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:a")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_WORK", "2:b")
    assert access.bot_token_for("default") == "1:a"
    assert access.bot_token_for("work") == "2:b"
    assert access.bot_token_for("other") is None


def test_allowlist_entries(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", " 42, @Team ,-1001234,42 ")
    assert access.allowlist_entries() == [42, "Team", -1001234]
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS")
    assert access.allowlist_entries() == []


@pytest.mark.asyncio
async def test_user_account_registers_only_allowlisted_dialogs(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    client = DialogClient([make_user(42), make_user(43), make_user(1)])
    ids = [d["id"] for d in await enumerate_channels(client, "default")]
    assert "telegram:default:dm:42" in ids
    assert "telegram:default:dm:43" not in ids
    assert "telegram:default:saved" in ids  # the account's own space isn't gated


@pytest.mark.asyncio
async def test_user_account_without_allowlist_registers_everything(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    client = DialogClient([make_user(42), make_user(43)])
    ids = [d["id"] for d in await enumerate_channels(client, "default")]
    assert {"telegram:default:dm:42", "telegram:default:dm:43"} <= set(ids)


@pytest.mark.asyncio
async def test_bot_registers_allowlisted_chats(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42,7,-1000000000555")
    client = BotClient(known={42: make_user(42, first_name="Owner")})
    descriptors = await enumerate_channels(client, "default")
    # 42 resolved, 7 registered by id, the unresolvable group skipped
    assert [d["id"] for d in descriptors] == ["telegram:default:dm:42", "telegram:default:dm:7"]
    assert descriptors[0]["label"] != "user:42"


@pytest.mark.asyncio
async def test_bot_without_allowlist_registers_nothing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    assert await enumerate_channels(BotClient(), "default") == []


class SendClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, peer, text, **kwargs):
        self.sent.append((peer, text))
        return SimpleNamespace(id=5)


def _publish_handler(client):
    async def resolve(peer_id, _client):
        return f"peer:{peer_id}"

    async def connected(_client):
        return None

    return make_publish_handler(
        {"default": client}, resolve_entity_fn=resolve, ensure_connected_fn=connected
    )


@pytest.mark.asyncio
async def test_publish_refuses_chats_outside_the_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    client = SendClient()
    handle = _publish_handler(client)
    text = [{"type": "text", "text": "hi"}]
    assert (await handle({"channelId": "telegram:default:dm:42", "content": text}))["delivered"]
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_CHAT_IDS"):
        await handle({"channelId": "telegram:default:dm:43", "content": text})
    await handle({"channelId": "telegram:default:saved", "content": text})
    assert [peer for peer, _ in client.sent] == ["peer:42", "me"]


@pytest.mark.asyncio
async def test_sign_in_with_bot_token(monkeypatch):
    from telegram_mcp import runner

    client = BotClient()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert await runner._sign_in_with_bot_token("default", client) is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:a")
    assert await runner._sign_in_with_bot_token("default", client) is True
    assert client.signed_in_with == "1:a"
