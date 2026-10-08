"""Point mcctl at a throwaway MC_HOME before it is imported, and give tests fake servers."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="mc-test-home-"))
os.environ["MC_HOME"] = str(HOME)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from mcctl import core  # noqa: E402


@pytest.fixture(autouse=True)
def clean_home():
    from mcctl import platform
    platform._ttl_cache.clear()              # lan_ip / vpn_active are cached for a few seconds: never carry one test's answer into the next
    for sub in ("servers", "backups", "tunnel"):
        shutil.rmtree(HOME / sub, ignore_errors=True)
        (HOME / sub).mkdir()
    for f in (".mc-settings.json", ".mc-events.jsonl", ".mc-metrics.jsonl", ".mc-voice.json"):
        (HOME / f).unlink(missing_ok=True)
    yield


@pytest.fixture
def make_server():
    def make(name="pytest-a", props=None, **inst):
        d = HOME / "servers" / name
        d.mkdir(parents=True)
        data = {"name": name, "mc_version": "26.3", "loader": "fabric", "java_major": 25, "port": 25565}
        data.update(inst)
        (d / "instance.json").write_text(json.dumps(data))
        base = {"server-port": data["port"], "difficulty": "easy", "view-distance": "10", "white-list": "true",
                "online-mode": "false"}
        base.update(props or {})
        (d / "server.properties").write_text("".join(f"{k}={v}\n" for k, v in base.items()))
        return core.Server(name)
    return make
