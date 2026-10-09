"""Per-player details, ban list, private messages and whitelist-online."""
import json

import pytest

from mcctl import players
from mcctl.core import Fail, Server

UUID = "11111111-2222-3333-4444-555555555555"


def seed(s):
    (s.dir / "usercache.json").write_text(json.dumps([{"name": "Steve", "uuid": UUID}, {"name": "Alex", "uuid": "a-b"}]))
    (s.dir / "ops.json").write_text(json.dumps([{"name": "Steve"}]))
    (s.dir / "whitelist.json").write_text(json.dumps([{"name": "Steve"}, {"name": "Alex"}]))
    (s.dir / "banned-players.json").write_text(json.dumps(
        [{"name": "Griefer", "reason": "griefing", "created": "2026-10-01", "source": "Server"}]))
    stats = s.dir / "world" / "stats"
    stats.mkdir(parents=True)
    (stats / f"{UUID}.json").write_text(json.dumps({"stats": {"minecraft:custom": {
        "minecraft:play_time": 20 * 3600 * 5, "minecraft:deaths": 3, "minecraft:mob_kills": 40,
        "minecraft:walk_one_cm": 250000, "minecraft:jump": 1234}}}))
    adv = s.dir / "world" / "advancements"
    adv.mkdir()
    (adv / f"{UUID}.json").write_text(json.dumps({"minecraft:story/root": {"done": True},
                                                  "minecraft:recipes/x": {"done": True},
                                                  "minecraft:story/other": {"done": False}, "DataVersion": 1}))
    s.log_path.parent.mkdir(parents=True, exist_ok=True)
    s.log_path.write_text("[11:00:00] [Server thread/INFO]: Steve left the game\n[12:30:00] [Server thread/INFO]: Steve left the game\n")


def test_details_from_stats_files_without_the_server_running(make_server):
    s = make_server()
    seed(s)
    d = players.details(s, "Steve")
    assert d["uuid"] == UUID and d["op"] and d["whitelisted"] and not d["online"] and d["ban"] is None
    assert d["stats"]["deaths"] == 3 and d["stats"]["playtime"] == 360000 and d["stats"]["advancements"] == 1
    assert d["last_seen"] == "12:30:00"
    text = players.format_details(d)
    assert "5h 0m" in text and "Deaths        3" in text and "operator, whitelisted" in text and "12:30:00" in text


def test_details_for_banned_and_unknown_players(make_server):
    s = make_server()
    seed(s)
    g = players.details(s, "Griefer")
    assert g["ban"]["reason"] == "griefing" and "BANNED" in players.format_details(g)
    assert players.details(s, "Alex")["stats"]["deaths"] == 0                       # known, no stats file yet
    ghost = players.details(s, "Nobody")                                            # offline-mode server: UUID is derived
    assert ghost["uuid"] == players.offline_uuid("Nobody") and not ghost["online"]
    online_only = make_server("paid", props={"online-mode": "true"}, port=25570)
    assert players.find_uuid(online_only, "Nobody") is None                         # no network call, just unknown
    with pytest.raises(Fail):
        players.details(s, "bad name; stop")


def test_bans_tell_and_whitelist_online(make_server, monkeypatch):
    s = make_server()
    seed(s)
    assert players.banned(s)[0]["name"] == "Griefer"
    sent = []
    monkeypatch.setattr(Server, "require_up", lambda self: None)
    monkeypatch.setattr(Server, "run_and_show", lambda self, cmd, wait=1.5: sent.append(cmd))
    players.tell(s, "Steve", "hello\nstop")
    assert sent == ["tell Steve hello stop"]                                        # control characters can't smuggle a command
    with pytest.raises(Fail):
        players.tell(s, "Steve", "   ")
    with pytest.raises(Fail):
        players.tell(s, "@a", "hi")
    monkeypatch.setattr(players, "online", lambda srv: ["Steve", "Newbie"])
    added = []
    monkeypatch.setattr(players, "whitelist_add", lambda srv, names: added.append(names))
    players.whitelist_online(s)
    assert added == [["Steve", "Newbie"]]
