"""Simple Voice Chat through the internet address.

The mod sends audio over UDP on a port of its own (24454 unless its config says otherwise). A player's client works out where to
send it from the address it joined with plus the port the server tells it, unless `voice_host` in voicechat-server.properties
says "host:port". Proton forwards ONE port, for TCP and for UDP alike, so:

  * the VPN's forwarder (vpn._forward_script) also listens for UDP on that forwarded port and hands it to the mod's own port, and
  * `voice_host` is set to "<exit ip>:<forwarded port>", so every client sends its audio there.

`voice_host` is global (every player uses it), so it is only set while everyone arrives through the internet address anyway:
in hidden mode, or while this Mac's own VPN is on (it blocks the Wi-Fi). Otherwise audio goes straight to this Mac, as before, and
a `voice_host` that mc wrote is taken out again. A `voice_host` somebody else wrote is never touched.

The address changes whenever the VPN reconnects, and the mod reads its config only at start, so the agent rewrites the line and
restarts the server once nobody is on it (`maintain`). Starting a server writes it first (`servers.start`), so no restart is needed then.
"""
import json
import re
import time

from . import platform, share, vpn
from .atomic import write_atomic, write_json_atomic
from .core import ROOT, all_servers

STATE_FILE = ROOT / ".mc-voice.json"        # per server: the voice_host mc wrote, and a restart that is waiting for the room to empty
DEFAULT_PORT = 24454
SETTLE = 120                                # seconds a change must stand before the server is restarted for it (the Mac's VPN may flap)

_installed = {}                             # (mods folder, its mtime) -> is Simple Voice Chat in it


def installed(s):
    """Is Simple Voice Chat in this server's mods (or plugins) folder? Cached until the folder changes."""
    try:
        key = (str(s.mods_dir), s.mods_dir.stat().st_mtime_ns)
    except OSError:
        return False
    if key not in _installed:
        from . import moddoctor                 # imported here: moddoctor asks this module about routes
        if len(_installed) > 64:
            _installed.clear()
        _installed[key] = any(m.id == "voicechat" for m in moddoctor.scan(s))
    return _installed[key]


def config_file(s):
    base = s.mods_dir / "voicechat" if s.loader_info.content == "plugins" else s.dir / "config" / "voicechat"
    return base / "voicechat-server.properties"


def props(s):
    """The mod's server config as a dict, or None while the mod hasn't written it yet (it does on its first start)."""
    from . import moddoctor
    return moddoctor._properties(config_file(s))


def udp_port(s):
    """The UDP port the mod listens on here: its `port` (24454 by default); -1 means it shares Minecraft's own port."""
    raw = (props(s) or {}).get("port", str(DEFAULT_PORT))
    port = int(raw) if raw.lstrip("-").isdigit() else DEFAULT_PORT
    return s.port if port == -1 else port


BIND_FAILURE = re.compile(r"voice.?chat[^\n]*?(address already in use|bindexception|failed to bind|could(?:n't| not) (?:bind|start)"
                          r"|failed to start|cannot assign requested address)[^\n]*", re.I)


def log_failure(s):
    """The line from the server's log that says Simple Voice Chat could not open its UDP port (taken by something else, or the
    address is wrong), or ''. Minecraft starts a fresh latest.log on every start, so this is about the current or last run."""
    from .game import tail_text
    try:
        text = tail_text(s.log_path)
    except OSError:
        return ""
    m = BIND_FAILURE.search(text)
    return m.group(0).strip()[:200] if m else ""


