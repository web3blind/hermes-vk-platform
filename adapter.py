"""VK Messenger platform adapter for Hermes Agent.

MVP transport:
- inbound: VK community Group Long Poll (`message_new` events)
- outbound: VK `messages.send`

The adapter is intentionally plugin-only: it registers through
`ctx.register_platform()` and does not require gateway core changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home
from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    resolve_channel_prompt,
    resolve_channel_skills,
)

logger = logging.getLogger(__name__)

VK_API_VERSION = "5.199"
VK_API_BASE = "https://api.vk.com/method"
DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
DEFAULT_DEDUPE_TTL_SECONDS = 30 * 60
VK_TRANSIENT_RETRY_DELAY_SECONDS = 5.0
VALID_ACCESS_POLICIES = {"any", "user_only", "peer_only", "peer_and_user"}
DOWNLOADABLE_MIME_PREFIXES = ("image/", "video/", "audio/", "application/pdf", "application/octet-stream")


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _split_csv(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value).split(",")
    return {str(item).strip() for item in items if str(item).strip()}


def _parse_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_users_by_peer(value: Any) -> dict[str, set[str]]:
    """Parse per-peer user allowlists from config/env.

    YAML config may provide ``{peer_id: [user_id, ...]}``. Environment values
    use ``peer:user|user;peer:user`` to avoid introducing a JSON parser burden
    in setup examples.
    """
    result: dict[str, set[str]] = {}
    if not value:
        return result
    if isinstance(value, dict):
        for peer, users in value.items():
            peer_id = str(peer).strip()
            parsed_users = _split_csv(users)
            if peer_id and parsed_users:
                result[peer_id] = parsed_users
        return result
    for chunk in str(value).split(";"):
        if ":" not in chunk:
            continue
        peer, users = chunk.split(":", 1)
        peer_id = peer.strip()
        parsed_users = {item.strip() for item in users.replace(",", "|").split("|") if item.strip()}
        if peer_id and parsed_users:
            result[peer_id] = parsed_users
    return result


def _redact_token(text: str) -> str:
    for token in (os.getenv("VK_GROUP_TOKEN", ""), os.getenv("VK_USER_TOKEN", ""), os.getenv("VKBLOG_USER_TOKEN", "")):
        if token:
            text = text.replace(token, "[REDACTED]")
    return text


def _http_json(url: str, params: Optional[dict[str, Any]] = None, *, timeout: int = 35) -> dict[str, Any]:
    """Perform a blocking GET/POST-like request and return decoded JSON.

    VK API accepts query/form params for these methods. We use stdlib only to
    avoid adding dependencies to the gateway process.
    """
    data = None
    if params is not None:
        data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Hermes-VK-Adapter/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed HTTPS APIs
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(_redact_token(str(exc))) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"VK returned non-JSON response: {raw[:200]!r}") from exc
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        msg = err.get("error_msg") if isinstance(err, dict) else str(err)
        code = err.get("error_code") if isinstance(err, dict) else "unknown"
        raise RuntimeError(_redact_token(f"VK API error {code}: {msg}"))
    return payload


async def _http_json_async(url: str, params: Optional[dict[str, Any]] = None, *, timeout: int = 35) -> dict[str, Any]:
    return await asyncio.to_thread(_http_json, url, params, timeout=timeout)


def _multipart_upload(url: str, field_name: str, file_path: str, *, timeout: int = 120) -> dict[str, Any]:
    """Upload one local file as multipart/form-data and return decoded JSON."""
    path = Path(file_path)
    boundary = f"----HermesVK{uuid.uuid4().hex}"
    filename = path.name or "file"
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = header + path.read_bytes() + footer
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": "Hermes-VK-Adapter/0.1",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - VK-provided HTTPS upload URL
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(_redact_token(str(exc))) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"VK upload returned non-JSON response: {raw[:200]!r}") from exc
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(_redact_token(str(payload.get("error"))))
    return payload


async def _multipart_upload_async(url: str, field_name: str, file_path: str, *, timeout: int = 120) -> dict[str, Any]:
    return await asyncio.to_thread(_multipart_upload, url, field_name, file_path, timeout=timeout)


def _is_retryable_vk_error(exc: Exception) -> bool:
    """Return whether an outbound VK failure is worth one delayed retry.

    VK upload endpoints can occasionally return a bare ``"unknown error"``
    despite the same file/peer succeeding moments later.  Retry only transient
    transport/server/rate-limit shapes; never retry auth, permission, missing
    upload URL, malformed response, or local validation errors.
    """
    text = str(exc).lower()
    retryable_markers = (
        "unknown error",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "temporary failure",
        "connection reset",
        "remote end closed connection",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "too many requests",
        "too many requests per second",
        "vk api error 6:",
        "vk api error 10:",
    )
    non_retryable_markers = (
        "vk api error 5:",   # auth failed
        "vk api error 7:",   # permission denied
        "vk api error 15:",  # access denied
        "vk api error 100:", # invalid parameter
        "missing upload_url",
        "missing owner_id/id",
        "non-json response",
        "too large",
        "exceeded size limit",
    )
    return any(marker in text for marker in retryable_markers) and not any(
        marker in text for marker in non_retryable_markers
    )


async def _retry_vk_transient_once(label: str, operation):
    try:
        return await operation()
    except Exception as exc:
        if not _is_retryable_vk_error(exc):
            raise
        logger.warning(
            "VK: transient %s failed (%s); retrying once in %.0fs",
            label,
            _redact_token(str(exc)),
            VK_TRANSIENT_RETRY_DELAY_SECONDS,
        )
        await asyncio.sleep(VK_TRANSIENT_RETRY_DELAY_SECONDS)
        return await operation()


def _download_attachment(
    url: str,
    media_type: str = "",
    *,
    timeout: int = 60,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
) -> str:
    """Download a VK attachment to a bounded local cache path.

    Core gateway enrichers (STT, image/video context) expect local files. VK
    Long Poll gives direct HTTPS attachment URLs, so voice messages must be
    materialized locally before dispatching ``MessageEvent``.  Downloads are
    capped and streamed so a hostile attachment cannot be read fully into RAM.
    """
    parsed = urllib.parse.urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if not suffix:
        guessed = mimetypes.guess_extension(media_type or "") or ""
        suffix = guessed if guessed in {".ogg", ".mp3", ".wav", ".jpg", ".jpeg", ".png", ".mp4", ".pdf"} else ".bin"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    cache_dir = get_hermes_home() / "cache" / "vk" / "attachments"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"vk_{digest}{suffix}"

    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-VK-Adapter/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - VK-provided attachment URL
            content_length = resp.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise RuntimeError(f"VK attachment is too large: {content_length} bytes > {max_bytes}")
                except ValueError:
                    pass
            content_type = (resp.headers.get_content_type() or "").lower()
            declared_type = (media_type or "").lower()
            if declared_type and not declared_type.startswith(DOWNLOADABLE_MIME_PREFIXES):
                raise RuntimeError(f"VK attachment media type is not allowed: {declared_type}")
            if content_type and content_type != "application/octet-stream":
                declared_top = declared_type.split("/", 1)[0] if "/" in declared_type else ""
                content_top = content_type.split("/", 1)[0] if "/" in content_type else ""
                if declared_top in {"image", "video", "audio"} and content_top != declared_top:
                    raise RuntimeError(f"VK attachment content type mismatch: {content_type} for {declared_type}")
            total = 0
            with target.open("wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        fh.close()
                        target.unlink(missing_ok=True)
                        raise RuntimeError(f"VK attachment exceeded size limit: {total} bytes > {max_bytes}")
                    fh.write(chunk)
    except urllib.error.URLError as exc:
        raise RuntimeError(_redact_token(str(exc))) from exc
    return str(target)


async def _download_attachment_async(
    url: str,
    media_type: str = "",
    *,
    timeout: int = 60,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
) -> str:
    return await asyncio.to_thread(_download_attachment, url, media_type, timeout=timeout, max_bytes=max_bytes)


def _looks_like_downloadable_attachment_url(url: str) -> bool:
    """Return whether a VK media URL points to a downloadable file-like resource."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    # A regular VK video attachment may only expose a watch page such as
    # https://vk.com/video-1_2. Downloading that HTML page as video would make
    # the gateway lie to the agent about a local media file. Keep it as a URL.
    if host in {"vk.com", "m.vk.com"} and parsed.path.startswith("/video"):
        return False
    return True


