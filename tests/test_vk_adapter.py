import importlib.util
import json
import os
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest


def _load_local_vk_plugin_module(module_name: str, file_name: str) -> None:
    local_root = Path(__file__).resolve().parents[1]
    module_path = local_root / file_name
    for package_name in ("plugins", "plugins.platforms", "plugins.platforms.vk"):
        if package_name not in sys.modules:
            package = types.ModuleType(package_name)
            package.__path__ = []  # type: ignore[attr-defined]
            sys.modules[package_name] = package
    sys.modules["plugins"].platforms = sys.modules["plugins.platforms"]
    sys.modules["plugins.platforms"].vk = sys.modules["plugins.platforms.vk"]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {module_name} from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    setattr(sys.modules["plugins.platforms.vk"], module_name.rsplit(".", 1)[-1], module)


_load_local_vk_plugin_module("plugins.platforms.vk.adapter", "adapter.py")
_load_local_vk_plugin_module("plugins.platforms.vk.setup_helper", "setup_helper.py")

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType, MessageEvent, ProcessingOutcome
from gateway.session import SessionSource, build_session_key
from plugins.platforms.vk.adapter import (
    VKAdapter,
    _download_attachment,
    _apply_yaml_config,
    _env_enablement,
    _format_lane_yaml_snippet,
    _is_retryable_vk_error,
    _is_safe_lane_id,
    _load_lane_state,
    _normalize_project_lanes,
    _parse_project_create_text,
    _parse_project_new_fallback,
    _parse_vk_target_ref,
    _looks_like_downloadable_attachment_url,
    _split_csv,
    _standalone_send,
    _upsert_lane_in_config,
    _vk_api_error_message,
)
from plugins.platforms.vk.setup_helper import import_project_lanes_to_vk


@pytest.fixture(autouse=True)
def clear_vk_env(monkeypatch):
    for key in (
        "VK_GROUP_TOKEN",
        "VK_USER_TOKEN",
        "VKBLOG_USER_TOKEN",
        "VK_GROUP_ID",
        "VK_ALLOWED_USERS",
        "VK_ALLOWED_PEERS",
        "VK_ALLOW_ALL_USERS",
        "VK_HOME_CHANNEL",
        "VK_DOWNLOAD_ATTACHMENTS",
        "VK_ACCESS_POLICY",
        "VK_ALLOWED_USERS_BY_PEER",
        "VK_DEDUPE_TTL_SECONDS",
        "VK_MAX_ATTACHMENT_BYTES",
        "VK_REACTIONS_ENABLED",
        "VK_REACTION_PROGRESS",
        "VK_REACTION_OK",
        "VK_REACTION_FAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    # VK project-lane creation/editing can persist to config by design. Tests
    # must never mutate the real ~/.hermes/config.yaml; individual tests that
    # assert config persistence override these fakes with their own captures.
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr("hermes_cli.config.save_config", lambda *_args, **_kwargs: None)


def test_split_csv_accepts_strings_and_iterables():
    assert _split_csv("1, 2,,3 ") == {"1", "2", "3"}
    assert _split_csv(["4", 5, ""]) == {"4", "5"}


def test_vk_api_error_912_includes_chat_bot_settings_hint():
    message = _vk_api_error_message(912, "This is a chat bot feature, change this status in settings")

    assert "VK API error 912" in message
    assert "chat-bot-only feature" in message
    assert "work in chats" in message
    assert "restart the Hermes gateway" in message


def test_vk_adapter_denies_by_default_and_allows_explicit_users_or_peers():
    cfg = PlatformConfig(
        enabled=True,
        extra={
            "group_id": "123456789",
            "allowed_users": ["100"],
            "allowed_peers": ["2000000001"],
        },
    )
    adapter = VKAdapter(cfg)

    assert adapter._is_authorized(from_id="100", peer_id="1") is True
    assert adapter._is_authorized(from_id="999", peer_id="2000000001") is True
    assert adapter._is_authorized(from_id="999", peer_id="1") is False


def test_vk_access_policy_peer_and_user_requires_both_allowlists():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "group_id": "123456789",
                "allowed_users": ["100"],
                "allowed_peers": ["2000000001"],
                "access_policy": "peer_and_user",
            },
        )
    )

    assert adapter._is_authorized(from_id="100", peer_id="2000000001") is True
    assert adapter._is_authorized(from_id="100", peer_id="2000000999") is False
    assert adapter._is_authorized(from_id="999", peer_id="2000000001") is False


def test_vk_allowed_users_by_peer_limits_specific_chat_members():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000001", "2000000002"],
                "allowed_users_by_peer": {"2000000001": ["100", "101"]},
            },
        )
    )

    assert adapter._is_authorized(from_id="100", peer_id="2000000001") is True
    assert adapter._is_authorized(from_id="999", peer_id="2000000001") is False
    assert adapter._is_authorized(from_id="999", peer_id="2000000002") is True


def test_vk_unknown_access_policy_fails_closed():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "group_id": "123456789",
                "allowed_users": ["100"],
                "allowed_peers": ["2000000001"],
                "access_policy": "typo",
            },
        )
    )

    assert adapter._is_authorized(from_id="100", peer_id="2000000001") is False


def test_vk_session_keys_are_isolated_per_peer_and_user():
    platform = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"})).platform
    family = SessionSource(
        platform=platform,
        chat_id="2000000042",
        chat_type="group",
        user_id="123456",
    )
    personal = SessionSource(
        platform=platform,
        chat_id="2000000043",
        chat_type="group",
        user_id="123456",
    )
    another_user_same_chat = SessionSource(
        platform=platform,
        chat_id="2000000042",
        chat_type="group",
        user_id="42",
    )

    assert build_session_key(family) == "agent:main:vk:group:2000000042:123456"
    assert build_session_key(personal) == "agent:main:vk:group:2000000043:123456"
    assert build_session_key(another_user_same_chat) == "agent:main:vk:group:2000000042:42"
    assert len({build_session_key(family), build_session_key(personal), build_session_key(another_user_same_chat)}) == 3


def test_vk_peer_allowlist_is_honored_by_gateway_authz(monkeypatch):
    """VK owns peer allowlisting, so gateway auth must trust restricted intake."""
    for key in (
        "VK_ALLOWED_USERS",
        "VK_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)

    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={"group_id": "123456789", "allowed_peers": ["2000000044"]},
        )
    )

    try:
        from gateway.run import GatewayRunner
    except ModuleNotFoundError:
        # Standalone public-plugin CI installs only the narrow test deps, not the
        # full Hermes gateway dependency set. The adapter-level contract still
        # matters there: restricted intake must advertise that gateway auth can
        # trust this platform's own allowlist.
        assert adapter.enforces_own_access_policy is True
        return

    runner = cast(Any, object.__new__(GatewayRunner))
    platform = adapter.platform
    runner.config = SimpleNamespace(platforms={platform: PlatformConfig(enabled=True, extra={})})
    runner.adapters = {platform: adapter}
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_args, **_kwargs: False)

    source = SessionSource(
        platform=platform,
        user_id="2000000044",
        user_name="VK user 2000000044",
        chat_id="2000000044",
        chat_name="VK peer 2000000044",
        chat_type="dm",
    )

    assert adapter._is_authorized(from_id="2000000044", peer_id="2000000044") is True
    assert runner._is_user_authorized(source) is True


def test_env_enablement_requires_token_and_group_id_without_leaking_token():
    with patch.dict(os.environ, {}, clear=True):
        assert _env_enablement() is None

    with patch.dict(
        os.environ,
        {
            "VK_GROUP_TOKEN": "example-token-value",
            "VK_GROUP_ID": "-123456789",
            "VK_ALLOWED_USERS": "100,200",
            "VK_ALLOWED_PEERS": "2000000001",
            "VK_HOME_CHANNEL": "2000000001",
        },
        clear=True,
    ):
        data = _env_enablement()

    assert data == {
        "group_id": "123456789",
        "allowed_users": ["100", "200"],
        "allowed_peers": ["2000000001"],
        "allow_all_users": False,
        "home_channel": "2000000001",
    }
    assert "example-token-value" not in repr(data)


def test_yaml_config_bridges_vk_channel_prompts_and_skill_bindings():
    platform_cfg = SimpleNamespace(token=None)

    with patch.dict(os.environ, {}, clear=True):
        extra = _apply_yaml_config(
            {
                "vk": {
                    "allowed_peers": ["2000000042"],
                    "channel_prompts": {"2000000042": "family prompt"},
                    "channel_skill_bindings": [{"id": "2000000042", "skills": ["perplex"]}],
                }
            },
            platform_cfg,
        )
        assert os.environ["VK_ALLOWED_PEERS"] == "2000000042"

    assert extra == {
        "allowed_peers": ["2000000042"],
        "channel_prompts": {"2000000042": "family prompt"},
        "channel_skill_bindings": [{"id": "2000000042", "skills": ["perplex"]}],
    }


def test_vk_apply_yaml_config_preserves_canonical_reaction_settings(monkeypatch):
    extra = _apply_yaml_config(
        {
            "platforms": {
                "vk": {
                    "extra": {
                        "reactions_enabled": True,
                        "reaction_progress": 10,
                        "reaction_ok": 4,
                        "reaction_fail": 0,
                    }
                }
            }
        },
        SimpleNamespace(token=None),
    )

    assert extra is not None
    assert extra["reactions_enabled"] is True
    assert extra["reaction_progress"] == 10
    assert extra["reaction_ok"] == 4
    assert extra["reaction_fail"] == 0


def test_project_lanes_config_is_normalized_and_bridged(monkeypatch):
    platform_cfg = SimpleNamespace(token=None)

    extra = _apply_yaml_config(
        {
            "vk": {
                "project_lanes": {
                    2000000042: {
                        "base_folder": "ai-projects",
                        "default_skill": "coding",
                        "lanes": [
                            {
                                "id": "tccc-ai",
                                "name": "TCCC AI\nInjected",
                                "description": "ИИ-аудит web3 проектов",
                                "folder": "tccc-ai",
                                "workdir": "/home/assistent/ai-projects/tccc-ai",
                                "skills": ["tccc-ai-product-development", "coding", "coding"],
                                "aliases": ["tccc", "аудит"],
                            },
                            {"id": "bad/id", "name": "Bad"},
                        ],
                    }
                }
            }
        },
        platform_cfg,
    )

    lanes = extra["project_lanes"]
    assert set(lanes) == {"2000000042"}
    peer = lanes["2000000042"]
    assert peer["default_skills"] == ["coding"]
    assert peer["lanes"][0]["id"] == "tccc-ai"
    assert peer["lanes"][0]["name"] == "TCCC AI Injected"
    assert peer["lanes"][0]["skills"] == ["tccc-ai-product-development", "coding"]
    assert peer["alias_to_id"]["tccc"] == "tccc-ai"
    assert "bad/id" not in peer["lane_by_id"]


def test_canonical_platform_project_lanes_config_is_bridged():
    platform_cfg = SimpleNamespace(token=None)

    extra = _apply_yaml_config(
        {
            "platforms": {
                "vk": {
                    "extra": {
                        "project_lanes": {
                            "enabled": True,
                            "chats": {"2000000042": {"lanes": [{"id": "vk-plugin", "name": "VK Plugin"}]}},
                        }
                    }
                }
            }
        },
        platform_cfg,
    )

    assert extra is not None
    assert extra["project_lanes"]["2000000042"]["lanes"][0]["id"] == "vk-plugin"


def test_upsert_lane_in_config_writes_canonical_project_lanes():
    cfg = {"vk": {"project_lanes": {"legacy": {"lanes": []}}}}
    _upsert_lane_in_config(
        cfg,
        "2000000042",
        {"id": "gito", "name": "Gito", "workdir": "/tmp/gito", "skills": ["coding"], "aliases": ["gito"]},
    )

    lanes = cast(dict[str, Any], cfg["platforms"])["vk"]["extra"]["project_lanes"]["chats"]["2000000042"]["lanes"]
    assert lanes == [{"id": "gito", "name": "Gito", "workdir": "/tmp/gito", "skills": ["coding"], "aliases": ["gito"]}]


