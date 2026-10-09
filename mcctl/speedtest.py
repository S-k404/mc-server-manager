"""Speed tests: how fast the server answers, how long a connection takes, how far away each connected player is,
and (on macOS) how good this Mac's internet connection is for hosting.

Nothing here changes anything: it only measures. Three sources:
  * the Minecraft status ping (the same handshake the multiplayer screen uses) to each address people join by;
  * the kernel's own smoothed round-trip time for each connected player's TCP connection (`nettop`), which needs no
    ICMP and no cooperation from the player;
  * Apple's `networkQuality` for internet speed (it measures whichever interface the Mac is using right now, which
    is the VPN when a Mac-wide VPN is on).
"""
import ipaddress
import json
import re
import shutil
import socket
import statistics
import struct
import subprocess
import time

from . import game, platform, players, share, vpn
from .core import Fail, die

PROTOCOL = 767                       # any value works for a status request; servers answer whatever the version
LOGIN_RE = re.compile(r"(?P<name>\w{1,16})\[/(?P<ip>[0-9a-fA-F:.]+):(?P<port>\d+)\] logged in")
TICK_RE = re.compile(r"Average time per tick: ([\d.]+)ms")
PCT_RE = re.compile(r"P50: ([\d.]+)ms P95: ([\d.]+)ms P99: ([\d.]+)ms")
MBIT_PER_PLAYER = 0.15               # a rough, generous figure for upload per connected player


# ---------- the Minecraft status protocol ----------
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(sock):
    n = shift = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("closed")
        n |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return n
        shift += 7
        if shift > 35:
            raise ConnectionError("bad varint")


def _packet(packet_id, payload=b""):
    body = _varint(packet_id) + payload
    return _varint(len(body)) + body


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def status_ping_once(host, port, timeout=2.0):
    """One multiplayer-list style ping. Returns {connect, status, ping} in ms plus what the server said about itself."""
    t0 = time.perf_counter()
    with socket.create_connection((host, port), timeout) as sock:
        sock.settimeout(timeout)
        connect = (time.perf_counter() - t0) * 1000
        addr = host.encode()
        sock.sendall(_packet(0x00, _varint(PROTOCOL) + _varint(len(addr)) + addr + struct.pack(">H", port) + _varint(1)))
        t1 = time.perf_counter()
        sock.sendall(_packet(0x00))
        length = _read_varint(sock)
        body = _read_exact(sock, length)
        status = (time.perf_counter() - t1) * 1000
        if body[:1] != b"\x00":
            raise ConnectionError("unexpected reply")
        # skip the packet id and the string length varint, then the JSON
        i = 1
        while body[i] & 0x80:
            i += 1
        info = json.loads(body[i + 1:].decode("utf-8", "replace"))
        token = struct.pack(">q", int(time.time() * 1000))
        t2 = time.perf_counter()
        sock.sendall(_packet(0x01, token))
        length = _read_varint(sock)
        if _read_exact(sock, length)[1:] != token:
            raise ConnectionError("pong didn't match")
        ping = (time.perf_counter() - t2) * 1000
    return {"connect": connect, "status": status, "ping": ping, "info": info}


def summarize(values):
    """min / average / max / jitter (average change between neighbouring samples) for a list of ms values."""
    if not values:
        return None
    jitter = statistics.fmean(abs(a - b) for a, b in zip(values, values[1:])) if len(values) > 1 else 0.0
    return {"min": min(values), "avg": statistics.fmean(values), "max": max(values), "jitter": jitter}


def measure_address(label, host, port, samples=8, timeout=2.0):
    """Ping one address several times. A sample that fails counts as loss."""
    rows, last, error = {"connect": [], "status": [], "ping": []}, None, None
    for _ in range(samples):
        try:
            one = status_ping_once(host, port, timeout)
        except (OSError, ConnectionError, ValueError, json.JSONDecodeError) as e:
            error = str(e) or type(e).__name__
            continue
        last = one["info"]
        for k in rows:
            rows[k].append(one[k])
        time.sleep(0.05)
    out = {"label": label, "address": f"{host}:{port}", "samples": samples, "ok": len(rows["ping"]), "error": error}
    out.update({k: summarize(v) for k, v in rows.items()})
    if last:
        out["version"] = (last.get("version") or {}).get("name")
        out["players"] = last.get("players", {}).get("online"), last.get("players", {}).get("max")
    return out


# ---------- the server itself ----------
def server_tick(s):
    """Tick time over the console: how quickly the server gets through a tick (50 ms is the limit for 20 TPS)."""
    reply = s.run("tick query")
    m, p = TICK_RE.search(reply), PCT_RE.search(reply)
    if not m:
        return None
    mspt = float(m.group(1))
    return {"mspt": mspt, "tps": min(20.0, 1000.0 / mspt) if mspt > 0 else 20.0,
            "p95": float(p.group(2)) if p else None, "p99": float(p.group(3)) if p else None}


