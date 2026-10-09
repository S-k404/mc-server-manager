import json
import zipfile

import pytest

from mcctl import core, upgrade


@pytest.mark.parametrize("bad", ["../evil.jar", "/etc/passwd", "mods/../../x"])
def test_modpack_paths_cannot_escape(tmp_path, bad):
    with pytest.raises(core.Fail):
        upgrade._safe_path(tmp_path, bad)


def test_modpack_path_ok(tmp_path):
    assert upgrade._safe_path(tmp_path, "mods/a.jar") == tmp_path / "mods" / "a.jar"


def test_read_index(tmp_path):
    pack = tmp_path / "p.mrpack"
    with zipfile.ZipFile(pack, "w") as z:
        z.writestr("modrinth.index.json", json.dumps({"dependencies": {"minecraft": "26.3"}}))
    assert upgrade.read_index(pack)["dependencies"]["minecraft"] == "26.3"
    bad = tmp_path / "bad.mrpack"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("other.txt", "x")
    with pytest.raises(core.Fail):
        upgrade.read_index(bad)


def test_upgrade_refuses_missing_mods_and_downgrades(make_server):
    s = make_server()
    p = upgrade.UpgradePlan(s, "26.4", 25, "0.19.5", rows=[(type("E", (), {"title": "Old Mod"})(), None)])
    with pytest.raises(core.Fail, match="Old Mod"):
        upgrade.apply(p)
    p2 = upgrade.UpgradePlan(s, "26.1", 25, None, downgrade=True)
    with pytest.raises(core.Fail, match="older version"):
        upgrade.apply(p2)


# ---------- agent ----------
from mcctl import agent  # noqa: E402


def test_schedule_parsing():
    assert agent.parse_every("6h") == 21600 and agent.parse_every("1d") == 86400
    assert agent.parse_at("5:00") == "05:00"
    with pytest.raises(core.Fail):
        agent.parse_every("5m")      # too frequent
    with pytest.raises(core.Fail):
        agent.parse_at("25:00")


def test_agent_respects_ingame_stop(make_server, monkeypatch):
    s = make_server()
    s.state_dir.mkdir()
    (s.state_dir / "exit").write_text("0")
    s.update(desired="running")
    started = []
    monkeypatch.setattr(agent.servers, "start", lambda srv, **k: started.append(srv.name))
    agent.tick({})
    assert s.inst.desired == "stopped" and not started


def test_agent_keep_alive_restarts_then_gives_up(make_server, monkeypatch):
    s = make_server(keep_alive=True)
    s.state_dir.mkdir()
    (s.state_dir / "exit").write_text("1")
    s.update(desired="running")
    started = []
    monkeypatch.setattr(agent.servers, "start", lambda srv, **k: started.append(srv.name))
    monkeypatch.setattr(agent, "notify", lambda *a: None)
    state, now = {}, 1_000_000.0
    for i in range(6):
        (s.state_dir / "started").write_text(str(now + i))   # each restart = a new run that crashes again
        state = agent.tick(state, now=now + i * 60)
    assert len(started) == agent.MAX_CRASHES                  # then it stops trying
    assert state["servers"][s.name]["gave_up"]


def test_agent_restarts_after_reboot_only_with_keep_alive(make_server, monkeypatch):
    s = make_server(keep_alive=True)
    s.update(desired="running")                                # no exit file: Mac lost power
    started = []
    monkeypatch.setattr(agent.servers, "start", lambda srv, **k: started.append(srv.name))
    agent.tick({}, first=True)
    assert started == [s.name]
