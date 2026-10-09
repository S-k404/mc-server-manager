"""`mc mods config`: every mod's config file read, broken ones moved aside, Simple Voice Chat's port checked."""
import json

import pytest
from typer.testing import CliRunner

from mcctl import core, modconfig, voice
from mcctl.cli import app
from mcctl.core import Fail, BACKUPS
from test_moddoctor import fabric_jar, put


def modded(make_server, name="pytest-cfg"):
    s = make_server(name)
    put(s, "lithium.jar", fabric_jar("lithium"))
    put(s, "easyauth.jar", fabric_jar("easyauth"))
    put(s, "sodium.jar", fabric_jar("sodium"))
    return s


def write(s, rel, text=""):
    p = s.dir / "config" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def stopped(monkeypatch, *running):
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: self.name in running)


def by_file(items):
    return {i.file: i for i in items}


def test_healthy_configs_of_every_style_read_fine_and_know_their_mod(make_server):
    s = modded(make_server)
    write(s, "lithium.properties", "mixin.ai.pathing=true\n")
    write(s, "easyauth/config.json", json.dumps({"main": {"enableGlobalPassword": False}}))
    write(s, "sodium-options.json", '// written by hand\n{\n  "quality": {"weather": "FAST",},\n  /* block comment */\n  "x": 1,\n}\n')
    write(s, "neo-common.toml", '[general]\nname = "x"\n')
    write(s, "odd.json5", "{ unquoted: 'keys', }")                     # json5: never judged
    items = by_file(modconfig.check(s))
    assert not modconfig.problems(list(items.values()))
    assert items["config/lithium.properties"].mod == "lithium.jar"
    assert items["config/easyauth/config.json"].mod == "easyauth.jar"
    assert items["config/sodium-options.json"].mod == "sodium.jar"         # comments and trailing commas are accepted


def test_unreadable_and_empty_configs_are_problems_with_a_fix(make_server):
    s = modded(make_server)
    write(s, "easyauth/config.json", '{"main": {"a": 1,')                # truncated
    write(s, "broken.toml", "[general\nname = 1\n")
    write(s, "empty.json", "")
    write(s, "lithium.properties", "x=1\n")
    items = by_file(modconfig.check(s))
    bad = {k for k, v in items.items() if v.level == "error"}
    assert bad == {"config/easyauth/config.json", "config/broken.toml", "config/empty.json"}
    assert "isn't valid JSON" in items["config/easyauth/config.json"].message
    assert "isn't valid TOML" in items["config/broken.toml"].message
    assert "is empty" in items["config/empty.json"].message
    assert all(items[k].action == ("aside", k) for k in bad)
    assert modconfig.check(s)[0].level == "error"                          # problems come first


def test_fix_moves_them_aside_and_deletes_nothing(make_server, monkeypatch):
    s = modded(make_server)
    stopped(monkeypatch)
    bad = write(s, "easyauth/config.json", '{"main": {"a": 1,')
    write(s, "lithium.properties", "x=1\n")
    said = []
    done = modconfig.fix(s, say=said.append)
    assert len(done) == 1 and "the mod writes a fresh one" in done[0] and said == done
    assert not bad.exists() and (s.dir / "config" / "lithium.properties").exists()   # only the broken one moved
    kept = list((BACKUPS / s.name / "config-removed").iterdir())
    assert len(kept) == 1 and kept[0].name.endswith("easyauth__config.json") and kept[0].read_text() == '{"main": {"a": 1,'
    assert modconfig.fix(s) == [] and not modconfig.problems(modconfig.check(s))


def test_a_running_server_is_never_touched(make_server, monkeypatch):
    s = modded(make_server)
    stopped(monkeypatch, s.name)
    bad = write(s, "easyauth/config.json", "{")
    with pytest.raises(Fail, match="Stop it first"):
        modconfig.fix(s)
    assert bad.exists()


def test_a_server_with_nothing_to_fix_can_be_checked_while_running(make_server, monkeypatch):
    s = modded(make_server)
    stopped(monkeypatch, s.name)
    write(s, "lithium.properties", "x=1\n")
    assert modconfig.fix(s) == []                                           # no error, no stop needed


