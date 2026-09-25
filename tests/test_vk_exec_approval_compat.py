"""Exercise legacy and prompt-hook approval entry points with real VK callbacks."""
import asyncio
import json
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_vk_adapter import (
    VKAdapter, _ApprovalEntry, callback_env, clear_vk_env, callback_update,
    sent_callback,
)
from gateway.platforms.base import BasePlatformAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['legacy', 'modern'])
@pytest.mark.parametrize('flags,expected', [
    ({}, {'once', 'session', 'always', 'deny'}),
    ({'allow_permanent': False}, {'once', 'session', 'deny'}),
    ({'allow_session': False}, {'once', 'deny'}),
    ({'smart_denied': True}, {'once', 'deny'}),
])
async def test_both_approval_apis(callback_env, entrypoint, flags, expected):
    adapter, calls, approval = callback_env
    if entrypoint == 'modern' and not hasattr(BasePlatformAdapter, '_send_exec_approval_prompt'):
        pytest.skip('Host predates the prompt-hook API; legacy path is tested separately')
    entry = _ApprovalEntry({'command': 'touch demo', 'description': 'test'})
    approval._gateway_queues['s'] = [entry]
    sender = (adapter.send_exec_approval if entrypoint == 'legacy'
              else BasePlatformAdapter.send_exec_approval.__get__(adapter))
    result = await sender('2000000042', 'touch demo', 's', 'test', **flags)
    assert result.success
    params = next(p for m, p in calls if m == 'messages.send')
    keyboard = json.loads(params['keyboard'])
    assert keyboard['inline'] is True
    choices = {json.loads(b['action']['payload'])['vkea']
               for row in keyboard['buttons'] for b in row}
    assert choices == expected
    payload = sent_callback(calls)
    await adapter._handle_update(callback_update(payload, peer=2000000043))
    assert not entry.event.is_set()
    await adapter._handle_update(callback_update(payload))
    assert entry.event.is_set() and entry.result == 'once'


@pytest.mark.asyncio
async def test_modern_prompt_preserves_text_and_lane(callback_env):
    adapter, calls, approval = callback_env
    entry = _ApprovalEntry({'command': 'touch demo', 'description': 'test'})
    approval._gateway_queues['s'] = [entry]
    from unittest.mock import AsyncMock
    adapter._remember_message_lane = AsyncMock()
    prompt = SimpleNamespace(
        chat_id='2000000042', command='touch demo', session_key='s',
        description='test', metadata={'thread_id': 'project-a'},
        actions=[('Allow', 'once', ''), ('Deny', 'deny', '')],
        smart_denied=False, text='A custom host-provided approval notice',
    )
    result = await adapter._send_exec_approval_prompt(prompt)
    assert result.success
    assert calls[0][1]['message'] == prompt.text
    adapter._remember_message_lane.assert_awaited_once_with('2000000042', 'cmid:77', 'project-a')


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['legacy', 'modern'])
async def test_approval_failure_keeps_text_fallback_available(callback_env, entrypoint):
    adapter, calls, _ = callback_env
    if entrypoint == 'modern' and not hasattr(BasePlatformAdapter, '_send_exec_approval_prompt'):
        pytest.skip('Host predates the prompt-hook API')
    sender = (adapter.send_exec_approval if entrypoint == 'legacy'
              else BasePlatformAdapter.send_exec_approval.__get__(adapter))
    result = await sender('2000000042', 'touch demo', 'missing', 'test')
    assert not result.success and not calls and not adapter._approval_state


def test_gateway_dispatches_native_vk_approval(callback_env, monkeypatch):
    module = pytest.importorskip('gateway.run_turn_runner')
    if not hasattr(module, '_renders_exec_approval_buttons'):
        pytest.skip('Host predates explicit approval capability detection')
    adapter, calls, approval = callback_env
    assert module._renders_exec_approval_buttons(VKAdapter)
    entry = _ApprovalEntry({'command': 'touch demo', 'description': 'test'})
    approval._gateway_queues['s'] = [entry]
    ctx = SimpleNamespace(_status_adapter=adapter, _status_chat_id='2000000042',
                          session_key='s', _status_thread_metadata=None)
    runner = module.TurnRunner(Mock(), ctx)
    monkeypatch.setattr(runner, '_close_native_stream_boundary', lambda *_: None)
    # Only replace the thread-to-event-loop bridge, not dispatch/rendering.
    def schedule(coro, *_):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future
    monkeypatch.setattr(runner, '_schedule', schedule)
    monkeypatch.setattr('gateway.run_turn_runner_approval_settle.register_timeout_notice', lambda *_a, **_k: None)
    runner._approval_notify_sync({'command': 'touch demo', 'description': 'test'})
    sends = [p for m, p in calls if m == 'messages.send']
    assert len(sends) == 1 and 'keyboard' in sends[0]
    asyncio.run(adapter._handle_update(callback_update(sent_callback(calls))))
    assert entry.event.is_set() and entry.result == 'once'
