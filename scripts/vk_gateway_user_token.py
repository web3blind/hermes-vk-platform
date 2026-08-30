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

DEFAULT_CAMOFOX_URL = "http://127.0.0.1:9377"
DEFAULT_CAMOFOX_USER_ID = "hermes_1674ee0144"
ENV_CAMOFOX_URL_NAME = "VK_GATEWAY_CAMOFOX_URL"
ENV_CAMOFOX_USER_ID_NAME = "VK_GATEWAY_CAMOFOX_USER_ID"

DEFAULT_CLIENT_ID = "54526246"  # Local/default app; other installs should pass their own app id.
DEFAULT_REDIRECT_URI = "https://oauth.vk.com/blank.html"
DEFAULT_SCOPE = "8212"  # photos + video + wall; enough for video.get/media probes.
DEFAULT_API_VERSION = "5.199"
ENV_APP_ID_NAME = "VK_GATEWAY_APP_ID"
ENV_REDIRECT_URI_NAME = "VK_GATEWAY_REDIRECT_URI"
ENV_SCOPE_NAME = "VK_GATEWAY_SCOPE"
ENV_CLIENT_SECRET_FILE_NAME = "VK_GATEWAY_CLIENT_SECRET_FILE"
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


def _default_client_id() -> str:
    return os.environ.get(ENV_APP_ID_NAME, "").strip() or DEFAULT_CLIENT_ID


def _default_redirect_uri() -> str:
    return os.environ.get(ENV_REDIRECT_URI_NAME, "").strip() or DEFAULT_REDIRECT_URI


def _default_scope() -> str:
    return os.environ.get(ENV_SCOPE_NAME, "").strip() or DEFAULT_SCOPE


def _default_client_secret_file() -> str | None:
    return os.environ.get(ENV_CLIENT_SECRET_FILE_NAME, "").strip() or None


def _camofox_url() -> str:
    return os.environ.get(ENV_CAMOFOX_URL_NAME, "").strip().rstrip("/") or DEFAULT_CAMOFOX_URL


def _camofox_user_id() -> str:
    return os.environ.get(ENV_CAMOFOX_USER_ID_NAME, "").strip() or DEFAULT_CAMOFOX_USER_ID


