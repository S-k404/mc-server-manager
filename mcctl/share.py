"""Share modes: how friends find a server without being handed this Mac's Wi-Fi address.

Unset (the default), nothing changes: the invite shows the Wi-Fi address like it always did. Two modes, switched with
`mc share mode hidden|local` (machine-wide, stored as the `share_mode` setting):

  hidden  The server listens on 127.0.0.1 only (`server-ip`, applied at every start; Docker servers publish their port on
          127.0.0.1 instead). Friends join only through the Docker VPN's forwarded address, so the Wi-Fi never sees a
          Minecraft server on this Mac. The VPN's forwarder reaches the server over Docker's host gateway; whether Docker
          Desktop delivers that to the loopback is checked for real by `probe_chain` (mc doctor), not assumed.
  local   The server listens on the Wi-Fi as usual, but the invite carries a name (`mc-ab12.local`, published with macOS's own
          `dns-sd`) instead of the number. That only hides the number from casual view: anyone on the same Wi-Fi who looks can
          still find this Mac. The name is a child process kept alive by the agent; it changes no system setting.

mc never changes another app's settings or the system's: the Mac's own Bonjour name is only read, never set.
"""
import json
import os
import random
import re
import shutil
import signal
import string
import subprocess
import time

from . import container as dk
from . import platform, vpn
from .atomic import write_json_atomic
from .core import ROOT, all_servers, default_server_name, die, get_server, info, ok, save_setting, setting, warn

MODES = {
    "hidden": "Friends join only through the Docker VPN address. This Mac's Wi-Fi address is never shown, and the server "
              "doesn't listen on the Wi-Fi. Costs about 60-120 ms. Needs the Docker VPN with a forwarded port (mc vpn).",
    "local": "Friends type a name like mc-ab12.local instead of the number. That hides the number from casual view only: "
             "anyone on the same Wi-Fi who looks can still find this Mac.",
}
LOOPBACK = "127.0.0.1"
STATE_FILE = ROOT / ".mc-bonjour.json"
SERVICE = "_minecraft._tcp"


def mode():
    m = setting("share_mode")
    return m if m in MODES else None


def wanted_bind():
    """The `server-ip` this mode wants: loopback (hidden), empty = every interface (local), or None = leave it alone."""
    return {"hidden": LOOPBACK, "local": ""}.get(mode())


def publish_host():
    """Docker servers publish their game port on this prefix of `-p`."""
    return f"{LOOPBACK}:" if mode() == "hidden" else ""


def apply_bind(s):
    """Called before a server starts: put `server-ip` where the mode wants it. A server-ip somebody set on purpose to
    something else is only replaced for hidden (which needs loopback), never in local mode."""
    want = wanted_bind()
    if want is None or s.docker:
        return
    cur = s.prop("server-ip")
    if want == LOOPBACK and cur != LOOPBACK:
        s.set_prop("server-ip", LOOPBACK)
        info(f"  hidden mode: '{s.name}' listens on this Mac only (server-ip={LOOPBACK})")
    elif want == "" and cur == LOOPBACK:
        s.set_prop("server-ip", "")
        info(f"  local mode: '{s.name}' listens on the Wi-Fi again (server-ip cleared)")


# ---------- the Bonjour name (local mode) ----------
def alias():
    """This Mac's invite name, `mc-` plus four random characters, made once."""
    name = setting("share_alias")
    if not (isinstance(name, str) and re.fullmatch(r"mc-[a-z0-9]{4}", name)):
        name = "mc-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        save_setting("share_alias", name)
    return name


def host():
    return f"{alias()}.local"


def bonjour_args(name, port, hostname, ip):
    """`dns-sd -P`: register a proxy service, which also publishes an address record for `hostname`."""
    return ["dns-sd", "-P", name, SERVICE, "local", str(port), hostname, ip]


def _state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _is_dns_sd(pid):
    """Is this pid alive and really a dns-sd process (a reused pid must never be killed or trusted)?"""
    try:
        import psutil
        p = psutil.Process(int(pid))
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE and "dns-sd" in " ".join(p.cmdline())
    except Exception:                                  # noqa: BLE001 - gone, or not ours to inspect
        return False


def bonjour_alive():
    """Is the name being published right now? Cached for 3 s: the dashboard asks every second."""
    def look():
        st = _state()
        return bool(st.get("pid")) and _is_dns_sd(st["pid"])
    return platform._cached("bonjour_alive", 3, look)


def _forget():
    platform._ttl_cache.pop("bonjour_alive", None)


def bonjour_stop():
    st = _state()
    if st.get("pid") and _is_dns_sd(st["pid"]):
        try:
            os.kill(int(st["pid"]), signal.SIGTERM)
        except OSError:
            pass
    STATE_FILE.unlink(missing_ok=True)
    _forget()


