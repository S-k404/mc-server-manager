"""Docker runtime, VPN/tunnel stack, agent guard and cats. Docker itself is faked: these run offline."""
import json
import os
import stat
import time

import pytest
from rich.console import Console

from mcctl import agent, cat, config, container as dk, core, servers, settings_schema, vpn
from mcctl.core import Fail


def fake_inspect(monkeypatch, status="running", health="healthy", exit_code=0, started="2026-10-02T14:30:00.123456789Z"):
    """Make every container look like this; status=None means 'no such container'."""
    info = None if status is None else {"State": {"Status": status, "ExitCode": exit_code, "StartedAt": started,
                                                  **({"Health": {"Status": health}} if health else {})}}
    monkeypatch.setattr(dk, "inspect", lambda name, fresh=False: info)


# ---------- config ----------
def test_schema_v2_migrates_to_v3_with_native_runtime(make_server):
    s = make_server(schema=2)
    assert s.inst.runtime == "native" and s.docker is False
    assert json.loads(s.cfg_path.read_text())["schema"] == config.SCHEMA == 3


def test_runtime_is_validated(make_server):
    s = make_server()
    with pytest.raises(config.ConfigError):
        s.update(runtime="lxc")


# ---------- server model on Docker ----------
def test_docker_states(make_server, monkeypatch):
    s = make_server(runtime="docker", desired="running")
    assert s.container == "minecraftvps-pytest-a"
    fake_inspect(monkeypatch, "running", "healthy")
    assert s.up() and s.state() == "running" and s.accepting()
    fake_inspect(monkeypatch, "running", "starting")
    assert s.state() == "starting" and not s.accepting()   # port is published before the server is ready
    fake_inspect(monkeypatch, "restarting", "starting")
    assert s.up()
    fake_inspect(monkeypatch, "exited", None, exit_code=137)
    assert not s.up() and s.last_exit() == 137 and s.state() == "crashed"
    fake_inspect(monkeypatch, "exited", None, exit_code=0)
    assert s.state() == "stopped"                            # clean /stop is not a crash
    fake_inspect(monkeypatch, None)
    assert s.state() == "stopped" and s.last_exit() is None


def test_status_checks_survive_missing_docker(make_server, monkeypatch):
    """`mc ls` on a machine without Docker (e.g. a VPS the server was deployed to) must not blow up."""
    s = make_server(runtime="docker")
    monkeypatch.setattr(dk, "docker_path", lambda: None)
    dk._cache.clear()
    assert s.state() == "stopped" and not s.up()


def test_started_at_parses_docker_nanoseconds(monkeypatch):
    fake_inspect(monkeypatch, started="2026-10-02T14:30:00.123456789Z")
    assert dk.started_at("x") == pytest.approx(1790951400.123456, abs=1)
    fake_inspect(monkeypatch, started="0001-01-01T00:00:00Z")
    assert dk.started_at("x") is None


def test_java_flags_shared_by_both_runtimes(make_server):
    s = make_server(ram_gb=3)
    flags = s.java_flags()
    assert "-Xmx3G" in flags and flags[-3:] == ["-jar", "fabric-server-launch.jar", "nogui"]
    assert s.launch_argv("/j/java")[-len(flags) - 1:] == ["/j/java", *flags]


# ---------- runtime setting ----------
def test_runtime_setting_needs_stopped_server_and_docker(make_server, monkeypatch):
    s = make_server()
    monkeypatch.setattr(dk, "require", lambda: (_ for _ in ()).throw(Fail("Docker isn't running.")))
    with pytest.raises(Fail, match="Docker isn't running"):
        settings_schema.apply(s, "runtime", "docker")
    assert s.inst.runtime == "native"                         # nothing saved on failure
    monkeypatch.setattr(dk, "require", lambda: None)
    settings_schema.apply(s, "runtime", "docker")
    assert s.inst.runtime == "docker"
    fake_inspect(monkeypatch, "running")
    with pytest.raises(Fail, match="Stop"):
        settings_schema.apply(s, "runtime", "native")        # can't switch under a running server
    with pytest.raises(Fail):
        settings_schema.apply(s, "runtime", "lxc")


# ---------- launching the container ----------
@pytest.fixture
def launched(monkeypatch):
    calls = {}
    monkeypatch.setattr(dk, "require", lambda: None)
    monkeypatch.setattr(dk, "vm_memory_gb", lambda: 7.7)
    monkeypatch.setattr(dk, "ensure_network", lambda: None)
    monkeypatch.setattr(dk, "pull", lambda image, say=None: calls.setdefault("image", image))
    monkeypatch.setattr(dk, "remove", lambda name: calls.setdefault("removed", name))
    monkeypatch.setattr(dk, "run", lambda *args: calls.setdefault("args", list(args)))
    return calls


