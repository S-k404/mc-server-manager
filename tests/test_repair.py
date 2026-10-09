"""`mc fix`: the ordered repair. Docker, the servers and the VPN are faked; these run offline."""
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from mcctl import container as dk, moddoctor, network, repair, servers, share, voice, vpn
from mcctl.cli import app


@pytest.fixture
def world(make_server, monkeypatch):
    s = make_server("main")
    w = SimpleNamespace(server=s, up=False, started=[], hops=[vpn.Step(True, "Docker answers", "", "")], actions=[], docker=True)
    monkeypatch.setattr(vpn, "enabled", lambda: True)
    monkeypatch.setattr(vpn, "forward_server", lambda: "main")
    monkeypatch.setattr(dk, "available", lambda: w.docker)
    monkeypatch.setattr(type(s), "up", lambda self, sessions=None: w.up)
    monkeypatch.setattr(type(s), "state", lambda self, sessions=None: "running" if w.up else "stopped")
    monkeypatch.setattr(servers, "start", lambda srv: (w.started.append(srv.name), setattr(w, "up", True)))
    monkeypatch.setattr(vpn, "fix", lambda start_servers=False, say=None: (w.hops, w.actions))
    monkeypatch.setattr(vpn, "public_address", lambda live=False: "203.0.113.5:43318")
    monkeypatch.setattr(vpn, "fix_hint", lambda hops: "")
    monkeypatch.setattr(share, "bonjour_ensure", lambda: None)
    monkeypatch.setattr(share, "needs_restart", lambda srv: False)
    monkeypatch.setattr(voice, "servers_using_voice", lambda: [])
    monkeypatch.setattr(moddoctor, "check", lambda srv: [])
    monkeypatch.setattr(network, "auth_checks", lambda *a, **k: [])
    return w


def names(steps):
    return [st.what for st in steps]


def test_everything_is_checked_in_order_and_a_stopped_server_is_started(world):
    world.actions = ["recreated the VPN"]
    steps = repair.run(say=lambda m: None)
    assert names(steps) == ["Docker answers", "Server 'main' was stopped: started it", "Did: recreated the VPN",
                            "The internet path: every hop answers", "Minecraft sign-in through the launcher proxy"]
    assert world.started == ["main"] and not [st for st in steps if st.ok is False]


def test_a_server_stopped_on_purpose_stays_stopped_when_asked_not_to_start_it(world):
    steps = repair.run(start_servers=False, say=lambda m: None)
    assert world.started == [] and not any("main" in st.what for st in steps)


def test_a_server_that_crashed_is_started_even_without_the_flag(world, monkeypatch):
    monkeypatch.setattr(type(world.server), "state", lambda self, sessions=None: "crashed")
    repair.run(start_servers=False, say=lambda m: None)
    assert world.started == ["main"]


def test_docker_down_stops_the_vpn_part_but_not_the_rest(world):
    world.docker = False
    steps = repair.run(say=lambda m: None)
    assert [st for st in steps if st.ok is False][0].what == "Docker answers"
    assert not any(st.what.startswith(("Did:", "The internet path")) for st in steps)
    assert world.started == ["main"]                                            # a native server doesn't need Docker


def test_a_still_broken_path_is_listed_with_its_hint(world, monkeypatch):
    world.hops = [vpn.Step(True, "Docker answers", "", ""), vpn.Step(False, "VPN connected (gluetun healthy)", "unhealthy", "mc vpn logs vpn"),
                  vpn.Step(False, "Joining 1.2.3.4:5 from this Mac", "timed out", "")]
    monkeypatch.setattr(vpn, "fix_hint", lambda hops: "Turn the Mac's Proton on and run: mc fix")
    steps = repair.run(say=lambda m: None)
    bad = [st for st in steps if st.ok is False]
    assert [b.what for b in bad] == ["VPN connected (gluetun healthy)", "The internet path is still broken"]     # a lone self-test is ignored
    assert "Turn the Mac's Proton on" in bad[-1].detail


def test_a_server_that_will_not_start_is_reported_not_raised(world, monkeypatch):
    from mcctl.core import Fail

    def boom(srv):
        raise Fail("Port 25565 is in use by another program.")
    monkeypatch.setattr(servers, "start", boom)
    steps = repair.run(say=lambda m: None)
    bad = next(st for st in steps if st.ok is False)
    assert bad.what == "Server 'main' won't start" and "in use" in bad.detail and "mc -s main crash" in bad.fix


def test_voice_chat_and_mod_problems_are_part_of_the_sequence(world, monkeypatch):
    monkeypatch.setattr(voice, "servers_using_voice", lambda: [world.server])
    monkeypatch.setattr(voice, "maintain", lambda srv, log=None, now=None, settle=120: "voice chat address set to 203.0.113.5:43318")
    monkeypatch.setattr(voice, "route", lambda srv: "vpn")
    monkeypatch.setattr(moddoctor, "check", lambda srv: [moddoctor.Finding("missing", "error", "a.jar", "A needs B, which is not installed.")])
    steps = repair.run(say=lambda m: None)
    v = next(st for st in steps if st.what.startswith("Voice chat"))
    assert v.ok and "through the internet address" in v.what and "set to 203.0.113.5:43318" in v.detail
    m = next(st for st in steps if "mod problem" in st.what)
    assert m.ok is None and "mods doctor" in m.fix
    assert names(steps).index(v.what) < names(steps).index(m.what)             # voice before mods, as documented