def test_big_and_hidden_and_backup_files_are_ignored(make_server):
    s = modded(make_server)
    write(s, ".hidden.json", "{")
    write(s, "old.json.bak", "{")
    write(s, "huge.json", "{" + " " * 2_100_000)
    write(s, "a/b/c/d/deep.json", "{")                                      # four folders down: not a mod's own config
    assert modconfig.check(s) == [] or not modconfig.problems(modconfig.check(s))


# ---------- Simple Voice Chat ----------
def voice_pair(make_server, monkeypatch):
    a = modded(make_server, "alpha")
    b = modded(make_server, "beta")
    for srv, port in ((a, 25565), (b, 25566)):
        put(srv, "voicechat.jar", fabric_jar("voicechat", "2.5"))
        write(srv, "voicechat/voicechat-server.properties", "port=24454\nbind_address=\n")
    return a, b


def test_a_voice_port_another_running_server_holds_is_a_problem_the_fix_repairs(make_server, monkeypatch):
    a, b = voice_pair(make_server, monkeypatch)
    stopped(monkeypatch, "alpha")
    monkeypatch.setattr(voice, "udp_free", lambda port, bind="": True)
    items = modconfig.check(b)
    clash = next(i for i in items if i.action == ("voiceport",))
    assert "also used by the running server 'alpha'" in clash.message
    done = modconfig.fix(b, items)
    assert done and "now 24455" in done[0] and voice.udp_port(b) == 24455
    assert not [i for i in modconfig.check(b) if i.action == ("voiceport",)]


def test_an_out_of_range_voice_port_and_a_port_held_by_a_program_are_found(make_server, monkeypatch):
    a, b = voice_pair(make_server, monkeypatch)
    stopped(monkeypatch)
    write(b, "voicechat/voicechat-server.properties", "port=80\n")
    monkeypatch.setattr(voice, "udp_free", lambda port, bind="": True)
    assert any("outside 1024-65535" in i.message for i in modconfig.check(b))
    assert "outside 1024-65535" in modconfig.fix(b)[0]
    write(a, "voicechat/voicechat-server.properties", "port=24454\n")
    monkeypatch.setattr(voice, "udp_free", lambda port, bind="": port != 24454)
    assert any("held by another program" in i.message for i in modconfig.check(a))


def test_voice_chat_before_its_first_start_is_only_a_note(make_server, monkeypatch):
    s = modded(make_server)
    put(s, "voicechat.jar", fabric_jar("voicechat", "2.5"))
    stopped(monkeypatch)
    note = next(i for i in modconfig.check(s) if "hasn't written its config" in i.message)
    assert note.level == "note" and not modconfig.problems(modconfig.check(s))


# ---------- the command and `mc fix` ----------
def test_cli_mods_config_lists_exits_1_on_problems_and_fix_repairs(make_server, monkeypatch):
    s = modded(make_server)
    stopped(monkeypatch)
    write(s, "lithium.properties", "x=1\n")
    write(s, "easyauth/config.json", "{")
    out = CliRunner().invoke(app, ["-s", s.name, "mods", "config"])
    assert out.exit_code == 1 and "[ok] config/lithium.properties reads fine." in out.output
    assert "[x] config/easyauth/config.json isn't valid JSON" in out.output and "mods config --fix" in out.output
    fixed = CliRunner().invoke(app, ["-s", s.name, "mods", "config", "--fix"])
    assert fixed.exit_code == 0 and "moved config/easyauth/config.json aside" in fixed.output
    assert CliRunner().invoke(app, ["-s", s.name, "mods", "config"]).exit_code == 0


def test_mc_fix_repairs_configs_before_it_starts_a_server_and_only_reports_for_a_running_one(make_server, monkeypatch):
    from mcctl import repair, servers, vpn
    s = modded(make_server)
    write(s, "easyauth/config.json", "{")
    monkeypatch.setattr(vpn, "enabled", lambda: False)
    order = []
    monkeypatch.setattr(servers, "start", lambda srv: order.append("start"))
    monkeypatch.setattr(modconfig, "fix", lambda srv, items=None, say=print: order.append("fix") or ["moved it aside"])
    stopped(monkeypatch)
    steps = repair.run(say=lambda m: None)
    assert order[:2] == ["fix", "start"] and any(st.what.startswith("Mod config on") and st.ok for st in steps)
    stopped(monkeypatch, s.name)
    order.clear()
    steps = repair.run(say=lambda m: None)
    note = next(st for st in steps if "mod config problem" in st.what)
    assert note.ok is None and "stop, then" in note.fix and "fix" not in order
