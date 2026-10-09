"""Without tmux installed mc says so where it matters and otherwise sees no sessions, instead of crashing with FileNotFoundError."""
import pytest

from mcctl import core, servers
from mcctl.core import Fail


def test_no_tmux_means_no_sessions(make_server):
    s = make_server()
    assert core.session_names() == set()
    assert s.up() is False and s.state() == "stopped"


def test_a_tmux_question_returns_a_failed_result_not_an_exception():
    done = core.tmux("list-sessions", capture=True)
    assert done.returncode == 127 and done.stdout == ""


def test_typing_into_a_console_that_cannot_exist_does_nothing(make_server):
    make_server().send("say hi")                      # no tmux, no session: nothing to type into, and no crash


def test_force_stop_without_tmux_just_says_it_is_not_running(make_server, capsys):
    s = make_server()
    servers.kill(s)
    assert "not running" in capsys.readouterr().out


def test_starting_a_native_server_says_how_to_install_tmux_and_leaves_it_stopped(make_server, monkeypatch):
    s = make_server()
    monkeypatch.setattr(servers.provision, "ensure_java", lambda major: "java")
    monkeypatch.setattr(core.Server, "launch_argv", lambda self, java: ["java"])
    monkeypatch.setattr(core.Server, "ensure_rcon", lambda self, others=(): None)
    with pytest.raises(Fail, match=r"brew install tmux.*apt install tmux"):
        servers.start(s, wait=False)
    assert s.inst.desired != "running" and not s.up()                     # a failed start is not recorded as "wanted running"