def test_launch_container_mounts_same_folder_and_keeps_rcon_local(make_server, launched):
    s = make_server(runtime="docker", ram_gb=2, java_major=25, rcon_port=25575, keep_alive=True)
    servers.launch_container(s)
    a = launched["args"]
    assert launched["image"] == "eclipse-temurin:25-jre" and launched["removed"] == s.container
    assert a[a.index("-v") + 1] == f"{s.dir}:/data"                  # the same servers/<name>/ folder
    assert "127.0.0.1:25575:25575" in a and "25565:25565" in a        # RCON never exposed to the network
    assert a[a.index("--restart") + 1] == "on-failure:3"
    assert a[a.index("--memory") + 1] == "3584m"                      # 2 GB heap + 1.5 GB overhead
    assert a[-len(s.java_flags()) - 1:] == ["java", *s.java_flags()]
    assert a[a.index("--cap-drop") + 1] == "ALL" and "no-new-privileges" in a   # no Linux capabilities, can't gain any
    assert "--pids-limit" in a


def test_launch_container_no_restart_policy_without_keep_alive(make_server, launched):
    s = make_server(runtime="docker", rcon_port=25575)
    servers.launch_container(s)
    a = launched["args"]
    assert a[a.index("--restart") + 1] == "no"


def test_launch_container_refuses_when_docker_has_too_little_memory(make_server, launched, monkeypatch):
    monkeypatch.setattr(dk, "vm_memory_gb", lambda: 3.0)
    s = make_server(runtime="docker", ram_gb=4, rcon_port=25575)
    with pytest.raises(Fail, match="Docker Desktop"):
        servers.launch_container(s)
    assert "args" not in launched


def test_delete_removes_the_container(make_server, launched, monkeypatch):
    fake_inspect(monkeypatch, "exited", None)
    s = make_server(runtime="docker")
    servers.delete(s.name)
    assert launched["removed"] == s.container


# ---------- agent ----------
def test_agent_leaves_docker_servers_alone_while_docker_is_down(make_server, monkeypatch):
    """A stale/blind agent once marked a Docker server 'stopped from in-game' and reset desired."""
    s = make_server(runtime="docker", desired="running", keep_alive=True)
    monkeypatch.setattr(dk, "available", lambda: False)
    agent.reconcile(s, {}, first=True, now=time.time())
    assert s.inst.desired == "running"


def test_agent_respects_in_game_stop_on_docker(make_server, monkeypatch):
    s = make_server(runtime="docker", desired="running")
    monkeypatch.setattr(dk, "available", lambda: True)
    fake_inspect(monkeypatch, "exited", None, exit_code=0)
    agent.reconcile(s, {}, first=False, now=time.time())
    assert s.inst.desired == "stopped"


def test_agent_restarts_crashed_docker_server_with_keep_alive(make_server, monkeypatch):
    s = make_server(runtime="docker", desired="running", keep_alive=True)
    monkeypatch.setattr(dk, "available", lambda: True)
    fake_inspect(monkeypatch, "exited", None, exit_code=137)
    started = []
    monkeypatch.setattr(agent.servers, "start", lambda srv: started.append(srv.name))
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    agent.reconcile(s, {}, first=False, now=time.time())
    assert started == [s.name]