@pytest.mark.asyncio
async def test_vk_fallback_cmid_poll_handles_new_user_messages(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(enabled=True, extra={"group_id": "123456789", "allowed_peers": ["2000000009"]})
    )
    adapter._fallback_last_cmid["2000000009"] = 184
    adapter._fetch_cmid_items = AsyncMock(
        return_value=[
            {"from_id": -123456789, "peer_id": 2000000009, "conversation_message_id": 185, "text": "bot echo"},
            {"from_id": 103088086, "peer_id": 2000000009, "conversation_message_id": 186, "text": "user text"},
        ]
    )
    adapter.handle_message = AsyncMock()

    handled = await adapter._fallback_poll_once()

    assert handled == 1
    adapter.handle_message.assert_awaited_once()
    await_args = adapter.handle_message.await_args
    assert await_args is not None
    event = await_args.args[0]
    assert event.source.chat_id == "2000000009"
    assert event.message_id == "186"
    assert event.text == "user text"


@pytest.mark.asyncio
async def test_project_new_persists_lane_to_canonical_config(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    saved = {}

    def fake_load_config():
        return {}

    def fake_save_config(cfg, **_kwargs):
        saved.update(cfg)

    monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)
    monkeypatch.setattr("hermes_cli.config.save_config", fake_save_config)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["2000000042"]}))

    assert await adapter._add_custom_lane("2000000042", {"id": "gito", "name": "Gito", "skills": ["coding"]}) is True

    lanes = saved["platforms"]["vk"]["extra"]["project_lanes"]["chats"]["2000000042"]["lanes"]
    assert lanes[0]["id"] == "gito"
    resolved = adapter._resolve_lane("2000000042", "gito")
    assert resolved is not None
    assert resolved["name"] == "Gito"


def test_vk_lanes_importer_promotes_legacy_and_telegram_topics(monkeypatch):
    cfg = {
        "vk": {"project_lanes": {"2000000001": {"lanes": [{"id": "legacy", "name": "Legacy", "skills": ["coding"]}]}}},
        "platforms": {
            "telegram": {
                "extra": {
                    "group_topics": [
                        {"chat_id": "-1001234567890", "topics": [{"id": "tg-topic", "name": "TG Topic", "thread_id": 42, "workdir": "/tmp/tg"}]}
                    ],
                    "dm_topics": [
                        {"chat_id": "-100", "topics": [{"id": "dm-topic", "name": "DM Topic", "thread_id": 43}]}
                    ]
                }
            }
        },
    }
    saved = {}

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.save_config", lambda new_cfg, **_kwargs: saved.update(new_cfg))

    report = import_project_lanes_to_vk("2000000042", sources=["legacy", "telegram"], telegram_chat_id="-1001234567890")

    lanes = saved["platforms"]["vk"]["extra"]["project_lanes"]["chats"]["2000000042"]["lanes"]
    ids = {lane["id"] for lane in lanes}
    assert report["imported"] == 2
    assert ids == {"legacy", "tg-topic"}


def test_project_lane_id_validation_rejects_session_and_path_unsafe_values():
    assert _is_safe_lane_id("tccc-ai") is True
    for value in ("", "bad/id", "bad:id", "bad id", "../bad", "bad\\id"):
        assert _is_safe_lane_id(value) is False


def test_vk_target_parser_accepts_project_lane_thread_target():
    assert _parse_vk_target_ref("2000000042") == ("2000000042", None)
    assert _parse_vk_target_ref("2000000042:lane:topic-42") == ("2000000042", "lane:topic-42")
    assert _parse_vk_target_ref("2000000042:lane:bad/id") is None


def test_corrupt_project_lane_state_fails_open(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")

    assert _load_lane_state(path) == {"active": {}, "pending_create": {}, "pending_edit": {}, "custom_lanes": {}, "project_list_messages": {}, "project_list_pages": {}, "message_lanes": {}, "pinned": {}}


def test_project_new_fast_fallback_parses_name_skills_and_context():
    lane = _parse_project_new_fallback("Rialo blockchain-project-research,coding ретродроп и аналитика")

    assert lane == {
        "id": "rialo",
        "name": "Rialo",
        "description": "ретродроп и аналитика",
        "folder": "rialo",
        "skills": ["blockchain-project-research", "coding"],
    }
    snippet = _format_lane_yaml_snippet(lane)
    assert "id: rialo" in snippet
    assert "- blockchain-project-research" in snippet


def test_project_create_text_parses_labeled_and_freeform_inputs():
    labeled = _parse_project_create_text(
        "Название: Rialo\nНазначение: ретродроп и аналитика\nПапка: rialo\nСкиллы: blockchain-project-research, coding"
    )
    assert labeled is not None
    assert labeled["id"] == "rialo"
    assert labeled["skills"] == ["blockchain-project-research", "coding"]

    freeform = _parse_project_create_text(
        "Создай проект Rialo для ретродропа и аналитики. Папка rialo. Скиллы blockchain-project-research, coding."
    )
    assert freeform is not None
    assert freeform["id"] == "rialo"
    assert "blockchain-project-research" in freeform["skills"]


def test_vk_attachment_extraction_covers_required_inbound_formats():
    adapter = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"}))
    attachments = [
        {"type": "photo", "photo": {"sizes": [{"width": 10, "height": 10, "url": "small"}, {"width": 20, "height": 20, "url": "big"}]}},
        {"type": "video", "video": {"player": "https://vkvideo.example/video"}},
        {
            "type": "video",
            "video": {
                "player": "https://vkvideo.example/watch-page",
                "files": {"mp4_360": "https://psv4.userapi.com/native-video-360.mp4"},
            },
        },
        {"type": "video", "video": {"owner_id": -1, "id": 2, "access_key": "secret"}},
        {"type": "audio", "audio": {"url": "https://vk.example/audio.mp3"}},
        {"type": "audio_message", "audio_message": {"link_ogg": "https://vk.example/voice.ogg"}},
        {"type": "video_message", "video_message": {"link_mp4": "https://vk.example/round.mp4"}},
        {"type": "doc", "doc": {"url": "https://vk.example/file.pdf", "ext": "pdf"}},
    ]

    message_type, urls, media_types = adapter._extract_attachment_media(attachments)  # type: ignore[attr-defined]

    assert message_type is MessageType.PHOTO
    assert urls == [
        "big",
        "https://psv4.userapi.com/native-video-360.mp4",
        "https://vk.example/audio.mp3",
        "https://vk.example/voice.ogg",
        "https://vk.example/round.mp4",
        "https://vk.example/file.pdf",
    ]
    assert media_types == [
        "image/jpeg",
        "video/mp4",
        "audio/mpeg",
        "audio/ogg",
        "video/mp4",
        "application/pdf",
    ]


def test_vk_user_token_does_not_fall_back_to_vkblog_alias(monkeypatch):
    monkeypatch.setenv("VKBLOG_USER_TOKEN", "vkblog-token")

    adapter = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"}))

    assert adapter.user_token == ""


def test_vk_user_token_uses_explicit_gateway_env(monkeypatch):
    monkeypatch.setenv("VKBLOG_USER_TOKEN", "vkblog-token")
    monkeypatch.setenv("VK_USER_TOKEN", "gateway-user-token")

    adapter = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"}))

    assert adapter.user_token == "gateway-user-token"


def test_vk_video_without_direct_media_uses_best_preview_frame():
    adapter = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"}))
    attachments = [
        {
            "type": "video",
            "video": {
                "owner_id": 103,
                "id": 456,
                "is_from_message": 1,
                "first_frame": [
                    {"width": 160, "height": 90, "url": "https://sun9-1.userapi.com/small.jpg"},
                    {"width": 640, "height": 360, "url": "https://sun9-1.userapi.com/big.jpg"},
                ],
                "image": [{"width": 320, "height": 180, "url": "https://sun9-1.userapi.com/mid.jpg"}],
            },
        }
    ]

    message_type, urls, media_types = adapter._extract_attachment_media(attachments)  # type: ignore[attr-defined]

    assert message_type is MessageType.VIDEO
    assert urls == ["https://sun9-1.userapi.com/big.jpg"]
    assert media_types == ["image/jpeg"]


def test_vk_video_merge_preserves_original_preview_when_enrichment_has_no_file():
    original = {
        "owner_id": 103,
        "id": 456,
        "access_key": "ak",
        "first_frame": [{"width": 640, "height": 360, "url": "https://sun9-1.userapi.com/live.jpg"}],
    }
    enriched = {
        "owner_id": 103,
        "id": 456,
        "is_from_message": 1,
        "player": "https://vkvideo.example/watch",
        "first_frame": [{"width": 640, "height": 360, "url": "https://iv.okcdn.ru/expired.jpg"}],
    }

    merged = VKAdapter._merge_video_payload(original, enriched)

    assert merged["player"] == "https://vkvideo.example/watch"
    assert merged["is_from_message"] == 1
    assert merged["access_key"] == "ak"
    assert merged["first_frame"] == original["first_frame"]


@pytest.mark.asyncio
async def test_project_lane_active_selection_routes_message_as_thread(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {
                    "2000000042": {
                        "lanes": [
                            {
                                "id": "tccc-ai",
                                "name": "TCCC AI",
                                "description": "audit context",
                                "skills": ["tccc-ai-product-development", "coding"],
                                "aliases": ["tccc"],
                            }
                        ]
                    }
                },
            },
        )
    )
    adapter._vk_method = AsyncMock(return_value={"response": {"items": []}})
    adapter.handle_message = AsyncMock()
    await adapter._set_active_lane_id("2000000042", "100", "tccc-ai")

    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000042,
                    "conversation_message_id": 10,
                    "text": "сделай аудит",
                }
            },
        }
    )

    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "thread"
    assert event.source.thread_id == "lane:tccc-ai"
    assert event.source.chat_topic == "TCCC AI"
    assert event.auto_skill == ["tccc-ai-product-development", "coding"]
    assert "audit context" in event.channel_prompt
    assert build_session_key(event.source) == "agent:main:vk:thread:2000000042:lane:tccc-ai"


@pytest.mark.asyncio
async def test_project_alias_one_shot_routes_without_changing_active_lane(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {
                    "2000000042": {
                        "lanes": [{"id": "tccc-ai", "name": "TCCC AI", "aliases": ["tccc"]}]
                    }
                },
            },
        )
    )
    adapter._vk_method = AsyncMock(return_value={"response": {"items": []}})
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 11, "text": "@tccc задача"}},
        }
    )

    event = adapter.handle_message.await_args.args[0]
    assert event.text == "задача"
    assert event.source.thread_id == "lane:tccc-ai"


@pytest.mark.asyncio
async def test_vk_button_text_mentions_are_stripped_before_gateway_message(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "240237574",
                "allowed_peers": ["2000000042"],
                "project_lanes": {
                    "2000000042": {
                        "lanes": [
                            {
                                "id": "hermes-vk-plugin",
                                "name": "Hermes VK-плагин",
                                "aliases": ["vk-plugin"],
                                "workdir": "",
                                "skills": [],
                            }
                        ]
                    }
                },
            },
        )
    )
    adapter._vk_method = AsyncMock(return_value={"response": {"items": []}})
    adapter.handle_message = AsyncMock()
    await adapter._set_active_lane_id("2000000042", "100", "hermes-vk-plugin")

    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000042,
                    "conversation_message_id": 15,
                    "text": "[club240237574|@club240237574] 3",
                }
            },
        }
    )

    event = adapter.handle_message.await_args.args[0]
    assert event.text == "3"
    assert event.source.thread_id == "lane:hermes-vk-plugin"


