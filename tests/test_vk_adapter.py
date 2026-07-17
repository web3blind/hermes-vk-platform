import os
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from gateway.session import SessionSource, build_session_key
from adapter import (
    VKAdapter,
    _download_attachment,
    _apply_yaml_config,
    _env_enablement,
    _is_retryable_vk_error,
    _looks_like_downloadable_attachment_url,
    _split_csv,
)


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
    ):
        monkeypatch.delenv(key, raising=False)


def test_split_csv_accepts_strings_and_iterables():
    assert _split_csv("1, 2,,3 ") == {"1", "2", "3"}
    assert _split_csv(["4", 5, ""]) == {"4", "5"}


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


def test_vk_user_token_falls_back_to_vkblog_alias(monkeypatch):
    monkeypatch.setenv("VKBLOG_USER_TOKEN", "vkblog-token")

    adapter = VKAdapter(PlatformConfig(enabled=True, extra={"group_id": "123456789"}))

    assert adapter.user_token == "vkblog-token"


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
        "adapter._download_attachment_async",
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

    with patch("adapter._download_attachment_async", AsyncMock(side_effect=fake_download)) as download:
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

    monkeypatch.setattr("adapter.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("adapter.urllib.request.urlopen", lambda *_args, **_kwargs: Response())

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

    monkeypatch.setattr("adapter.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("adapter.urllib.request.urlopen", lambda *_args, **_kwargs: Response())

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
    with patch("adapter._download_attachment_async", AsyncMock()) as download:
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
    with patch("adapter._multipart_upload_async", AsyncMock(return_value={"photo": "server-payload"})):
        result = await adapter.send_image_file("2000000001", str(image), caption="caption")

    assert result.success is True
    assert result.message_id == "123"


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
    with patch("adapter._multipart_upload_async", upload), \
         patch("adapter.asyncio.sleep", AsyncMock()) as sleep:
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
    with patch("adapter._multipart_upload_async", upload), \
         patch("adapter.asyncio.sleep", AsyncMock()) as sleep:
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
    with patch("adapter.asyncio.sleep", AsyncMock()) as sleep:
        result = await adapter._send_attachment("2000000001", "doc-1_2", "caption")

    assert result.success is True
    assert result.message_id == "123"
    assert attempts == 2
    sleep.assert_awaited_once_with(5.0)


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
