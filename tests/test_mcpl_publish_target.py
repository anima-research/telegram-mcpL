"""MCPL RFC-011 targeted publish on Telegram.

A forum supergroup declares `exact`: a publish naming a topic lands exactly
in that topic, None lands in the chat itself (General), and anything that
can't be honored fails with nothing posted. Every other chat declares
`root`: None lands in the chat, and a topic id is refused. A publish without
`threadId` keeps the legacy behavior (covered in test_mcpl_handlers.py).

Platform boundary: Telethon's client is a stub here. Telegram's own answers
(the sent message's reply header, GetForumTopicsByID, a refusal's RPC code)
are modeled, not observed.
"""

from types import SimpleNamespace

import pytest
from telethon.errors import RPCError
from telethon.tl import types

from telegram_mcp.mcpl.channels import entity_to_descriptor, publish_declarations
from telegram_mcp.mcpl.events import redeclare_publish_target
from telegram_mcp.mcpl.handlers import make_publish_handler, publish_frame_handler
from telegram_mcp.mcpl.policy import PolicyState
from telegram_mcp.mcpl.ready import make_on_ready
from tests.test_mcpl_channels import make_channel


class Client:
    """A Telethon stand-in: send_message records and answers like Telegram
    (a reply header naming the topic for a topic post), and calling the
    client runs a forum-topics lookup."""

    def __init__(self, *, topics=None, lookup_error=None, send_error=None, header=...):
        self.sent: list[tuple] = []
        self.lookups: list = []
        self._topics = topics if topics is not None else []
        self._lookup_error = lookup_error
        self._send_error = send_error
        self._header = header

    async def send_message(self, peer, text, **kwargs):
        if self._send_error is not None:
            raise self._send_error
        self.sent.append((peer, text, kwargs))
        if self._header is not ...:
            header = self._header
        elif "reply_to" in kwargs:
            header = SimpleNamespace(
                forum_topic=True, reply_to_msg_id=kwargs["reply_to"], reply_to_top_id=None
            )
        else:
            header = None
        return SimpleNamespace(id=999, reply_to=header)

    async def __call__(self, request):
        self.lookups.append(request)
        if self._lookup_error is not None:
            raise self._lookup_error
        return SimpleNamespace(topics=self._topics)


FORUM = SimpleNamespace(forum=True)
PLAIN = SimpleNamespace(forum=False)


def handler(client, peer):
    async def resolve(peer_id, client_):
        return peer

    async def ensure(c):
        pass

    return make_publish_handler(
        {"default": client}, resolve_entity_fn=resolve, ensure_connected_fn=ensure
    )


def params(channel="telegram:default:supergroup:2001", **extra):
    return {"channelId": channel, "content": [{"type": "text", "text": "answer"}], **extra}


@pytest.mark.asyncio
async def test_topic_target_posts_into_that_topic_and_echoes_it():
    client = Client(topics=[SimpleNamespace(id=42, closed=False)])
    result = await handler(client, FORUM)(params(threadId="42"))
    assert result == {"delivered": True, "messageId": "999", "threadId": "42"}
    assert client.sent == [(FORUM, "answer", {"reply_to": 42})]
    assert len(client.lookups) == 1
    assert client.lookups[0].topics == [42]


@pytest.mark.asyncio
async def test_null_posts_in_the_chat_itself_and_echoes_null():
    for peer, channel in [
        (FORUM, "telegram:default:supergroup:2001"),
        (PLAIN, "telegram:default:dm:42"),
    ]:
        client = Client()
        result = await handler(client, peer)(params(channel, threadId=None))
        assert result == {"delivered": True, "messageId": "999", "threadId": None}
        assert client.sent == [(peer, "answer", {})]
        assert client.lookups == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_kwargs, peer, thread_id, reason",
    [
        ({}, PLAIN, "42", "no forum topics"),
        ({"topics": []}, FORUM, "42", "no topic 42"),
        ({"topics": [types.ForumTopicDeleted(id=42)]}, FORUM, "42", "no topic 42"),
        ({"lookup_error": ConnectionError("down")}, FORUM, "42", "could not confirm topic 42"),
        ({}, FORUM, "abc", "invalid threadId"),
        ({}, FORUM, "0", "invalid threadId"),
        ({}, FORUM, "", "invalid threadId"),
        ({}, FORUM, 42, "invalid threadId"),
    ],
)
async def test_targets_that_cannot_be_honored_post_nothing(client_kwargs, peer, thread_id, reason):
    client = Client(**client_kwargs)
    result = await handler(client, peer)(params(threadId=thread_id))
    assert result["delivered"] is False
    assert "messageId" not in result and "threadId" not in result
    assert reason in result["reason"]
    assert client.sent == []


