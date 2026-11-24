"""Inbound contracts: sticker attachments, download retry, reaction surfacing."""
import asyncio
from types import SimpleNamespace

import pytest

from test_vk_adapter import VKAdapter, PlatformConfig, _register_vk_platform_once, clear_vk_env
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

from unittest.mock import AsyncMock
from test_vk_adapter import drain_intake

UPDATE = {"type": "message_reaction_event", "object": {
    "peer_id": 2000000042, "cmid": 123, "reacted_id": 100, "reaction_id": 1}}


def reaction_adapter(monkeypatch):
    obj = VKAdapter(PlatformConfig(extra={"group_id": "42", "allowed_users": "100,101",
        "allowed_peers": "2000000042", "access_policy": "peer_and_user"}))
    obj.group_id = "42"
    obj.require_mention = True
    obj.allowed_users = {"100", "101"}
    obj.allowed_peers = {"2000000042"}
    obj.access_policy = "peer_and_user"
    obj._conversation_context_cache["2000000042"] = ("Chat", "Topic")
    events = []
    async def capture(event):
        events.append(event)
    obj.set_message_handler(capture)
    api = AsyncMock(return_value={"response": {"items": [{
        "from_id": -42, "peer_id": 2000000042, "conversation_message_id": 123}]}})
    monkeypatch.setattr(obj, "_vk_method", api)
    return obj, events, api


async def settle(obj):
    while obj._intake_tasks:
        await drain_intake(obj)
    for task in list(obj._background_tasks):
        await task


@pytest.mark.asyncio
async def test_documented_reaction_reaches_real_handler_once(monkeypatch):
    obj, events, api = reaction_adapter(monkeypatch)
    await obj._handle_update(UPDATE)
    await settle(obj)
    await obj._handle_update(UPDATE)
    await settle(obj)
    assert len(events) == 1
    assert "❤️" in events[0].text
    assert events[0].source.chat_type == "group"
    assert events[0].source.user_id == "100"
    assert not events[0].allow_gateway_control
    assert api.await_count == 1
    await obj.disconnect()

@pytest.mark.parametrize("photo,images,sticker_id,expected", [
    ({}, [{"url": "https://pic/a.png", "width": 5}], 99, "https://pic/a.png"),
    ({"sizes": [None, {"width": "bad"}]}, [None, {"url": "https://pic/b.png", "width": "bad"}], 99, "https://pic/b.png"),
    ({}, [], "../../escape", ""),
    ({}, [], True, ""),
])
def test_sticker_falls_through_malformed_candidates(adapter, photo, images, sticker_id, expected):
    assert adapter._attachment_url("sticker", {
        "photo": photo, "images": images, "sticker_id": sticker_id}) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("too large"), PermissionError("denied"),
    ValueError("invalid MIME")])
async def test_permanent_download_failure_is_not_retried(adapter, monkeypatch, error):
    download = AsyncMock(side_effect=error)
    monkeypatch.setattr(vk, "_download_attachment_async", download)
    adapter.download_attachments = True
    assert await adapter._materialize_inbound_media(vk.MessageType.PHOTO,
        ["https://pic/a.jpg"], ["image/jpeg"]) == ["https://pic/a.jpg"]
    assert download.await_count == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"reacted_id": 999}, {"reacted_id": -42}, {"peer_id": 2000000099},
    {"reaction_id": None}, {"reaction_id": 0}, {"reaction_id": 65},
    {"cmid": True}, {"reacted_id": "100"}])
async def test_invalid_or_denied_reaction_does_no_work(monkeypatch, change):
    obj, events, api = reaction_adapter(monkeypatch)
    await obj._handle_update({"type": UPDATE["type"], "object": {**UPDATE["object"], **change}})
    await settle(obj)
    assert not events and not obj._intake_tasks
    api.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", [100, -999])
async def test_reaction_on_human_or_other_community_is_not_invocation(monkeypatch, sender):
    obj, events, api = reaction_adapter(monkeypatch)
    api.return_value["response"]["items"][0]["from_id"] = sender
    await obj._handle_update(UPDATE)
    await settle(obj)
    assert not events


