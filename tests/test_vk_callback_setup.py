"""Operator-facing callback setup contract; no live VK access."""
import importlib.util
from pathlib import Path


def test_setup_names_callback_event_and_its_purpose(capsys):
    path = Path(__file__).resolve().parents[1] / 'setup_helper.py'
    spec = importlib.util.spec_from_file_location('vk_callback_setup', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._print_vk_prerequisites()
    output = capsys.readouterr().out
    assert 'message_new' in output
    assert 'message_edit' in output
    assert 'message_event' in output
    assert 'callback' in output.lower()
