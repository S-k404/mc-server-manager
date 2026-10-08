"""Health checks (`mc doctor`) and invite messages."""
import base64
import http.client
import re
import shutil
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import container as dk
from . import moddoctor, platform, share, vpn
from .core import ROOT, port_open
from .provision import installed_javas


@dataclass
class Check:
    group: str
    good: bool
    message: str
    fix: str = ""


# ---------- Minecraft / Microsoft sign-in reachability ----------
# Signing in needs the first four; the others are what an online-mode server and the launcher fetch afterwards.
SIGNIN_HOSTS = ("login.live.com", "login.microsoftonline.com", "user.auth.xboxlive.com", "xsts.auth.xboxlive.com",
                "api.minecraftservices.com")
OTHER_HOSTS = ("sessionserver.mojang.com", "textures.minecraft.net", "piston-meta.mojang.com")
AUTH_HOSTS = SIGNIN_HOSTS + OTHER_HOSTS
PROBE_SECONDS = 5


def probe(host, proxy=None, timeout=PROBE_SECONDS):
    """(reachable, detail). A TLS handshake plus any HTTP answer counts; a refusal, timeout, reset or an untrusted
    certificate (a firewall's block page) does not. `proxy` is vpn.proxy_info(): connect through its HTTP CONNECT."""
    try:
        if proxy:
            conn = http.client.HTTPSConnection(proxy["host"], proxy["port"], timeout=timeout,
                                               context=ssl.create_default_context())
            token = base64.b64encode(f"{proxy['user']}:{proxy['password']}".encode()).decode()
            conn.set_tunnel(host, 443, headers={"Proxy-Authorization": f"Basic {token}"})
        else:
            conn = http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())
        try:
            conn.request("HEAD", "/", headers={"User-Agent": "mc-local-cli"})
            return True, f"HTTP {conn.getresponse().status}"
        finally:
            conn.close()
    except ssl.SSLCertVerificationError:
        return False, "certificate not trusted: a firewall is probably intercepting it"
    except ssl.SSLError as e:
        return False, f"TLS failed ({e.reason or 'error'})"
    except socket.gaierror:
        return False, "name not found (DNS blocked?)"
    except (TimeoutError, socket.timeout):
        return False, f"no answer in {timeout} s"
    except ConnectionRefusedError:
        return False, "refused"
    except ConnectionResetError:
        return False, "connection reset (blocked)"
    except OSError as e:                                   # includes "Tunnel connection failed: 407 ..." from a proxy
        return False, ("the proxy rejected the password (407)" if "407" in str(e) else str(e)[:70] or type(e).__name__)
    except http.client.HTTPException as e:
        return False, f"bad reply ({type(e).__name__})"


PATIENT_SECONDS = 12
TRANSIENT = ("no answer", "connection reset", "TLS failed", "bad reply", "Tunnel connection failed")   # a slow or just-started route, not a verdict


def patient_probe(host, proxy=None, tries=2, pause=4, sleep=time.sleep):
    """probe() for a route that has only just come up (a tunnel inside the Mac's own VPN answers its first connections slowly: its
    DNS and TLS warm up). A host that doesn't answer gets PATIENT_SECONDS and one more go; a certificate or password
    problem is a verdict and is returned at once."""
    for attempt in range(tries):
        reachable, detail = probe(host, proxy, timeout=PATIENT_SECONDS)
        if reachable or not detail.startswith(TRANSIENT) or attempt == tries - 1:
            return reachable, detail
        sleep(pause)


def _probe_all(proxy, probe_fn, hosts):
    with ThreadPoolExecutor(max_workers=len(hosts)) as ex:       # in parallel: a blocked host costs 5 s, not 5 s each
        return dict(zip(hosts, ex.map(lambda h: probe_fn(h, proxy), hosts), strict=True))


SESSION_FIX = ("an online-mode server checks every player with this host from this Mac, so players can't join it while "
               "the Mac VPN is off. Either keep the Mac VPN on and turn on Proton's 'Allow LAN connections', or run that "
               "server in offline mode with logins (mc cracked on)")


