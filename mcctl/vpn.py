"""Internet play without touching your router: ProtonVPN port forwarding, in Docker.

A ProtonVPN container (gluetun) asks Proton for a forwarded port (NAT-PMP, paid plans, P2P servers) and redirects
it to one server's port. Friends join `<VPN exit IP>:<forwarded port>`. Only this container is on the VPN, so the Mac
itself stays off it and LAN play keeps working.

Containers (both `--restart unless-stopped`):
  minecraft-vpn      gluetun, WireGuard to ProtonVPN; key + country in tunnel/vpn.env. Port forwarding is on, and
                     gluetun redirects the (random) forwarded port to the server's own port inside the container.
  minecraft-forward  socat inside the VPN's network: <server port> -> the same port on the Mac. Because gluetun does
                     the redirect, this never has to follow the forwarded port when Proton hands out a new one.

The same container also runs gluetun's built-in HTTP proxy for signing in: Microsoft/Xbox sign-in is blocked on some
managed Wi-Fi networks, so the launcher (Prism) signs in through `127.0.0.1:8888` -> the VPN, while the Mac itself stays
off the VPN and LAN friends can still join. The proxy is published on this Mac's loopback only and has a random password
(tunnel/proxy.env, owner-only, handed to Docker as an env file, never on a command line). gluetun's firewall accepts the
published port by itself: it allows packets arriving on its local-network interface "to reach Gluetun" (checked in its source).

The forwarded port and the exit IP both change whenever the VPN reconnects, so the address is re-read on every status
refresh (and cached in tunnel/public.json for the dashboard's invite button).
"""
import collections
import json
import os
import re
import secrets
import socket
import time

from . import container as dk
from .atomic import write_atomic, write_json_atomic
from .core import TUNNEL_DIR, Fail, all_servers, default_server_name, die, info, ok, port_open, save_setting, setting, warn

VPN = "minecraft-vpn"
FORWARD = "minecraft-forward"
STACK = (VPN, FORWARD)
LEGACY = ("minecraft-tunnel",)       # the playit container of older versions; removed whenever the stack starts/stops
ENV_FILE = TUNNEL_DIR / "vpn.env"
CACHE_FILE = TUNNEL_DIR / "public.json"
CACHE_SECONDS = 90
PROXY_FILE = TUNNEL_DIR / "proxy.env"
PROXY_HOST, PROXY_PORT, PROXY_USER = "127.0.0.1", 8888, "mc"
IMAGE_VPN = "ghcr.io/qdm12/gluetun:v3"
IMAGE_FORWARD = "alpine/socat:latest"
PORT_FILE = "/tmp/gluetun/forwarded_port"
NO_IP = ("Proton forwarded a port, but this tool couldn't learn the VPN's public address (the container can't reach ipinfo.io and "
         "gluetun's log has no 'gateway external IPv4 address' line). mc vpn logs vpn")
NEED_NATPMP = ("Proton gave no forwarded port. Port forwarding needs a paid plan, a WireGuard config created with "
               "'NAT-PMP (Port Forwarding)' ticked (account.proton.me -> VPN -> WireGuard configuration), and a P2P "
               "server. Make a new config with that option, then: mc vpn setup")


# ---------- config ----------
def configured():
    return ENV_FILE.exists() and "WIREGUARD_PRIVATE_KEY=" in ENV_FILE.read_text()


def enabled():
    """True once `mc vpn setup` has been run: `mc up`/`mc down` then drive the Docker stack."""
    return setting("tunnel") == "docker"


def _env_values():
    """tunnel/vpn.env as a dict (empty when it isn't there)."""
    out = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip():
                out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def place():
    """(country, city) the VPN connects to, from tunnel/vpn.env. None means no preference: until you choose, Proton picks."""
    env = _env_values()
    return env.get("SERVER_COUNTRIES") or None, env.get("SERVER_CITIES") or None


def describe_place(country, city):
    """'Amsterdam, Netherlands', 'Netherlands' or 'any country'."""
    return ", ".join(part for part in (city, country) if part) or "any country"


def _clean_place(value, what):
    """A country or city name that is safe to put in an env file (one line, letters and a few marks)."""
    value = " ".join(str(value).split())
    if not value or len(value) > 60 or not value[0].isalpha() or not all(c.isalpha() or c in " .'-," for c in value):
        die(f"That doesn't look like a {what} name (letters, spaces and dashes only, e.g. '{'Netherlands' if what == 'country' else 'Amsterdam'}').")
    return value


def _pick(value, saved, what):
    """The new country or city: None keeps what is saved, 'any' (or an empty city) clears it, anything else must be a name."""
    if value is None:
        return saved
    return None if str(value).strip().lower() in ("", "any") else _clean_place(value, what)


def _write_env(private_key, country, city):
    body = ("VPN_SERVICE_PROVIDER=protonvpn\nVPN_TYPE=wireguard\n"
            f"WIREGUARD_PRIVATE_KEY={private_key}\n"
            + (f"SERVER_COUNTRIES={country}\n" if country else "")
            + (f"SERVER_CITIES={city}\n" if city else ""))
    TUNNEL_DIR.mkdir(exist_ok=True)
    write_atomic(ENV_FILE, body, mode=0o600)