@pytest.mark.asyncio
async def test_project_command_is_consumed_before_gateway_slash_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {"2000000042": {"lanes": [{"id": "tccc-ai", "name": "TCCC AI"}]}},
            },
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 12, "text": "/project list"}}}
    )

    adapter.handle_message.assert_not_awaited()
    send_calls = [call[1] for call in calls if call[0] == "messages.send"]
    assert any("keyboard" in params for params in send_calls)
    assert any("TCCC AI: /project tccc-ai" in params.get("message", "") for params in send_calls)
    assert any("Если кнопки видны" in params.get("message", "") for params in send_calls)


@pytest.mark.asyncio
async def test_project_menu_button_with_bot_mention_and_payload_shows_inline_project_list(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {"2000000042": {"lanes": [{"id": "tccc-ai", "name": "TCCC AI"}]}},
            },
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000042,
                    "conversation_message_id": 13,
                    "text": "@club123456789 Проекты",
                    "payload": json.dumps({"vkpl": "list"}),
                }
            },
        }
    )

    adapter.handle_message.assert_not_awaited()
    send_calls = [params for method, params in calls if method == "messages.send"]
    assert any("TCCC AI: /project tccc-ai" in params.get("message", "") for params in send_calls)
    keyboard = json.loads(send_calls[-1]["keyboard"])
    assert keyboard["inline"] is True
    assert any(button["action"].get("label") == "TCCC AI" for row in keyboard["buttons"] for button in row)


@pytest.mark.asyncio
async def test_project_menu_button_with_bot_mention_without_payload_still_shows_list(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {"2000000042": {"lanes": [{"id": "tccc-ai", "name": "TCCC AI"}]}},
            },
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 14, "text": "@club123456789 Проекты"}}}
    )

    adapter.handle_message.assert_not_awaited()
    send_calls = [params for method, params in calls if method == "messages.send"]
    assert any("TCCC AI: /project tccc-ai" in params.get("message", "") for params in send_calls)


@pytest.mark.asyncio
async def test_invite_command_returns_current_vk_chat_invite_without_agent_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["2000000042"]})
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        if method == "messages.getInviteLink":
            return {"response": {"link": "https://vk.me/join/testInvite"}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 12, "text": "/invite"}}}
    )

    adapter.handle_message.assert_not_awaited()
    invite_calls = [params for method, params in calls if method == "messages.getInviteLink"]
    assert invite_calls == [{"peer_id": "2000000042", "reset": 0}]
    assert any("https://vk.me/join/testInvite" in params.get("message", "") for method, params in calls if method == "messages.send")


@pytest.mark.asyncio
async def test_invite_command_in_dm_returns_safe_message(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["12345"]}))
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 12345, "conversation_message_id": 12, "text": "/invite"}}}
    )

    adapter.handle_message.assert_not_awaited()
    assert not any(method == "messages.getInviteLink" for method, _params in calls)
    assert any("только в VK-чате" in params.get("message", "") for method, params in calls if method == "messages.send")


@pytest.mark.asyncio
async def test_commands_button_shows_vk_command_list_without_agent_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["2000000042"]}))
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 12, "text": "Команды", "payload": json.dumps({"vkpl": "commands"})}}}
    )

    adapter.handle_message.assert_not_awaited()
    send_calls = [params for method, params in calls if method == "messages.send"]
    assert any("Команды VK-чата" in params.get("message", "") for params in send_calls)
    assert any("/invite" in params.get("message", "") for params in send_calls)
    keyboard = json.loads(send_calls[-1]["keyboard"])
    labels = [button["action"].get("label") for row in keyboard["buttons"] for button in row]
    assert keyboard["inline"] is True
    assert "/project list" in labels
    assert "/project new" in labels
    assert "/project edit" in labels
    assert "/invite" in labels


@pytest.mark.asyncio
async def test_commands_inline_invite_button_executes_invite_without_agent_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["2000000042"]}))
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        if method == "messages.getInviteLink":
            return {"response": {"link": "https://vk.me/join/testInvite"}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000042,
                    "conversation_message_id": 13,
                    "text": "/invite",
                    "payload": json.dumps({"vkpl": "cmd", "cmd": "/invite"}),
                }
            },
        }
    )

    adapter.handle_message.assert_not_awaited()
    assert any(method == "messages.getInviteLink" for method, _params in calls)
    assert any("https://vk.me/join/testInvite" in params.get("message", "") for method, params in calls if method == "messages.send")


@pytest.mark.asyncio
async def test_project_list_next_label_fallback_uses_saved_page_state(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    lanes = [{"id": f"project-{i}", "name": f"Project {i}"} for i in range(12)]
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"group_id": "123456789", "allowed_peers": ["2000000042"], "project_lanes": {"2000000042": {"lanes": lanes}}},
        )
    )
    adapter._lane_state.setdefault("project_list_messages", {})["2000000042:100"] = "cmid:55"
    adapter._lane_state.setdefault("project_list_pages", {})["2000000042:100"] = 0
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        if method == "messages.edit":
            return {"response": 1}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 13, "text": "Следующая"}}}
    )

    adapter.handle_message.assert_not_awaited()
    edit_calls = [params for method, params in calls if method == "messages.edit"]
    assert len(edit_calls) == 1
    assert "Project 11: /project project-11" in edit_calls[0]["message"]
    assert adapter._lane_state["project_list_pages"]["2000000042:100"] == 1


@pytest.mark.asyncio
async def test_project_list_number_command_shows_requested_page(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    lanes = [{"id": f"project-{i}", "name": f"Project {i}"} for i in range(12)]
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"group_id": "123456789", "allowed_peers": ["2000000042"], "project_lanes": {"2000000042": {"lanes": lanes}}},
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 14, "text": "/project list 2"}}}
    )

    adapter.handle_message.assert_not_awaited()
    send_calls = [params for method, params in calls if method == "messages.send"]
    assert any("Project 11: /project project-11" in params.get("message", "") for params in send_calls)
    keyboard = json.loads(send_calls[-1]["keyboard"])
    labels = [button["action"].get("label") for row in keyboard["buttons"] for button in row]
    assert "Предыдущая" in labels


@pytest.mark.asyncio
async def test_project_list_number_edits_previous_list_message_when_possible(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    lanes = [{"id": f"project-{i}", "name": f"Project {i}"} for i in range(12)]
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"group_id": "123456789", "allowed_peers": ["2000000042"], "project_lanes": {"2000000042": {"lanes": lanes}}},
        )
    )
    adapter._lane_state.setdefault("project_list_messages", {})["2000000042:100"] = "cmid:55"
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        if method == "messages.edit":
            return {"response": 1}
        raise AssertionError(method)

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 15, "text": "/project list 2"}}}
    )

    adapter.handle_message.assert_not_awaited()
    edit_calls = [params for method, params in calls if method == "messages.edit"]
    send_calls = [params for method, params in calls if method == "messages.send"]
    assert len(edit_calls) == 1
    assert send_calls == []
    assert edit_calls[0]["conversation_message_id"] == "55"
    assert "Project 11: /project project-11" in edit_calls[0]["message"]
    keyboard = json.loads(edit_calls[0]["keyboard"])
    labels = [button["action"].get("label") for row in keyboard["buttons"] for button in row]
    assert "Предыдущая" in labels


@pytest.mark.asyncio
async def test_project_text_button_selects_lane_without_agent_turn(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {"2000000042": {"lanes": [{"id": "tccc-ai", "name": "TCCC AI"}]}},
            },
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000042,
                    "conversation_message_id": 13,
                    "text": "TCCC AI",
                    "payload": json.dumps({"vkpl": "select", "id": "tccc-ai"}),
                }
            },
        }
    )

    adapter.handle_message.assert_not_awaited()
    assert adapter._get_active_lane_id("2000000042", "100") == "tccc-ai"
    assert any("Проект выбран: TCCC AI" in params.get("message", "") for method, params in calls if method == "messages.send")


def test_project_list_inline_text_keyboard_keeps_visible_labels_without_color(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {"2000000042": {"lanes": [{"id": "tccc-ai", "name": "TCCC AI"}]}},
            },
        )
    )

    keyboard = json.loads(adapter._project_list_keyboard("2000000042"))
    flat_buttons = [button for row in keyboard["buttons"] for button in row]
    project_button = next(button for button in flat_buttons if button["action"].get("label") == "TCCC AI")

    assert keyboard["inline"] is True
    assert project_button["action"]["type"] == "text"
    assert json.loads(project_button["action"]["payload"])["id"] == "tccc-ai"
    assert all("color" not in button for button in flat_buttons)
    resolved = adapter._resolve_lane("2000000042", "TCCC AI")
    assert resolved is not None
    assert resolved["id"] == "tccc-ai"


def test_project_list_keyboard_shows_twenty_projects_without_callback_pagination(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    lanes = [{"id": "hermes-vk-plugin", "name": "Hermes VK-плагин"}]
    lanes.extend({"id": f"project-{i}", "name": f"Project {i}"} for i in range(19))
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"group_id": "123456789", "allowed_peers": ["2000000042"], "project_lanes": {"2000000042": {"lanes": lanes}}},
        )
    )

    keyboard = json.loads(adapter._project_list_keyboard("2000000042"))
    flat_buttons = [button for row in keyboard["buttons"] for button in row]
    labels = [button["action"].get("label") for button in flat_buttons]

    assert "Hermes VK-плагин" in labels
    assert "Далее" not in labels
    assert "Следующая" in labels
    next_payload = json.loads(next(button for button in flat_buttons if button["action"].get("label") == "Следующая")["action"]["payload"])
    assert next_payload["cmd"] == "/project list 2"
    assert len(flat_buttons) == 9
    assert len(keyboard["buttons"]) <= 3
    assert all(button["action"]["type"] == "text" for button in flat_buttons)

    page_two = json.loads(adapter._project_list_keyboard("2000000042", page=1))
    page_two_labels = [button["action"].get("label") for row in page_two["buttons"] for button in row]
    assert "Предыдущая" in page_two_labels
    assert "Следующая" in page_two_labels
    assert "Далее" not in page_two_labels

    text = adapter._project_list_text("2000000042")
    assert "Project 18" in text
    assert "Кнопками показаны первые 8 проектов" in text


def test_project_list_sorts_lanes_by_latest_thread_session(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    lanes = [
        {"id": "old", "name": "Old"},
        {"id": "new", "name": "New"},
        {"id": "never", "name": "Never"},
    ]
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sessions (source TEXT, chat_id TEXT, chat_type TEXT, thread_id TEXT, started_at REAL, ended_at REAL, last_activity_at REAL)"
    )
    con.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("vk", "2000000042", "thread", "lane:old", 10.0, 20.0, 30.0),
            ("vk", "2000000042", "thread", "lane:new", 10.0, 20.0, 99.0),
            ("vk", "2000009999", "thread", "lane:old", 10.0, 20.0, 1000.0),
            ("telegram", "2000000042", "thread", "lane:old", 10.0, 20.0, 1000.0),
        ],
    )
    con.commit()
    con.close()
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"group_id": "123456789", "project_lanes": {"2000000042": {"lanes": lanes}}},
        )
    )

    visible, page, total_pages = adapter._project_list_items("2000000042", page=0, page_size=8)

    assert page == 0
    assert total_pages == 1
    assert [lane["id"] for lane in visible] == ["new", "old", "never"]


def test_project_list_pinned_lanes_stay_above_session_recency(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE sessions (source TEXT, chat_id TEXT, chat_type TEXT, thread_id TEXT, started_at REAL, ended_at REAL, last_activity_at REAL)"
        )
        con.execute("INSERT INTO sessions VALUES ('vk', '2000000042', 'thread', 'lane:new', 1, 1, 200)")
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "project_lanes": {
                    "2000000042": {
                        "lanes": [
                            {"id": "pinned", "name": "Pinned"},
                            {"id": "new", "name": "New"},
                        ]
                    }
                },
            },
        )
    )
    adapter._lane_state.setdefault("pinned", {})["2000000042"] = ["pinned"]

    visible, _page, _total_pages = adapter._project_list_items("2000000042", page=0, page_size=8)

    assert [lane["id"] for lane in visible] == ["pinned", "new"]


