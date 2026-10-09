"""Start-up speed: the bare `mc` fast path, the quick directory walk, one tmux question for a burst of calls, a world measured on a
side thread, and the dashboard building its tabs after the first paint."""
import asyncio
import os
import runpy
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from textual.widgets import TabbedContent

from mcctl import cli, core, errlog, settings_schema
from mcctl.resources import Sampler
from mcctl.tui import app as tui
from mcctl.tui.more_panes import net_segments
from test_tui import ready


# ---------- the bare `mc` goes straight to the dashboard ----------
def run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.delenv("_MC_COMPLETE", raising=False)
    calls = []
    monkeypatch.setattr(tui, "run", lambda target=None: calls.append(("dashboard", target)))
    monkeypatch.setattr(cli, "run", lambda argv=None: calls.append(("cli", argv)))
    runpy.run_module("mcctl", run_name="__main__")
    return calls


def test_a_bare_mc_opens_the_dashboard_without_the_command_tree(monkeypatch):
    assert run_main(monkeypatch, ["mc"]) == [("dashboard", None)]


def test_anything_else_goes_through_the_command_line(monkeypatch):
    assert run_main(monkeypatch, ["mc", "ls"]) == [("cli", None)]
    assert run_main(monkeypatch, ["mc", "-s", "main"]) == [("cli", None)]          # a server flag alone is still the CLI's business