def setup(private_key, country=None, city=None):
    """Write tunnel/vpn.env (owner-only) with a ProtonVPN WireGuard key, and turn the Docker stack on.
    `country` and `city` default to the saved ones ('any' clears one). With no country chosen Proton picks, which can be
    far away: a country, and a city in it, near your friends means less lag."""
    private_key = private_key.strip()
    if len(private_key) != 44 or not private_key.endswith("="):
        die("That doesn't look like a WireGuard private key (44 characters ending in '=').")
    saved_country, saved_city = place()
    country, city = _pick(country, saved_country, "country"), _pick(city, saved_city, "city")
    _write_env(private_key, country, city)
    save_setting("tunnel", "docker")
    ok(f"Saved tunnel/vpn.env (exit: {describe_place(country, city)}). Start it: mc vpn start")
    if not country:
        info("  No exit country is chosen, so Proton picks one. Choose one near your friends for less lag: mc vpn setup --country <name>")
    warn("Port forwarding only works if this key came from a config made with 'NAT-PMP (Port Forwarding)' ticked "
         "(paid Proton plan).")


def set_place(country=None, city=None):
    """Change the exit country and/or city without typing the key again. Applies the next time the VPN starts."""
    if not configured():
        die("No VPN set up yet. Run: mc vpn setup")
    saved_country, saved_city = place()
    country, city = _pick(country, saved_country, "country"), _pick(city, saved_city, "city")
    key = _env_values().get("WIREGUARD_PRIVATE_KEY")
    if not key:
        die("tunnel/vpn.env has no key. Run: mc vpn setup")
    _write_env(key, country, city)
    ok(f"The VPN will exit in {describe_place(country, city)}" + (" (any city)" if country and not city else "") +
       ". It applies the next time the VPN starts: mc vpn start")
    info("  Compare the lag: mc speedtest (the internet address row). A name gluetun doesn't know stops the VPN from "
         "starting; mc vpn logs vpn says so.")
    return country, city


def forward_server():
    """Name of the server the forwarded port leads to (Proton gives one port, so one server is public)."""
    names = [s.name for s in all_servers()]
    chosen = setting("vpn_forward_server")
    return chosen if chosen in names else (default_server_name() if default_server_name() in names else
                                           (names[0] if names else None))


def set_forward_server(name):
    if name not in [s.name for s in all_servers()]:
        die(f"No server called '{name}'.")
    save_setting("vpn_forward_server", name)
    ok(f"The internet address now leads to '{name}'.")
    if dk.available() and dk.state(VPN) == "running":
        start()                       # the port redirect is set when the VPN container starts


MTU_RANGE = (1000, 1420)
NESTED_MTU = 1280      # smaller than the Mac VPN's own tunnel can carry, so big packets inside it don't vanish


def mtu():
    """Packet size (WireGuard MTU) for the container: a saved choice, else NESTED_MTU while this Mac's own VPN is on
    (a tunnel inside a tunnel: packets as big as gluetun's default 1400 don't fit through the outer one and are dropped,
    which lets small things work, e.g. Proton's port request and a status ping, while big ones, e.g. a login, never arrive),
    else None = gluetun's own default."""
    from . import platform                       # imported here: keeps this module light for the agent
    chosen = setting("vpn_mtu")
    if isinstance(chosen, int) and MTU_RANGE[0] <= chosen <= MTU_RANGE[1]:
        return chosen
    return NESTED_MTU if platform.vpn_active() else None


def set_mtu(value):
    """`auto` or a number; applies at the next `mc vpn start`."""
    text = str(value).strip().lower()
    if text == "auto":
        save_setting("vpn_mtu", "auto")
        return ok("Packet size: automatic (smaller while this Mac's own VPN is on). Applies at the next: mc vpn start")
    if not text.isdigit() or not MTU_RANGE[0] <= int(text) <= MTU_RANGE[1]:
        die(f"Give a number between {MTU_RANGE[0]} and {MTU_RANGE[1]}, or auto.")
    save_setting("vpn_mtu", int(text))
    ok(f"Packet size {int(text)}. Applies at the next: mc vpn start")


def _mtu_env():
    value = mtu()
    return ["-e", f"WIREGUARD_MTU={value}"] if value else []


def _forward_port():
    name = forward_server()
    return next((s.port for s in all_servers() if s.name == name), None)


# ---------- sign-in proxy ----------
def _proxy_password():
    """The sign-in proxy's password. Made once and kept in tunnel/proxy.env (owner-only), which Docker reads as an env file."""
    try:
        for line in PROXY_FILE.read_text().splitlines():
            if line.startswith("HTTPPROXY_PASSWORD=") and len(line) > len("HTTPPROXY_PASSWORD=") + 12:
                return line.split("=", 1)[1]
    except OSError:
        pass
    password = secrets.token_urlsafe(18)                  # letters, digits, - and _ : safe in env files and proxy URLs
    TUNNEL_DIR.mkdir(exist_ok=True)
    fd = os.open(PROXY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"HTTPPROXY_USER={PROXY_USER}\nHTTPPROXY_PASSWORD={password}\n")
    os.chmod(PROXY_FILE, 0o600)
    return password