def test_project_list_keeps_config_order_when_session_db_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    lanes = [
        {"id": "first", "name": "First"},
        {"id": "second", "name": "Second"},
    ]
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"group_id": "123456789", "project_lanes": {"2000000042": {"lanes": lanes}}},
        )
    )

    visible, _page, _total_pages = adapter._project_list_items("2000000042", page=0, page_size=8)

    assert [lane["id"] for lane in visible] == ["first", "second"]

@pytest.mark.asyncio
async def test_project_new_pending_routes_unparseable_text_to_normal_agent_turn(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"group_id": "123456789", "allowed_peers": ["2000000042"]},
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 13, "text": "/project new"}}}
    )
    adapter.handle_message.assert_not_awaited()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 14, "text": "создай новый проект"}}}
    )

    event = adapter.handle_message.await_args.args[0]
    assert "VK project lane creation mode" in event.channel_prompt
    assert event.text.startswith("создай новый проект")


@pytest.mark.asyncio
async def test_project_new_pending_labeled_text_creates_lane_immediately(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["2000000042"]})
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 15, "text": "/project new"}}}
    )
    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000042,
                    "conversation_message_id": 16,
                    "text": "Название: Rialo\nНазначение: ретродроп\nПапка: rialo\nСкиллы: coding",
                }
            },
        }
    )

    adapter.handle_message.assert_not_awaited()
    assert adapter._get_active_lane_id("2000000042", "100") == "rialo"
    created = adapter._resolve_lane("2000000042", "rialo")
    assert created is not None
    assert created["name"] == "Rialo"
    assert any(call[0] == "messages.send" and "Проект создан" in call[1].get("message", "") for call in calls)


@pytest.mark.asyncio
async def test_project_new_fast_fallback_creates_and_selects_custom_lane(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["2000000042"]})
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 17, "text": "/project new Rialo coding тестовый проект"}}}
    )

    adapter.handle_message.assert_not_awaited()
    assert adapter._get_active_lane_id("2000000042", "100") == "rialo"
    created = adapter._resolve_lane("2000000042", "rialo")
    assert created is not None
    assert created["skills"] == ["coding"]


@pytest.mark.asyncio
async def test_project_edit_fast_fallback_updates_existing_custom_lane(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["2000000042"]})
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 17, "text": "/project new Gito coding старое описание; папка gito"}}}
    )
    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000042,
                    "conversation_message_id": 18,
                    "text": "/project edit gito папка ai-projects/gito; workdir /home/assistent/ai-projects/gito; контекст: существующий проект Gito",
                }
            },
        }
    )

    adapter.handle_message.assert_not_awaited()
    edited = adapter._resolve_lane("2000000042", "gito")
    assert edited is not None
    assert edited["folder"] == "ai-projects/gito"
    assert edited["workdir"] == "/home/assistent/ai-projects/gito"
    assert edited["description"] == "существующий проект Gito"
    assert any("Проект обновлён: Gito" in params.get("message", "") for method, params in calls if method == "messages.send")


@pytest.mark.asyncio
async def test_project_edit_pending_unparseable_text_routes_to_normal_agent_turn(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {"2000000042": {"lanes": [{"id": "gito", "name": "Gito", "description": "old"}]}},
            },
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        if method == "messages.getByConversationMessageId":
            return {"response": {"items": []}}
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()
    await adapter._set_active_lane_id("2000000042", "100", "gito")

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 19, "text": "/project edit"}}}
    )
    adapter.handle_message.assert_not_awaited()

    await adapter._handle_update(
        {"type": "message_new", "object": {"message": {"from_id": 100, "peer_id": 2000000042, "conversation_message_id": 20, "text": "сделай так, чтобы модель сама поняла нужные изменения"}}}
    )

    event = adapter.handle_message.await_args.args[0]
    assert "VK project lane edit mode" in event.channel_prompt
    assert "Project to edit: gito" in event.channel_prompt
    assert event.text.startswith("сделай так")


@pytest.mark.asyncio
async def test_project_message_event_select_sets_active_lane_without_agent_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {"2000000042": {"lanes": [{"id": "tccc-ai", "name": "TCCC AI"}]}},
            },
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {
            "type": "message_event",
            "object": {
                "peer_id": 2000000042,
                "user_id": 100,
                "event_id": "evt1",
                "payload": {"vkpl": "select", "id": "tccc-ai"},
            },
        }
    )

    adapter.handle_message.assert_not_awaited()
    assert adapter._get_active_lane_id("2000000042", "100") == "tccc-ai"
    assert any(call[0] == "messages.sendMessageEventAnswer" for call in calls)


@pytest.mark.asyncio
async def test_vk_send_exec_approval_renders_full_button_set(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["2000000042"]}))
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 77}]}

    adapter._vk_method = fake_vk_method

    result = await adapter.send_exec_approval(
        chat_id="2000000042",
        command="touch /tmp/demo",
        session_key="agent:main:vk:thread:2000000042:lane:gito",
        description="dangerous command",
    )

    assert result.success is True
    assert result.message_id == "cmid:77"
    send_params = [params for method, params in calls if method == "messages.send"][0]
    assert "Command Approval Required" in send_params["message"]
    keyboard = json.loads(send_params["keyboard"])
    assert keyboard["inline"] is True
    labels = [button["action"]["label"] for row in keyboard["buttons"] for button in row]
    assert labels == ["✅ Allow Once", "✅ Session", "✅ Always", "❌ Deny"]
    payloads = [json.loads(button["action"]["payload"]) for row in keyboard["buttons"] for button in row]
    assert [payload["vkea"] for payload in payloads] == ["once", "session", "always", "deny"]
    assert len({payload["id"] for payload in payloads}) == 1
    assert adapter._approval_state[payloads[0]["id"]] == "agent:main:vk:thread:2000000042:lane:gito"


@pytest.mark.asyncio
async def test_vk_send_exec_approval_smart_deny_renders_two_buttons(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["2000000042"]}))
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 78}]}

    adapter._vk_method = fake_vk_method

    result = await adapter.send_exec_approval(
        chat_id="2000000042",
        command="curl example.test",
        session_key="s",
        allow_permanent=False,
        smart_denied=True,
    )

    assert result.success is True
    keyboard = json.loads([params for method, params in calls if method == "messages.send"][0]["keyboard"])
    labels = [button["action"]["label"] for row in keyboard["buttons"] for button in row]
    assert labels == ["✅ Allow Once", "❌ Deny"]


@pytest.mark.asyncio
async def test_vk_exec_approval_callback_resolves_and_edits_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_users": ["100"], "allowed_peers": ["2000000042"]}))
    adapter._approval_state[5] = "agent:main:vk:thread:2000000042:lane:gito"
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method

    with patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
        await adapter._handle_update(
            {
                "type": "message_event",
                "object": {
                    "peer_id": 2000000042,
                    "user_id": 100,
                    "event_id": "evt-approval",
                    "conversation_message_id": 55,
                    "payload": {"vkea": "session", "id": 5},
                },
            }
        )

    resolve.assert_called_once_with("agent:main:vk:thread:2000000042:lane:gito", "session")
    assert 5 not in adapter._approval_state
    assert any(method == "messages.sendMessageEventAnswer" for method, _params in calls)
    edit_calls = [params for method, params in calls if method == "messages.edit"]
    assert edit_calls and edit_calls[0]["conversation_message_id"] == "55"
    assert "Approved for session" in edit_calls[0]["message"]
    removed_keyboard = json.loads(edit_calls[0]["keyboard"])
    assert removed_keyboard["inline"] is True
    assert removed_keyboard["buttons"] == []


@pytest.mark.asyncio
async def test_vk_send_slash_confirm_renders_three_buttons(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["2000000042"]}))
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 88}]}

    adapter._vk_method = fake_vk_method

    result = await adapter.send_slash_confirm(
        chat_id="2000000042",
        title="/reload-mcp",
        message="Approve reload?",
        session_key="agent:main:vk:group:2000000042:100",
        confirm_id="confirm-1",
    )

    assert result.success is True
    assert result.message_id == "cmid:88"
    send_params = [params for method, params in calls if method == "messages.send"][0]
    keyboard = json.loads(send_params["keyboard"])
    labels = [button["action"]["label"] for row in keyboard["buttons"] for button in row]
    assert labels == ["✅ Approve Once", "🔒 Always Approve", "❌ Cancel"]
    payloads = [json.loads(button["action"]["payload"]) for row in keyboard["buttons"] for button in row]
    assert [payload["vksc"] for payload in payloads] == ["once", "always", "cancel"]
    assert adapter._slash_confirm_state["confirm-1"] == "agent:main:vk:group:2000000042:100"


@pytest.mark.asyncio
async def test_vk_slash_confirm_callback_resolves_edits_and_sends_result(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_users": ["100"], "allowed_peers": ["2000000042"]}))
    adapter._slash_confirm_state["confirm-1"] = "agent:main:vk:group:2000000042:100"
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method

    async def fake_resolve(session_key, confirm_id, choice):
        assert session_key == "agent:main:vk:group:2000000042:100"
        assert confirm_id == "confirm-1"
        assert choice == "always"
        return "Reload complete"

    with patch("tools.slash_confirm.resolve", fake_resolve):
        await adapter._handle_update(
            {
                "type": "message_event",
                "object": {
                    "peer_id": 2000000042,
                    "user_id": 100,
                    "event_id": "evt-confirm",
                    "conversation_message_id": 56,
                    "payload": {"vksc": "always", "id": "confirm-1"},
                },
            }
        )

    assert "confirm-1" not in adapter._slash_confirm_state
    assert any(method == "messages.sendMessageEventAnswer" for method, _params in calls)
    edit_calls = [params for method, params in calls if method == "messages.edit"]
    assert edit_calls and "Always approve" in edit_calls[0]["message"]
    send_calls = [params for method, params in calls if method == "messages.send"]
    assert send_calls and send_calls[-1]["message"] == "Reload complete"


@pytest.mark.asyncio
async def test_vk_send_clarify_renders_numbered_buttons_and_remembers_lane(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {
                    "2000000042": {
                        "lanes": [
                            {"id": "gito", "name": "Gito", "folder": "/tmp/gito", "skills": ["coding"]}
                        ]
                    }
                },
            },
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 91}]}

    adapter._vk_method = fake_vk_method

    result = await adapter.send_clarify(
        chat_id="2000000042",
        question="Разрешаешь обновить kernel/headers?",
        choices=["Да, поставить и перезагрузить", "Поставить без reboot", "Искать другой протокол"],
        clarify_id="clarify-1",
        session_key="agent:main:vk:thread:2000000042:lane:gito",
        metadata={"thread_id": "lane:gito"},
    )

    assert result.success is True
    assert result.message_id == "cmid:91"
    send_params = [params for method, params in calls if method == "messages.send"][0]
    assert "❓ Разрешаешь обновить kernel/headers?" in send_params["message"]
    assert "1. Да, поставить и перезагрузить" in send_params["message"]
    assert "3. Искать другой протокол" in send_params["message"]
    keyboard = json.loads(send_params["keyboard"])
    assert keyboard["inline"] is True
    labels = [button["action"]["label"] for row in keyboard["buttons"] for button in row]
    assert labels == ["1", "2", "3", "✏️ Свой ответ"]
    action_types = [button["action"]["type"] for row in keyboard["buttons"] for button in row]
    assert action_types == ["text", "text", "text", "text"]
    payloads = [json.loads(button["action"]["payload"]) for row in keyboard["buttons"] for button in row]
    assert [payload["vkcl"] for payload in payloads] == ["0", "1", "2", "other"]
    assert {payload["id"] for payload in payloads} == {"clarify-1"}
    assert adapter._clarify_state["clarify-1"] == "agent:main:vk:thread:2000000042:lane:gito"
    assert adapter._lane_state["message_lanes"]["2000000042:91"] == "gito"


