"""Offline invocation/security tests against the public plugin and real Hermes core."""
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_vk_adapter import (
    VKAdapter, PlatformConfig, build_session_key, intake_update, callback_update,
    drain_intake, clear_vk_env, callback_env,
)
from plugins.platforms.vk.adapter import _apply_yaml_config

NAME = r"(?<![\w@])(?:ИИЛада|курсор)\b[,:;!?-]?"
PEER = "2000000042"


@pytest.fixture
def gated(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    a = VKAdapter(PlatformConfig(enabled=True, extra={
        "group_id": "123", "allowed_peers": [PEER, "2000000043", "100"],
        "allowed_users_by_peer": {"2000000042": ["100"]},
        "require_mention": True, "mention_patterns": [NAME],
        "project_lanes": {PEER: {"lanes": [
            {"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}]}},
    }))
    a._vk_method = AsyncMock(return_value={"response": {}})
    a.handle_message = AsyncMock()
    return a


@pytest.mark.asyncio
@pytest.mark.parametrize("text,accept", [
    ("Курсор помоги", True), ("КУРСОР: помоги", True), ("эй, курсор!", True),
    ("(курсор), помоги", True), ("курсор-помоги", True),
    ("ИИЛада?", True), ("иилада", True), ("ИИЛАДА", True),
    ("ИИЛадааа", False), ("суперИИЛада", False), ("ИИЛада_тест", False),
    ("ИИЛада2", False), ("ИИЛадаё", False), ("ёкурсор", False),
    ("курсорный", False), ("суперкурсор", False), ("курсор_тест", False),
    ("@курсор", False), ("курсор2", False), ("обычная реплика", False),
    ("[club123|Курсор] помоги", True), ("@club123 помоги", True),
    ("@club1234 помоги", False), ("[club456|другой бот] помоги", False),
    ("/stop", True), ("/approve", False), ("/deny", False), ("/help", False),
    ("/new", False), ("/project new", False), ("/unknown", False),
    ("/help курсор", True),
])
async def test_raw_intake_name_gate(gated, text, accept):
    await gated._handle_update(intake_update(1, text))
    await drain_intake(gated)
    assert gated.handle_message.await_count == int(accept)
    if not accept:
        gated._vk_method.assert_not_awaited()
        assert not gated._seen_message_keys
        assert not gated._intake_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_from,accept", [(-123, True), (-456, False), (100, False), (None, False)])
async def test_reply_targets_only_this_community(gated, reply_from, accept):
    update = intake_update(1, "помоги")
    update["object"]["message"]["reply_message"] = {
        "from_id": reply_from, "conversation_message_id": 50, "text": "Курсор"}
    await gated._handle_update(update)
    await drain_intake(gated)
    assert gated.handle_message.await_count == int(accept)


@pytest.mark.asyncio
async def test_forwarded_name_and_media_caption_do_not_invoke(gated):
    update = intake_update(1, "", media=True)
    update["object"]["message"]["fwd_messages"] = [{"from_id": 100, "text": "Курсор помоги"}]
    gated._materialize_inbound_media = AsyncMock()
    await gated._handle_update(update)
    await drain_intake(gated)
    gated.handle_message.assert_not_awaited()
    gated._vk_method.assert_not_awaited()
    gated._materialize_inbound_media.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Курсор помоги", "ИИЛада помоги", "/stop", "/approve", "/deny", "/projects", "ответ"])
async def test_sender_acl_precedes_routing_enrichment_and_controls(gated, text):
    gated.build_source = Mock(side_effect=AssertionError("ACL bypass reached router"))
    gated._enrich_message_from_api = AsyncMock(side_effect=AssertionError("ACL bypass fetched"))
    gated._persist_lane_state = AsyncMock()
    before = copy.deepcopy(gated._lane_state)
    update = intake_update(1, text, user=222, media=True)
    update["object"]["message"].update(reply_message={"from_id": -123}, payload={"vkpl": "select", "id": "a"})
    await gated._handle_update(update)
    assert gated._lane_state == before
    gated.build_source.assert_not_called()
    gated._enrich_message_from_api.assert_not_awaited()
    gated._persist_lane_state.assert_not_awaited()
    gated._vk_method.assert_not_awaited()
    gated.handle_message.assert_not_awaited()
    assert not gated._seen_message_keys and not gated._intake_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"vkpl": "select", "id": "a"}, {"vkea": "once", "id": "test"},
    {"vksc": "once", "id": "test"}, {"vkcl": "0", "id": "test"}])
async def test_unauthorized_callbacks_have_no_ack_or_mutation(gated, payload):
    before = copy.deepcopy(gated._lane_state)
    await gated._handle_update(callback_update(payload, user=222))
    assert gated._lane_state == before
    gated._vk_method.assert_not_awaited()
    gated.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_groups_dm_and_default_off(gated):
    await gated._handle_update(intake_update(1, "обычно", peer=2000000043))
    await gated._handle_update(intake_update(2, "обычно", peer=100))
    await drain_intake(gated)
    assert [c.args[0].source.chat_id for c in gated.handle_message.await_args_list] == ["100"]
    # Only the course-like fixture peer has an owner ACL; others retain peer-only access.
    await gated._handle_update(intake_update(20, "ИИЛада помоги", peer=2000000043, user=222))
    reply = intake_update(21, "без имени", peer=2000000043, user=333)
    reply["object"]["message"]["reply_message"] = {"from_id": -123}
    await gated._handle_update(reply)
    await drain_intake(gated)
    assert [c.args[0].source.user_id for c in gated.handle_message.await_args_list] == ["100", "222", "333"]
    for setting in ({}, {"require_mention": False}, {"mention_patterns": [NAME]}):
        a = VKAdapter(PlatformConfig(enabled=True, extra={"allowed_peers": [PEER], **setting}))
        a._vk_method = AsyncMock(return_value={"response": {}})
        a.handle_message = AsyncMock()
        await a._handle_update(intake_update(1, "обычно"))
        await drain_intake(a)
        a.handle_message.assert_awaited_once()


@pytest.mark.parametrize("root", ["vk", "gateway", "platforms"])
def test_yaml_bridge_global_settings(root):
    values = {"require_mention": True, "mention_patterns": [NAME]}
    cfg = {"vk": values} if root == "vk" else ({"gateway": {"vk": values}} if root == "gateway" else {"platforms": {"vk": {"extra": values}}})
    pc = PlatformConfig(enabled=True)
    merged = _apply_yaml_config(cfg, pc)
    assert merged["require_mention"] is True
    assert merged["mention_patterns"] == [NAME]


@pytest.mark.asyncio
async def test_pending_clarify_is_exact_routed_session(gated, monkeypatch):
    from tools import clarify_gateway
    monkeypatch.setattr(clarify_gateway, "_entries", {})
    monkeypatch.setattr(clarify_gateway, "_session_index", {})
    await gated._set_active_lane_id(PEER, "100", "a")
    source = gated.build_source(chat_id=PEER, chat_type="thread", user_id="100", thread_id="lane:a")
    key = build_session_key(source)
    clarify_gateway.register("pending", key, "Question?", None)
    await gated._handle_update(intake_update(1, "ответ"))
    await drain_intake(gated)
    gated.handle_message.assert_awaited_once()
    assert gated.handle_message.await_args.args[0].source.thread_id == "lane:a"
    gated.handle_message.reset_mock()
    await gated._set_active_lane_id(PEER, "100", "b")
    await gated._handle_update(intake_update(2, "посторонний ответ"))
    await gated._handle_update(intake_update(3, "ответ", peer=2000000043))
    await drain_intake(gated)
    gated.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_intake_build_source_and_scoped_gateway(gated, monkeypatch, tmp_path):
    from gateway.run import GatewayRunner
    from gateway.profile_routing import parse_profile_routes
    from gateway.platforms.base import BasePlatformAdapter
    from hermes_constants import get_hermes_home
    import gateway.run as run
    home = tmp_path / "course-fixture"
    home.mkdir()
    monkeypatch.setattr(run, "_multiplex_profile_homes", lambda cfg: [("course-fixture", home)])
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=True, profile_routes=parse_profile_routes([
        dict(name="course-vk", platform="vk", chat_id=PEER, profile="course-fixture")]))
    # No session DB or model: stop AFTER the real scoped gateway dispatch boundary.
    observed = []
    async def sink(event):
        observed.append((event, get_hermes_home()))
        return None
    runner._handle_message = sink
    runner._resolve_profile_home_for_source = lambda source: home if source.profile else tmp_path
    gated.gateway_runner = runner
    gated.handle_message = BasePlatformAdapter.handle_message.__get__(gated)
    gated.set_message_handler(runner._make_default_profile_message_handler())
    gated.send_typing = AsyncMock()
    gated.on_message_start = AsyncMock()
    gated.on_message_complete = AsyncMock()
    await gated._handle_update(intake_update(1, "Курсор помоги"))
    await drain_intake(gated)
    if gated._background_tasks:
        await asyncio.gather(*list(gated._background_tasks))
    assert len(observed) == 1
    event, scope = observed[0]
    assert scope == home
    assert event.source.profile == "course-fixture"
    assert event.source._transport_adapter_ref() is gated
    assert event.source._authorization_profile_home == tmp_path
    assert event.source.platform.value == "vk" and event.source.user_id == "100"
    assert "course-fixture" in gated._event_session_key(event)
    # A real scoped clarify registry entry must match the source produced by
    # intake, not the unprofiled transport key or another lane/profile.
    from tools import clarify_gateway
    monkeypatch.setattr(clarify_gateway, "_entries", {})
    monkeypatch.setattr(clarify_gateway, "_session_index", {})
    entry = clarify_gateway.register("scoped-answer", gated._event_session_key(event), "Question?", None)
    await gated._handle_update(intake_update(10, "raw scoped answer"))
    await drain_intake(gated)
    if gated._background_tasks:
        await asyncio.gather(*list(gated._background_tasks))
    assert len(observed) == 2
    assert observed[-1][0].text == "raw scoped answer" and observed[-1][1] == home
    assert gated._event_session_key(observed[-1][0]) == entry.session_key
    clarify_gateway.resolve_gateway_clarify("scoped-answer", "raw scoped answer")
    assert entry.response == "raw scoped answer"
    await gated._handle_update(intake_update(2, "обычно"))
    await gated._handle_update(intake_update(3, "Курсор /stop", user=222))
    await drain_intake(gated)
    assert len(observed) == 2
    runner.config.profile_routes = parse_profile_routes([
        dict(name="tg", platform="telegram", chat_id=PEER, profile="course-fixture")])
    assert gated.build_source(chat_id=PEER, user_id="100").profile is None


@pytest.mark.asyncio
async def test_unaddressed_clarify_keeps_checked_route_and_raw_answer(gated, monkeypatch):
    from tools import clarify_gateway
    monkeypatch.setattr(clarify_gateway, "_entries", {})
    monkeypatch.setattr(clarify_gateway, "_session_index", {})
    await gated._set_active_lane_id(PEER, "100", "a")
    source = gated.build_source(chat_id=PEER, chat_type="thread", user_id="100", thread_id="lane:a")
    clarify_gateway.register("raw-answer", build_session_key(source), "Which?", ["Beta", "Other"])
    gated._persist_lane_state = AsyncMock()
    gated._enrich_message_from_api = AsyncMock()
    await gated._handle_update(intake_update(1, "Beta"))
    await drain_intake(gated)
    gated._enrich_message_from_api.assert_not_awaited()
    gated._persist_lane_state.assert_not_awaited()
    event = gated.handle_message.await_args.args[0]
    assert event.text == "Beta" and event.source.thread_id == "lane:a"
    assert gated._get_active_lane_id(PEER, "100") == "a"


@pytest.mark.asyncio
async def test_gated_malformed_text_payload_does_not_wake(gated):
    for i, payload in enumerate(({"vkpl": []}, {"vkpl": {}}, {"vkpl": "cmd"},
                                 {"vkpl": "select", "id": "missing"})):
        update = intake_update(i + 1, "обычно")
        update["object"]["message"]["payload"] = payload
        await gated._handle_update(update)
    await drain_intake(gated)
    gated.handle_message.assert_not_awaited()
    gated._vk_method.assert_not_awaited()


@pytest.mark.asyncio
async def test_denied_unaddressed_does_not_prune_expired_lane_state(gated):
    gated._lane_state["pending_edit"] = {
        gated._state_key(PEER, "100"): {"lane_id": "a", "expires_at": 0}}
    before = copy.deepcopy(gated._lane_state)
    await gated._handle_update(intake_update(1, "not addressed"))
    assert gated._lane_state == before
    gated._vk_method.assert_not_awaited()
    gated.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "test_exec_callback_binds_peer_prompt_choices_and_exact_queue_entry",
    "test_slash_callback_nonce_scope_stale_and_ack_before_handler",
    "test_clarify_callback_scope_invalid_index_and_text_button_prompt",
    "test_vk_clarify_other_callback_switches_to_text_capture",
])
async def test_enabled_gate_preserves_bound_controls(callback_env, case):
    import test_vk_adapter as baseline
    callback_env[0].require_mention = True
    await getattr(baseline, case)(callback_env)


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["once", "deny"])
async def test_enabled_gate_real_approval_waiters(callback_env, monkeypatch, choice):
    import test_vk_adapter as baseline
    callback_env[0].require_mention = True
    await baseline.test_exec_callback_unblocks_real_waiter(callback_env, monkeypatch, choice)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["/stop", "/approve", "/deny", "answer"])
