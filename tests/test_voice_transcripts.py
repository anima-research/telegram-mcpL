"""Integration tests: transcript mixing into message_to_dict / format_message_line /
list_messages, plus the transcribe_voice tool.

message_to_dict/format_message_line are also exercised directly (without a
chat_id) by test_reply_quote.py and test_forward_attribution.py with bare fake
messages that have no voice/video_note attribute - those keep passing
unmodified, which is the backward-compatibility contract these tests pin down
explicitly for the voice case.
"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from telegram_mcp import transcription
from telegram_mcp.tools import messages
from telegram_mcp.tools.messages import format_message_line, message_to_dict


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


def _msg(**overrides):
    base = dict(
        id=200,
        sender=None,
        sender_id=42,
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        message=None,
        reply_to=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _voice_msg(**overrides):
    base = dict(
        voice=SimpleNamespace(),
        video_note=None,
        file=SimpleNamespace(duration=23, ext=".oga", mime_type="audio/ogg"),
    )
    base.update(overrides)
    return _msg(**base)


class _FakeClient:
    def __init__(self, messages_list=None, messages_by_id=None):
        self._list = messages_list or []
        self._by_id = messages_by_id or {}
        self.download_calls = []

    async def get_messages(self, entity, limit=None, ids=None, **kwargs):
        if ids is not None:
            return self._by_id.get(ids)
        return self._list[:limit] if limit else list(self._list)

    async def get_input_entity(self, entity):
        return "peer"

    async def __call__(self, request):
        raise AssertionError("no raw RPC expected in this test")

    async def download_media(self, msg, file=None):
        self.download_calls.append(msg.id)
        return b"audio-bytes"


def _patch_client(monkeypatch, client, entity, numeric_chat_id):
    monkeypatch.setattr(messages, "get_client", lambda account=None: client)
    monkeypatch.setattr(messages, "resolve_entity", _async_return(entity))
    monkeypatch.setattr(messages, "get_marked_id", lambda e: numeric_chat_id)


# ---------------------------------------------------------------------------
# message_to_dict
# ---------------------------------------------------------------------------


def test_message_to_dict_voice_without_chat_id_is_backward_compatible():
    d = message_to_dict(_voice_msg())  # no chat_id, as existing callers use it
    assert d["media"] == "voice"
    assert d["duration"] == 23
    assert "transcript" not in d
    assert "transcript_status" not in d


def test_message_to_dict_voice_cache_miss_is_pending(monkeypatch, transcript_cache_dir):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    d = message_to_dict(_voice_msg(id=5), chat_id=1)
    assert d["duration"] == 23
    assert d["transcript_status"] == "pending"
    assert "transcript" not in d


def test_message_to_dict_voice_cache_hit_includes_source_and_note(
    monkeypatch, transcript_cache_dir
):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    transcription.save_transcript(1, 5, "groq", "hello there", duration=23)

    d = message_to_dict(_voice_msg(id=5), chat_id=1)

    assert d["transcript"] == "hello there"
    assert d["transcript_source"] == "groq"
    assert "not a verbatim quote" in d["transcript_note"]
    assert "transcript_status" not in d


def test_message_to_dict_voice_off_mode_shows_duration_only(monkeypatch, transcript_cache_dir):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "off")
    transcription.save_transcript(1, 5, "groq", "must not surface")

    d = message_to_dict(_voice_msg(id=5), chat_id=1)

    assert d["duration"] == 23
    assert "transcript" not in d
    assert "transcript_status" not in d


def test_message_to_dict_text_message_untouched():
    d = message_to_dict(_msg(message="hello"))
    assert d["text"] == "hello"
    assert "duration" not in d
    assert "transcript" not in d


# ---------------------------------------------------------------------------
# format_message_line
# ---------------------------------------------------------------------------


def test_format_message_line_voice_without_chat_id_shows_duration_only():
    line = format_message_line(
        _voice_msg(file=SimpleNamespace(duration=83, ext=".oga", mime_type="audio/ogg"))
    )
    assert "Message: voice 1:23" in line
    assert "transcript" not in line


def test_format_message_line_voice_ready_includes_transcript_and_source(
    monkeypatch, transcript_cache_dir
):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    transcription.save_transcript(1, 5, "telegram", "we should ship this")

    line = format_message_line(_voice_msg(id=5), chat_id=1)

    assert "voice 0:23 | transcript (telegram, not verbatim): we should ship this" in line


def test_format_message_line_voice_pending(monkeypatch, transcript_cache_dir):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")

    line = format_message_line(_voice_msg(id=5), chat_id=1)

    assert "Message: voice 0:23 | transcript pending" in line


def test_format_message_line_text_message_still_shows_empty_literal():
    line = format_message_line(_msg(message=None))
    assert "Message: [empty]" in line


# ---------------------------------------------------------------------------
# list_messages: media-label bugfix + transcript mixing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_photo_shows_media_label(monkeypatch, transcript_cache_dir):
    """Upstream bug: list_messages built its record by hand and never called
    get_media_label, so any media-with-no-caption message looked exactly like
    an empty text message. Independent of transcription."""
    entity = SimpleNamespace()
    photo_msg = _msg(id=8, photo=SimpleNamespace())
    client = _FakeClient(messages_list=[photo_msg])
    _patch_client(monkeypatch, client, entity, 555)

    result = await messages.list_messages(chat_id=555, limit=10, account=None)

    rec = json.loads(result)["results"][0]
    assert rec["media"] == "photo"


@pytest.mark.asyncio
async def test_list_messages_voice_shows_media_label_and_cached_transcript(
    monkeypatch, transcript_cache_dir
):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    voice_msg = _voice_msg(id=7)
    transcription.save_transcript(555, 7, "groq", "we should ship this", duration=23)
    client = _FakeClient(messages_list=[voice_msg])
    _patch_client(monkeypatch, client, entity, 555)

    result = await messages.list_messages(chat_id=555, limit=10, account=None)

    rec = json.loads(result)["results"][0]
    assert rec["media"] == "voice"
    assert rec["transcript"] == "we should ship this"
    assert rec["transcript_source"] == "groq"


@pytest.mark.asyncio
async def test_list_messages_voice_cache_miss_is_pending_on_demand(
    monkeypatch, transcript_cache_dir
):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    client = _FakeClient(messages_list=[_voice_msg(id=7)])
    _patch_client(monkeypatch, client, entity, 555)

    result = await messages.list_messages(chat_id=555, limit=10, account=None)

    rec = json.loads(result)["results"][0]
    assert rec["transcript_status"] == "pending"
    assert "transcript" not in rec


@pytest.mark.asyncio
async def test_list_messages_auto_mode_prefetches_and_caches(monkeypatch, transcript_cache_dir):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "auto")
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE_MAX_VOICES", "5")
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE_MAX_SECONDS", "500")
    entity = SimpleNamespace()
    client = _FakeClient(messages_list=[_voice_msg(id=9)])
    _patch_client(monkeypatch, client, entity, 555)
    monkeypatch.setattr(
        transcription,
        "transcribe",
        _async_return({"status": "ok", "text": "prefetched text", "lang": "ru"}),
    )

    result = await messages.list_messages(chat_id=555, limit=10, account=None)

    rec = json.loads(result)["results"][0]
    assert rec["transcript"] == "prefetched text"
    assert transcription.get_cached_transcript(555, 9)["text"] == "prefetched text"


@pytest.mark.asyncio
async def test_list_messages_on_demand_mode_never_prefetches(monkeypatch, transcript_cache_dir):
    """The expensive Groq path must never fire on a plain listing call - only
    auto mode (or an explicit transcribe_voice call) is allowed to spend it."""
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    client = _FakeClient(messages_list=[_voice_msg(id=9)])
    _patch_client(monkeypatch, client, entity, 555)

    async def _must_not_be_called(*a, **kw):
        raise AssertionError("on-demand mode must not call the transcription engine")

    monkeypatch.setattr(transcription, "transcribe", _must_not_be_called)

    await messages.list_messages(chat_id=555, limit=10, account=None)  # must not raise


# ---------------------------------------------------------------------------
# get_history / get_messages threading chat_id through to the cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_history_fills_cached_transcript(monkeypatch, transcript_cache_dir):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    client = _FakeClient(messages_list=[_voice_msg(id=7)])
    _patch_client(monkeypatch, client, entity, 555)
    transcription.save_transcript(555, 7, "telegram", "hello from cache")

    result = await messages.get_history(chat_id=555, limit=10, account=None)

    rec = json.loads(result)["results"][0]
    assert rec["transcript"] == "hello from cache"


@pytest.mark.asyncio
async def test_get_messages_fills_cached_transcript_in_text_line(
    monkeypatch, transcript_cache_dir
):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    client = _FakeClient(messages_list=[_voice_msg(id=7)])
    _patch_client(monkeypatch, client, entity, 555)
    transcription.save_transcript(555, 7, "groq", "cached line text")

    result = await messages.get_messages(chat_id=555, page=1, page_size=10, account=None)

    assert "cached line text" in result
    assert "(groq, not verbatim)" in result


# ---------------------------------------------------------------------------
# transcribe_voice tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_voice_disabled_by_toggle(monkeypatch, transcript_cache_dir):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "off")

    result = await messages.transcribe_voice(chat_id=1, message_id=1, account=None)

    payload = json.loads(result)
    assert payload["transcribed"] is False
    assert payload["reason"] == "transcription_disabled"


@pytest.mark.asyncio
async def test_transcribe_voice_returns_cached_without_calling_engine(
    monkeypatch, transcript_cache_dir
):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    transcription.save_transcript(555, 42, "groq", "cached text", duration=23)
    entity = SimpleNamespace()
    client = _FakeClient()
    _patch_client(monkeypatch, client, entity, 555)

    async def _must_not_be_called(*a, **kw):
        raise AssertionError("must not call the engine on a cache hit")

    monkeypatch.setattr(transcription, "transcribe", _must_not_be_called)

    result = await messages.transcribe_voice(chat_id=555, message_id=42, account=None)

    payload = json.loads(result)
    assert payload == {
        "transcribed": True,
        "cached": True,
        "text": "cached text",
        "source": "groq",
        "duration": 23,
        "note": "Machine transcript, not a verbatim quote.",
    }


@pytest.mark.asyncio
async def test_transcribe_voice_rejects_non_voice_message(monkeypatch, transcript_cache_dir):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    text_msg = _msg(id=9, message="hi")
    client = _FakeClient(messages_by_id={9: text_msg})
    _patch_client(monkeypatch, client, entity, 1)

    result = await messages.transcribe_voice(chat_id=1, message_id=9, account=None)

    assert "no voice message" in result


@pytest.mark.asyncio
async def test_transcribe_voice_rejects_unknown_engine(monkeypatch, transcript_cache_dir):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={5: _voice_msg(id=5)})
    _patch_client(monkeypatch, client, entity, 1)

    result = await messages.transcribe_voice(
        chat_id=1, message_id=5, engine="whisper-cloud", account=None
    )

    assert "Invalid engine" in result


@pytest.mark.asyncio
async def test_transcribe_voice_groq_without_api_key(monkeypatch, transcript_cache_dir):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={5: _voice_msg(id=5)})
    _patch_client(monkeypatch, client, entity, 1)

    result = await messages.transcribe_voice(chat_id=1, message_id=5, engine="groq", account=None)

    assert "GROQ_API_KEY" in result


@pytest.mark.asyncio
async def test_transcribe_voice_pending_degrades_gracefully_without_caching(
    monkeypatch, transcript_cache_dir
):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={5: _voice_msg(id=5)})
    _patch_client(monkeypatch, client, entity, 1)
    monkeypatch.setattr(transcription, "transcribe", _async_return({"status": "pending"}))

    result = await messages.transcribe_voice(
        chat_id=1, message_id=5, engine="telegram", account=None
    )

    payload = json.loads(result)
    assert payload["transcribed"] is False
    assert payload["reason"] == "pending"
    assert transcription.get_cached_transcript(1, 5) is None


@pytest.mark.asyncio
async def test_transcribe_voice_premium_required_degrades_gracefully(
    monkeypatch, transcript_cache_dir
):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={5: _voice_msg(id=5)})
    _patch_client(monkeypatch, client, entity, 1)
    monkeypatch.setattr(transcription, "transcribe", _async_return({"status": "premium_required"}))

    result = await messages.transcribe_voice(
        chat_id=1, message_id=5, engine="telegram", account=None
    )

    payload = json.loads(result)
    assert payload["sent"] is False
    assert payload["reason"] == "telegram_premium_required"


@pytest.mark.asyncio
async def test_transcribe_voice_success_caches_result(monkeypatch, transcript_cache_dir):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={5: _voice_msg(id=5)})
    _patch_client(monkeypatch, client, entity, 1)
    monkeypatch.setattr(
        transcription, "transcribe", _async_return({"status": "ok", "text": "hi", "lang": "ru"})
    )

    result = await messages.transcribe_voice(
        chat_id=1, message_id=5, engine="telegram", account=None
    )

    payload = json.loads(result)
    assert payload["transcribed"] is True
    assert payload["cached"] is False
    assert payload["text"] == "hi"
    assert payload["source"] == "telegram"
    cached = transcription.get_cached_transcript(1, 5)
    assert cached["text"] == "hi"
    assert cached["lang"] == "ru"


@pytest.mark.asyncio
async def test_transcribe_voice_defaults_to_configured_engine(monkeypatch, transcript_cache_dir):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE_ENGINE", "telegram")
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={5: _voice_msg(id=5)})
    _patch_client(monkeypatch, client, entity, 1)

    seen_engine = {}

    async def _fake_transcribe(cl, ent, msg, engine):
        seen_engine["engine"] = engine
        return {"status": "ok", "text": "x"}

    monkeypatch.setattr(transcription, "transcribe", _fake_transcribe)

    await messages.transcribe_voice(chat_id=1, message_id=5, account=None)  # no engine= passed

    assert seen_engine["engine"] == "telegram"


@pytest.mark.asyncio
async def test_transcribe_voice_explicit_engine_overrides_default(
    monkeypatch, transcript_cache_dir
):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE_ENGINE", "groq")
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={5: _voice_msg(id=5)})
    _patch_client(monkeypatch, client, entity, 1)

    seen_engine = {}

    async def _fake_transcribe(cl, ent, msg, engine):
        seen_engine["engine"] = engine
        return {"status": "ok", "text": "x"}

    monkeypatch.setattr(transcription, "transcribe", _fake_transcribe)

    await messages.transcribe_voice(chat_id=1, message_id=5, engine="telegram", account=None)

    assert seen_engine["engine"] == "telegram"


@pytest.mark.asyncio
async def test_transcribe_voice_openai_without_url(monkeypatch, transcript_cache_dir):
    monkeypatch.delenv("TELEGRAM_TRANSCRIBE_OPENAI_URL", raising=False)
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={5: _voice_msg(id=5)})
    _patch_client(monkeypatch, client, entity, 1)

    result = await messages.transcribe_voice(
        chat_id=1, message_id=5, engine="openai", account=None
    )

    assert "TELEGRAM_TRANSCRIBE_OPENAI_URL" in result


@pytest.mark.asyncio
async def test_transcribe_voice_openai_caches_under_its_own_engine(
    monkeypatch, transcript_cache_dir
):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE_OPENAI_URL", "http://localhost:5092/v1")
    transcription.save_transcript(1, 5, "groq", "groq text")
    monkeypatch.setattr(
        transcription,
        "transcribe",
        _async_return({"status": "ok", "text": "openai text", "lang": "nl"}),
    )
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={5: _voice_msg(id=5)})
    _patch_client(monkeypatch, client, entity, 1)

    result = json.loads(
        await messages.transcribe_voice(chat_id=1, message_id=5, engine="OpenAI", account=None)
    )

    assert result["text"] == "openai text"
    assert result["source"] == "openai"
    assert transcription.get_cached_transcript(1, 5, source="openai")["text"] == "openai text"


@pytest.mark.asyncio
async def test_transcribe_voice_long_run_continues_in_background(
    monkeypatch, transcript_cache_dir
):
    """A transcription that outlasts the wait budget reports "running", keeps
    going after the call returns, caches its text, and a later call returns it
    without a second engine run."""
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE_WAIT_SECONDS", "0.05")
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={5: _voice_msg(id=5)})
    _patch_client(monkeypatch, client, entity, 1)
    release = asyncio.Event()
    calls = []

    async def slow_transcribe(*args, **kwargs):
        calls.append(1)
        await release.wait()
        return {"status": "ok", "text": "long one", "lang": "ru"}

    monkeypatch.setattr(transcription, "transcribe", slow_transcribe)

    first = json.loads(
        await messages.transcribe_voice(chat_id=1, message_id=5, engine="telegram", account=None)
    )
    assert first["transcribed"] is False
    assert first["reason"] == "running"
    second = json.loads(
        await messages.transcribe_voice(chat_id=1, message_id=5, engine="telegram", account=None)
    )
    assert second["reason"] == "running"
    assert transcription.get_cached_transcript(1, 5) is None

    release.set()
    for _ in range(100):
        if not transcription._BACKGROUND:
            break
        await asyncio.sleep(0.01)
    assert transcription.get_cached_transcript(1, 5)["text"] == "long one"

    third = json.loads(
        await messages.transcribe_voice(chat_id=1, message_id=5, engine="telegram", account=None)
    )
    assert third["transcribed"] is True
    assert third["text"] == "long one"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_transcribe_voice_late_result_is_pushed(monkeypatch, transcript_cache_dir):
    """With push/event available, a caller told "running" needn't poll: the
    finished transcript is pushed once, with the message id in it."""
    from telegram_mcp.mcpl import push

    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE_WAIT_SECONDS", "0.05")
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={8: _voice_msg(id=8)})
    _patch_client(monkeypatch, client, entity, 1)
    release = asyncio.Event()
    pushed = []

    async def slow_transcribe(*args, **kwargs):
        await release.wait()
        return {"status": "ok", "text": "довга розповідь", "lang": "uk"}

    async def fake_push(event_id, text, origin):
        pushed.append((event_id, text, origin))
        return True

    monkeypatch.setattr(transcription, "transcribe", slow_transcribe)
    monkeypatch.setattr(push, "available", lambda: True)
    monkeypatch.setattr(push, "push_text", fake_push)

    first = json.loads(
        await messages.transcribe_voice(chat_id=1, message_id=8, engine="telegram", account=None)
    )
    assert first["reason"] == "running"
    assert "no need to call again" in first["detail"]

    release.set()
    for _ in range(100):
        if pushed:
            break
        await asyncio.sleep(0.01)
    assert len(pushed) == 1
    event_id, text, origin = pushed[0]
    assert event_id.startswith("transcript:1:8:telegram:")
    assert "message_id 8" in text and "довга розповідь" in text
    assert "not a verbatim quote" in text
    assert origin["messageId"] == 8
    assert transcription.get_cached_transcript(1, 8)["text"] == "довга розповідь"


@pytest.mark.asyncio
async def test_transcribe_voice_late_failure_is_pushed(monkeypatch, transcript_cache_dir):
    from telegram_mcp.mcpl import push

    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE_WAIT_SECONDS", "0.05")
    entity = SimpleNamespace()
    client = _FakeClient(messages_by_id={9: _voice_msg(id=9)})
    _patch_client(monkeypatch, client, entity, 1)
    release = asyncio.Event()
    pushed = []

    async def slow_fail(*args, **kwargs):
        await release.wait()
        return {"status": "error", "error": "HTTP 500 from engine"}

    async def fake_push(event_id, text, origin):
        pushed.append(text)
        return True

    monkeypatch.setattr(transcription, "transcribe", slow_fail)
    monkeypatch.setattr(push, "available", lambda: True)
    monkeypatch.setattr(push, "push_text", fake_push)

    await messages.transcribe_voice(chat_id=1, message_id=9, engine="telegram", account=None)
    release.set()
    for _ in range(100):
        if pushed:
            break
        await asyncio.sleep(0.01)
    assert pushed and "failed: HTTP 500 from engine" in pushed[0]
    assert transcription.get_cached_transcript(1, 9) is None


@pytest.mark.asyncio
async def test_push_text_needs_grant():
    from telegram_mcp.mcpl import push
    from telegram_mcp.mcpl.policy import FS_USES, PolicyState

    class _T:
        def __init__(self):
            self.sent = []

        async def request(self, method, params):
            self.sent.append((method, params))
            return {"accepted": True}

    t = _T()
    policy = PolicyState()
    policy.apply({"effectiveCapabilities": list(FS_USES)}, is_request=True)
    push.bind(t, policy)
    try:
        assert await push.push_text("e1", "hi", {}) is False and not t.sent
        policy.apply({"effectiveCapabilities": list(FS_USES) + ["pushEvents"]}, is_request=True)
        assert await push.push_text("e2", "hi", {"kind": "transcript"}) is True
        method, params = t.sent[0]
        assert method == "push/event"
        assert params["featureSet"] == "telegram.transcripts"
        assert params["payload"]["content"] == [{"type": "text", "text": "hi"}]
    finally:
        push.bind(None, None)