@pytest.mark.asyncio
async def test_vk_clarify_callback_resolves_choice_and_removes_buttons(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_users": ["100"], "allowed_peers": ["2000000042"]}))
    session_key = "agent:main:vk:thread:2000000042:lane:gito"
    adapter._clarify_state["clarify-1"] = session_key
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method

    from tools import clarify_gateway
    clarify_gateway.register(
        clarify_id="clarify-1",
        session_key=session_key,
        question="Pick one",
        choices=["First choice", "Second choice", "Third choice"],
    )
    try:
        with patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True) as resolve:
            await adapter._handle_update(
                {
                    "type": "message_event",
                    "object": {
                        "peer_id": 2000000042,
                        "user_id": 100,
                        "event_id": "evt-clarify",
                        "conversation_message_id": 57,
                        "payload": {"vkcl": "1", "id": "clarify-1"},
                    },
                }
            )
    finally:
        clarify_gateway.clear_session(session_key)

    resolve.assert_called_once_with("clarify-1", "Second choice")
    assert "clarify-1" not in adapter._clarify_state
    assert any(method == "messages.sendMessageEventAnswer" for method, _params in calls)
    edit_calls = [params for method, params in calls if method == "messages.edit"]
    assert edit_calls and edit_calls[0]["conversation_message_id"] == "57"
    assert "Second choice" in edit_calls[0]["message"]
    removed_keyboard = json.loads(edit_calls[0]["keyboard"])
    assert removed_keyboard["inline"] is True
    assert removed_keyboard["buttons"] == []


@pytest.mark.asyncio
async def test_vk_clarify_text_button_message_new_payload_resolves_choice(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_users": ["100"], "allowed_peers": ["2000000042"]}))
    session_key = "agent:main:vk:thread:2000000042:lane:gito"
    adapter._clarify_state["clarify-text"] = session_key
    adapter.handle_message = AsyncMock()
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": {"items": []}}

    adapter._vk_method = fake_vk_method

    from tools import clarify_gateway
    clarify_gateway.register(
        clarify_id="clarify-text",
        session_key=session_key,
        question="Pick one",
        choices=["First choice", "Second choice"],
    )
    try:
        with patch("tools.clarify_gateway.resolve_gateway_clarify", return_value=True) as resolve:
            await adapter._handle_update(
                {
                    "type": "message_new",
                    "object": {
                        "message": {
                            "from_id": 100,
                            "peer_id": 2000000042,
                            "conversation_message_id": 58,
                            "text": "2",
                            "payload": json.dumps({"vkcl": "1", "id": "clarify-text"}),
                        }
                    },
                }
            )
    finally:
        clarify_gateway.clear_session(session_key)

    resolve.assert_called_once_with("clarify-text", "Second choice")
    adapter.handle_message.assert_not_awaited()
    assert "clarify-text" not in adapter._clarify_state



@pytest.mark.asyncio
async def test_vk_clarify_other_callback_switches_to_text_capture(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_users": ["100"], "allowed_peers": ["2000000042"]}))
    adapter._clarify_state["clarify-2"] = "agent:main:vk:group:2000000042:100"
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method

    with patch("tools.clarify_gateway.mark_awaiting_text", return_value=True) as mark:
        await adapter._handle_update(
            {
                "type": "message_event",
                "object": {
                    "peer_id": 2000000042,
                    "user_id": 100,
                    "event_id": "evt-clarify-other",
                    "conversation_message_id": 58,
                    "payload": {"vkcl": "other", "id": "clarify-2"},
                },
            }
        )

    mark.assert_called_once_with("clarify-2")
    assert adapter._clarify_state["clarify-2"] == "agent:main:vk:group:2000000042:100"
    assert any(method == "messages.sendMessageEventAnswer" for method, _params in calls)
    edit_calls = [params for method, params in calls if method == "messages.edit"]
    assert edit_calls and edit_calls[0]["conversation_message_id"] == "58"
    assert "Напиши свой ответ" in edit_calls[0]["message"]
    removed_keyboard = json.loads(edit_calls[0]["keyboard"])
    assert removed_keyboard["inline"] is True
    assert removed_keyboard["buttons"] == []


@pytest.mark.asyncio
async def test_vk_send_clarify_oversized_choices_uses_numbered_text_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789", "allowed_peers": ["2000000042"]}))
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 92}]}

    adapter._vk_method = fake_vk_method

    result = await adapter.send_clarify(
        chat_id="2000000042",
        question="Выбери одиннадцать?",
        choices=[f"Choice {idx}" for idx in range(1, 12)],
        clarify_id="clarify-big",
        session_key="agent:main:vk:group:2000000042:100",
    )

    assert result.success is True
    send_params = [params for method, params in calls if method == "messages.send"][0]
    assert "11. Choice 11" in send_params["message"]
    assert "Reply with the number" in send_params["message"]
    assert "keyboard" in send_params  # default project keyboard remains, not clarify inline buttons
    keyboard = json.loads(send_params["keyboard"])
    payloads = [json.loads(button["action"]["payload"]) for row in keyboard["buttons"] for button in row]
    assert not any("vkcl" in payload for payload in payloads)
    assert "clarify-big" not in adapter._clarify_state


@pytest.mark.asyncio
async def test_vk_reply_to_clarify_prompt_restores_lane_for_text_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {
                    "2000000042": {
                        "lanes": [
                            {"id": "gito", "name": "Gito", "folder": "/tmp/gito", "skills": ["coding"]}
                        ]
                    }
                },
            },
        )
    )
    adapter._lane_state.setdefault("message_lanes", {})["2000000042:92"] = "gito"
    adapter._vk_method = AsyncMock(return_value={"response": {"items": []}})
    adapter.handle_message = AsyncMock()

    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000042,
                    "conversation_message_id": 93,
                    "text": "2",
                    "reply_message": {"conversation_message_id": 92},
                }
            },
        }
    )

    event = adapter.handle_message.await_args.args[0]
    assert event.text == "2"
    assert event.source.chat_type == "thread"
    assert event.source.thread_id == "lane:gito"
    assert build_session_key(event.source) == "agent:main:vk:thread:2000000042:lane:gito"


@pytest.mark.asyncio
async def test_vk_message_new_duplicate_is_ignored_before_enrichment_and_download():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={"group_id": "123456789", "allowed_peers": ["2000000001"], "dedupe_ttl_seconds": 300},
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._vk_method = AsyncMock(return_value={"response": {"items": []}})
    update = {
        "type": "message_new",
        "object": {
            "message": {
                "from_id": 100,
                "peer_id": 2000000001,
                "conversation_message_id": 7,
                "text": "повтори",
            }
        },
    }

    await adapter._handle_update(update)
    await adapter._handle_update(update)

    assert adapter.handle_message.await_count == 1
    assert adapter._vk_method.await_count == 2  # first enrich + first context resolve only


@pytest.mark.asyncio
async def test_vk_message_edit_dispatches_as_followup_event():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={"group_id": "123456789", "allowed_peers": ["2000000001"]},
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._vk_method = AsyncMock(return_value={"response": {"items": []}})

    await adapter._handle_update(
        {
            "type": "message_edit",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000001,
                    "conversation_message_id": 8,
                    "text": "дописанный текст после завершения",
                }
            },
        }
    )

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.text == "дописанный текст после завершения"
    assert event.message_id == "8"
    assert event.source.chat_id == "2000000001"


@pytest.mark.asyncio
async def test_vk_message_edit_duplicate_same_text_is_ignored_but_changed_text_dispatches():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={"group_id": "123456789", "allowed_peers": ["2000000001"]},
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._vk_method = AsyncMock(return_value={"response": {"items": []}})

    base = {
        "type": "message_edit",
        "object": {
            "message": {
                "from_id": 100,
                "peer_id": 2000000001,
                "conversation_message_id": 8,
                "text": "первая редакция",
            }
        },
    }
    await adapter._handle_update(base)
    await adapter._handle_update(base)
    changed = {
        "type": "message_edit",
        "object": {
            "message": {
                "from_id": 100,
                "peer_id": 2000000001,
                "conversation_message_id": 8,
                "text": "вторая редакция",
            }
        },
    }
    await adapter._handle_update(changed)

    assert adapter.handle_message.await_count == 2


@pytest.mark.asyncio
async def test_vk_message_edit_from_community_echo_is_ignored():
    adapter = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789", "allowed_peers": ["2000000001"]}))
    adapter.handle_message = AsyncMock()
    adapter._vk_method = AsyncMock()

    await adapter._handle_update(
        {
            "type": "message_edit",
            "object": {
                "message": {
                    "from_id": -123456789,
                    "peer_id": 2000000001,
                    "conversation_message_id": 9,
                    "text": "progress edited",
                }
            },
        }
    )

    adapter.handle_message.assert_not_awaited()
    adapter._vk_method.assert_not_awaited()


@pytest.mark.asyncio
async def test_vk_message_new_with_attachment_builds_media_event():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={"group_id": "123456789", "allowed_peers": ["2000000001"]},
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._vk_method = AsyncMock(
        return_value={"response": {"items": [{"chat_settings": {"title": "Voice Test Chat"}}]}}
    )

    with patch(
        "plugins.platforms.vk.adapter._download_attachment_async",
        AsyncMock(return_value="/tmp/hermes-vk-voice.ogg"),
    ) as download:
        await adapter._handle_update(
            {
                "type": "message_new",
                "object": {
                    "message": {
                        "from_id": 100,
                        "peer_id": 2000000001,
                        "conversation_message_id": 7,
                        "text": "смотри",
                        "attachments": [
                            {"type": "audio_message", "audio_message": {"link_ogg": "https://vk.example/voice.ogg"}}
                        ],
                    }
                },
            }
        )

    event = adapter.handle_message.call_args.args[0]
    assert event.message_type is MessageType.VOICE
    assert event.media_urls == ["/tmp/hermes-vk-voice.ogg"]
    assert event.media_types == ["audio/ogg"]
    assert event.source.chat_name == "Voice Test Chat"
    assert event.source.chat_topic == "VK Web chat URL: https://vk.com/im?sel=c1"
    download.assert_awaited_once_with("https://vk.example/voice.ogg", "audio/ogg", max_bytes=26214400)
    assert "голосовое сообщение" in event.text


@pytest.mark.asyncio
async def test_vk_pure_forwarded_message_dispatches_context_event():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={"group_id": "123456789", "allowed_peers": ["2000000001"]},
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._vk_method = AsyncMock(return_value={"response": {"items": []}})

    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000001,
                    "conversation_message_id": 8,
                    "text": "",
                    "fwd_messages": [
                        {
                            "from_id": 200,
                            "text": "пересланный смысл",
                            "attachments": [{"type": "photo", "photo": {"sizes": []}}],
                        }
                    ],
                }
            },
        }
    )

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert "[VK forwarded messages]" in event.text
    assert "from_id=200" in event.text
    assert "пересланный смысл" in event.text
    assert "фото" in event.text
    assert event.message_type is MessageType.TEXT
    assert event.media_urls == []


