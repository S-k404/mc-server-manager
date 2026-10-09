"""Share modes: hidden (loopback only, Docker VPN address only) and local (a Bonjour name instead of the number)."""
import asyncio
import json
import subprocess
import sys
import types

import pytest

from mcctl import alerts, core, network, platform, servers, share, speedtest, support, vpn
from mcctl import container as dk
from mcctl.core import Fail

REAL_POPEN = subprocess.Popen
LAN = "10.77.88.99"                       # a distinctive fake Wi-Fi address: if it shows up anywhere, something leaked


@pytest.fixture
def wifi(monkeypatch):
    """A Wi-Fi address and no Mac-wide VPN. (Enough for the dashboard: it must not think this is macOS, or it would run
    macOS-only commands that don't exist where the tests run.)"""
    monkeypatch.setattr(platform, "lan_ip", lambda: LAN)
    monkeypatch.setattr(platform, "vpn_active", lambda: False)


@pytest.fixture
def mac(wifi, monkeypatch):
    """`wifi`, plus macOS's dns-sd and a stand-in for the dns-sd process."""
    monkeypatch.setattr(share.platform, "IS_MAC", True)
    monkeypatch.setattr(share.shutil, "which", lambda name: "/usr/bin/dns-sd")
    spawned = []

    def fake_popen(args, **kw):
        # a real process whose command line contains "dns-sd", so liveness and pid-reuse checks are exercised for real
        proc = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(60)", "dns-sd"])
        spawned.append((args, proc))
        return proc
    # replace only share's own reference: patching the global subprocess.Popen would also hijack every subprocess.run
    monkeypatch.setattr(share, "subprocess", types.SimpleNamespace(Popen=fake_popen, run=subprocess.run,
                                                                   DEVNULL=subprocess.DEVNULL))
    monkeypatch.setattr(share.time, "sleep", lambda s: None)
    yield spawned
    for _, proc in spawned:
        proc.kill()
        proc.wait()


def mode(name):
    core.save_setting("share_mode", name)


# ---------- the setting ----------
def test_nothing_changes_until_a_mode_is_chosen(make_server):
    s = make_server()
    assert share.mode() is None and share.wanted_bind() is None and share.publish_host() == ""
    share.apply_bind(s)
    assert s.prop("server-ip") == ""                                   # untouched
    core.save_setting("share_mode", "nonsense")
    assert share.mode() is None                                         # an unknown value counts as unset


def test_modes_map_to_a_bind_address_and_a_docker_publish_prefix():
    mode("hidden")
    assert share.wanted_bind() == "127.0.0.1" and share.publish_host() == "127.0.0.1:"
    mode("local")
    assert share.wanted_bind() == "" and share.publish_host() == ""


def test_choosing_a_mode_is_validated(make_server, mac):
    make_server()
    with pytest.raises(Fail, match="hidden or local"):
        share.set_mode("everything")
    assert share.mode() is None


def test_local_mode_needs_dns_sd_a_server_and_an_address(make_server, mac, monkeypatch):
    with pytest.raises(Fail, match="Create a server first"):
        share.set_mode("local")
    make_server()
    monkeypatch.setattr(share.platform, "IS_MAC", False)
    with pytest.raises(Fail, match="dns-sd"):
        share.set_mode("local")
    monkeypatch.setattr(share.platform, "IS_MAC", True)
    monkeypatch.setattr(platform, "lan_ip", lambda: "unknown")
    with pytest.raises(Fail, match="no Wi-Fi/LAN address"):
        share.set_mode("local")
    assert share.mode() is None                                         # a refused switch changes nothing


# ---------- where the server listens ----------
def test_hidden_mode_makes_a_native_server_listen_on_loopback_only(make_server, capsys):
    s = make_server()
    mode("hidden")
    share.apply_bind(s)
    assert s.prop("server-ip") == "127.0.0.1" and "listens on this Mac only" in capsys.readouterr().out
    share.apply_bind(s)
    assert capsys.readouterr().out == ""                                # already right: nothing said, nothing written