def _default_port():
    names = [s.name for s in all_servers()]
    chosen = default_server_name()
    return get_server(chosen if chosen in names else names[0]).port if names else None


def bonjour_ensure():
    """Keep the name published in local mode and not otherwise. Called by the agent every pass and by `set_mode`.
    Returns a sentence when it changed something, else None."""
    if mode() != "local":
        if _state():
            bonjour_stop()
            return "stopped publishing the local name"
        return None
    if not (platform.IS_MAC and shutil.which("dns-sd")):
        return None
    ip, port = platform.lan_ip(), _default_port()
    if ip in ("", "unknown") or port is None:
        return None
    want = {"ip": ip, "port": port, "host": host()}
    st = _state()
    if st.get("pid") and _is_dns_sd(st["pid"]) and all(st.get(k) == v for k, v in want.items()):
        return None
    bonjour_stop()
    proc = subprocess.Popen(bonjour_args("mc", port, want["host"], ip), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    write_json_atomic(STATE_FILE, {**want, "pid": proc.pid, "at": time.time()}, indent=None)
    _forget()
    return f"published {want['host']} -> {ip}"


def local_hostname():
    """The Mac's own Bonjour name (read-only: `scutil --get LocalHostName`), or None."""
    if not platform.IS_MAC:
        return None
    r = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def personal_name_warning():
    """A sentence when the Mac's own Bonjour name looks like it contains a person's name, else None. The default macOS
    name is "<First name>s-MacBook-Pro", and every device on the Wi-Fi can see it whatever mc does."""
    name = local_hostname()
    if not name:
        return None
    import getpass
    user = getpass.getuser().lower()
    if re.match(r"(?i)^[a-z]+s-(macbook|mac-mini|mac-studio|mac-pro|imac)", name) or (len(user) >= 3 and user in name.lower()):
        return (f"this Mac's own Bonjour name '{name}.local' looks like it contains a name, and anyone on the Wi-Fi can see it "
                "(System Settings -> General -> Sharing -> Local hostname; mc never changes it)")
    return None


# ---------- switching ----------
def needs_restart(s):
    """Does this running server have to restart before the current mode applies? Mirrors apply_bind (and, for Docker,
    the host address its game port was published on)."""
    want = wanted_bind()
    if want is None or not s.up():
        return False
    if s.docker:
        ports = (dk.inspect(s.container) or {}).get("NetworkSettings", {}).get("Ports", {})
        hosts = {b.get("HostIp") for b in (ports.get(f"{s.port}/tcp") or [])}
        return hosts != {LOOPBACK} if want == LOOPBACK else hosts == {LOOPBACK}
    cur = s.prop("server-ip")
    return cur != LOOPBACK if want == LOOPBACK else cur == LOOPBACK


def set_mode(new, say=ok):
    """Choose a share mode. Returns the names of running servers that need a restart for it to take effect."""
    if new not in MODES:
        die("Choose a share mode: hidden or local.")
    if new == "local":
        if not (platform.IS_MAC and shutil.which("dns-sd")):
            die("Local-name mode needs macOS's own dns-sd tool, which isn't available here. Use hidden mode, or keep "
                "sharing the address as before.")
        if _default_port() is None:
            die("Create a server first (mc create): the local name is published for your servers.")
        if platform.lan_ip() in ("", "unknown"):
            die("This Mac has no Wi-Fi/LAN address right now, so there is nothing to give a name to. Connect to a network.")
    save_setting("share_mode", new)
    say(f"Share mode: {new}. {MODES[new]}")
    if new == "local":
        bonjour_ensure()
        time.sleep(0.5)
        if not bonjour_alive():
            die("Couldn't publish the local name: dns-sd stopped right away. Try by hand: dns-sd -G v4 localhost")
        say(f"Friends can join as {host()}:<port>")
    else:
        bonjour_stop()
        if not vpn.enabled():
            warn("Hidden mode shares only the Docker VPN address, and the Docker VPN isn't set up: nobody can join until "
                 "it is (mc vpn setup).")
        if any(s.has_mod("voicechat") for s in all_servers()):
            warn("Voice chat can't work in hidden mode: its UDP port isn't forwarded by the VPN, and the server no longer "
                 "listens on the Wi-Fi.")
    return [s.name for s in all_servers() if needs_restart(s)]


# ---------- proof that hidden mode still works ----------
def probe_chain(port, timeout=15):
    """Send a real Minecraft status request into the VPN's forwarder and see whether the server answers: forwarder ->
    Docker's host gateway -> this Mac's server (on its loopback in hidden mode). Returns (worked, detail)."""
    worked, _ = vpn.probe_in_forwarder("127.0.0.1", port, timeout)
    if worked:
        return True, "the VPN's forwarder reached the server"
    if dk.state(vpn.FORWARD) != "running":
        return False, "the VPN's forwarder isn't running"
    return False, "the forwarder couldn't get an answer from the server"