def udp_free(port, bind=""):
    """Can this Mac open UDP `port` (on `bind`, default every address) right now?"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind((bind or "0.0.0.0", port))
            return True
        except OSError:
            return False


def ensure_port(s):
    """Give this server's voice chat a UDP port nothing else holds: not another server's voice chat (two servers on this Mac can't
    share one: the second one's voice chat never starts) and not any program. Returns a sentence when it changed the port, else ''.
    `port=-1` (it shares Minecraft's own port) is left alone. Call it for a server that is stopped; for a running one it acts only
    when the log says the bind failed."""
    conf = props(s) if installed(s) else None
    if conf is None:
        return ""
    raw = conf.get("port", str(DEFAULT_PORT))
    port = int(raw) if raw.lstrip("-").isdigit() else DEFAULT_PORT
    if port == -1:
        return ""
    taken = {udp_port(o) for o in servers_using_voice() if o.name != s.name and o.up()}     # only a server that is running holds its port
    bind = conf.get("bind_address", "") or s.prop("server-ip")
    bind = "" if bind == "*" else bind
    out_of_range = not 1024 <= port <= 65535
    clash = out_of_range or port in taken or (log_failure(s) if s.up() else not udp_free(port, bind))
    if not clash:
        return ""
    for candidate in range(DEFAULT_PORT, DEFAULT_PORT + 100):
        if candidate != port and candidate not in taken and udp_free(candidate, bind):
            _set_property(config_file(s), "port", candidate)
            why = ("it is outside 1024-65535" if out_of_range else
                   "another server's voice chat has it" if port in taken else "it is in use")
            return f"voice chat port {port} can't be used ({why}): now {candidate}"
    return ""


def route(s):
    """'vpn' = audio should travel through the internet address, 'direct' = straight to this Mac."""
    if not (vpn.enabled() and vpn.forward_server() == s.name):
        return "direct"
    if share.mode() == "hidden":
        return "vpn"                            # nobody can reach the Wi-Fi: audio has to come the way the game does
    return "vpn" if platform.vpn_active() else "direct"     # this Mac's own VPN blocks the Wi-Fi: everyone arrives through the address


def wanted(s):
    """The voice_host this server should have: 'ip:port', '' (none: direct), or None (the address isn't known yet)."""
    return vpn.public_now() if route(s) == "vpn" else ""


def _set_property(path, key, value):
    text = path.read_text(errors="replace")
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=", text, re.M):
        text = re.sub(rf"^{re.escape(key)}=.*$", lambda m: line, text, count=1, flags=re.M)
    else:
        text += ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    write_atomic(path, text)


def _load():
    try:
        data = json.loads(STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    write_json_atomic(STATE_FILE, data, indent=None)


def sync(s, now=None):
    """Make `voice_host` say where audio should go. Returns a sentence when it changed the file, else ''."""
    if not installed(s):
        return ""
    current = props(s)
    if current is None:
        return ""
    want = wanted(s)
    if want is None:
        return ""
    have = current.get("voice_host", "")
    if have == want:
        return ""
    state = _load()
    mine = (state.get(s.name) or {}).get("value", "")
    if have and have != mine:
        return ""                               # somebody else's setting: never overwritten, never removed
    _set_property(config_file(s), "voice_host", want)
    entry = state.setdefault(s.name, {})
    entry["value"] = want
    entry["changed"] = now or time.time()
    _save(state)
    return f"voice chat address set to {want}" if want else "voice chat address cleared (audio goes straight to this Mac)"


def _bump(name, now):
    data = _load()
    data.setdefault(name, {})["changed"] = now
    _save(data)


def pending_restart(s):
    return bool((_load().get(s.name) or {}).get("pending"))


def maintain(s, log=print, now=None, settle=SETTLE):
    """Agent step: sync voice_host; when it changed under a running server, restart the server once the change has stood for a
    while and nobody is on it (the mod reads its config at start). Returns what it did, or ''."""
    now = now or time.time()
    done = ""
    try:
        changed = sync(s, now)
    except OSError as e:
        return f"voice chat: couldn't update its config: {e}"
    if s.up():
        try:
            moved = ensure_port(s)                  # acts only when the log shows the voice port could not be opened
        except OSError:
            moved = ""
        if moved:
            changed = (changed + "; " if changed else "") + moved
            _bump(s.name, now)
    state = _load()
    entry = state.get(s.name) or {}
    if changed:
        log(f"{s.name}: {changed}")
        done = changed
        if s.up():
            entry["pending"] = True
    if not s.up():
        entry.pop("pending", None)
    elif entry.get("pending") and now - entry.get("changed", 0) >= settle and not s.busy() and s.accepting():
        from . import countdown, events, players
        if not countdown.pending(s) and not players.online(s):
            countdown.schedule(s, "restart", "15s")
            events.add(s.name, "info", "restarting so voice chat uses its new address")
            entry.pop("pending", None)
            done = (done + "; " if done else "") + "restarting (nobody is on) so voice chat uses it"
    if entry or s.name in state:
        state[s.name] = entry
        _save(state)
    return done


def listening_udp_ports(proc_net_udp):
    """Ports with a bound UDP socket (state 07), from the text of /proc/net/udp (and udp6)."""
    found = set()
    for line in proc_net_udp.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[3] == "07" and ":" in parts[1]:
            try:
                found.add(int(parts[1].rsplit(":", 1)[1], 16))
            except ValueError:
                pass
    return found


def servers_using_voice():
    return [s for s in all_servers() if installed(s)]
