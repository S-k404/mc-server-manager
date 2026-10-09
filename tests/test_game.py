"""Game features, input validation, cracked mode, flight, and the ProtonVPN LAN check."""
import json
import subprocess

import pytest

from mcctl import game, mods, platform, players
from mcctl.core import Fail


class FakeRun:
    """Stand-in for Server.run: records console commands and returns canned replies."""
    def __init__(self, s, monkeypatch, replies=None):
        self.sent, self.replies = [], replies or {}
        monkeypatch.setattr(type(s), "accepting", lambda self_: True)
        monkeypatch.setattr(type(s), "up", lambda self_: True)
        monkeypatch.setattr(type(s), "run", lambda self_, command, wait=1.5: self.record(command))

    def record(self, command):
        self.sent.append(command)
        return next((v for k, v in self.replies.items() if command.startswith(k)), "ok")


@pytest.fixture
def srv(make_server, monkeypatch):
    s = make_server()
    return s, FakeRun(s, monkeypatch)


# ---------- validation: nothing odd reaches the console ----------
@pytest.mark.parametrize("bad", ["Steve; stop", "a b", "@a", "x\nstop", "", "waytoolongplayername1", "../x"])
def test_player_names_are_validated(srv, bad):
    s, fake = srv
    for call in (lambda: game.gamemode(s, "creative", bad), lambda: game.give(s, bad, "diamond"),
                 lambda: game.teleport(s, bad, ("Steve",)), lambda: players.kick(s, bad),
                 lambda: players.ban(s, bad), lambda: players.op(s, [bad])):
        with pytest.raises(Fail):
            call()
    assert fake.sent == []


@pytest.mark.parametrize("bad", ["dia mond", "diamond;stop", "DIAMOND SWORD", "x\ny", ""])
def test_item_ids_are_validated(srv, bad):
    s, fake = srv
    with pytest.raises(Fail):
        game.give(s, "Steve", bad)
    assert fake.sent == []


def test_numbers_and_enums_are_bounded(srv):
    s, fake = srv
    for call in (lambda: game.set_time(s, "99999"), lambda: game.set_time(s, "-1"), lambda: game.set_weather(s, "hail"),
                 lambda: game.gamemode(s, "god", "Steve"), lambda: game.give(s, "Steve", "diamond", 0),
                 lambda: game.give(s, "Steve", "diamond", 7000), lambda: game.border(s, 0),
                 lambda: game.teleport(s, "Steve", ("1", "2")), lambda: game.teleport(s, "Steve", ("a", "b", "c")),
                 lambda: game.set_spawn(s, "1", "x", "3")):
        with pytest.raises(Fail):
            call()
    assert fake.sent == []


def test_free_text_cannot_inject_a_second_command(srv):
    s, fake = srv
    players.say(s, "hello\nstop")
    players.kick(s, "Steve", "bye\r\nop Eve")
    assert fake.sent == ["say hello stop", "kick Steve bye  op Eve"]       # newlines gone: still one command each


# ---------- commands sent ----------
def test_commands_use_real_console_syntax(srv):
    s, fake = srv
    game.set_time(s, "night")
    game.set_time(s, "6000")
    game.set_weather(s, "rain", 5)
    game.gamemode(s, "creative", "Steve")
    game.teleport(s, "Steve", ("Alex",))
    game.teleport(s, "Steve", ("100", "64", "-200"))
    game.give(s, "Steve", "minecraft:diamond_sword", 2)
    game.border(s, 500, seconds=30, center=("0", "0"))
    game.set_spawn(s, 0, 70, 0)
    assert fake.sent == ["time set night", "time set 6000", "weather rain 300", "gamemode creative Steve",
                         "tp Steve Alex", "tp Steve 100.0 64.0 -200.0", "give Steve minecraft:diamond_sword 2",
                         "worldborder center 0.0 0.0", "worldborder set 500 30", "setworldspawn 0 70 0"]


def test_where_parses_position_and_dimension(srv, monkeypatch):
    s, _ = srv
    FakeRun(s, monkeypatch, {"data get entity Steve Pos": "Steve has the following entity data: [123.5d, 64.0d, -12.25d]",
                             "data get entity Steve Dimension": 'Steve has the following entity data: "minecraft:the_nether"',
                             "data get entity Ghost": "No entity was found"})
    assert game.where(s, "Steve") == {"x": 123.5, "y": 64.0, "z": -12.25, "dimension": "the_nether"}
    assert game.where(s, "Ghost") is None


# ---------- stats and feed ----------
def test_leaderboard_reads_stats_files_even_when_stopped(make_server):
    s = make_server()
    stats = s.dir / "world" / "stats"
    stats.mkdir(parents=True)
    (s.dir / "usercache.json").write_text(json.dumps([{"name": "Steve", "uuid": "u-1"}, {"name": "Alex", "uuid": "u-2"}]))
    (stats / "u-1.json").write_text(json.dumps({"stats": {"minecraft:custom": {"minecraft:play_time": 72000, "minecraft:deaths": 3}}}))
    (stats / "u-2.json").write_text(json.dumps({"stats": {"minecraft:custom": {"minecraft:play_time": 144000}}}))
    (stats / "broken.json").write_text("{nope")
    assert game.leaderboard(s, "playtime") == [("Alex", 144000), ("Steve", 72000)]
    assert game.leaderboard(s, "deaths")[0] == ("Steve", 3)
    assert game.format_stat("playtime", 144000) == "2h 0m"
    with pytest.raises(Fail):
        game.leaderboard(s, "nonsense")