def test_local_mode_gives_the_wifi_back_but_never_overrides_a_deliberate_address(make_server):
    s = make_server("a", props={"server-ip": "127.0.0.1"})
    mode("local")
    share.apply_bind(s)
    assert s.prop("server-ip") == ""                                    # mc's own loopback bind is undone
    chosen = make_server("b", props={"server-ip": "192.168.1.50"})
    share.apply_bind(chosen)
    assert chosen.prop("server-ip") == "192.168.1.50"                   # somebody set this on purpose
    mode("hidden")
    share.apply_bind(chosen)
    assert chosen.prop("server-ip") == "127.0.0.1"                      # hidden has to win: it is the whole point


def test_a_docker_server_is_not_given_a_server_ip(make_server):
    s = make_server(runtime="docker")
    mode("hidden")
    share.apply_bind(s)
    assert s.prop("server-ip") == ""                   # inside the container it would break the published port instead


def test_starting_a_server_applies_the_mode_before_launching(make_server, monkeypatch):
    s = make_server()
    mode("hidden")
    seen = []

    def stop_here(major):
        seen.append(s.prop("server-ip"))
        raise Fail("stop here")
    monkeypatch.setattr(servers.provision, "ensure_java", stop_here)
    with pytest.raises(Fail, match="stop here"):
        servers.start(s)
    assert seen == ["127.0.0.1"]                       # already loopback when Java would have been launched


def test_hidden_docker_servers_publish_their_game_port_on_loopback(make_server, monkeypatch):
    calls = {}
    monkeypatch.setattr(dk, "require", lambda: None)
    monkeypatch.setattr(dk, "vm_memory_gb", lambda: 7.7)
    monkeypatch.setattr(dk, "ensure_network", lambda: None)
    monkeypatch.setattr(dk, "pull", lambda image, say=None: None)
    monkeypatch.setattr(dk, "refresh", lambda image, say=None: None)
    monkeypatch.setattr(dk, "remove", lambda name: None)
    monkeypatch.setattr(dk, "run", lambda *a: calls.setdefault("args", list(a)))
    s = make_server(runtime="docker", ram_gb=2, rcon_port=25575)
    servers.launch_container(s)
    assert "25565:25565" in calls["args"]
    calls.clear()
    mode("hidden")
    servers.launch_container(s)
    assert "127.0.0.1:25565:25565" in calls["args"] and "127.0.0.1:25575:25575" in calls["args"]


def test_which_running_servers_need_a_restart(make_server, monkeypatch):
    s = make_server()
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: True)
    assert not share.needs_restart(s)                                    # no mode: nothing to apply
    mode("hidden")
    assert share.needs_restart(s)                                        # still listening on every interface
    s.set_prop("server-ip", "127.0.0.1")
    assert not share.needs_restart(s)
    mode("local")
    assert share.needs_restart(s)                                        # still on loopback from hidden mode
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: False)
    assert not share.needs_restart(s)                                    # a stopped server picks it up when it starts


def test_a_running_docker_server_is_judged_by_the_address_its_port_was_published_on(make_server, monkeypatch):
    s = make_server(runtime="docker")
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: True)
    published = {"host": "0.0.0.0"}
    monkeypatch.setattr(dk, "inspect", lambda name, fresh=False: {"NetworkSettings": {"Ports": {
        "25565/tcp": [{"HostIp": published["host"]}]}}})
    mode("hidden")
    assert share.needs_restart(s)
    published["host"] = "127.0.0.1"
    assert not share.needs_restart(s)
    mode("local")
    assert share.needs_restart(s)


def test_switching_reports_the_servers_that_need_a_restart_and_warns_about_voice_chat(make_server, mac, monkeypatch, capsys):
    s = make_server()
    (s.dir / "mods").mkdir()
    (s.dir / "mods" / "voicechat-fabric-2.5.jar").write_bytes(b"x")
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: True)
    restart = share.set_mode("hidden", say=lambda msg: None)
    out = capsys.readouterr().out
    assert restart == [s.name] and "Voice chat can't work in hidden mode" in out and "mc vpn setup" in out


