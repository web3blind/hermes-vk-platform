"""Interactive setup helper for the Hermes VK Messenger platform plugin.

The helper is intentionally small and dependency-light.  Hermes calls it from
`hermes gateway setup` through the platform registry `setup_fn` hook.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any, Iterable


def _slugify_lane_id(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"[-_]{2,}", "-", text).strip("-_")
    return text[:64]


def _lane_from_mapping(item: dict[str, Any], *, source: str) -> dict[str, Any] | None:
    name = str(item.get("name") or item.get("title") or item.get("topic") or item.get("thread_name") or "").strip()
    id_source = item.get("id") or item.get("lane_id")
    if not id_source:
        folderish = str(item.get("folder") or item.get("workdir") or "").strip()
        if folderish:
            id_source = Path(folderish).name
    lane_id = _slugify_lane_id(id_source or name)
    if not lane_id and item.get("thread_id"):
        lane_id = f"topic-{item.get('thread_id')}"
    if not lane_id or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", lane_id):
        return None
    lane: dict[str, Any] = {"id": lane_id, "name": name or lane_id}
    aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
    if aliases:
        lane["aliases"] = [str(a).strip().lstrip("@").lower() for a in aliases if str(a).strip()]
    workdir = str(item.get("workdir") or item.get("folder") or "").strip()
    if workdir:
        lane["workdir"] = workdir
    folder = str(item.get("folder") or "").strip()
    if folder:
        lane["folder"] = folder
    skills = item.get("skills") if isinstance(item.get("skills"), list) else item.get("skill")
    if skills:
        lane["skills"] = [str(s).strip() for s in (skills if isinstance(skills, list) else str(skills).split(",")) if str(s).strip()]
    desc_parts = []
    if item.get("description") or item.get("context"):
        desc_parts.append(str(item.get("description") or item.get("context")).strip())
    if item.get("thread_id"):
        desc_parts.append(f"Imported from {source} thread/topic {item.get('thread_id')}.")
    elif source:
        desc_parts.append(f"Imported from {source}.")
    if desc_parts:
        lane["description"] = " ".join(p for p in desc_parts if p)
    return lane


def _collect_telegram_topic_lanes(cfg: dict[str, Any], *, from_chat: str = "") -> list[dict[str, Any]]:
    lanes: list[dict[str, Any]] = []
    candidates: list[Any] = []
    platforms = cfg.get("platforms") if isinstance(cfg.get("platforms"), dict) else {}
    telegram_platform = platforms.get("telegram") if isinstance(platforms, dict) else {}
    tg_extra_obj = telegram_platform.get("extra") if isinstance(telegram_platform, dict) else {}
    tg_extra = tg_extra_obj if isinstance(tg_extra_obj, dict) else {}
    telegram_legacy = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    for value in (
        tg_extra.get("group_topics"),
        tg_extra.get("dm_topics"),
        telegram_legacy.get("group_topics") if isinstance(telegram_legacy, dict) else None,
        telegram_legacy.get("dm_topics") if isinstance(telegram_legacy, dict) else None,
    ):
        if isinstance(value, list):
            candidates.extend(value)
    for chat in candidates:
        if not isinstance(chat, dict):
            continue
        if from_chat and str(chat.get("chat_id") or "") != str(from_chat):
            continue
        for topic in chat.get("topics") or []:
            if isinstance(topic, dict):
                lane = _lane_from_mapping(topic, source="telegram")
                if lane:
                    lanes.append(lane)
    return lanes


def _collect_discord_thread_lanes(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    lanes: list[dict[str, Any]] = []
    discord = cfg.get("discord") if isinstance(cfg.get("discord"), dict) else {}
    for key in ("threads", "thread_mappings", "channel_threads"):
        value = discord.get(key)
        items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
        for item in items:
            if isinstance(item, dict):
                lane = _lane_from_mapping(item, source="discord")
                if lane:
                    lanes.append(lane)
    return lanes


def _upsert_lane(chats: dict[str, Any], peer_id: str, lane: dict[str, Any]) -> None:
    chat = chats.setdefault(str(peer_id), {})
    if not isinstance(chat, dict):
        chat = {}
        chats[str(peer_id)] = chat
    lanes = chat.setdefault("lanes", [])
    if not isinstance(lanes, list):
        lanes = []
        chat["lanes"] = lanes
    lane_name = str(lane.get("name") or "").strip().lower()
    for idx, item in enumerate(lanes):
        if isinstance(item, dict) and str(item.get("id") or "").lower() == lane["id"]:
            lanes[idx] = {**item, **lane}
            return
        if lane_name and isinstance(item, dict) and str(item.get("name") or "").strip().lower() == lane_name:
            merged = {**item, **lane, "id": str(item.get("id") or lane["id"])}
            lanes[idx] = merged
            return
    lanes.append(lane)


def import_project_lanes_to_vk(peer_id: str, *, sources: Iterable[str] = ("legacy", "telegram", "discord"), telegram_chat_id: str = "", dry_run: bool = False) -> dict:
    """Import project/thread mappings into canonical VK project-lane config.

    Writes to ``platforms.vk.extra.project_lanes``. Existing lanes with the same
    id are updated in place. Returns a compact report for tests/CLI.
    """
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    source_set = {str(s).strip().lower() for s in sources if str(s).strip()}
    imported: list[dict] = []
    if "legacy" in source_set:
        legacy = ((cfg.get("vk") or {}).get("project_lanes") if isinstance(cfg.get("vk"), dict) else None) or {}
        if isinstance(legacy, dict):
            for peer_cfg in legacy.values():
                if isinstance(peer_cfg, dict):
                    for lane in peer_cfg.get("lanes") or []:
                        if isinstance(lane, dict):
                            mapped = _lane_from_mapping(lane, source="vk.legacy")
                            if mapped:
                                imported.append(mapped)
    if "telegram" in source_set:
        imported.extend(_collect_telegram_topic_lanes(cfg, from_chat=telegram_chat_id))
    if "discord" in source_set:
        imported.extend(_collect_discord_thread_lanes(cfg))

    platforms = cfg.setdefault("platforms", {})
    vk_cfg = platforms.setdefault("vk", {})
    extra = vk_cfg.setdefault("extra", {})
    feature = extra.setdefault("project_lanes", {})
    feature["enabled"] = True
    chats = feature.setdefault("chats", {})
    before = len((chats.get(str(peer_id)) or {}).get("lanes") or []) if isinstance(chats.get(str(peer_id)), dict) else 0
    for lane in imported:
        _upsert_lane(chats, str(peer_id), lane)
    after = len((chats.get(str(peer_id)) or {}).get("lanes") or []) if isinstance(chats.get(str(peer_id)), dict) else 0
    if not dry_run:
        save_config(
            cfg,
            preserve_keys={
                ("platforms", "vk", "extra", "project_lanes"),
                ("platforms", "vk", "extra", "project_lanes", "chats", str(peer_id)),
            },
        )
    return {"peer_id": str(peer_id), "sources": sorted(source_set), "imported": len(imported), "before": before, "after": after, "dry_run": dry_run}


def _ask(prompt: str, *, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled. Existing settings were left unchanged.")
        raise SystemExit(1)
    return value or default


def _ask_secret(prompt: str, *, existing: bool = False) -> str:
    try:
        from hermes_cli.secret_prompt import masked_secret_prompt

        suffix = " [already set; Enter to keep]" if existing else ""
        return masked_secret_prompt(f"{prompt}{suffix}: ").strip()
    except ImportError:  # pragma: no cover - Hermes always has this in normal use
        import getpass

        suffix = " [already set; Enter to keep]" if existing else ""
        return getpass.getpass(f"{prompt}{suffix}: ").strip()


def _truthy_answer(value: str) -> bool:
    return value.strip().lower() in {"1", "y", "yes", "true", "on", "да", "д"}


def _save_env(name: str, value: str, *, secret: bool = False) -> None:
    from hermes_cli.config import save_env_value

    save_env_value(name, value)
    os.environ[name] = value
    marker = "secret " if secret else ""
    print(f"  ✓ Saved {marker}{name} to ~/.hermes/.env")


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _join_csv(values: Iterable[str]) -> str:
    return ",".join(str(v).strip() for v in values if str(v).strip())


def _print_vk_prerequisites() -> None:
    print("""
