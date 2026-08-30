import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vk_gateway_user_token.py"


def run_helper(args, *, env=None):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_auth_url_uses_gateway_app_env(tmp_path):
    rc, stdout, stderr = run_helper(
        ["auth-url"],
        env={
            "HERMES_HOME": str(tmp_path),
            "VK_GATEWAY_APP_ID": "123456",
            "VK_GATEWAY_SCOPE": "16",
            "VK_GATEWAY_REDIRECT_URI": "https://oauth.vk.com/blank.html",
        },
    )

    assert rc == 0, stderr
    data = json.loads(stdout)
    assert data["client_id"] == "123456"
    assert data["scope"] == "16"
    assert "client_id=123456" in data["auth_url"]
    assert "scope=16" in data["auth_url"]
    assert data["stores_as"] == "VK_USER_TOKEN"


def test_exchange_code_missing_secret_uses_selected_app_id(tmp_path):
    rc, stdout, _stderr = run_helper(
        ["exchange-code", "--code", "dummy", "--client-id", "999999"],
        env={"HERMES_HOME": str(tmp_path)},
    )

    assert rc != 0
    data = json.loads(stdout)
    assert data["ok"] is False
    assert data["error"] == "missing_client_secret"
    assert data["path"].endswith("vk_app_999999_client_secret")


def test_token_status_uses_metadata_refresh_app(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "vk_gateway_user_token_meta.json").write_text(
        json.dumps(
            {
                "client_id": "777777",
                "redirect_uri": "https://example.invalid/callback",
                "refresh_scope": "16",
                "expires_at": 1,
            }
        )
    )

    rc, stdout, stderr = run_helper(["token-status"], env={"HERMES_HOME": str(tmp_path)})

    assert rc != 0, stderr
    data = json.loads(stdout)
    assert data["needs_refresh"] is True
    assert "client_id=777777" in data["auth_url"]
    assert "scope=16" in data["auth_url"]
    assert "redirect_uri=https%3A%2F%2Fexample.invalid%2Fcallback" in data["auth_url"]
    assert data["token_printed"] is False