# ---------- the Bonjour name ----------
def test_the_invite_name_is_made_once_and_has_a_fixed_shape():
    first = share.alias()
    assert first.startswith("mc-") and len(first) == 7 and share.alias() == first and share.host() == first + ".local"


def test_dns_sd_is_asked_to_publish_the_name_and_its_address():
    assert share.bonjour_args("mc", 25565, "mc-ab12.local", LAN) == [
        "dns-sd", "-P", "mc", "_minecraft._tcp", "local", "25565", "mc-ab12.local", LAN]


def test_local_mode_publishes_once_and_only_republishes_when_something_changes(make_server, mac, monkeypatch):
    make_server()
    share.set_mode("local", say=lambda msg: None)
    [(args, proc)] = mac
    assert args == ["dns-sd", "-P", "mc", "_minecraft._tcp", "local", "25565", share.host(), LAN]
    assert share.bonjour_alive() and json.loads(share.STATE_FILE.read_text())["pid"] == proc.pid
    assert share.bonjour_ensure() is None and len(mac) == 1              # nothing changed: the same process keeps running
    monkeypatch.setattr(platform, "lan_ip", lambda: "10.77.88.100")      # the Wi-Fi address changed
    assert "published" in share.bonjour_ensure() and len(mac) == 2
    assert mac[1][0][-1] == "10.77.88.100"
    mac[0][1].wait(timeout=5)                                           # the old registration was ended (SIGTERM)
    mac[1][1].kill()                                                    # the process dies on its own
    mac[1][1].wait()
    share._forget()
    assert not share.bonjour_alive() and share.bonjour_ensure() and len(mac) == 3     # and is brought back


def test_leaving_local_mode_stops_publishing(make_server, mac):
    make_server()
    share.set_mode("local", say=lambda msg: None)
    proc = mac[0][1]
    share.set_mode("hidden", say=lambda msg: None)
    proc.wait(timeout=5)
    assert not share.STATE_FILE.exists() and not share.bonjour_alive()
    mode("local")
    share.bonjour_ensure()
    mode("hidden")
    assert share.bonjour_ensure() == "stopped publishing the local name"
    assert share.bonjour_ensure() is None


def test_a_reused_pid_is_never_trusted_or_killed(make_server, mac):
    bystander = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(60)"])    # not a dns-sd process
    try:
        share.STATE_FILE.write_text(json.dumps({"pid": bystander.pid, "ip": LAN, "port": 25565, "host": share.host()}))
        assert not share.bonjour_alive()
        share.bonjour_stop()
        assert bystander.poll() is None                                  # still running: mc did not kill someone else's process
    finally:
        bystander.kill()
        bystander.wait()


def test_a_publisher_that_dies_at_once_is_reported(make_server, mac, monkeypatch):
    make_server()
    monkeypatch.setattr(share, "bonjour_alive", lambda: False)
    with pytest.raises(Fail, match="dns-sd stopped right away"):
        share.set_mode("local", say=lambda msg: None)


def test_the_macs_own_bonjour_name_is_only_read_never_set(monkeypatch):
    monkeypatch.setattr(share.platform, "IS_MAC", True)
    for name, flagged in (("Alexs-MacBook-Pro", True), ("studio-7", False), ("Jordans-iMac", True)):
        monkeypatch.setattr(share, "local_hostname", lambda name=name: name)
        assert (share.personal_name_warning() is not None) is flagged
    monkeypatch.setattr(share, "local_hostname", lambda: "workstation-ravi")
    monkeypatch.setattr("getpass.getuser", lambda: "ravi")
    assert "looks like it contains a name" in share.personal_name_warning()
    assert "mc never changes it" in share.personal_name_warning()
    monkeypatch.setattr(share, "local_hostname", lambda: None)
    assert share.personal_name_warning() is None


# ---------- what may be shown ----------
def forwarding(monkeypatch, address="203.0.113.2:54321"):
    monkeypatch.setattr(vpn, "enabled", lambda: True)
    monkeypatch.setattr(vpn, "forward_server", lambda: "pytest-a")
    monkeypatch.setattr(vpn, "public_address", lambda live=False: address)