async def test_enabled_gate_control_bypasses_blocked_media(gated, monkeypatch, control):
    if control in {"/approve", "/deny"}:
        from tools import approval
        from test_vk_adapter import _ApprovalEntry
        source = gated.build_source(chat_id=PEER, chat_type="group", user_id="100")
        monkeypatch.setattr(approval, "_gateway_queues", {gated._event_session_key(
            SimpleNamespace(source=source)): [_ApprovalEntry({"command": "test-only"})]})
    from tools import clarify_gateway
    monkeypatch.setattr(clarify_gateway, "_entries", {})
    monkeypatch.setattr(clarify_gateway, "_session_index", {})
    started, release = asyncio.Event(), asyncio.Event()
    async def materialize(message_type, media_urls, media_types):
        started.set()
        await release.wait()
        return media_urls
    gated._materialize_inbound_media = materialize
    await gated._handle_update(intake_update(1, "Курсор listen", media=True))
    try:
        await asyncio.wait_for(started.wait(), 1)
        if control == "answer":
            source = gated.build_source(chat_id=PEER, chat_type="group", user_id="100")
            clarify_gateway.register("blocked", build_session_key(source), "Question", None)
        await gated._handle_update(intake_update(2, control))
        assert gated.handle_message.await_count == 1
        assert gated.handle_message.await_args.args[0].text == control
    finally:
        release.set()
        await drain_intake(gated)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["/stop", "/approve", "/deny", "answer"])
