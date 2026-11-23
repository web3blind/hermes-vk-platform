"""Inbound contracts: sticker attachments, download retry, reaction surfacing."""
import asyncio
from types import SimpleNamespace

import pytest

from test_vk_adapter import VKAdapter, PlatformConfig, _register_vk_platform_once
import plugins.platforms.vk.adapter as vk


@pytest.fixture(scope="module", autouse=True)
def vk_platform_registered():
    _register_vk_platform_once()


@pytest.fixture
def adapter():
    obj = VKAdapter(PlatformConfig(extra={}))
    obj.token = "test-placeholder"
    return obj


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=100.0, sleeps=[])

    async def sleep(delay):
        state.sleeps.append(delay)
        state.now += delay

    monkeypatch.setattr(vk, "asyncio", SimpleNamespace(sleep=sleep, Lock=asyncio.Lock, Semaphore=asyncio.Semaphore, get_running_loop=asyncio.get_running_loop))
    return state


# ── stickers ────────────────────────────────────────────────────────────────

def test_sticker_with_sticker_id_only_builds_cdn_url(adapter):
    kind, urls, media = adapter._extract_attachment_media(
        [{"type": "sticker", "sticker": {"product_id": 4707, "sticker_id": 99}}])
    assert kind == vk.MessageType.PHOTO
    assert urls == ["https://vk.ru/sticker/1-99-512b"]
    assert media == ["image/jpeg"]


def test_sticker_prefers_inline_photo_over_cdn(adapter):
    sticker = {"product_id": 4707, "sticker_id": 99,
               "photo": {"sizes": [{"url": "https://pic/s606.jpg", "width": 606}]}}
    kind, urls, media = adapter._extract_attachment_media([{"type": "sticker", "sticker": sticker}])
    assert kind == vk.MessageType.PHOTO
    assert urls == ["https://pic/s606.jpg"]


def test_sticker_picks_largest_inline_image(adapter):
    sticker = {"product_id": 4707, "sticker_id": 99, "images_with_background": [
        {"url": "https://pic/s160.png", "width": 160, "height": 160},
        {"url": "https://pic/s510.png", "width": 510, "height": 510},
    ]}
    kind, urls, _media = adapter._extract_attachment_media([{"type": "sticker", "sticker": sticker}])
    assert kind == vk.MessageType.PHOTO
    assert urls == ["https://pic/s510.png"]


# ── inbound download retry ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inbound_download_retries_once_after_transient_drop(adapter, monkeypatch, clock):
    calls = {"n": 0}

    async def flaky(url, media_type, max_bytes=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError("Remote end closed connection without response")
        return "/tmp/materialized.jpg"

    monkeypatch.setattr(vk, "_download_attachment_async", flaky)
    adapter.download_attachments = True
    out = await adapter._materialize_inbound_media(vk.MessageType.PHOTO, ["https://pic/a.jpg"], ["image/jpeg"])
    assert out == ["/tmp/materialized.jpg"]
    assert calls["n"] == 2
    assert clock.sleeps == [1]  # one bounded retry, one second apart


@pytest.mark.asyncio
async def test_inbound_download_exhaustion_falls_back_to_url(adapter, monkeypatch, clock):
    async def always_down(url, media_type, max_bytes=None):
        raise ConnectionResetError("Remote end closed connection")

    monkeypatch.setattr(vk, "_download_attachment_async", always_down)
    adapter.download_attachments = True
    out = await adapter._materialize_inbound_media(vk.MessageType.PHOTO, ["https://pic/a.jpg"], ["image/jpeg"])
    assert out == ["https://pic/a.jpg"]  # intake never breaks on a dead CDN
    assert clock.sleeps == [1]


# ── inbound reactions ───────────────────────────────────────────────────────

def reaction_adapter(monkeypatch, reads):
    obj = VKAdapter(PlatformConfig(extra={}))
    obj.token = "test-placeholder"
    events = []

    async def fake_method(name, params):
        del name, params
        reactions = reads.pop(0) if reads else reads[-1] if reads else None
        return {"response": {"items": [{"reactions": reactions}]}}

    async def capture(event):
        events.append(event)

    monkeypatch.setattr(obj, "_vk_method", fake_method)
    monkeypatch.setattr(obj, "_dispatch_inbound", capture)
    monkeypatch.setattr(obj, "_is_authorized", lambda **_kw: True)
    return obj, events


UPDATE = {"type": "message_reply", "object": {"peer_id": "8511807", "conversation_message_id": 123}}
USER_REACT = [{"reaction_id": 1, "count": 1, "user_ids": [8511807]}]
COMMUNITY_REACT = [{"reaction_id": 10, "count": 1, "user_ids": [-66539541]}]


@pytest.mark.asyncio
async def test_user_reaction_surfaces_once_with_emoji(monkeypatch):
    obj, events = reaction_adapter(monkeypatch, [USER_REACT])
    await obj._surface_reaction(UPDATE)
    await obj._surface_reaction(UPDATE)  # repeat poll: deduped
    assert len(events) == 1
    text = events[0].text
    assert "[VK reaction]" in text and "❤️" in text and "#123" in text


@pytest.mark.asyncio
async def test_reaction_read_waits_out_vk_index_lag(monkeypatch, clock):
    obj, events = reaction_adapter(monkeypatch, [None, None, USER_REACT])
    await obj._surface_reaction(UPDATE)
    assert clock.sleeps == [2, 5]  # escalating re-reads until the reaction appears
    assert len(events) == 1


@pytest.mark.asyncio
async def test_community_ack_reactions_stay_silent(monkeypatch):
    obj, events = reaction_adapter(monkeypatch, [COMMUNITY_REACT])
    await obj._surface_reaction(UPDATE)
    assert events == []


@pytest.mark.asyncio
async def test_unauthorized_reactor_is_dropped(monkeypatch):
    obj, events = reaction_adapter(monkeypatch, [USER_REACT])
    monkeypatch.setattr(obj, "_is_authorized", lambda **_kw: False)
    await obj._surface_reaction(UPDATE)
    assert events == []


@pytest.mark.asyncio
async def test_reaction_errors_never_escape(monkeypatch):
    obj, events = reaction_adapter(monkeypatch, [USER_REACT])

    async def boom(name, params):
        raise RuntimeError("api down")

    monkeypatch.setattr(obj, "_vk_method", boom)
    await obj._surface_reaction(UPDATE)  # must not raise
    assert events == []