def test_default_mode_shows_the_wifi_address_as_before(make_server, mac):
    s = make_server()
    a = network.display_address(s)
    assert a["wifi"] == f"{LAN}:25565" and a["mode"] is None and a["this_mac"] == "localhost:25565"
    assert network.invite(s).splitlines()[1] == f"  Address: {LAN}:25565  (same Wi-Fi, VPN off)"


def test_local_mode_shows_the_name_while_it_is_published(make_server, mac):
    s = make_server()
    mode("local")
    a = network.display_address(s)
    assert a["wifi"] is None and "isn't published" in a["note"]            # not yet: no address that doesn't work
    assert "isn't published yet" in network.invite(s) and LAN not in network.invite(s)
    share.bonjour_ensure()
    a = network.display_address(s)
    assert a["wifi"] == f"{share.host()}:25565"
    text = network.invite(s)
    assert f"  Address: {share.host()}:25565  (same Wi-Fi)" in text and LAN not in text
    assert "turn on 'Allow LAN connections'" in text


def test_hidden_mode_never_shows_the_wifi_address(make_server, mac, monkeypatch):
    s = make_server()
    mode("hidden")
    forwarding(monkeypatch)
    a = network.display_address(s)
    assert a["wifi"] is None and a["internet"] == "203.0.113.2:54321" and LAN not in json.dumps(a)
    text = network.invite(s)
    assert "From anywhere: 203.0.113.2:54321" in text and LAN not in text and "Address:" not in text
    assert "stay on the VPN" in text
    monkeypatch.setattr(vpn, "public_address", lambda live=False: None)
    assert "not ready yet" in network.invite(s)                           # no address yet: say so, never fall back to the Wi-Fi


def test_show_ip_reveals_the_address_on_purpose(make_server, mac):
    s = make_server()
    mode("hidden")
    assert network.display_address(s, show_ip=True)["wifi"] == f"{LAN}:25565"
    assert f"Address: {LAN}:25565  (same Wi-Fi, VPN off)" in network.invite(s, show_ip=True)


def test_the_internet_address_belongs_to_the_forwarded_server_only(make_server, mac, monkeypatch):
    make_server("pytest-a")
    other = make_server("pytest-b", port=25566)
    forwarding(monkeypatch)
    assert network.display_address(other)["internet"] is None


def test_wifi_label_for_a_screen(make_server, mac):
    s = make_server()
    assert network.wifi_label(network.display_address(s)) == f"{LAN}:25565"
    mode("hidden")
    assert network.wifi_label(network.display_address(s)) == "hidden"
    mode("local")
    assert network.wifi_label(network.display_address(s)) == "name not published"


# ---------- proof that hidden mode works ----------
def decode_printf(octal_string):
    """What `printf` would write for a string of \\NNN escapes."""
    out, i = bytearray(), 0
    while i < len(octal_string):
        assert octal_string[i] == "\\"
        out.append(int(octal_string[i + 1:i + 4], 8))
        i += 4
    return bytes(out)


def test_the_chain_probe_sends_a_real_status_request_through_the_forwarder(monkeypatch):
    ran = []

    def docker(*args, **kw):
        ran.append(args)
        reply = b"\x80\x01\x00\x7b" + b'{"version":{"name":"26.3"},"players":{"max":20}}'
        return subprocess.CompletedProcess(args, 0, stdout=reply.hex() + "\n", stderr="")      # the container prints hex
    monkeypatch.setattr(dk, "docker", docker)
    ok, detail = share.probe_chain(25565)
    assert ok and "reached the server" in detail
    args = ran[0]
    assert args[:3] == ("exec", vpn.FORWARD, "sh") and "socat" in args[-1] and "TCP:127.0.0.1:25565" in args[-1]
    octal = args[-1].split("printf '")[1].split("'")[0]
    packet = decode_printf(octal)
    assert packet == (speedtest._packet(0x00, speedtest._varint(speedtest.PROTOCOL) + speedtest._varint(9) + b"localhost"
                                        + (25565).to_bytes(2, "big") + speedtest._varint(1)) + speedtest._packet(0x00))