def proxy_info():
    """The launcher's sign-in proxy: where it is, who/what to type, and whether it is answering right now.
    `password` is None until the VPN has been started once (reading never creates it)."""
    password = _proxy_password() if PROXY_FILE.exists() else None
    up = bool(password) and dk.available() and dk.state(VPN) == "running" and port_open(PROXY_HOST, PROXY_PORT, 0.5)
    return {"host": PROXY_HOST, "port": PROXY_PORT, "user": PROXY_USER, "password": password, "up": up,
            "url": f"http://{PROXY_USER}:{password}@{PROXY_HOST}:{PROXY_PORT}" if password else None}


def proxy_help(info=None):
    """Plain-ASCII lines telling a person what to enter in their launcher."""
    info = info or proxy_info()
    if not info["password"]:
        return ["The sign-in proxy isn't set up yet. Start the VPN first: mc vpn start"]
    lines = ["Sign in through the Docker VPN, so this Mac can stay off a Mac-wide VPN (and Wi-Fi friends can still join).",
             "In Prism Launcher: Settings -> Proxy -> HTTP, then enter:",
             f"  Address   {info['host']}",
             f"  Port      {info['port']}",
             f"  Username  {info['user']}",
             f"  Password  {info['password']}",
             "Only the launcher's sign-in uses it. The game still connects to servers directly."]
    if not info["up"]:
        lines.append("It is NOT answering right now. Start (or restart) the VPN to turn it on: mc vpn start")
    return lines


# ---------- lifecycle ----------
def _voice_target():
    """The UDP port of Simple Voice Chat on the server the address leads to, or None (not installed)."""
    from . import voice
    server = next((x for x in all_servers() if x.name == forward_server()), None)
    return voice.udp_port(server) if server is not None and voice.installed(server) else None


def _forward_script(ip, forwarded=None):
    ports = sorted({s.port for s in all_servers()})
    parts = [f"socat TCP-LISTEN:{p},fork,reuseaddr TCP:{ip}:{p}" for p in ports]
    target = _forward_port()
    if forwarded and forwarded not in ports and target:
        # gluetun's own redirect (VPN_PORT_FORWARDING_LISTENING_PORT) needs a recent image; where it is missing the
        # connection arrives on Proton's port itself, so listen there too. Where the redirect works this is never reached.
        parts.append(f"socat TCP-LISTEN:{forwarded},fork,reuseaddr TCP:{ip}:{target}")
    udp = _voice_target()
    if forwarded and udp:
        # Proton forwards the same number for UDP: Simple Voice Chat's audio (voice.py). Each player gets its own socket
        # towards the mod; a quiet one is dropped after a minute (the mod's clients send a keep-alive every second).
        parts.append(f"socat -T 60 UDP4-LISTEN:{forwarded},fork,reuseaddr UDP4:{ip}:{udp}")
    return " & ".join(parts) + " & wait"


def _remove_all():
    for name in (*reversed(STACK), *LEGACY):
        dk.remove(name)


def start(wait=True, patience=120):
    dk.require()
    if not configured():
        die("No VPN set up yet. Get a WireGuard private key from your Proton account "
            "(account.proton.me -> VPN -> WireGuard configuration, tick 'NAT-PMP (Port Forwarding)'), then: mc vpn setup")
    if not all_servers():
        die("No servers yet; create one first: mc create")
    if not place()[0]:
        info("No exit country is chosen, so Proton picks one, which can be far from your friends. "
             "Choose one near them for less lag: mc vpn setup --country <name>")
    dk.ensure_network()
    for image in (IMAGE_VPN, IMAGE_FORWARD):
        dk.pull(image, say=ok)
    dk.refresh(IMAGE_VPN, say=warn)     # an old cached gluetun ignores VPN_PORT_FORWARDING_LISTENING_PORT
    _remove_all()
    ip = dk.host_gateway_ip()
    _proxy_password()
    try:
        dk.run("--name", VPN, "--restart", "unless-stopped", "--network", dk.NETWORK,
               "--cap-drop", "ALL", "--cap-add", "NET_ADMIN", "--cap-add", "CHOWN", "--security-opt", "no-new-privileges",
               "--tmpfs", "/tmp/gluetun",      # gluetun writes the forwarded port here; the image's own dir isn't writable
               "--device", "/dev/net/tun", "--env-file", str(ENV_FILE), "--env-file", str(PROXY_FILE),
               "-e", f"FIREWALL_OUTBOUND_SUBNETS={ip}/32",
               "-e", "VPN_PORT_FORWARDING=on", "-e", "PORT_FORWARD_ONLY=on",
               "-e", f"VPN_PORT_FORWARDING_LISTENING_PORT={_forward_port()}",
               "-e", "HTTPPROXY=on", "-e", "HTTPPROXY_STEALTH=on", *_mtu_env(),
               "-p", f"{PROXY_HOST}:{PROXY_PORT}:{PROXY_PORT}",      # the launcher's sign-in proxy: this Mac only
               IMAGE_VPN)
    except Fail as e:
        if "8888" in str(e) or "already allocated" in str(e):
            die(f"Port {PROXY_PORT} on this Mac is already in use, so the sign-in proxy can't start. Close whatever "
                f"uses it (lsof -i :{PROXY_PORT}) and run: mc vpn start")
        raise
    if wait and not _wait_healthy(VPN, patience):
        tail = dk.docker("logs", "--tail", "6", VPN)
        lines = (tail.stdout + tail.stderr).strip().splitlines()[-4:]
        dk.remove(VPN)  # don't leave a failing container retrying in the background
        waited = f"{patience // 60} minutes" if patience % 60 == 0 else f"{patience} seconds"
        die(f"The VPN didn't come up within {waited}:\n  " + "\n  ".join(lines) +
            "\nCheck the key, country and city in tunnel/vpn.env (mc vpn logs vpn). Needs a paid plan with P2P servers there.")
    _start_dependents(ip)
    save_setting("vpn_desired", True)
    ok("VPN running in Docker. Mac traffic is NOT going through the VPN.")
    proxy = proxy_info()
    ok(f"Launcher sign-in proxy: {proxy['host']}:{proxy['port']}  (settings for Prism: mc vpn proxy)")
    if wait:
        _report_address()