def _markdown_to_vk_plain_text(content: str) -> str:
    """Convert common Markdown into readable VK plain text.

    VK's regular ``messages.send`` API does not expose Telegram-like
    ``parse_mode`` for Markdown/HTML. Sending raw agent Markdown makes VK users
    see literal ``**bold**`` markers, so preserve the words and links while
    dropping presentation-only markers.
    """
    text = str(content or "")
    if not text:
        return ""

    # Keep code content but drop fenced-code language/fences.
    text = re.sub(r"```[ \t]*[A-Za-z0-9_+.#-]*\n?", "", text)
    text = text.replace("```", "")

    # Markdown images/links become readable text with URL when useful.
    text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", lambda m: f"{m.group(1).strip() or 'изображение'}: {m.group(2)}", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", lambda m: f"{m.group(1).strip()} ({m.group(2)})", text)

    # Headings and blockquotes are readable without their Markdown markers.
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^>\s?", "", text)

    # Common inline emphasis/decoration markers. Use conservative patterns so
    # normal underscores inside identifiers/URLs are left alone.
    replacements = [
        (r"\*\*\*([^*\n]+)\*\*\*", r"\1"),
        (r"\*\*([^*\n]+)\*\*", r"\1"),
        (r"__([^_\n]+)__", r"\1"),
        (r"~~([^~\n]+)~~", r"\1"),
        (r"\|\|([^|\n]+)\|\|", r"\1"),
        (r"`([^`\n]+)`", r"\1"),
        (r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1"),
        (r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1"),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text)

    # Telegram-style copyable code blocks may leave indentation intact; that is
    # fine for VK. Only trim trailing spaces introduced by marker removal.
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    return text.strip()


class VKAdapter(BasePlatformAdapter):
    """VK community messages adapter backed by Group Long Poll."""

    enforces_own_access_policy = True

    def __init__(self, config, **_: Any) -> None:
        super().__init__(config=config, platform=Platform("vk"))
        extra = getattr(config, "extra", {}) or {}

        self.token = os.getenv("VK_GROUP_TOKEN") or getattr(config, "token", None) or extra.get("group_token", "")
        # Optional user token for VK API methods that are not available to
        # community tokens (notably ``video.get``). It is used only inside the
        # VK plugin as a native API fallback, never exposed to the agent.
        self.user_token = os.getenv("VK_USER_TOKEN") or os.getenv("VKBLOG_USER_TOKEN") or extra.get("user_token", "")
        self.group_id = str(os.getenv("VK_GROUP_ID") or extra.get("group_id", "")).lstrip("-")
        self.api_version = str(os.getenv("VK_API_VERSION") or extra.get("api_version", VK_API_VERSION))

        self.allowed_users = _split_csv(os.getenv("VK_ALLOWED_USERS") or extra.get("allowed_users"))
        self.allowed_peers = _split_csv(os.getenv("VK_ALLOWED_PEERS") or extra.get("allowed_peers"))
        self.allowed_users_by_peer = _parse_users_by_peer(
            os.getenv("VK_ALLOWED_USERS_BY_PEER") or extra.get("allowed_users_by_peer")
        )
        self.allow_all_users = _truthy(os.getenv("VK_ALLOW_ALL_USERS") or extra.get("allow_all_users"))
        self.access_policy = str(os.getenv("VK_ACCESS_POLICY") or extra.get("access_policy") or "any").strip().lower()
        self.dedupe_ttl_seconds = _parse_positive_int(
            os.getenv("VK_DEDUPE_TTL_SECONDS") or extra.get("dedupe_ttl_seconds"),
            DEFAULT_DEDUPE_TTL_SECONDS,
        )
        self.max_attachment_bytes = _parse_positive_int(
            os.getenv("VK_MAX_ATTACHMENT_BYTES") or extra.get("max_attachment_bytes"),
            DEFAULT_MAX_ATTACHMENT_BYTES,
        )
        access_policy = "allowlist" if self.allowed_users or self.allowed_peers or self.allowed_users_by_peer else "open"
        self._dm_policy = access_policy
        self._group_policy = access_policy

        self.home_channel = str(os.getenv("VK_HOME_CHANNEL") or extra.get("home_channel") or "").strip()
        self.max_message_length = int(extra.get("max_message_length") or 4096)
        self.download_attachments = _truthy(os.getenv("VK_DOWNLOAD_ATTACHMENTS") or extra.get("download_attachments"))

        self._poll_task: Optional[asyncio.Task] = None
        self._lp_server = ""
        self._lp_key = ""
        self._lp_ts = ""
        self._lock_key: Optional[str] = None
        self._conversation_context_cache: dict[str, tuple[str, str]] = {}
        self._seen_message_keys: dict[str, float] = {}

    @property
    def name(self) -> str:
        return "VK Messenger"

    def format_message(self, content: str) -> str:
        return _markdown_to_vk_plain_text(content)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.token or not self.group_id:
            logger.error("VK: VK_GROUP_TOKEN and VK_GROUP_ID must be configured")
            self._set_fatal_error("config_missing", "VK_GROUP_TOKEN and VK_GROUP_ID must be set", retryable=False)
            return False

        try:
            from gateway.status import acquire_scoped_lock

            lock_key = self.group_id
            if not acquire_scoped_lock("vk", lock_key):
                self._set_fatal_error("lock_conflict", "VK group is already used by another profile", retryable=False)
                return False
            self._lock_key = lock_key
        except ImportError:
            self._lock_key = None

        try:
            await self._refresh_longpoll_server()
        except Exception as exc:
            logger.error("VK: failed to initialize long poll — %s", _redact_token(str(exc)))
            self._set_fatal_error("longpoll_init_failed", str(exc), retryable=True)
            await self._release_lock()
            return False

        self._poll_task = asyncio.create_task(self._poll_loop(), name="vk-longpoll")
        self._mark_connected()
        logger.info("VK: connected to group %s via long poll", self.group_id)
        return True

    async def disconnect(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        self._mark_disconnected()
        await self._release_lock()

    async def _release_lock(self) -> None:
        if not self._lock_key:
            return
        try:
            from gateway.status import release_scoped_lock

            release_scoped_lock("vk", self._lock_key)
        except ImportError:
            pass
        finally:
            self._lock_key = None

    async def _vk_method(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        access_token: Optional[str] = None,
    ) -> dict[str, Any]:
        merged = dict(params or {})
        merged.setdefault("access_token", access_token or self.token)
        merged.setdefault("v", self.api_version)
        return await _http_json_async(f"{VK_API_BASE}/{method}", merged)

    async def _refresh_longpoll_server(self) -> None:
        payload = await self._vk_method("groups.getLongPollServer", {"group_id": self.group_id})
        response = payload.get("response") or {}
        self._lp_server = str(response.get("server") or "")
        self._lp_key = str(response.get("key") or "")
        self._lp_ts = str(response.get("ts") or "")
        if not self._lp_server or not self._lp_key or not self._lp_ts:
            raise RuntimeError("VK long poll server response is missing server/key/ts")

    async def _poll_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                params = {
                    "act": "a_check",
                    "key": self._lp_key,
                    "ts": self._lp_ts,
                    "wait": 25,
                }
                data = await _http_json_async(self._lp_server, params, timeout=40)

                if "failed" in data:
                    failed = int(data.get("failed") or 0)
                    if failed in {1, 2} and data.get("ts"):
                        self._lp_ts = str(data["ts"])
                    else:
                        await self._refresh_longpoll_server()
                    continue

                self._lp_ts = str(data.get("ts") or self._lp_ts)
                for update in data.get("updates") or []:
                    await self._handle_update(update)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("VK: long poll error — %s", _redact_token(str(exc)))
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 60.0)
                try:
                    await self._refresh_longpoll_server()
                except Exception:
                    pass

    async def _handle_update(self, update: dict[str, Any]) -> None:
        if update.get("type") != "message_new":
            return
        obj = update.get("object") or {}
        msg = obj.get("message") or obj
        if not isinstance(msg, dict):
            return

        # VK uses from_id < 0 for communities. Ignore bot/community echoes.
        from_id = str(msg.get("from_id") or "")
        if from_id.startswith("-"):
            return

        peer_id = str(msg.get("peer_id") or "")
        if not self._is_authorized(from_id=from_id, peer_id=peer_id):
            logger.info("VK: ignoring unauthorized sender=%s peer=%s", from_id, peer_id)
            return

        conversation_message_id = msg.get("conversation_message_id") or msg.get("id")
        if self._is_duplicate_event(peer_id=peer_id, conversation_message_id=conversation_message_id):
            logger.info("VK: ignoring duplicate event peer=%s cmid=%s", peer_id, conversation_message_id)
            return
        msg = await self._enrich_message_from_api(msg, peer_id=peer_id, conversation_message_id=conversation_message_id)

        text = str(msg.get("text") or "").strip()
        attachments = msg.get("attachments") or []
        media_type, media_urls, media_types = self._extract_attachment_media(attachments)
        if media_urls:
            media_urls = await self._materialize_inbound_media(media_type, media_urls, media_types)
        attachment_summary = self._summarize_attachments(attachments) if attachments else ""
        if attachment_summary:
            text = self._merge_caption(text, attachment_summary)
        forwarded_summary = self._summarize_forwarded_messages(msg)
        if forwarded_summary:
            text = self._merge_caption(text, forwarded_summary)
        if not text and not media_urls:
            return

        chat_type = "group" if peer_id.startswith("200000") else "dm"
        chat_name, chat_topic = await self._resolve_conversation_context(peer_id, chat_type=chat_type)
        user_name = f"VK user {from_id}"

        source = self.build_source(
            chat_id=peer_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=from_id,
            user_name=user_name,
            chat_topic=chat_topic,
            message_id=str(conversation_message_id) if conversation_message_id else None,
        )
        event = MessageEvent(
            text=text,
            message_type=media_type,
            source=source,
            raw_message=update,
            message_id=str(conversation_message_id) if conversation_message_id else None,
            media_urls=media_urls,
            media_types=media_types,
            auto_skill=resolve_channel_skills(self.config.extra, peer_id),
            channel_prompt=resolve_channel_prompt(self.config.extra, peer_id),
        )
        await self.handle_message(event)

    async def _enrich_message_from_api(
        self,
        msg: dict[str, Any],
        *,
        peer_id: str,
        conversation_message_id: Any,
    ) -> dict[str, Any]:
        """Fetch the canonical message object from VK before media handling.

        Long Poll/Callback payloads can contain shortened attachment objects or
        web-player URLs.  For chat media we should use VK's message API as the
        source of truth, not scrape public links.  ``messages.getByConversationMessageId``
        returns the message in the peer with its attachments (audio_message,
        video_message, photo, doc, etc.).  If the call fails or returns nothing,
        keep the original event payload.
        """
        if not peer_id or not conversation_message_id:
            return msg
        try:
            payload = await self._vk_method(
                "messages.getByConversationMessageId",
                {
                    "peer_id": str(peer_id),
                    "conversation_message_ids": str(conversation_message_id),
                },
            )
            response = payload.get("response") or {}
            items = response.get("items") if isinstance(response, dict) else response
            if isinstance(items, list) and items and isinstance(items[0], dict):
                enriched = dict(msg)
                enriched.update(items[0])
                return await self._enrich_video_attachments_from_api(
                    enriched,
                    peer_id=peer_id,
                    conversation_message_id=conversation_message_id,
                )
        except Exception as exc:
            logger.debug(
                "VK: failed to enrich message peer=%s cmid=%s — %s",
                peer_id,
                conversation_message_id,
                _redact_token(str(exc)),
            )
        return msg

    async def _enrich_video_attachments_from_api(
        self,
        msg: dict[str, Any],
        *,
        peer_id: str = "",
        conversation_message_id: Any = None,
    ) -> dict[str, Any]:
        """Ask VK API for richer video objects when message payload has only ids.

        ``messages.getByConversationMessageId`` is the source of truth for the
        message.  Some VK ``video`` attachments still arrive as owner/id/player
        metadata without a downloadable file URL.  The documented native follow-up
        is ``video.get`` with ``owner_id_video_id_access_key``.  Community tokens
        commonly cannot call that method, so an optional user token may be
        configured; if VK still returns no direct file fields, keep the message
        metadata but do not synthesize a public ``vk.com/video...`` URL.
        """
        attachments = msg.get("attachments") or []
        if not isinstance(attachments, list):
            return msg

        changed = False
        enriched_attachments: list[Any] = []
        for attachment in attachments:
            if not isinstance(attachment, dict) or attachment.get("type") != "video":
                enriched_attachments.append(attachment)
                continue
            raw_payload = attachment.get("video")
            payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
            if self._video_has_direct_media(payload):
                enriched_attachments.append(attachment)
                continue
            ref = self._video_api_ref(payload)
            if not ref:
                enriched_attachments.append(attachment)
                continue
            try:
                history_video = await self._video_from_history_attachments(
                    peer_id=peer_id,
                    conversation_message_id=conversation_message_id,
                    original=payload,
                )
                if history_video:
                    merged_video = self._merge_video_payload(payload, history_video)
                    enriched_attachment = dict(attachment)
                    enriched_attachment["video"] = merged_video
                    enriched_attachments.append(enriched_attachment)
                    changed = True
                    continue
            except Exception as exc:
                logger.debug("VK: getHistoryAttachments fallback failed for %s — %s", ref, _redact_token(str(exc)))
            if not self.user_token:
                enriched_attachments.append(attachment)
                continue
            try:
                video_payload = await self._vk_method(
                    "video.get",
                    {"videos": ref},
                    access_token=self.user_token,
                )
                items = (video_payload.get("response") or {}).get("items") or []
                if items and isinstance(items[0], dict):
                    merged_video = self._merge_video_payload(payload, items[0])
                    enriched_attachment = dict(attachment)
                    enriched_attachment["video"] = merged_video
                    enriched_attachments.append(enriched_attachment)
                    changed = True
                    continue
            except Exception as exc:
                logger.debug("VK: video.get fallback failed for %s — %s", ref, _redact_token(str(exc)))
            enriched_attachments.append(attachment)

        if changed:
            enriched_msg = dict(msg)
            enriched_msg["attachments"] = enriched_attachments
            return enriched_msg
        return msg

    @classmethod
    def _merge_video_payload(cls, original: dict[str, Any], enriched: dict[str, Any]) -> dict[str, Any]:
        """Merge richer VK video metadata without losing live message previews.

        For message-scoped VK videos, ``video.get`` may add ``player`` and
        ``is_from_message`` while returning expired ``first_frame``/``image``
        URLs.  The original message attachment often carries still-downloadable
        preview URLs.  If the enriched object still has no direct media file,
        preserve those original previews so Hermes can at least run vision on a
        frame instead of dropping the attachment.
        """
        merged = dict(original)
        merged.update(enriched)
        if not cls._video_has_direct_media(merged):
            for key in ("first_frame", "image", "access_key"):
                if original.get(key):
                    merged[key] = original[key]
        return merged

    async def _video_from_history_attachments(
        self,
        *,
        peer_id: str,
        conversation_message_id: Any,
        original: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a richer video object from VK's message-attachment index.

        VK docs point bots at ``messages.getHistoryAttachments`` for dialog
        media.  When called with the current conversation message id and
        ``attachment_position=1`` it may expose message-scoped video objects that
        ``video.get`` cannot download.  Empty/irrelevant results are ignored.
        """
        if not peer_id or not conversation_message_id:
            return {}
        payload = await self._vk_method(
            "messages.getHistoryAttachments",
            {
                "peer_id": str(peer_id),
                "media_type": "video",
                "conversation_message_id": str(conversation_message_id),
                "attachment_position": "1",
                "count": "10",
            },
        )
        items = (payload.get("response") or {}).get("items") or []
        original_ref = self._video_api_ref(original)
        for item in items:
            if not isinstance(item, dict):
                continue
            attachment = item.get("attachment") or item
            if not isinstance(attachment, dict):
                continue
            kind = attachment.get("type")
            video = attachment.get("video") if kind == "video" else attachment
            if not isinstance(video, dict):
                continue
            if original_ref and self._video_api_ref(video) and self._video_api_ref(video) != original_ref:
                continue
            return video
        return {}

    @staticmethod
    def _video_api_ref(payload: dict[str, Any]) -> str:
        owner_id = payload.get("owner_id")
        video_id = payload.get("id") or payload.get("video_id")
        if owner_id is None or video_id is None:
            return ""
        ref = f"{owner_id}_{video_id}"
        access_key = str(payload.get("access_key") or "").strip()
        if access_key:
            ref += f"_{access_key}"
        return ref

    @staticmethod
    def _video_has_direct_media(payload: dict[str, Any]) -> bool:
        if payload.get("url"):
            return True
        files = payload.get("files") or {}
        if isinstance(files, dict):
            return any(files.get(key) for key in ("mp4_1080", "mp4_720", "mp4_480", "mp4_360", "mp4_240", "external"))
        return False

    @staticmethod
    def _video_image_url(payload: dict[str, Any]) -> str:
        """Return the best available preview frame for a video attachment.

        VK message-scoped videos can be visible in the web UI while the API
        withholds direct ``files``/``url`` media sources.  In that case a
        ``first_frame``/``image`` preview is still useful context for Hermes and
        should be routed as an image instead of dropping the attachment entirely.
        """
        candidates = []
        for key in ("first_frame", "image"):
            value = payload.get(key) or []
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
        if not candidates:
            return ""
        best = max(candidates, key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0))
        return str(best.get("url") or "")

    async def _resolve_conversation_context(self, peer_id: str, *, chat_type: str) -> tuple[str, str]:
        """Return a useful VK chat label and browser URL for prompt context.

        Telegram gives Hermes the real chat title in every update, so requests
        like "open this chat in the browser" have enough context. VK Group Long
        Poll only includes numeric peer ids, so resolve the title from the VK
        API and include the deterministic VK Web URL as channel-topic metadata.
        """
        if peer_id in self._conversation_context_cache:
            return self._conversation_context_cache[peer_id]

        name = f"VK peer {peer_id}"
        web_url = self._vk_web_chat_url(peer_id)
        topic = f"VK Web chat URL: {web_url}"
        try:
            payload = await self._vk_method("messages.getConversationsById", {"peer_ids": str(peer_id)})
            items = (payload.get("response") or {}).get("items") or []
            item = items[0] if items else {}
            settings = item.get("chat_settings") or {}
            title = str(settings.get("title") or "").strip()
            if title:
                name = title
            elif chat_type == "dm":
                profiles = (payload.get("response") or {}).get("profiles") or []
                profile = profiles[0] if profiles else {}
                first = str(profile.get("first_name") or "").strip()
                last = str(profile.get("last_name") or "").strip()
                full_name = " ".join(part for part in (first, last) if part).strip()
                if full_name:
                    name = full_name
        except Exception as exc:
            logger.debug("VK: failed to resolve conversation context for peer=%s — %s", peer_id, _redact_token(str(exc)))

        self._conversation_context_cache[peer_id] = (name, topic)
        return name, topic

    @staticmethod
    def _vk_web_chat_url(peer_id: str) -> str:
        if str(peer_id).startswith("200000"):
            try:
                return f"https://vk.com/im?sel=c{int(peer_id) - 2_000_000_000}"
            except ValueError:
                pass
        return f"https://vk.com/im?sel={urllib.parse.quote(str(peer_id))}"

    async def _materialize_inbound_media(
        self,
        message_type: MessageType,
        media_urls: list[str],
        media_types: list[str],
    ) -> list[str]:
        """Return local paths for inbound media that core gateway enrichers need.

        Voice messages always need a local file for STT. Other direct VK media
        URLs are also cached so the gateway's audio/video/document context notes
        point to files the agent can actually open. Non-downloadable watch pages
        (notably regular ``vk.com/video...`` links) remain URLs.
        """
        materialized: list[str] = []
        for index, url in enumerate(media_urls):
            media_type = media_types[index] if index < len(media_types) else ""
            should_download = self.download_attachments or _looks_like_downloadable_attachment_url(url)
            if not should_download:
                materialized.append(url)
                continue
            try:
                materialized.append(await _download_attachment_async(url, media_type, max_bytes=self.max_attachment_bytes))
            except Exception as exc:
                logger.warning("VK: failed to download inbound attachment — %s", _redact_token(str(exc)))
                materialized.append(url)
        return materialized

    def _is_duplicate_event(self, *, peer_id: str, conversation_message_id: Any) -> bool:
        if not peer_id or conversation_message_id in (None, "") or self.dedupe_ttl_seconds <= 0:
            return False
        now = time.monotonic()
        # Keep the tiny in-memory cache bounded by evicting expired keys on every
        # message.  Long Poll duplicates usually arrive shortly after reconnect.
        expired = [key for key, expires_at in self._seen_message_keys.items() if expires_at <= now]
        for key in expired:
            self._seen_message_keys.pop(key, None)
        key = f"{peer_id}:{conversation_message_id}"
        if key in self._seen_message_keys:
            return True
        self._seen_message_keys[key] = now + self.dedupe_ttl_seconds
        return False

    def _is_authorized(self, *, from_id: str, peer_id: str) -> bool:
        if self.allow_all_users:
            return True

        peer_allowed = bool(peer_id and peer_id in self.allowed_peers)
        user_allowed = bool(from_id and from_id in self.allowed_users)
        peer_specific_users = self.allowed_users_by_peer.get(peer_id) if peer_id else None
        if peer_specific_users is not None:
            return bool(from_id and from_id in peer_specific_users)

        if self.access_policy not in VALID_ACCESS_POLICIES:
            logger.error("VK: unknown access policy %r; denying message", self.access_policy)
            return False
        if self.access_policy == "any":
            return user_allowed or peer_allowed
        if self.access_policy == "user_only":
            return user_allowed
        if self.access_policy == "peer_only":
            return peer_allowed
        return peer_allowed and user_allowed

    @staticmethod
    def _attachment_url(kind: str, payload: dict[str, Any]) -> str:
        if kind == "photo":
            sizes = payload.get("sizes") or []
            if sizes:
                best = max(sizes, key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0))
                return str(best.get("url") or "")
        if kind in {"doc", "audio"}:
            return str(payload.get("url") or payload.get("player") or "")
        if kind == "video":
            # Native media intake must not synthesize or prefer public VK watch
            # pages.  Only return direct file fields obtained from VK API.  If
            # VK exposes only player/owner/id metadata, fall back to the best
            # first_frame/image preview so the agent can still see context.
            direct_url = str(payload.get("url") or "")
            if direct_url:
                return direct_url
            files = payload.get("files") or {}
            if isinstance(files, dict):
                for key in ("mp4_1080", "mp4_720", "mp4_480", "mp4_360", "mp4_240", "external"):
                    value = str(files.get(key) or "")
                    if value:
                        return value
            return VKAdapter._video_image_url(payload)
        if kind == "audio_message":
            return str(payload.get("link_ogg") or payload.get("link_mp3") or "")
        if kind == "video_message":
            return str(payload.get("link_mp4") or payload.get("link") or "")
        return ""

    @staticmethod
    def _attachment_media_type(kind: str, payload: dict[str, Any], message_type: MessageType) -> str:
        """Return a MIME-like media type for gateway routing.

        The gateway's per-attachment routing checks MIME prefixes such as
        ``image/`` and ``video/``. Passing Hermes message-type labels like
        ``"video"`` makes video attachments look like opaque text and skips the
        video context note.
        """
        if kind == "photo":
            return "image/jpeg"
        if kind in {"video", "video_message"}:
            if kind == "video" and not VKAdapter._video_has_direct_media(payload) and VKAdapter._video_image_url(payload):
                return "image/jpeg"
            return "video/mp4"
        if kind == "audio_message":
            return "audio/ogg" if payload.get("link_ogg") else "audio/mpeg"
        if kind == "audio":
            return "audio/mpeg"
        if kind == "doc":
            ext = str(payload.get("ext") or "").lower().lstrip(".")
            if message_type == MessageType.VOICE:
                return "audio/ogg" if ext in {"ogg", "opus"} else "audio/mpeg"
            if ext:
                guessed, _ = mimetypes.guess_type(f"file.{ext}")
                if guessed:
                    return guessed
            return "application/octet-stream"
        return ""

    @staticmethod
    def _message_type_for_attachment(kind: str, payload: Optional[dict[str, Any]] = None) -> MessageType:
        payload = payload or {}
        if kind == "photo":
            return MessageType.PHOTO
        if kind in {"video", "video_message"}:
            return MessageType.VIDEO
        if kind == "audio_message":
            return MessageType.VOICE
        if kind == "audio":
            return MessageType.AUDIO
        if kind == "doc":
            # VK voice messages may arrive as doc-like objects in some clients/libraries.
            if payload.get("type") == 5 or payload.get("ext") in {"ogg", "opus"}:
                return MessageType.VOICE
            return MessageType.DOCUMENT
        return MessageType.TEXT

    def _summarize_forwarded_messages(self, msg: dict[str, Any]) -> str:
        """Return a compact text representation of VK replies/forwards.

        VK puts forwarded messages in ``fwd_messages`` and replies in
        ``reply_message``. They are not normal attachments, so without this a
        pure forward has empty ``text``/``attachments`` and is silently ignored.
        Keep the summary compact and mark it as quoted context so the agent does
        not confuse it with the user's own new words.
        """
        parts: list[str] = []
        reply = msg.get("reply_message")
        if isinstance(reply, dict):
            rendered = self._render_forwarded_message(reply, depth=0)
            if rendered:
                parts.append("[VK reply]\n" + rendered)
        forwards = msg.get("fwd_messages") or []
        if isinstance(forwards, list):
            rendered_forwards = [self._render_forwarded_message(item, depth=0) for item in forwards if isinstance(item, dict)]
            rendered_forwards = [item for item in rendered_forwards if item]
            if rendered_forwards:
                parts.append("[VK forwarded messages]\n" + "\n---\n".join(rendered_forwards[:5]))
                if len(rendered_forwards) > 5:
                    parts.append(f"[VK forwarded messages: {len(rendered_forwards) - 5} more omitted]")
        return "\n\n".join(parts)

    def _render_forwarded_message(self, item: dict[str, Any], *, depth: int) -> str:
        if depth > 2:
            return "[nested forwarded messages omitted]"
        lines: list[str] = []
        from_id = item.get("from_id")
        if from_id not in (None, ""):
            lines.append(f"from_id={from_id}")
        text = str(item.get("text") or "").strip()
        if text:
            lines.append(text)
        attachments = item.get("attachments") or []
        if isinstance(attachments, list) and attachments:
            lines.append(self._summarize_attachments(attachments))
        nested = item.get("fwd_messages") or []
        if isinstance(nested, list) and nested:
            nested_rendered = [self._render_forwarded_message(child, depth=depth + 1) for child in nested if isinstance(child, dict)]
            nested_rendered = [child for child in nested_rendered if child]
            if nested_rendered:
                lines.append("nested forwards:\n" + "\n---\n".join(nested_rendered[:3]))
        return "\n".join(lines).strip()

    def _extract_attachment_media(self, attachments: list[Any]) -> tuple[MessageType, list[str], list[str]]:
        message_type = MessageType.TEXT
        media_urls: list[str] = []
        media_types: list[str] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            kind = str(attachment.get("type") or "")
            payload = attachment.get(kind) if kind else None
            if not isinstance(payload, dict):
                payload = {}
            current_type = self._message_type_for_attachment(kind, payload)
            if message_type == MessageType.TEXT and current_type != MessageType.TEXT:
                message_type = current_type
            url = self._attachment_url(kind, payload)
            if url:
                media_urls.append(url)
                media_types.append(self._attachment_media_type(kind, payload, current_type))
        return message_type, media_urls, media_types

    def _summarize_attachments(self, attachments: list[Any]) -> str:
        labels = []
        label_by_kind = {
            "photo": "фото",
            "video": "видео",
            "audio": "аудио",
            "audio_message": "голосовое сообщение",
            "video_message": "видеосообщение",
            "doc": "документ",
            "sticker": "стикер",
        }
        for attachment in attachments:
            if isinstance(attachment, dict):
                kind = str(attachment.get("type") or "")
                payload = attachment.get(kind) if kind else None
                if kind == "video" and isinstance(payload, dict) and payload.get("is_from_message"):
                    labels.append("видеосообщение")
                else:
                    labels.append(label_by_kind.get(kind, kind or "вложение"))
        if not labels:
            return "[VK attachment]"
        return "[VK attachment: " + ", ".join(labels) + "]"

    @staticmethod
    def _format_attachment_ref(kind: str, payload: dict[str, Any]) -> str:
        owner_id = payload.get("owner_id")
        item_id = payload.get("id") or payload.get("video_id")
        access_key = payload.get("access_key")
        if owner_id is None or item_id is None:
            raise RuntimeError(f"VK {kind} upload response is missing owner_id/id")
        ref = f"{kind}{owner_id}_{item_id}"
        if access_key:
            ref += f"_{access_key}"
        return ref

    @staticmethod
    def _sent_message_id(payload: dict[str, Any]) -> Optional[str]:
        """Extract an editable id from ``messages.send`` response.

        VK's legacy ``peer_id`` send response is a scalar (and community bots in
        chats can return ``0``).  The newer ``peer_ids`` form returns objects
        containing ``conversation_message_id``; that cmid is the reliable id for
        bot edits in a peer, so encode it explicitly for ``edit_message``.
        """
        response = payload.get("response") if isinstance(payload, dict) else None
        item: Any = None
        if isinstance(response, list) and response:
            item = response[0]
        elif isinstance(response, dict):
            item = response
        if isinstance(item, dict):
            cmid = item.get("conversation_message_id")
            if cmid not in (None, 0, "0", ""):
                return f"cmid:{cmid}"
            mid = item.get("message_id") or item.get("id")
            if mid not in (None, 0, "0", ""):
                return str(mid)
        if response not in (None, 0, "0", ""):
            return str(response)
        return None

    async def _send_attachment(self, chat_id: str, attachment: str, caption: Optional[str] = None) -> SendResult:
        try:
            async def op():
                return await self._vk_method(
                    "messages.send",
                    {
                        "peer_ids": str(chat_id),
                        "message": caption or "",
                        "attachment": attachment,
                        "random_id": random.randint(1, 2_147_483_647),
                    },
                )

            payload = await _retry_vk_transient_once("messages.send attachment", op)
            message_id = self._sent_message_id(payload)
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            return SendResult(success=False, error=_redact_token(str(exc)), retryable=True)

    async def _upload_photo_attachment(self, chat_id: str, image_path: str) -> str:
        async def op():
            server = await self._vk_method("photos.getMessagesUploadServer", {"peer_id": str(chat_id)})
            upload_url = (server.get("response") or {}).get("upload_url")
            if not upload_url:
                raise RuntimeError("VK photo upload server response is missing upload_url")
            uploaded = await _multipart_upload_async(upload_url, "photo", image_path)
            saved = await self._vk_method("photos.saveMessagesPhoto", uploaded)
            photos = saved.get("response") or []
            if not photos:
                raise RuntimeError("VK photo save response is empty")
            return self._format_attachment_ref("photo", photos[0])

        return await _retry_vk_transient_once("photo upload", op)

    async def _upload_doc_attachment(self, chat_id: str, file_path: str, *, doc_type: Optional[str] = None) -> str:
        async def op():
            params = {"peer_id": str(chat_id)}
            if doc_type:
                params["type"] = doc_type
            server = await self._vk_method("docs.getMessagesUploadServer", params)
            upload_url = (server.get("response") or {}).get("upload_url")
            if not upload_url:
                raise RuntimeError("VK docs upload server response is missing upload_url")
            uploaded = await _multipart_upload_async(upload_url, "file", file_path)
            saved = await self._vk_method("docs.save", uploaded)
            response = saved.get("response") or {}
            doc = response.get("audio_message") or response.get("doc") or response
            kind = "doc"
            return self._format_attachment_ref(kind, doc)

        return await _retry_vk_transient_once("doc upload", op)

    async def _upload_video_attachment(self, chat_id: str, video_path: str, caption: Optional[str] = None) -> str:
        async def op():
            server = await self._vk_method("video.save", {"group_id": self.group_id, "name": caption or Path(video_path).name})
            response = server.get("response") or {}
            upload_url = response.get("upload_url")
            if not upload_url:
                raise RuntimeError("VK video.save response is missing upload_url")
            await _multipart_upload_async(upload_url, "video_file", video_path, timeout=300)
            return self._format_attachment_ref("video", response)

        return await _retry_vk_transient_once("video upload", op)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self.token:
            return SendResult(success=False, error="VK_GROUP_TOKEN is not configured")

        chunks = self.truncate_message(self.format_message(content or ""), self.max_message_length)
        last_message_id: Optional[str] = None
        continuation_ids: list[str] = []
        try:
            for chunk in chunks:
                async def op(chunk=chunk):
                    return await self._vk_method(
                        "messages.send",
                        {
                            "peer_ids": str(chat_id),
                            "message": chunk,
                            "random_id": random.randint(1, 2_147_483_647),
                        },
                    )

                payload = await _retry_vk_transient_once("messages.send", op)
                message_id = self._sent_message_id(payload)
                if last_message_id:
                    continuation_ids.append(last_message_id)
                last_message_id = message_id
            return SendResult(success=True, message_id=last_message_id, continuation_message_ids=tuple(continuation_ids))
        except Exception as exc:
            return SendResult(success=False, error=_redact_token(str(exc)), retryable=True)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        if not self.token:
            return SendResult(success=False, error="VK_GROUP_TOKEN is not configured")
        if str(message_id) in {"", "0", "__no_edit__"}:
            return SendResult(success=False, error="VK did not return an editable message id", retryable=False)
        try:
            edit_params = {
                "peer_id": str(chat_id),
                "message": self.format_message(content or ""),
            }
            if str(message_id).startswith("cmid:"):
                edit_params["conversation_message_id"] = str(message_id).split(":", 1)[1]
            else:
                edit_params["message_id"] = str(message_id)
            await self._vk_method(
                "messages.edit",
                edit_params,
            )
            return SendResult(success=True, message_id=str(message_id))
        except Exception as exc:
            return SendResult(success=False, error=_redact_token(str(exc)), retryable=True)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        try:
            attachment = await self._upload_photo_attachment(chat_id, image_path)
        except Exception as exc:
            return SendResult(success=False, error=_redact_token(str(exc)), retryable=True)
        return await self._send_attachment(chat_id, attachment, caption)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # VK API cannot attach arbitrary remote images directly to messages;
        # keep remote URLs visible rather than silently downloading untrusted data.
        text = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        try:
            attachment = await self._upload_doc_attachment(chat_id, file_path)
        except Exception as exc:
            return SendResult(success=False, error=_redact_token(str(exc)), retryable=True)
        return await self._send_attachment(chat_id, attachment, caption)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        try:
            attachment = await self._upload_doc_attachment(chat_id, audio_path, doc_type="audio_message")
        except Exception as exc:
            return SendResult(success=False, error=_redact_token(str(exc)), retryable=True)
        return await self._send_attachment(chat_id, attachment, caption)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        try:
            attachment = await self._upload_video_attachment(chat_id, video_path, caption)
        except Exception as exc:
            return SendResult(success=False, error=_redact_token(str(exc)), retryable=True)
        return await self._send_attachment(chat_id, attachment, caption)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        try:
            await self._vk_method("messages.setActivity", {"peer_id": str(chat_id), "type": "typing"})
        except Exception:
            logger.debug("VK: send_typing failed", exc_info=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_id = str(chat_id)
        return {
            "id": chat_id,
            "name": f"VK peer {chat_id}",
            "type": "group" if chat_id.startswith("200000") else "dm",
            "chat_id": chat_id,
        }


def check_vk_requirements() -> bool:
    """No optional Python dependencies are required for VK MVP."""
    return True


def _env_enablement() -> Optional[dict[str, Any]]:
    token = os.getenv("VK_GROUP_TOKEN")
    group_id = os.getenv("VK_GROUP_ID")
    if not token or not group_id:
        return None
    extra = {
        "group_id": group_id.lstrip("-"),
        "allowed_users": sorted(_split_csv(os.getenv("VK_ALLOWED_USERS"))),
        "allowed_peers": sorted(_split_csv(os.getenv("VK_ALLOWED_PEERS"))),
        "allow_all_users": _truthy(os.getenv("VK_ALLOW_ALL_USERS")),
    }
    access_policy = os.getenv("VK_ACCESS_POLICY")
    if access_policy:
        extra["access_policy"] = access_policy
    users_by_peer = os.getenv("VK_ALLOWED_USERS_BY_PEER")
    if users_by_peer:
        extra["allowed_users_by_peer"] = {k: sorted(v) for k, v in _parse_users_by_peer(users_by_peer).items()}
    max_attachment_bytes = os.getenv("VK_MAX_ATTACHMENT_BYTES")
    if max_attachment_bytes:
        extra["max_attachment_bytes"] = _parse_positive_int(max_attachment_bytes, DEFAULT_MAX_ATTACHMENT_BYTES)
    dedupe_ttl = os.getenv("VK_DEDUPE_TTL_SECONDS")
    if dedupe_ttl:
        extra["dedupe_ttl_seconds"] = _parse_positive_int(dedupe_ttl, DEFAULT_DEDUPE_TTL_SECONDS)
    home = os.getenv("VK_HOME_CHANNEL")
    if home:
        extra["home_channel"] = home
    return extra


def _apply_yaml_config(yaml_cfg: dict[str, Any], platform_cfg: Any) -> Optional[dict[str, Any]]:
    vk_cfg = ((yaml_cfg or {}).get("gateway") or {}).get("vk") or (yaml_cfg or {}).get("vk") or {}
    if not isinstance(vk_cfg, dict):
        return None
    extra: dict[str, Any] = {}
    mapping = {
        "group_id": "VK_GROUP_ID",
        "group_token": "VK_GROUP_TOKEN",
        "allowed_users": "VK_ALLOWED_USERS",
        "allowed_peers": "VK_ALLOWED_PEERS",
        "allow_all_users": "VK_ALLOW_ALL_USERS",
        "access_policy": "VK_ACCESS_POLICY",
        "allowed_users_by_peer": "VK_ALLOWED_USERS_BY_PEER",
        "dedupe_ttl_seconds": "VK_DEDUPE_TTL_SECONDS",
        "max_attachment_bytes": "VK_MAX_ATTACHMENT_BYTES",
        "home_channel": "VK_HOME_CHANNEL",
    }
    for key, env_name in mapping.items():
        value = vk_cfg.get(key)
        if value is None:
            continue
        if key in {"allowed_users", "allowed_peers"} and isinstance(value, (list, tuple, set)):
            value_str = ",".join(str(v) for v in value)
        elif key == "allowed_users_by_peer" and isinstance(value, dict):
            value_str = ";".join(
                f"{peer}:{'|'.join(str(user) for user in users)}"
                for peer, users in value.items()
                if isinstance(users, (list, tuple, set))
            )
        else:
            value_str = str(value)
        if not os.getenv(env_name):
            os.environ[env_name] = value_str
        if key != "group_token":
            extra[key] = value
    if "group_token" in vk_cfg and not getattr(platform_cfg, "token", None):
        platform_cfg.token = str(vk_cfg["group_token"])
    for key in ("channel_prompts", "channel_skill_bindings"):
        value = vk_cfg.get(key)
        if value is not None:
            extra[key] = value
    return extra or None


def _is_connected(config: Any) -> bool:
    extra = getattr(config, "extra", {}) or {}
    token = os.getenv("VK_GROUP_TOKEN") or getattr(config, "token", None) or extra.get("group_token")
    group_id = os.getenv("VK_GROUP_ID") or extra.get("group_id")
    return bool(token and group_id)


async def _standalone_send(chat_id: str, text: str, **_: Any) -> dict[str, Any]:
    token = os.getenv("VK_GROUP_TOKEN")
    if not token:
        return {"success": False, "error": "VK_GROUP_TOKEN is not configured"}
    params = {
        "access_token": token,
        "v": os.getenv("VK_API_VERSION") or VK_API_VERSION,
        "peer_id": str(chat_id),
        "message": text or "",
        "random_id": random.randint(1, 2_147_483_647),
    }
    try:
        payload = await _http_json_async(f"{VK_API_BASE}/messages.send", params)
        return {"success": True, "message_id": payload.get("response")}
    except Exception as exc:
        return {"success": False, "error": _redact_token(str(exc))}


def _build_adapter(config: Any) -> VKAdapter:
    return VKAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    try:
        from .setup_helper import setup_vk_platform
    except Exception:  # pragma: no cover - setup helper is optional at runtime
        setup_vk_platform = None

    ctx.register_platform(
        name="vk",
        label="VK Messenger",
        adapter_factory=_build_adapter,
        check_fn=check_vk_requirements,
        validate_config=_is_connected,
        is_connected=_is_connected,
        required_env=["VK_GROUP_TOKEN", "VK_GROUP_ID"],
        install_hint="Create a VK community token with messages permissions and run `hermes gateway setup` or set VK_GROUP_TOKEN/VK_GROUP_ID",
        setup_fn=setup_vk_platform,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="VK_ALLOWED_USERS",
        allow_all_env="VK_ALLOW_ALL_USERS",
        cron_deliver_env_var="VK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        emoji="🔵",
        platform_hint="You are connected through VK Messenger. Keep replies concise; VK has no Telegram-style topics.",
        max_message_length=4096,
        allow_update_command=True,
    )