def _http_json(method: str, url: str, *, body: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    data = None
    headers = {"User-Agent": "hermes-vk-gateway-token/1.0"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    api_key = os.environ.get("CAMOFOX_API_KEY", "").strip()
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "ignore")[:300]
        return {"ok": False, "error": "http_error", "status": exc.code, "message": payload}
    except Exception as exc:
        return {"ok": False, "error": "request_failed", "message": str(exc)[:300]}


def _extract_oauth_code(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    parts = {}
    parts.update(urllib.parse.parse_qs(parsed.query))
    parts.update(urllib.parse.parse_qs(parsed.fragment))
    code = (parts.get("code") or [""])[0]
    error = (parts.get("error") or [""])[0]
    return code, error


def client_secret_path(home: Path, client_id: str, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return home / "secrets" / f"vk_app_{client_id}_client_secret"


def load_client_secret(home: Path, client_id: str, explicit: str | None = None) -> str:
    path = client_secret_path(home, client_id, explicit)
    if not path.exists():
        print(_json({"ok": False, "error": "missing_client_secret", "path": str(path)}))
        raise SystemExit(2)
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
    client_id = args.client_id or _default_client_id()
    redirect_uri = args.redirect_uri or _default_redirect_uri()
    scope = args.scope or _default_scope()
    url = "https://oauth.vk.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "display": "page",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "response_type": "code",
            "v": DEFAULT_API_VERSION,
        }
    )
    print(_json({"ok": True, "client_id": client_id, "redirect_uri": redirect_uri, "scope": scope, "auth_url": url, "stores_as": ENV_TOKEN_NAME}))
    return 0


def command_exchange_code(args: argparse.Namespace) -> int:
    home = hermes_home()
    client_id = args.client_id or _default_client_id()
    redirect_uri = args.redirect_uri or _default_redirect_uri()
    scope = args.scope or _default_scope()
    client_secret = load_client_secret(home, client_id, args.client_secret_path or _default_client_secret_file())
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
                    "refresh_scope": scope,
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
    auth_scope = str(meta.get("refresh_scope") or args.scope or _default_scope())
    client_id = str(meta.get("client_id") or args.client_id or _default_client_id())
    redirect_uri = str(meta.get("redirect_uri") or args.redirect_uri or _default_redirect_uri())
    auth_url = "https://oauth.vk.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "display": "page",
            "redirect_uri": redirect_uri,
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


def command_refresh_browser(args: argparse.Namespace) -> int:
    """Try to refresh the gateway user token through an already logged-in Camofox session."""
    home = hermes_home()
    client_id = args.client_id or _default_client_id()
    redirect_uri = args.redirect_uri or _default_redirect_uri()
    scope = args.scope or _default_scope()
    secret_path = client_secret_path(home, client_id, args.client_secret_path or _default_client_secret_file())
    if not secret_path.exists():
        print(_json({"ok": False, "refreshed": False, "needs_human_auth": False, "error": "missing_client_secret", "path": str(secret_path), "token_printed": False}))
        return 2

    auth_url = "https://oauth.vk.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "display": "page",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "response_type": "code",
            "v": DEFAULT_API_VERSION,
        }
    )
    base = (args.camofox_url or _camofox_url()).rstrip("/")
    user_id = args.camofox_user_id or _camofox_user_id()
    opened = _http_json(
        "POST",
        f"{base}/tabs/open",
        body={"url": auth_url, "userId": user_id, "listItemId": "vk-gateway-oauth-refresh"},
        timeout=args.open_timeout,
    )
    if not opened.get("ok"):
        print(_json({"ok": False, "refreshed": False, "needs_human_auth": False, "error": "browser_open_failed", "browser": opened, "token_printed": False}))
        return 3

    tab_id = str(opened.get("tabId") or opened.get("targetId") or "")
    observed_url = str(opened.get("url") or "")
    deadline = time.time() + args.wait_seconds
    code = ""
    error = ""
    while time.time() < deadline:
        code, error = _extract_oauth_code(observed_url)
        if code or error:
            break
        tabs = _http_json("GET", f"{base}/tabs?" + urllib.parse.urlencode({"userId": user_id}), timeout=5)
        for tab in tabs.get("tabs") or []:
            if tab_id and str(tab.get("tabId") or tab.get("targetId") or "") == tab_id:
                observed_url = str(tab.get("url") or observed_url)
                break
            if str(tab.get("listItemId") or "") == "vk-gateway-oauth-refresh":
                observed_url = str(tab.get("url") or observed_url)
                tab_id = str(tab.get("tabId") or tab.get("targetId") or tab_id)
                break
        time.sleep(args.poll_interval)

    if error:
        print(_json({"ok": False, "refreshed": False, "needs_human_auth": True, "error": "oauth_error", "oauth_error": error, "final_host": urllib.parse.urlparse(observed_url).netloc, "token_printed": False}))
        return 4
    if not code:
        snapshot = _http_json("GET", f"{base}/snapshot?" + urllib.parse.urlencode({"targetId": tab_id, "userId": user_id, "format": "aria"}), timeout=5) if tab_id else {}
        text = str(snapshot.get("snapshot") or "").lower()
        needs_login = any(marker in text for marker in ("вход", "log in", "login", "пароль", "password", "captcha", "подтверд"))
        print(_json({"ok": False, "refreshed": False, "needs_human_auth": True, "error": "oauth_code_not_obtained", "browser_url_host": urllib.parse.urlparse(observed_url).netloc, "browser_url_path": urllib.parse.urlparse(observed_url).path, "login_or_challenge_likely": needs_login, "token_printed": False}))
        return 5

    exchange_args = argparse.Namespace(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        scope_note=args.scope_note,
        client_secret_path=str(secret_path),
        code=code,
        no_store=False,
    )
    rc = command_exchange_code(exchange_args)
    return rc