# ---------- connected players ----------
def logins(s, nbytes=1 << 20):
    """{name: (ip, client_port)} for the newest login line of each player in the log."""
    seen = {}
    for m in LOGIN_RE.finditer(game.tail_text(s.log_path, nbytes) if s.log_path.exists() else ""):
        seen[m["name"]] = (m["ip"], int(m["port"]))
    return seen


def tcp_rtts():
    """{(local_port, remote_port): rtt_ms} for every established TCP connection, from the kernel (macOS nettop)."""
    if not platform.IS_MAC or not shutil.which("nettop"):
        return {}
    try:
        out = subprocess.run(["nettop", "-m", "tcp", "-L", "1", "-n", "-x", "-J", "state,rtt_avg"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    rtts = {}
    for line in out.splitlines():
        if "<->" not in line or "Established" not in line:
            continue
        conn, *rest = line.split(",")
        rtt = re.search(r"([\d.]+) ms", ",".join(rest))
        local, _, remote = conn.split(" ", 1)[-1].partition("<->")
        lp, rp = re.split(r"[:.]", local)[-1], re.split(r"[:.]", remote)[-1]
        if rtt and lp.isdigit() and rp.isdigit():
            rtts[(int(lp), int(rp))] = float(rtt.group(1))
    return rtts


def where(ip):
    """What kind of address a player's connection comes from, which decides how far to trust its round-trip time."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if a.is_loopback:
        return "this Mac"
    if a.is_global:
        return "internet"
    if a in ipaddress.ip_network("172.16.0.0/12"):
        return "Docker / VPN forwarder"          # the real player is behind it: the number below is to the forwarder
    return "same network"


def quality(ms):
    """A word for a round-trip time as a Minecraft player feels it."""
    return ("excellent" if ms < 30 else "good" if ms < 60 else "okay" if ms < 100 else "laggy" if ms < 200 else "very laggy")


def address_latency(address):
    """'85 ms (good), measured 3m ago' for the last test of the internet address, or None when there is none to show."""
    from .resources import fmt_uptime
    last = vpn.last_latency(address)
    if not last:
        return None
    when = "just now" if last["age"] < 60 else f"{fmt_uptime(last['age'])} ago"
    return f"{last['ms']:.0f} ms ({quality(last['ms'])}), measured {when}"


def players_latency(s):
    """One row per connected player: name, address, kind of connection, TCP round trip in ms (None = can't tell)."""
    names = players.online(s)
    if not names:
        return []
    known, rtts = logins(s), (tcp_rtts() if not s.docker else {})
    rows = []
    for name in names:
        ip, port = known.get(name, (None, None))
        rtt = rtts.get((s.port, port)) if port else None
        rows.append({"name": name, "ip": ip, "where": where(ip) if ip else "unknown", "rtt": rtt,
                     "note": ("the server runs in Docker, so its connections aren't visible from here" if s.docker else
                              "no login line found in the log" if not ip else
                              "" if rtt is not None else "connection not found (just joined or left?)")})
    return rows


# ---------- internet speed ----------
def internet_speed(max_seconds=20):
    """Apple's networkQuality: download / upload / responsiveness over the interface the Mac is using right now."""
    if not platform.IS_MAC or not shutil.which("networkQuality"):
        raise Fail("The internet speed test uses macOS's networkQuality, which isn't available here. Use speedtest.net.")
    try:
        r = subprocess.run(["networkQuality", "-c", "-M", str(max_seconds)], capture_output=True, text=True,
                           timeout=max_seconds + 40)
        d = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        raise Fail(f"The internet speed test didn't finish ({type(e).__name__}). Check you're online and try again.") from e
    if "dl_throughput" not in d and "ul_throughput" not in d:
        raise Fail("The internet speed test returned nothing: " + (r.stderr.strip() or "no data"))
    up = d.get("ul_throughput", 0) / 1e6
    return {"down": d.get("dl_throughput", 0) / 1e6, "up": up, "responsiveness": d.get("responsiveness"),
            "idle_rtt": d.get("base_rtt"), "interface": d.get("interface_name"), "endpoint": d.get("test_endpoint"),
            "via_vpn": str(d.get("interface_name", "")).startswith(("utun", "tun", "wg")),
            "player_estimate": int(up / MBIT_PER_PLAYER)}


# ---------- everything together ----------
WIFI = "Wi-Fi / LAN address"
INTERNET = "internet address (through the VPN)"


def shown(label, host, port):
    """What may be printed for an address. The pings still go to the real one; in local mode the Wi-Fi address is printed as
    the published name and in hidden mode not at all."""
    if label == WIFI and share.mode() == "local":
        return f"{share.host()}:{port}"
    if label == WIFI and share.mode() == "hidden":
        return "(hidden)"
    return f"{host}:{port}"


def addresses(s):
    """Where people join: this Mac, the Wi-Fi address, and the internet address when the VPN forwarding is up."""
    out = [("this Mac", "127.0.0.1", s.port, None)]
    lan = platform.lan_ip()
    if share.mode() == "hidden":
        out.append((WIFI, lan, s.port, "hidden mode: this server doesn't listen on the Wi-Fi, so there is nothing to ping"))
    elif lan and lan != "unknown":
        skip = ("skipped: a Mac-wide VPN is on, so this Mac's test of its own Wi-Fi address would go through the VPN "
                "and say nothing about your Wi-Fi") if platform.vpn_active() else None
        out.append((WIFI, lan, s.port, skip))
    try:
        public = vpn.public_address(live=False) if vpn.enabled() else None
    except Exception:
        public = None
    if public and ":" in str(public):
        host, _, port = str(public).rpartition(":")
        if port.isdigit():
            out.append((INTERNET, host, int(port), None))
    return out


def run(s, samples=8, internet=False, say=lambda text: None):
    """Test one server: tick time, each join address, connected players, optionally the internet connection."""
    if not s.accepting():
        die(f"'{s.name}' isn't running, so there is nothing to ping. Start it: mc -s {s.name} start")
    report = {"server": s.name, "samples": samples}
    say("Asking the server how it is doing...")
    report["tick"] = server_tick(s)
    report["addresses"] = []
    for label, host, port, skip in addresses(s):
        text = shown(label, host, port)
        if skip:
            report["addresses"].append({"label": label, "address": text, "skipped": skip})
            continue
        say(f"Pinging {label} ({text}) {samples} times...")
        row = {**measure_address(label, host, port, samples), "address": text}
        report["addresses"].append(row)
        if label == INTERNET and row["ok"]:
            vpn.remember_latency(text, row["ping"]["avg"])      # the Network tab shows it next to the address
    say("Checking connected players...")
    report["players"] = players_latency(s)
    if internet:
        say("Testing internet speed (about 20 seconds)...")
        try:
            report["internet"] = internet_speed()
        except Fail as e:
            report["internet_error"] = str(e)
    return report


# ---------- text for the CLI and the dashboard ----------
def _ms(stat, key="avg"):
    return f"{stat[key]:.1f} ms" if stat else "-"


def format_players(rows):
    """Text lines for the connected-players table."""
    if not rows:
        return ["  nobody is online"]
    lines = []
    for p in rows:
        if p["rtt"] is not None:
            lines.append(f"  {p['name']:<18} {p['rtt']:6.1f} ms  {quality(p['rtt']):<10} {p['where']}"
                         + ("  (to the forwarder, not the player)" if p["where"] == "Docker / VPN forwarder" else ""))
        else:
            lines.append(f"  {p['name']:<18}      -  {p['where']}" + (f"  ({p['note']})" if p["note"] else ""))
    return lines


def format_report(r):
    lines = [f"Speed test | {r['server']}", ""]
    t = r.get("tick")
    if t:
        verdict = "smooth" if t["mspt"] < 25 else "getting busy" if t["mspt"] < 45 else "lagging"
        lines.append(f"Server speed   {t['mspt']:.1f} ms per tick (limit 50) | {t['tps']:.1f} TPS | "
                     + (f"slowest 5% {t['p95']:.1f} ms | " if t.get("p95") else "") + verdict)
    else:
        lines.append("Server speed   couldn't read the tick time (is it still starting?)")
    lines += ["", "Latency to the server (what the multiplayer list measures)"]
    for a in r.get("addresses", []):
        if a.get("skipped"):
            lines.append(f"  {a['label']:<36} {a['skipped']}")
            continue
        if not a["ok"]:
            lines.append(f"  {a['label']:<36} no answer ({a['error'] or 'timed out'})")
            continue
        loss = a["samples"] - a["ok"]
        lines.append(f"  {a['label']:<36} {_ms(a['ping'])} avg | {_ms(a['ping'], 'min')} best | {_ms(a['ping'], 'max')} worst"
                     f" | jitter {_ms(a['ping'], 'jitter')} | connect {_ms(a['connect'])}"
                     + (f" | {loss}/{a['samples']} lost" if loss else ""))
    lines += ["", "Connected players (round trip from this Mac to each player's connection)", *format_players(r.get("players", []))]
    if "internet" in r:
        i = r["internet"]
        lines += ["", "This Mac's internet connection (Apple networkQuality)",
                  f"  download {i['down']:.0f} Mbit/s | upload {i['up']:.0f} Mbit/s | idle latency {i['idle_rtt']:.0f} ms"
                  + (f" | responsiveness {i['responsiveness']:.0f} RPM" if i.get("responsiveness") else ""),
                  f"  measured over {i['interface']}" + (" (your Mac-wide VPN: this is the VPN's speed, not your plain connection)"
                                                           if i["via_vpn"] else ""),
                  f"  upload alone could carry roughly {i['player_estimate']} players"]
    elif r.get("internet_error"):
        lines += ["", r["internet_error"]]
    return "\n".join(lines)