def test_feed_classifies_log_lines(make_server):
    s = make_server()
    (s.dir / "usercache.json").write_text(json.dumps([{"name": "Steve", "uuid": "u"}]))
    (s.dir / "logs").mkdir()
    s.log_path.write_text("\n".join([
        "[10:00:00] [Server thread/INFO]: Steve joined the game",
        "[10:00:05] [Server thread/INFO]: <Steve> hello",
        "[10:00:06] [Server thread/INFO]: [Not Secure] <Steve> secure chat",
        "[10:01:00] [Server thread/INFO]: Steve has made the advancement [Stone Age]",
        "[10:02:00] [Server thread/INFO]: Steve was slain by Zombie",
        "[10:02:30] [Server thread/INFO]: Steve lost connection: Disconnected",
        "[10:03:00] [Server thread/INFO]: Steve left the game",
        "[10:03:01] [Server thread/INFO]: Starting minecraft server",
    ]))
    assert [(e[1], e[2]) for e in game.feed(s)] == [
        ("join", "Steve joined the game"), ("chat", "<Steve> hello"), ("chat", "<Steve> secure chat"),
        ("advancement", "Steve has made the advancement [Stone Age]"), ("death", "Steve was slain by Zombie"),
        ("leave", "Steve left the game")]


def test_tail_text_starts_on_a_line_boundary(tmp_path):
    f = tmp_path / "big.log"
    f.write_text("".join(f"line {i}\n" for i in range(5000)))
    tail = game.tail_text(f, 200)
    assert tail.startswith("line ") and tail.endswith("line 4999\n") and len(tail) <= 200
    small = tmp_path / "small.log"
    small.write_text("a\nb\n")
    assert game.tail_text(small) == "a\nb\n"


# ---------- cracked accounts ----------
def test_cracked_on_installs_login_mod_and_sets_properties(make_server, monkeypatch):
    s = make_server(props={"online-mode": "true", "enforce-secure-profile": "true"})
    added = []
    monkeypatch.setattr(mods, "add", lambda srv, names: added.extend(names))
    players.set_cracked(s, True)
    assert added == ["easyauth", "skinrestorer"]
    assert s.prop("online-mode") == "false" and s.prop("enforce-secure-profile") == "false"
    assert players.cracked_allowed(s)


def test_cracked_on_refuses_vanilla_without_explicit_override(make_server, monkeypatch):
    s = make_server(loader="vanilla", props={"online-mode": "true"})
    monkeypatch.setattr(mods, "add", lambda *a: pytest.fail("vanilla can't run mods"))
    with pytest.raises(Fail, match="--insecure"):
        players.set_cracked(s, True)
    assert s.prop("online-mode") == "true"                                     # nothing changed
    players.set_cracked(s, True, insecure=True)
    assert s.prop("online-mode") == "false"


def test_cracked_off_restores_paid_only(make_server):
    s = make_server(props={"online-mode": "false", "enforce-secure-profile": "false"})
    players.set_cracked(s, False)
    assert s.prop("online-mode") == "true" and s.prop("enforce-secure-profile") == "true"


# ---------- flight ----------
def test_flight_toggle(make_server):
    s = make_server(props={"allow-flight": "false"})
    game.set_flight(s, True)
    assert s.prop("allow-flight") == "true"
    game.set_flight(s, False)
    assert s.prop("allow-flight") == "false"


# ---------- ProtonVPN LAN check (read-only) ----------
def fake_defaults(monkeypatch, plist_bytes):
    monkeypatch.setattr(platform, "IS_MAC", True)
    monkeypatch.setattr(platform.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, plist_bytes, b""))


@pytest.mark.parametrize("value,expected", [(b'"off"', False), (b'"on"', True), (b"true", True), (b'"false"', False)])
def test_proton_lan_setting_is_parsed(monkeypatch, value, expected):
    import plistlib
    fake_defaults(monkeypatch, plistlib.dumps({"excludeLocalNetworksabc123": value, "other": 1}))
    assert platform.proton_allows_lan() is expected


def test_proton_lan_setting_unknown_when_absent_or_unreadable(monkeypatch):
    import plistlib
    fake_defaults(monkeypatch, plistlib.dumps({"other": 1}))
    assert platform.proton_allows_lan() is None
    fake_defaults(monkeypatch, b"not a plist")
    assert platform.proton_allows_lan() is None


def test_mc_never_writes_protons_settings(monkeypatch):
    """The LAN check only ever runs `defaults export` (read-only)."""
    import plistlib
    calls = []
    monkeypatch.setattr(platform, "IS_MAC", True)
    monkeypatch.setattr(platform.subprocess, "run",
                        lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, plistlib.dumps({}), b""))
    platform.proton_allows_lan()
    assert calls == [["defaults", "export", "ch.protonvpn.mac", "-"]]
