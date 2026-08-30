#!/usr/bin/env python3
"""Manage an optional VK user token for the Hermes VK gateway plugin.

This helper mirrors the proven vkpublish server-side OAuth code exchange flow,
but stores a separate gateway-scoped token. It never prints access tokens or the
protected client secret.

Commands:
  auth-url       Print an OAuth URL for Denis to open in VK.
  exchange-code  Exchange a response_type=code value on this server, store token.
  token-status  Check metadata and live-validate the saved token with users.get.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CLIENT_ID = "54526246"
DEFAULT_REDIRECT_URI = "https://oauth.vk.com/blank.html"
DEFAULT_SCOPE = "8212"  # photos + video + wall; enough for video.get/media probes.
DEFAULT_API_VERSION = "5.199"
TOKEN_SECRET_NAME = "vk_gateway_user_token"
TOKEN_META_NAME = "vk_gateway_user_token_meta.json"
ENV_TOKEN_NAME = "VK_USER_TOKEN"
ENV_USER_ID_NAME = "VK_USER_ID"


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def _safe_error(data: dict[str, Any]) -> dict[str, Any]:
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return {
            "error_code": err.get("error_code") or err.get("code"),
            "error_msg": err.get("error_msg") or err.get("msg") or err.get("error"),
        }
    return {"error": str(data)[:300]}


def vk_request(endpoint: str, params: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    q = {"v": DEFAULT_API_VERSION, **{k: str(v) for k, v in params.items() if v is not None}}
    if token:
        q["access_token"] = token
    host = "oauth.vk.com" if endpoint == "access_token" else "api.vk.com"
    url = f"https://{host}/{endpoint}?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-vk-gateway-token/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        try:
            return json.loads(body)
        except Exception:
            return {"error": {"error_code": exc.code, "error_msg": body[:300]}}
    except Exception as exc:
        return {"error": {"error_code": "request_failed", "error_msg": str(exc)[:300]}}


def client_secret_path(home: Path, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return home / "secrets" / f"vk_app_{DEFAULT_CLIENT_ID}_client_secret"


def load_client_secret(home: Path, explicit: str | None = None) -> str:
    path = client_secret_path(home, explicit)
    if not path.exists():
        raise SystemExit(_json({"ok": False, "error": "missing_client_secret", "path": str(path)}))
    return path.read_text(encoding="utf-8").strip()


def save_env_var(env_path: Path, updates: dict[str, str]) -> None:
    lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines() if env_path.exists() else []
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                if key not in seen:
                    new_lines.append(f"{key}={updates[key]}")
                    seen.add(key)
                continue
        new_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)


def token_paths(home: Path) -> tuple[Path, Path]:
    secrets = home / "secrets"
    return secrets / TOKEN_SECRET_NAME, secrets / TOKEN_META_NAME


def live_probe(token: str) -> dict[str, Any]:
    users = vk_request("method/users.get", {}, token)
    if "error" in users:
        return {"ok": False, "method": "users.get", **_safe_error(users)}
    response = users.get("response")
    user_id = response[0].get("id") if isinstance(response, list) and response and isinstance(response[0], dict) else None
    video_probe = vk_request("method/video.get", {"count": "1"}, token)
    video_ok = "error" not in video_probe
    return {
        "ok": True,
        "user_id": user_id,
        "video_get_probe_ok": video_ok,
        "video_get_error": None if video_ok else _safe_error(video_probe),
    }


def command_auth_url(args: argparse.Namespace) -> int:
    scope = args.scope or DEFAULT_SCOPE
    url = "https://oauth.vk.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id or DEFAULT_CLIENT_ID,
            "display": "page",
            "redirect_uri": args.redirect_uri or DEFAULT_REDIRECT_URI,
            "scope": scope,
            "response_type": "code",
            "v": DEFAULT_API_VERSION,
        }
    )
    print(_json({"ok": True, "scope": scope, "auth_url": url, "stores_as": ENV_TOKEN_NAME}))
    return 0


def command_exchange_code(args: argparse.Namespace) -> int:
    home = hermes_home()
    client_id = args.client_id or DEFAULT_CLIENT_ID
    redirect_uri = args.redirect_uri or DEFAULT_REDIRECT_URI
    client_secret = load_client_secret(home, args.client_secret_path)
    token_data = vk_request(
        "access_token",
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": args.code,
        },
    )
    if "access_token" not in token_data:
        print(_json({"exchange_ok": False, "response": _safe_error(token_data)}))
        return 1

    token = str(token_data["access_token"])
    user_id = str(token_data.get("user_id") or "")
    expires_in = int(token_data.get("expires_in") or 0)
    now = int(time.time())
    probe = live_probe(token)

    stored = False
    token_path, meta_path = token_paths(home)
    if not args.no_store:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(token + "\n", encoding="utf-8")
        os.chmod(token_path, 0o600)
        meta_path.write_text(
            _json(
                {
                    "user_id": user_id or probe.get("user_id"),
                    "issued_at": now,
                    "expires_in": expires_in,
                    "expires_at": now + expires_in if expires_in else None,
                    "source": "server_code_exchange",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "refresh_scope": args.scope or DEFAULT_SCOPE,
                    "scope_note": args.scope_note,
                    "env_key": ENV_TOKEN_NAME,
                    "secret_name": TOKEN_SECRET_NAME,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(meta_path, 0o600)
        updates = {ENV_TOKEN_NAME: token}
        if user_id:
            updates[ENV_USER_ID_NAME] = user_id
        save_env_var(home / ".env", updates)
        stored = True

    print(
        _json(
            {
                "exchange_ok": True,
                "stored": stored,
                "token_len": len(token),
                "user_id": user_id or probe.get("user_id"),
                "expires_in": expires_in,
                "expires_at": now + expires_in if expires_in else None,
                "probe": probe,
                "token_printed": False,
            }
        )
    )
    return 0 if probe.get("ok") else 1


def command_token_status(args: argparse.Namespace) -> int:
    home = hermes_home()
    token_path, meta_path = token_paths(home)
    token = token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    now = int(time.time())
    expires_at = int(meta.get("expires_at") or 0)
    seconds_left = expires_at - now if expires_at else None
    needs_refresh = not token or not expires_at or (seconds_left is not None and seconds_left < args.min_seconds_left)
    probe = live_probe(token) if token else {"ok": False, "error": "missing_token"}
    if not probe.get("ok"):
        needs_refresh = True
    auth_scope = str(meta.get("refresh_scope") or args.scope or DEFAULT_SCOPE)
    auth_url = "https://oauth.vk.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": str(meta.get("client_id") or args.client_id or DEFAULT_CLIENT_ID),
            "display": "page",
            "redirect_uri": str(meta.get("redirect_uri") or args.redirect_uri or DEFAULT_REDIRECT_URI),
            "scope": auth_scope,
            "response_type": "code",
            "v": DEFAULT_API_VERSION,
        }
    )
    print(
        _json(
            {
                "ok": bool(token) and bool(probe.get("ok")) and not needs_refresh,
                "token_exists": bool(token),
                "metadata_exists": meta_path.exists(),
                "seconds_left": seconds_left,
                "needs_refresh": needs_refresh,
                "live_probe": probe,
                "auth_url": auth_url if needs_refresh else None,
                "token_printed": False,
            }
        )
    )
    return 0 if not needs_refresh else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage explicit VK_USER_TOKEN for Hermes VK gateway media enrichment")
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth-url")
    p_auth.add_argument("--scope", default=DEFAULT_SCOPE)
    p_auth.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    p_auth.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    p_auth.set_defaults(func=command_auth_url)

    p_exchange = sub.add_parser("exchange-code")
    p_exchange.add_argument("--code", required=True)
    p_exchange.add_argument("--scope", default=DEFAULT_SCOPE)
    p_exchange.add_argument("--scope-note", default="photos+video+wall, no offline; gateway media enrichment")
    p_exchange.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    p_exchange.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    p_exchange.add_argument("--client-secret-path", default=None)
    p_exchange.add_argument("--no-store", action="store_true")
    p_exchange.set_defaults(func=command_exchange_code)

    p_status = sub.add_parser("token-status")
    p_status.add_argument("--min-seconds-left", type=int, default=3600)
    p_status.add_argument("--scope", default=DEFAULT_SCOPE)
    p_status.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    p_status.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    p_status.set_defaults(func=command_token_status)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
