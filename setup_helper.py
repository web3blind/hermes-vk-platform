"""Interactive setup helper for the Hermes VK Messenger platform plugin.

The helper is intentionally small and dependency-light.  Hermes calls it from
`hermes gateway setup` through the platform registry `setup_fn` hook.
"""

from __future__ import annotations

import os
from typing import Iterable


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
  4. Create a community access token with messages permission.
  5. Add the community bot to the VK chat if you want group conversation support.
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
    home = _ask("Default VK peer id for cron/home delivery; empty to skip", default=current_home)
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
