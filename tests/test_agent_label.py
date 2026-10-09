"""The launchd job has a neutral name; an agent installed under its earlier, login-named label is moved, never doubled."""
import plistlib
import subprocess

import pytest

from mcctl import agent, platform

OLD = "com.someone.mc-agent"


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A fake LaunchAgents folder with the agent installed under the old label, and a launchctl that records its calls."""
    launch = tmp_path / "LaunchAgents"
    launch.mkdir()
    monkeypatch.setattr(platform, "IS_MAC", True)
    monkeypatch.setattr(agent, "LAUNCH_DIR", launch)
    monkeypatch.setattr(agent, "PLIST", launch / f"{agent.LABEL}.plist")
    monkeypatch.setattr(agent, "LEGACY_LABELS", (OLD,))
    monkeypatch.setattr(agent, "LOG_FILE", tmp_path / "logs" / "agent.log")
    old_plist = launch / f"{OLD}.plist"
    old_plist.write_bytes(plistlib.dumps({"Label": OLD}))

    calls, loaded = [], {"bootstrap_fails": False}

    def launchctl(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "bootstrap" and loaded["bootstrap_fails"]:
            return subprocess.CompletedProcess(argv, 5, "", "Bootstrap failed: 5")
        return subprocess.CompletedProcess(argv, 0, "state = running\nlast exit code = 0\n", "")

    monkeypatch.setattr(agent.subprocess, "run", launchctl)
    return type("Mac", (), {"old": old_plist, "new": launch / f"{agent.LABEL}.plist", "calls": calls, "state": loaded})


def verbs(calls):
    return [(c[1], c[2].rsplit("/", 1)[-1] if c[1] != "bootstrap" else c[3].rsplit("/", 1)[-1]) for c in calls]


def test_the_label_names_nobody():
    assert agent.LABEL == "local.mcctl.agent"
    assert agent.LABEL not in agent.LEGACY_LABELS and all(label.endswith(".mc-agent") for label in agent.LEGACY_LABELS)


def test_install_stops_and_removes_the_old_job_and_loads_the_new_one(mac, capsys):
    assert agent.agent_installed() and agent.legacy_installed()            # before: only the old name
    agent.install()
    assert not mac.old.exists() and mac.new.exists()
    assert plistlib.loads(mac.new.read_bytes())["Label"] == agent.LABEL
    seq = verbs(mac.calls)
    assert seq.index(("bootout", OLD)) < seq.index(("bootstrap", f"{agent.LABEL}.plist"))   # the old one stops first: never two at once
    assert agent.agent_installed() and not agent.legacy_installed()
    assert "Moved the agent" in capsys.readouterr().out


def test_install_twice_leaves_one_job(mac):
    agent.install()
    agent.install()
    assert [p.name for p in mac.new.parent.glob("*.plist")] == [mac.new.name]


def test_a_new_job_that_will_not_load_leaves_the_old_plist_for_next_login(mac):
    mac.state["bootstrap_fails"] = True
    with pytest.raises(agent.Fail, match="refused to load"):
        agent.install()
    assert mac.old.exists()


def test_uninstall_removes_both_names(mac):
    agent.install()
    mac.old.write_bytes(plistlib.dumps({"Label": OLD}))                    # as if an old copy came back
    agent.uninstall()
    assert not mac.old.exists() and not mac.new.exists() and not agent.agent_installed()
    assert ("bootout", OLD) in verbs(mac.calls) and ("bootout", agent.LABEL) in verbs(mac.calls)


def test_uninstall_alone_clears_an_agent_that_was_never_moved(mac):
    agent.uninstall()
    assert not mac.old.exists() and ("bootout", OLD) in verbs(mac.calls)


def test_status_looks_under_the_old_name_until_it_is_moved(mac):
    assert agent.agent_running()
    printed = [c for c in mac.calls if c[1] == "print"]
    assert printed[-1][2].endswith(OLD)
    agent.install()
    agent.agent_running()
    assert [c for c in mac.calls if c[1] == "print"][-1][2].endswith(agent.LABEL)


def test_linux_has_no_old_name_to_move(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "IS_MAC", False)
    monkeypatch.setattr(agent, "UNIT", tmp_path / "mc-agent.service")
    assert not agent.legacy_installed() and not agent.agent_installed()