@pytest.mark.asyncio
async def test_reply_to_message_id_cannot_be_combined_with_a_target():
    client = Client(topics=[SimpleNamespace(id=42)])
    result = await handler(client, FORUM)(params(threadId="42", replyToMessageId="7"))
    assert result["delivered"] is False
    assert "replyToMessageId" in result["reason"]
    assert client.sent == []


@pytest.mark.asyncio
async def test_telegrams_refusal_is_a_definite_failure_and_other_errors_stay_errors():
    refused = RPCError(request=None, message="TOPIC_CLOSED", code=400)
    client = Client(topics=[SimpleNamespace(id=42)], send_error=refused)
    result = await handler(client, FORUM)(params(threadId="42"))
    assert result["delivered"] is False
    assert "messageId" not in result
    assert "Telegram refused" in result["reason"]

    flaky = Client(send_error=RPCError(request=None, message="INTERNAL", code=500))
    with pytest.raises(RPCError):
        await handler(flaky, FORUM)(params(threadId=None))


@pytest.mark.asyncio
async def test_a_reply_header_that_cannot_tell_carries_no_echo():
    client = Client(
        topics=[SimpleNamespace(id=42)],
        header=SimpleNamespace(forum_topic=False, reply_to_msg_id=42),
    )
    result = await handler(client, FORUM)(params(threadId="42"))
    assert result == {"delivered": True, "messageId": "999"}


@pytest.mark.asyncio
async def test_a_post_that_landed_elsewhere_is_echoed_as_it_landed():
    # The echo is Telegram's answer, not the request: the host compares them.
    client = Client(
        topics=[SimpleNamespace(id=42)],
        header=SimpleNamespace(forum_topic=True, reply_to_msg_id=43, reply_to_top_id=None),
    )
    result = await handler(client, FORUM)(params(threadId="42"))
    assert result["threadId"] == "43"


@pytest.mark.asyncio
async def test_a_targeted_publish_must_be_a_request():
    client = Client()
    frame = publish_frame_handler(handler(client, PLAIN))
    # A Notification naming a place can't be answered: dropped, never posted.
    assert await frame(params("telegram:default:dm:42", threadId=None), False) is None
    assert client.sent == []
    # A legacy Notification still posts; a targeted Request posts and answers.
    await frame(params("telegram:default:dm:42"), False)
    result = await frame(params("telegram:default:dm:42", threadId=None), True)
    assert result == {"delivered": True, "messageId": "999", "threadId": None}
    assert len(client.sent) == 2


@pytest.mark.asyncio
async def test_an_empty_targeted_publish_is_a_refusal_not_an_error():
    client = Client()
    result = await handler(client, PLAIN)(
        {"channelId": "telegram:default:dm:42", "content": [], "threadId": None}
    )
    assert result["delivered"] is False and "messageId" not in result
    assert client.sent == []
    # Legacy callers keep the error.
    with pytest.raises(ValueError):
        await handler(client, PLAIN)({"channelId": "telegram:default:dm:42", "content": []})


@pytest.mark.asyncio
async def test_topic_one_is_general_and_is_refused_as_a_topic():
    client = Client(topics=[SimpleNamespace(id=1)])
    result = await handler(client, FORUM)(params(threadId="1"))
    assert result["delivered"] is False
    assert "threadId null" in result["reason"]
    assert client.sent == [] and client.lookups == []


# ---------------------------------------------------------------------------
# Re-declaration when a chat's publish target changes after registration
# ---------------------------------------------------------------------------


class Transport:
    host_capabilities: dict = {}

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send_notification(self, method, params):
        self.sent.append((method, params))


