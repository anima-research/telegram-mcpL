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

import logging
from types import SimpleNamespace

import pytest
from telethon.errors import RPCError
from telethon.tl import types

from telegram_mcp.mcpl.channels import entity_to_descriptor, forum_topic_of, publish_declarations
from telegram_mcp.mcpl.events import redeclare_publish_target
from telegram_mcp.mcpl.handlers import (
    make_list_handler,
    make_open_handler,
    make_publish_handler,
    publish_frame_handler,
)
from telegram_mcp.mcpl.policy import PolicyState
from telegram_mcp.mcpl.ready import make_on_ready
from tests.test_mcpl_channels import make_channel


@pytest.fixture(autouse=True)
def fresh_declarations():
    # The store is process-wide: no test sees another's records, even when
    # one fails partway.
    publish_declarations.reset()
    yield
    publish_declarations.reset()


class Client:
    """A Telethon stand-in: send_message records and answers like Telegram
    (its own reply header naming the topic for a topic post), and calling the
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
            header = types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=kwargs["reply_to"])
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
        # Topic ids not in the form this server sends. Where int() reads one
        # as a topic, the forum holds that topic, so only the check stands
        # between "01" or Arabic-Indic one and General (topic 1), and between
        # "05" and topic 5 with an echo of "5". Superscript one passes
        # isdigit() but makes int() raise.
        ({"topics": [SimpleNamespace(id=1)]}, FORUM, "01", "invalid threadId"),
        ({"topics": [SimpleNamespace(id=1)]}, FORUM, "\u0661", "invalid threadId"),
        ({"topics": [SimpleNamespace(id=5)]}, FORUM, "05", "invalid threadId"),
        ({}, FORUM, "\u00b9", "invalid threadId"),
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
async def test_a_post_telegram_reports_outside_any_topic_is_echoed_as_the_chat_itself():
    # Telegram's own header, read as incoming threadId is: a reply that isn't
    # in a topic is in the chat itself. The host compares it with "42".
    client = Client(
        topics=[SimpleNamespace(id=42)],
        header=types.MessageReplyHeader(forum_topic=False, reply_to_msg_id=42),
    )
    result = await handler(client, FORUM)(params(threadId="42"))
    assert result == {"delivered": True, "messageId": "999", "threadId": None}


@pytest.mark.asyncio
async def test_a_header_telethon_rebuilt_from_the_request_carries_no_echo():
    # On Telegram's short answer, Telethon builds the sent message from the
    # request, so its header is the InputReplyToMessage we sent: no report of
    # where the post landed.
    client = Client(
        topics=[SimpleNamespace(id=42)],
        header=types.InputReplyToMessage(reply_to_msg_id=42),
    )
    result = await handler(client, FORUM)(params(threadId="42"))
    assert result == {"delivered": True, "messageId": "999"}


@pytest.mark.asyncio
async def test_a_reply_inside_a_topic_echoes_the_topic_not_the_replied_message():
    client = Client(
        topics=[SimpleNamespace(id=42)],
        header=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=77, reply_to_top_id=42),
    )
    result = await handler(client, FORUM)(params(threadId="42"))
    assert result["threadId"] == "42"


@pytest.mark.asyncio
async def test_a_post_telegram_places_in_general_echoes_the_chat_itself():
    # General is topic 1, which is the chat itself: null, as a host asked.
    client = Client(header=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=1))
    result = await handler(client, FORUM)(params(threadId=None))
    assert result == {"delivered": True, "messageId": "999", "threadId": None}


@pytest.mark.parametrize(
    "header, topic",
    [
        (None, None),
        (types.MessageReplyHeader(forum_topic=False, reply_to_msg_id=50), None),
        (types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=42), "42"),
        (types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=77, reply_to_top_id=42), "42"),
        (types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=1), None),
        (types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=77, reply_to_top_id=1), None),
        (types.MessageReplyHeader(forum_topic=True), None),
    ],
)
def test_one_reading_of_a_reply_header_names_the_topic(header, topic):
    assert forum_topic_of(header) == topic


@pytest.mark.asyncio
async def test_a_post_that_landed_elsewhere_is_echoed_as_it_landed():
    # The echo is Telegram's answer, not the request: the host compares them.
    client = Client(
        topics=[SimpleNamespace(id=42)],
        header=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=43),
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
    # Recorded as declared: the same chat reads as current, and a forum
    # turned on since reads as a change.
    assert not publish_declarations.is_stale(entity_to_descriptor(plain, account_label="default"))
    forum_now = make_channel(2001, broadcast=False, megagroup=True, forum=True)
    assert publish_declarations.is_stale(entity_to_descriptor(forum_now, account_label="default"))


@pytest.mark.asyncio
async def test_a_chat_that_turns_its_forum_on_is_redeclared_exact_before_its_messages():
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


@pytest.mark.asyncio
async def test_redeclaration_needs_the_chat_registered_and_the_register_grant():
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


@pytest.mark.asyncio
async def test_a_topic_message_is_forwarded_only_after_its_chat_is_redeclared(monkeypatch):
    import telegram_mcp.mcpl.events as events_mod
    from telegram_mcp.mcpl.events import attach_event_handlers, reset_attached_clients

    reset_attached_clients()
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
    reset_attached_clients()


# ---------------------------------------------------------------------------
# The declaration the host holds decides; every failure before the send is a
# refusal; every descriptor sent is recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_chat_last_declared_root_refuses_a_topic_even_with_its_forum_on():
    # The forum came on, but the host hasn't been told: it holds root.
    publish_declarations.record(
        [
            entity_to_descriptor(
                make_channel(2001, broadcast=False, megagroup=True), account_label="default"
            )
        ]
    )
    client = Client(topics=[SimpleNamespace(id=42)])
    result = await handler(client, FORUM)(params(threadId="42"))
    assert result["delivered"] is False and "declared root" in result["reason"]
    assert client.sent == [] and client.lookups == []
    # null is honored on a root chat.
    result = await handler(client, FORUM)(params(threadId=None))
    assert result == {"delivered": True, "messageId": "999", "threadId": None}


@pytest.mark.asyncio
async def test_failures_before_the_send_are_refusals_not_errors():
    async def ensure(c):
        pass

    async def unresolvable(peer_id, client_):
        raise ValueError("Could not find the input entity for PeerChannel(channel_id=2001)")

    client = Client(topics=[SimpleNamespace(id=42)])
    publish = make_publish_handler(
        {"default": client}, resolve_entity_fn=unresolvable, ensure_connected_fn=ensure
    )
    result = await publish(params(threadId="42"))
    assert result["delivered"] is False and "could not reach this chat" in result["reason"]
    assert "messageId" not in result and client.sent == []

    result = await publish(params("telegram:other:supergroup:2001", threadId="42"))
    assert result["delivered"] is False and "unknown account" in result["reason"]

    result = await publish(params("not-a-telegram-id", threadId=None))
    assert result["delivered"] is False and "could not reach this chat" in result["reason"]

    # An invalid target is refused before the chat is resolved at all.
    resolved = []

    async def resolve(peer_id, client_):
        resolved.append(peer_id)
        return FORUM

    publish = make_publish_handler(
        {"default": client}, resolve_entity_fn=resolve, ensure_connected_fn=ensure
    )
    result = await publish(params(threadId="abc"))
    assert result["delivered"] is False and "invalid threadId" in result["reason"]
    assert resolved == []


@pytest.mark.asyncio
async def test_a_refused_target_is_named_bounded():
    client = Client()
    huge = "x" * 100_000
    result = await handler(client, FORUM)(params(threadId=huge))
    assert result["delivered"] is False and len(result["reason"]) < 300
    # A topic id is a message id: more than ten digits is no topic id.
    result = await handler(client, FORUM)(params(threadId="12345678901"))
    assert result["delivered"] is False and "invalid threadId" in result["reason"]
    assert client.sent == []


@pytest.mark.asyncio
async def test_a_dropped_targeted_notification_is_logged_where_an_operator_looks(caplog):
    client = Client()
    frame = publish_frame_handler(handler(client, PLAIN))
    with caplog.at_level(logging.ERROR, logger="telegram_mcp.mcpl"):
        await frame(params("telegram:default:dm:42", threadId=None), False)
    assert client.sent == []
    assert any(
        r.levelno >= logging.ERROR and "telegram:default:dm:42" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_the_list_answer_and_the_open_answer_carry_what_the_host_holds(monkeypatch):
    import telegram_mcp.mcpl.handlers as handlers_mod

    plain = entity_to_descriptor(
        make_channel(2001, broadcast=False, megagroup=True), account_label="default"
    )

    async def enumerate_channels(client, label):
        return [plain]

    monkeypatch.setattr(handlers_mod, "enumerate_channels", enumerate_channels)
    listed = await make_list_handler({"default": object()})({})
    assert listed == {"channels": [plain]}
    # Recorded: a forum turned on later is a change to re-declare.
    forum_now = entity_to_descriptor(
        make_channel(2001, broadcast=False, megagroup=True, forum=True), account_label="default"
    )
    assert publish_declarations.is_stale(forum_now)

    opened = await make_open_handler()({"type": "telegram", "address": {"id": plain["id"]}})
    assert opened["channel"]["capabilities"] == {"publish": {"target": "root"}}
    # A channel never declared gets no declaration in the answer.
    opened = await make_open_handler()(
        {"type": "telegram", "address": {"id": "telegram:default:dm:7"}}
    )
    assert "capabilities" not in opened["channel"]


# ---------------------------------------------------------------------------
# Re-declaration through the real handlers
# ---------------------------------------------------------------------------


class HandlerClient:
    """Captures the handlers attach_event_handlers registers, by event:
    NewMessage, ChatAction, and Raw (Telegram's updateChannel)."""

    def __init__(self, entity=None):
        self.handlers: dict = {}
        self.entity = entity
        self.fetched: list = []

    async def get_me(self):
        return SimpleNamespace(id=99, username="me")

    async def get_entity(self, peer):
        self.fetched.append(peer)
        if isinstance(self.entity, Exception):
            raise self.entity
        return self.entity

    def on(self, event_filter):
        def decorator(fn):
            name = getattr(event_filter, "__name__", None) or type(event_filter).__name__
            self.handlers[name] = fn
            return fn

        return decorator


async def attach(monkeypatch, client, transport, payload=None):
    import telegram_mcp.mcpl.events as events_mod
    from telegram_mcp.mcpl.events import attach_event_handlers, reset_attached_clients

    reset_attached_clients()
    if payload is not None:

        async def incoming(event, **kwargs):
            return payload

        monkeypatch.setattr(events_mod, "build_incoming_message", incoming)
    await attach_event_handlers(client, account_label="default", transport=transport)
    reset_attached_clients()


TOPIC_MESSAGE = {
    "channelId": "telegram:default:supergroup:2001",
    "messageId": "7",
    "threadId": "42",
    "content": [],
}


def declare(**channel):
    descriptor = entity_to_descriptor(
        make_channel(2001, broadcast=False, megagroup=True, **channel), account_label="default"
    )
    publish_declarations.record([descriptor])
    return descriptor


@pytest.mark.asyncio
async def test_telegrams_channel_update_redeclares_a_forum_turned_off(monkeypatch):
    declare(forum=True)
    client = HandlerClient(entity=make_channel(2001, broadcast=False, megagroup=True))
    transport = Transport()
    await attach(monkeypatch, client, transport)

    await client.handlers["Raw"](types.UpdateChannel(channel_id=2001))
    assert [m for m, _ in transport.sent] == ["channels/changed"]
    assert transport.sent[0][1]["updated"][0]["capabilities"] == {"publish": {"target": "root"}}
    assert publish_declarations.target_of("telegram:default:supergroup:2001") == "root"

    # Again, with nothing changed: nothing to say.
    await client.handlers["Raw"](types.UpdateChannel(channel_id=2001))
    assert len(transport.sent) == 1


@pytest.mark.asyncio
async def test_a_channel_update_for_a_chat_the_host_holds_nothing_for_is_ignored(monkeypatch):
    client = HandlerClient(entity=make_channel(3001, broadcast=False, megagroup=True, forum=True))
    transport = Transport()
    await attach(monkeypatch, client, transport)
    await client.handlers["Raw"](types.UpdateChannel(channel_id=3001))
    assert transport.sent == [] and client.fetched == []


@pytest.mark.asyncio
async def test_a_channel_update_telegram_wont_let_us_refetch_is_a_warning(monkeypatch, caplog):
    # An account that left a supergroup, or was removed from it, gets
    # updateChannel too, and its refetch may be refused: a warning naming the
    # chat, without a traceback.
    declare(forum=True)
    refused = RPCError(request=None, message="CHANNEL_PRIVATE", code=400)
    client = HandlerClient(entity=refused)
    transport = Transport()
    await attach(monkeypatch, client, transport)
    with caplog.at_level(logging.WARNING, logger="telegram_mcp.mcpl"):
        await client.handlers["Raw"](types.UpdateChannel(channel_id=2001))
    assert transport.sent == []
    assert publish_declarations.target_of("telegram:default:supergroup:2001") == "exact"
    [record] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert "telegram:default:supergroup:2001" in record.getMessage()
    assert "CHANNEL_PRIVATE" in record.getMessage()


@pytest.mark.asyncio
async def test_a_topic_message_proves_its_chat_a_forum_whatever_a_cached_chat_says(monkeypatch):
    declare()  # registered root
    client = HandlerClient()
    transport = Transport()
    await attach(monkeypatch, client, transport, payload=TOPIC_MESSAGE)

    async def stale_chat():
        return make_channel(2001, broadcast=False, megagroup=True)  # forum not yet seen

    await client.handlers["NewMessage"](SimpleNamespace(out=False, get_chat=stale_chat))
    assert [m for m, _ in transport.sent] == ["channels/changed", "channels/incoming"]
    assert transport.sent[0][1]["updated"][0]["capabilities"] == {"publish": {"target": "exact"}}


@pytest.mark.asyncio
async def test_a_failed_redeclaration_never_costs_the_message(monkeypatch):
    declare()
    client = HandlerClient()
    transport = Transport()
    await attach(monkeypatch, client, transport, payload=TOPIC_MESSAGE)

    async def unreachable():
        raise ConnectionError("lost the chat")

    await client.handlers["NewMessage"](SimpleNamespace(out=False, get_chat=unreachable))
    assert [m for m, _ in transport.sent] == ["channels/incoming"]
    assert transport.sent[0][1]["messages"][0]["threadId"] == "42"


@pytest.mark.asyncio
async def test_a_join_records_what_it_declared(monkeypatch):
    import telegram_mcp.mcpl.events as events_mod

    joined = entity_to_descriptor(
        make_channel(2001, broadcast=False, megagroup=True, forum=True), account_label="default"
    )

    async def join_payload(event, **kwargs):
        return {"added": [joined]}

    monkeypatch.setattr(events_mod, "_build_chat_action_payload", join_payload)
    client = HandlerClient()
    transport = Transport()
    await attach(monkeypatch, client, transport)
    await client.handlers["ChatAction"](SimpleNamespace())
    assert [m for m, _ in transport.sent] == ["channels/changed"]
    assert publish_declarations.target_of(joined["id"]) == "exact"


@pytest.mark.asyncio
async def test_a_new_host_session_starts_with_no_records():
    publish_declarations.record(
        [
            entity_to_descriptor(
                make_channel(9999, broadcast=False, megagroup=True), account_label="default"
            )
        ]
    )
    plain = make_channel(2001, broadcast=False, megagroup=True)

    async def enumerate_channels(client, label):
        return [entity_to_descriptor(plain, account_label=label)]

    async def attach_handlers(client, **kwargs):
        pass

    on_ready = make_on_ready(
        {"default": object()},
        policy=PolicyState(),
        enumerate_channels=enumerate_channels,
        attach_event_handlers=attach_handlers,
    )
    await on_ready(Transport())
    assert publish_declarations.target_of("telegram:default:supergroup:9999") is None
    assert publish_declarations.target_of("telegram:default:supergroup:2001") == "root"


@pytest.mark.asyncio
async def test_the_runners_dispatcher_takes_a_targeted_publish_only_as_a_request(monkeypatch):
    import telegram_mcp.runner as runner

    client = Client()

    async def resolve(peer_id, client_):
        return PLAIN

    async def ensure(c):
        pass

    monkeypatch.setattr(runner, "clients", {"default": client})
    monkeypatch.setattr(runner, "resolve_entity", resolve)
    monkeypatch.setattr(runner, "ensure_connected", ensure)
    dispatcher = runner._build_dispatcher(PolicyState())

    class Wire:
        def __init__(self):
            self.responses: list = []

        async def send_response(self, msg_id, result):
            self.responses.append((msg_id, result))

        async def send_error(self, msg_id, code, message, data=None):
            self.responses.append((msg_id, {"error": code, "message": message}))

    wire = Wire()
    targeted = params("telegram:default:dm:42", threadId=None)
    await dispatcher.handle_inbound(
        {"jsonrpc": "2.0", "method": "channels/publish", "params": targeted}, wire
    )
    assert client.sent == [] and wire.responses == []

    await dispatcher.handle_inbound(
        {
            "jsonrpc": "2.0",
            "method": "channels/publish",
            "params": params("telegram:default:dm:42"),
        },
        wire,
    )
    assert len(client.sent) == 1 and wire.responses == []

    await dispatcher.handle_inbound(
        {"jsonrpc": "2.0", "id": 5, "method": "channels/publish", "params": targeted}, wire
    )
    assert len(client.sent) == 2
    assert wire.responses == [(5, {"delivered": True, "messageId": "999", "threadId": None})]