def auth_checks(probe_fn=probe, hosts=AUTH_HOSTS, direct=True):
    """Can the Minecraft/Microsoft sign-in servers be reached straight from this Mac (`direct`), and through the Docker VPN's
    sign-in proxy? `probe_fn(host, proxy)` returns (reachable, detail); tests pass a fake."""
    direct_group, proxy_group = "Sign-in: straight from this Mac", "Sign-in: through the Docker VPN proxy"
    proxy = vpn.proxy_info() if vpn.enabled() else None
    via_proxy = _probe_all(proxy, probe_fn, hosts) if proxy and proxy["up"] else None
    checks = []

    def report(group, results, fix_for):
        for host, (good, detail) in results.items():
            checks.append(Check(group, good, f"{host} reachable ({detail})" if good else f"{host} blocked: {detail}",
                                "" if good else fix_for(host)))
        return all(results[h][0] for h in SIGNIN_HOSTS if h in results)

    if not direct:
        direct_ok = None
    elif platform.vpn_active():
        checks.append(Check(direct_group, True, "not measured: the Mac VPN is on, so a test would measure the VPN, "
                            "not this Wi-Fi (turn it off to see what the Wi-Fi blocks)"))
        direct_ok = None
    else:
        start = "mc vpn start, then put the settings from `mc vpn proxy` in your launcher"
        direct_ok = report(direct_group, _probe_all(None, probe_fn, hosts),
                           lambda h: SESSION_FIX if h == "sessionserver.mojang.com"
                           else ("" if h not in SIGNIN_HOSTS else
                                 "use the sign-in proxy: mc vpn proxy" if via_proxy else f"sign in through the Docker VPN: {start}"))
    proxy_ok = report(proxy_group, via_proxy, lambda h: "mc vpn logs  (and check the password: mc vpn proxy)") \
        if via_proxy else None
    if direct_ok:
        checks.append(Check("Sign-in", True, "this network lets Minecraft sign-in through; no VPN or proxy is needed"))
    elif direct_ok is False and proxy_ok:
        checks.append(Check("Sign-in", True, "sign-in is blocked on this Wi-Fi but works through the Docker VPN proxy: "
                            "put its settings in your launcher (mc vpn proxy)"))
    elif direct_ok is False and proxy_ok is False:
        checks.append(Check("Sign-in", False, "sign-in is blocked on this Wi-Fi and through the Docker VPN proxy too",
                            "mc vpn logs"))
    return checks


