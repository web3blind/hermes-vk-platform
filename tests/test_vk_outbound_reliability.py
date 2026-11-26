"""Deterministic outbound retry, idempotency and pacing contracts."""
import asyncio
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_vk_adapter import VKAdapter, PlatformConfig
import plugins.platforms.vk.adapter as vk


@pytest.fixture
def adapter(monkeypatch):
    obj = VKAdapter(PlatformConfig(extra={}))
    obj.token = "test-placeholder"
    monkeypatch.setattr(obj, "_should_attach_project_keyboard", lambda _: False)
    return obj


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=100.0, sleeps=[])

    async def sleep(delay):
        state.sleeps.append(delay)
        state.now += delay
    monkeypatch.setattr(vk, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(vk, "asyncio", SimpleNamespace(sleep=sleep, Lock=asyncio.Lock, get_running_loop=asyncio.get_running_loop))
    monkeypatch.setattr(vk, "VK_TRANSIENT_RETRY_DELAY_SECONDS", 0)
    return state


def photo_transport(adapter, monkeypatch, errors=(), stage="server"):
    starts = []
    failures = iter(errors)
    def fail():
        error = next(failures, None)
        if error:
            raise RuntimeError(error)
    async def method(name, params):
        if name == "photos.getMessagesUploadServer":
            starts.append(vk.time.monotonic())
            if stage == "server":
                fail()
            return {"response": {"upload_url": "https://upload.invalid"}}
        if stage == "save":
            fail()
        return {"response": [{"owner_id": 1, "id": 2}]}
    async def upload(*args):
        if stage == "upload":
            fail()
        return {}
    monkeypatch.setattr(adapter, "_vk_method", method)
    monkeypatch.setattr(vk, "_multipart_upload_async", upload)
    return starts


@pytest.mark.parametrize("error", [
    "VK API error 100: photo is undefined",
    "VK API error 100: One of the parameters specified was missing or invalid: photo is undefined",
    "HTTP Error 500: Internal Server Error",
    "unknown error",
])
@pytest.mark.parametrize("stage", ["server", "upload", "save"])
def test_photo_retries_once_and_paces_retry(adapter, monkeypatch, clock, error, stage):
    starts = photo_transport(adapter, monkeypatch, [error], stage)
    assert asyncio.run(adapter._upload_photo_attachment("123456", "image.png")) == "photo1_2"
    assert starts == [100, 101.5]


@pytest.mark.parametrize("error", [
    "VK API error 5: authorization failed Internal Server Error",
    "VK API error 7: permission denied photo is undefined",
    "VK API error 15: access denied Internal Server Error",
    "VK API error 100: invalid parameter Internal Server Error",
    "missing upload_url Internal Server Error",
    "too large photo is undefined",
    "VK API error 5: User authorization failed",
    "VK API error 100: photo is undefined permission denied",
    "non-json response Internal Server Error",
    "missing owner_id/id Internal Server Error",
    "exceeded size limit Internal Server Error",
])
def test_photo_permanent_errors_win(adapter, monkeypatch, clock, error):
    starts = photo_transport(adapter, monkeypatch, [error])
    with pytest.raises(RuntimeError, match=error):
        asyncio.run(adapter._upload_photo_attachment("123456", "image.png"))
    assert len(starts) == 1


@pytest.mark.parametrize("error_type", [FileNotFoundError, PermissionError, IsADirectoryError, ValueError])
def test_photo_local_validation_with_transient_words_is_not_retried(adapter, monkeypatch, clock, error_type):
    method = AsyncMock(return_value={"response": {"upload_url": "https://upload.invalid"}})
    upload = AsyncMock(side_effect=error_type("Internal Server Error photo is undefined"))
    monkeypatch.setattr(adapter, "_vk_method", method)
    monkeypatch.setattr(vk, "_multipart_upload_async", upload)
    with pytest.raises(error_type):
        asyncio.run(adapter._upload_photo_attachment("123456", "image.png"))
    assert method.await_count == upload.await_count == 1


def test_photo_retry_is_bounded_and_failure_does_not_poison_pacing(adapter, monkeypatch, clock):
    error = "HTTP Error 500: Internal Server Error"
    # Photo uploads allow three retries (four attempts), so exhausting the
    # bound needs four queued failures before the give-up is observable.
    starts = photo_transport(adapter, monkeypatch, [error, error, error, error])
    async def scenario():
        with pytest.raises(RuntimeError, match=error):
            await adapter._upload_photo_attachment("123456", "bad.png")
        assert await adapter._upload_photo_attachment("123456", "good.png") == "photo1_2"
    asyncio.run(scenario())
    assert starts == [100, 101.5, 103, 104.5, 106]


@pytest.mark.parametrize("stage", ["server", "upload", "save"])
@pytest.mark.parametrize("errors", [
    ["HTTP Error 405: Not Allowed"],
    ["HTTP Error 500: Internal Server Error", "HTTP Error 405: Not Allowed"],
])
def test_photo_405_recovers_with_production_retry_delay(adapter, monkeypatch, clock, stage, errors):
    monkeypatch.setattr(vk, "VK_TRANSIENT_RETRY_DELAY_SECONDS", 5.0)
    starts = photo_transport(adapter, monkeypatch, errors, stage)
    assert asyncio.run(adapter._upload_photo_attachment("123456", "image.png")) == "photo1_2"
    assert starts == [100 + 5 * i for i in range(len(errors) + 1)]
    assert clock.sleeps == [5.0] * len(errors)


@pytest.mark.parametrize("error", [
    "VK API error 5: authorization failed HTTP Error 405",
    "VK API error 7: permission denied HTTP Error 405",
    "VK API error 15: access denied HTTP Error 405",
    "VK API error 100: invalid parameter HTTP Error 405",
    "missing upload_url HTTP Error 405",
    "missing owner_id/id HTTP Error 405",
    "non-json response HTTP Error 405",
    "exceeded size limit HTTP Error 405",
])
def test_photo_permanent_405_is_not_retried(adapter, monkeypatch, clock, error):
    starts = photo_transport(adapter, monkeypatch, [error])
    with pytest.raises(RuntimeError, match=error):
        asyncio.run(adapter._upload_photo_attachment("123456", "image.png"))
    assert starts == [100]
    assert clock.sleeps == []


def test_photo_405_exhaustion_propagates_and_recovers(adapter, monkeypatch, clock):
    error = "HTTP Error 405: Not Allowed"
    starts = photo_transport(adapter, monkeypatch, [error] * 4)
    async def scenario():
        with pytest.raises(RuntimeError, match=error):
            await adapter._upload_photo_attachment("123456", "bad.png")
        assert await adapter._upload_photo_attachment("123456", "good.png") == "photo1_2"
    asyncio.run(scenario())
    assert starts == [100, 101.5, 103, 104.5, 106]


@pytest.mark.parametrize("operation", ["text", "attachment", "doc", "video"])
def test_other_operations_do_not_gain_405_retries(adapter, monkeypatch, clock, operation):
    method = AsyncMock(side_effect=RuntimeError("HTTP Error 405: Not Allowed"))
    monkeypatch.setattr(adapter, "_vk_method", method)
    async def scenario():
        if operation == "text":
            assert not (await adapter.send("123456", "hello")).success
        elif operation == "attachment":
            assert not (await adapter._send_attachment("123456", "photo1_2")).success
        else:
            upload = adapter._upload_doc_attachment if operation == "doc" else adapter._upload_video_attachment
            with pytest.raises(RuntimeError, match="405"):
                await upload("123456", "file.bin")
    asyncio.run(scenario())
    assert method.await_count == 1
    assert clock.sleeps == []


def test_default_retry_bound_and_production_delay(monkeypatch):
    operation = AsyncMock(side_effect=RuntimeError("connection reset"))
    sleep = AsyncMock()
    monkeypatch.setattr(vk.asyncio, "sleep", sleep)
    monkeypatch.setattr(vk, "VK_TRANSIENT_RETRY_DELAY_SECONDS", 5.0)
    with pytest.raises(RuntimeError, match="connection reset"):
        asyncio.run(vk._retry_vk_transient_once("test", operation))
    assert operation.await_count == 2
    sleep.assert_awaited_once_with(5.0)


def test_generic_send_does_not_gain_photo_retry_triggers(adapter, monkeypatch, clock):
    for error in ["HTTP Error 500: Internal Server Error", "VK API error 100: photo is undefined"]:
        method = AsyncMock(side_effect=RuntimeError(error))
        monkeypatch.setattr(adapter, "_vk_method", method)
        assert not asyncio.run(adapter.send("123456", "hello")).success
        assert method.await_count == 1


@pytest.mark.parametrize("attachment", [False, True])
def test_stable_retry_ids_and_distinct_logical_sends(adapter, monkeypatch, clock, attachment):
    ids = itertools.count(100)
    monkeypatch.setattr(vk.random, "randint", lambda *_: next(ids))
    calls = []
    async def method(name, params):
        calls.append(dict(params))
        if len(calls) % 2:
            raise RuntimeError("connection reset")
        return {"response": [{"conversation_message_id": len(calls)}]}
    monkeypatch.setattr(adapter, "_vk_method", method)
    monkeypatch.setattr(adapter, "truncate_message", lambda *_: ["one", "two"])
    async def scenario():
        for _ in range(2):
            result = await (adapter._send_attachment("123456", "photo1_2") if attachment else adapter.send("123456", "hello"))
            assert result.success
    asyncio.run(scenario())
    logical_ids = []
    for first, retry in zip(calls[::2], calls[1::2]):
        assert first == retry
        logical_ids.append(first["random_id"])
    assert len(set(logical_ids)) == len(logical_ids)


def test_sequential_spacing_and_same_adapter_across_loops(adapter, monkeypatch, clock):
    starts = photo_transport(adapter, monkeypatch)
    async def pair():
        await adapter._upload_photo_attachment("123456", "a.png")
        await adapter._upload_photo_attachment("123456", "b.png")
    asyncio.run(pair())
    asyncio.run(pair())
    assert all(b - a >= 1.5 for a, b in (starts[:2], starts[2:]))


def test_concurrent_waiters_cancel_and_recover(adapter, monkeypatch, clock):
    starts = photo_transport(adapter, monkeypatch)
    async def scenario():
        entered = asyncio.Queue()
        releases = asyncio.Queue()
        async def sleep(delay):
            if delay > 0:
                target = clock.now + delay
                await entered.put(delay)
                await releases.get()
                clock.now = max(clock.now, target)
        monkeypatch.setattr(vk.asyncio, "sleep", sleep)
        await adapter._upload_photo_attachment("123456", "first.png")
        cancelled = asyncio.create_task(adapter._upload_photo_attachment("123456", "cancel.png"))
        assert await entered.get() > 0
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        holder = asyncio.create_task(adapter._upload_photo_attachment("123456", "holder.png"))
        assert await entered.get() > 0
        queued = asyncio.Event()
        async def waiting():
            queued.set()
            await adapter._upload_photo_attachment("123456", "queued.png")
        waiter = asyncio.create_task(waiting())
        await queued.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await releases.put(None)
        await holder
        tasks = [asyncio.create_task(adapter._upload_photo_attachment("123456", str(i))) for i in range(3)]
        for _ in tasks:
            assert await entered.get() > 0
            await releases.put(None)
        await asyncio.gather(*tasks)
    for _ in range(2):
        starts.clear()
        asyncio.run(asyncio.wait_for(scenario(), 3))
        assert len(starts) == 5
        assert all(b - a >= 1.5 for a, b in zip(starts, starts[1:]))