@pytest.mark.asyncio
async def test_vk_forwarded_photo_with_comment_routes_media_url():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={"group_id": "123456789", "allowed_peers": ["2000000001"]},
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._vk_method = AsyncMock(return_value={"response": {"items": []}})

    with patch("plugins.platforms.vk.adapter._download_attachment_async", AsyncMock(return_value="/tmp/forwarded-photo.jpg")) as download:
        await adapter._handle_update(
            {
                "type": "message_new",
                "object": {
                    "message": {
                        "from_id": 100,
                        "peer_id": 2000000001,
                        "conversation_message_id": 10,
                        "text": "посмотри фото",
                        "fwd_messages": [
                            {
                                "from_id": 200,
                                "text": "",
                                "attachments": [
                                    {
                                        "type": "photo",
                                        "photo": {
                                            "sizes": [
                                                {"width": 10, "height": 10, "url": "https://vk.example/small.jpg"},
                                                {"width": 100, "height": 100, "url": "https://vk.example/big.jpg"},
                                            ]
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                },
            }
        )

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.text.startswith("посмотри фото")
    assert "[VK forwarded messages]" in event.text
    assert "[VK attachment: фото]" in event.text
    assert event.message_type is MessageType.PHOTO
    assert event.media_urls == ["/tmp/forwarded-photo.jpg"]
    assert event.media_types == ["image/jpeg"]
    download.assert_awaited_once_with("https://vk.example/big.jpg", "image/jpeg", max_bytes=26214400)


@pytest.mark.asyncio
async def test_vk_reply_photo_routes_media_url():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={"group_id": "123456789", "allowed_peers": ["2000000001"]},
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._vk_method = AsyncMock(return_value={"response": {"items": []}})

    with patch("plugins.platforms.vk.adapter._download_attachment_async", AsyncMock(return_value="/tmp/reply-photo.jpg")) as download:
        await adapter._handle_update(
            {
                "type": "message_new",
                "object": {
                    "message": {
                        "from_id": 100,
                        "peer_id": 2000000001,
                        "conversation_message_id": 11,
                        "text": "что на этом фото?",
                        "reply_message": {
                            "from_id": 200,
                            "text": "",
                            "attachments": [
                                {
                                    "type": "photo",
                                    "photo": {
                                        "sizes": [
                                            {"width": 100, "height": 60, "url": "https://vk.example/reply.jpg"}
                                        ]
                                    },
                                }
                            ],
                        },
                    }
                },
            }
        )

    event = adapter.handle_message.call_args.args[0]
    assert "[VK reply]" in event.text
    assert event.message_type is MessageType.PHOTO
    assert event.media_urls == ["/tmp/reply-photo.jpg"]
    assert event.media_types == ["image/jpeg"]
    download.assert_awaited_once_with("https://vk.example/reply.jpg", "image/jpeg", max_bytes=26214400)


@pytest.mark.asyncio
async def test_vk_forwarded_message_with_comment_preserves_both_layers():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={"group_id": "123456789", "allowed_peers": ["2000000001"]},
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._vk_method = AsyncMock(return_value={"response": {"items": []}})

    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000001,
                    "conversation_message_id": 9,
                    "text": "мой комментарий",
                    "reply_message": {"from_id": 300, "text": "исходное сообщение"},
                    "fwd_messages": [{"from_id": 200, "text": "пересланный смысл"}],
                }
            },
        }
    )

    event = adapter.handle_message.call_args.args[0]
    assert event.text.startswith("мой комментарий")
    assert "[VK reply]" in event.text
    assert "исходное сообщение" in event.text
    assert "[VK forwarded messages]" in event.text
    assert "пересланный смысл" in event.text


@pytest.mark.asyncio
async def test_vk_materializes_direct_media_urls_for_gateway_processors():
    adapter = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"}))

    async def fake_download(url, media_type, *, max_bytes):
        assert max_bytes == 26214400
        return f"/tmp/{media_type.replace('/', '-')}-{url.rsplit('/', 1)[-1]}"

    urls = [
        "https://sun9-1.userapi.com/photo.jpg",
        "https://psv4.userapi.com/video_message.mp4",
        "https://psv4.userapi.com/song.mp3",
        "https://psv4.userapi.com/report.pdf",
    ]
    media_types = ["image/jpeg", "video/mp4", "audio/mpeg", "application/pdf"]

    with patch("plugins.platforms.vk.adapter._download_attachment_async", AsyncMock(side_effect=fake_download)) as download:
        result = await adapter._materialize_inbound_media(MessageType.PHOTO, urls, media_types)

    assert result == [
        "/tmp/image-jpeg-photo.jpg",
        "/tmp/video-mp4-video_message.mp4",
        "/tmp/audio-mpeg-song.mp3",
        "/tmp/application-pdf-report.pdf",
    ]
    assert download.await_count == 4


def test_vk_download_attachment_rejects_content_length_over_limit(monkeypatch, tmp_path):
    class Headers:
        def get(self, name):
            return "11" if name == "Content-Length" else None

        def get_content_type(self):
            return "audio/ogg"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            raise AssertionError("body should not be read when Content-Length exceeds limit")

    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("plugins.platforms.vk.adapter.urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(RuntimeError, match="too large"):
        _download_attachment("https://vk.example/voice.ogg", "audio/ogg", max_bytes=10)


def test_vk_download_attachment_streams_and_stops_after_limit(monkeypatch, tmp_path):
    class Headers:
        def get(self, _name):
            return None

        def get_content_type(self):
            return "application/octet-stream"

    class Response:
        headers = Headers()
        chunks = [b"12345", b"67890", b"x"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            return self.chunks.pop(0) if self.chunks else b""

    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("plugins.platforms.vk.adapter.urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(RuntimeError, match="exceeded size limit"):
        _download_attachment("https://vk.example/file.pdf", "application/pdf", max_bytes=10)

    assert not list((tmp_path / "cache" / "vk" / "attachments").glob("*.pdf"))


def test_vk_regular_video_watch_page_is_not_treated_as_downloadable_file():
    assert _looks_like_downloadable_attachment_url("https://vk.com/video-1_2?access_key=abc") is False
    assert _looks_like_downloadable_attachment_url("https://m.vk.com/video-1_2") is False
    assert _looks_like_downloadable_attachment_url("https://psv4.userapi.com/video_message.mp4") is True


def test_vk_video_from_message_is_summarized_as_video_message():
    adapter = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"}))
    summary = adapter._summarize_attachments(
        [{"type": "video", "video": {"owner_id": 103, "id": 456, "is_from_message": True}}]
    )
    assert summary == "[VK attachment: видеосообщение]"


@pytest.mark.asyncio
async def test_vk_video_history_attachments_fallback_uses_message_context():
    adapter = VKAdapter(PlatformConfig(enabled=True, token="group-token", extra={"group_id": "123456789"}))
    calls = []

    async def fake_vk_method(method, params=None, *, access_token=None):
        calls.append((method, params, access_token))
        assert method == "messages.getHistoryAttachments"
        return {
            "response": {
                "items": [
                    {
                        "attachment": {
                            "type": "video",
                            "video": {
                                "owner_id": 103,
                                "id": 456,
                                "access_key": "ak",
                                "files": {"mp4_480": "https://psv4.userapi.com/history-video-480.mp4"},
                            },
                        }
                    }
                ]
            }
        }

    adapter._vk_method = fake_vk_method
    msg = {"attachments": [{"type": "video", "video": {"owner_id": 103, "id": 456, "access_key": "ak"}}]}

    enriched = await adapter._enrich_video_attachments_from_api(msg, peer_id="2000000042", conversation_message_id=42)
    _message_type, urls, _media_types = adapter._extract_attachment_media(enriched["attachments"])

    assert calls == [
        (
            "messages.getHistoryAttachments",
            {
                "peer_id": "2000000042",
                "media_type": "video",
                "conversation_message_id": "42",
                "attachment_position": "1",
                "count": "10",
            },
            None,
        )
    ]
    assert urls == ["https://psv4.userapi.com/history-video-480.mp4"]


@pytest.mark.asyncio
async def test_vk_video_get_enrichment_uses_api_ref_and_direct_files():
    adapter = VKAdapter(
        PlatformConfig(enabled=True, token="group-token", extra={"group_id": "123456789", "user_token": "user-token"})
    )
    calls = []

    async def fake_vk_method(method, params=None, *, access_token=None):
        calls.append((method, params, access_token))
        return {
            "response": {
                "items": [
                    {
                        "owner_id": 103,
                        "id": 456,
                        "player": "https://vkvideo.example/watch",
                        "files": {"mp4_720": "https://psv4.userapi.com/native-video-720.mp4"},
                    }
                ]
            }
        }

    adapter._vk_method = fake_vk_method
    msg = {
        "attachments": [
            {"type": "video", "video": {"owner_id": 103, "id": 456, "access_key": "ak", "player": "https://vkvideo.example/watch"}}
        ]
    }

    enriched = await adapter._enrich_video_attachments_from_api(msg)
    message_type, urls, media_types = adapter._extract_attachment_media(enriched["attachments"])

    assert calls == [("video.get", {"videos": "103_456_ak"}, "user-token")]
    assert message_type is MessageType.VIDEO
    assert urls == ["https://psv4.userapi.com/native-video-720.mp4"]
    assert media_types == ["video/mp4"]


@pytest.mark.asyncio
async def test_vk_video_get_failure_preserves_original_preview():
    adapter = VKAdapter(
        PlatformConfig(enabled=True, token="group-token", extra={"group_id": "123456789", "user_token": "user-token"})
    )
    calls = []

    async def fake_vk_method(method, params=None, *, access_token=None):
        calls.append((method, params, access_token))
        if method == "messages.getHistoryAttachments":
            return {"response": {"items": []}}
        if method == "video.get":
            raise RuntimeError("VK API error 5: User authorization failed")
        raise AssertionError(method)

    adapter._vk_method = fake_vk_method
    msg = {
        "attachments": [
            {
                "type": "video",
                "video": {
                    "owner_id": 103,
                    "id": 456,
                    "access_key": "ak",
                    "player": "https://vkvideo.example/watch",
                    "first_frame": [{"width": 640, "height": 360, "url": "https://sun9-1.userapi.com/live.jpg"}],
                },
            }
        ]
    }

    enriched = await adapter._enrich_video_attachments_from_api(msg, peer_id="2000000042", conversation_message_id=42)
    message_type, urls, media_types = adapter._extract_attachment_media(enriched["attachments"])

    assert calls == [
        (
            "messages.getHistoryAttachments",
            {
                "peer_id": "2000000042",
                "media_type": "video",
                "conversation_message_id": "42",
                "attachment_position": "1",
                "count": "10",
            },
            None,
        ),
        ("video.get", {"videos": "103_456_ak"}, "user-token"),
    ]
    assert message_type is MessageType.VIDEO
    assert urls == ["https://sun9-1.userapi.com/live.jpg"]
    assert media_types == ["image/jpeg"]


@pytest.mark.asyncio
async def test_vk_video_without_native_file_does_not_become_public_watch_url():
    adapter = VKAdapter(
        PlatformConfig(enabled=True, token="group-token", extra={"group_id": "123456789", "user_token": "user-token"})
    )

    async def fake_vk_method(method, params=None, *, access_token=None):
        return {
            "response": {
                "items": [
                    {
                        "owner_id": 103,
                        "id": 456,
                        "player": "https://vkvideo.example/watch",
                    }
                ]
            }
        }

    adapter._vk_method = fake_vk_method
    msg = {"attachments": [{"type": "video", "video": {"owner_id": 103, "id": 456, "access_key": "ak"}}]}

    enriched = await adapter._enrich_video_attachments_from_api(msg)
    _message_type, urls, _media_types = adapter._extract_attachment_media(enriched["attachments"])

    assert urls == []


@pytest.mark.asyncio
async def test_vk_materializer_keeps_regular_video_watch_page_as_url():
    adapter = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"}))
    with patch("plugins.platforms.vk.adapter._download_attachment_async", AsyncMock()) as download:
        result = await adapter._materialize_inbound_media(
            MessageType.VIDEO,
            ["https://vk.com/video-1_2?access_key=abc"],
            ["video/mp4"],
        )

    assert result == ["https://vk.com/video-1_2?access_key=abc"]
    download.assert_not_called()


@pytest.mark.asyncio
async def test_vk_enriches_message_attachments_from_conversation_api():
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))

    async def fake_vk_method(method, params=None):
        assert method == "messages.getByConversationMessageId"
        assert params == {"peer_id": "2000000042", "conversation_message_ids": "42"}
        return {
            "response": {
                "items": [
                    {
                        "conversation_message_id": 42,
                        "text": "",
                        "attachments": [
                            {
                                "type": "video_message",
                                "video_message": {"link_mp4": "https://psv4.userapi.com/round.mp4"},
                            }
                        ],
                    }
                ]
            }
        }

    adapter._vk_method = fake_vk_method

    enriched = await adapter._enrich_message_from_api(
        {"conversation_message_id": 42, "attachments": []},
        peer_id="2000000042",
        conversation_message_id=42,
    )

    assert enriched["attachments"][0]["type"] == "video_message"
    message_type, urls, media_types = adapter._extract_attachment_media(enriched["attachments"])
    assert message_type is MessageType.VIDEO
    assert urls == ["https://psv4.userapi.com/round.mp4"]
    assert media_types == ["video/mp4"]


@pytest.mark.asyncio
async def test_vk_message_new_resolves_channel_prompt_and_skills():
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "channel_prompts": {"2000000042": "Family search chat; prefer perplex for web search."},
                "channel_skill_bindings": [{"id": "2000000042", "skills": ["perplex"]}],
            },
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._vk_method = AsyncMock(
        return_value={"response": {"items": [{"chat_settings": {"title": "Example VK Chat"}}]}}
    )

    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000042,
                    "conversation_message_id": 8,
                    "text": "найди новости",
                }
            },
        }
    )

    event = adapter.handle_message.call_args.args[0]
    assert event.auto_skill == ["perplex"]
    assert event.channel_prompt == "Family search chat; prefer perplex for web search."
    assert event.source.chat_name == "Example VK Chat"
    assert event.source.chat_topic == "VK Web chat URL: https://vk.com/im?sel=c42"