def doctor(servers, auth=False):
    """Health checks; returns a list of Check. `auth` adds the sign-in reachability probes (about 5 s when something is blocked)."""
    checks = []
    add = lambda group, good, msg_ok, msg_bad, fix="": checks.append(
        Check(group, good, msg_ok if good else msg_bad, "" if good else fix))
    ports = {}
    for s in servers:
        g = f"Server '{s.name}'"
        inst = s.inst
        add(g, inst.java_major in installed_javas(), f"Java {inst.java_major} present",
            f"Java {inst.java_major} missing", "downloads automatically on start")
        state = s.state()
        add(g, state != "crashed", f"state: {state}", "crashed last time it ran",
            f"see why: mc -s {s.name} crash")
        if s.loader_info.content:
            bad = moddoctor.problems(moddoctor.check(s))
            add(g, not bad, "mods: nothing missing, nothing in conflict",
                f"mods: {len(bad)} problem(s), e.g. {bad[0].message}" if bad else "", f"mc -s {s.name} mods doctor --fix")
        if state == "running":
            m = re.search(r"Average time per tick: ([\d.]+)ms", s.run("tick query"))
            if m:
                ms = float(m.group(1))
                add(g, ms < 40, f"tick time {ms} ms (no server lag)", f"tick time {ms} ms: server overloaded",
                    "lower view distance (mc set view-distance 6) or remove heavy mods")
            add(g, s.rcon() is not None, "RCON control channel on (strong random password)", "RCON off",
                f"restart it: mc -s {s.name} restart")
        if s.prop("online-mode") == "false":
            add(g, s.has_mod("easyauth"), "offline mode protected by EasyAuth logins",
                "offline mode WITHOUT a login mod: anyone can use an op's name", "mc mods add easyauth")
        if s.world_dir.exists():
            from . import backups
            newest = next(iter(backups.list_backups(s)), None)
            if newest is None:
                add(g, False, "", "no backups yet", f"mc -s {s.name} backup")
            else:
                st_ = backups.status(newest)
                add(g, st_ != "corrupt", f"latest backup {newest.name} ({st_})",
                    f"latest backup {newest.name} is corrupt", f"make a new one: mc -s {s.name} backup")
        add(g, s.prop("white-list") == "true", "whitelist on", "whitelist off: anyone with the address can join",
            "mc whitelist on")
        ports.setdefault(s.port, []).append(s.name)
    for p, names in ports.items():
        if len(names) > 1:
            add("Ports", False, "", f"port {p} shared by {', '.join(names)}", "mc -s <name> set server-port <n>")

    vpn_on = platform.vpn_active()
    lan = platform.proton_allows_lan() if vpn_on else None
    if not vpn_on:
        add("Network", True, "VPN off (Wi-Fi friends can connect)", "")
    elif lan is True:
        add("Network", True, "VPN on, and ProtonVPN allows local-network traffic (Wi-Fi friends can connect)", "")
    elif lan is False:
        add("Network", False, "",
            "VPN is on and ProtonVPN's 'Allow LAN connections' is off, so Wi-Fi friends can't reach this Mac",
            "ProtonVPN -> Settings -> Connection -> Allow LAN connections. It only lets local-network traffic skip the "
            "tunnel; internet traffic stays on the VPN. (mc doesn't change Proton's settings.)")
    else:
        add("Network", False, "", "VPN is on: Wi-Fi friends may be blocked",
            "enable your VPN app's local-network / LAN option, or use the Docker VPN (mc vpn) instead of a Mac-wide VPN")
    ip = platform.lan_ip()
    mode = share.mode()
    if mode == "local":
        add("Network", share.bonjour_alive(), f"the name {share.host()} is published (friends join by name, not number)",
            f"the name {share.host()} isn't published, so friends can't use it",
            "the agent keeps it alive (mc agent install); to publish it now: mc share mode local")
        warning = share.personal_name_warning()
        add("Network", warning is None, "this Mac's own Bonjour name doesn't contain a person's name", warning or "")
    for s in servers:
        if s.accepting() and not vpn_on:   # connecting to our own LAN address proves nothing while a VPN is on
            reachable = port_open(ip, s.port)
            if mode == "hidden":
                add("Network", not reachable, f"'{s.name}' is not reachable from the Wi-Fi (hidden mode)",
                    f"'{s.name}' is still reachable from the Wi-Fi although hidden mode is on",
                    f"restart it so it listens on this Mac only: mc -s {s.name} restart")
            else:
                shown = f"{share.host()}:{s.port}" if mode == "local" else f"{ip}:{s.port}"   # local mode never prints the number
                add("Network", reachable, f"'{s.name}' reachable on Wi-Fi at {shown}", f"'{s.name}' not reachable at {shown}",
                    "check the macOS firewall allows incoming connections")
        if (mode == "hidden" and s.accepting() and vpn.enabled() and vpn.forward_server() == s.name and dk.available()
                and dk.state(vpn.FORWARD) == "running"):
            worked, detail = share.probe_chain(s.port)
            add("Internet", worked, f"hidden mode works end to end: {detail}",
                f"hidden mode can't reach '{s.name}' through the VPN: {detail}",
                "if Docker Desktop doesn't deliver the forwarder's connection to this Mac's loopback, use local mode "
                "instead: mc share mode local")
        if s.docker and s.up():
            rcon = s.inst.rcon_port
            ports = (dk.inspect(s.container) or {}).get("NetworkSettings", {}).get("Ports", {})
            exposed = [b.get("HostIp") for b in (ports.get(f"{rcon}/tcp") or [])]
            add(f"Server '{s.name}'", exposed == ["127.0.0.1"], "RCON only reachable from this machine",
                f"RCON is published on {exposed}", f"restart it: mc -s {s.name} restart")
    if vpn.enabled():
        st = vpn.status()
        good = st.get("vpn_health") == "healthy" and st.get("containers", {}).get(vpn.VPN) == "running"
        add("Internet", good, f"Docker VPN healthy (exit {st.get('exit', '?')})",
            "Docker VPN is not running" if st.get("docker") else "Docker isn't running", "mc vpn start")
        if good and "proxy_up" in st:
            add("Internet", st["proxy_up"], f"launcher sign-in proxy answering at {vpn.PROXY_HOST}:{vpn.PROXY_PORT} (mc vpn proxy)",
                "the launcher sign-in proxy isn't answering (the VPN was started before it existed, or port "
                f"{vpn.PROXY_PORT} is taken)", "mc vpn start")
        if good:
            add("Internet", bool(st.get("public")),
                f"friends can join '{st.get('forward_server')}' at {st.get('public')}",
                "the VPN is up but Proton gave no forwarded port, so friends can't join from the internet",
                vpn.NEED_NATPMP)
    else:
        add("Internet", True, "internet play is off (friends on your Wi-Fi can still join; `mc vpn setup` turns it on)", "")
    free = shutil.disk_usage(ROOT).free / 2**30
    add("System", free > 5, f"{free:.0f} GB disk free", f"only {free:.1f} GB disk free", "free up disk space")
    if auth:
        checks += auth_checks()
    return checks


