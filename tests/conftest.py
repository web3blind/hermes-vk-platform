"""Initialize isolated homes before any plugin/host imports (env-i runner safe)."""
import os
import shutil
import tempfile
from pathlib import Path

_SCRATCH = Path(os.environ.get("TMPDIR") or Path.home() / ".hermes/cache/scratch")
_SCRATCH.mkdir(parents=True, exist_ok=True)
_ROOT = Path(tempfile.mkdtemp(prefix="vk-public-tests-", dir=_SCRATCH))
for key, name in (("HOME", "user"), ("HERMES_HOME", "hermes"),
                  ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                  ("XDG_DATA_HOME", "data"), ("TMPDIR", "tmp")):
    target = _ROOT / name
    target.mkdir(parents=True)
    os.environ[key] = str(target)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_ROOT, ignore_errors=True)