@pytest.mark.asyncio
async def test_registration_records_what_each_chat_declared():
    publish_declarations.reset()
    plain = make_channel(2001, broadcast=False, megagroup=True)

    async def enumerate_channels(client, label):
        return [entity_to_descriptor(plain, account_label=label)]

    async def attach(client, **kwargs):
        pass

    transport = Transport()
    on_ready = make_on_ready(
        {"default": object()},
        policy=PolicyState(),
        enumerate_channels=enumerate_channels,
        attach_event_handlers=attach,
    )
    await on_ready(transport)
    assert transport.sent[0][0] == "channels/register"
    forum_now = make_channel(2001, broadcast=False, megagroup=True, forum=True)
    assert publish_declarations.is_stale(entity_to_descriptor(forum_now, account_label="default"))
    publish_declarations.reset()


@pytest.mark.asyncio
async def test_a_chat_that_turns_its_forum_on_is_redeclared_exact_before_its_messages():
    publish_declarations.reset()
    registered = entity_to_descriptor(
        make_channel(2001, broadcast=False, megagroup=True), account_label="default"
    )
    publish_declarations.record([registered])
    transport = Transport()
    forum_now = make_channel(2001, broadcast=False, megagroup=True, forum=True)

    kwargs = dict(account_label="default", self_id=99, transport=transport)
    assert await redeclare_publish_target(forum_now, **kwargs) is True
    assert transport.sent == [
        (
            "channels/changed",
            {"updated": [entity_to_descriptor(forum_now, account_label="default", self_id=99)]},
        )
    ]
    assert transport.sent[0][1]["updated"][0]["capabilities"] == {"publish": {"target": "exact"}}
    # Declared now: the next message re-declares nothing.
    assert await redeclare_publish_target(forum_now, **kwargs) is False
    assert len(transport.sent) == 1

    # Turning it off again re-declares root.
    assert (
        await redeclare_publish_target(
            make_channel(2001, broadcast=False, megagroup=True), **kwargs
        )
        is True
    )
    assert transport.sent[-1][1]["updated"][0]["capabilities"] == {"publish": {"target": "root"}}
    publish_declarations.reset()


@pytest.mark.asyncio
async def test_redeclaration_needs_the_chat_registered_and_the_register_grant():
    publish_declarations.reset()
    transport = Transport()
    forum_now = make_channel(2001, broadcast=False, megagroup=True, forum=True)
    # Never registered: nothing to correct.
    assert (
        await redeclare_publish_target(
            forum_now, account_label="default", self_id=99, transport=transport
        )
        is False
    )

    publish_declarations.record(
        [
            entity_to_descriptor(
                make_channel(2001, broadcast=False, megagroup=True), account_label="default"
            )
        ]
    )
    no_register = SimpleNamespace(can_register=False)
    assert (
        await redeclare_publish_target(
            forum_now,
            account_label="default",
            self_id=99,
            transport=transport,
            policy=no_register,
        )
        is False
    )
    assert transport.sent == []
    publish_declarations.reset()


@pytest.mark.asyncio
async def test_a_topic_message_is_forwarded_only_after_its_chat_is_redeclared(monkeypatch):
    import telegram_mcp.mcpl.events as events_mod
    from telegram_mcp.mcpl.events import attach_event_handlers, reset_attached_clients

    reset_attached_clients()
    publish_declarations.reset()
    publish_declarations.record(
        [
            entity_to_descriptor(
                make_channel(2001, broadcast=False, megagroup=True), account_label="default"
            )
        ]
    )
    forum_now = make_channel(2001, broadcast=False, megagroup=True, forum=True)

    class FakeClient:
        def __init__(self):
            self.handlers: dict = {}

        async def get_me(self):
            return SimpleNamespace(id=99, username="me")

        def on(self, event_filter):
            def decorator(fn):
                self.handlers[getattr(event_filter, "__name__", "")] = fn
                return fn

            return decorator

    async def topic_message(event, **kwargs):
        return {
            "channelId": "telegram:default:supergroup:2001",
            "messageId": "7",
            "threadId": "42",
            "content": [],
        }

    monkeypatch.setattr(events_mod, "build_incoming_message", topic_message)
    transport = Transport()
    client = FakeClient()
    await attach_event_handlers(client, account_label="default", transport=transport)

    async def get_chat():
        return forum_now

    await client.handlers["NewMessage"](SimpleNamespace(out=False, get_chat=get_chat))
    assert [m for m, _ in transport.sent] == ["channels/changed", "channels/incoming"]
    assert transport.sent[0][1]["updated"][0]["capabilities"] == {"publish": {"target": "exact"}}
    publish_declarations.reset()
    reset_attached_clients()