@pytest.mark.asyncio
async def test_vk_conversation_context_resolves_title_and_browser_url():
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))
    adapter._vk_method = AsyncMock(
        return_value={"response": {"items": [{"chat_settings": {"title": "Example VK Chat"}}]}}
    )

    name, topic = await adapter._resolve_conversation_context("2000000042", chat_type="group")

    assert name == "Example VK Chat"
    assert topic == "VK Web chat URL: https://vk.com/im?sel=c42"
    adapter._vk_method.assert_awaited_once_with("messages.getConversationsById", {"peer_ids": "2000000042"})

    assert await adapter._resolve_conversation_context("2000000042", chat_type="group") == (name, topic)
    assert adapter._vk_method.await_count == 1


@pytest.mark.asyncio
async def test_vk_edit_message_uses_messages_edit():
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))
    calls = []

    async def fake_vk_method(method, params=None):
        calls.append((method, params))
        return {"response": 1}

    adapter._vk_method = fake_vk_method

    result = await adapter.edit_message("2000000043", "42", "progress")

    assert result.success is True
    assert result.message_id == "42"
    assert calls == [
        (
            "messages.edit",
            {"peer_id": "2000000043", "message_id": "42", "message": "progress"},
        )
    ]


@pytest.mark.asyncio
async def test_vk_send_image_file_uploads_and_sends_attachment(tmp_path):
    image = tmp_path / "pic.png"
    image.write_bytes(b"not really png")
    adapter = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"}))

    async def fake_vk_method(method, params=None):
        if method == "photos.getMessagesUploadServer":
            return {"response": {"upload_url": "https://upload.example/photo"}}
        if method == "photos.saveMessagesPhoto":
            return {"response": [{"owner_id": -123456789, "id": 11, "access_key": "ak"}]}
        if method == "messages.send":
            assert params is not None
            assert params["attachment"] == "photo-123456789_11_ak"
            assert params["message"] == "caption"
            return {"response": 123}
        raise AssertionError(method)

    adapter._vk_method = fake_vk_method
    with patch("plugins.platforms.vk.adapter._multipart_upload_async", AsyncMock(return_value={"photo": "server-payload"})):
        result = await adapter.send_image_file("2000000001", str(image), caption="caption")

    assert result.success is True
    assert result.message_id == "123"


@pytest.mark.asyncio
async def test_vk_send_video_falls_back_to_document_when_video_save_needs_user_auth(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake mp4")
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))
    calls = []

    async def fake_vk_method(method, params=None):
        calls.append((method, params))
        if method == "video.save":
            raise RuntimeError("VK API error 5: User authorization failed")
        if method == "docs.getMessagesUploadServer":
            assert params == {"peer_id": "2000000001"}
            return {"response": {"upload_url": "https://upload.example/doc"}}
        if method == "docs.save":
            return {"response": {"doc": {"owner_id": -123456789, "id": 33, "access_key": "ak"}}}
        if method == "messages.send":
            assert params is not None
            assert params["attachment"] == "doc-123456789_33_ak"
            assert params["message"] == "caption"
            return {"response": 123}
        raise AssertionError(method)

    adapter._vk_method = fake_vk_method
    with patch("plugins.platforms.vk.adapter._multipart_upload_async", AsyncMock(return_value={"file": "server-payload"})) as upload:
        result = await adapter.send_video("2000000001", str(video), caption="caption")

    assert result.success is True
    assert result.message_id == "123"
    assert [method for method, _params in calls] == [
        "video.save",
        "docs.getMessagesUploadServer",
        "docs.save",
        "messages.send",
    ]
    upload.assert_awaited_once_with("https://upload.example/doc", "file", str(video))


@pytest.mark.asyncio
async def test_vk_send_video_preserves_non_auth_video_upload_errors(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake mp4")
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))
    adapter._vk_method = AsyncMock(side_effect=RuntimeError("VK video.save response is missing upload_url"))

    result = await adapter.send_video("2000000001", str(video), caption="caption")

    assert result.success is False
    assert "missing upload_url" in (result.error or "")


def test_vk_retryable_error_classifier_is_narrow():
    assert _is_retryable_vk_error(RuntimeError('VK upload failed: "unknown error"')) is True
    assert _is_retryable_vk_error(RuntimeError('VK API error 6: Too many requests per second')) is True
    assert _is_retryable_vk_error(RuntimeError('VK API error 5: User authorization failed')) is False
    assert _is_retryable_vk_error(RuntimeError('VK docs upload server response is missing upload_url')) is False


@pytest.mark.asyncio
async def test_vk_voice_upload_retries_once_on_transient_upload_error(tmp_path):
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"ogg")
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))
    calls = []

    async def fake_vk_method(method, params=None):
        calls.append((method, params))
        if method == "docs.getMessagesUploadServer":
            return {"response": {"upload_url": "https://upload.example/doc"}}
        if method == "docs.save":
            return {"response": {"audio_message": {"owner_id": -123456789, "id": 22, "access_key": "ak"}}}
        if method == "messages.send":
            assert params is not None
            assert params["attachment"] == "doc-123456789_22_ak"
            return {"response": 123}
        raise AssertionError(method)

    adapter._vk_method = fake_vk_method
    upload = AsyncMock(side_effect=[RuntimeError('VK upload failed: "unknown error"'), {"file": "server-payload"}])
    with patch("plugins.platforms.vk.adapter._multipart_upload_async", upload), \
         patch("plugins.platforms.vk.adapter.asyncio.sleep", AsyncMock()) as sleep:
        result = await adapter.send_voice("2000000001", str(audio), caption="caption")

    assert result.success is True
    assert result.message_id == "123"
    assert upload.await_count == 2
    sleep.assert_awaited_once_with(5.0)
    assert [method for method, _params in calls].count("docs.getMessagesUploadServer") == 2


@pytest.mark.asyncio
async def test_vk_voice_upload_does_not_retry_non_retryable_error(tmp_path):
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"ogg")
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))

    async def fake_vk_method(method, params=None):
        if method == "docs.getMessagesUploadServer":
            return {"response": {"upload_url": "https://upload.example/doc"}}
        raise AssertionError(method)

    adapter._vk_method = fake_vk_method
    upload = AsyncMock(side_effect=RuntimeError("VK upload returned non-JSON response: '<html>'"))
    with patch("plugins.platforms.vk.adapter._multipart_upload_async", upload), \
         patch("plugins.platforms.vk.adapter.asyncio.sleep", AsyncMock()) as sleep:
        result = await adapter.send_voice("2000000001", str(audio), caption="caption")

    assert result.success is False
    assert upload.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_vk_send_attachment_retries_once_on_transient_send_error():
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))
    attempts = 0

    async def fake_vk_method(method, params=None):
        nonlocal attempts
        attempts += 1
        assert method == "messages.send"
        if attempts == 1:
            raise RuntimeError("VK API error 6: Too many requests per second")
        return {"response": 123}

    adapter._vk_method = fake_vk_method
    with patch("plugins.platforms.vk.adapter.asyncio.sleep", AsyncMock()) as sleep:
        result = await adapter._send_attachment("2000000001", "doc-1_2", "caption")

    assert result.success is True
    assert result.message_id == "123"
    assert attempts == 2
    sleep.assert_awaited_once_with(5.0)


def test_vk_format_message_converts_markdown_to_readable_plain_text():
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))

    formatted = adapter.format_message(
        "## Заголовок\n"
        "**Жирный** и *курсив*, ~~зачёркнуто~~, `код`, ||спойлер||.\n"
        "Ссылка: [пример](https://example.com/path_a).\n"
        "![обложка](https://example.com/pic.png)\n"
        "> цитата\n"
        "```python\nprint('ok')\n```"
    )

    assert "**" not in formatted
    assert "```" not in formatted
    assert "##" not in formatted
    assert "Заголовок" in formatted
    assert "Жирный и курсив" in formatted
    assert "пример (https://example.com/path_a)" in formatted
    assert "обложка: https://example.com/pic.png" in formatted
    assert "цитата" in formatted
    assert "print('ok')" in formatted


@pytest.mark.asyncio
async def test_vk_send_plainifies_markdown_before_messages_send():
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))
    calls = []

    async def fake_vk_method(method: str, params: dict[str, Any] | None = None, *, access_token: str | None = None):
        calls.append((method, params, access_token))
        assert method == "messages.send"
        assert params is not None
        assert params["message"] == "Важное: текст (https://example.com)"
        return {"response": 123}

    adapter._vk_method = fake_vk_method

    result = await adapter.send("2000000042", "**Важное**: [текст](https://example.com)")

    assert result.success is True
    assert result.message_id == "123"
    assert calls


@pytest.mark.asyncio
async def test_vk_send_uses_peer_ids_response_cmid_for_editable_progress():
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))
    calls = []

    async def fake_vk_method(method, params=None):
        calls.append((method, params))
        assert method == "messages.send"
        assert params is not None
        assert params["peer_ids"] == "2000000042"
        assert "peer_id" not in params
        return {"response": [{"peer_id": 2000000042, "message_id": 0, "conversation_message_id": 141}]}

    adapter._vk_method = fake_vk_method

    result = await adapter.send("2000000042", "progress")

    assert result.success is True
    assert result.message_id == "cmid:141"
    assert calls


@pytest.mark.asyncio
async def test_vk_send_response_zero_is_not_treated_as_editable_message_id():
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))

    async def fake_vk_method(method, params=None):
        assert method == "messages.send"
        return {"response": 0}

    adapter._vk_method = fake_vk_method

    result = await adapter.send("2000000042", "progress")

    assert result.success is True
    assert result.message_id is None