def test_shell_completion_never_takes_the_fast_path(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mc"])
    monkeypatch.setenv("_MC_COMPLETE", "complete_zsh")
    calls = []
    monkeypatch.setattr(tui, "run", lambda target=None: calls.append("dashboard"))
    monkeypatch.setattr(cli, "run", lambda argv=None: calls.append("cli"))
    runpy.run_module("mcctl", run_name="__main__")
    assert calls == ["cli"]


def test_an_error_in_the_dashboard_is_reported_like_any_command_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mc"])
    monkeypatch.delenv("_MC_COMPLETE", raising=False)

    def boom(target=None):
        raise core.Fail("No servers yet.")
    monkeypatch.setattr(tui, "run", boom)
    with pytest.raises(SystemExit) as e:
        runpy.run_module("mcctl", run_name="__main__")
    assert e.value.code == 1 and "No servers yet." in capsys.readouterr().out


@pytest.mark.parametrize("error, code", [(core.Fail("x"), 1), (KeyboardInterrupt(), 130), (BrokenPipeError(), 1), (ValueError("bug"), 1)])
def test_handle_error_always_exits_with_the_right_code(error, code, monkeypatch):
    monkeypatch.delenv("MC_DEBUG", raising=False)
    with pytest.raises(SystemExit) as e:
        cli.handle_error(error, ["start"])
    assert e.value.code == code


def test_a_bug_is_logged_and_shown_as_one_short_line(monkeypatch, capsys):
    monkeypatch.delenv("MC_DEBUG", raising=False)
    seen = []
    monkeypatch.setattr(errlog, "record", lambda e, argv: seen.append((type(e).__name__, argv)) or core.ROOT / "logs" / "mc-errors.log")
    with pytest.raises(SystemExit):
        cli.handle_error(ValueError("bug"), ["start"])
    out = capsys.readouterr().out
    assert seen == [("ValueError", ["start"])] and "Something went wrong: ValueError: bug" in out and "MC_DEBUG=1" in out
    monkeypatch.setenv("MC_DEBUG", "1")
    with pytest.raises(ValueError):
        cli.handle_error(ValueError("bug"), [])


# ---------- the data side ----------
def test_dir_size_adds_every_file_and_ignores_symlinks_and_unreadable_folders(tmp_path):
    (tmp_path / "region").mkdir()
    (tmp_path / "region" / "a.mca").write_bytes(b"x" * 1000)
    (tmp_path / "region" / "deep").mkdir()
    (tmp_path / "region" / "deep" / "b.dat").write_bytes(b"y" * 234)
    (tmp_path / "level.dat").write_bytes(b"z" * 66)
    os.symlink(tmp_path / "region", tmp_path / "link")                  # a loop of links must not be walked twice
    assert core.dir_size(tmp_path) == 1300
    assert core.dir_size(tmp_path / "missing") == 0


def test_one_tmux_question_serves_a_burst_of_up_calls(make_server, monkeypatch):
    s = make_server("burst")
    asked = []

    def fake_run(cmd, **kw):
        asked.append(cmd[1])
        return subprocess.CompletedProcess(cmd, 0, "mc-burst\nmc-other\n", "")
    monkeypatch.setattr(core.subprocess, "run", fake_run)
    monkeypatch.setattr(core, "_SESSIONS", (0.0, frozenset()))
    assert all(s.up() for _ in range(30))
    assert asked == ["list-sessions"]                                    # not thirty has-session calls


def test_starting_or_ending_a_session_makes_the_next_question_ask_tmux_again(make_server, monkeypatch):
    s = make_server("burst2")
    asked = []
    monkeypatch.setattr(core.subprocess, "run", lambda cmd, **kw: asked.append(cmd[1]) or subprocess.CompletedProcess(cmd, 0, "mc-burst2\n", ""))
    monkeypatch.setattr(core, "_SESSIONS", (0.0, frozenset()))
    s.up()
    core.tmux("kill-session", "-t", "=mc-burst2")
    s.up()
    assert asked == ["list-sessions", "kill-session", "list-sessions"]


def test_the_session_answer_expires(make_server, monkeypatch):
    s = make_server("burst3")
    asked = []
    monkeypatch.setattr(core.subprocess, "run", lambda cmd, **kw: asked.append(cmd[1]) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(core, "_SESSIONS", (0.0, frozenset()))
    s.up()
    monkeypatch.setattr(core, "_SESSIONS", (time.monotonic() - core.SESSIONS_TTL - 0.1, frozenset()))
    s.up()
    assert asked == ["list-sessions", "list-sessions"]


def test_a_server_that_is_up_is_up_and_a_stopped_one_is_not(make_server, monkeypatch):
    a, b = make_server("on"), make_server("off")
    monkeypatch.setattr(core.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "mc-on\n", ""))
    monkeypatch.setattr(core, "_SESSIONS", (0.0, frozenset()))
    assert a.up() and not b.up() and not a.up(sessions=set()) and b.up(sessions={"mc-off"})


def test_the_dashboards_sampler_does_not_wait_for_the_world_to_be_measured(make_server):
    s = make_server("big")
    region = s.dir / "world" / "region"
    region.mkdir(parents=True)
    for i in range(20):
        (region / f"r.{i}.0.mca").write_bytes(b"0" * 50_000)
    quick = Sampler(background_world=True)
    assert quick.world_size_mb(s) == 0.0                                   # nothing measured yet: answer at once
    for _ in range(100):
        time.sleep(0.02)
        if quick.world_size_mb(s):
            break
    assert quick.world_size_mb(s) == pytest.approx(1.0, abs=0.01)
    assert Sampler().world_size_mb(s) == pytest.approx(1.0, abs=0.01)      # the command line still waits for the real number


def test_a_game_rule_list_asks_whether_the_server_is_accepting_only_once():
    asked = []
    server = SimpleNamespace(accepting=lambda: asked.append(1) or False)
    rule = settings_schema.GAMERULES[0]
    for _ in range(5):
        assert settings_schema.current(server, rule, accepting=False) is None
    assert asked == []
    assert settings_schema.current(server, rule) is None and asked == [1]    # the old way still works


# ---------- the dashboard builds its tabs after the first paint ----------
def test_the_status_bar_says_checking_until_docker_answers():
    segments = net_segments({"tunnel": "docker", "pending": True, "share_mode": "local", "share_host": "mc-ab12.local"}, False)
    assert ("internet o checking...", "grey62") in segments
    assert ("internet o VPN down", "yellow") in net_segments({"tunnel": "docker", "share_mode": "local"}, False)


def test_every_tab_is_built_after_the_first_paint(make_server):
    s = make_server("pytest-lazy")

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            await ready(app, pilot)
            assert app.panes_ready and all(app._mounted.get(n) for n in tui.LAZY_PANES)
            assert {p.id for p in app.query("TabPane")} == {f"tab-{n}" for n in tui.TABS}
            assert all(app.query(f"#pane-{n}") for n in tui.TABS)
    asyncio.run(go())


def test_a_tab_that_isnt_built_yet_ignores_calls_and_loads_when_it_is(make_server, monkeypatch):
    s = make_server("pytest-lazy2")

    async def go():
        async def never(self):                                             # hold the background build back
            return None
        monkeypatch.setattr(tui.Dashboard, "_mount_rest", never)
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            for _ in range(100):
                await pilot.pause(0.1)
                if app.snapshot is not None:
                    break
            assert not app.panes_ready and app.pane("settings") is tui.NOT_MOUNTED
            assert app.pane("settings").set_values({"a": "b"}) is None     # a no-op, not a crash
            assert not app.query("#pane-network") and app.query("#pane-console")
            app.activity("kept for later")                                 # the Overview isn't built: the line waits
            assert app._early_activity == [("kept for later", "")]
            await app.ensure_pane("overview")
            assert app._early_activity == [] and app.pane("overview") is not tui.NOT_MOUNTED
            app.query_one("#tabs", TabbedContent).active = "tab-overview"
            for _ in range(50):                                            # a log in a hidden tab draws its lines once it is shown
                await pilot.pause(0.1)
                if any("kept for later" in str(ln.text) for ln in app.query_one("#activity").lines):
                    break
            assert any("kept for later" in str(ln.text) for ln in app.query_one("#activity").lines)
            app.query_one("#tabs", TabbedContent).active = "tab-network"   # opening a tab builds it at once
            for _ in range(100):
                await pilot.pause(0.1)
                if app.pane_built("network") and app.query("#pane-network"):
                    break
            assert app.pane("network").server is not None and app.pane("network").server.name == s.name
    asyncio.run(go())


def test_the_tab_left_open_last_time_is_built_before_it_is_shown(make_server):
    s = make_server("pytest-lazy3")
    core.save_setting("dash_tab", "network")

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            await ready(app, pilot)
            assert app.query_one("#tabs", TabbedContent).active == "tab-network"
            assert app.pane("network").server.name == s.name
    asyncio.run(go())


def test_the_status_bar_is_drawn_before_docker_has_answered(make_server, monkeypatch):
    s = make_server("pytest-lazy4")

    async def go():
        release = []

        def slow(self):                                                   # Docker takes a long time to answer
            for _ in range(200):
                if release:
                    break
                time.sleep(0.02)
            return {"docker": True, "tunnel": "off", "tunnel_ok": False, "share_mode": None, "bonjour": False, "share_host": None,
                    "proton_lan": None, "marker": "slow answer"}
        monkeypatch.setattr(tui.Dashboard, "_compute_net", slow)
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            for _ in range(100):
                await pilot.pause(0.1)
                if app.snapshot is not None:
                    break
            assert app.snapshot is not None and app.snapshot.net.get("pending")      # first data without waiting for Docker
            release.append(1)
            for _ in range(100):
                await pilot.pause(0.1)
                if app.snapshot.net.get("marker"):
                    break
            assert app.snapshot.net.get("marker") == "slow answer"                  # and the real answer is drawn when it lands
    asyncio.run(go())