async def test_real_base_busy_guard_only_accepts_controls(gated, monkeypatch, control):
    if control in {"/approve", "/deny"}:
        from tools import approval
        from test_vk_adapter import _ApprovalEntry
        source = gated.build_source(chat_id=PEER, chat_type="group", user_id="100")
        monkeypatch.setattr(approval, "_gateway_queues", {gated._event_session_key(
            SimpleNamespace(source=source)): [_ApprovalEntry({"command": "test-only"})]})
    from gateway.platforms.base import BasePlatformAdapter
    from tools import clarify_gateway
    monkeypatch.setattr(clarify_gateway, "_entries", {})
    monkeypatch.setattr(clarify_gateway, "_session_index", {})
    gated.handle_message = BasePlatformAdapter.handle_message.__get__(gated)
    reached = AsyncMock(return_value=None)
    gated.set_message_handler(reached)
    gated._heal_stale_session_lock = Mock()
    source = gated.build_source(chat_id=PEER, chat_type="group", user_id="100")
    key = build_session_key(source)
    gated._active_sessions[key] = asyncio.Event()
    try:
        await gated._handle_update(intake_update(1, "ordinary busy reply"))
        await drain_intake(gated)
        reached.assert_not_awaited()
        assert not gated._pending_messages
        if control == "answer":
            clarify_gateway.register("busy", key, "Question", None)
        await gated._handle_update(intake_update(2, control))
        await drain_intake(gated)
        reached.assert_awaited_once()
        assert reached.await_args.args[0].text == control
        assert not gated._pending_messages
    finally:
        gated._active_sessions.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("text,command", [("yes", "/approve"), ("no", "/deny"), ("approve session", "/approve session"),
    ("/approve", "/approve"), ("/deny", "/deny"), ("/approve session", "/approve session")])