def server_down_note():
    """Why friends get 'Connection reset' although the address is right: the server it leads to isn't running."""
    name = forward_server()
    server = next((x for x in all_servers() if x.name == name), None)
    if server is not None and not server.up():
        return f"'{name}' isn't running, so anyone who uses the address gets 'Connection reset'. Start it: mc -s {name} start"
    return None


def _report_address():
    for _ in range(30):
        port = forwarded_port()
        if port:
            if _forwarder_port() != str(port) or _forwarder_udp() != str(_voice_target() or ""):
                _start_dependents(forwarded=port)
            st = status()
            if st.get("public"):
                ok(f"Friends join '{forward_server()}' at {st['public']} "
                   "(it changes whenever the VPN reconnects: mc vpn address)")
                if note := server_down_note():
                    warn(note)
                return
            return warn(NO_IP)
        time.sleep(1)
    warn(NEED_NATPMP)


def _start_dependents(ip=None, forwarded=None):
    ip = ip or dk.host_gateway_ip()
    dk.remove(FORWARD)
    dk.run("--name", FORWARD, "--restart", "unless-stopped", "--network", f"container:{VPN}", *dk.HARDENING,
           "--label", f"mc.forwarded={forwarded or ''}", "--label", f"mc.udp={(_voice_target() or '') if forwarded else ''}",
           "--entrypoint", "sh", IMAGE_FORWARD, "-c", _forward_script(ip, forwarded))


def _forwarder_udp():
    """The UDP port the running forwarder was started to pass on for voice chat ('' = none), or None when it isn't there."""
    dep = dk.inspect(FORWARD)
    return None if dep is None else (dep.get("Config", {}).get("Labels") or {}).get("mc.udp", "")


def _forwarder_port():
    """The Proton port the running forwarder was started to listen on ('' = none), or None when it isn't there."""
    dep = dk.inspect(FORWARD)
    return None if dep is None else (dep.get("Config", {}).get("Labels") or {}).get("mc.forwarded", "")


def _wait_healthy(name, seconds):
    for _ in range(seconds):
        h = dk.health(name)
        if h == "healthy":
            return True
        if dk.state(name) in ("exited", "dead", "missing"):
            return False
        time.sleep(1)
    return False


def stop():
    dk.require()
    save_setting("vpn_desired", False)
    _remove_all()
    CACHE_FILE.unlink(missing_ok=True)
    ok("VPN stopped.")


def heal():
    """Bring the stack back after a Docker/Mac restart. Called by the agent; a no-op when it's healthy or off."""
    if not (enabled() and configured() and setting("vpn_desired") and dk.available()):
        return None
    vpn = dk.inspect(VPN)
    if vpn is None or vpn["State"]["Status"] != "running":
        start(wait=False)
        return "restarted the VPN stack"
    # The forwarder shares the VPN's network and breaks when it's recreated; restart it if it's older than the VPN.
    dep = dk.inspect(FORWARD)
    if dep is None or dep["State"]["Status"] != "running" or dep["State"]["StartedAt"] < vpn["State"]["StartedAt"]:
        _start_dependents(forwarded=forwarded_port())
        return "restarted the forwarder"
    port = forwarded_port()
    if port and (_forwarder_port() != str(port) or _forwarder_udp() != str(_voice_target() or "")):
        _start_dependents(forwarded=port)           # Proton gave a new port, or voice chat was added or moved, since it started
        return "pointed the forwarder at Proton's current port"
    return _restart_if_stuck(dk.health(VPN), port)


STUCK_AFTER = 180        # seconds a VPN may stay unhealthy, or without a forwarded port, before it is recreated
RESTART_COOLDOWN = 300   # and it is never recreated more often than this (every restart changes the address)
_stuck = {"since": None, "restarted": 0.0}


