"""Latency tests: the Minecraft status protocol, per-player round trips from the kernel, VPN-aware skipping, internet speed."""
import json
import socket
import subprocess
import threading

import pytest

from mcctl import core, platform, players, speedtest as sp
from mcctl.core import Fail


def varint(n):
    return sp._varint(n)


def read_varint(conn):
    return sp._read_varint(conn)


def serve_status(info, connections=3):
    """A tiny stand-in for a Minecraft server's status screen. Returns (port, thread)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def handle():
        for _ in range(connections):
            conn, _ = srv.accept()
            with conn:
                n = read_varint(conn)                                  # handshake
                data = sp._read_exact(conn, n)
                assert data[0] == 0x00 and data.endswith(b"\x01")      # next state: status
                n = read_varint(conn)                                  # status request
                assert sp._read_exact(conn, n) == b"\x00"
                text = json.dumps(info).encode()
                conn.sendall(sp._packet(0x00, varint(len(text)) + text))
                try:
                    n = read_varint(conn)                              # ping -> pong with the same payload
                    ping = sp._read_exact(conn, n)
                    conn.sendall(sp._packet(0x01, ping[1:]))
                except ConnectionError:
                    pass
        srv.close()
    t = threading.Thread(target=handle, daemon=True)
    t.start()
    return srv.getsockname()[1], t


def test_status_ping_speaks_the_real_protocol_and_reads_the_servers_answer():
    info = {"version": {"name": "26.3", "protocol": 1}, "players": {"online": 2, "max": 20}, "description": "hi"}
    port, thread = serve_status(info, connections=3)
    got = sp.measure_address("this Mac", "127.0.0.1", port, samples=3)
    thread.join(2)
    assert got["ok"] == 3 and got["error"] is None and got["version"] == "26.3" and got["players"] == (2, 20)
    for key in ("connect", "status", "ping"):
        assert got[key]["min"] >= 0 and got[key]["min"] <= got[key]["avg"] <= got[key]["max"]


def test_an_address_that_does_not_answer_counts_as_loss_not_a_crash():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                          # nothing listens here now
    got = sp.measure_address("nowhere", "127.0.0.1", port, samples=2, timeout=0.3)
    assert got["ok"] == 0 and got["ping"] is None and got["error"]


def test_summarize_reports_jitter_as_the_average_change_between_samples():
    s = sp.summarize([10.0, 20.0, 10.0, 20.0])
    assert s["min"] == 10 and s["max"] == 20 and s["avg"] == 15 and s["jitter"] == 10
    assert sp.summarize([]) is None and sp.summarize([5.0])["jitter"] == 0


NETTOP = """,state,rtt_avg,
java.4242,,,
tcp4 10.77.88.219:25590<->10.77.88.50:51234,Established,23.50 ms,
tcp4 10.77.88.219:25590<->8.8.8.8:60001,Established,81.25 ms,
tcp4 10.77.88.219:25590<->*:*,Listen,,
tcp4 127.0.0.1:50809<->127.0.0.1:50808,Established,1.00 ms,
tcp6 ::1.25590<->::1.50999,Established,0.50 ms,
"""


def test_tcp_rtts_reads_the_kernels_round_trip_for_each_connection(monkeypatch):
    monkeypatch.setattr(platform, "IS_MAC", True)
    monkeypatch.setattr(sp.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(sp.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=NETTOP, stderr=""))
    rtts = sp.tcp_rtts()
    assert rtts[(25590, 51234)] == 23.5 and rtts[(25590, 60001)] == 81.25 and rtts[(50809, 50808)] == 1.0
    assert rtts[(25590, 50999)] == 0.5 and len(rtts) == 4              # the Listen line is ignored
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    assert sp.tcp_rtts() == {}                                         # no nettop (Linux): just "can't tell"


def test_players_latency_matches_logins_to_connections(make_server, monkeypatch):
    s = make_server(port=25590)
    s.log_path.parent.mkdir(parents=True, exist_ok=True)
    s.log_path.write_text(
        "[10:00:00] [Server thread/INFO]: Steve[/10.77.88.50:40000] logged in with entity id 1 at (0.0, 70.0, 0.0)\n"
        "[10:05:00] [Server thread/INFO]: Steve[/10.77.88.50:51234] logged in with entity id 7 at (1.0, 70.0, 1.0)\n"
        "[10:06:00] [Server thread/INFO]: Alex[/8.8.8.8:60001] logged in with entity id 8 at (0.0, 70.0, 0.0)\n"
        "[10:07:00] [Server thread/INFO]: Kim[/172.18.0.2:33333] logged in with entity id 9 at (0.0, 70.0, 0.0)\n")
    monkeypatch.setattr(players, "online", lambda server: ["Steve", "Alex", "Kim", "Ghost"])
    monkeypatch.setattr(sp, "tcp_rtts", lambda: {(25590, 51234): 23.5, (25590, 60001): 81.25, (25590, 33333): 0.4})
    rows = {r["name"]: r for r in sp.players_latency(s)}
    assert rows["Steve"]["rtt"] == 23.5 and rows["Steve"]["where"] == "same network"          # the newest login wins
    assert rows["Alex"]["rtt"] == 81.25 and rows["Alex"]["where"] == "internet"
    assert rows["Kim"]["where"] == "Docker / VPN forwarder"
    assert rows["Ghost"]["rtt"] is None and "no login line" in rows["Ghost"]["note"]
    text = "\n".join(sp.format_players(list(rows.values())))
    assert "excellent" in text and "okay" in text and "to the forwarder, not the player" in text


def test_a_server_in_docker_says_it_cannot_see_connections(make_server, monkeypatch):
    s = make_server(runtime="docker")
    monkeypatch.setattr(players, "online", lambda server: ["Steve"])
    monkeypatch.setattr(sp, "logins", lambda server: {"Steve": ("1.2.3.4", 5)})
    row = sp.players_latency(s)[0]
    assert row["rtt"] is None and "Docker" in row["note"]


def test_where_and_quality_words():
    assert sp.where("127.0.0.1") == "this Mac" and sp.where("::1") == "this Mac"
    assert sp.where("10.0.0.7") == "same network" and sp.where("192.0.2.9") == "same network"
    assert sp.where("172.17.0.3") == "Docker / VPN forwarder" and sp.where("8.8.8.8") == "internet"
    assert [sp.quality(x) for x in (10, 45, 80, 150, 400)] == ["excellent", "good", "okay", "laggy", "very laggy"]


def test_run_skips_the_wifi_address_while_a_mac_wide_vpn_is_on(make_server, monkeypatch):
    s = make_server(port=25590)
    monkeypatch.setattr(core.Server, "accepting", lambda self: True)
    monkeypatch.setattr(core.Server, "run", lambda self, cmd, wait=1.5: "Average time per tick: 4.0ms\nP50: 3.0ms P95: 6.0ms P99: 9.0ms")
    monkeypatch.setattr(platform, "lan_ip", lambda: "10.0.0.5")
    monkeypatch.setattr(platform, "vpn_active", lambda: True)
    monkeypatch.setattr(sp.vpn, "enabled", lambda: False)
    pinged = []
    monkeypatch.setattr(sp, "measure_address", lambda label, host, port, samples=8, timeout=2.0:
                        pinged.append(host) or {"label": label, "address": f"{host}:{port}", "samples": samples, "ok": samples,
                                                "error": None, "connect": sp.summarize([0.5]), "status": sp.summarize([1.0]),
                                                "ping": sp.summarize([0.3, 0.4])})
    monkeypatch.setattr(players, "online", lambda server: [])
    report = sp.run(s, samples=4)
    assert pinged == ["127.0.0.1"]                                     # the LAN address was not pinged through the VPN
    text = sp.format_report(report)
    assert "4.0 ms per tick" in text and "smooth" in text and "skipped: a Mac-wide VPN is on" in text
    assert "nobody is online" in text
    monkeypatch.setattr(platform, "vpn_active", lambda: False)
    pinged.clear()
    sp.run(s, samples=4)
    assert pinged == ["127.0.0.1", "10.0.0.5"]


def test_run_refuses_a_server_that_is_not_running(make_server):
    with pytest.raises(Fail, match="isn't running"):
        sp.run(make_server(), samples=2)


def test_internet_speed_reads_networkquality_and_warns_when_it_went_through_a_vpn(monkeypatch):
    out = {"dl_throughput": 31231972, "ul_throughput": 42318612, "responsiveness": 261.7, "base_rtt": 236.9,
           "interface_name": "utun9", "test_endpoint": "x.aaplimg.com"}
    monkeypatch.setattr(platform, "IS_MAC", True)
    monkeypatch.setattr(sp.shutil, "which", lambda name: "/usr/bin/networkQuality")
    monkeypatch.setattr(sp.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=json.dumps(out), stderr=""))
    r = sp.internet_speed()
    assert round(r["down"]) == 31 and round(r["up"]) == 42 and r["via_vpn"] and r["player_estimate"] == 282
    text = sp.format_report({"server": "x", "tick": None, "addresses": [], "players": [], "internet": r})
    assert "download 31 Mbit/s" in text and "your Mac-wide VPN" in text and "roughly 282 players" in text
    monkeypatch.setattr(sp.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="not json", stderr=""))
    with pytest.raises(Fail):
        sp.internet_speed()
    monkeypatch.setattr(platform, "IS_MAC", False)
    with pytest.raises(Fail, match="networkQuality"):
        sp.internet_speed()


def test_dashboard_speed_test_button_shows_the_report(make_server, monkeypatch):
    import asyncio
    from textual.widgets import Button, TabbedContent
    from mcctl.tui import app as tui
    from tests.test_tui import ready
    make_server("pytest-speed")
    monkeypatch.setattr(sp, "run", lambda s, samples=8, internet=False, say=lambda t: None:
                        {"server": s.name, "samples": samples, "tick": {"mspt": 3.0, "tps": 20.0, "p95": 4.0, "p99": 5.0},
                         "addresses": [], "players": [{"name": "Steve", "ip": "10.0.0.7", "where": "same network", "rtt": 12.0, "note": ""}]})

    async def go():
        app = tui.Dashboard(target="pytest-speed")
        async with app.run_test(size=(170, 56)) as pilot:
            await ready(app, pilot)
            app.query_one("#tabs", TabbedContent).active = "tab-network"
            await pilot.pause(0.5)
            app.query_one("#net-speed", Button).press()
            for _ in range(60):
                await pilot.pause(0.1)
                text = "\n".join(ln.text for ln in app.query_one("#net-speed-out").lines)
                if "Steve" in text:
                    break
            assert "3.0 ms per tick" in text and "Steve" in text and "excellent" in text
    asyncio.run(go())
