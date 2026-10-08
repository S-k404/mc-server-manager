import json

import pytest

from mcctl import config, core, players, servers, settings_schema
from mcctl.cli import pop_server_flag


def test_offline_uuid_matches_java():
    # Known value of UUID.nameUUIDFromBytes("OfflinePlayer:Notch")
    assert players.offline_uuid("Notch") == "b50ad385-829d-3141-a216-7e7d7539ba7f"


def test_config_migrates_v1_and_keeps_unknown_keys(make_server):
    s = make_server(autobackup=True, custom_thing=5)
    inst = s.inst
    assert inst.schema == config.SCHEMA and inst.desired == "stopped" and inst.autobackup
    saved = json.loads(s.cfg_path.read_text())
    assert saved["schema"] == config.SCHEMA and saved["custom_thing"] == 5


def test_config_rejects_bad_values(make_server):
    s = make_server()
    with pytest.raises(config.ConfigError):
        s.update(loader="bukkit")


def test_properties_edit(make_server):
    s = make_server()
    s.set_prop("view-distance", 6)
    s.set_prop("new-key", "x=y")
    assert s.prop("view-distance") == "6" and s.prop("new-key") == "x=y"
    assert s.port == 25565


def test_state_crashed_vs_stopped(make_server):
    s = make_server()
    assert s.state() == "stopped"
    s.state_dir.mkdir()
    (s.state_dir / "exit").write_text("137")
    s.update(desired="running")
    assert s.state() == "crashed"
    (s.state_dir / "exit").write_text("0")
    assert s.state() == "stopped"


def test_used_and_free_ports(make_server):
    make_server("pytest-a", port=30000, rcon_port=30010)
    assert {30000, 30010} <= core.used_ports()
    assert core.free_port(30000) not in (30000, 30010)


def test_default_server_resolution(make_server):
    make_server("pytest-a")
    assert core.default_server_name() == "pytest-a"
    make_server("pytest-b", port=25566)
    assert core.default_server_name() is None
    core.save_setting("default", "pytest-b")
    assert core.default_server_name() == "pytest-b"


def test_pop_server_flag_anywhere():
    assert pop_server_flag(["start", "-s", "x"]) == ["-s", "x", "start"]
    assert pop_server_flag(["--server=y", "mods", "add", "z"]) == ["-s", "y", "mods", "add", "z"]
    assert pop_server_flag(["cmd", "say", "hi"]) == ["cmd", "say", "hi"]


def test_settings_validation():
    s = settings_schema.find("difficulty")
    assert s.normalize("HARD") == "hard"
    with pytest.raises(core.Fail):
        s.normalize("extreme")
    vd = settings_schema.find("view-distance")
    with pytest.raises(core.Fail):
        vd.normalize("99")
    assert settings_schema.find("keep_inventory").normalize("on") == "true"


def test_settings_apply_offline(make_server):
    s = make_server()
    assert settings_schema.apply(s, "view-distance", "8") == "saved"
    assert s.prop("view-distance") == "8"
    assert settings_schema.apply(s, "ram_gb", "6") == "saved" and s.inst.ram_gb == 6
    with pytest.raises(core.Fail):
        settings_schema.apply(s, "no-such-setting", "1")


def test_pvp_resolves_to_gamerule_when_property_missing(make_server):
    s = make_server()  # no pvp property, like 26.x
    assert settings_schema.find("pvp", s) in settings_schema.GAMERULES
    s.set_prop("pvp", "true")
    assert settings_schema.find("pvp", s) in settings_schema.PROPS


def test_port_clash_rejected(make_server):
    make_server("pytest-a", port=25565)
    b = make_server("pytest-b", port=25566)
    with pytest.raises(core.Fail):
        settings_schema.apply(b, "server-port", "25565")


def test_crash_summary_names_suspect_mod(make_server):
    s = make_server()
    s.state_dir.mkdir()
    (s.state_dir / "started").write_text("0")
    (s.state_dir / "exit").write_text("1")
    (s.dir / "crash-reports").mkdir()
    (s.dir / "crash-reports" / "crash-1.txt").write_text(
        "Description: Mixin apply failed\n"
        "Mixin [coolmod.mixins.json:SomeMixin] from mod coolmod failed\n"
        "at net.minecraft.server.Main\n")
    c = servers.crash_summary(s)
    assert c["headline"].startswith("Mixin apply failed")
    assert "coolmod" in c["suspects"] and "minecraft" not in c["suspects"]


def test_crash_summary_explains_kill(make_server):
    s = make_server()
    s.state_dir.mkdir()
    (s.state_dir / "exit").write_text("137")
    assert "memory" in servers.crash_summary(s)["headline"]


def test_player_list_parse_stays_on_its_own_line():
    # A console-fallback reply is several log lines; an empty list used to pick up the next line as a "player".
    empty = "There are 0 of a max of 20 players online: \nThread RCON Client /127.0.0.1 started"
    assert core.parse_player_list(empty) == (20, [])
    assert core.parse_player_list("There are 2 of a max of 20 players online: Alex, Steve\nnoise") == (20, ["Alex", "Steve"])
    assert core.parse_player_list("There are 1 of a max of 8 players online:\n") == (8, [])
    assert core.parse_player_list("Unknown command") is None


def test_online_ignores_trailing_log_lines(make_server, monkeypatch):
    s = make_server()
    monkeypatch.setattr(core.Server, "accepting", lambda self: True)
    monkeypatch.setattr(core.Server, "run", lambda self, cmd, wait=1.5: "There are 0 of a max of 20 players online: \nstarted")
    assert players.online(s) == []