def main() -> int:
    parser = argparse.ArgumentParser(description="Manage explicit VK_USER_TOKEN for Hermes VK gateway media enrichment")
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth-url")
    p_auth.add_argument("--scope", default=None, help=f"OAuth scope; default env {ENV_SCOPE_NAME} or {DEFAULT_SCOPE}")
    p_auth.add_argument("--client-id", default=None, help=f"VK app id; default env {ENV_APP_ID_NAME} or local default")
    p_auth.add_argument("--redirect-uri", default=None, help=f"OAuth redirect URI; default env {ENV_REDIRECT_URI_NAME} or blank.html")
    p_auth.set_defaults(func=command_auth_url)

    p_exchange = sub.add_parser("exchange-code")
    p_exchange.add_argument("--code", required=True)
    p_exchange.add_argument("--scope", default=None, help=f"OAuth scope stored for refresh; default env {ENV_SCOPE_NAME} or {DEFAULT_SCOPE}")
    p_exchange.add_argument("--scope-note", default="photos+video+wall, no offline; gateway media enrichment")
    p_exchange.add_argument("--client-id", default=None, help=f"VK app id; default env {ENV_APP_ID_NAME} or local default")
    p_exchange.add_argument("--redirect-uri", default=None, help=f"OAuth redirect URI; default env {ENV_REDIRECT_URI_NAME} or blank.html")
    p_exchange.add_argument("--client-secret-path", default=None, help=f"Protected key path; default env {ENV_CLIENT_SECRET_FILE_NAME} or ~/.hermes/secrets/vk_app_<client_id>_client_secret")
    p_exchange.add_argument("--no-store", action="store_true")
    p_exchange.set_defaults(func=command_exchange_code)

    p_status = sub.add_parser("token-status")
    p_status.add_argument("--min-seconds-left", type=int, default=3600)
    p_status.add_argument("--scope", default=None, help=f"OAuth scope for refresh URL; default env {ENV_SCOPE_NAME} or {DEFAULT_SCOPE}")
    p_status.add_argument("--client-id", default=None, help=f"VK app id; default env {ENV_APP_ID_NAME} or local default")
    p_status.add_argument("--redirect-uri", default=None, help=f"OAuth redirect URI; default env {ENV_REDIRECT_URI_NAME} or blank.html")
    p_status.set_defaults(func=command_token_status)

    p_browser = sub.add_parser("refresh-browser")
    p_browser.add_argument("--scope", default=None, help=f"OAuth scope; default env {ENV_SCOPE_NAME} or {DEFAULT_SCOPE}")
    p_browser.add_argument("--scope-note", default="photos+video+wall, no offline; gateway browser refresh")
    p_browser.add_argument("--client-id", default=None, help=f"VK app id; default env {ENV_APP_ID_NAME} or local default")
    p_browser.add_argument("--redirect-uri", default=None, help=f"OAuth redirect URI; default env {ENV_REDIRECT_URI_NAME} or blank.html")
    p_browser.add_argument("--client-secret-path", default=None, help=f"Protected key path; default env {ENV_CLIENT_SECRET_FILE_NAME} or ~/.hermes/secrets/vk_app_<client_id>_client_secret")
    p_browser.add_argument("--camofox-url", default=None, help=f"Camofox REST URL; default env {ENV_CAMOFOX_URL_NAME} or {DEFAULT_CAMOFOX_URL}")
    p_browser.add_argument("--camofox-user-id", default=None, help=f"Camofox user id; default env {ENV_CAMOFOX_USER_ID_NAME} or {DEFAULT_CAMOFOX_USER_ID}")
    p_browser.add_argument("--wait-seconds", type=float, default=45.0)
    p_browser.add_argument("--poll-interval", type=float, default=1.0)
    p_browser.add_argument("--open-timeout", type=float, default=30.0)
    p_browser.set_defaults(func=command_refresh_browser)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
