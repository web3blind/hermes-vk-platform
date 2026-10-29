"""Private-chat peer override restores text/voice without disabling other gates."""
from unittest.mock import AsyncMock

import pytest

from test_vk_adapter import (VKAdapter, PlatformConfig, intake_update, drain_intake,
                             clear_vk_env)  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [False, True], ids=["text", "voice"])
async def test_private_peer_override_restores_intake_and_logs_global_rejection(voice, caplog):
    peer = "2000000042"
    adapter = VKAdapter(PlatformConfig(enabled=True, extra={
        "allowed_peers": [peer, "2000000043"],
        "require_mention": True,
        "require_mention_by_peer": {peer: False},
    }))
    adapter._vk_method = AsyncMock(return_value={"response": {}})
    adapter.handle_message = AsyncMock()
    adapter._materialize_inbound_media = AsyncMock(return_value=["/isolated/voice.ogg"])
    update = intake_update(1, "" if voice else "private text")
    if voice:
        update["object"]["message"]["attachments"] = [{
            "type": "audio_message", "audio_message": {
                "link_ogg": "https://example.com/voice.ogg", "duration": 3,
            },
        }]
    with caplog.at_level("INFO"):
        await adapter._handle_update(intake_update(2, "private text", peer=2000000043))
        await adapter._handle_update(update)
        await drain_intake(adapter)
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_id == peer
    assert event.source.role_authorized is True
    assert await adapter.reauthorize_native_roles(event.source) is True
    adapter.allowed_peers.remove(peer)
    assert await adapter.reauthorize_native_roles(event.source) is False
    if voice:
        assert event.message_type.value in {"voice", "audio"}
        assert event.media_urls == ["/isolated/voice.ogg"]
        adapter._materialize_inbound_media.assert_awaited_once()
    else:
        assert event.text == "private text"
    assert "ignoring unaddressed message peer=2000000043 cmid=2" in caplog.text
    assert "private text" not in caplog.text
