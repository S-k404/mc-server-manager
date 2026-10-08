"""Sign-in reachability (`mc net auth`) and the launcher sign-in proxy that runs inside the Docker VPN. Offline."""
import asyncio
import os
import socket
import stat
import threading

import pytest

from mcctl import container as dk
from mcctl import network, support, vpn
from mcctl.core import Fail
from test_docker import KEY

PROXY = {"host": "127.0.0.1", "port": 8888, "user": "mc", "password": "s3cret-pass_word-123", "up": True,
         "url": "http://mc:s3cret-pass_word-123@127.0.0.1:8888"}


# ---------- the probe itself, against real local sockets ----------
def serve_once(reply):
    """A tiny proxy-like TCP server that answers the first request with `reply` (bytes) and closes."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(reply)
        conn.close()
        srv.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def test_probe_reports_a_proxy_that_rejects_the_password():
    port = serve_once(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
    ok, detail = network.probe("login.live.com", {**PROXY, "port": port}, timeout=3)
    assert not ok and "rejected the password" in detail


def test_probe_reports_a_refused_connection():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()                                                    # nothing listens here any more
    ok, detail = network.probe("login.live.com", {**PROXY, "port": port}, timeout=3)
    assert not ok and detail == "refused"


def test_probe_reports_a_name_that_does_not_resolve():
    ok, detail = network.probe("does-not-exist.invalid", timeout=3)
    assert not ok and ("name not found" in detail or "no answer" in detail)


# ---------- what the checks conclude ----------
def fake(blocked=(), details="HTTP 200"):
    return lambda host, proxy=None: (host not in blocked, details if host not in blocked else "connection reset (blocked)")


@pytest.fixture
def net(monkeypatch):
    """No Mac VPN, no Docker VPN: only the direct probes run."""
    monkeypatch.setattr(network.platform, "vpn_active", lambda: False)
    monkeypatch.setattr(vpn, "enabled", lambda: False)


def by_group(checks):
    out = {}
    for c in checks:
        out.setdefault(c.group, []).append(c)
    return out


def test_direct_sign_in_works_means_no_proxy_is_needed(net):
    checks = network.auth_checks(fake())
    assert len(checks) == len(network.AUTH_HOSTS) + 1 and all(c.good for c in checks)
    assert "no VPN or proxy is needed" in checks[-1].message


def test_blocked_sign_in_points_at_the_docker_vpn_and_the_proxy_settings(net):
    checks = network.auth_checks(fake(blocked=("login.live.com", "user.auth.xboxlive.com")))
    bad = [c for c in checks if not c.good]
    assert {c.message.split(" ")[0] for c in bad} == {"login.live.com", "user.auth.xboxlive.com"}
    assert all("mc vpn start" in c.fix and "mc vpn proxy" in c.fix for c in bad)
    assert not any(c.group == "Sign-in" for c in checks)           # no verdict while there is nothing to compare with


def test_a_blocked_session_server_explains_the_online_mode_consequence(net):
    checks = network.auth_checks(fake(blocked=("sessionserver.mojang.com",)))
    [bad] = [c for c in checks if not c.good]
    assert "online-mode server" in bad.fix and "Allow LAN connections" in bad.fix and "mc cracked on" in bad.fix
    assert checks[-1].good and "no VPN or proxy is needed" in checks[-1].message   # sign-in itself is fine


def test_proxy_that_works_turns_a_blocked_wifi_into_a_good_verdict(net, monkeypatch):
    monkeypatch.setattr(vpn, "enabled", lambda: True)
    monkeypatch.setattr(vpn, "proxy_info", lambda: PROXY)

    def probe(host, proxy=None):
        return (True, "HTTP 200") if proxy else (False, "connection reset (blocked)")
    checks = network.auth_checks(probe)
    groups = by_group(checks)
    assert all(not c.good for c in groups["Sign-in: straight from this Mac"])
    assert all(c.good for c in groups["Sign-in: through the Docker VPN proxy"])
    assert "works through the Docker VPN proxy" in groups["Sign-in"][0].message and groups["Sign-in"][0].good
    assert all("use the sign-in proxy" in c.fix for c in groups["Sign-in: straight from this Mac"] if c.fix and "online-mode" not in c.fix)


def test_blocked_both_ways_is_a_problem(net, monkeypatch):
    monkeypatch.setattr(vpn, "enabled", lambda: True)
    monkeypatch.setattr(vpn, "proxy_info", lambda: PROXY)
    checks = network.auth_checks(lambda host, proxy=None: (False, "no answer in 5 s"))
    verdict = by_group(checks)["Sign-in"][0]
    assert not verdict.good and "through the Docker VPN proxy too" in verdict.message and verdict.fix == "mc vpn logs"


def test_a_proxy_that_is_not_answering_is_not_probed(net, monkeypatch):
    monkeypatch.setattr(vpn, "enabled", lambda: True)
    monkeypatch.setattr(vpn, "proxy_info", lambda: {**PROXY, "up": False})
    seen = []
    network.auth_checks(lambda host, proxy=None: seen.append(proxy) or (True, "HTTP 200"))
    assert seen and all(p is None for p in seen)                   # direct only


def test_mac_vpn_on_means_the_direct_result_is_not_measured(monkeypatch):
    monkeypatch.setattr(network.platform, "vpn_active", lambda: True)
    monkeypatch.setattr(vpn, "enabled", lambda: False)
    called = []
    checks = network.auth_checks(lambda host, proxy=None: called.append(host) or (True, "x"))
    assert called == [] and len(checks) == 1 and "not measured" in checks[0].message and checks[0].good


def test_probes_run_in_parallel_not_one_after_another(net):
    import time
    started = []

    def slow(host, proxy=None):
        started.append(time.monotonic())
        time.sleep(0.3)
        return True, "HTTP 200"
    t0 = time.monotonic()
    network.auth_checks(slow)
    assert time.monotonic() - t0 < 1.2 and len(started) == len(network.AUTH_HOSTS)   # 8 x 0.3 s would be 2.4 s


def test_doctor_runs_the_sign_in_probes_only_when_asked(make_server, monkeypatch):
    s = make_server()
    monkeypatch.setattr(network.platform, "vpn_active", lambda: False)
    monkeypatch.setattr(network.platform, "lan_ip", lambda: "10.0.0.5")
    calls = []
    monkeypatch.setattr(network, "auth_checks", lambda: calls.append(1) or [network.Check("Sign-in", True, "fine")])
    assert not any(c.group == "Sign-in" for c in network.doctor([s]))
    assert any(c.group == "Sign-in" for c in network.doctor([s], auth=True)) and calls == [1]


def test_doctor_reports_the_sign_in_proxy(make_server, monkeypatch):
    s = make_server()
    vpn.setup(KEY)
    monkeypatch.setattr(network.platform, "vpn_active", lambda: False)
    monkeypatch.setattr(network.platform, "lan_ip", lambda: "10.0.0.5")
    base = {"docker": True, "vpn_health": "healthy", "exit": "Amsterdam, NL", "containers": {vpn.VPN: "running"},
            "forward_server": s.name, "public": "203.0.113.2:54321"}
    monkeypatch.setattr(vpn, "status", lambda: {**base, "proxy_up": True})
    assert any("sign-in proxy answering" in c.message and c.good for c in network.doctor([s]))
    monkeypatch.setattr(vpn, "status", lambda: {**base, "proxy_up": False})
    bad = [c for c in network.doctor([s]) if not c.good and "sign-in proxy" in c.message]
    assert bad and bad[0].fix == "mc vpn start"


# ---------- the proxy inside the VPN container ----------
def test_proxy_password_is_made_once_and_kept_owner_only():
    assert not vpn.PROXY_FILE.exists() and vpn.proxy_info()["password"] is None   # reading never creates it
    first = vpn._proxy_password()
    assert len(first) >= 20 and vpn._proxy_password() == first
    assert stat.S_IMODE(os.stat(vpn.PROXY_FILE).st_mode) == 0o600
    text = vpn.PROXY_FILE.read_text()
    assert f"HTTPPROXY_USER={vpn.PROXY_USER}\n" in text and f"HTTPPROXY_PASSWORD={first}\n" in text
    assert vpn.proxy_info()["url"] == f"http://mc:{first}@127.0.0.1:8888"


def started_vpn(make_server, monkeypatch, run=None):
    make_server("a", port=25565)
    vpn.setup(KEY)
    ran = []
    monkeypatch.setattr(dk, "require", lambda: None)
    monkeypatch.setattr(dk, "ensure_network", lambda: None)
    monkeypatch.setattr(dk, "pull", lambda image, say=None: None)
    monkeypatch.setattr(dk, "remove", lambda name: None)
    monkeypatch.setattr(dk, "host_gateway_ip", lambda: "192.168.65.254")
    monkeypatch.setattr(dk, "run", run or (lambda *a: ran.append(a)))
    monkeypatch.setattr(dk, "available", lambda: False)
    vpn.start(wait=False)
    return ran


def test_vpn_start_publishes_the_proxy_on_loopback_without_a_secret_on_the_command_line(make_server, monkeypatch):
    ran = started_vpn(make_server, monkeypatch)
    gluetun = next(a for a in ran if a[a.index("--name") + 1] == vpn.VPN)
    assert "HTTPPROXY=on" in gluetun and "HTTPPROXY_STEALTH=on" in gluetun
    assert "127.0.0.1:8888:8888" in gluetun                        # this Mac only, never the Wi-Fi
    assert str(vpn.PROXY_FILE) in gluetun and str(vpn.ENV_FILE) in gluetun
    password = vpn._proxy_password()
    assert not any(password in part for a in ran for part in a)   # handed over as an env file, not an argument
    assert not any("HTTPPROXY_PASSWORD" in part or "HTTPPROXY_USER" in part for a in ran for part in a)


def test_vpn_start_explains_a_busy_proxy_port(make_server, monkeypatch):
    def busy(*a):
        raise Fail("docker run failed: Bind for 127.0.0.1:8888 failed: port is already allocated")
    with pytest.raises(Fail, match="already in use.*lsof -i :8888"):
        started_vpn(make_server, monkeypatch, run=busy)


def test_vpn_start_still_reports_other_docker_errors_unchanged(make_server, monkeypatch):
    def broken(*a):
        raise Fail("docker run failed: no such device /dev/net/tun")
    with pytest.raises(Fail, match="/dev/net/tun"):
        started_vpn(make_server, monkeypatch, run=broken)


def test_proxy_info_needs_a_running_vpn_and_an_answering_port(monkeypatch):
    vpn._proxy_password()
    monkeypatch.setattr(dk, "available", lambda: True)
    monkeypatch.setattr(dk, "state", lambda name: "running")
    monkeypatch.setattr(vpn, "port_open", lambda host, port, timeout=1.5: True)
    assert vpn.proxy_info()["up"] is True
    monkeypatch.setattr(vpn, "port_open", lambda host, port, timeout=1.5: False)
    assert vpn.proxy_info()["up"] is False
    monkeypatch.setattr(dk, "available", lambda: False)             # Docker absent: report "not up", never fail
    monkeypatch.setattr(vpn, "port_open", lambda host, port, timeout=1.5: True)
    assert vpn.proxy_info()["up"] is False


def test_launcher_instructions(monkeypatch):
    assert "mc vpn start" in vpn.proxy_help({"password": None})[0]
    text = "\n".join(vpn.proxy_help(PROXY))
    for want in ("Settings -> Proxy -> HTTP", "127.0.0.1", "8888", "Username  mc", "Password  s3cret-pass_word-123"):
        assert want in text
    assert "NOT answering" not in text
    assert "NOT answering" in "\n".join(vpn.proxy_help({**PROXY, "up": False}))
    assert all(ord(ch) < 128 for ch in text)                        # the project keeps new UI text ASCII


def test_diagnostics_bundle_hides_the_proxy_password():
    leaked = f"launcher proxy {PROXY['url']} and password: {PROXY['password']}"
    clean = support.redact(leaked)
    assert PROXY["password"] not in clean and "http://mc:<hidden>@127.0.0.1:8888" in clean


# ---------- command line and dashboard ----------
def run_cli(*args):
    from typer.testing import CliRunner
    from mcctl.cli import app
    return CliRunner().invoke(app, list(args))


def test_cli_vpn_proxy_prints_what_to_type_into_the_launcher(monkeypatch):
    monkeypatch.setattr(vpn, "proxy_info", lambda: PROXY)
    r = run_cli("vpn", "proxy")
    assert r.exit_code == 0, r.output
    assert "Password  s3cret-pass_word-123" in r.output and "mc net auth" in r.output
    monkeypatch.setattr(vpn, "proxy_info", lambda: {**PROXY, "password": None, "up": False, "url": None})
    assert "mc vpn start" in run_cli("vpn", "proxy").output


def test_cli_net_auth_lists_each_result_and_the_fix(monkeypatch):
    monkeypatch.setattr(network, "auth_checks", lambda: [
        network.Check("Sign-in: straight from this Mac", True, "login.live.com reachable (HTTP 200)"),
        network.Check("Sign-in: straight from this Mac", False, "xsts.auth.xboxlive.com blocked: refused", "mc vpn start")])
    out = run_cli("net", "auth").output
    assert "[ok] login.live.com reachable" in out and "[x]" in out and "-> mc vpn start" in out


def test_dashboard_network_tab_shows_the_proxy_settings_and_the_sign_in_test(make_server, monkeypatch):
    from mcctl.tui import app as tui
    from mcctl.tui.modals import TextResult
    from test_tui import ready
    s = make_server("pytest-signin")
    monkeypatch.setattr(vpn, "proxy_info", lambda: PROXY)
    monkeypatch.setattr(network, "auth_checks", lambda: [
        network.Check("Sign-in: straight from this Mac", False, "login.live.com blocked: refused", "mc vpn start")])

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 60)) as pilot:
            await ready(app, pilot)
            app.action_tab("network")
            await pilot.pause(0.5)
            for button, expected in (("#net-proxy", "Password  s3cret-pass_word-123"), ("#net-auth", "login.live.com blocked")):
                app.query_one(button).press()
                for _ in range(40):
                    await pilot.pause(0.1)
                    if isinstance(app.screen, TextResult):
                        break
                assert isinstance(app.screen, TextResult) and expected in app.screen.body
                await pilot.press("escape")
                await pilot.pause(0.3)
    asyncio.run(go())


# ---------- patience for a route that has only just come up ----------
def test_a_host_that_is_slow_to_answer_gets_a_second_try_with_more_time(monkeypatch):
    calls, naps = [], []

    def probe(host, proxy=None, timeout=network.PROBE_SECONDS):
        calls.append(timeout)
        return (True, "HTTP 200") if len(calls) == 2 else (False, f"no answer in {timeout} s")
    monkeypatch.setattr(network, "probe", probe)
    assert network.patient_probe("login.live.com", PROXY, sleep=naps.append) == (True, "HTTP 200")
    assert calls == [network.PATIENT_SECONDS] * 2 and len(naps) == 1       # the first try failed, the second one passed


def test_a_verdict_is_not_retried_and_a_host_that_stays_silent_is_reported(monkeypatch):
    calls, naps = [], []
    monkeypatch.setattr(network, "probe", lambda h, p=None, timeout=5: calls.append(h) or (False, "the proxy rejected the password (407)"))
    assert network.patient_probe("x", PROXY, sleep=naps.append)[0] is False and len(calls) == 1 and not naps
    calls.clear()
    monkeypatch.setattr(network, "probe", lambda h, p=None, timeout=5: calls.append(h) or (False, "no answer in 12 s"))
    assert network.patient_probe("x", PROXY, sleep=naps.append) == (False, "no answer in 12 s") and len(calls) == 2


def test_auth_checks_can_skip_the_direct_probe(net, monkeypatch):
    monkeypatch.setattr(vpn, "enabled", lambda: True)
    monkeypatch.setattr(vpn, "proxy_info", lambda: PROXY)
    seen = []
    checks = network.auth_checks(lambda host, proxy=None: seen.append((host, proxy)) or (True, "HTTP 200"),
                                 hosts=network.SIGNIN_HOSTS, direct=False)
    assert seen and all(p is PROXY for _, p in seen) and {h for h, _ in seen} == set(network.SIGNIN_HOSTS)
    assert not any(c.group == "Sign-in: straight from this Mac" for c in checks)
