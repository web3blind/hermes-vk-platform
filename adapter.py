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
import sqlite3
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
    ProcessingOutcome,
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

# VK message reactions are identified by community-scoped numeric ids, not
# emoji. Common default set (per nanobot-channel-vk's README):
# 1=❤️ 2=🔥 3=😂 4=👍 8=😡 10=👌 16=🎉. There is no 👀 in the default set,
# so the intake ack uses 👌; communities that rearrange reactions can override
# via VK_REACTION_PROGRESS / VK_REACTION_OK / VK_REACTION_FAIL. 0 = disabled.
_VK_REACTION_OK = 4  # 👍
_VK_REACTION_FAIL = 8  # override via VK_REACTION_FAIL if the community map differs
_VK_REACTION_PROGRESS_DEFAULT = 10  # 👌 — "seen, working on it"


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


LANE_THREAD_PREFIX = "lane:"
PROJECT_LANE_STATE_FILENAME = "vk_project_lanes_state.json"
PROJECT_CREATE_TTL_SECONDS = 15 * 60
_SAFE_LANE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _sanitize_lane_label(value: Any, max_len: int = 64) -> str:
    text = _CONTROL_CHARS_RE.sub(" ", str(value or "").replace("\r", " ").replace("\n", " "))
    text = " ".join(text.split()).strip()
    if max_len and len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _slugify_lane_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    # Keep ASCII slugs deterministic and path/session-key safe. Cyrillic names
    # without explicit folder/id become empty and require clarification.
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"[-_]{2,}", "-", text).strip("-_")
    return text[:64]


def _is_safe_lane_id(value: str) -> bool:
    text = str(value or "").strip()
    if not text or ".." in text or ":" in text or "/" in text or "\\" in text:
        return False
    return bool(_SAFE_LANE_ID_RE.fullmatch(text))


def _normalize_lane_skills(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    result: list[str] = []
    for item in items:
        skill = str(item or "").strip()
        if skill and skill not in result:
            result.append(skill)
    return result


def _normalize_project_lanes(raw: Any) -> dict[str, Any]:
    """Return normalized VK project-lane config, ignoring unsafe entries.

    Shape: {peer_id: {base_folder, default_skills, lanes, lane_by_id, alias_to_id}}.
    The function is pure and deliberately permissive: bad entries are skipped so
    a typo in optional project-lane config cannot disable the VK gateway.
    """
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, Any] = {}
    for peer, peer_cfg in raw.items():
        peer_id = str(peer or "").strip()
        if not peer_id or not isinstance(peer_cfg, dict):
            continue
        lanes_raw = peer_cfg.get("lanes") or []
        if not isinstance(lanes_raw, list):
            continue
        lanes: list[dict[str, Any]] = []
        lane_by_id: dict[str, dict[str, Any]] = {}
        alias_to_id: dict[str, str] = {}
        for lane_raw in lanes_raw:
            if not isinstance(lane_raw, dict):
                continue
            lane_id = str(lane_raw.get("id") or "").strip().lower()
            if not _is_safe_lane_id(lane_id) or lane_id in lane_by_id:
                continue
            name = _sanitize_lane_label(lane_raw.get("name") or lane_id, 80) or lane_id
            aliases: list[str] = []
            for alias in lane_raw.get("aliases") or []:
                alias_text = str(alias or "").strip().lower()
                if not alias_text or alias_text in alias_to_id or alias_text in lane_by_id:
                    continue
                aliases.append(alias_text)
                alias_to_id[alias_text] = lane_id
            lane = {
                "id": lane_id,
                "name": name,
                "description": str(lane_raw.get("description") or lane_raw.get("context") or "").strip(),
                "folder": str(lane_raw.get("folder") or "").strip(),
                "workdir": str(lane_raw.get("workdir") or "").strip(),
                "skills": _normalize_lane_skills(lane_raw.get("skills") or lane_raw.get("skill")),
                "aliases": aliases,
            }
            lanes.append(lane)
            lane_by_id[lane_id] = lane
        if lanes:
            normalized[peer_id] = {
                "base_folder": str(peer_cfg.get("base_folder") or "").strip(),
                "default_skills": _normalize_lane_skills(peer_cfg.get("default_skills") or peer_cfg.get("default_skill")),
                "lanes": lanes,
                "lane_by_id": lane_by_id,
                "alias_to_id": alias_to_id,
            }
    return normalized


def _extract_project_lanes_config(raw: Any) -> Any:
    """Return the actual per-peer lane mapping from supported config shapes.

    Supported shapes:
    - legacy/direct: {"2000000001": {"lanes": [...]}}
    - canonical feature: {"enabled": true, "chats": {"2000000001": {"lanes": [...]}}}
    """
    if not isinstance(raw, dict):
        return raw
    chats = raw.get("chats")
    if isinstance(chats, dict):
        if "enabled" in raw and not _truthy(raw.get("enabled")):
            return {}
        return chats
    return raw


def _lane_for_config(lane: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(lane.get("id") or "").strip(),
        "name": str(lane.get("name") or "").strip(),
    }
    for key in ("description", "folder", "workdir", "plan"):
        value = str(lane.get(key) or "").strip()
        if value:
            result[key] = value
    skills = _normalize_lane_skills(lane.get("skills"))
    if skills:
        result["skills"] = skills
    aliases = [str(a).strip().lstrip("@").lower() for a in lane.get("aliases") or [] if str(a).strip()]
    if aliases:
        result["aliases"] = aliases
    return result


def _upsert_lane_in_config(config: dict[str, Any], peer_id: str, lane: dict[str, Any]) -> dict[str, Any]:
    """Return *config* with a VK project lane persisted under canonical plugin config.

    This mirrors Telegram topic persistence: chat-created routing metadata is
    promoted into the Hermes config automatically instead of relying on a later
    manual export step. Legacy ``vk.project_lanes`` remains readable but new
    writes go to ``platforms.vk.extra.project_lanes``.
    """
    peer = str(peer_id or "").strip()
    if not peer:
        return config
    normalized = _normalize_project_lanes({peer: {"lanes": [lane]}}).get(peer)
    if not normalized or not normalized.get("lanes"):
        return config
    lane_entry = _lane_for_config(normalized["lanes"][0])
    platforms = config.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        platforms = {}
        config["platforms"] = platforms
    vk_cfg = platforms.setdefault("vk", {})
    if not isinstance(vk_cfg, dict):
        vk_cfg = {}
        platforms["vk"] = vk_cfg
    extra = vk_cfg.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        vk_cfg["extra"] = extra
    feature = extra.setdefault("project_lanes", {})
    if not isinstance(feature, dict):
        feature = {}
        extra["project_lanes"] = feature
    feature["enabled"] = True
    chats = feature.setdefault("chats", {})
    if not isinstance(chats, dict):
        chats = {}
        feature["chats"] = chats
    chat_cfg = chats.setdefault(peer, {})
    if not isinstance(chat_cfg, dict):
        chat_cfg = {}
        chats[peer] = chat_cfg
    lanes = chat_cfg.setdefault("lanes", [])
    if not isinstance(lanes, list):
        lanes = []
        chat_cfg["lanes"] = lanes
    replaced = False
    for idx, item in enumerate(lanes):
        if isinstance(item, dict) and str(item.get("id") or "").strip().lower() == lane_entry["id"]:
            lanes[idx] = lane_entry
            replaced = True
            break
    if not replaced:
        lanes.append(lane_entry)
    return config


def _build_project_lane_prompt(peer_id: str) -> str:
    return (
        "[VK project lane creation mode]\n"
        f"The user is creating a VK project lane for peer {peer_id}.\n"
        "Extract: name, context/description, folder/id, skills.\n"
        "Validate slug and skills. If anything is missing or ambiguous, ask one concise clarification.\n"
        "When the adapter can parse fields deterministically it will persist the lane through the approved VK project-lane config path."
    )


def _build_project_lane_edit_prompt(peer_id: str, lane: dict[str, Any]) -> str:
    return (
        "[VK project lane edit mode]\n"
        f"The user is editing a VK project lane for peer {peer_id}.\n"
        f"Project to edit: {lane.get('id')} ({lane.get('name')}).\n"
        "Use the user's requested change to update this lane's metadata only: name, context/description, folder, workdir, plan, skills, aliases.\n"
        "When field changes are deterministic, the adapter persists them through the approved VK project-lane config path."
    )


def _lane_state_path() -> Path:
    return get_hermes_home() / PROJECT_LANE_STATE_FILENAME


def _session_state_db_path() -> Path:
    return get_hermes_home() / "state.db"


def _parse_vk_target_ref(target_ref: str) -> tuple[str, Optional[str]] | None:
    raw = str(target_ref or "").strip()
    if not raw:
        return None
    if ":" in raw:
        chat_id, thread_id = raw.split(":", 1)
        chat_id = chat_id.strip()
        thread_id = thread_id.strip()
        if chat_id and thread_id.startswith(LANE_THREAD_PREFIX):
            lane_id = thread_id[len(LANE_THREAD_PREFIX) :]
            if _is_safe_lane_id(lane_id):
                return chat_id, thread_id
            return None
    if raw.isdigit() or raw.startswith("200000"):
        return raw, None
    return None


def _load_lane_state(path: Path) -> dict[str, Any]:
    empty = {"active": {}, "pending_create": {}, "pending_edit": {}, "custom_lanes": {}, "project_list_messages": {}, "project_list_pages": {}, "message_lanes": {}, "pinned": {}}
    try:
        if not path.exists():
            return dict(empty)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(empty)
        active = data.get("active") if isinstance(data.get("active"), dict) else {}
        pending = data.get("pending_create") if isinstance(data.get("pending_create"), dict) else {}
        pending_edit = data.get("pending_edit") if isinstance(data.get("pending_edit"), dict) else {}
        custom = data.get("custom_lanes") if isinstance(data.get("custom_lanes"), dict) else {}
        project_list_messages = data.get("project_list_messages") if isinstance(data.get("project_list_messages"), dict) else {}
        project_list_pages = data.get("project_list_pages") if isinstance(data.get("project_list_pages"), dict) else {}
        message_lanes = data.get("message_lanes") if isinstance(data.get("message_lanes"), dict) else {}
        pinned = data.get("pinned") if isinstance(data.get("pinned"), dict) else {}
        return {
            "active": dict(active),
            "pending_create": dict(pending),
            "pending_edit": dict(pending_edit),
            "custom_lanes": dict(custom),
            "project_list_messages": dict(project_list_messages),
            "project_list_pages": dict(project_list_pages),
            "message_lanes": dict(message_lanes),
            "pinned": dict(pinned),
        }
    except Exception as exc:
        logger.warning("VK: project lane state is unreadable; starting with empty state — %s", _redact_token(str(exc)))
        return dict(empty)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _project_create_prompt_text() -> str:
    return (
        "Новый проект. Опишите проект: название, назначение/контекст, папку/id и скиллы.\n\n"
        "Можно свободной фразой:\n"
        "Создай проект Rialo для ретродропа и аналитики. Папка rialo. "
        "Скиллы blockchain-project-research, coding.\n\n"
        "Или строго по полям:\n"
        "Название: Rialo\n"
        "Назначение: ретродроп и аналитика проекта Rialo\n"
        "Папка: rialo\n"
        "Скиллы: blockchain-project-research, coding"
    )


def _project_edit_prompt_text(lane: dict[str, Any]) -> str:
    return (
        f"Редактирование проекта: {lane.get('name') or lane.get('id')}.\n"
        "Напишите, что изменить: название, контекст/описание, папку, workdir, plan, скиллы или aliases.\n\n"
        "Пример:\n"
        "папка ai-projects/gito; workdir /home/assistent/ai-projects/gito; контекст: существующий проект Gito\n\n"
        "Или одной командой:\n"
        f"/project edit {lane.get('id')} папка ai-projects/gito; workdir /home/assistent/ai-projects/gito"
    )


