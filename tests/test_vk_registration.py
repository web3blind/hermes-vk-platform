import importlib.util
import inspect
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import uuid

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package():
    package_name = f"vk_registration_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        package_name,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


class _CapturingContext:
    def __init__(self, error=None):
        self.kwargs = None
        self.error = error

    def register_platform(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error


def _assert_base_registration(kwargs):
    assert kwargs["name"] == "vk"
    assert kwargs["allowed_users_env"] == "VK_ALLOWED_USERS"
    assert kwargs["allow_all_env"] == "VK_ALLOW_ALL_USERS"
    assert kwargs["cron_deliver_env_var"] == "VK_HOME_CHANNEL"
    assert callable(kwargs["validate_config"])
    assert callable(kwargs["is_connected"])
    assert callable(kwargs["env_enablement_fn"])
    assert callable(kwargs["apply_yaml_config_fn"])
    assert callable(kwargs["standalone_sender_fn"])


def test_register_includes_lane_parser_when_platform_entry_supports_it():
    from gateway.platform_registry import PlatformEntry

    assert "parse_target_ref_fn" in inspect.signature(PlatformEntry).parameters
    plugin = _load_plugin_package()
    ctx = _CapturingContext()

    plugin.register(ctx)

    _assert_base_registration(ctx.kwargs)
    assert callable(ctx.kwargs["parse_target_ref_fn"])
    assert ctx.kwargs["parse_target_ref_fn"]("2000000042:lane:alpha") == (
        "2000000042",
        "lane:alpha",
    )


def test_register_omits_only_lane_parser_for_missing_field_shape(monkeypatch, caplog):
    import gateway.platform_registry as registry_module

    original = registry_module.PlatformEntry

    def platform_entry_without_parser(
        name,
        label,
        adapter_factory,
        check_fn,
        validate_config=None,
        required_env=None,
        install_hint="",
        **kwargs,
    ):
        return original(
            name=name,
            label=label,
            adapter_factory=adapter_factory,
            check_fn=check_fn,
            validate_config=validate_config,
            required_env=required_env or [],
            install_hint=install_hint,
            **kwargs,
        )

    # Give the synthetic reported-API model an explicit signature without the
    # parser field while retaining a kwargs channel for unrelated core fields.
    platform_entry_without_parser.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [parameter
        for name, parameter in inspect.signature(original).parameters.items()
        if name != "parse_target_ref_fn"]
    )
    monkeypatch.setattr(registry_module, "PlatformEntry", platform_entry_without_parser)
    plugin = _load_plugin_package()
    ctx = _CapturingContext()

    with caplog.at_level(logging.WARNING):
        plugin.register(ctx)

    _assert_base_registration(ctx.kwargs)
    assert "parse_target_ref_fn" not in ctx.kwargs
    assert "lane-qualified outbound targets require a newer Hermes build" in caplog.text


def test_register_propagates_unrelated_type_error():
    plugin = _load_plugin_package()
    ctx = _CapturingContext(TypeError("unrelated registration failure"))

    with pytest.raises(TypeError, match="unrelated registration failure"):
        plugin.register(ctx)


def _discovery_probe(tmp_path, missing_parser):
    home = tmp_path / "home"
    plugin_copy = home / "plugins" / "vk-platform"
    shutil.copytree(
        PLUGIN_ROOT,
        plugin_copy,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "*.pyc"),
    )
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - vk-platform\n",
        encoding="utf-8",
    )
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    script = textwrap.dedent(
        f"""
        import inspect
        import json
        import logging
        import sys

        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        import gateway.platform_registry as registry_module
        original = registry_module.PlatformEntry
        if {missing_parser!r}:
            legacy_signature = inspect.Signature(
                [parameter
                for name, parameter in inspect.signature(original).parameters.items()
                if name != "parse_target_ref_fn"]
            )
            def platform_entry_without_parser(*args, **kwargs):
                # Enforce the missing-field contract, not just its introspection.
                legacy_signature.bind(*args, **kwargs)
                return original(*args, **kwargs)
            platform_entry_without_parser.__signature__ = legacy_signature
            registry_module.PlatformEntry = platform_entry_without_parser

        from hermes_cli.plugins import PluginManager
        manager = PluginManager()
        manager.discover_and_load()
        state = manager._plugins["vk-platform"]
        entry = registry_module.platform_registry.get("vk")
        print(json.dumps({{
            "enabled": state.enabled,
            "error": state.error,
            "entry": entry is not None,
            "allowed_users_env": entry.allowed_users_env if entry else None,
            "allow_all_env": entry.allow_all_env if entry else None,
            "validate_config": callable(entry.validate_config) if entry else False,
            "apply_yaml_config_fn": callable(entry.apply_yaml_config_fn) if entry else False,
            "standalone_sender_fn": callable(entry.standalone_sender_fn) if entry else False,
            "parser": callable(entry.parse_target_ref_fn) if entry else False,
        }}))
        """
    )
    env = os.environ.copy()
    env.update(
        HOME=str(tmp_path / "os-home"),
        HERMES_HOME=str(home),
        HERMES_BUNDLED_PLUGINS=str(bundled),
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1]), result.stderr


@pytest.mark.parametrize("missing_parser", [False, True], ids=["modern", "reported-missing-field"])
def test_real_plugin_discovery_registration(tmp_path, missing_parser):
    result, stderr = _discovery_probe(tmp_path, missing_parser)

    assert result == {
        "enabled": True,
        "error": None,
        "entry": True,
        "allowed_users_env": "VK_ALLOWED_USERS",
        "allow_all_env": "VK_ALLOW_ALL_USERS",
        "validate_config": True,
        "apply_yaml_config_fn": True,
        "standalone_sender_fn": True,
        "parser": not missing_parser,
    }
    warning = "lane-qualified outbound targets require a newer Hermes build"
    assert (warning in stderr) is missing_parser
