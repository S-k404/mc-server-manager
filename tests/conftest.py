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

# The RAM the machine has decides which sizes the new-server dialog offers and what `mc set ram_gb` accepts (total - 4 GB).
# Pin it before anything else imports mcctl, so a small CI runner and a big laptop run the same tests.
from mcctl import platform as _platform  # noqa: E402

_platform.total_ram_gb = lambda: 32

from mcctl import core  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch):
    """Tests never talk to a real tmux (the machine's own sessions must not leak in, and a runner may have none). With no tmux mc sees no sessions."""
    monkeypatch.setattr(core, "TMUX", "/nonexistent/tmux")


@pytest.fixture(autouse=True)
def no_real_docker(monkeypatch):
    """Tests never run the real docker (a machine with Docker running would pull images and touch its containers). A test that needs a
    docker answer patches `container.docker`/`require`/... itself; one that forgot gets "Docker isn't installed" on every machine."""
    from mcctl import container
    monkeypatch.setattr(container, "docker_path", lambda: None)


@pytest.fixture(autouse=True)
def no_desktop_notifications(monkeypatch):
    """A test never shows a real notification: on a Mac that is osascript, which pops up on a laptop and can hang on a CI runner with no desktop."""
    from mcctl import platform
    monkeypatch.setattr(platform, "notify", lambda title, message: None)


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