async def test_pending_plaintext_approval_matches_exact_lane(gated, monkeypatch, text, command):
    from tools import approval
    from test_vk_adapter import _ApprovalEntry
    monkeypatch.setattr(approval, "_gateway_queues", {})
    await gated._set_active_lane_id(PEER, "100", "a")
    source = gated.build_source(chat_id=PEER, chat_type="thread", user_id="100", thread_id="lane:a")
    key = build_session_key(source)
    entry = _ApprovalEntry({"command": "test-only-never-executed"})
    approval._gateway_queues[key] = [entry]
    await gated._handle_update(intake_update(1, text, user=222))
    await gated._handle_update(intake_update(2, text, peer=2000000043))
    await gated._set_active_lane_id(PEER, "100", "b")
    await gated._handle_update(intake_update(3, text))
    await drain_intake(gated)
    gated.handle_message.assert_not_awaited()
    await gated._set_active_lane_id(PEER, "100", "a")
    await gated._handle_update(intake_update(4, text))
    await drain_intake(gated)
    gated.handle_message.assert_awaited_once()
    event = gated.handle_message.await_args.args[0]
    assert event.text == command and gated._event_session_key(event) == key
    assert not entry.event.is_set(), "adapter must not resolve the core approval itself"


@pytest.mark.asyncio
@pytest.mark.parametrize("peer,is_group", [(1999999999, False), (2000000000, True),
    (2000010000, True), (2100000000, True)])