def _extract_inline_value(text: str, labels: tuple[str, ...]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    next_labels = (
        r"папка|folder|workdir|рабочая папка|план-файл|plan(?: file)?|контекст|context|"
        r"описание|description|скиллы|skills|навыки"
    )
    match = re.search(
        rf"(?:^|[;,.\n])\s*(?:{label_pattern})\s*:?\s*([^;\n]+?)(?=\s*(?:[;,.]\s*(?:{next_labels})\b|\n\s*(?:{next_labels})\b)|$)",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(match.group(1).strip().split()) if match else ""


def _parse_project_new_fallback(args: str) -> dict[str, Any] | None:
    parts = str(args or "").strip().split(maxsplit=2)
    if len(parts) < 3:
        return None
    name, skills_raw, context = parts
    skills = _normalize_lane_skills(skills_raw)
    slug = _slugify_lane_id(name)
    if not name.strip() or not skills or not context.strip() or not _is_safe_lane_id(slug):
        return None
    folder = _extract_inline_value(context, ("папка", "folder")) or slug
    workdir = _extract_inline_value(context, ("workdir", "рабочая папка"))
    plan_file = _extract_inline_value(context, ("план-файл проекта", "план-файл", "plan file", "plan"))
    lane = {"id": slug, "name": name.strip(), "description": context.strip(), "folder": folder, "skills": skills}
    if workdir:
        lane["workdir"] = workdir
    if plan_file:
        lane["plan"] = plan_file
    return lane


def _parse_project_edit_text(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    updates: dict[str, Any] = {}
    name = _extract_inline_value(raw, ("название", "имя", "name")) or _extract_labeled_value(raw, ("Название", "Имя", "Name"))
    folder = _extract_inline_value(raw, ("папка", "folder")) or _extract_labeled_value(raw, ("Папка", "Folder", "ID", "Id", "id"))
    workdir = _extract_inline_value(raw, ("workdir", "рабочая папка")) or _extract_labeled_value(raw, ("Workdir", "Рабочая папка"))
    plan_file = _extract_inline_value(raw, ("план-файл проекта", "план-файл", "plan file", "plan")) or _extract_labeled_value(raw, ("План", "Plan", "Plan file"))
    context = _extract_inline_value(raw, ("контекст", "описание", "context", "description")) or _extract_labeled_value(raw, ("Назначение", "Контекст", "Описание", "Context", "Description"))
    skills_raw = _extract_inline_value(raw, ("скиллы", "skills", "навыки")) or _extract_labeled_value(raw, ("Скиллы", "Навыки", "Skills"))
    aliases_raw = _extract_inline_value(raw, ("aliases", "alias", "алиасы", "псевдонимы")) or _extract_labeled_value(raw, ("Aliases", "Алиасы", "Псевдонимы"))
    if name:
        updates["name"] = _sanitize_lane_label(name, 80)
    if folder:
        updates["folder"] = folder
    if workdir:
        updates["workdir"] = workdir
    if plan_file:
        updates["plan"] = plan_file
    if context:
        updates["description"] = context
    if skills_raw:
        skills = _normalize_lane_skills(skills_raw.replace(" и ", ","))
        if skills:
            updates["skills"] = skills
    if aliases_raw:
        aliases = [item.strip().lstrip("@") for item in re.split(r"[,\s]+", aliases_raw) if item.strip().lstrip("@")]
        if aliases:
            updates["aliases"] = aliases
    return updates or None


def _extract_labeled_value(text: str, labels: tuple[str, ...]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    next_labels = (
        "Название|Имя|Name|Проект|Project|Назначение|Контекст|Описание|Context|Description|"
        "Папка|Folder|ID|Id|id|Скиллы|Навыки|Skills"
    )
    match = re.search(
        rf"(?:^|\n)\s*(?:{label_pattern})\s*:\s*(.*?)(?=\n\s*(?:{next_labels})\s*:|\Z)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return " ".join(match.group(1).strip().split()) if match else ""


def _parse_project_create_text(text: str) -> dict[str, Any] | None:
    """Parse labeled or simple free-form project creation text deterministically.

    This is a local convenience path. If it cannot parse safely, the adapter
    routes the message to a normal Hermes turn with the project-creation prompt.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    name = _extract_labeled_value(raw, ("Название", "Имя", "Name", "Проект", "Project"))
    context = _extract_labeled_value(raw, ("Назначение", "Контекст", "Описание", "Context", "Description"))
    folder = _extract_labeled_value(raw, ("Папка", "Folder", "ID", "Id", "id"))
    skills_raw = _extract_labeled_value(raw, ("Скиллы", "Навыки", "Skills"))

    if not any((name, context, folder, skills_raw)):
        # Simple Russian/English free-form fallback, intentionally conservative.
        name_match = re.search(r"(?:проект|project)\s+([A-Za-z0-9_-]{2,64})", raw, flags=re.IGNORECASE)
        folder_match = re.search(r"(?:папка|folder|id)\s+([A-Za-z0-9_-]{2,64})", raw, flags=re.IGNORECASE)
        skills_match = re.search(r"(?:скиллы|skills|навыки)\s+([^\.\n]+)", raw, flags=re.IGNORECASE)
        if name_match:
            name = name_match.group(1)
        if folder_match:
            folder = folder_match.group(1)
        if skills_match:
            skills_raw = skills_match.group(1)
        context = raw

    skills = _normalize_lane_skills(skills_raw.replace(" и ", ",")) if skills_raw else []
    slug = _slugify_lane_id(folder or name)
    if not name or not context or not skills or not _is_safe_lane_id(slug):
        return None
    return {
        "id": slug,
        "name": _sanitize_lane_label(name, 80),
        "description": context,
        "folder": slug,
        "workdir": "",
        "skills": skills,
        "aliases": [slug] if slug.lower() != name.lower() else [],
    }


def _format_lane_yaml_snippet(lane: dict[str, Any]) -> str:
    lines = [
        f"        - id: {lane['id']}",
        f"          name: {lane['name']}",
        f"          description: {lane['description']}",
        f"          folder: {lane.get('folder') or lane['id']}",
        "          skills:",
    ]
    for skill in lane.get("skills") or []:
        lines.append(f"            - {skill}")
    return "\n".join(lines)


def _parse_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


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


def _vk_api_error_message(code: Any, msg: Any) -> str:
    """Return a redacted, operator-actionable VK API error message."""
    base = f"VK API error {code}: {msg}"
    if str(code) == "912":
        base += (
            "\n\nHint: VK says this is a chat-bot-only feature. "
            "For community bots in group conversations, open the VK community "
            "settings and enable bot capabilities for conversations: community "
            "messages, bot/Long Poll message events, and the setting that allows "
            "the community bot to work in chats / be added to conversations. "
            "In the Russian VK UI this is usually: Управление сообществом → "
            "Сообщения → Настройки для бота → Возможности ботов. Enable "
            "'Возможности ботов' and 'Разрешать добавлять сообщество в чаты'. "
            "After changing VK settings, restart the Hermes gateway and test the peer again."
        )
    return _redact_token(base)


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
        raise RuntimeError(_vk_api_error_message(code, msg))
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


def _is_vk_auth_or_permission_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "vk api error 5" in text
        or "user authorization failed" in text
        or "vk api error 7" in text
        or "permission" in text
        or "vk api error 15" in text
        or "access denied" in text
    )


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

        # Message-reaction acks (mirrors Telegram's 👀 → ✅/❌ lifecycle):
        # progress reaction on intake, swapped for OK/FAIL when processing
        # completes. VK identifies reactions by numeric id, not emoji; ids are
        # community-scoped, so the common defaults are exposed as env vars
        # (see the _VK_REACTION_* constants). 0 disables a step.
        self.reactions_enabled = _truthy(os.getenv("VK_REACTIONS_ENABLED") or extra.get("reactions_enabled"))
        self.reaction_progress = _parse_non_negative_int(
            os.getenv("VK_REACTION_PROGRESS") or extra.get("reaction_progress"),
            _VK_REACTION_PROGRESS_DEFAULT,
        )
        self.reaction_ok = _parse_non_negative_int(
            os.getenv("VK_REACTION_OK") or extra.get("reaction_ok"), _VK_REACTION_OK
        )
        self.reaction_fail = _parse_non_negative_int(
            os.getenv("VK_REACTION_FAIL") or extra.get("reaction_fail"), _VK_REACTION_FAIL
        )
        self._delete_reaction_supported: Optional[bool] = None

        self.fallback_poll_enabled = _truthy(os.getenv("VK_FALLBACK_POLL_ENABLED") or extra.get("fallback_poll_enabled") or "true")
        self.fallback_poll_interval_seconds = _parse_positive_int(
            os.getenv("VK_FALLBACK_POLL_INTERVAL_SECONDS") or extra.get("fallback_poll_interval_seconds"),
            15,
        )
        self.fallback_poll_batch_size = _parse_positive_int(
            os.getenv("VK_FALLBACK_POLL_BATCH_SIZE") or extra.get("fallback_poll_batch_size"),
            25,
        )

        self._poll_task: Optional[asyncio.Task] = None
        self._fallback_poll_task: Optional[asyncio.Task] = None
        self._fallback_last_cmid: dict[str, int] = {}
        self._fallback_bootstrapped_logged = False
        self._lp_server = ""
        self._lp_key = ""
        self._lp_ts = ""
        self._lp_no_update_cycles = 0
        self._lock_key: Optional[str] = None
        self._conversation_context_cache: dict[str, tuple[str, str]] = {}
        self._seen_message_keys: dict[str, float] = {}
        self._seen_edit_keys: dict[str, float] = {}
        self._approval_counter = 0
        self._approval_state: dict[int, str] = {}
        self._slash_confirm_state: dict[str, str] = {}
        self._clarify_state: dict[str, str] = {}
        try:
            raw_project_lanes = extra.get("project_lanes")
            raw_platforms = extra.get("platforms") if isinstance(extra.get("platforms"), dict) else None
            if raw_project_lanes is None:
                vk_extra = (((raw_platforms or {}).get("vk") or {}).get("extra") or {})
                if isinstance(vk_extra, dict):
                    raw_project_lanes = vk_extra.get("project_lanes")
            self.project_lanes = _normalize_project_lanes(_extract_project_lanes_config(raw_project_lanes))
        except Exception as exc:
            logger.warning("VK: invalid project_lanes config ignored — %s", _redact_token(str(exc)))
            self.project_lanes = {}
        self._lane_state_path = _lane_state_path()
        self._lane_state = _load_lane_state(self._lane_state_path)

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
        if self.fallback_poll_enabled:
            self._fallback_poll_task = asyncio.create_task(self._fallback_poll_loop(), name="vk-fallback-poll")
        self._mark_connected()
        logger.info("VK: connected to group %s via long poll", self.group_id)
        return True

    async def disconnect(self) -> None:
        for task in (self._poll_task, self._fallback_poll_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._poll_task = None
        self._fallback_poll_task = None
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

    # ── Message-reaction ack lifecycle (👌 → 👍/👎) ────────────────────────

    async def _send_reaction(self, chat_id: Any, message_id: Any, reaction_id: int) -> bool:
        """Set a numeric reaction on a peer message. Best-effort (False on failure).

        ``message_id`` is the VK conversation_message_id (cmid), which is what
        MessageEvent.message_id carries for inbound messages.
        """
        try:
            peer_id = int(str(chat_id).strip())
            cmid = int(str(message_id).strip())
            reaction = int(reaction_id)
        except (TypeError, ValueError):
            return False
        try:
            result = await self._vk_method(
                "messages.sendReaction",
                {"peer_id": peer_id, "cmid": cmid, "reaction_id": reaction},
            )
        except Exception as exc:
            logger.debug("VK: sendReaction failed — %s", _redact_token(str(exc)))
            return False
        if "error" in result:
            logger.debug(
                "VK: sendReaction error %s — %s",
                result["error"].get("error_code"),
                result["error"].get("error_msg", ""),
            )
            return False
        return bool(result.get("response"))

    async def _remove_reacted(self, chat_id: str, message_id: str) -> bool:
        """Remove our reaction from a message. Soft-fails (False), never raises.

        A community token can only retract the reaction it set itself. Some API
        versions do not expose ``messages.deleteReaction``; the first call
        records support in ``_delete_reaction_supported`` so later calls skip
        the API round-trip when the method is unavailable.
        """
        if self._delete_reaction_supported is False:
            return False
        try:
            peer_id = int(str(chat_id).strip())
            cmid = int(str(message_id).strip())
        except (TypeError, ValueError):
            return False
        try:
            result = await self._vk_method(
                "messages.deleteReaction", {"peer_id": peer_id, "cmid": cmid}
            )
        except Exception as exc:
            logger.debug("VK: deleteReaction failed — %s", _redact_token(str(exc)))
            return False
        err = result.get("error") or {}
        if err.get("error_code") == 3:  # Unknown method passed
            self._delete_reaction_supported = False
            return False
        if "error" in result:
            logger.debug("VK: deleteReaction error %s — %s", err.get("error_code"), err.get("error_msg", ""))
            return False
        self._delete_reaction_supported = True
        return bool(result.get("response"))

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Set the progress reaction on the triggering message while the agent works."""
        if not self.reactions_enabled:
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if chat_id and message_id and self.reaction_progress > 0:
            await self._send_reaction(chat_id, message_id, self.reaction_progress)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Swap the progress reaction for a final OK/FAIL reaction (VK numeric ids)."""
        if not self.reactions_enabled:
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if not chat_id or not message_id:
            return
        if outcome == ProcessingOutcome.CANCELLED:
            if self.reaction_progress > 0:
                await self._remove_reacted(str(chat_id), str(message_id))
            return
        final_id = self.reaction_ok if outcome == ProcessingOutcome.SUCCESS else self.reaction_fail
        if final_id <= 0:
            await self._remove_reacted(str(chat_id), str(message_id))
            return
        # VK replaces a sender's previous reaction on sendReaction, so sending
        # the final id directly replaces the intake ack. Communities that
        # disable the progress reaction (0) just get the final one.
        await self._send_reaction(chat_id, message_id, final_id)

    def _state_key(self, peer_id: str, user_id: str) -> str:
        return f"{peer_id}:{user_id}"

    def _message_lane_key(self, peer_id: str, message_id: Any) -> str:
        return f"{peer_id}:{message_id}"

    def _lane_from_thread_id(self, peer_id: str, thread_id: Any) -> dict[str, Any] | None:
        raw = str(thread_id or "").strip()
        if not raw.startswith(LANE_THREAD_PREFIX):
            return None
        return self._resolve_lane(peer_id, raw[len(LANE_THREAD_PREFIX) :])

    async def _remember_message_lane(self, peer_id: str, message_id: Any, thread_id: Any) -> None:
        lane = self._lane_from_thread_id(peer_id, thread_id)
        if not lane or not message_id:
            return
        try:
            session_store = getattr(self, "_session_store", None)
            if session_store is not None:
                from gateway.config import Platform
                from gateway.session import SessionSource

                session_store.get_or_create_session(
                    SessionSource(
                        platform=Platform("vk"),
                        chat_id=str(peer_id),
                        chat_type="thread",
                        user_id="system:cron",
                        user_name="Cron",
                        thread_id=f"{LANE_THREAD_PREFIX}{lane['id']}",
                        chat_topic=lane.get("name"),
                    )
                )
        except Exception as exc:
            logger.debug("VK: failed to pre-create lane session for delivered message — %s", _redact_token(str(exc)))
        messages = self._lane_state.setdefault("message_lanes", {})
        if not isinstance(messages, dict):
            messages = {}
            self._lane_state["message_lanes"] = messages
        key = self._message_lane_key(str(peer_id), str(message_id).removeprefix("cmid:"))
        messages[key] = lane["id"]
        try:
            await self._persist_lane_state()
        except Exception as exc:
            logger.warning("VK: failed to persist message lane mapping — %s", _redact_token(str(exc)))

    def _reply_lane(self, peer_id: str, msg: dict[str, Any]) -> dict[str, Any] | None:
        reply = msg.get("reply_message") if isinstance(msg.get("reply_message"), dict) else None
        if not reply:
            return None
        reply_id = reply.get("conversation_message_id") or reply.get("id")
        if reply_id is None:
            return None
        messages_obj = self._lane_state.get("message_lanes")
        messages = messages_obj if isinstance(messages_obj, dict) else {}
        lane_id = messages.get(self._message_lane_key(str(peer_id), str(reply_id)))
        return self._resolve_lane(peer_id, str(lane_id or "")) if lane_id else None

    def _peer_lanes(self, peer_id: str) -> dict[str, Any] | None:
        peer = str(peer_id)
        base = self.project_lanes.get(peer) if isinstance(self.project_lanes, dict) else None
        cfg: dict[str, Any] = {
            "base_folder": "",
            "default_skills": [],
            "lanes": [],
            "lane_by_id": {},
            "alias_to_id": {},
        }
        if isinstance(base, dict):
            cfg["base_folder"] = base.get("base_folder") or ""
            cfg["default_skills"] = list(base.get("default_skills") or [])
            for lane in base.get("lanes") or []:
                if isinstance(lane, dict) and lane.get("id") not in cfg["lane_by_id"]:
                    cfg["lanes"].append(lane)
                    cfg["lane_by_id"][lane["id"]] = lane
            cfg["alias_to_id"].update(base.get("alias_to_id") or {})
        custom_raw = (self._lane_state.get("custom_lanes") or {}).get(peer) or []
        custom_cfg = _normalize_project_lanes({peer: {"lanes": custom_raw}}).get(peer) or {}
        for lane in custom_cfg.get("lanes") or []:
            if lane.get("id") in cfg["lane_by_id"]:
                for idx, existing in enumerate(cfg["lanes"]):
                    if existing.get("id") == lane.get("id"):
                        cfg["lanes"][idx] = lane
                        break
                cfg["lane_by_id"][lane["id"]] = lane
                continue
            cfg["lanes"].append(lane)
            cfg["lane_by_id"][lane["id"]] = lane
        cfg["alias_to_id"].update(custom_cfg.get("alias_to_id") or {})
        return cfg if cfg["lanes"] else None

    def _should_attach_project_keyboard(self, chat_id: str) -> bool:
        """Return True when the VK peer/DM should receive the persistent helper keyboard.

        The keyboard is an accessibility affordance, not evidence that a peer
        already has lanes.  Authorized chats and DMs should expose `Проекты`,
        `Новый проект`, and `Команды` so users do not need to memorize slash
        commands before the first project exists.
        """
        peer = str(chat_id or "").strip()
        if not peer:
            return False
        if self.allow_all_users:
            return True
        if peer in self.allowed_peers:
            return True
        if peer in self.allowed_users:
            return True
        if self.home_channel and peer == self.home_channel:
            return True
        if isinstance(self.project_lanes, dict) and peer in self.project_lanes:
            return True
        return False

    def _lane_last_activity(self, peer_id: str, lane_ids: list[str]) -> dict[str, float]:
        """Return latest Hermes session activity per VK project lane.

        Project lanes are synthetic Hermes threads (`thread_id = lane:<id>`), so
        the canonical recency source is the shared session database.  Fail open:
        if the DB is unavailable or migrated, keep config order instead of
        breaking `/project list`.
        """
        safe_ids = [str(lane_id or "").strip() for lane_id in lane_ids if str(lane_id or "").strip()]
        if not safe_ids:
            return {}
        db_path = _session_state_db_path()
        if not db_path.exists():
            return {}
        placeholders = ",".join("?" for _ in safe_ids)
        thread_ids = [f"{LANE_THREAD_PREFIX}{lane_id}" for lane_id in safe_ids]
        try:
            uri = db_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1.0) as con:
                rows = con.execute(
                    f"""
                    SELECT thread_id, MAX(COALESCE(last_activity_at, ended_at, started_at, 0)) AS activity
                    FROM sessions
                    WHERE source = 'vk'
                      AND chat_id = ?
                      AND chat_type = 'thread'
                      AND thread_id IN ({placeholders})
                    GROUP BY thread_id
                    """,
                    [str(peer_id), *thread_ids],
                ).fetchall()
        except Exception as exc:
            logger.debug("VK: could not read lane session recency; keeping config order — %s", _redact_token(str(exc)))
            return {}
        result: dict[str, float] = {}
        for thread_id, activity in rows:
            lane_id = str(thread_id or "")
            if lane_id.startswith(LANE_THREAD_PREFIX):
                lane_id = lane_id[len(LANE_THREAD_PREFIX) :]
            try:
                result[lane_id] = float(activity or 0)
            except (TypeError, ValueError):
                continue
        return result

    def _sort_lanes_by_session_recency(self, peer_id: str, lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        activity = self._lane_last_activity(peer_id, [str(lane.get("id") or "") for lane in lanes if isinstance(lane, dict)])
        pinned_obj = self._lane_state.get("pinned")
        pinned_for_peer = (pinned_obj if isinstance(pinned_obj, dict) else {}).get(str(peer_id))
        pinned_ids = {str(item) for item in pinned_for_peer} if isinstance(pinned_for_peer, list) else set()
        if not activity and not pinned_ids:
            return lanes
        ordered = sorted(
            enumerate(lanes),
            key=lambda pair: (
                str(pair[1].get("id") or "") in pinned_ids,
                activity.get(str(pair[1].get("id") or ""), 0.0),
                -pair[0],
            ),
            reverse=True,
        )
        return [lane for _idx, lane in ordered]

    def _is_pinned_lane(self, peer_id: str, lane_id: str) -> bool:
        pinned_obj = self._lane_state.get("pinned")
        pinned_for_peer = (pinned_obj if isinstance(pinned_obj, dict) else {}).get(str(peer_id))
        return str(lane_id) in {str(item) for item in pinned_for_peer} if isinstance(pinned_for_peer, list) else False

    async def _set_pinned_lane(self, peer_id: str, lane_id: str, pinned: bool) -> bool:
        lane = self._resolve_lane(peer_id, lane_id)
        if not lane:
            return False
        pinned_root = self._lane_state.setdefault("pinned", {})
        if not isinstance(pinned_root, dict):
            pinned_root = {}
            self._lane_state["pinned"] = pinned_root
        current = [str(item) for item in pinned_root.get(str(peer_id), [])] if isinstance(pinned_root.get(str(peer_id)), list) else []
        if pinned and lane["id"] not in current:
            current.insert(0, lane["id"])
        elif not pinned:
            current = [item for item in current if item != lane["id"]]
        pinned_root[str(peer_id)] = current
        try:
            await self._persist_lane_state()
        except Exception as exc:
            logger.warning("VK: failed to persist pinned project state — %s", _redact_token(str(exc)))
        return True

    def _resolve_lane(self, peer_id: str, token: str) -> dict[str, Any] | None:
        cfg = self._peer_lanes(peer_id)
        if not cfg:
            return None
        lookup = str(token or "").strip().lower()
        lane_id = (cfg.get("alias_to_id") or {}).get(lookup) or lookup
        lane = (cfg.get("lane_by_id") or {}).get(lane_id)
        if isinstance(lane, dict):
            return lane
        for candidate in cfg.get("lanes") or []:
            if isinstance(candidate, dict) and str(candidate.get("name") or "").strip().lower() == lookup:
                return candidate
        return None

    def _get_active_lane_id(self, peer_id: str, user_id: str) -> str | None:
        key = self._state_key(peer_id, user_id)
        lane_id = str((self._lane_state.get("active") or {}).get(key) or "").strip()
        if lane_id and self._resolve_lane(peer_id, lane_id):
            return lane_id
        return None

    async def _persist_lane_state(self) -> None:
        data = self._lane_state
        path = self._lane_state_path
        await asyncio.to_thread(_atomic_write_json, path, data)

    async def _persist_lane_config(self, peer_id: str, lane: dict[str, Any]) -> bool:
        def write() -> bool:
            from hermes_cli.config import load_config, save_config

            cfg = load_config()
            if not isinstance(cfg, dict):
                cfg = {}
            _upsert_lane_in_config(cfg, str(peer_id), lane)
            save_config(
                cfg,
                preserve_keys={
                    ("platforms", "vk", "extra", "project_lanes"),
                    ("platforms", "vk", "extra", "project_lanes", "chats", str(peer_id)),
                },
            )
            return True

        try:
            await asyncio.to_thread(write)
            cfg = self.project_lanes.setdefault(str(peer_id), {"base_folder": "", "default_skills": [], "lanes": [], "lane_by_id": {}, "alias_to_id": {}})
            normalized = _normalize_project_lanes({str(peer_id): {"lanes": [lane]}}).get(str(peer_id))
            if normalized and normalized.get("lanes"):
                new_lane = normalized["lanes"][0]
                lanes = cfg.setdefault("lanes", [])
                replaced = False
                for idx, item in enumerate(lanes):
                    if isinstance(item, dict) and item.get("id") == new_lane["id"]:
                        lanes[idx] = new_lane
                        replaced = True
                        break
                if not replaced:
                    lanes.append(new_lane)
                cfg.setdefault("lane_by_id", {})[new_lane["id"]] = new_lane
                cfg.setdefault("alias_to_id", {})
                for alias in new_lane.get("aliases") or []:
                    cfg["alias_to_id"][alias] = new_lane["id"]
            return True
        except Exception as exc:
            logger.warning("VK: failed to persist project lane config — %s", _redact_token(str(exc)))
            return False

    async def _add_custom_lane(self, peer_id: str, lane: dict[str, Any]) -> bool:
        normalized = _normalize_project_lanes({str(peer_id): {"lanes": [lane]}}).get(str(peer_id))
        if not normalized or not normalized.get("lanes"):
            return False
        lane = normalized["lanes"][0]
        persisted = await self._persist_lane_config(peer_id, lane)
        if not persisted:
            custom = self._lane_state.setdefault("custom_lanes", {})
            lanes = custom.setdefault(str(peer_id), [])
            if not isinstance(lanes, list):
                lanes = []
                custom[str(peer_id)] = lanes
            replaced = False
            for idx, item in enumerate(lanes):
                if isinstance(item, dict) and str(item.get("id") or "") == lane["id"]:
                    lanes[idx] = lane
                    replaced = True
                    break
            if not replaced:
                lanes.append(lane)
        try:
            await self._persist_lane_state()
        except Exception as exc:
            logger.warning("VK: failed to persist custom project lane — %s", _redact_token(str(exc)))
        return True

    async def _update_custom_lane(self, peer_id: str, lane_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        existing = self._resolve_lane(peer_id, lane_id)
        if not existing or not updates:
            return None
        merged = dict(existing)
        merged.update(updates)
        merged["id"] = str(existing.get("id") or lane_id)
        normalized = _normalize_project_lanes({str(peer_id): {"lanes": [merged]}}).get(str(peer_id))
        if not normalized or not normalized.get("lanes"):
            return None
        lane = normalized["lanes"][0]
        custom = self._lane_state.setdefault("custom_lanes", {})
        lanes = custom.setdefault(str(peer_id), [])
        if not isinstance(lanes, list):
            lanes = []
            custom[str(peer_id)] = lanes
        replaced = False
        for idx, item in enumerate(lanes):
            if isinstance(item, dict) and str(item.get("id") or "") == lane["id"]:
                lanes[idx] = lane
                replaced = True
                break
        if not replaced:
            lanes.append(lane)
        await self._persist_lane_config(peer_id, lane)
        try:
            await self._persist_lane_state()
        except Exception as exc:
            logger.warning("VK: failed to persist edited project lane — %s", _redact_token(str(exc)))
        return lane

    async def _set_active_lane_id(self, peer_id: str, user_id: str, lane_id: str | None) -> bool:
        if lane_id and not self._resolve_lane(peer_id, lane_id):
            return False
        active = self._lane_state.setdefault("active", {})
        key = self._state_key(peer_id, user_id)
        if lane_id:
            active[key] = str(lane_id)
        else:
            active.pop(key, None)
        try:
            await self._persist_lane_state()
        except Exception as exc:
            logger.warning("VK: failed to persist project lane state — %s", _redact_token(str(exc)))
        return True

    def _pending_create(self, peer_id: str, user_id: str) -> dict[str, Any] | None:
        pending = self._lane_state.get("pending_create") or {}
        item = pending.get(self._state_key(peer_id, user_id))
        if not isinstance(item, dict):
            return None
        if float(item.get("expires_at") or 0) < time.time():
            pending.pop(self._state_key(peer_id, user_id), None)
            return None
        return item

    async def _set_pending_create(self, peer_id: str, user_id: str) -> None:
        pending = self._lane_state.setdefault("pending_create", {})
        pending[self._state_key(peer_id, user_id)] = {"expires_at": time.time() + PROJECT_CREATE_TTL_SECONDS}
        try:
            await self._persist_lane_state()
        except Exception as exc:
            logger.warning("VK: failed to persist project create state — %s", _redact_token(str(exc)))

    async def _clear_pending_create(self, peer_id: str, user_id: str) -> None:
        pending = self._lane_state.setdefault("pending_create", {})
        pending.pop(self._state_key(peer_id, user_id), None)
        try:
            await self._persist_lane_state()
        except Exception as exc:
            logger.warning("VK: failed to persist project create state — %s", _redact_token(str(exc)))

    def _pending_edit(self, peer_id: str, user_id: str) -> dict[str, Any] | None:
        pending = self._lane_state.get("pending_edit") or {}
        item = pending.get(self._state_key(peer_id, user_id))
        if not isinstance(item, dict):
            return None
        if float(item.get("expires_at") or 0) < time.time():
            pending.pop(self._state_key(peer_id, user_id), None)
            return None
        lane_id = str(item.get("lane_id") or "")
        if not lane_id or not self._resolve_lane(peer_id, lane_id):
            pending.pop(self._state_key(peer_id, user_id), None)
            return None
        return item

    async def _set_pending_edit(self, peer_id: str, user_id: str, lane_id: str) -> None:
        pending = self._lane_state.setdefault("pending_edit", {})
        pending[self._state_key(peer_id, user_id)] = {"lane_id": lane_id, "expires_at": time.time() + PROJECT_CREATE_TTL_SECONDS}
        try:
            await self._persist_lane_state()
        except Exception as exc:
            logger.warning("VK: failed to persist project edit state — %s", _redact_token(str(exc)))

    async def _clear_pending_edit(self, peer_id: str, user_id: str) -> None:
        pending = self._lane_state.setdefault("pending_edit", {})
        pending.pop(self._state_key(peer_id, user_id), None)
        try:
            await self._persist_lane_state()
        except Exception as exc:
            logger.warning("VK: failed to persist project edit state — %s", _redact_token(str(exc)))

    def _project_keyboard(self) -> str:
        buttons = [
            [{"action": {"type": "text", "label": "Проекты", "payload": json.dumps({"vkpl": "list"}, ensure_ascii=False)}, "color": "secondary"}],
            [{"action": {"type": "text", "label": "Новый проект", "payload": json.dumps({"vkpl": "new"}, ensure_ascii=False)}, "color": "primary"}],
            [{"action": {"type": "text", "label": "Команды", "payload": json.dumps({"vkpl": "commands"}, ensure_ascii=False)}, "color": "secondary"}],
        ]
        return json.dumps({"one_time": False, "inline": False, "buttons": buttons}, ensure_ascii=False)

    def _project_cancel_keyboard(self) -> str:
        payload = json.dumps({"vkpl": "cancel"}, ensure_ascii=False)
        return json.dumps(
            {
                "one_time": False,
                "inline": True,
                "buttons": [[{"action": {"type": "callback", "label": "Отмена", "payload": payload}}]],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _inline_callback_keyboard(buttons: list[tuple[str, dict[str, Any]]], *, row_size: int = 2) -> str:
        rows: list[list[dict[str, Any]]] = []
        row: list[dict[str, Any]] = []
        for label, payload_obj in buttons:
            row.append(
                {
                    "action": {
                        "type": "callback",
                        "label": label,
                        "payload": json.dumps(payload_obj, ensure_ascii=False),
                    }
                }
            )
            if len(row) >= row_size:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return json.dumps({"one_time": False, "inline": True, "buttons": rows}, ensure_ascii=False)

    def _exec_approval_keyboard(self, approval_id: int, *, allow_permanent: bool, allow_session: bool, smart_denied: bool) -> str:
        buttons: list[tuple[str, dict[str, Any]]] = [
            ("✅ Allow Once", {"vkea": "once", "id": approval_id}),
        ]
        if not smart_denied and allow_session:
            buttons.append(("✅ Session", {"vkea": "session", "id": approval_id}))
            if allow_permanent:
                buttons.append(("✅ Always", {"vkea": "always", "id": approval_id}))
        buttons.append(("❌ Deny", {"vkea": "deny", "id": approval_id}))
        return self._inline_callback_keyboard(buttons, row_size=2)

    def _slash_confirm_keyboard(self, confirm_id: str) -> str:
        return self._inline_callback_keyboard(
            [
                ("✅ Approve Once", {"vksc": "once", "id": confirm_id}),
                ("🔒 Always Approve", {"vksc": "always", "id": confirm_id}),
                ("❌ Cancel", {"vksc": "cancel", "id": confirm_id}),
            ],
            row_size=2,
        )

    def _clarify_keyboard(self, clarify_id: str, choices: list[Any]) -> str:
        rows: list[list[dict[str, Any]]] = []
        row: list[dict[str, Any]] = []
        for idx in range(len(choices)):
            row.append(
                {
                    "action": {
                        "type": "text",
                        "label": str(idx + 1),
                        "payload": json.dumps({"vkcl": str(idx), "id": clarify_id}, ensure_ascii=False),
                    }
                }
            )
            if len(row) >= 5:
                rows.append(row)
                row = []
        row.append(
            {
                "action": {
                    "type": "text",
                    "label": "✏️ Свой ответ",
                    "payload": json.dumps({"vkcl": "other", "id": clarify_id}, ensure_ascii=False),
                }
            }
        )
        if row:
            rows.append(row)
        return json.dumps({"one_time": False, "inline": True, "buttons": rows}, ensure_ascii=False)

    @staticmethod
    def _empty_inline_keyboard() -> str:
        return json.dumps({"one_time": False, "inline": True, "buttons": []}, ensure_ascii=False)

    def _project_selected_keyboard(self, peer_id: str, lane_id: str) -> str:
        is_pinned = self._is_pinned_lane(peer_id, lane_id)
        action = "unpin" if is_pinned else "pin"
        label = "Открепить проект" if is_pinned else "Закрепить проект"
        payload = json.dumps({"vkpl": action, "id": lane_id}, ensure_ascii=False)
        return json.dumps(
            {
                "one_time": False,
                "inline": True,
                "buttons": [[{"action": {"type": "text", "label": label, "payload": payload}}]],
            },
            ensure_ascii=False,
        )

    def _project_list_items(self, peer_id: str, page: int = 0, page_size: int = 8) -> tuple[list[dict[str, Any]], int, int]:
        cfg = self._peer_lanes(peer_id) or {}
        lanes = self._sort_lanes_by_session_recency(str(peer_id), list(cfg.get("lanes") or []))
        total_pages = max(1, (len(lanes) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        return lanes[page * page_size : (page + 1) * page_size], page, total_pages

    def _project_list_text(self, peer_id: str, page: int = 0, page_size: int = 8) -> str:
        cfg = self._peer_lanes(peer_id) or {}
        lanes = self._sort_lanes_by_session_recency(str(peer_id), list(cfg.get("lanes") or []))
        if not lanes:
            return "Проекты пока не созданы. Используй кнопку Новый проект или /project new."
        visible, page, total_pages = self._project_list_items(peer_id, page=page, page_size=page_size)
        lines = ["Выберите проект:"]
        for lane in lanes:
            name = _sanitize_lane_label(lane.get("name"), 80)
            lane_id = str(lane.get("id") or "")
            aliases = ", ".join("@" + str(alias) for alias in (lane.get("aliases") or [])[:4])
            suffix = f"; aliases: {aliases}" if aliases else ""
            lines.append(f"- {name}: /project {lane_id}{suffix}")
        if len(lanes) > len(visible):
            lines.append(f"Кнопками показаны первые {len(visible)} проектов; остальные доступны текстовой командой из списка.")
        else:
            lines.append("Если кнопки видны, можно нажать кнопку проекта; если нет — используй строку выше.")
        return "\n".join(lines)

    def _project_commands_text(self) -> str:
        return "\n".join(
            [
                "Команды VK-чата:",
                "/project — текущий проект и меню",
                "/project list — список проектов",
                "/project list N — открыть страницу N списка проектов",
                "/project search <query> — найти проект",
                "/project <id-or-alias> — выбрать проект",
                "@alias <text> — разовое сообщение в проект без переключения",
                "/project new — создать проект пошагово",
                "/project new <name> <skills_csv> <context> — создать проект одной командой",
                "/project edit — изменить текущий проект пошагово",
                "/project edit <id> <что изменить> — изменить проект одной командой",
                "/project pin — закрепить текущий проект первым в списке",
                "/project unpin — открепить текущий проект",
                "/project off — выйти из проектного режима",
                "/invite — актуальный инвайт в текущий VK-чат",
                "Кнопки ниже кликабельные: нажми команду, и VK отправит её в чат.",
            ]
        )

    def _project_commands_keyboard(self) -> str:
        rows = [
            [
                {"action": {"type": "text", "label": "/project", "payload": json.dumps({"vkpl": "cmd", "cmd": "/project"}, ensure_ascii=False)}},
                {"action": {"type": "text", "label": "/project list", "payload": json.dumps({"vkpl": "cmd", "cmd": "/project list"}, ensure_ascii=False)}},
            ],
            [
                {"action": {"type": "text", "label": "/project new", "payload": json.dumps({"vkpl": "cmd", "cmd": "/project new"}, ensure_ascii=False)}},
                {"action": {"type": "text", "label": "/project edit", "payload": json.dumps({"vkpl": "cmd", "cmd": "/project edit"}, ensure_ascii=False)}},
            ],
            [
                {"action": {"type": "text", "label": "/project pin", "payload": json.dumps({"vkpl": "cmd", "cmd": "/project pin"}, ensure_ascii=False)}},
                {"action": {"type": "text", "label": "/project unpin", "payload": json.dumps({"vkpl": "cmd", "cmd": "/project unpin"}, ensure_ascii=False)}},
            ],
            [
                {"action": {"type": "text", "label": "/project off", "payload": json.dumps({"vkpl": "cmd", "cmd": "/project off"}, ensure_ascii=False)}},
                {"action": {"type": "text", "label": "/invite", "payload": json.dumps({"vkpl": "cmd", "cmd": "/invite"}, ensure_ascii=False)}},
            ],
        ]
        return json.dumps({"one_time": False, "inline": True, "buttons": rows}, ensure_ascii=False)

    def _strip_bot_mention_prefix(self, text: str) -> str:
        raw = str(text or "").strip()
        group_id = str(self.group_id or "").strip()
        if group_id:
            raw = re.sub(rf"^@club{re.escape(group_id)}\b\s*", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(rf"^\[club{re.escape(group_id)}\|[^\]]+\]\s*", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"^@club\d+\b\s+", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"^\[club\d+\|[^\]]+\]\s*", "", raw, flags=re.IGNORECASE).strip()
        return raw

    def _project_list_keyboard(self, peer_id: str, page: int = 0, page_size: int = 8) -> str:
        visible, page, total_pages = self._project_list_items(peer_id, page=page, page_size=page_size)
        rows: list[list[dict[str, Any]]] = []
        row: list[dict[str, Any]] = []
        for lane in visible:
            payload = json.dumps({"vkpl": "select", "id": lane["id"]}, ensure_ascii=False)
            row.append({"action": {"type": "text", "label": _sanitize_lane_label(lane.get("name"), 40), "payload": payload}})
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        nav: list[dict[str, Any]] = []
        if page > 0:
            nav.append(
                {
                    "action": {
                        "type": "text",
                        "label": "Предыдущая",
                        "payload": json.dumps({"vkpl": "page", "p": page - 1, "cmd": f"/project list {page}"}, ensure_ascii=False),
                    }
                }
            )
        if page + 1 < total_pages:
            nav.append(
                {
                    "action": {
                        "type": "text",
                        "label": "Следующая",
                        "payload": json.dumps({"vkpl": "page", "p": page + 1, "cmd": f"/project list {page + 2}"}, ensure_ascii=False),
                    }
                }
            )
        if nav:
            rows.append(nav)
        return json.dumps({"one_time": False, "inline": True, "buttons": rows}, ensure_ascii=False)

    async def _send_project_text(self, peer_id: str, text: str, *, keyboard: str | None = None) -> str | None:
        try:
            params = {"peer_ids": str(peer_id), "message": self.format_message(text), "random_id": random.randint(1, 2_147_483_647)}
            if keyboard:
                params["keyboard"] = keyboard
            payload = await self._vk_method("messages.send", params)
            return self._sent_message_id(payload)
        except Exception as exc:
            logger.warning("VK: failed to send project-lane control message — %s", _redact_token(str(exc)))
            return None

    async def _edit_project_text(self, peer_id: str, message_id: str, text: str, *, keyboard: str | None = None) -> bool:
        if not message_id or message_id in {"0", "__no_edit__"}:
            return False
        try:
            params = {"peer_id": str(peer_id), "message": self.format_message(text)}
            if str(message_id).startswith("cmid:"):
                params["conversation_message_id"] = str(message_id).split(":", 1)[1]
            else:
                params["message_id"] = str(message_id)
            if keyboard:
                params["keyboard"] = keyboard
            await self._vk_method("messages.edit", params)
            return True
        except Exception as exc:
            logger.debug("VK: failed to edit project-lane control message — %s", _redact_token(str(exc)))
            return False

    async def _send_or_edit_project_list(self, peer_id: str, user_id: str, page: int = 0, *, prefer_edit: bool = False) -> None:
        _visible, page, _total_pages = self._project_list_items(peer_id, page)
        text = self._project_list_text(peer_id, page)
        keyboard = self._project_list_keyboard(peer_id, page)
        state_key = self._state_key(peer_id, user_id)
        messages = self._lane_state.setdefault("project_list_messages", {})
        pages = self._lane_state.setdefault("project_list_pages", {})
        message_id = str(messages.get(state_key) or "") if isinstance(messages, dict) else ""
        edited = False
        if prefer_edit and message_id:
            edited = await self._edit_project_text(peer_id, message_id, text, keyboard=keyboard)
        if not edited:
            sent_id = await self._send_project_text(peer_id, text, keyboard=keyboard)
            if sent_id and isinstance(messages, dict):
                messages[state_key] = sent_id
        if edited or (isinstance(messages, dict) and messages.get(state_key)):
            if isinstance(pages, dict):
                pages[state_key] = page
            try:
                await self._persist_lane_state()
            except Exception as exc:
                logger.debug("VK: failed to persist project-list state — %s", _redact_token(str(exc)))

    async def _answer_message_event(self, event_id: str, user_id: str, peer_id: str, text: str) -> None:
        if not event_id:
            return
        try:
            await self._vk_method(
                "messages.sendMessageEventAnswer",
                {
                    "event_id": str(event_id),
                    "user_id": str(user_id),
                    "peer_id": str(peer_id),
                    "event_data": json.dumps({"type": "show_snackbar", "text": text[:90]}, ensure_ascii=False),
                },
            )
        except Exception as exc:
            logger.debug("VK: message_event answer failed — %s", _redact_token(str(exc)))

    async def _handle_clarify_payload(
        self,
        *,
        peer_id: str,
        user_id: str,
        payload: dict[str, Any],
        message_id: Any = None,
        event_id: str = "",
    ) -> bool:
        if payload.get("vkcl") is None:
            return False

        choice_token = str(payload.get("vkcl") or "")
        clarify_id = str(payload.get("id") or "")
        session_key = self._clarify_state.get(clarify_id)
        if not clarify_id or not session_key:
            await self._answer_message_event(event_id, user_id, peer_id, "Вопрос уже обработан или устарел")
            return True

        if choice_token == "other":
            flipped = False
            try:
                from tools.clarify_gateway import mark_awaiting_text

                flipped = mark_awaiting_text(clarify_id)
            except Exception as exc:
                logger.error("VK: clarify Other callback failed — %s", _redact_token(str(exc)), exc_info=True)
            if not flipped:
                self._clarify_state.pop(clarify_id, None)
                await self._answer_message_event(event_id, user_id, peer_id, "Вопрос уже устарел")
                return True
            label = "✏️ Напиши свой ответ в чат"
            await self._answer_message_event(event_id, user_id, peer_id, label)
            if message_id:
                await self._edit_project_text(peer_id, f"cmid:{message_id}", "✏️ Напиши свой ответ в чат.", keyboard=self._empty_inline_keyboard())
            return True

        try:
            idx = int(choice_token)
        except (TypeError, ValueError):
            await self._answer_message_event(event_id, user_id, peer_id, "Некорректный вариант")
            return True

        resolved_text: Optional[str] = None
        try:
            from tools.clarify_gateway import _entries as _clarify_entries  # type: ignore

            entry = _clarify_entries.get(clarify_id)
            if entry and entry.choices and 0 <= idx < len(entry.choices):
                resolved_text = str(entry.choices[idx])
        except Exception:
            resolved_text = None

        if resolved_text is None:
            await self._answer_message_event(event_id, user_id, peer_id, "Вопрос уже устарел")
            self._clarify_state.pop(clarify_id, None)
            return True

        resolved = False
        try:
            from tools.clarify_gateway import resolve_gateway_clarify

            resolved = resolve_gateway_clarify(clarify_id, resolved_text)
        except Exception as exc:
            logger.error("VK: failed to resolve clarify button — %s", _redact_token(str(exc)), exc_info=True)
        self._clarify_state.pop(clarify_id, None)
        if resolved:
            label = f"✓ {resolved_text[:60]}"
            try:
                self.resume_typing_for_chat(str(peer_id))
            except Exception:
                pass
        else:
            label = "⌛ Вопрос устарел"
        await self._answer_message_event(event_id, user_id, peer_id, label)
        if message_id:
            await self._edit_project_text(peer_id, f"cmid:{message_id}", label, keyboard=self._empty_inline_keyboard())
        return True

    async def _handle_message_event_update(self, update: dict[str, Any]) -> None:
        obj = update.get("object") or {}
        if not isinstance(obj, dict):
            return
        peer_id = str(obj.get("peer_id") or "")
        user_id = str(obj.get("user_id") or obj.get("from_id") or "")
        if not self._is_authorized(from_id=user_id, peer_id=peer_id):
            logger.info("VK: ignoring unauthorized callback sender=%s peer=%s", user_id, peer_id)
            return
        raw_payload = obj.get("payload") or {}
        if isinstance(raw_payload, str):
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                payload = {}
        else:
            payload = raw_payload if isinstance(raw_payload, dict) else {}

        if payload.get("vkea") is not None:
            choice = str(payload.get("vkea") or "")
            if choice not in {"once", "session", "always", "deny"}:
                await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, "Некорректное подтверждение")
                return
            try:
                approval_id = int(payload.get("id"))
            except (TypeError, ValueError):
                await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, "Некорректное подтверждение")
                return
            session_key = self._approval_state.pop(approval_id, None)
            if not session_key:
                await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, "Подтверждение уже обработано или устарело")
                return
            try:
                from tools.approval import resolve_gateway_approval

                count = resolve_gateway_approval(session_key, choice)
            except Exception as exc:
                logger.error("VK: failed to resolve approval button — %s", _redact_token(str(exc)), exc_info=True)
                count = 0
            if count:
                label_map = {
                    "once": "✅ Approved once",
                    "session": "✅ Approved for session",
                    "always": "✅ Approved permanently",
                    "deny": "❌ Denied",
                }
                label = label_map.get(choice, "Resolved")
                try:
                    self.resume_typing_for_chat(str(peer_id))
                except Exception:
                    pass
            else:
                label = "⌛ Approval expired"
            await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, label)
            message_id = obj.get("conversation_message_id") or obj.get("message_id") or obj.get("cmid")
            if message_id:
                await self._edit_project_text(peer_id, f"cmid:{message_id}", label, keyboard=self._empty_inline_keyboard())
            return

        if payload.get("vksc") is not None:
            choice = str(payload.get("vksc") or "")
            if choice not in {"once", "always", "cancel"}:
                await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, "Некорректное подтверждение")
                return
            confirm_id = str(payload.get("id") or "")
            session_key = self._slash_confirm_state.pop(confirm_id, None)
            if not confirm_id or not session_key:
                await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, "Подтверждение уже обработано или устарело")
                return
            label_map = {
                "once": "✅ Approved once",
                "always": "🔒 Always approve",
                "cancel": "❌ Cancelled",
            }
            label = label_map.get(choice, "Resolved")
            await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, label)
            message_id = obj.get("conversation_message_id") or obj.get("message_id") or obj.get("cmid")
            if message_id:
                await self._edit_project_text(peer_id, f"cmid:{message_id}", label, keyboard=self._empty_inline_keyboard())
            try:
                from tools import slash_confirm as _slash_confirm_mod

                result_text = await _slash_confirm_mod.resolve(session_key, confirm_id, choice)
                if result_text:
                    await self._send_project_text(peer_id, result_text)
            except Exception as exc:
                logger.error("VK: slash-confirm callback failed — %s", _redact_token(str(exc)), exc_info=True)
            return

        if await self._handle_clarify_payload(
            peer_id=peer_id,
            user_id=user_id,
            payload=payload,
            message_id=obj.get("conversation_message_id") or obj.get("message_id") or obj.get("cmid"),
            event_id=str(obj.get("event_id") or ""),
        ):
            return

        if payload.get("vkpl") is None:
            return
        action = str(payload.get("vkpl") or "")
        if action == "select":
            lane = self._resolve_lane(peer_id, str(payload.get("id") or ""))
            if not lane:
                await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, "Проект не найден")
                return
            await self._set_active_lane_id(peer_id, user_id, lane["id"])
            await self._send_project_text(
                peer_id,
                f"Проект выбран: {lane['name']}\n\nГде остановились:\n— пока нет истории\n\nПиши задачу. /new начнёт новую сессию внутри этого проекта.",
                keyboard=self._project_keyboard(),
            )
            await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, "Проект выбран")
            return
        if action == "page":
            page = int(payload.get("p") or 0)
            await self._send_or_edit_project_list(peer_id, user_id, page, prefer_edit=True)
            return
        if action == "commands":
            await self._send_project_text(peer_id, self._project_commands_text(), keyboard=self._project_commands_keyboard())
            await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, "Команды")
            return
        if action == "cmd":
            command = str(payload.get("cmd") or "").strip()
            if command == "/invite":
                await self._handle_invite_command(peer_id, command)
            elif command:
                await self._handle_project_command(peer_id, user_id, command)
            return
        if action == "new":
            await self._set_pending_create(peer_id, user_id)
            await self._send_project_text(peer_id, _project_create_prompt_text(), keyboard=self._project_cancel_keyboard())
            return
        if action == "cancel":
            await self._clear_pending_create(peer_id, user_id)
            await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, "Отменено")
            return
        await self._answer_message_event(str(obj.get("event_id") or ""), user_id, peer_id, "Неизвестное действие")

    async def _handle_project_command(self, peer_id: str, user_id: str, text: str) -> bool:
        lowered = text.strip()
        if lowered in {"Проекты", "Меню", "Новый проект", "Команды"}:
            if lowered == "Проекты":
                await self._send_or_edit_project_list(peer_id, user_id)
            elif lowered == "Новый проект":
                await self._set_pending_create(peer_id, user_id)
                await self._send_project_text(peer_id, _project_create_prompt_text(), keyboard=self._project_cancel_keyboard())
            elif lowered == "Команды":
                await self._send_project_text(peer_id, self._project_commands_text(), keyboard=self._project_commands_keyboard())
            else:
                lane_id = self._get_active_lane_id(peer_id, user_id)
                lane = self._resolve_lane(peer_id, lane_id or "") if lane_id else None
                current = lane.get("name") if lane else "не выбран"
                await self._send_project_text(peer_id, f"Меню проектов VK\nТекущий проект: {current}\n/project list — список\n/project new — новый проект\n/project edit — изменить текущий проект\n/project edit <id> <что изменить> — быстрое изменение\n/project off — выйти из проекта", keyboard=self._project_keyboard())
            return True
        if not lowered.startswith("/project"):
            if lowered.lower() in {"/commands", "/command", "команды"}:
                await self._send_project_text(peer_id, self._project_commands_text(), keyboard=self._project_commands_keyboard())
                return True
            if lowered in {"Следующая", "Предыдущая"}:
                pages = self._lane_state.setdefault("project_list_pages", {})
                state_key = self._state_key(peer_id, user_id)
                current_page = 0
                if isinstance(pages, dict):
                    try:
                        current_page = int(pages.get(state_key) or 0)
                    except (TypeError, ValueError):
                        current_page = 0
                delta = 1 if lowered == "Следующая" else -1
                await self._send_or_edit_project_list(peer_id, user_id, current_page + delta, prefer_edit=True)
                return True
            return False
        args = lowered[len("/project"):].strip()
        if not args:
            lane_id = self._get_active_lane_id(peer_id, user_id)
            lane = self._resolve_lane(peer_id, lane_id or "") if lane_id else None
            current = lane.get("name") if lane else "не выбран"
            await self._send_project_text(peer_id, f"Текущий проект: {current}\n/project list — список\n/project new — новый проект\n/project edit — изменить текущий проект\n/project edit <id> <что изменить> — быстрое изменение\n/project off — выйти из проекта", keyboard=self._project_keyboard())
            return True
        if args == "list" or re.fullmatch(r"list\s+\d+", args):
            page = 0
            match = re.fullmatch(r"list\s+(\d+)", args)
            if match:
                page = max(0, int(match.group(1)) - 1)
            await self._send_or_edit_project_list(peer_id, user_id, page, prefer_edit=bool(match))
            return True
        if args.startswith("search "):
            query = args[len("search "):].strip().lower()
            cfg = self._peer_lanes(peer_id) or {}
            matches = []
            for lane in cfg.get("lanes") or []:
                haystack = " ".join(
                    str(part or "") for part in (lane.get("id"), lane.get("name"), lane.get("description"), " ".join(lane.get("aliases") or []))
                ).lower()
                if query and query in haystack:
                    matches.append(f"- {lane.get('name')} (`{lane.get('id')}`)")
            await self._send_project_text(peer_id, "Найденные проекты:\n" + ("\n".join(matches) if matches else "ничего не найдено"), keyboard=self._project_keyboard())
            return True
        if args == "off":
            await self._set_active_lane_id(peer_id, user_id, None)
            await self._send_project_text(peer_id, "Проектный режим отключён для тебя в этом чате.", keyboard=self._project_keyboard())
            return True
        if args in {"pin", "unpin"}:
            lane_id = self._get_active_lane_id(peer_id, user_id)
            lane = self._resolve_lane(peer_id, lane_id or "") if lane_id else None
            if not lane:
                await self._send_project_text(peer_id, "Сначала выбери проект, потом /project pin или /project unpin.", keyboard=self._project_list_keyboard(peer_id))
                return True
            pinned = args == "pin"
            await self._set_pinned_lane(peer_id, lane["id"], pinned)
            await self._send_project_text(peer_id, ("Проект закреплён первым в списке: " if pinned else "Проект откреплён: ") + lane["name"], keyboard=self._project_keyboard())
            return True
        if args.startswith("pin ") or args.startswith("unpin "):
            command, _, target = args.partition(" ")
            lane = self._resolve_lane(peer_id, target.strip())
            if not lane:
                await self._send_project_text(peer_id, "Проект не найден. Открой список: /project list", keyboard=self._project_list_keyboard(peer_id))
                return True
            pinned = command == "pin"
            await self._set_pinned_lane(peer_id, lane["id"], pinned)
            await self._send_project_text(peer_id, ("Проект закреплён первым в списке: " if pinned else "Проект откреплён: ") + lane["name"], keyboard=self._project_keyboard())
            return True
        if args == "new":
            await self._set_pending_create(peer_id, user_id)
            await self._send_project_text(peer_id, _project_create_prompt_text(), keyboard=self._project_cancel_keyboard())
            return True
        if args.startswith("new "):
            lane = _parse_project_new_fallback(args[4:])
            if not lane:
                await self._send_project_text(peer_id, "Формат: /project new <name> <skills_csv> <context...>")
                return True
            if not await self._add_custom_lane(peer_id, lane):
                await self._send_project_text(peer_id, "Не удалось создать проект: id уже занят или данные не прошли проверку.")
                return True
            await self._set_active_lane_id(peer_id, user_id, lane["id"])
            await self._send_project_text(
                peer_id,
                "Проект создан и выбран: " + lane["name"] + "\n\n```yaml\n" + _format_lane_yaml_snippet(lane) + "\n```",
                keyboard=self._project_keyboard(),
            )
            return True
        if args == "edit":
            lane_id = self._get_active_lane_id(peer_id, user_id)
            lane = self._resolve_lane(peer_id, lane_id or "") if lane_id else None
            if not lane:
                await self._send_project_text(peer_id, "Сначала выбери проект или укажи его явно: /project edit <id> <что изменить>", keyboard=self._project_list_keyboard(peer_id))
                return True
            await self._set_pending_edit(peer_id, user_id, lane["id"])
            await self._send_project_text(peer_id, _project_edit_prompt_text(lane), keyboard=self._project_cancel_keyboard())
            return True
        if args.startswith("edit "):
            target_and_rest = args[5:].strip()
            target, sep, edit_text = target_and_rest.partition(" ")
            lane = self._resolve_lane(peer_id, target)
            if not lane:
                await self._send_project_text(peer_id, "Проект для редактирования не найден. Формат: /project edit <id> <что изменить>", keyboard=self._project_list_keyboard(peer_id))
                return True
            if not sep or not edit_text.strip():
                await self._set_pending_edit(peer_id, user_id, lane["id"])
                await self._send_project_text(peer_id, _project_edit_prompt_text(lane), keyboard=self._project_cancel_keyboard())
                return True
            updates = _parse_project_edit_text(edit_text)
            if not updates:
                await self._set_pending_edit(peer_id, user_id, lane["id"])
                await self._send_project_text(peer_id, "Не смогла надёжно разобрать изменения. Напиши их следующим сообщением свободно или по полям.", keyboard=self._project_cancel_keyboard())
                return True
            edited = await self._update_custom_lane(peer_id, lane["id"], updates)
            if not edited:
                await self._send_project_text(peer_id, "Не удалось обновить проект: данные не прошли проверку.", keyboard=self._project_keyboard())
                return True
            await self._set_active_lane_id(peer_id, user_id, edited["id"])
            await self._send_project_text(peer_id, "Проект обновлён: " + edited["name"] + "\n\n```yaml\n" + _format_lane_yaml_snippet(edited) + "\n```", keyboard=self._project_keyboard())
            return True
        lane = self._resolve_lane(peer_id, args)
        if not lane:
            await self._send_project_text(peer_id, "Проект не найден. Открой список: /project list", keyboard=self._project_list_keyboard(peer_id))
            return True
        await self._set_active_lane_id(peer_id, user_id, lane["id"])
        await self._send_project_text(peer_id, f"Проект выбран: {lane['name']}\n\nГде остановились:\n— пока нет истории\n\nПиши задачу. /new начнёт новую сессию внутри этого проекта.", keyboard=self._project_selected_keyboard(peer_id, lane["id"]))
        return True

    async def _handle_invite_command(self, peer_id: str, text: str) -> bool:
        if text.strip().lower() != "/invite":
            return False
        if not str(peer_id).startswith("200000"):
            await self._send_project_text(peer_id, "Команда /invite работает только в VK-чате, не в личном диалоге.")
            return True
        try:
            payload = await self._vk_method("messages.getInviteLink", {"peer_id": str(peer_id), "reset": 0})
            response = payload.get("response")
            link = ""
            if isinstance(response, dict):
                link = str(response.get("link") or response.get("invite_link") or "").strip()
            elif isinstance(response, str):
                link = response.strip()
            if not link:
                raise RuntimeError("VK invite response does not contain link")
            await self._send_project_text(peer_id, f"Актуальный инвайт в этот VK-чат:\n{link}")
        except Exception as exc:
            logger.warning("VK: failed to fetch invite link for peer=%s — %s", peer_id, _redact_token(str(exc)))
            await self._send_project_text(peer_id, "Не удалось получить инвайт в текущий VK-чат. Проверь права бота в этом чате.")
        return True

    def _one_shot_lane_for_text(self, peer_id: str, text: str) -> tuple[dict[str, Any] | None, str]:
        if not text.startswith("@"):
            return None, text
        first, _, rest = text.partition(" ")
        alias = first[1:].strip().lower()
        lane = self._resolve_lane(peer_id, alias)
        if not lane:
            return None, text
        return lane, rest.strip()

    async def _refresh_longpoll_server(self) -> None:
        payload = await self._vk_method("groups.getLongPollServer", {"group_id": self.group_id})
        response = payload.get("response") or {}
        self._lp_server = str(response.get("server") or "")
        self._lp_key = str(response.get("key") or "")
        self._lp_ts = str(response.get("ts") or "")
        if not self._lp_server or not self._lp_key or not self._lp_ts:
            raise RuntimeError("VK long poll server response is missing server/key/ts")
        try:
            lp_host = urllib.parse.urlparse(self._lp_server).netloc or "unknown"
        except Exception:
            lp_host = "unknown"
        logger.info(
            "VK: long poll server refreshed group=%s host=%s ts=%s api_version=%s",
            self.group_id,
            lp_host,
            self._lp_ts,
            self.api_version,
        )

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
                data = await asyncio.wait_for(
                    _http_json_async(self._lp_server, params, timeout=40),
                    timeout=50,
                )

                if "failed" in data:
                    failed = int(data.get("failed") or 0)
                    if failed in {1, 2} and data.get("ts"):
                        self._lp_ts = str(data["ts"])
                    else:
                        await self._refresh_longpoll_server()
                    continue

                previous_ts = self._lp_ts
                self._lp_ts = str(data.get("ts") or self._lp_ts)
                updates = data.get("updates") or []
                if updates:
                    self._lp_no_update_cycles = 0
                    logger.info(
                        "VK: long poll returned %d update(s) ts=%s->%s types=%s",
                        len(updates),
                        previous_ts,
                        self._lp_ts,
                        ",".join(str(update.get("type") or "") for update in updates[:10]),
                    )
                else:
                    self._lp_no_update_cycles += 1
                    if self._lp_no_update_cycles in {1, 3, 12} or self._lp_no_update_cycles % 24 == 0:
                        logger.info(
                            "VK: long poll no updates streak=%d ts=%s->%s",
                            self._lp_no_update_cycles,
                            previous_ts,
                            self._lp_ts,
                        )
                for update in updates:
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

    def _fallback_peer_ids(self) -> list[str]:
        peers = set(self.allowed_peers or set())
        if self.home_channel:
            peers.add(str(self.home_channel))
        if isinstance(self.project_lanes, dict):
            peers.update(str(peer) for peer in self.project_lanes.keys())
        return sorted(peer for peer in peers if peer)

    async def _fetch_cmid_items(self, peer_id: str, cmids: list[int]) -> list[dict[str, Any]]:
        if not cmids:
            return []
        payload = await self._vk_method(
            "messages.getByConversationMessageId",
            {
                "peer_id": str(peer_id),
                "conversation_message_ids": ",".join(str(cmid) for cmid in cmids),
            },
        )
        response = payload.get("response") or {}
        items = response.get("items") if isinstance(response, dict) else []
        return [item for item in (items or []) if isinstance(item, dict)]

    async def _cmid_exists(self, peer_id: str, cmid: int) -> bool:
        try:
            return bool(await self._fetch_cmid_items(peer_id, [cmid]))
        except Exception as exc:
            logger.debug("VK: fallback cmid probe failed peer=%s cmid=%s — %s", peer_id, cmid, _redact_token(str(exc)))
            return False

    async def _discover_latest_cmid(self, peer_id: str) -> int:
        high = 1
        while high < 131072 and await self._cmid_exists(peer_id, high):
            high *= 2
        low = high // 2
        while low + 1 < high:
            mid = (low + high) // 2
            if await self._cmid_exists(peer_id, mid):
                low = mid
            else:
                high = mid
        return low

    async def _bootstrap_fallback_poll(self) -> None:
        for peer_id in self._fallback_peer_ids():
            if peer_id in self._fallback_last_cmid:
                continue
            try:
                self._fallback_last_cmid[peer_id] = await self._discover_latest_cmid(peer_id)
            except Exception as exc:
                logger.debug("VK: fallback bootstrap failed peer=%s — %s", peer_id, _redact_token(str(exc)))
        if self._fallback_last_cmid and not self._fallback_bootstrapped_logged:
            sample = ", ".join(
                f"{peer}:{cmid}" for peer, cmid in sorted(self._fallback_last_cmid.items())[:20]
            )
            logger.info(
                "VK: fallback cmid poll bootstrapped for %d peer(s): %s",
                len(self._fallback_last_cmid),
                sample,
            )
            self._fallback_bootstrapped_logged = True

    async def _fallback_poll_once(self) -> int:
        await self._bootstrap_fallback_poll()
        handled = 0
        batch_size = max(1, min(int(self.fallback_poll_batch_size), 100))
        for peer_id in self._fallback_peer_ids():
            last = int(self._fallback_last_cmid.get(peer_id, 0))
            ids = list(range(last + 1, last + batch_size + 1))
            try:
                items = await self._fetch_cmid_items(peer_id, ids)
            except Exception as exc:
                logger.debug("VK: fallback cmid poll failed peer=%s — %s", peer_id, _redact_token(str(exc)))
                continue
            if not items:
                continue
            max_seen = max(int(item.get("conversation_message_id") or 0) for item in items)
            self._fallback_last_cmid[peer_id] = max(last, max_seen)
            user_items = [
                item for item in items if str(item.get("from_id") or "") and not str(item.get("from_id") or "").startswith("-")
            ]
            if user_items:
                logger.info(
                    "VK: fallback cmid poll found %d user message(s) peer=%s cmid=%s..%s previous=%s",
                    len(user_items),
                    peer_id,
                    min(int(item.get("conversation_message_id") or 0) for item in user_items),
                    max(int(item.get("conversation_message_id") or 0) for item in user_items),
                    last,
                )
            for item in sorted(items, key=lambda msg: int(msg.get("conversation_message_id") or 0)):
                from_id = str(item.get("from_id") or "")
                if not from_id or from_id.startswith("-"):
                    continue
                await self._handle_update({"type": "message_new", "object": {"message": item}})
                handled += 1
        return handled

    async def _fallback_poll_loop(self) -> None:
        while True:
            try:
                handled = await self._fallback_poll_once()
                if handled:
                    logger.info("VK: fallback cmid poll handled %d message(s)", handled)
                await asyncio.sleep(max(5, int(self.fallback_poll_interval_seconds)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("VK: fallback cmid poll error — %s", _redact_token(str(exc)))
                await asyncio.sleep(30)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        update_type = str(update.get("type") or "")
        if update_type == "message_event":
            try:
                await self._handle_message_event_update(update)
            except Exception as exc:
                logger.warning("VK: project callback handling failed safely — %s", _redact_token(str(exc)))
            return
        if update_type not in {"message_new", "message_edit"}:
            return
        obj = update.get("object") or {}
        msg = obj.get("message") or obj
        if not isinstance(msg, dict):
            return

        # VK uses from_id < 0 for communities. Ignore bot/community echoes,
        # including our own progress-message edits delivered as message_edit.
        from_id = str(msg.get("from_id") or "")
        if from_id.startswith("-"):
            return

        peer_id = str(msg.get("peer_id") or "")
        if not self._is_authorized(from_id=from_id, peer_id=peer_id):
            logger.info("VK: ignoring unauthorized sender=%s peer=%s", from_id, peer_id)
            return

        conversation_message_id = msg.get("conversation_message_id") or msg.get("id")
        if update_type == "message_new":
            if self._is_duplicate_event(peer_id=peer_id, conversation_message_id=conversation_message_id):
                logger.info("VK: ignoring duplicate event peer=%s cmid=%s", peer_id, conversation_message_id)
                return
        else:
            if self._is_duplicate_edit_event(peer_id=peer_id, conversation_message_id=conversation_message_id, msg=msg):
                logger.info("VK: ignoring duplicate edit event peer=%s cmid=%s", peer_id, conversation_message_id)
                return
        msg = await self._enrich_message_from_api(msg, peer_id=peer_id, conversation_message_id=conversation_message_id)

        text = str(msg.get("text") or "").strip()
        attachments = msg.get("attachments") or []
        media_type, media_urls, media_types = self._extract_attachment_media(attachments)
        nested_media_type, nested_media_urls, nested_media_types = self._extract_nested_message_media(msg)
        if nested_media_urls:
            if media_type == MessageType.TEXT and nested_media_type != MessageType.TEXT:
                media_type = nested_media_type
            media_urls.extend(nested_media_urls)
            media_types.extend(nested_media_types)
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
        control_text = self._strip_bot_mention_prefix(text)
        if control_text != text:
            text = control_text

        try:
            raw_button_payload = msg.get("payload") or {}
            if isinstance(raw_button_payload, str):
                try:
                    button_payload = json.loads(raw_button_payload)
                except json.JSONDecodeError:
                    button_payload = {}
            else:
                button_payload = raw_button_payload if isinstance(raw_button_payload, dict) else {}
            if await self._handle_clarify_payload(
                peer_id=peer_id,
                user_id=from_id,
                payload=button_payload,
                message_id=conversation_message_id,
            ):
                return
            lane_to_select = None
            if button_payload.get("vkpl") == "list":
                await self._send_or_edit_project_list(peer_id, from_id)
                return
            if button_payload.get("vkpl") == "menu":
                await self._handle_project_command(peer_id, from_id, "Меню")
                return
            if button_payload.get("vkpl") == "new":
                await self._set_pending_create(peer_id, from_id)
                await self._send_project_text(peer_id, _project_create_prompt_text(), keyboard=self._project_cancel_keyboard())
                return
            if button_payload.get("vkpl") == "page":
                page = int(button_payload.get("p") or 0)
                await self._send_or_edit_project_list(peer_id, from_id, page, prefer_edit=True)
                return
            if button_payload.get("vkpl") == "commands":
                await self._send_project_text(peer_id, self._project_commands_text(), keyboard=self._project_commands_keyboard())
                return
            if button_payload.get("vkpl") == "cmd":
                command = str(button_payload.get("cmd") or "").strip()
                if command == "/invite":
                    await self._handle_invite_command(peer_id, command)
                elif command:
                    await self._handle_project_command(peer_id, from_id, command)
                return
            if button_payload.get("vkpl") == "select":
                lane_to_select = self._resolve_lane(peer_id, str(button_payload.get("id") or ""))
            elif button_payload.get("vkpl") in {"pin", "unpin"}:
                lane = self._resolve_lane(peer_id, str(button_payload.get("id") or ""))
                if lane:
                    pinned = button_payload.get("vkpl") == "pin"
                    await self._set_pinned_lane(peer_id, lane["id"], pinned)
                    await self._send_project_text(
                        peer_id,
                        ("Проект закреплён первым в списке: " if pinned else "Проект откреплён: ") + lane["name"],
                        keyboard=self._project_keyboard(),
                    )
                    return
            elif control_text and not control_text.startswith("/") and not media_urls:
                lane_to_select = self._resolve_lane(peer_id, control_text)
            if lane_to_select:
                await self._set_active_lane_id(peer_id, from_id, lane_to_select["id"])
                await self._send_project_text(
                    peer_id,
                    f"Проект выбран: {lane_to_select['name']}\n\nГде остановились:\n— пока нет истории\n\nПиши задачу. /new начнёт новую сессию внутри этого проекта.",
                    keyboard=self._project_selected_keyboard(peer_id, lane_to_select["id"]),
                )
                return
        except Exception as exc:
            logger.warning("VK: project button selection failed safely; falling back to normal message — %s", _redact_token(str(exc)))

        chat_type = "group" if peer_id.startswith("200000") else "dm"
        chat_name, chat_topic = await self._resolve_conversation_context(peer_id, chat_type=chat_type)
        user_name = f"VK user {from_id}"

        try:
            pending_create = self._pending_create(peer_id, from_id)
            if pending_create:
                parsed_lane = _parse_project_create_text(text)
                if parsed_lane:
                    await self._clear_pending_create(peer_id, from_id)
                    if await self._add_custom_lane(peer_id, parsed_lane):
                        await self._set_active_lane_id(peer_id, from_id, parsed_lane["id"])
                        await self._send_project_text(peer_id, "Проект создан и выбран: " + parsed_lane["name"], keyboard=self._project_keyboard())
                        return
                    await self._send_project_text(peer_id, "Не удалось создать проект: id уже занят или данные не прошли проверку.", keyboard=self._project_cancel_keyboard())
                    return
        except Exception as exc:
            logger.warning("VK: pending project local creation failed safely — %s", _redact_token(str(exc)))

        try:
            pending_edit = self._pending_edit(peer_id, from_id)
            if pending_edit:
                lane_id = str(pending_edit.get("lane_id") or "")
                updates = _parse_project_edit_text(text)
                if updates:
                    await self._clear_pending_edit(peer_id, from_id)
                    edited = await self._update_custom_lane(peer_id, lane_id, updates)
                    if edited:
                        await self._set_active_lane_id(peer_id, from_id, edited["id"])
                        await self._send_project_text(peer_id, "Проект обновлён: " + edited["name"], keyboard=self._project_keyboard())
                        return
                    await self._send_project_text(peer_id, "Не удалось обновить проект: данные не прошли проверку.", keyboard=self._project_cancel_keyboard())
                    return
        except Exception as exc:
            logger.warning("VK: pending project local edit failed safely — %s", _redact_token(str(exc)))

        try:
            wants_cancel = text.strip().lower() in {"отмена", "cancel"}
            if wants_cancel and self._pending_create(peer_id, from_id):
                await self._clear_pending_create(peer_id, from_id)
                await self._send_project_text(peer_id, "Создание проекта отменено.", keyboard=self._project_keyboard())
                return
            if wants_cancel and self._pending_edit(peer_id, from_id):
                await self._clear_pending_edit(peer_id, from_id)
                await self._send_project_text(peer_id, "Редактирование проекта отменено.", keyboard=self._project_keyboard())
                return
        except Exception as exc:
            logger.warning("VK: pending project cancel failed safely — %s", _redact_token(str(exc)))

        try:
            if await self._handle_invite_command(peer_id, control_text):
                return
        except Exception as exc:
            logger.warning("VK: invite command failed safely; falling back to normal message — %s", _redact_token(str(exc)))

        try:
            if await self._handle_project_command(peer_id, from_id, control_text):
                return
        except Exception as exc:
            logger.warning("VK: project command failed safely; falling back to normal message — %s", _redact_token(str(exc)))

        pending_create = None
        pending_edit = None
        try:
            pending_create = self._pending_create(peer_id, from_id)
        except Exception as exc:
            logger.warning("VK: pending project state failed safely — %s", _redact_token(str(exc)))
        try:
            pending_edit = self._pending_edit(peer_id, from_id)
        except Exception as exc:
            logger.warning("VK: pending project edit state failed safely — %s", _redact_token(str(exc)))

        selected_lane = None
        one_shot_lane = None
        try:
            reply_lane = self._reply_lane(peer_id, msg)
            one_shot_lane, stripped_text = self._one_shot_lane_for_text(peer_id, text)
            if reply_lane:
                selected_lane = reply_lane
            elif pending_edit:
                selected_lane = self._resolve_lane(peer_id, str(pending_edit.get("lane_id") or ""))
            elif one_shot_lane and stripped_text:
                selected_lane = one_shot_lane
                text = stripped_text
            elif one_shot_lane and media_urls:
                selected_lane = one_shot_lane
            else:
                active_lane_id = self._get_active_lane_id(peer_id, from_id)
                selected_lane = self._resolve_lane(peer_id, active_lane_id or "") if active_lane_id else None
        except Exception as exc:
            logger.warning("VK: project lane routing failed safely; using root chat — %s", _redact_token(str(exc)))
            selected_lane = None

        event_chat_type = chat_type
        event_chat_topic = chat_topic
        event_auto_skill = resolve_channel_skills(self.config.extra, peer_id)
        event_channel_prompt = resolve_channel_prompt(self.config.extra, peer_id)
        if selected_lane:
            event_chat_type = "thread"
            event_chat_topic = selected_lane.get("name") or event_chat_topic
            lane_skills = selected_lane.get("skills") or []
            default_skills = (self._peer_lanes(peer_id) or {}).get("default_skills") or []
            merged_skills: list[str] = []
            for skill in [*(event_auto_skill or []), *default_skills, *lane_skills]:
                if skill and skill not in merged_skills:
                    merged_skills.append(skill)
            event_auto_skill = merged_skills or event_auto_skill
            lane_prompt_parts = []
            if event_channel_prompt:
                lane_prompt_parts.append(event_channel_prompt)
            if selected_lane.get("description"):
                lane_prompt_parts.append(f"[VK project lane]\nProject: {selected_lane.get('name')}\nContext: {selected_lane.get('description')}")
            event_channel_prompt = "\n\n".join(lane_prompt_parts) or event_channel_prompt

        if pending_create:
            try:
                await self._clear_pending_create(peer_id, from_id)
            except Exception as exc:
                logger.warning("VK: failed to clear pending project state — %s", _redact_token(str(exc)))
            event_channel_prompt = "\n\n".join(
                part for part in (event_channel_prompt, _build_project_lane_prompt(peer_id)) if part
            )

        if pending_edit and selected_lane:
            try:
                await self._clear_pending_edit(peer_id, from_id)
            except Exception as exc:
                logger.warning("VK: failed to clear pending project edit state — %s", _redact_token(str(exc)))
            event_channel_prompt = "\n\n".join(
                part for part in (event_channel_prompt, _build_project_lane_edit_prompt(peer_id, selected_lane)) if part
            )

        source = self.build_source(
            chat_id=peer_id,
            chat_name=chat_name,
            chat_type=event_chat_type,
            user_id=from_id,
            user_name=user_name,
            thread_id=f"{LANE_THREAD_PREFIX}{selected_lane['id']}" if selected_lane else None,
            chat_topic=event_chat_topic,
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
            auto_skill=event_auto_skill,
            channel_prompt=event_channel_prompt,
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

    def _is_duplicate_edit_event(self, *, peer_id: str, conversation_message_id: Any, msg: dict[str, Any]) -> bool:
        if not peer_id or conversation_message_id in (None, "") or self.dedupe_ttl_seconds <= 0:
            return False
        now = time.monotonic()
        expired = [key for key, expires_at in self._seen_edit_keys.items() if expires_at <= now]
        for key in expired:
            self._seen_edit_keys.pop(key, None)
        text = str(msg.get("text") or "")
        attachments = msg.get("attachments") or []
        forwards = msg.get("fwd_messages") or []
        reply = msg.get("reply_message") or None
        digest = hashlib.sha256(
            json.dumps(
                {"text": text, "attachments": attachments, "reply_message": reply, "fwd_messages": forwards},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
        key = f"{peer_id}:{conversation_message_id}:{digest}"
        if key in self._seen_edit_keys:
            return True
        self._seen_edit_keys[key] = now + self.dedupe_ttl_seconds
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

    def _extract_nested_message_media(self, msg: dict[str, Any]) -> tuple[MessageType, list[str], list[str]]:
        """Return media URLs attached to VK replies/forwarded messages.

        Top-level message attachments are handled separately.  VK stores media
        inside ``reply_message`` and ``fwd_messages`` when a user replies to or
        forwards a message; those nested attachments must still become
        ``MessageEvent.media_urls`` so downstream image/audio/video processors can
        inspect them.  The textual forwarded summary remains quoted context.
        """
        message_type = MessageType.TEXT
        media_urls: list[str] = []
        media_types: list[str] = []

        def visit(item: dict[str, Any], *, depth: int) -> None:
            nonlocal message_type
            if depth > 2 or len(media_urls) >= 10:
                return
            attachments = item.get("attachments") or []
            if isinstance(attachments, list) and attachments:
                current_type, current_urls, current_media_types = self._extract_attachment_media(attachments)
                if message_type == MessageType.TEXT and current_type != MessageType.TEXT:
                    message_type = current_type
                remaining = max(0, 10 - len(media_urls))
                media_urls.extend(current_urls[:remaining])
                media_types.extend(current_media_types[:remaining])
            reply = item.get("reply_message")
            if isinstance(reply, dict):
                visit(reply, depth=depth + 1)
            forwards = item.get("fwd_messages") or []
            if isinstance(forwards, list):
                for child in forwards[:5]:
                    if isinstance(child, dict):
                        visit(child, depth=depth + 1)

        reply = msg.get("reply_message")
        if isinstance(reply, dict):
            visit(reply, depth=0)
        forwards = msg.get("fwd_messages") or []
        if isinstance(forwards, list):
            for item in forwards[:5]:
                if isinstance(item, dict):
                    visit(item, depth=0)
        return message_type, media_urls, media_types

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
            default_keyboard = self._project_keyboard() if self._should_attach_project_keyboard(chat_id) else None

            async def op():
                params = {
                    "peer_ids": str(chat_id),
                    "message": caption or "",
                    "attachment": attachment,
                    "random_id": random.randint(1, 2_147_483_647),
                }
                if default_keyboard:
                    params["keyboard"] = default_keyboard
                return await self._vk_method("messages.send", params)

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

    def _format_exec_approval(self, command: str, description: str, smart_denied: bool = False) -> str:
        cmd_preview = command if len(command) <= 1500 else command[:1500] + "..."
        text = (
            "⚠️ Command Approval Required\n\n"
            f"Command:\n```\n{cmd_preview}\n```\n"
            f"Reason: {description}"
        )
        if smart_denied:
            text += "\n\nSmart DENY: owner override applies to this one operation only."
        return text

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
        allow_permanent: bool = True,
        allow_session: bool = True,
        smart_denied: bool = False,
    ) -> SendResult:
        """Send a VK inline-keyboard dangerous-command approval prompt.

        This implements the gateway's native button approval contract while
        keeping the gateway text fallback available if sending fails.
        """
        if not self.token:
            return SendResult(success=False, error="VK_GROUP_TOKEN is not configured")
        try:
            self._approval_counter += 1
            approval_id = self._approval_counter
            params = {
                "peer_ids": str(chat_id),
                "message": self.format_message(self._format_exec_approval(command, description, smart_denied)),
                "random_id": random.randint(1, 2_147_483_647),
                "keyboard": self._exec_approval_keyboard(
                    approval_id,
                    allow_permanent=allow_permanent,
                    allow_session=allow_session,
                    smart_denied=smart_denied,
                ),
            }
            payload = await self._vk_method("messages.send", params)
            message_id = self._sent_message_id(payload)
            self._approval_state[approval_id] = session_key
            thread_id = (metadata or {}).get("thread_id") if isinstance(metadata, dict) else None
            if thread_id and message_id:
                await self._remember_message_lane(str(chat_id), message_id, thread_id)
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            return SendResult(success=False, error=_redact_token(str(exc)), retryable=True)

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a VK inline-keyboard slash-command confirmation prompt."""
        if not self.token:
            return SendResult(success=False, error="VK_GROUP_TOKEN is not configured")
        try:
            prompt = f"{title}\n\n{message}" if title else message
            params = {
                "peer_ids": str(chat_id),
                "message": self.format_message(prompt),
                "random_id": random.randint(1, 2_147_483_647),
                "keyboard": self._slash_confirm_keyboard(confirm_id),
            }
            payload = await self._vk_method("messages.send", params)
            message_id = self._sent_message_id(payload)
            self._slash_confirm_state[str(confirm_id)] = session_key
            thread_id = (metadata or {}).get("thread_id") if isinstance(metadata, dict) else None
            if thread_id and message_id:
                await self._remember_message_lane(str(chat_id), message_id, thread_id)
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            return SendResult(success=False, error=_redact_token(str(exc)), retryable=True)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a VK inline-keyboard clarify prompt for small choice lists.

        Mirrors Telegram's clarify UX: the full choice text stays in the
        message body, while buttons are short numbers plus an explicit
        free-text option.  Oversized choice lists fall back to the gateway's
        numbered text mode instead of paginating a safety prompt.
        """
        if not choices:
            return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata=metadata)
        if len(choices) > 9:
            return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata=metadata)
        if not self.token:
            return SendResult(success=False, error="VK_GROUP_TOKEN is not configured")

        try:
            option_lines = "\n".join(f"{idx + 1}. {choice}" for idx, choice in enumerate(choices))
            message = f"❓ {question}\n\n{option_lines}"
            params = {
                "peer_ids": str(chat_id),
                "message": self.format_message(message),
                "random_id": random.randint(1, 2_147_483_647),
                "keyboard": self._clarify_keyboard(str(clarify_id), choices),
            }
            payload = await self._vk_method("messages.send", params)
            message_id = self._sent_message_id(payload)
            self._clarify_state[str(clarify_id)] = session_key
            thread_id = (metadata or {}).get("thread_id") if isinstance(metadata, dict) else None
            if thread_id and message_id:
                await self._remember_message_lane(str(chat_id), message_id, thread_id)
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            return SendResult(success=False, error=_redact_token(str(exc)), retryable=True)

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
        default_keyboard = self._project_keyboard() if self._should_attach_project_keyboard(chat_id) else None
        last_message_id: Optional[str] = None
        continuation_ids: list[str] = []
        try:
            for chunk in chunks:
                async def op(chunk=chunk):
                    params = {
                        "peer_ids": str(chat_id),
                        "message": chunk,
                        "random_id": random.randint(1, 2_147_483_647),
                    }
                    if default_keyboard:
                        params["keyboard"] = default_keyboard
                    return await self._vk_method("messages.send", params)

                payload = await _retry_vk_transient_once("messages.send", op)
                message_id = self._sent_message_id(payload)
                if last_message_id:
                    continuation_ids.append(last_message_id)
                last_message_id = message_id
            thread_id = (metadata or {}).get("thread_id") if isinstance(metadata, dict) else None
            if thread_id and last_message_id:
                await self._remember_message_lane(str(chat_id), last_message_id, thread_id)
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
            if not _is_vk_auth_or_permission_error(exc):
                return SendResult(success=False, error=_redact_token(str(exc)), retryable=True)
            logger.info(
                "VK: video.save is not available with current auth; sending video as document attachment instead"
            )
            try:
                attachment = await self._upload_doc_attachment(chat_id, video_path)
            except Exception as doc_exc:
                return SendResult(
                    success=False,
                    error=_redact_token(f"video upload failed: {exc}; document fallback failed: {doc_exc}"),
                    retryable=True,
                )
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
    root = yaml_cfg or {}
    platforms_cfg = root.get("platforms") if isinstance(root.get("platforms"), dict) else {}
    platforms_vk_cfg = platforms_cfg.get("vk") if isinstance(platforms_cfg, dict) and isinstance(platforms_cfg.get("vk"), dict) else {}
    platforms_vk_extra_obj = platforms_vk_cfg.get("extra") if isinstance(platforms_vk_cfg, dict) else {}
    platforms_vk_extra = platforms_vk_extra_obj if isinstance(platforms_vk_extra_obj, dict) else {}
    legacy_vk_cfg = ((root.get("gateway") or {}).get("vk") if isinstance(root.get("gateway"), dict) else None) or root.get("vk") or {}
    vk_cfg = legacy_vk_cfg if isinstance(legacy_vk_cfg, dict) else {}
    if not isinstance(vk_cfg, dict) and not isinstance(platforms_vk_cfg, dict):
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
    if isinstance(platforms_vk_extra, dict):
        for key in ("reactions_enabled", "reaction_progress", "reaction_ok", "reaction_fail"):
            if key in platforms_vk_extra:
                extra[key] = platforms_vk_extra[key]

    canonical_project_lanes = platforms_vk_extra.get("project_lanes") if isinstance(platforms_vk_extra, dict) else None
    if canonical_project_lanes is not None:
        extra["project_lanes"] = _normalize_project_lanes(_extract_project_lanes_config(canonical_project_lanes))
    elif "project_lanes" in vk_cfg:
        extra["project_lanes"] = _normalize_project_lanes(_extract_project_lanes_config(vk_cfg.get("project_lanes")))
    return extra or None


def _is_connected(config: Any) -> bool:
    extra = getattr(config, "extra", {}) or {}
    token = os.getenv("VK_GROUP_TOKEN") or getattr(config, "token", None) or extra.get("group_token")
    group_id = os.getenv("VK_GROUP_ID") or extra.get("group_id")
    return bool(token and group_id)


async def _standalone_send(config_or_chat_id: Any, chat_id: Any = None, text: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Standalone sender used by `hermes send --to vk:...` and cron delivery.

    Hermes' standalone platform contract calls handlers as
    `(platform_config, chat_id, text, ...)`.  Keep compatibility with the older
    two-argument `(chat_id, text, ...)` shape so direct callers do not break.
    """
    platform_config = None
    if text is None:
        # Legacy shape: _standalone_send(chat_id, text, ...)
        text = "" if chat_id is None else str(chat_id)
        chat_id = config_or_chat_id
    else:
        platform_config = config_or_chat_id

    extra = getattr(platform_config, "extra", {}) or {}
    token = (
        os.getenv("VK_GROUP_TOKEN")
        or getattr(platform_config, "token", None)
        or extra.get("group_token")
    )
    if not token:
        return {"success": False, "error": "VK_GROUP_TOKEN is not configured"}
    params = {
        "access_token": token,
        "v": os.getenv("VK_API_VERSION") or VK_API_VERSION,
        "peer_ids": str(chat_id),
        "message": text or "",
        "random_id": random.randint(1, 2_147_483_647),
    }
    try:
        payload = await _http_json_async(f"{VK_API_BASE}/messages.send", params)
        response = payload.get("response")
        message_id = VKAdapter._sent_message_id(payload) if isinstance(response, list) else response
        thread_id = kwargs.get("thread_id")
        if thread_id and message_id:
            state = _load_lane_state(_lane_state_path())
            if str(thread_id).startswith(LANE_THREAD_PREFIX):
                lane_id = str(thread_id)[len(LANE_THREAD_PREFIX) :]
                if _is_safe_lane_id(lane_id):
                    messages = state.setdefault("message_lanes", {})
                    if isinstance(messages, dict):
                        messages[f"{chat_id}:{str(message_id).removeprefix('cmid:')}"] = lane_id
                        _lane_state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return {"success": True, "message_id": message_id}
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
        parse_target_ref_fn=_parse_vk_target_ref,
        standalone_sender_fn=_standalone_send,
        emoji="🔵",
        platform_hint="You are connected through VK Messenger. Keep replies concise; VK has no Telegram-style topics.",
        max_message_length=4096,
        allow_update_command=True,
    )