VK community prerequisites:
  1. Create or choose a VK community.
  2. Enable community messages.
  3. Enable bot/Long Poll API message events for the community.
  4. For group conversations, enable the VK setting that allows the community
     bot to work in chats / be added to conversations. If this is off, VK may
     return API error 912: "This is a chat bot feature".
     Russian VK UI path: Управление сообществом → Сообщения → Настройки для
     бота → Возможности ботов. Enable "Возможности ботов" and
     "Разрешать добавлять сообщество в чаты".
  5. Create a community access token with messages permission.
  6. Add the community bot to the VK chat if you want group conversation support.
""".strip())


def setup_vk_platform() -> None:
    """Prompt for the minimal safe VK platform configuration.

    Secrets and operational env vars are stored in `~/.hermes/.env` using
    Hermes' own config helper.  The helper defaults to allowlist mode and does
    not enable open access unless the operator explicitly asks for it.
    """
    try:
        from hermes_cli.config import get_env_value
        from hermes_constants import display_hermes_home
    except ImportError as exc:  # pragma: no cover - defensive for non-Hermes import
        raise RuntimeError("This setup helper must be run inside Hermes Agent") from exc

    print("\nVK Messenger platform setup for Hermes Agent")
    print("This stores secrets in ~/.hermes/.env and does not print token values.\n")
    _print_vk_prerequisites()
    print()

    existing_token = bool(get_env_value("VK_GROUP_TOKEN"))
    token = _ask_secret("VK community token (messages permission)", existing=existing_token)
    if token:
        _save_env("VK_GROUP_TOKEN", token, secret=True)
    elif not existing_token:
        print("  ! VK_GROUP_TOKEN was not set. Add it later before starting the gateway.")

    current_group = get_env_value("VK_GROUP_ID") or ""
    group_id = _ask("Numeric VK group/community id, without leading minus", default=current_group).lstrip("-")
    if group_id:
        _save_env("VK_GROUP_ID", group_id)

    print("\nAccess control (recommended: allow only your user id and/or known peer ids).")
    current_users = get_env_value("VK_ALLOWED_USERS") or ""
    users = _ask("Allowed VK user ids, comma-separated; empty to skip", default=current_users)
    if users:
        _save_env("VK_ALLOWED_USERS", _join_csv(_split_csv(users)))

    current_peers = get_env_value("VK_ALLOWED_PEERS") or ""
    peers = _ask("Allowed VK peer/chat ids, comma-separated; empty to skip", default=current_peers)
    if peers:
        _save_env("VK_ALLOWED_PEERS", _join_csv(_split_csv(peers)))

    current_policy = get_env_value("VK_ACCESS_POLICY") or "any"
    print(
        "\nAccess policy options: any (user OR peer), user_only, peer_only, peer_and_user.\n"
        "Warning: peer_only/allowed peer chats allow every participant of that VK chat."
    )
    policy = _ask("VK access policy", default=current_policy).strip().lower() or "any"
    if policy not in {"any", "user_only", "peer_only", "peer_and_user"}:
        print("  ! Unknown policy; saving fail-closed value is not useful, keeping 'any'.")
        policy = "any"
    _save_env("VK_ACCESS_POLICY", policy)

    current_users_by_peer = get_env_value("VK_ALLOWED_USERS_BY_PEER") or ""
    users_by_peer = _ask(
        "Optional per-peer user allowlist (peer:user|user;peer:user), empty to skip",
        default=current_users_by_peer,
    )
    if users_by_peer:
        _save_env("VK_ALLOWED_USERS_BY_PEER", users_by_peer)

    current_open = get_env_value("VK_ALLOW_ALL_USERS") or "false"
    if not users and not peers:
        allow_all = _ask("No allowlist was provided. Allow all VK users? UNSAFE, testing only", default=current_open)
        _save_env("VK_ALLOW_ALL_USERS", "true" if _truthy_answer(allow_all) else "false")
    elif current_open and _truthy_answer(current_open):
        keep_open = _ask("VK_ALLOW_ALL_USERS is currently true. Disable open access now?", default="yes")
        if _truthy_answer(keep_open):
            _save_env("VK_ALLOW_ALL_USERS", "false")

    current_home = get_env_value("VK_HOME_CHANNEL") or ""
    print(
        "\nVK_HOME_CHANNEL is only the default peer for cron/home/notification delivery.\n"
        "Do not change it just because you created a new thematic chat; add that\n"
        "chat to VK_ALLOWED_PEERS and configure channel_prompts/skills instead."
    )
    home_prompt = "Default VK Home peer id for cron/home delivery; Enter to keep/skip"
    home = _ask(home_prompt, default=current_home)
    if home:
        _save_env("VK_HOME_CHANNEL", home)

    current_download = get_env_value("VK_DOWNLOAD_ATTACHMENTS") or ""
    download = _ask("Download inbound attachments to Hermes cache? true/false", default=current_download or "false")
    _save_env("VK_DOWNLOAD_ATTACHMENTS", "true" if _truthy_answer(download) else "false")

    current_max_attachment = get_env_value("VK_MAX_ATTACHMENT_BYTES") or "26214400"
    max_attachment = _ask("Maximum inbound attachment size in bytes", default=current_max_attachment)
    if max_attachment:
        _save_env("VK_MAX_ATTACHMENT_BYTES", max_attachment)

    current_dedupe_ttl = get_env_value("VK_DEDUPE_TTL_SECONDS") or "1800"
    dedupe_ttl = _ask("Duplicate event TTL in seconds", default=current_dedupe_ttl)
    if dedupe_ttl:
        _save_env("VK_DEDUPE_TTL_SECONDS", dedupe_ttl)

    print("\nDone. Next steps:")
    print("  1. Run: hermes gateway restart")
    print("  2. Send a message to the VK community or an allowed VK chat.")
    print("  3. If it does not respond, check: ~/.hermes/logs/gateway.log")
    print(f"\nConfig home: {display_hermes_home()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VK platform setup helper")
    sub = parser.add_subparsers(dest="cmd")
    lanes = sub.add_parser("lanes", help="Manage VK project lanes")
    lanes_sub = lanes.add_subparsers(dest="lanes_cmd")
    imp = lanes_sub.add_parser("import", help="Import Telegram/Discord/legacy project mappings into VK lanes")
    imp.add_argument("--to-peer", required=True, help="VK peer id to receive imported lanes")
    imp.add_argument("--from", dest="sources", default="legacy,telegram,discord", help="Comma-separated sources: legacy,telegram,discord")
    imp.add_argument("--telegram-chat-id", default="", help="Optional Telegram group chat id to import from, e.g. -1001234567890")
    imp.add_argument("--dry-run", action="store_true", help="Preview without writing config.yaml")
    args = parser.parse_args(argv)
    if args.cmd == "lanes" and args.lanes_cmd == "import":
        report = import_project_lanes_to_vk(
            args.to_peer,
            sources=[part.strip() for part in str(args.sources).split(",") if part.strip()],
            telegram_chat_id=str(args.telegram_chat_id or ""),
            dry_run=bool(args.dry_run),
        )
        print(
            f"VK project lanes import: peer={report['peer_id']} sources={','.join(report['sources'])} "
            f"imported={report['imported']} before={report['before']} after={report['after']} dry_run={report['dry_run']}"
        )
        return 0
    setup_vk_platform()
    return 0


if __name__ == "__main__":  # pragma: no cover - manual helper entrypoint
    raise SystemExit(main())