def _restart_if_stuck(health, port, now=None):
    """A VPN that stays unhealthy, or never gets a forwarded port, is recreated (not at once: gluetun mends itself, and every
    restart changes the address). Returns what it did, or None."""
    now = time.monotonic() if now is None else now
    problem = "unhealthy" if health == "unhealthy" else ("without a forwarded port" if not port and health == "healthy" else None)
    if problem is None:
        _stuck["since"] = None
        return None
    _stuck["since"] = _stuck["since"] if _stuck["since"] is not None else now
    if now - _stuck["since"] < STUCK_AFTER or now - _stuck["restarted"] < RESTART_COOLDOWN:
        return None
    _stuck.update(since=None, restarted=now)
    start(wait=False)
    return f"recreated the VPN (it stayed {problem} for {STUCK_AFTER // 60} minutes)"


# ---------- status ----------
def forwarded_port():
    """The port Proton forwarded to the VPN container, or None (not forwarded yet / not supported by this key)."""
    if dk.state(VPN) != "running":
        return None
    r = dk.docker("exec", VPN, "cat", PORT_FILE)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        return int(r.stdout.strip())
    found = re.findall(r"port forwarded is (\d+)", logs(VPN, 200))
    return int(found[-1]) if found else None


def status():
    """Dict describing the stack, for `mc vpn`, the dashboard and the doctor."""
    if not dk.available():
        return {"docker": False}
    out = {"docker": True, "configured": configured(), "containers": {n: dk.state(n) for n in STACK},
           "vpn_health": dk.health(VPN), "forward_server": forward_server()}
    if out["containers"][VPN] == "running":
        out["port"] = forwarded_port()
        data = _lookup_exit(dk.started_at(VPN))
        if data:
            out["exit"] = f"{data.get('city', '?')}, {data.get('country', '?')} ({data.get('ip', '?')})"
            out["exit_ip"] = data.get("ip")
        elif out["port"] and (ip := gateway_ip(out["port"])):
            out["exit"], out["exit_ip"] = ip, ip     # the container's ipinfo lookup failed; Proton told gluetun the address
        out["proxy_up"] = port_open(PROXY_HOST, PROXY_PORT, 0.5)
        if out.get("exit_ip") and out["port"]:
            out["public"] = f"{out['exit_ip']}:{out['port']}"
    _write_cache(out.get("public"), out["forward_server"])
    return out


_exit_cache = {}      # container start time -> when we last asked ipinfo, and what it said (None = it failed)
_gateway_cache = {}   # forwarded port -> the address gluetun logged with it


def _lookup_exit(started):
    """City, country and address of the VPN's exit from ipinfo.io, asked from inside the container. Cached: a failure
    for a minute (it waits 6 s), a success for ten."""
    c = _exit_cache
    if c.get("key") == started and time.monotonic() - c["at"] < (600 if c["data"] else 60):
        return c["data"]
    r = dk.docker("exec", VPN, "wget", "-qO-", "-T", "6", "https://ipinfo.io/json")
    data = None
    if r.returncode == 0:
        try:
            data = json.loads(r.stdout)
        except ValueError:
            pass
    c.update(key=started, at=time.monotonic(), data=data)
    return data


def gateway_ip(port=None):
    """The address Proton forwards `port` on, as gluetun logged it ('gateway external IPv4 address is ...'), or None."""
    if port in _gateway_cache:
        return _gateway_cache[port]
    found = re.findall(r"gateway external IPv4 address is (\d{1,3}(?:\.\d{1,3}){3})", logs(VPN, 3000))
    if found and port:
        _gateway_cache[port] = found[-1]
    return found[-1] if found else None


def _write_cache(public, server):
    try:
        if public:
            write_json_atomic(CACHE_FILE, {"public": public, "server": server, "at": time.time()}, indent=None)
        else:
            CACHE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def public_now():
    """'ip:port' from gluetun's own state with as few docker calls as possible (no city lookup), or None."""
    port = forwarded_port()
    ip = gateway_ip(port) if port else None
    return f"{ip}:{port}" if ip and port else None


def public_address(live=False):
    """'ip:port' friends can join, or None. `live` asks Docker now (slow); otherwise a recent cached answer is used,
    which the dashboard refreshes every few seconds, so a stopped VPN stops being advertised within ~90 s."""
    if live:
        return status().get("public")
    try:
        data = json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return None
    return data["public"] if time.time() - data.get("at", 0) < CACHE_SECONDS else None


# ---------- how far away the internet address is ----------
LATENCY_FILE = TUNNEL_DIR / "latency.json"
LATENCY_FRESH = 24 * 3600


def remember_latency(address, ms):
    """Keep the latest measured round trip to the internet address (from `mc speedtest`), so the dashboard can show it next to
    the address. Best effort: a failed write is not worth an error."""
    try:
        write_json_atomic(LATENCY_FILE, {"address": address, "ms": round(float(ms), 1), "at": time.time()}, indent=None)
    except (OSError, ValueError, TypeError):
        pass