@pytest.mark.asyncio
async def test_vk_edit_message_uses_conversation_message_id_for_cmid():
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))
    calls = []

    async def fake_vk_method(method, params=None):
        calls.append((method, params))
        return {"response": 1}

    adapter._vk_method = fake_vk_method

    result = await adapter.edit_message("2000000042", "cmid:141", "progress edited")

    assert result.success is True
    assert calls == [
        (
            "messages.edit",
            {"peer_id": "2000000042", "message": "progress edited", "conversation_message_id": "141"},
        )
    ]


@pytest.mark.asyncio
async def test_vk_edit_message_rejects_non_editable_zero_id_without_api_call():
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))
    adapter._vk_method = AsyncMock()

    result = await adapter.edit_message("2000000042", "0", "progress")

    assert result.success is False
    assert result.retryable is False
    assert "editable" in (result.error or "")
    adapter._vk_method.assert_not_called()


@pytest.mark.asyncio
async def test_vk_send_restores_project_keyboard_for_lane_chat(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "project_lanes": {"2000000042": {"lanes": [{"id": "gito", "name": "Gito"}]}},
            },
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method

    result = await adapter.send("2000000042", "Stopped.")

    assert result.success is True
    send_params = [params for method, params in calls if method == "messages.send"][-1]
    keyboard = json.loads(send_params["keyboard"])
    labels = [button["action"].get("label") for row in keyboard["buttons"] for button in row]
    assert keyboard["inline"] is False
    assert "Проекты" in labels
    assert "Команды" in labels
    assert "Меню" not in labels


@pytest.mark.asyncio
async def test_vk_send_remembers_lane_for_thread_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {"2000000042": {"lanes": [{"id": "topic-42", "name": "Health"}]}},
            },
        )
    )

    async def fake_vk_method(method, params=None, **_kwargs):
        assert params is not None
        assert params["keyboard"]
        return {"response": [{"conversation_message_id": 77}]}

    adapter._vk_method = fake_vk_method

    result = await adapter.send("2000000042", "health cron", metadata={"thread_id": "lane:topic-42"})

    assert result.success is True
    assert adapter._lane_state["message_lanes"]["2000000042:77"] == "topic-42"


@pytest.mark.asyncio
async def test_vk_reply_to_remembered_cron_message_routes_to_lane(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "group_id": "123456789",
                "allowed_peers": ["2000000042"],
                "project_lanes": {
                    "2000000042": {
                        "lanes": [
                            {"id": "topic-42", "name": "Health", "skills": ["my-health"]},
                            {"id": "search-analytics", "name": "Поиск и аналитика", "skills": ["perplex"]},
                        ]
                    }
                },
            },
        )
    )
    adapter._lane_state.setdefault("active", {})["2000000042:100"] = "search-analytics"
    adapter._lane_state.setdefault("message_lanes", {})["2000000042:77"] = "topic-42"
    adapter.handle_message = AsyncMock()
    adapter._enrich_message_from_api = AsyncMock(side_effect=lambda msg, **_kwargs: msg)

    await adapter._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": 100,
                    "peer_id": 2000000042,
                    "conversation_message_id": 88,
                    "text": "готово",
                    "reply_message": {"conversation_message_id": 77, "text": "health cron"},
                }
            },
        }
    )

    assert adapter.handle_message.await_args is not None
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "thread"
    assert event.source.thread_id == "lane:topic-42"
    assert event.source.chat_topic == "Health"
    assert event.auto_skill == ["my-health"]


@pytest.mark.asyncio
async def test_vk_send_attaches_project_keyboard_for_allowed_chat_without_lanes(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"group_id": "123456789", "allowed_peers": ["2000000099"]},
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method

    result = await adapter.send("2000000099", "hello")

    assert result.success is True
    send_params = [params for method, params in calls if method == "messages.send"][-1]
    labels = [button["action"].get("label") for row in json.loads(send_params["keyboard"])["buttons"] for button in row]
    assert labels == ["Проекты", "Новый проект", "Команды"]


@pytest.mark.asyncio
async def test_vk_send_attaches_project_keyboard_for_allowed_dm_without_lanes(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"group_id": "123456789", "allowed_users": ["100"]},
        )
    )
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method

    result = await adapter.send("100", "hello")

    assert result.success is True
    send_params = [params for method, params in calls if method == "messages.send"][-1]
    labels = [button["action"].get("label") for row in json.loads(send_params["keyboard"])["buttons"] for button in row]
    assert labels == ["Проекты", "Новый проект", "Команды"]


@pytest.mark.asyncio
async def test_vk_send_does_not_attach_project_keyboard_for_unlisted_peer(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.vk.adapter.get_hermes_home", lambda: tmp_path)
    adapter = VKAdapter(PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"}))
    calls = []

    async def fake_vk_method(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"response": [{"conversation_message_id": 1}]}

    adapter._vk_method = fake_vk_method

    result = await adapter.send("2000000099", "hello")

    assert result.success is True
    send_params = [params for method, params in calls if method == "messages.send"][-1]
    assert "keyboard" not in send_params


@pytest.mark.asyncio
async def test_vk_standalone_send_accepts_platform_config_contract(monkeypatch):
    calls = []

    async def fake_http_json_async(url, params):
        calls.append((url, params))
        return {"response": 321}

    monkeypatch.setattr("plugins.platforms.vk.adapter._http_json_async", fake_http_json_async)
    pconfig = PlatformConfig(enabled=True, token="test-token", extra={"group_id": "123456789"})

    result = await _standalone_send(pconfig, "2000000042", "hello")

    assert result == {"success": True, "message_id": 321}
    assert calls[0][1]["access_token"] == "test-token"
    assert calls[0][1]["peer_ids"] == "2000000042"
    assert calls[0][1]["message"] == "hello"


@pytest.mark.asyncio
async def test_vk_standalone_send_keeps_legacy_two_argument_shape(monkeypatch):
    calls = []

    async def fake_http_json_async(url, params):
        calls.append((url, params))
        return {"response": 654}

    monkeypatch.setenv("VK_GROUP_TOKEN", "env-token")
    monkeypatch.setattr("plugins.platforms.vk.adapter._http_json_async", fake_http_json_async)

    result = await _standalone_send("2000000042", "legacy")

    assert result == {"success": True, "message_id": 654}
    assert calls[0][1]["access_token"] == "env-token"
    assert calls[0][1]["peer_ids"] == "2000000042"
    assert calls[0][1]["message"] == "legacy"


# ── Message-reaction ack lifecycle (👌 → 👍/👎) ─────────────────────────────


def _register_vk_platform_once():
    """Register 'vk' in the platform registry so Platform('vk') resolves.

    Mirrors what the gateway's plugin loader does; needed because these tests
    exercise the adapter directly without the plugin loader present.
    """
    from gateway.config import Platform
    from gateway.platform_registry import PlatformEntry, platform_registry

    if platform_registry.is_registered("vk"):
        return

    module = sys.modules["plugins.platforms.vk.adapter"]

    platform_registry.register(
        PlatformEntry(
            name="vk",
            label="VK",
            adapter_factory=module.VKAdapter,
            check_fn=lambda: True,
            source="plugin",
            plugin_name="vk-platform",
        )
    )


@pytest.fixture(scope="module", autouse=True)
def vk_platform_registered():
    _register_vk_platform_once()
    yield


def _reaction_platform():
    return VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"})).platform


def _reaction_adapter(monkeypatch, **extra):
    """Adapter with reactions configured and VK API calls captured."""
    calls = []

    async def fake_http_json_async(url, params):
        method = url.rsplit("/", 1)[-1]
        calls.append((method, dict(params)))
        if method == "messages.deleteReaction":
            return {"response": 1}
        return {"response": 1}

    monkeypatch.setattr("plugins.platforms.vk.adapter._http_json_async", fake_http_json_async)
    adapter = VKAdapter(
        PlatformConfig(
            enabled=True,
            extra={"group_id": "123456789", "reactions_enabled": True},
        )
    )
    return adapter, calls


def _reaction_event():
    source = SessionSource(
        platform=_reaction_platform(),
        chat_id="100",
        chat_name="Test User",
        chat_type="dm",
        user_id="100",
    )
    return MessageEvent(
        text="ping",
        message_type=MessageType.TEXT,
        source=source,
        raw_message={},
        message_id="7",
    )


@pytest.mark.asyncio
async def test_vk_reactions_disabled_by_default(monkeypatch):
    calls = []

    async def fake_http_json_async(url, params):
        calls.append((url.rsplit("/", 1)[-1], dict(params)))
        return {"response": 1}

    monkeypatch.setattr("plugins.platforms.vk.adapter._http_json_async", fake_http_json_async)
    adapter = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"}))
    event = _reaction_event()

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert calls == []
    assert adapter.reactions_enabled is False


@pytest.mark.asyncio
async def test_vk_reactions_progress_then_ok(monkeypatch):
    adapter, calls = _reaction_adapter(monkeypatch)
    event = _reaction_event()

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert calls[0][0] == "messages.sendReaction"
    assert calls[0][1]["peer_id"] == 100
    assert calls[0][1]["cmid"] == 7
    assert calls[0][1]["reaction_id"] == 10  # progress = 👌
    assert calls[1][0] == "messages.sendReaction"
    assert calls[1][1]["reaction_id"] == 4  # OK = 👍


@pytest.mark.asyncio
async def test_vk_reactions_failure_sets_fail_reaction(monkeypatch):
    adapter, calls = _reaction_adapter(monkeypatch)
    event = _reaction_event()

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    assert calls[0][1]["reaction_id"] == 10  # progress = 👌
    assert calls[1][1]["reaction_id"] == 8  # FAIL default


@pytest.mark.asyncio
async def test_vk_reactions_cancelled_removes_progress(monkeypatch):
    adapter, calls = _reaction_adapter(monkeypatch)
    event = _reaction_event()

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    assert calls[0][0] == "messages.sendReaction"
    assert calls[1][0] == "messages.deleteReaction"
    assert calls[1][1]["peer_id"] == 100
    assert calls[1][1]["cmid"] == 7


@pytest.mark.asyncio
async def test_vk_reactions_custom_ids_from_env(monkeypatch):
    monkeypatch.setenv("VK_REACTION_PROGRESS", "16")
    monkeypatch.setenv("VK_REACTION_OK", "2")
    monkeypatch.setenv("VK_REACTION_FAIL", "3")
    adapter, calls = _reaction_adapter(monkeypatch)
    event = _reaction_event()

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert adapter.reaction_progress == 16
    assert calls[0][1]["reaction_id"] == 16
    assert calls[1][1]["reaction_id"] == 2


@pytest.mark.asyncio
async def test_vk_reactions_invalid_env_ids_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("VK_REACTION_PROGRESS", "not-an-int")
    monkeypatch.setenv("VK_REACTION_OK", "-1")
    monkeypatch.setenv("VK_REACTION_FAIL", "also-not-an-int")
    adapter, calls = _reaction_adapter(monkeypatch)
    event = _reaction_event()

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    assert adapter.reaction_progress == 10
    assert adapter.reaction_ok == 4
    assert adapter.reaction_fail == 8
    assert calls[0][1]["reaction_id"] == 10
    assert calls[1][1]["reaction_id"] == 8


@pytest.mark.asyncio
async def test_vk_reactions_send_reaction_soft_fails(monkeypatch):
    async def fake_http_json_async(url, params):
        return {"error": {"error_code": 1009, "error_msg": "Unknown reaction passed"}}

    monkeypatch.setattr("plugins.platforms.vk.adapter._http_json_async", fake_http_json_async)
    adapter = VKAdapter(
        PlatformConfig(enabled=True, extra={"group_id": "123456789", "reactions_enabled": True})
    )
    event = _reaction_event()

    # Must not raise — reactions are cosmetic.
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)


@pytest.mark.asyncio
async def test_vk_reactions_skipped_without_message_id(monkeypatch):
    adapter, calls = _reaction_adapter(monkeypatch)
    event = _reaction_event()
    event.message_id = None

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert calls == []
