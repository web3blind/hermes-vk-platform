"""Public callback API through real host imports and native tracked intake."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_vk_adapter as existing
from test_vk_adapter import callback_update

# Register shared fixtures explicitly instead of importing fixture functions.
pytest_plugins = ['test_vk_adapter']


async def dispatch(adapter, payload, **kwargs):
    update = callback_update(payload, **kwargs)
    await adapter._handle_update(update)
    tasks = list(adapter._intake_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
    await asyncio.sleep(0)
    assert not adapter._intake_tasks
    return update


def answers(calls):
    return [json.loads(p['event_data'])['text'] for m, p in calls
            if m == 'messages.sendMessageEventAnswer']


@pytest.mark.parametrize('namespace', ['', 'UPPER', 'white space', 'a.b', 'a' * 65, None, 123, [], 'a\n'])
def test_namespace_validation(callback_env, namespace):
    adapter, _, _ = callback_env
    with pytest.raises(ValueError):
        adapter.register_callback_handler(namespace, AsyncMock())


@pytest.mark.asyncio
async def test_two_plugins_identity_payload_ack_and_unload(callback_env):
    adapter, calls, _ = callback_env
    first = AsyncMock(return_value='First accepted')
    second = AsyncMock(return_value=None)
    unload = []
    ctx = SimpleNamespace(on_unload=unload.append)
    ctx.on_unload(adapter.register_callback_handler('plugin-one', first))
    ctx.on_unload(adapter.register_callback_handler('plugin_two', second))
    with pytest.raises(ValueError):
        adapter.register_callback_handler('plugin-one', second)
    with pytest.raises(TypeError):
        adapter.register_callback_handler('not-callable', None)
    payload = {'hermes_callback': 'plugin-one', 'data': {'nonce': 'actual', 'choice': 2}}
    update = callback_update(payload, user=101, cmid=78)
    update['object']['message_id'] = 998
    original = copy.deepcopy(update['object'])
    await adapter._handle_update(update)
    await asyncio.gather(*list(adapter._intake_tasks))
    event = first.await_args.args[0]
    assert {key: event[key] for key in original} == original
    assert event['data'] == payload['data']
    event['payload']['data']['nonce'] = 'mutated'
    assert update['object'] == original
    assert answers(calls) == ['First accepted']
    await dispatch(adapter, json.dumps({'hermes_callback': 'plugin_two', 'data': None}))
    second.assert_awaited_once()
    assert second.await_args.args[0]['payload'] == {'hermes_callback': 'plugin_two', 'data': None}
    # Old cleanup must not remove a newly registered handler with the same name.
    unload[0]()
    replacement = AsyncMock(return_value='Replacement')
    fresh_cleanup = adapter.register_callback_handler('plugin-one', replacement)
    unload[0]()
    await dispatch(adapter, payload)
    replacement.assert_awaited_once()
    fresh_cleanup()
    unload[1]()
    before = len(answers(calls))
    await dispatch(adapter, payload)
    assert len(answers(calls)) == before + 1
    assert answers(calls)[-1] == 'Действие закрыто или недоступно'
    assert not adapter._callback_handlers


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'hermes_callback': 'unknown', 'data': {}},
    {'hermes_callback': None, 'data': {}},
    {'hermes_callback': [], 'data': {}},
    {'hermes_callback': 'Bad', 'data': {}},
    {'hermes_callback': 'ok'},
    {'hermes_callback': 'ok', 'data': {}, 'vkpl': 'new'},
    {'hermes_callback': 'ok', 'data': {}, 'vkea': 'always'},
    {'hermes_callback': 'ok', 'data': {}, 'vksc': 'always'},
    {'hermes_callback': 'ok', 'data': {}, 'vkcq': '1'},
    {'hermes_callback': 'ok', 'data': float('nan')},
    {'hermes_callback': 'ok', 'data': 'x' * 4096},
    {'hermes_callback': 'ok', 'data': object()},
])
async def test_owned_invalid_envelopes_never_fall_through(callback_env, payload):
    adapter, calls, _ = callback_env
    handler = AsyncMock()
    adapter.register_callback_handler('ok', handler)
    adapter._handle_clarify_payload = AsyncMock(return_value=False)
    adapter._set_pending_create = AsyncMock()
    await dispatch(adapter, payload)
    handler.assert_not_awaited()
    adapter._handle_clarify_payload.assert_not_awaited()
    adapter._set_pending_create.assert_not_awaited()
    assert answers(calls) == ['Действие закрыто или недоступно']


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [{'user': 999}, {'peer': 2000000999}])
async def test_acl_precedes_dispatch(callback_env, kwargs):
    adapter, calls, _ = callback_env
    adapter.access_policy = 'peer_and_user'
    adapter.allowed_users = {'100'}
    handler = AsyncMock()
    adapter.register_callback_handler('ok', handler)
    await dispatch(adapter, {'hermes_callback': 'ok', 'data': {}}, **kwargs)
    handler.assert_not_awaited()
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['raise', 'timeout', 'cancel', 'invalid-result'])
async def test_failure_timeout_cancellation_sanitized_and_tracked(callback_env, caplog, mode):
    adapter, calls, _ = callback_env
    finished = asyncio.Event()
    async def handler(event):
        try:
            if mode == 'raise':
                raise RuntimeError('SECRET-MUST-NOT-LEAK')
            if mode == 'cancel':
                raise asyncio.CancelledError()
            if mode == 'invalid-result':
                return {'not': 'an ack'}
            await asyncio.Event().wait()
        finally:
            finished.set()
    adapter._callback_handler_timeout = 0.01
    adapter.register_callback_handler('ok', handler)
    await dispatch(adapter, {'hermes_callback': 'ok', 'data': {}}, cmid=999)
    assert finished.is_set()
    assert len(answers(calls)) == 1
    assert 'SECRET-MUST-NOT-LEAK' not in caplog.text + str(calls)
    assert answers(calls)[0] in {'Действие прервано', 'Не удалось обработать действие'}
    assert all(m == 'messages.sendMessageEventAnswer' for m, _ in calls)


@pytest.mark.asyncio
async def test_external_intake_cancellation_stops_handler(callback_env):
    adapter, calls, _ = callback_env
    started = asyncio.Event()
    finished = asyncio.Event()
    async def handler(event):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
    adapter.register_callback_handler('ok', handler)
    await adapter._handle_update(callback_update({'hermes_callback': 'ok', 'data': {}}))
    await started.wait()
    tasks = list(adapter._intake_tasks)
    assert len(tasks) == 1
    tasks[0].cancel()
    result = await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert isinstance(result[0], asyncio.CancelledError)
    assert finished.is_set() and not adapter._intake_tasks
    assert answers(calls) == ['Действие прервано']


@pytest.mark.asyncio
async def test_plugins_coexist_with_real_builtin_callbacks(callback_env):
    adapter, calls, _ = callback_env
    one, two = AsyncMock(), AsyncMock()
    adapter.register_callback_handler('one', one)
    adapter.register_callback_handler('two', two)
    await existing.test_vk_exec_approval_callback_resolves_and_edits_prompt(callback_env)
    await existing.test_vk_slash_confirm_callback_resolves_edits_and_sends_result(callback_env)
    await existing.test_vk_clarify_callback_resolves_choice_and_removes_buttons(callback_env)
    calls.clear()
    await dispatch(adapter, {'vkpl': 'new'})
    assert adapter._pending_create('2000000042', '100')
    await dispatch(adapter, {'vkpl': 'cancel'})
    assert not adapter._pending_create('2000000042', '100')
    one.assert_not_awaited()
    two.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_missing_message_ids_stay_missing_and_ack_is_bounded(callback_env):
    adapter, calls, _ = callback_env
    handler = AsyncMock(return_value='x' * 200)
    adapter.register_callback_handler('a' * 64, handler)
    update = callback_update({'hermes_callback': 'a' * 64, 'data': []})
    del update['object']['conversation_message_id']
    await adapter._handle_update(update)
    await asyncio.gather(*list(adapter._intake_tasks))
    event = handler.await_args.args[0]
    assert 'conversation_message_id' not in event
    assert 'message_id' not in event and 'cmid' not in event
    assert event['peer_id'] == 2000000042 and event['user_id'] == 100
    assert answers(calls) == ['x' * 90]


@pytest.mark.asyncio
async def test_callback_lane_uses_sent_message_not_active_lane(callback_env):
    adapter, _, _ = callback_env
    adapter.project_lanes = existing._normalize_project_lanes({'2000000042': {'lanes': [
        {'id': 'original', 'name': 'Original'}, {'id': 'current', 'name': 'Current'}]}})
    result = await adapter.send('2000000042', 'Plugin question', metadata={'thread_id': 'lane:original'})
    assert result.success and result.message_id == 'cmid:77'
    await adapter._set_active_lane_id('2000000042', '100', 'current')
    adapter._lane_state = existing._load_lane_state(adapter._lane_state_path)
    seen = []
    async def handler(event):
        seen.append(adapter._reply_lane(str(event['peer_id']), {'reply_message': {
            'conversation_message_id': event.get('conversation_message_id')}}))
    adapter.register_callback_handler('lane', handler)
    await dispatch(adapter, {'hermes_callback': 'lane', 'data': {}}, cmid=77)
    await dispatch(adapter, {'hermes_callback': 'lane', 'data': {}}, cmid=999)
    assert seen[0]['id'] == 'original'
    assert seen[1] is None