def test_the_chain_probe_tells_a_dead_forwarder_from_a_silent_server(monkeypatch):
    monkeypatch.setattr(dk, "docker", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"))
    monkeypatch.setattr(dk, "state", lambda name: "exited")
    assert share.probe_chain(25565) == (False, "the VPN's forwarder isn't running")
    monkeypatch.setattr(dk, "state", lambda name: "running")
    assert share.probe_chain(25565) == (False, "the forwarder couldn't get an answer from the server")


# ---------- the doctor ----------
def doctor(servers_, monkeypatch, vpn_on=False, docker=False):
    monkeypatch.setattr(network.platform, "vpn_active", lambda: vpn_on)
    monkeypatch.setattr(network, "installed_javas", lambda: [25])
    monkeypatch.setattr(core.Server, "accepting", lambda self: True)
    monkeypatch.setattr(core.Server, "state", lambda self, sessions=None: "running")
    monkeypatch.setattr(core.Server, "run", lambda self, cmd, wait=1.5: "")
    monkeypatch.setattr(core.Server, "rcon", lambda self: object())
    monkeypatch.setattr(dk, "available", lambda: docker)
    return network.doctor(servers_)


def test_doctor_in_hidden_mode_wants_the_wifi_to_be_closed(make_server, mac, monkeypatch):
    s = make_server()
    mode("hidden")
    monkeypatch.setattr(network, "port_open", lambda host, port, timeout=1.5: False)
    good = [c for c in doctor([s], monkeypatch) if "hidden mode" in c.message]
    assert len(good) == 1 and good[0].good and LAN not in good[0].message
    monkeypatch.setattr(network, "port_open", lambda host, port, timeout=1.5: True)
    bad = [c for c in doctor([s], monkeypatch) if "still reachable from the Wi-Fi" in c.message]
    assert bad and not bad[0].good and "restart" in bad[0].fix and LAN not in bad[0].message


def test_doctor_checks_hidden_mode_end_to_end_when_the_vpn_forwards_this_server(make_server, mac, monkeypatch):
    s = make_server()
    mode("hidden")
    monkeypatch.setattr(network, "port_open", lambda host, port, timeout=1.5: False)
    forwarding(monkeypatch)
    monkeypatch.setattr(vpn, "status", lambda: {"docker": True, "vpn_health": "healthy", "containers": {vpn.VPN: "running"},
                                                 "forward_server": s.name, "public": "203.0.113.2:54321", "exit": "Amsterdam"})
    monkeypatch.setattr(network.dk, "state", lambda name: "running")
    monkeypatch.setattr(network.dk, "inspect", lambda name, fresh=False: None)
    monkeypatch.setattr(share, "probe_chain", lambda port: (True, "the VPN's forwarder reached the server"))
    checks = doctor([s], monkeypatch, docker=True)
    assert any(c.good and "works end to end" in c.message for c in checks)
    monkeypatch.setattr(share, "probe_chain", lambda port: (False, "the forwarder couldn't get an answer from the server"))
    bad = [c for c in doctor([s], monkeypatch, docker=True) if not c.good and "can't reach" in c.message]
    assert bad and "mc share mode local" in bad[0].fix


def test_doctor_in_local_mode_checks_the_name_and_never_prints_the_number(make_server, mac, monkeypatch):
    s = make_server()
    mode("local")
    monkeypatch.setattr(network, "port_open", lambda host, port, timeout=1.5: True)
    monkeypatch.setattr(share, "local_hostname", lambda: "Alexs-MacBook-Pro")
    checks = doctor([s], monkeypatch)
    assert any(not c.good and "isn't published" in c.message for c in checks)
    assert any(not c.good and "looks like it contains a name" in c.message for c in checks)
    assert any(c.good and f"reachable on Wi-Fi at {share.host()}:25565" in c.message for c in checks)
    assert not any(LAN in c.message or LAN in c.fix for c in checks)
    share.bonjour_ensure()
    monkeypatch.setattr(share, "local_hostname", lambda: "studio-7")
    again = doctor([s], monkeypatch)
    assert any(c.good and "is published" in c.message for c in again) and not any("contains a name" in c.message for c in again)


def test_doctor_in_the_default_mode_is_unchanged(make_server, mac, monkeypatch):
    s = make_server()
    monkeypatch.setattr(network, "port_open", lambda host, port, timeout=1.5: True)
    checks = doctor([s], monkeypatch)
    assert any(c.good and f"reachable on Wi-Fi at {LAN}:25565" in c.message for c in checks)


# ---------- everything else that prints an address ----------
def test_the_diagnostics_bundle_masks_addresses_but_not_loopback_or_versions():
    text = "listening on 10.77.88.99:25565, public 203.0.113.2, gateway 192.168.65.254, local 127.0.0.1, Minecraft 1.21.4, Java 21.0.5"
    clean = support.redact(text)
    assert "10.77.88.99" not in clean and "203.0.113.2" not in clean and "192.168.65.254" not in clean
    assert clean.count("<ip>") == 3 and "127.0.0.1" in clean and "1.21.4" in clean and "21.0.5" in clean


def test_the_speed_test_never_prints_the_wifi_address_in_hidden_or_local_mode(make_server, mac, monkeypatch):
    s = make_server()
    monkeypatch.setattr(core.Server, "accepting", lambda self: True)
    monkeypatch.setattr(speedtest, "server_tick", lambda srv: None)
    monkeypatch.setattr(speedtest, "players_latency", lambda srv: [])
    pinged = []
    monkeypatch.setattr(speedtest, "measure_address", lambda label, host, port, samples=8, timeout=2.0:
                        pinged.append(host) or {"label": label, "address": f"{host}:{port}", "samples": samples, "ok": 1})
    said = []
    mode("local")
    share.bonjour_ensure()
    report = speedtest.run(s, 1, say=said.append)
    assert LAN in pinged                                                 # the real address is still what gets pinged
    assert LAN not in json.dumps(report) and LAN not in " ".join(said)
    wifi = next(a for a in report["addresses"] if a["label"] == speedtest.WIFI)
    assert wifi["address"] == f"{share.host()}:25565"
    mode("hidden")
    pinged.clear()
    said.clear()
    report = speedtest.run(s, 1, say=said.append)
    wifi = next(a for a in report["addresses"] if a["label"] == speedtest.WIFI)
    assert LAN not in json.dumps(report) and LAN not in " ".join(said) and "hidden mode" in wifi["skipped"]
    assert LAN not in pinged                                             # nothing to ping: the server isn't on the Wi-Fi


def test_a_changed_internet_address_posts_the_whole_invite_once_a_mode_is_chosen(make_server, mac, monkeypatch):
    make_server()
    forwarding(monkeypatch, "203.0.113.2:54321")
    state = {"vpn_address": "203.0.113.9:1111"}
    [(level, _, text)] = alerts.check_vpn(state, 10_000, address="203.0.113.2:54321")
    assert "changed to 203.0.113.2:54321" in text and "Join my Minecraft server" not in text   # no mode: as before
    mode("hidden")
    state = {"vpn_address": "203.0.113.9:1111"}
    [(level, _, text)] = alerts.check_vpn(state, 20_000, address="203.0.113.2:54321")
    assert "Join my Minecraft server 'pytest-a'" in text and "From anywhere: 203.0.113.2:54321" in text and LAN not in text


# ---------- command line ----------
def run_cli(*args):
    from typer.testing import CliRunner
    from mcctl.cli import app
    return CliRunner().invoke(app, list(args))


def test_cli_share_mode_shows_choices_and_switches(make_server, mac, monkeypatch):
    s = make_server()
    out = run_cli("share", "mode").output
    assert "not chosen" in out and "hidden" in out and "local" in out and "mc share mode hidden|local" in out
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: True)
    r = run_cli("share", "mode", "hidden")
    assert r.exit_code == 0, r.output
    assert "[ok] Share mode: hidden" in r.output and f"Restart '{s.name}' to apply it" in r.output and "restart --in 2m" in r.output
    assert share.mode() == "hidden" and "hidden" in run_cli("share", "mode").output.split("\n")[0]
    assert run_cli("share", "mode", "everything").exit_code != 0


