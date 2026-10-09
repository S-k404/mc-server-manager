"""`mc vpn test`: the step-by-step trace of the internet path. Docker is faked; these run offline."""
import subprocess
from types import SimpleNamespace

import pytest

from mcctl import container as dk, platform, voice, vpn

PROC_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:63DD 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 11 1 0
   1: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12 1 0
   2: 0200000A:C8B2 0100007F:63DD 01 00000000:00000000 00:00000000 00000000     0        0 13 1 0
"""
NAT = """-P PREROUTING ACCEPT
-A PREROUTING -i tun0 -p tcp -m tcp --dport 43318 -j REDIRECT --to-ports 25565
-A PREROUTING -i tun0 -p udp -m udp --dport 43318 -j REDIRECT --to-ports 25565
"""


def test_listening_ports_reads_only_listening_sockets():
    assert vpn.listening_ports(PROC_TCP) == {25565, 8080}   # 0x63DD, 0x1F90; the ESTABLISHED one is ignored


def test_redirects_reads_gluetuns_nat_rules():
    assert vpn.redirects(NAT) == {43318: 25565}
    assert vpn.redirects("-P PREROUTING ACCEPT\n") == {}


@pytest.mark.parametrize("error, word", [(ConnectionRefusedError(), "refused"), (ConnectionResetError(), "reset"),
                                         (TimeoutError(), "timed out"), (ConnectionError("closed"), "closed")])
def test_reach_names_how_it_failed(monkeypatch, error, word):
    from mcctl import speedtest

    def boom(host, port, timeout=2.0):
        raise error
    monkeypatch.setattr(speedtest, "status_ping_once", boom)
    worked, why = vpn.reach("203.0.113.5", 43318)
    assert not worked and word in why


def test_reach_reports_a_working_address(monkeypatch):
    from mcctl import speedtest
    monkeypatch.setattr(speedtest, "status_ping_once", lambda h, p, timeout=2.0: {"connect": 40.0, "status": 20.0})
    assert vpn.reach("203.0.113.5", 43318) == (True, "answered in 60 ms")


INPUT_RULES = """-P INPUT DROP
-A INPUT -i lo -j ACCEPT
-A INPUT -i tun0 -p tcp -m tcp --dport 43318 -j ACCEPT
-A INPUT -i tun0 -p tcp -m tcp --dport 25565 -j ACCEPT
"""
REPLY = (b"\xa3\x01\x00\x9f" + b'{"version":{"name":"26.3","protocol":800},"players":{"max":20,"online":0}}').hex()   # as `od` prints it


@pytest.fixture
def stack(monkeypatch):
    """A healthy stack by default; tests break one hop at a time."""
    s = SimpleNamespace(state={vpn.VPN: "running", vpn.FORWARD: "running"}, status={
        "vpn_health": "healthy", "exit": "Amsterdam, NL (203.0.113.5)", "exit_ip": "203.0.113.5", "port": 43318,
        "public": "203.0.113.5:43318"}, nat=NAT, tcp=PROC_TCP,
        reach=(True, "answered in 60 ms"), reach_local=(True, "answered in 3 ms"), server_up=True, upstream=(0, ""),
        replies={"direct": REPLY, "listener": REPLY},
        input=INPUT_RULES, mtu="1400\n", voice_installed=False, udp="")

    def fake_docker(*args, **kw):
        if args[2] == "socat":                   # the forwarder opening its own connection to Docker's host address
            return subprocess.CompletedProcess(args, s.upstream[0], "", s.upstream[1])
        if args[2] == "sh":                      # a status request sent from inside the forwarder
            return subprocess.CompletedProcess(args, 0, s.replies["listener" if "TCP:127.0.0.1" in args[4] else "direct"], "")
        if args[2] == "iptables" and "INPUT" in args:
            return subprocess.CompletedProcess(args, 0 if s.input is not None else 1, s.input or "", "")
        if args[2] == "cat" and "/proc/net/udp" in args[3]:
            return subprocess.CompletedProcess(args, 0, s.udp, "")
        if args[2] == "cat" and "/sys/class/net" in args[3]:
            return subprocess.CompletedProcess(args, 0, s.mtu, "")
        out = {"iptables": s.nat, "cat": s.tcp}.get(args[2])
        return subprocess.CompletedProcess(args, 0 if out is not None else 1, out or "", "")
    monkeypatch.setattr(dk, "available", lambda: True)
    monkeypatch.setattr(dk, "docker", fake_docker)
    monkeypatch.setattr(dk, "state", lambda name: s.state[name])
    monkeypatch.setattr(dk, "inspect", lambda name, fresh=False: {"Config": {"Cmd": [
        "-c", "socat TCP-LISTEN:25565,fork,reuseaddr TCP:192.168.65.254:25565 & wait"]}})
    monkeypatch.setattr(dk, "image_built", lambda image: "2025-01-02T03:04:05Z")
    monkeypatch.setattr(vpn, "configured", lambda: True)
    monkeypatch.setattr(vpn, "status", lambda: s.status)
    monkeypatch.setattr(vpn, "_forward_port", lambda: 25565)
    monkeypatch.setattr(vpn, "logs", lambda name, lines=40: "socat: bind failed\nsecond line")
    monkeypatch.setattr(voice, "installed", lambda srv: s.voice_installed)
    monkeypatch.setattr(vpn, "forward_server", lambda: "main")
    monkeypatch.setattr(vpn, "all_servers", lambda: [SimpleNamespace(name="main", up=lambda: s.server_up, prop=lambda k, d="": d)])
    monkeypatch.setattr(vpn, "reach", lambda host, port, timeout=6: s.reach_local if host == "127.0.0.1" else s.reach)
    monkeypatch.setattr(platform, "vpn_active", lambda: False)
    return s


def failed(steps):
    return [st.what for st in steps if st.ok is False]


def test_trace_of_a_healthy_stack_has_no_failures(stack):
    steps = vpn.trace()
    assert failed(steps) == [] and len(steps) >= 13
    assert any(st.what.startswith("Joining 203.0.113.5:43318") and st.ok for st in steps)


def test_trace_stops_where_proton_gave_no_port(stack):
    stack.status = {**stack.status, "port": None, "public": None}
    steps = vpn.trace()
    assert failed(steps) == ["Proton gave a forwarded port"]
    assert steps[-1].what == "Proton gave a forwarded port" and "NAT-PMP" in steps[-1].fix


def test_trace_flags_a_tunnel_that_carries_nothing(stack):
    stack.status = {**stack.status, "exit": None, "exit_ip": None}
    assert failed(vpn.trace()) == ["Traffic passes through the tunnel"]


def test_trace_flags_a_missing_redirect_when_nothing_listens_on_protons_port(stack):
    stack.nat = "-P PREROUTING ACCEPT\n"
    steps = vpn.trace()
    assert failed(steps) == ["Connections to 43318 reach the forwarder"]
    detail = next(st.detail for st in steps if st.what.startswith("Connections to"))
    assert "no redirect and no listener" in detail and "2025-01-02" in detail    # the image's age says if it is too old


def test_trace_accepts_the_forwarder_listening_on_protons_port_itself(stack):
    stack.nat = "-P PREROUTING ACCEPT\n"
    stack.tcp = PROC_TCP + "   3: 00000000:A936 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 14 1 0\n"
    steps = vpn.trace()                                                         # 0xA936 = 43318
    assert failed(steps) == []
    assert "listens on 43318 itself" in next(st.detail for st in steps if st.what.startswith("Connections to"))


def test_trace_names_why_the_forwarder_cannot_open_its_upstream_connection(stack):
    stack.upstream = (1, "E connect(5, AF=2 192.168.65.254:25565, 16): Connection refused")
    steps = vpn.trace()
    assert failed(steps) == ["Forwarder opens a connection to 192.168.65.254:25565 (Docker's address for this Mac)"]
    assert "Connection refused" in next(st.detail for st in steps if st.what.startswith("Forwarder opens"))


def test_trace_flags_a_dead_forwarder_with_its_log(stack):
    stack.state[vpn.FORWARD] = "exited"
    stack.tcp = PROC_TCP.replace("63DD", "0050")
    steps = vpn.trace()
    assert failed(steps) == ["Forwarder container running", "Something listens on 25565 inside the VPN"]
    assert "socat: bind failed | second line" in next(st.detail for st in steps if st.what.startswith("Forwarder"))


def test_trace_flags_a_stopped_server_and_skips_the_probes(stack):
    stack.server_up = False
    stack.replies = {"direct": "should not be asked", "listener": "should not be asked"}
    steps = vpn.trace()
    assert failed(steps) == ["Server 'main' is running"]
    assert not any("status request" in st.what.lower() for st in steps)


def test_trace_separates_a_server_that_does_not_answer_from_a_docker_path_that_loses_the_answer(stack):
    stack.replies = {"direct": "", "listener": ""}                     # Docker connects, nothing comes back
    steps = vpn.trace()
    assert failed(steps) == ["Status request from the forwarder straight to 192.168.65.254:25565",
                             "Status request through the forwarder's own listener (127.0.0.1:25565)"]
    assert "nothing came back within 5 seconds" in next(st.detail for st in steps if "straight to" in st.what)
    stack.reach_local = (False, "timed out: the packets never come back")      # now the server itself is the problem
    assert "Server answers a status request on this Mac (127.0.0.1:25565)" in failed(vpn.trace())


def test_trace_keeps_socats_own_error_from_the_forwarder_probe(stack):
    stack.replies = {"direct": b"E connect(5, AF=2 192.168.65.254:25565, 16): Connection refused".hex(), "listener": REPLY}
    steps = vpn.trace()
    assert failed(steps) == ["Status request from the forwarder straight to 192.168.65.254:25565"]
    assert "Connection refused" in next(st.detail for st in steps if "straight to" in st.what)


def test_trace_explains_a_failed_self_test_while_the_mac_vpn_is_on(stack, monkeypatch):
    stack.reach = (False, "timed out: the packets never come back")
    monkeypatch.setattr(platform, "vpn_active", lambda: True)
    last = vpn.trace()[-1]
    assert last.ok is False and "Proton -> Proton" in last.detail


def test_trace_marks_an_unreadable_firewall_as_unknown_not_failed(stack):
    stack.nat = None
    steps = vpn.trace()
    assert failed(steps) == [] and any(st.ok is None for st in steps)


def test_trace_without_docker_stops_at_once(monkeypatch):
    monkeypatch.setattr(dk, "available", lambda: False)
    assert [st.what for st in vpn.trace()] == ["Docker answers"]


def test_cli_vpn_test_exits_nonzero_and_names_the_failing_hop(monkeypatch):
    from typer.testing import CliRunner
    from mcctl.cli import app
    monkeypatch.setattr(vpn, "trace", lambda: [vpn.Step(True, "Docker answers", "", ""),
                                               vpn.Step(False, "Proton gave a forwarded port", "none", "mc vpn setup")])
    result = CliRunner().invoke(app, ["vpn", "test"])
    assert result.exit_code == 1
    assert "[x] Proton gave a forwarded port" in result.output and "Stops at: Proton gave a forwarded port" in result.output
    assert "[ok] Docker answers" in result.output


GLUETUN_LOG = """2026-10-07T10:00:01Z INFO [vpn] connected
\x1b[33m2026-10-07T10:00:09Z WARN [port forwarding] natpmp: getting external IPv4 address: read udp 10.2.0.2:41234->10.2.0.1:5351: i/o timeout\x1b[0m
2026-10-07T10:00:10Z INFO [healthcheck] healthy
"""


def test_forward_clues_keeps_only_port_forwarding_lines_without_colour_codes(monkeypatch):
    monkeypatch.setattr(vpn, "logs", lambda name, lines=40: GLUETUN_LOG)
    clues = vpn.forward_clues()
    assert len(clues) == 1 and "i/o timeout" in clues[0] and "\x1b" not in clues[0]


def test_trace_shows_gluetuns_own_reason_when_there_is_no_port(stack, monkeypatch):
    stack.status = {**stack.status, "port": None, "public": None}
    monkeypatch.setattr(vpn, "logs", lambda name, lines=40: GLUETUN_LOG)
    step = vpn.trace()[-1]
    assert step.ok is False and "gluetun said:" in step.detail and "i/o timeout" in step.detail


def test_status_gets_the_public_address_from_gluetuns_log_when_ipinfo_fails(monkeypatch):
    monkeypatch.setattr(dk, "available", lambda: True)
    monkeypatch.setattr(dk, "state", lambda name: "running")
    monkeypatch.setattr(dk, "health", lambda name: "healthy")
    monkeypatch.setattr(dk, "started_at", lambda name: 1.0)
    monkeypatch.setattr(vpn, "configured", lambda: True)
    monkeypatch.setattr(vpn, "forward_server", lambda: "main")
    monkeypatch.setattr(vpn, "forwarded_port", lambda: 43318)
    monkeypatch.setattr(vpn, "_lookup_exit", lambda started: None)           # the container can't reach ipinfo.io
    monkeypatch.setattr(vpn, "port_open", lambda *a, **k: True)
    monkeypatch.setattr(vpn, "_write_cache", lambda public, server: None)
    monkeypatch.setattr(vpn, "_gateway_cache", {})
    log = "INFO [port forwarding] gateway external IPv4 address is 203.0.113.25\nINFO [port forwarding] port forwarded is 43318"
    monkeypatch.setattr(vpn, "logs", lambda name, lines=40: log)
    assert vpn.status()["public"] == "203.0.113.25:43318"


def test_status_prefers_ipinfo_and_a_failed_lookup_is_not_repeated_every_refresh(monkeypatch):
    calls = []

    def fake(*args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "wget: bad address")
    monkeypatch.setattr(dk, "docker", fake)
    monkeypatch.setattr(vpn, "_exit_cache", {})
    assert vpn._lookup_exit(5.0) is None and vpn._lookup_exit(5.0) is None
    assert len(calls) == 1
    ok_reply = subprocess.CompletedProcess((), 0, '{"ip": "1.2.3.4", "city": "Amsterdam", "country": "IN"}', "")
    monkeypatch.setattr(dk, "docker", lambda *a, **k: ok_reply)
    assert vpn._lookup_exit(6.0)["city"] == "Amsterdam"                       # a new container start asks again


def test_no_port_message_is_only_for_no_port():
    assert "NAT-PMP" in vpn.NEED_NATPMP and "NAT-PMP" not in vpn.NO_IP


def test_stop_all_is_a_command_and_one_failure_does_not_leave_the_rest_running(make_server, monkeypatch):
    from typer.testing import CliRunner
    from mcctl import servers
    from mcctl.cli import app
    from mcctl.core import Fail
    for name in ("a", "b", "c"):
        make_server(name)
    stopped = []

    def fake_stop(s, backup=None, wait=True):
        if s.name == "a":
            raise Fail("'a' did not stop within 2 minutes.")
        stopped.append(s.name)
    monkeypatch.setattr(servers, "stop", fake_stop)
    result = CliRunner().invoke(app, ["stop", "all"])
    assert sorted(stopped) == ["b", "c"]                                   # a failed, b and c were still stopped
    assert result.exit_code != 0 and "did not stop" in (result.output + str(result.exception))
    stopped.clear()
    assert CliRunner().invoke(app, ["stop", "b,c"]).exit_code == 0 and sorted(stopped) == ["b", "c"]
    stopped.clear()
    assert CliRunner().invoke(app, ["-s", "c", "stop"]).exit_code == 0 and stopped == ["c"]   # the old spelling still works


def test_server_down_note_explains_connection_reset(monkeypatch):
    up = {"main": False}
    monkeypatch.setattr(vpn, "forward_server", lambda: "main")
    monkeypatch.setattr(vpn, "all_servers", lambda: [SimpleNamespace(name="main", up=lambda: up["main"])])
    assert "Connection reset" in vpn.server_down_note() and "mc -s main start" in vpn.server_down_note()
    up["main"] = True
    assert vpn.server_down_note() is None
    monkeypatch.setattr(vpn, "all_servers", lambda: [])
    assert vpn.server_down_note() is None


def test_probe_in_forwarder_sends_a_real_status_request_and_reads_the_answer(monkeypatch):
    seen = []

    def fake(*args, **kw):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, REPLY, "")
    monkeypatch.setattr(dk, "docker", fake)
    assert vpn.probe_in_forwarder("192.168.65.254", 25565) == (True, "the server answered")
    cmd = seen[0][4]
    assert seen[0][:3] == ("exec", vpn.FORWARD, "sh") and "TCP:192.168.65.254:25565,connect-timeout=4" in cmd
    assert cmd.startswith("printf '\\") and "localhost".encode().hex() not in cmd     # bytes are sent as octal escapes
    monkeypatch.setattr(dk, "docker", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    worked, why = vpn.probe_in_forwarder("192.168.65.254", 25565)
    assert not worked and "nothing came back" in why


def test_cli_vpn_test_keeps_each_hop_on_one_line_even_when_piped(monkeypatch):
    from typer.testing import CliRunner
    from mcctl.cli import app
    long = "it said: " + "E connect(5, AF=2 192.168.65.254:25565, 16): Connection refused " * 3
    monkeypatch.setattr(vpn, "trace", lambda: [vpn.Step(False, "Status request from the forwarder straight to 192.168.65.254:25565", long, "")])
    out = CliRunner().invoke(app, ["vpn", "test"]).output.splitlines()
    assert any(ln.startswith("[x] Status request") and ln.rstrip().endswith("Connection refused") for ln in out)


def test_the_forwarder_probe_does_not_depend_on_tr_character_classes(monkeypatch):
    """The forwarder image's `tr` took '[:print:]' as a literal set of characters, which deleted the very text the probe
    looks for, so every probe failed whatever the server did. The container now prints hex and Python decodes it."""
    seen = []

    def fake(*args, **kw):
        seen.append(args[4])
        return subprocess.CompletedProcess(args, 0, "a301009f" + b'{"version":{"name":"26.3"}}'.hex() + "\n", "")
    monkeypatch.setattr(dk, "docker", fake)
    assert vpn.probe_in_forwarder("127.0.0.1", 25565) == (True, "the server answered")     # a non-ASCII length prefix is fine
    assert "[:print:]" not in seen[0] and "od -An -v -tx1" in seen[0]
    monkeypatch.setattr(dk, "docker", lambda *a, **k: subprocess.CompletedProcess(a, 0, "sh: od: not found", ""))
    assert vpn.probe_in_forwarder("127.0.0.1", 25565) == (False, "it said: sh: od: not found")   # a shell error is shown, not hidden


def test_accepted_ports_reads_input_accept_rules():
    assert vpn.accepted_ports(INPUT_RULES) == {43318, 25565}
    assert vpn.accepted_ports("-P INPUT DROP\n-A INPUT -p tcp --dport 80 -j DROP\n") == set()


def test_trace_checks_that_gluetuns_firewall_lets_the_port_in(stack):
    steps = vpn.trace()
    assert next(st for st in steps if st.what.startswith("gluetun's firewall")).ok is True
    stack.nat = "-P PREROUTING ACCEPT\n"                   # no redirect: the packet keeps Proton's port, which must be open
    stack.tcp = PROC_TCP + "   3: 00000000:A936 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 14 1 0\n"
    stack.input = "-P INPUT DROP\n-A INPUT -i lo -j ACCEPT\n"
    steps = vpn.trace()
    firewall = next(st for st in steps if st.what.startswith("gluetun's firewall"))
    assert firewall.what == "gluetun's firewall lets port 43318 in" and firewall.ok is None       # unknown, not a stop
    assert "INPUT rules: -A INPUT -i lo -j ACCEPT" in firewall.detail
    assert failed(steps) == []


def test_trace_warns_when_big_packets_run_inside_the_mac_vpn(stack, monkeypatch):
    monkeypatch.setattr(platform, "vpn_active", lambda: True)
    step = next(st for st in vpn.trace() if st.what.startswith("Tunnel packet size"))
    assert step.ok is None and "MTU 1400" in step.what and "mc vpn mtu 1280" in step.fix
    stack.mtu = "1280\n"
    assert next(st for st in vpn.trace() if st.what.startswith("Tunnel packet size")).ok is True
    monkeypatch.setattr(platform, "vpn_active", lambda: False)
    stack.mtu = "1400\n"
    assert next(st for st in vpn.trace() if st.what.startswith("Tunnel packet size")).ok is True      # alone, 1400 is fine


def test_mtu_is_smaller_inside_the_mac_vpn_unless_chosen(monkeypatch):
    from mcctl import platform as plat
    monkeypatch.setattr(plat, "vpn_active", lambda: False)
    assert vpn.mtu() is None and vpn._mtu_env() == []
    monkeypatch.setattr(plat, "vpn_active", lambda: True)
    assert vpn.mtu() == 1280 and vpn._mtu_env() == ["-e", "WIREGUARD_MTU=1280"]
    vpn.set_mtu("1200")
    assert vpn.mtu() == 1200
    vpn.set_mtu("auto")
    assert vpn.mtu() == 1280
    for bad in ("999", "1500", "big", "-5"):
        with pytest.raises(Exception, match="between 1000 and 1420"):
            vpn.set_mtu(bad)


def test_cli_vpn_mtu_shows_and_sets(monkeypatch):
    from typer.testing import CliRunner
    from mcctl import platform as plat
    from mcctl.cli import app
    monkeypatch.setattr(plat, "vpn_active", lambda: True)
    assert "1280" in CliRunner().invoke(app, ["vpn", "mtu"]).output
    assert CliRunner().invoke(app, ["vpn", "mtu", "1250"]).exit_code == 0 and vpn.mtu() == 1250


# ---------- self-repair ----------
@pytest.fixture
def recreated(monkeypatch):
    calls = []
    monkeypatch.setattr(vpn, "start", lambda wait=True, patience=120: calls.append("start"))
    monkeypatch.setattr(vpn, "_stuck", {"since": None, "restarted": -10_000.0})
    return calls


def test_a_vpn_that_stays_unhealthy_is_recreated_but_not_at_once_and_not_again_soon(recreated):
    assert vpn._restart_if_stuck("unhealthy", 53715, now=0) is None
    assert vpn._restart_if_stuck("unhealthy", 53715, now=100) is None and recreated == []      # gluetun may mend itself
    assert "stayed unhealthy" in vpn._restart_if_stuck("unhealthy", 53715, now=200)
    assert recreated == ["start"]
    assert vpn._restart_if_stuck("unhealthy", 53715, now=250) is None
    assert vpn._restart_if_stuck("unhealthy", 53715, now=450) is None and recreated == ["start"]   # cooldown: every restart moves the address
    assert vpn._restart_if_stuck("unhealthy", 53715, now=600) is not None and recreated == ["start", "start"]


def test_a_healthy_vpn_that_never_gets_a_port_is_recreated_too_and_recovery_resets_the_clock(recreated):
    assert vpn._restart_if_stuck("healthy", None, now=0) is None
    assert vpn._restart_if_stuck("healthy", 53715, now=150) is None            # recovered: the clock starts over
    assert vpn._restart_if_stuck("healthy", None, now=160) is None
    assert vpn._restart_if_stuck("healthy", None, now=300) is None
    assert "forwarded port" in vpn._restart_if_stuck("healthy", None, now=350)
    assert vpn._restart_if_stuck("starting", None, now=9999) is None           # still coming up: leave it alone


def test_fix_recreates_the_vpn_when_proton_gave_no_port_and_checks_again(stack, monkeypatch):
    stack.status = {**stack.status, "port": None, "public": None}

    def start(wait=True, patience=120):
        stack.status = {**stack.status, "port": 43318, "public": "203.0.113.5:43318"}
    monkeypatch.setattr(vpn, "start", start)
    monkeypatch.setattr(platform, "vpn_active", lambda: True)
    said = []
    steps, actions = vpn.fix(say=said.append)
    assert actions == ["recreated the VPN"] and failed(steps) == [] and said and "Recreating the VPN" in said[0]


def test_fix_leaves_a_stopped_server_alone_unless_told_to_start_it(stack, monkeypatch):
    from mcctl import servers
    stack.server_up = False
    monkeypatch.setattr(vpn, "start", lambda wait=True, patience=120: pytest.fail("the VPN is fine; it must not be recreated"))
    steps, actions = vpn.fix(say=lambda m: None)
    assert actions == [] and failed(steps) == ["Server 'main' is running"]
    started = []
    monkeypatch.setattr(servers, "start", lambda srv: (started.append(srv.name), setattr(stack, "server_up", True)))
    steps, actions = vpn.fix(start_servers=True, say=lambda m: None)
    assert started == ["main"] and actions == ["started 'main'"] and failed(steps) == []


def test_fix_gives_up_after_three_rounds_and_says_so_when_the_mac_vpn_is_off(stack, monkeypatch):
    stack.status = {**stack.status, "vpn_health": "unhealthy", "exit_ip": None, "exit": None, "port": None, "public": None}
    monkeypatch.setattr(vpn, "start", lambda wait=True, patience=120: None)                  # recreating changes nothing: the tunnel stays down
    monkeypatch.setattr(platform, "vpn_active", lambda: False)
    steps, actions = vpn.fix(say=lambda m: None)
    assert actions == ["recreated the VPN"] * 3 and failed(steps)
    assert "managed network" in vpn.fix_hint(steps) and "mc vpn fix" in vpn.fix_hint(steps)
    monkeypatch.setattr(platform, "vpn_active", lambda: True)
    assert vpn.fix_hint(steps) == ""                                           # the hint is only for "Mac VPN off"


def test_fix_reports_a_vpn_that_will_not_start_instead_of_crashing(stack, monkeypatch):
    from mcctl.core import Fail
    stack.state[vpn.VPN] = "exited"

    def boom(wait=True, patience=120):
        raise Fail("The VPN didn't come up within 2 minutes")
    monkeypatch.setattr(vpn, "start", boom)
    steps, actions = vpn.fix(say=lambda m: None)
    assert len(actions) == 1 and actions[0].startswith("could not recreate the VPN: The VPN didn't come up")


def test_cli_vpn_fix_exits_nonzero_with_the_hint_and_zero_when_repaired(monkeypatch):
    from typer.testing import CliRunner
    from mcctl.cli import app
    bad = [vpn.Step(True, "Docker answers", "", ""), vpn.Step(False, "VPN connected (gluetun healthy)", "unhealthy", "mc vpn logs vpn")]
    monkeypatch.setattr(vpn, "fix", lambda start_servers=False, say=None: (bad, ["recreated the VPN"]))
    monkeypatch.setattr(vpn, "fix_hint", lambda steps: "Turn the Mac's Proton on and run: mc vpn fix")
    result = CliRunner().invoke(app, ["vpn", "fix"])
    assert result.exit_code == 1 and "Did: recreated the VPN" in result.output and "Turn the Mac's Proton on" in result.output
    good = [vpn.Step(True, "Docker answers", "", ""), vpn.Step(False, "Joining 1.2.3.4:5 from this Mac", "timed out", "")]
    monkeypatch.setattr(vpn, "fix", lambda start_servers=False, say=None: (good, []))
    monkeypatch.setattr(vpn, "public_address", lambda live=False: "1.2.3.4:5")
    result = CliRunner().invoke(app, ["vpn", "fix"])
    assert result.exit_code == 0 and "Friends join: 1.2.3.4:5" in result.output       # a lone Proton->Proton self-test is not a failure



# ---------- voice chat steps ----------
UDP_TABLE = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops
  2: 00000000:A936 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 21 2 0000000000000000 0
"""
INPUT_WITH_UDP = INPUT_RULES + "-A INPUT -i tun0 -p udp -m udp --dport 43318 -j ACCEPT\n"


@pytest.fixture
def voiced(stack, monkeypatch):
    """The healthy stack with Simple Voice Chat installed, routed through the internet address."""
    from mcctl import moddoctor
    stack.voice_installed, stack.udp, stack.input = True, UDP_TABLE, INPUT_WITH_UDP
    conf = {"port": "24454", "voice_host": "203.0.113.5:43318"}
    monkeypatch.setattr(voice, "props", lambda srv: conf)
    monkeypatch.setattr(voice, "route", lambda srv: "vpn")
    monkeypatch.setattr(voice, "wanted", lambda srv: "203.0.113.5:43318")
    monkeypatch.setattr(voice, "udp_port", lambda srv: 24454)
    monkeypatch.setattr(voice, "_load", lambda: {})
    monkeypatch.setattr(voice, "pending_restart", lambda srv: False)
    monkeypatch.setattr(moddoctor, "_udp_ports", lambda srv: {24454})
    stack.conf = conf
    return stack


def test_voice_chat_hops_all_pass_when_it_is_routed_through_the_internet_address(voiced):
    steps = vpn.trace()
    assert failed(steps) == []
    names = [st.what for st in steps if st.what.startswith("Voice chat")]
    assert names[0] == "Voice chat: audio goes through the internet address" and len(names) == 5


def test_voice_chat_without_the_mod_adds_no_steps(stack):
    assert not any(st.what.startswith("Voice chat") for st in vpn.trace())


def test_a_stale_voice_host_is_a_failure_with_the_command_that_fixes_it(voiced, monkeypatch):
    voiced.conf["voice_host"] = "198.51.100.9:11111"
    monkeypatch.setattr(voice, "_load", lambda: {"main": {"value": "198.51.100.9:11111"}})       # written by mc earlier
    steps = vpn.trace()
    assert failed(steps) == ["Voice chat: voice_host"]
    step = next(st for st in steps if st.what == "Voice chat: voice_host")
    assert "should be 203.0.113.5:43318" in step.detail and "mc vpn voice" in step.fix


def test_a_hand_written_or_pending_voice_host_is_not_a_failure(voiced, monkeypatch):
    voiced.conf["voice_host"] = "voice.example.org:24454"
    step = next(st for st in vpn.trace() if st.what == "Voice chat: voice_host")
    assert step.ok is None and "mc leaves it alone" in step.detail
    voiced.conf["voice_host"] = "203.0.113.5:43318"
    monkeypatch.setattr(voice, "pending_restart", lambda srv: True)
    step = next(st for st in vpn.trace() if st.what == "Voice chat: voice_host")
    assert step.ok is None and "restarts when nobody is on it" in step.detail


def test_the_forwarder_must_listen_for_udp_on_protons_port(voiced):
    voiced.udp = UDP_TABLE.replace("A936", "0050")
    assert failed(vpn.trace()) == ["Voice chat: the forwarder listens for UDP on 43318"]


def test_gluetuns_udp_rule_is_unknown_not_failed_when_missing(voiced):
    voiced.input = INPUT_RULES
    step = next(st for st in vpn.trace() if "gluetun's firewall lets UDP" in st.what)
    assert step.ok is None


def test_the_mod_not_listening_on_this_mac_is_a_failure(voiced, monkeypatch):
    from mcctl import moddoctor
    monkeypatch.setattr(moddoctor, "_udp_ports", lambda srv: {9999})
    assert failed(vpn.trace()) == ["Voice chat: the mod listens on UDP 24454 on this Mac"]


def test_fix_rewrites_voice_host_instead_of_recreating_the_vpn(voiced, monkeypatch):
    voiced.conf["voice_host"] = "198.51.100.9:11111"
    monkeypatch.setattr(voice, "_load", lambda: {"main": {"value": "198.51.100.9:11111"}})
    monkeypatch.setattr(vpn, "start", lambda wait=True, patience=120: pytest.fail("the VPN is fine"))

    def maintain(srv, log=None, now=None):
        voiced.conf["voice_host"] = "203.0.113.5:43318"
        return "voice chat address set to 203.0.113.5:43318"
    monkeypatch.setattr(voice, "maintain", maintain)
    steps, actions = vpn.fix(say=lambda m: None)
    assert actions == ["voice chat address set to 203.0.113.5:43318"] and failed(steps) == []


def test_listening_udp_ports_reads_bound_sockets_only():
    assert voice.listening_udp_ports(UDP_TABLE) == {43318}
    assert voice.listening_udp_ports("  3: 0100007F:0035 0100007F:9999 01 0 0\n") == set()      # a connected socket, not a bound one


def test_accepted_ports_can_tell_udp_from_tcp():
    assert vpn.accepted_ports(INPUT_WITH_UDP, "udp") == {43318}
    assert vpn.accepted_ports(INPUT_WITH_UDP, "tcp") == {43318, 25565}
    assert vpn.accepted_ports(INPUT_RULES, "udp") == set()


# ---------- the three share modes: the trace must never show this Mac's Wi-Fi address ----------
WIFI = "192.168.1.77"


@pytest.mark.parametrize("mode", ["hidden", "local", None])
def test_the_trace_never_shows_the_wifi_address_in_any_share_mode(stack, monkeypatch, mode):
    from mcctl import share
    monkeypatch.setattr(share, "mode", lambda: mode)
    monkeypatch.setattr(platform, "lan_ip", lambda: WIFI)
    monkeypatch.setattr(vpn, "all_servers", lambda: [SimpleNamespace(
        name="main", up=lambda: stack.server_up, prop=lambda k, d="": WIFI if k == "server-ip" else d)])
    stack.server_up = True
    text = " ".join(f"{st.what} {st.detail} {st.fix}" for st in vpn.trace())
    assert WIFI not in text and "listens on one specific address" in text


def test_the_server_bind_is_described_not_quoted(stack, monkeypatch):
    for value, words in (("", "every address"), ("127.0.0.1", "this Mac only"), (WIFI, "one specific address")):
        monkeypatch.setattr(vpn, "all_servers", lambda value=value: [SimpleNamespace(
            name="main", up=lambda: True, prop=lambda k, d="", value=value: value if k == "server-ip" else d)])
        step = next(st for st in vpn.trace() if st.what == "Server 'main' is running")
        assert step.detail == f"listens on {words}"


def test_fix_warns_up_front_when_the_macs_own_vpn_is_off_and_the_tunnel_is_down(stack, monkeypatch):
    stack.status = {**stack.status, "vpn_health": "unhealthy", "exit_ip": None, "exit": None}
    monkeypatch.setattr(vpn, "start", lambda wait=True, patience=120: None)
    monkeypatch.setattr(platform, "vpn_active", lambda: False)
    said = []
    vpn.fix(say=said.append)
    assert said[0].startswith("Heads-up: this Mac's own Proton VPN is off") and "40 seconds" in said[0]
    said.clear()
    monkeypatch.setattr(platform, "vpn_active", lambda: True)
    vpn.fix(say=said.append)
    assert not any(m.startswith("Heads-up") for m in said)


def test_fix_waits_less_for_a_tunnel_that_cannot_connect_while_the_macs_own_vpn_is_off(stack, monkeypatch):
    stack.status = {**stack.status, "vpn_health": "unhealthy", "exit_ip": None, "exit": None}
    waits = []
    monkeypatch.setattr(vpn, "start", lambda wait=True, patience=120: waits.append(patience))
    monkeypatch.setattr(platform, "vpn_active", lambda: False)
    vpn.fix(say=lambda m: None)
    assert waits == [40, 40, 40]
    waits.clear()
    monkeypatch.setattr(platform, "vpn_active", lambda: True)
    vpn.fix(say=lambda m: None)
    assert waits == [120, 120, 120]                          # with the Mac's VPN on, give it the full two minutes


def test_mc_vpn_status_is_the_same_screen_as_plain_mc_vpn(monkeypatch):
    from typer.testing import CliRunner
    from mcctl.cli import app
    st = {"docker": True, "configured": True, "containers": {vpn.VPN: "running", vpn.FORWARD: "running"}, "vpn_health": "unhealthy",
          "forward_server": "main", "proxy_up": True, "port": 43318, "public": "203.0.113.5:43318", "exit": "Amsterdam, NL (203.0.113.5)"}
    monkeypatch.setattr(vpn, "status", lambda: st)
    monkeypatch.setattr(vpn, "server_down_note", lambda: None)
    explicit = CliRunner().invoke(app, ["vpn", "status"])
    plain = CliRunner().invoke(app, ["vpn"])
    assert explicit.exit_code == 0 and explicit.output == plain.output
    assert "203.0.113.5:43318" in explicit.output and "The VPN isn't healthy. Repair it: mc fix" in explicit.output