def display_address(s, live=False, show_ip=False):
    """Everything about where people join `s` that may be shown. Every screen and message asks here, so hidden mode can't
    leak the Wi-Fi address by one place forgetting. `show_ip=True` (mc share --show-ip) reveals it on purpose.
    Returns {mode, this_mac, wifi (what to tell people on the Wi-Fi, or None), internet (or None), note}."""
    mode = share.mode()
    wifi, note = None, ""
    if show_ip or mode is None:
        wifi = f"{platform.lan_ip()}:{s.port}"
    elif mode == "local":
        if share.bonjour_alive():
            wifi = f"{share.host()}:{s.port}"
        else:
            note = "the local name isn't published yet (mc share mode local)"
    else:
        note = "hidden: friends join through the internet address only"
    internet = vpn.public_address(live) if vpn.enabled() and vpn.forward_server() == s.name else None
    return {"mode": mode, "this_mac": f"localhost:{s.port}", "wifi": wifi, "internet": internet, "note": note}


def wifi_label(a):
    """The Wi-Fi part of display_address as short text for a screen."""
    return a["wifi"] or ("hidden" if a["mode"] == "hidden" else "name not published")


def invite(s, live=False, show_ip=False):
    """Message for friends. `live` asks Docker for the internet address now (the dashboard uses a cached one)."""
    a = display_address(s, live, show_ip)
    mode = a["mode"]
    lines = [f"Join my Minecraft server '{s.name}' (Java {s.version}):"]
    if a["wifi"]:
        lines.append(f"  Address: {a['wifi']}  (same Wi-Fi" + (")" if mode == "local" and not show_ip else ", VPN off)"))
    elif mode == "local":
        lines.append("  Address: the local name isn't published yet; I'll send it.")
    if a["internet"]:
        lines.append(f"  From anywhere: {a['internet']}  (this changes whenever the VPN reconnects; I'll send the new one)")
    elif mode == "hidden":
        lines.append("  Internet address: not ready yet; I'll send it as soon as the VPN is up.")
    if mode == "hidden":
        lines.append("  If Minecraft won't sign in on this Wi-Fi: use Proton VPN Free, sign in, stay on the VPN, and join "
                     "the internet address.")
    elif mode == "local":
        lines.append("  If Minecraft won't sign in on this Wi-Fi: use Proton VPN Free, sign in, turn on 'Allow LAN "
                     "connections' in it, then join.")
    if s.prop("online-mode") == "false":
        lines.append("  Cracked/offline accounts work.")
    if s.has_mod("easyauth"):
        lines.append("  First time: /register <password> <password>   After that: /login <password>")
    if s.has_mod("skinrestorer"):
        lines.append("  Set your skin: /skin set <minecraft name>")
    if s.prop("white-list") == "true":
        lines.append("  Send me your username so I can whitelist you.")
    return "\n".join(lines)
