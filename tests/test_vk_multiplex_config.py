"""Behavioral coverage for VK external-plugin profile isolation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("owner_source", ["dotenv", "canonical_yaml", "legacy_yaml"])
def test_external_vk_plugin_profile_loads_fail_closed(tmp_path, owner_source):
    root = tmp_path / "default"
    child = root / "profiles" / "route-only"
    owner = root / "profiles" / "independent"
    plugin_copy = root / "plugins" / "vk-platform"
    shutil.copytree(PLUGIN_ROOT, plugin_copy, ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "*.pyc"))
    for home in (root, child, owner):
        home.mkdir(parents=True, exist_ok=True)
        if home != root:
            shutil.copytree(plugin_copy, home / "plugins" / "vk-platform")
        (home / "config.yaml").write_text("plugins:\n  enabled: [vk-platform]\ngateway:\n  multiplex_profiles: true\n", encoding="utf-8")
        (home / ".env").write_text("", encoding="utf-8")
    (root / ".env").write_text("VK_GROUP_TOKEN=default-test-token\nVK_GROUP_ID=100\nVK_ALLOWED_USERS=11\nVK_ALLOW_ALL_USERS=true\nVK_USER_TOKEN=default-user-token\n", encoding="utf-8")  # pragma: allowlist secret - synthetic test credentials
    if owner_source == "dotenv":
        (owner / ".env").write_text("VK_GROUP_TOKEN=independent-test-token\nVK_GROUP_ID=200\nVK_ALLOWED_USERS=22\n", encoding="utf-8")  # pragma: allowlist secret - synthetic test credentials
        with (owner / "config.yaml").open("a", encoding="utf-8") as stream:
            stream.write("platforms:\n  vk:\n    enabled: true\n")
    elif owner_source == "canonical_yaml":
        with (owner / "config.yaml").open("a", encoding="utf-8") as stream:
            stream.write("platforms:\n  vk:\n    enabled: true\n    token: independent-test-token\n    extra:\n      group_id: '200'\n      allowed_users: ['22']\n")
    else:
        with (owner / "config.yaml").open("a", encoding="utf-8") as stream:
            stream.write("platforms:\n  vk:\n    enabled: true\nvk:\n  group_token: independent-test-token\n  group_id: '200'\n  allowed_users: ['22']\n")

    script = textwrap.dedent("""
        import inspect, json, os, sys
        from pathlib import Path
        import agent.secret_scope as secrets
        import gateway.config as config
        import gateway.run as run
        from gateway.platform_registry import platform_registry
        from hermes_cli.plugins import PluginManager
        root, child, owner, expected_plugin = map(Path, sys.argv[1:])
        PluginManager().discover_and_load()
        entry = platform_registry.get('vk')
        assert entry is not None
        assert Path(inspect.getfile(entry.adapter_factory)).resolve().is_relative_to(expected_plugin.resolve())
        secrets.set_multiplex_active(True)
        ambient = {k: v for k, v in os.environ.items() if k.startswith(('VK_', 'VKBLOG_'))}
        snapshots = {}
        for name, home in (('default', root), ('child', child), ('owner', owner), ('child-again', child), ('default-again', root)):
            with run._profile_runtime_scope(home, hydrate_secrets=False):
                manager = PluginManager()
                manager.discover_and_load()
                entry = platform_registry.get('vk')
                assert entry is not None
                assert Path(inspect.getfile(entry.adapter_factory)).resolve().is_relative_to((home / 'plugins' / 'vk-platform').resolve())
                loaded = config.load_gateway_config()
                vk = loaded.platforms.get(config.Platform('vk'))
                if name.startswith('child'):
                    assert vk is None or not vk.enabled
                    assert entry.env_enablement_fn() is None
                    empty = config.PlatformConfig()
                    assert not entry.is_connected(empty)
                    adapter = entry.adapter_factory(empty)
                    assert not adapter.token and not adapter.user_token
                    assert not adapter.allowed_users and not adapter.allowed_peers
                    assert adapter.allow_all_users is False
                    snapshots[name] = None
                else:
                    assert vk is not None and vk.enabled
                    adapter = entry.adapter_factory(vk)
                    snapshots[name] = [adapter.token, adapter.group_id, sorted(adapter.allowed_users), adapter.allow_all_users]
                    assert entry.is_connected(vk), (name, vk, snapshots[name])
                    if name == 'owner':
                        assert not adapter.user_token
            assert {k: v for k, v in os.environ.items() if k.startswith(('VK_', 'VKBLOG_'))} == ambient
        print(json.dumps(snapshots, sort_keys=True))
    """)
    env = os.environ.copy()
    env.update(HOME=str(tmp_path / "os-home"), HERMES_HOME=str(root), HERMES_BUNDLED_PLUGINS=str(tmp_path / "empty-bundled"), VK_GROUP_TOKEN="default-test-token", VK_GROUP_ID="100", VK_ALLOWED_USERS="11", VK_ALLOW_ALL_USERS="true", VK_USER_TOKEN="default-user-token")
    (tmp_path / "empty-bundled").mkdir()
    result = subprocess.run([sys.executable, "-c", script, str(root), str(child), str(owner), str(plugin_copy)], env=env, text=True, capture_output=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr
    snapshots = json.loads(result.stdout.strip().splitlines()[-1])
    assert snapshots["child"] is None and snapshots["child-again"] is None
    assert snapshots["default"] == snapshots["default-again"] == ["default-test-token", "100", ["11"], True]
    assert snapshots["owner"] == ["independent-test-token", "200", ["22"], False]