async def test_numeric_group_boundary(gated, peer, is_group):
    gated.allowed_peers.add(str(peer))
    await gated._handle_update(intake_update(1, "ordinary", peer=peer))
    await drain_intake(gated)
    assert gated.handle_message.await_count == int(not is_group)
    if is_group:
        gated._vk_method.assert_not_awaited()
    gated.handle_message.reset_mock()
    await gated._handle_update(intake_update(2, "курсор помоги", peer=peer))
    await drain_intake(gated)
    assert gated.handle_message.await_args.args[0].source.chat_type == ("group" if is_group else "dm")


@pytest.mark.asyncio
async def test_pending_clarify_does_not_unlock_unaddressed_commands(gated, monkeypatch):
    from tools import clarify_gateway
    monkeypatch.setattr(clarify_gateway, "_entries", {})
    monkeypatch.setattr(clarify_gateway, "_session_index", {})
    source = gated.build_source(chat_id=PEER, chat_type="group", user_id="100")
    clarify_gateway.register("question", build_session_key(source), "Question", None)
    before = copy.deepcopy(gated._lane_state)
    for i, text in enumerate(("/help", "/new", "/project new", "/unknown", "/stopnow")):
        await gated._handle_update(intake_update(i + 1, text))
    await drain_intake(gated)
    gated.handle_message.assert_not_awaited()
    gated._vk_method.assert_not_awaited()
    assert gated._lane_state == before
    assert not gated._seen_message_keys


@pytest.mark.asyncio
async def test_non_control_slash_reply_to_bot_remains_available(gated):
    update = intake_update(1, "/help")
    update["object"]["message"]["reply_message"] = {"from_id": -123}
    await gated._handle_update(update)
    await drain_intake(gated)
    gated.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_cancel_text_button_preserved(gated):
    await gated._set_pending_create(PEER, "100")
    update = intake_update(1, "Отмена")
    update["object"]["message"]["payload"] = {"vkpl": "cancel"}
    await gated._handle_update(update)
    await drain_intake(gated)
    assert gated._pending_create(PEER, "100") is None
    gated.handle_message.assert_not_awaited()


@pytest.mark.parametrize("patterns", [["[", NAME], "not-a-list", [None, 42, ""]])
def test_invalid_patterns_fail_safely(patterns):
    adapter = VKAdapter(PlatformConfig(enabled=True, extra={
        "require_mention": True, "mention_patterns": patterns}))
    assert len(adapter._mention_patterns) == (1 if isinstance(patterns, list) and NAME in patterns else 0)