def last_latency(address=None):
    """{'ms', 'age'} for the last measured round trip, or None when there is none, it is old, or it was measured against
    a different address (the forwarded port and the exit IP change whenever the VPN reconnects)."""
    try:
        data = json.loads(LATENCY_FILE.read_text())
        ms, at = float(data["ms"]), float(data["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = time.time() - at
    if age > LATENCY_FRESH or age < -60 or (address is not None and data.get("address") != address):
        return None
    return {"ms": ms, "age": max(0.0, age)}


def logs(name=VPN, lines=40):
    r = dk.docker("logs", "--tail", str(lines), name)
    return (r.stdout + r.stderr).strip()


def forward_clues(lines=400, keep=4):
    """The last few gluetun log lines about port forwarding: what it did, or why Proton gave no port."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", logs(VPN, lines))
    hits = [ln.strip() for ln in text.splitlines() if re.search(r"port.?forward|nat-?pmp|\bpmp\b", ln, re.I)]
    return [h[:220].encode("ascii", "replace").decode() for h in hits[-keep:]]


# ---------- trace: where does a friend's connection stop? ----------
Step = collections.namedtuple("Step", "ok what detail fix")   # ok: True | False | None (could not be checked)


def listening_ports(proc_net_tcp):
    """Ports in the LISTEN state, from the text of /proc/net/tcp (and tcp6)."""
    found = set()
    for line in proc_net_tcp.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[3] == "0A" and ":" in parts[1]:
            try:
                found.add(int(parts[1].rsplit(":", 1)[1], 16))
            except ValueError:
                pass
    return found


def redirects(iptables_nat):
    """{port people connect to: port it is sent to} from the REDIRECT rules in `iptables -t nat -S`."""
    return {int(m[1]): int(m[2]) for m in re.finditer(r"--dport (\d+)\b.*?-j REDIRECT --to-ports? (\d+)", iptables_nat)}


def accepted_ports(iptables_input, proto=None):
    """Destination ports with an ACCEPT rule in the text of `iptables -S INPUT` (only `tcp` or `udp` rules when `proto` says so)."""
    found = set()
    for line in iptables_input.splitlines():
        m = re.match(r"-A INPUT\b.*?--dport (\d+)\b.*?-j ACCEPT", line)
        if m and (proto is None or re.search(rf"-p {proto}\b", line)):
            found.add(int(m[1]))
    return found


def reach(host, port, timeout=6):
    """(worked, what happened) for one real Minecraft status request from this Mac to host:port."""
    from . import speedtest                      # imported here: speedtest imports vpn
    try:
        r = speedtest.status_ping_once(host, port, timeout)
    except ConnectionRefusedError:
        return False, "refused: the address answers but nothing listens on that port"
    except ConnectionResetError:
        return False, "connected, then reset: something accepted the connection and dropped it"
    except socket.timeout:
        return False, "timed out: the packets never come back"
    except ConnectionError:
        return False, "connected, then closed without an answer"
    except (OSError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"
    return True, f"answered in {r['connect'] + r['status']:.0f} ms"


def probe_in_forwarder(host, port, timeout=20):
    """(worked, what happened): a real Minecraft status request sent from inside the forwarder container to host:port.
    The container prints what came back as hex (`od`; its `tr` has no character classes, which once made every probe
    fail), decoded here, so binary replies are safe and socat's own error text is kept when it fails."""
    from . import speedtest                      # imported here: speedtest imports vpn
    import struct
    addr = b"localhost"
    packet = (speedtest._packet(0x00, speedtest._varint(speedtest.PROTOCOL) + speedtest._varint(len(addr)) + addr
                                + struct.pack(">H", port) + speedtest._varint(1)) + speedtest._packet(0x00))
    octal = "".join(f"\\{b:03o}" for b in packet)
    r = dk.docker("exec", FORWARD, "sh", "-c",
                  f"printf '{octal}' | socat -t 3 -T 5 - TCP:{host}:{port},connect-timeout=4 2>&1 | head -c 1500 | "
                  r"od -An -v -tx1 | tr -d ' \n'", timeout=timeout)
    try:
        text = "".join(chr(b) for b in bytes.fromhex(r.stdout.strip()) if 32 <= b < 127)
    except ValueError:                           # not hex: the shell itself complained
        text = r.stdout.strip()
    if '"version"' in text:
        return True, "the server answered"
    text = text or r.stderr.strip()
    return False, ("it said: " + text[:160]) if text else "connected, but nothing came back within 5 seconds"


def _inside(*cmd):
    """Output of a command run inside the VPN container (it shares its network with the forwarder), or None."""
    r = dk.docker("exec", VPN, *cmd, timeout=15)
    return r.stdout + r.stderr if r.returncode == 0 else None


def _voice_steps(add, server, port, filt):
    """The hops of Simple Voice Chat's audio (voice.py): the config line, the forwarder's UDP listener, gluetun's UDP rule, the mod."""
    from . import moddoctor, voice
    route, vp = voice.route(server), voice.udp_port(server)
    add(True, "Voice chat: audio goes " + ("through the internet address" if route == "vpn" else "straight to this Mac"),
        f"the mod's UDP port is {vp}")
    conf, want = voice.props(server), voice.wanted(server)
    have = (conf or {}).get("voice_host", "")
    if conf is None:
        add(None, "Voice chat: its config file", "the mod writes it on its first start")
    elif want is None:
        add(None, "Voice chat: the address players are given", "the internet address isn't known yet")
    elif have != want and have and have != (voice._load().get(server.name) or {}).get("value", ""):
        add(None, "Voice chat: voice_host", f"set by hand to {have}; mc leaves it alone")
    elif have == want and voice.pending_restart(server):
        add(None, "Voice chat: voice_host", f"{have or 'empty'} is written; the server restarts when nobody is on it")
    else:
        add(have == want, "Voice chat: voice_host", f"{have or 'empty'}" + ("" if have == want else f", should be {want or 'empty'}"),
            "mc vpn voice (rewrites it; the server restarts when nobody is on it)")
    udp = _inside("cat", "/proc/net/udp", "/proc/net/udp6")
    if udp is not None:
        add(port in voice.listening_udp_ports(udp), f"Voice chat: the forwarder listens for UDP on {port}", "", "mc vpn start")
    if filt is not None:
        add(True if port in accepted_ports(filt, "udp") else None, f"Voice chat: gluetun's firewall lets UDP {port} in",
            "" if port in accepted_ports(filt, "udp") else "no UDP ACCEPT rule found for it", "mc vpn start")
    bound = moddoctor._udp_ports(server)
    if bound is not None:
        add(vp in bound, f"Voice chat: the mod listens on UDP {vp} on this Mac", "",
            f"look for 'voicechat' lines in the log: mc -s {server.name} logs")


def trace():
    """Walk the whole way a friend's connection takes (VPN -> Proton's forwarded port -> gluetun's redirect -> the forwarder ->
    Docker's host gateway -> the server -> this Mac's own test) and report every hop. Returns [Step]; the first
    failing step is where it stops. Nothing here changes anything."""
    from . import platform                       # imported here: it imports this module
    steps = []

    def add(ok, what, detail="", fix=""):
        steps.append(Step(ok, what, detail, fix))
        return ok

    if not add(dk.available(), "Docker answers", fix="open Docker Desktop and wait for the whale icon to settle"):
        return steps
    if not add(configured(), "VPN key saved", fix="mc vpn setup"):
        return steps
    state = dk.state(VPN)
    if not add(state == "running", "VPN container running", state, "mc vpn start"):
        return steps
    st = status()
    add(st.get("vpn_health") == "healthy", "VPN connected (gluetun healthy)", str(st.get("vpn_health")), "mc vpn logs vpn")
    size = _inside("cat", "/sys/class/net/tun0/mtu")
    if size and size.strip().isdigit():
        cur = int(size.strip())
        risky = platform.vpn_active() and cur > NESTED_MTU + 20
        add(None if risky else True, f"Tunnel packet size (MTU {cur})",
            "this Mac's own VPN is on, so packets this big may be dropped inside it" if risky else "",
            f"mc vpn mtu {NESTED_MTU}, then mc vpn start")
    add(bool(st.get("exit_ip")), "Traffic passes through the tunnel",
        st.get("exit") or "the container could not look up its own exit address and gluetun logged none",
        "mc vpn logs vpn  (a managed Wi-Fi often blocks the container's plain WireGuard: it works inside this Mac's own VPN)")
    port, local = st.get("port"), _forward_port()
    if not add(bool(port), "Proton gave a forwarded port",
               str(port) if port else "none. gluetun said: " + (" | ".join(forward_clues()) or "nothing about port forwarding"),
               NEED_NATPMP):
        return steps
    nat = _inside("iptables", "-t", "nat", "-S")
    tcp = _inside("cat", "/proc/net/tcp", "/proc/net/tcp6")
    listening = listening_ports(tcp) if tcp is not None else None
    if nat is None and listening is None:
        add(None, f"Connections to {port} reach the forwarder", "the container's firewall and sockets could not be read")
    else:
        got = redirects(nat or "").get(port)
        direct = listening is not None and port in listening
        how = (f"gluetun redirects {port} to {local}" if got == local else
               f"the forwarder listens on {port} itself" if direct else "")
        if not how and nat is None:
            add(None, f"Connections to {port} reach the forwarder",
                f"gluetun's redirect could not be checked (its firewall can't be read) and nothing listens on {port} itself")
            how = "checked"
        if not how:
            rules = [ln for ln in (nat or "").splitlines() if ln.startswith("-A")][:3]
            built = dk.image_built(IMAGE_VPN)
            how = ("no redirect and no listener. gluetun image built " + (built[:10] or "unknown") +
                   (". nat rules: " + " | ".join(rules) if rules else ". no nat rules"))
        if how != "checked":
            add(bool(got == local or direct), f"Connections to {port} reach the forwarder", how, "mc vpn start")
    filt = _inside("iptables", "-S", "INPUT")
    if filt is not None:
        # a redirect rewrites the destination port before the INPUT chain sees the packet; without one it is Proton's port itself
        need = local if redirects(nat or "").get(port) == local else port
        open_ports = accepted_ports(filt)
        rules = " | ".join(ln for ln in filt.splitlines() if ln.startswith("-A"))[:300]
        add(True if need in open_ports else None, f"gluetun's firewall lets port {need} in",
            f"port {need} is allowed" if need in open_ports else f"no ACCEPT rule for port {need} found. INPUT rules: {rules or 'none'}",
            "mc vpn start (gluetun opens the forwarded port itself); if this stays, say so")
    fwd = dk.state(FORWARD)
    detail = fwd
    if fwd != "running":
        detail += ": " + (logs(FORWARD, 3).replace("\n", " | ") or "no log")
    add(fwd == "running", "Forwarder container running", detail, "mc vpn start")
    if listening is not None:
        add(local in listening, f"Something listens on {local} inside the VPN", "", "mc vpn start")
    server = next((s for s in all_servers() if s.name == forward_server()), None)
    if server is not None:
        bound = server.prop("server-ip")           # may be this Mac's Wi-Fi address: say where, never the number (share modes)
        bind = "every address" if not bound else "this Mac only" if bound.startswith("127.") else "one specific address"
        up = add(server.up(), f"Server '{server.name}' is running", f"listens on {bind}", f"mc -s {server.name} start")
        if up:
            worked, why = reach("127.0.0.1", local, timeout=8)
            add(worked, f"Server answers a status request on this Mac (127.0.0.1:{local})", why, f"mc -s {server.name} logs 30")
        if up and fwd == "running":
            dep = dk.inspect(FORWARD) or {}
            upstream = re.search(rf"TCP-LISTEN:{local},\S+ TCP:([\d.]+):(\d+)", " ".join(dep.get("Config", {}).get("Cmd") or []))
            if upstream:
                gw, gw_port = upstream[1], int(upstream[2])
                r = dk.docker("exec", FORWARD, "socat", "-u", "/dev/null", f"TCP:{gw}:{gw_port},connect-timeout=4", timeout=20)
                said = (r.stderr.strip().splitlines() or ["no message"])[-1][-160:]
                add(r.returncode == 0, f"Forwarder opens a connection to {gw}:{gw_port} (Docker's address for this Mac)",
                    "" if r.returncode == 0 else said,
                    "the container cannot reach this Mac: 'refused' = nothing accepts on that address (check server-ip above), "
                    "'timed out' = a firewall drops it")
                worked, why = probe_in_forwarder(gw, gw_port)
                add(worked, f"Status request from the forwarder straight to {gw}:{gw_port}", why,
                    "Docker connects but gets no answer from this Mac's side: a firewall (macOS, or the Mac's own VPN app) "
                    "between Docker and the server; if the line above this one passed, the server itself is fine")
            worked, why = probe_in_forwarder("127.0.0.1", local)
            add(worked, f"Status request through the forwarder's own listener (127.0.0.1:{local})", why,
                "mc vpn start (recreates the forwarder)")
    if server is not None:
        from . import voice
        if voice.installed(server):
            _voice_steps(add, server, port, filt)
    if st.get("public"):
        host, _, public_port = st["public"].rpartition(":")
        worked, why = reach(host, int(public_port))
        if not worked and platform.vpn_active():
            why += " (this Mac's own VPN is on, so this test goes Proton -> Proton: ask a friend off your network to try instead)"
        add(worked, f"Joining {st['public']} from this Mac", why, "")
    return steps


def fix(start_servers=False, say=info):
    """Run trace(), repair what can be repaired from here and run it again, up to three rounds. Returns (steps, actions).
    It recreates the VPN stack when the tunnel, the port, the redirect or the forwarder is bad, and starts the server the address
    leads to only when `start_servers` says so (a stopped server may be stopped on purpose). It never changes another app's settings."""
    from . import servers
    actions = []
    steps = trace()
    for _ in range(3):
        bad = [st for st in steps if st.ok is False and not st.what.startswith("Joining")]
        if not bad or bad[0].what in ("Docker answers", "VPN key saved"):
            break
        first = bad[0]
        if first.what.startswith("Voice chat"):
            from . import voice
            server = next((x for x in all_servers() if x.name == forward_server()), None)
            note = voice.maintain(server, log=lambda m: None) if server is not None else ""
            if not note:
                break
            actions.append(note)
        elif first.what.startswith("Server '"):
            server = next((x for x in all_servers() if x.name == forward_server()), None)
            if not start_servers or server is None:
                break
            say(f"Starting '{server.name}'...")
            servers.start(server)
            actions.append(f"started '{server.name}'")
        else:
            from . import platform
            mac_vpn_off_and_tunnel_down = (first.what.startswith(("VPN connected", "Traffic passes", "Proton gave"))
                                           and not platform.vpn_active())
            if mac_vpn_off_and_tunnel_down:
                say("Heads-up: this Mac's own Proton VPN is off. On a managed network or firewall the container usually can't connect without it. "
                    "Trying anyway; this takes about 40 seconds.")
            say(f"Not working: {first.what}. Recreating the VPN...")
            try:
                start(patience=40 if mac_vpn_off_and_tunnel_down else 120)
            except Fail as e:
                actions.append(f"could not recreate the VPN: {e}")
                break
            actions.append("recreated the VPN")
        steps = trace()
    return steps, actions


def fix_hint(steps):
    """Why a repair may not have worked, in one sentence, or ''."""
    from . import platform
    tunnel = ("VPN connected", "Traffic passes", "Proton gave")
    if any(st.ok is False and st.what.startswith(tunnel) for st in steps) and not platform.vpn_active():
        return ("The container's tunnel can't connect. On a managed network or firewall it only can while this Mac's own Proton VPN is on, "
                "because that VPN's Stealth protocol gets through. Turn it on and run: mc vpn fix")
    return ""