@pytest.mark.asyncio
async def test_only_documented_actor_not_api_reaction_list_is_forwarded(monkeypatch):
    obj, events, api = reaction_adapter(monkeypatch)
    api.return_value["response"]["items"][0]["reactions"] = [
        {"reaction_id": 4, "user_ids": [999, -42, 101]}]
    await obj._handle_update({"type": "message_reply", "object": UPDATE["object"]})
    api.assert_not_awaited()
    for actor in (100, 999, 101):
        await obj._handle_update({"type": UPDATE["type"], "object": {**UPDATE["object"], "reacted_id": actor}})
        await settle(obj)
    assert [e.source.user_id for e in events] == ["100", "101"]
    assert all("999" not in e.text and "-42" not in e.text for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("waiter", ["clarify", "confirm", "approval", "busy"])
async def test_feedback_never_consumes_native_waiter(monkeypatch, waiter):
    from tools import clarify_gateway, slash_confirm
    from gateway.session import build_session_key
    obj, events, api = reaction_adapter(monkeypatch)
    source = obj.build_source(chat_id="2000000042", chat_type="group", user_id="100")
    key = build_session_key(source)
    if waiter == "clarify":
        clarify_gateway.register("reaction-test", key, "Authorize payment?", None)
    elif waiter == "confirm":
        async def forbidden_confirm(_):
            events.append("BAD")
            return None
        slash_confirm.register(key, "reaction-test", "reset", forbidden_confirm)
    elif waiter == "approval":
        from tools import approval
        from test_vk_adapter import _ApprovalEntry
        entry = _ApprovalEntry({"command": "not-executed"})
        approval._gateway_queues[key] = [entry]
    else:
        obj._active_sessions[key] = asyncio.Event()
    try:
        await obj._handle_update(UPDATE)
        await settle(obj)
        assert not events
        if waiter == "clarify":
            assert clarify_gateway.has_pending(key)
        elif waiter == "confirm":
            assert slash_confirm.get_pending(key)
        elif waiter == "approval":
            assert approval._gateway_queues[key] == [entry]
            assert not entry.event.is_set()
    finally:
        clarify_gateway.clear_session(key)
        slash_confirm.clear(key)
        if waiter == "approval":
            approval._gateway_queues.pop(key, None)
        obj._active_sessions.pop(key, None)
        await obj.disconnect()


@pytest.mark.asyncio
async def test_original_lane_beats_active_lane_with_context(monkeypatch):
    obj, events, api = reaction_adapter(monkeypatch)
    obj.project_lanes = vk._normalize_project_lanes({"2000000042": {"default_skills": ["default"], "lanes": [
        {"id": "a", "name": "Alpha", "description": "Original", "skills": ["skill-a"]},
        {"id": "b", "name": "Beta"}]}})
    obj._lane_state["message_lanes"][obj._message_lane_key("2000000042", "123")] = "a"
    await obj._set_active_lane_id("2000000042", "100", "b")
    await obj._handle_update(UPDATE)
    await settle(obj)
    assert events[0].source.thread_id == "lane:a"
    assert events[0].source.chat_topic == "Alpha"
    assert events[0].auto_skill == ["default", "skill-a"]
    assert "Original" in events[0].channel_prompt
    assert await obj.reauthorize_native_roles(events[0].source)
    obj.allowed_users.clear()
    assert not await obj.reauthorize_native_roles(events[0].source)
    await obj.disconnect()


@pytest.mark.asyncio
async def test_reaction_flood_is_coalesced_and_disconnect_joins(monkeypatch):
    obj, events, api = reaction_adapter(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    async def blocked(name, params):
        if name != "messages.getByConversationMessageId" or str(params.get("conversation_message_ids")) == "999":
            return {"response": {"items": []}}
        started.set()
        await release.wait()
        return api.return_value
    api.side_effect = blocked
    for _ in range(100):
        await obj._handle_update(UPDATE)
    await started.wait()
    assert len(obj._intake_tasks) == 1
    for cmid in range(124, 224):
        await obj._handle_update({"type": UPDATE["type"], "object": {**UPDATE["object"], "cmid": cmid}})
    assert len(obj._reaction_pending) == 16
    assert len(obj._intake_tasks) == 16
    control_seen = asyncio.Event()
    async def control_handler(event):
        assert event.get_command() == "stop"
        control_seen.set()
    obj.set_message_handler(control_handler)
    await obj._handle_update({"type": "message_new", "object": {"message": {
        "from_id": 100, "peer_id": 2000000042, "conversation_message_id": 999, "text": "/stop"}}})
    await asyncio.wait_for(control_seen.wait(), 2)
    await obj.disconnect()
    assert not obj._reaction_pending and not obj._intake_tasks and not obj._reaction_seen
    await obj._handle_update(UPDATE)
    assert not obj._intake_tasks and not events


@pytest.mark.asyncio
async def test_reaction_overflow_after_lookup_is_silent(monkeypatch):
    obj, events, api = reaction_adapter(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    notices = AsyncMock()
    monkeypatch.setattr(obj, "_send_project_text", notices)
    async def blocked(name, params):
        started.set()
        await release.wait()
        return api.return_value
    api.side_effect = blocked
    await obj._handle_update(UPDATE)
    await started.wait()
    hold = asyncio.Event()
    fillers = [asyncio.create_task(hold.wait()) for _ in range(vk.MAX_PENDING_INTAKE - 1)]
    for task in fillers:
        obj._track_intake(task, "fixture")
    assert len(obj._intake_tasks) == vk.MAX_PENDING_INTAKE
    release.set()
    reaction_task = next(task for task in obj._intake_tasks if task not in fillers)
    await reaction_task
    await asyncio.sleep(0)
    assert not events and not obj._overflow_notices
    notices.assert_not_awaited()
    await obj.disconnect()


@pytest.mark.asyncio
async def test_cache_has_ttl_size_bound_and_removal_does_not_readd(monkeypatch):
    obj, events, api = reaction_adapter(monkeypatch)
    obj._reaction_seen = {("peer", str(i), "100", 1): float("inf") for i in range(1024)}
    await obj._handle_update(UPDATE)
    await settle(obj)
    assert len(obj._reaction_seen) == 1024
    await obj._handle_update({"type": UPDATE["type"], "object": {**UPDATE["object"], "reaction_id": None}})
    await obj._handle_update(UPDATE)
    await settle(obj)
    assert len(events) == 1
    key = ("2000000042", "123", "100", 1)
    obj._reaction_seen[key] = 0
    await obj._handle_update(UPDATE)
    await settle(obj)
    assert len(events) == 2
    await obj.disconnect()

@pytest.mark.asyncio
async def test_reaction_profile_route_rejection_is_fail_closed(monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.profile_routing import parse_profile_routes
    obj, events, api = reaction_adapter(monkeypatch)
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=True, profile_routes=parse_profile_routes([
        dict(name="missing-vk", platform="vk", chat_id="2000000042", profile="missing-reaction-fixture")]))
    obj.gateway_runner = runner
    source = obj.build_source(chat_id="2000000042", chat_type="group", user_id="100")
    assert source.profile_route_rejected
    await obj._handle_update(UPDATE)
    await settle(obj)
    assert not events
    await obj.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [(401, 1), (403, 1), (404, 1), (429, 2), (503, 2)])
async def test_download_http_retry_boundary(adapter, monkeypatch, clock, code, expected):
    import urllib.error
    download = AsyncMock(side_effect=urllib.error.HTTPError("https://pic/a.jpg", code, "failure", {}, None))
    monkeypatch.setattr(vk, "_download_attachment_async", download)
    adapter.download_attachments = True
    assert await adapter._materialize_inbound_media(vk.MessageType.PHOTO,
        ["https://pic/a.jpg"], ["image/jpeg"]) == ["https://pic/a.jpg"]
    assert download.await_count == expected