def test_a_running_server_that_needs_a_restart_for_the_mode_is_told_not_restarted(world, monkeypatch):
    world.up = True
    monkeypatch.setattr(share, "needs_restart", lambda srv: True)
    monkeypatch.setattr(share, "mode", lambda: "hidden")
    steps = repair.run(say=lambda m: None)
    step = next(st for st in steps if "needs a restart" in st.what)
    assert step.ok is None and "hidden" in step.what and "restart" in step.fix and world.started == []


def test_without_internet_play_it_says_so_and_does_not_touch_the_vpn(world, monkeypatch):
    monkeypatch.setattr(vpn, "enabled", lambda: False)
    monkeypatch.setattr(vpn, "fix", lambda **kw: pytest.fail("no VPN: nothing to fix"))
    steps = repair.run(say=lambda m: None)
    assert "Internet play is off" in names(steps)


def test_cli_fix_prints_the_invite_when_everything_is_fine_and_exits_1_when_not(world, monkeypatch):
    from mcctl import network
    monkeypatch.setattr(network, "invite", lambda srv, live=False, show_ip=False: "INVITE for " + srv.name)
    ok = CliRunner().invoke(app, ["fix"])
    assert ok.exit_code == 0 and "[ok] Docker answers" in ok.output and "INVITE for main" in ok.output
    world.docker = False
    bad = CliRunner().invoke(app, ["fix"])
    assert bad.exit_code == 1 and "[x] Docker answers" in bad.output and "still need you" in bad.output and "INVITE" not in bad.output


def test_a_repair_that_failed_is_not_reported_as_done(world):
    world.actions = ["could not recreate the VPN: The VPN didn't come up within 2 minutes"]
    steps = repair.run(say=lambda m: None)
    did = next(st for st in steps if st.what.startswith("Did:"))
    assert did.ok is False


def test_fix_restarts_the_server_for_voice_chat_at_once_when_nobody_is_on(world, monkeypatch):
    seen = []
    monkeypatch.setattr(voice, "servers_using_voice", lambda: [world.server])
    monkeypatch.setattr(voice, "route", lambda srv: "vpn")
    monkeypatch.setattr(voice, "maintain", lambda srv, log=None, now=None, settle=120: seen.append(settle) or "")
    repair.run(say=lambda m: None)
    assert seen == [0]                                          # no two-minute wait: this is a repair someone asked for


def check(host, good, group="Sign-in: through the Docker VPN proxy"):
    return network.Check(group, good, f"{host} reachable (ok)" if good else f"{host} blocked: TimeoutError", "")


def test_sign_in_through_the_proxy_is_part_of_the_repair(world, monkeypatch):
    hosts = list(network.SIGNIN_HOSTS)
    monkeypatch.setattr(network, "auth_checks", lambda *a, **k: [check(h, True) for h in hosts])
    step = next(st for st in repair.run(say=lambda m: None) if st.what.startswith("Minecraft sign-in"))
    assert step.ok is True and "mc vpn proxy" in step.detail
    monkeypatch.setattr(network, "auth_checks", lambda *a, **k: [check(hosts[0], False)] + [check(h, True) for h in hosts[1:]])
    step = next(st for st in repair.run(say=lambda m: None) if st.what.startswith("Minecraft sign-in"))
    assert step.ok is False and "1 of 5" in step.detail and hosts[0] in step.detail and "mc vpn logs vpn" in step.fix
    assert "mc fix" in step.fix                                                               # a cold tunnel: try again in a minute
    monkeypatch.setattr(network, "auth_checks", lambda *a, **k: [])                               # proxy not answering: unknown, not a failure
    step = next(st for st in repair.run(say=lambda m: None) if st.what.startswith("Minecraft sign-in"))
    assert step.ok is None
    monkeypatch.setattr(network, "auth_checks", lambda *a, **k: (_ for _ in ()).throw(OSError("no network")))
    step = next(st for st in repair.run(say=lambda m: None) if st.what.startswith("Minecraft sign-in"))
    assert step.ok is None and "OSError" in step.detail


def test_sign_in_is_not_probed_when_the_internet_path_is_broken(world, monkeypatch):
    world.hops = [vpn.Step(False, "VPN connected (gluetun healthy)", "unhealthy", "")]
    monkeypatch.setattr(network, "auth_checks", lambda *a, **k: pytest.fail("no point probing through a dead tunnel"))
    repair.run(say=lambda m: None)


def test_sign_in_is_probed_through_the_proxy_only_and_patiently(world, monkeypatch):
    seen = {}

    def fake(probe_fn=None, hosts=None, direct=True):
        seen.update(probe_fn=probe_fn, hosts=hosts, direct=direct)
        return []
    monkeypatch.setattr(network, "auth_checks", fake)
    repair.run(say=lambda m: None)
    assert seen["probe_fn"] is network.patient_probe and seen["hosts"] == network.SIGNIN_HOSTS and seen["direct"] is False
