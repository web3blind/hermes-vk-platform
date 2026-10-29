"""Fallback must never bootstrap by replaying historical commands (sparse CMIDs)."""
from unittest.mock import AsyncMock

import pytest

from test_vk_adapter import clear_vk_env, VKAdapter  # noqa: F401
from gateway.config import PlatformConfig

PEER = "2000000042"


def conversation(cmid=500, peer=PEER):
    return {"response": {"items": [{
        "peer": {"id": int(peer)},
        "last_conversation_message_id": cmid,
    }]}}


@pytest.fixture
def adapter():
    instance = VKAdapter(PlatformConfig(enabled=True, extra={
        "group_id": "123456789", "allowed_peers": [PEER],
    }))
    instance._handle_update = AsyncMock()
    return instance


@pytest.mark.asyncio
async def test_sparse_history_bootstraps_at_authoritative_tail_without_replay(adapter):
    adapter._vk_method = AsyncMock(return_value=conversation())

    async def sparse_items(peer, ids):
        # A deleted/invisible power-of-two message used to produce cursor 127.
        return [{"peer_id": int(peer), "conversation_message_id": cmid,
                 "from_id": 42, "date": 1, "text": "/new"}
                for cmid in ids if 1 <= cmid <= 500 and cmid != 128]

    adapter._fetch_cmid_items = AsyncMock(side_effect=sparse_items)
    assert await adapter._fallback_poll_once() == 0
    assert adapter._fallback_last_cmid[PEER] == 500
    adapter._handle_update.assert_not_awaited()
    adapter._vk_method.assert_awaited_once_with(
        "messages.getConversationsById", {"peer_ids": PEER, "extended": 0})
    adapter._fetch_cmid_items.assert_awaited_once()
    assert min(adapter._fetch_cmid_items.await_args.args[1]) == 501


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"error": {"error_code": 15}}, {}, {"response": {"items": []}},
    conversation(peer="2000000099"), conversation(cmid=None),
    conversation(cmid=-1), conversation(cmid=True), conversation(cmid="500"),
    RuntimeError("API unavailable"),
])
async def test_unavailable_tail_never_polls_from_zero_and_retries(adapter, payload):
    adapter._vk_method = AsyncMock(side_effect=[payload, conversation(700)])
    adapter._fetch_cmid_items = AsyncMock(return_value=[])
    assert await adapter._fallback_poll_once() == 0
    assert PEER not in adapter._fallback_last_cmid
    adapter._fetch_cmid_items.assert_not_awaited()
    adapter._handle_update.assert_not_awaited()

    assert await adapter._fallback_poll_once() == 0
    assert adapter._fallback_last_cmid[PEER] == 700
    adapter._fetch_cmid_items.assert_awaited_once()
    assert min(adapter._fetch_cmid_items.await_args.args[1]) == 701


@pytest.mark.asyncio
@pytest.mark.parametrize("date", [1, None, "invalid", True])
async def test_old_or_undated_commands_cannot_cross_startup_boundary(adapter, date):
    adapter._fallback_started_at = 1000
    adapter._fallback_last_cmid[PEER] = 500
    adapter._fetch_cmid_items = AsyncMock(return_value=[{
        "peer_id": int(PEER), "conversation_message_id": 501,
        "from_id": 42, "text": "/new", "date": date,
    }])
    assert await adapter._fallback_poll_once() == 0
    adapter._handle_update.assert_not_awaited()
    # Advance past rejected history rather than scanning the same batch forever.
    assert adapter._fallback_last_cmid[PEER] == 501


@pytest.mark.asyncio
async def test_fresh_messages_keep_order_skip_bot_echoes_and_longpoll_duplicates(adapter):
    adapter._fallback_started_at = 1000
    adapter._fallback_last_cmid[PEER] = 500
    adapter._fetch_cmid_items = AsyncMock(return_value=[{
        "peer_id": int(PEER), "conversation_message_id": cmid,
        "from_id": author, "text": "fresh", "date": 1000,
    } for cmid, author in [(504, 42), (503, -123456789), (502, 42), (501, 42)]])
    adapter._has_recent_message_key = lambda **kw: kw["conversation_message_id"] == 502
    assert await adapter._fallback_poll_once() == 2
    assert [call.args[0]["object"]["message"]["conversation_message_id"]
            for call in adapter._handle_update.await_args_list] == [501, 504]
    assert adapter._fallback_last_cmid[PEER] == 504


def test_fallback_boundary_is_captured_at_adapter_start(monkeypatch):
    monkeypatch.setattr("plugins.platforms.vk.adapter.time.time", lambda: 1000.9)
    instance = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"}))
    assert instance._fallback_started_at == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [False, True], ids=["text", "voice"])
async def test_fallback_dispatches_only_fresh_media_through_real_intake(voice):
    from test_vk_adapter import intake_update, drain_intake

    instance = VKAdapter(PlatformConfig(enabled=True, extra={
        "group_id": "123456789", "allowed_peers": [PEER],
        "require_mention": True, "require_mention_by_peer": {PEER: False},
    }))
    instance._fallback_started_at = 1000
    instance._fallback_last_cmid[PEER] = 500
    instance._vk_method = AsyncMock(return_value={"response": {}})
    instance.handle_message = AsyncMock()
    instance._materialize_inbound_media = AsyncMock(return_value=["/isolated/voice.ogg"])
    fresh = intake_update(502, "" if voice else "fresh text")["object"]["message"]
    fresh.update(peer_id=int(PEER), date=1000)
    if voice:
        fresh["attachments"] = [{"type": "audio_message", "audio_message": {
            "link_ogg": "https://example.com/voice.ogg", "duration": 3,
        }}]
    stale = dict(fresh, conversation_message_id=501, date=999, text="/new")
    instance._fetch_cmid_items = AsyncMock(return_value=[fresh, stale])

    assert await instance._fallback_poll_once() == 1
    await drain_intake(instance)
    instance.handle_message.assert_awaited_once()
    assert instance.handle_message.await_args is not None
    event = instance.handle_message.await_args.args[0]
    assert event.message_id == "502"
    assert event.source.chat_id == PEER
    if voice:
        assert event.message_type.value in {"voice", "audio"}
        assert event.media_urls == ["/isolated/voice.ogg"]
        instance._materialize_inbound_media.assert_awaited_once()
    else:
        assert event.text == "fresh text"
    assert instance._fallback_last_cmid[PEER] == 502