def test_cli_share_and_info_follow_the_mode(make_server, mac, monkeypatch):
    s = make_server()
    forwarding(monkeypatch)
    assert LAN in run_cli("-s", s.name, "info").output and LAN in run_cli("-s", s.name, "share").output
    mode("hidden")
    info_out, share_out = run_cli("-s", s.name, "info").output, run_cli("-s", s.name, "share").output
    assert LAN not in info_out and LAN not in share_out
    assert "203.0.113.2:54321" in info_out and "203.0.113.2:54321" in share_out and "hidden" in info_out
    assert LAN in run_cli("-s", s.name, "share", "--show-ip").output


# ---------- the dashboard ----------
def rendered_lines(app):
    """Everything the dashboard is drawing right now, as text lines."""
    return ["".join(seg.text for seg in strip) for strip in app.screen._compositor.render_strips()]


async def eventually(pilot, condition, seconds=10):
    """Poll `condition()` until it holds, then return True (False if it never did in time). The dashboard redraws on its own schedule,
    so a fixed pause here flaked whenever the machine was busy."""
    for _ in range(int(seconds / 0.1)):
        if condition():
            return True
        await pilot.pause(0.1)
    return condition()


def drawn(app):
    return "\n".join(rendered_lines(app))


def test_dashboard_play_together_card_follows_the_mode_and_its_buttons_switch_it(make_server, wifi, monkeypatch):
    from mcctl.tui import app as tui
    from test_tui import ready
    s = make_server("pytest-card")
    chosen = []
    monkeypatch.setattr(share, "set_mode", lambda new, say=None: chosen.append(new) or [])

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 60)) as pilot:
            await ready(app, pilot)
            app.action_tab("network")
            assert await eventually(pilot, lambda: "pick how friends find you" in drawn(app))
            assert "Not chosen yet" in drawn(app)
            app.query_one("#net-share-hidden").press()
            assert await eventually(pilot, lambda: chosen == ["hidden"])
            app.query_one("#net-share-local").press()
            assert await eventually(pilot, lambda: chosen == ["hidden", "local"])
            mode("hidden")
            app._net = None
            assert await eventually(pilot, lambda: "hidden: nobody can see you" in drawn(app))
            text = drawn(app)
            assert "( -_- )" in text
            assert "[ ] " in text or "[ok] " in text                        # the checklist is there
            # the status bar says so on every tab
            assert await eventually(pilot, lambda: any("pytest-card" in line and "│  hidden" in line for line in rendered_lines(app)))
    asyncio.run(go())


def test_hidden_mode_never_puts_the_wifi_address_on_any_dashboard_tab(make_server, wifi, monkeypatch):
    from textual.widgets import TabbedContent, TabPane
    from mcctl.tui import app as tui
    from test_tui import ready
    s = make_server("pytest-leak")
    forwarding(monkeypatch)
    monkeypatch.setattr(vpn, "status", lambda: {"docker": True, "vpn_health": "healthy", "containers": {vpn.VPN: "running"},
                                                 "forward_server": "pytest-a", "public": "203.0.113.2:54321", "exit": "Amsterdam"})

    async def scan(shows_it):
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(170, 60)) as pilot:
            await ready(app, pilot)
            hits = {}
            for pane in app.query_one("#tabs", TabbedContent).query(TabPane):
                app.action_tab(pane.id.removeprefix("tab-"))
                await pilot.pause(0.7)
                hits[pane.id] = any(LAN in line for line in rendered_lines(app))
            return hits
    default = asyncio.run(scan(True))
    assert any(default.values()), "the scan must be able to see the address in the default mode, or it proves nothing"
    mode("hidden")
    hidden = asyncio.run(scan(False))
    assert not any(hidden.values()), [tab for tab, leaked in hidden.items() if leaked]