def test_agent_code_stamp_tracks_source_mtime(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1")
    monkeypatch.setattr(agent, "__file__", str(tmp_path / "agent.py"))
    before = agent.code_stamp()
    os.utime(tmp_path / "a.py", (before + 100, before + 100))
    assert agent.code_stamp() == before + 100                       # a newer source file => agent should reload


# ---------- VPN / tunnel stack ----------
KEY = "A" * 43 + "="


def test_vpn_setup_writes_private_env_and_switches_tunnel(tmp_path):
    vpn.setup(KEY, "Netherlands")
    mode = stat.S_IMODE(os.stat(vpn.ENV_FILE).st_mode)
    assert mode == 0o600                                            # owner-only: it holds a private key
    text = vpn.ENV_FILE.read_text()
    assert "WIREGUARD_PRIVATE_KEY=" + KEY in text and "SERVER_COUNTRIES=Netherlands" in text
    assert vpn.configured() and vpn.enabled()


def test_vpn_setup_rejects_garbage_keys():
    with pytest.raises(Fail):
        vpn.setup("not-a-key")
    assert not vpn.enabled()


def test_forward_script_covers_every_server_port(make_server):
    make_server("a", port=25565)
    make_server("b", port=25566)
    script = vpn._forward_script("192.168.65.254")
    assert "TCP-LISTEN:25565,fork,reuseaddr TCP:192.168.65.254:25565" in script
    assert "TCP-LISTEN:25566" in script and script.endswith("& wait")


def test_forward_script_also_listens_on_protons_port_for_gluetuns_missing_redirect(make_server, monkeypatch):
    make_server("a", port=25565)
    monkeypatch.setattr(vpn, "forward_server", lambda: "a")
    script = vpn._forward_script("192.168.65.254", forwarded=43318)
    assert "TCP-LISTEN:43318,fork,reuseaddr TCP:192.168.65.254:25565" in script
    assert "43318" not in vpn._forward_script("192.168.65.254")        # nothing extra before a port exists


def test_vpn_start_needs_setup_and_a_server(make_server, monkeypatch):
    monkeypatch.setattr(dk, "require", lambda: None)
    with pytest.raises(Fail, match="mc vpn setup"):
        vpn.start()
    vpn.setup(KEY)
    with pytest.raises(Fail, match="No servers"):
        vpn.start()


def test_vpn_start_turns_on_port_forwarding_for_the_chosen_server(make_server, monkeypatch):
    """gluetun gets port forwarding + a redirect to that server's port; the old playit container is cleaned up."""
    make_server("a", port=25565)
    make_server("b", port=25566)
    vpn.setup(KEY)
    ran, removed = [], []
    monkeypatch.setattr(dk, "require", lambda: None)
    monkeypatch.setattr(dk, "ensure_network", lambda: None)
    monkeypatch.setattr(dk, "pull", lambda image, say=None: None)
    monkeypatch.setattr(dk, "remove", lambda name: removed.append(name))
    monkeypatch.setattr(dk, "host_gateway_ip", lambda: "192.168.65.254")
    monkeypatch.setattr(dk, "run", lambda *a: ran.append(a))
    monkeypatch.setattr(dk, "available", lambda: False)
    vpn.set_forward_server("b")
    vpn.start(wait=False)
    gluetun = next(a for a in ran if "--name" in a and a[a.index("--name") + 1] == vpn.VPN)
    assert "VPN_PORT_FORWARDING=on" in gluetun and "PORT_FORWARD_ONLY=on" in gluetun
    assert "VPN_PORT_FORWARDING_LISTENING_PORT=25566" in gluetun
    assert "--tmpfs" in gluetun and "/tmp/gluetun" in gluetun
    assert "minecraft-tunnel" in removed                            # legacy playit container
    assert not any("playit" in " ".join(a) for a in ran)
    assert any("socat" in " ".join(a) for a in ran)


def test_forward_server_falls_back_when_the_chosen_one_is_gone(make_server):
    make_server("a")
    core.save_setting("vpn_forward_server", "deleted")
    assert vpn.forward_server() == "a"
    with pytest.raises(Fail):
        vpn.set_forward_server("nope")


def test_forwarded_port_from_file_or_log(monkeypatch):
    from types import SimpleNamespace as R
    monkeypatch.setattr(dk, "state", lambda name: "running")
    monkeypatch.setattr(dk, "docker", lambda *a, **k: R(returncode=0, stdout="54321\n", stderr=""))
    assert vpn.forwarded_port() == 54321

    def fake(*a, **k):
        if a[0] == "exec":
            return R(returncode=1, stdout="", stderr="no such file")
        return R(returncode=0, stdout="INFO [port forwarding] port forwarded is 4242\n", stderr="")
    monkeypatch.setattr(dk, "docker", fake)
    assert vpn.forwarded_port() == 4242
    monkeypatch.setattr(dk, "state", lambda name: "exited")
    assert vpn.forwarded_port() is None


def test_public_address_is_cached_briefly_and_shows_in_invites(make_server):
    import json
    import time
    from mcctl import network
    s = make_server("a")
    vpn.setup(KEY)
    assert vpn.public_address() is None and "From anywhere" not in network.invite(s)
    vpn._write_cache("203.0.113.2:54321", "a")
    assert vpn.public_address() == "203.0.113.2:54321"
    assert "From anywhere: 203.0.113.2:54321" in network.invite(s)
    other = make_server("b", port=25570)
    assert "From anywhere" not in network.invite(other)             # the address leads to 'a', not 'b'
    vpn.CACHE_FILE.write_text(json.dumps({"public": "1.1.1.1:1", "server": "a", "at": time.time() - 500}))
    assert vpn.public_address() is None                             # stale: the VPN may have reconnected


def test_vpn_heal_is_a_noop_when_off_and_restarts_when_down(monkeypatch):
    started = []
    monkeypatch.setattr(vpn, "start", lambda wait=True, patience=120: started.append(wait))
    monkeypatch.setattr(dk, "available", lambda: True)
    assert vpn.heal() is None and not started                      # not enabled
    vpn.setup(KEY)
    core.save_setting("vpn_desired", True)
    monkeypatch.setattr(dk, "inspect", lambda name, fresh=False: None)
    assert vpn.heal() == "restarted the VPN stack" and started == [False]
    core.save_setting("vpn_desired", False)
    started.clear()
    assert vpn.heal() is None and not started                      # user stopped it on purpose


def test_vpn_heal_restarts_the_forwarder_when_it_predates_the_vpn(monkeypatch):
    vpn.setup(KEY)
    core.save_setting("vpn_desired", True)
    monkeypatch.setattr(dk, "available", lambda: True)
    infos = {vpn.VPN: {"State": {"Status": "running", "StartedAt": "2026-10-02T15:00:00Z"}},
             vpn.FORWARD: {"State": {"Status": "running", "StartedAt": "2026-10-02T14:00:00Z"}}}
    monkeypatch.setattr(dk, "inspect", lambda name, fresh=False: infos[name])
    redone = []
    monkeypatch.setattr(vpn, "_start_dependents", lambda ip=None, forwarded=None: redone.append(forwarded))
    monkeypatch.setattr(vpn, "forwarded_port", lambda: 43318)
    assert vpn.heal() == "restarted the forwarder" and redone == [43318]   # it joined a network that's gone


def test_vpn_heal_points_the_forwarder_at_a_new_proton_port(monkeypatch):
    vpn.setup(KEY)
    core.save_setting("vpn_desired", True)
    monkeypatch.setattr(dk, "available", lambda: True)
    started = {"State": {"Status": "running", "StartedAt": "2026-10-02T15:00:00Z"}}
    infos = {vpn.VPN: started, vpn.FORWARD: {**started, "Config": {"Labels": {"mc.forwarded": "40000"}}}}
    monkeypatch.setattr(dk, "inspect", lambda name, fresh=False: infos[name])
    redone = []
    monkeypatch.setattr(vpn, "_start_dependents", lambda ip=None, forwarded=None: redone.append(forwarded))
    monkeypatch.setattr(vpn, "forwarded_port", lambda: 43318)
    assert vpn.heal() == "pointed the forwarder at Proton's current port" and redone == [43318]
    redone.clear()
    infos[vpn.FORWARD]["Config"]["Labels"]["mc.forwarded"] = "43318"
    assert vpn.heal() is None and redone == []


# ---------- cats ----------
def test_cat_is_silent_in_pipes_and_when_disabled(monkeypatch, capsys):
    console = Console(force_terminal=False)
    monkeypatch.setattr(cat, "CHANCE", 1.0)
    cat.maybe(console)
    assert capsys.readouterr().out == ""                            # stdout isn't a terminal under pytest
    monkeypatch.setattr(cat.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("MC_NO_CAT", "1")
    assert not cat.enabled()


def test_cat_prints_when_forced_and_toggles(capsys):
    console = Console(force_terminal=False, width=60)
    for i in range(len(cat.CATS)):
        cat.show(console, i)
    out = capsys.readouterr().out
    assert "( o.o )" in out and out.count("\n") > len(cat.CATS)
    cat.set_enabled(False)
    assert core.setting("cats") is False


# ---------- doctor ----------
def test_doctor_reports_the_internet_address_or_the_natpmp_hint(make_server, monkeypatch):
    from mcctl import network
    s = make_server()
    monkeypatch.setattr(network.platform, "vpn_active", lambda: False)
    monkeypatch.setattr(network.platform, "lan_ip", lambda: "10.0.0.5")
    checks = network.doctor([s])                                    # internet play not set up: informational, not a problem
    assert any("internet play is off" in c.message and c.good for c in checks)
    vpn.setup(KEY)
    base = {"docker": True, "vpn_health": "healthy", "exit": "Amsterdam, NL", "containers": {vpn.VPN: "running"},
            "forward_server": s.name}
    monkeypatch.setattr(vpn, "status", lambda: {**base, "public": "203.0.113.2:54321"})
    assert any("203.0.113.2:54321" in c.message and c.good for c in network.doctor([s]))
    monkeypatch.setattr(vpn, "status", lambda: dict(base))
    bad = [c for c in network.doctor([s]) if not c.good and "forwarded port" in c.message]
    assert bad and "NAT-PMP" in bad[0].fix
