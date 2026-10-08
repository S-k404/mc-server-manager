"""Everything OS-specific lives here, so the rest of mcctl runs on macOS now and Linux later."""
import platform as _platform
import shutil
import socket
import subprocess
import time
import sys
from pathlib import Path

import psutil

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def keep_awake_prefix():
    """Command prefix that stops the machine sleeping while a server runs."""
    if IS_MAC and shutil.which("caffeinate"):
        return ["caffeinate", "-i"]
    return []


_ttl_cache = {}


def _cached(key, ttl, fn):
    """fn()'s result, reused for `ttl` seconds. These answers change rarely but used to cost a subprocess every second."""
    now = time.monotonic()
    hit = _ttl_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _ttl_cache[key] = (now, value)
    return value


def lan_ip():
    """This machine's address on the local network (not the VPN tunnel). Cached for 30 s."""
    return _cached("lan_ip", 30, _lan_ip)


def _lan_ip():
    if IS_MAC:
        for iface in ("en0", "en1"):
            out = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True).stdout.strip()
            if out:
                return out
    for name, addrs in psutil.net_if_addrs().items():
        if name.startswith(("lo", "utun", "tun", "wg", "docker", "br-", "veth")):
            continue
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                return a.address
    return "unknown"


def vpn_active():
    """True when the default route goes through a tunnel interface (VPN). Cached for 3 s."""
    return _cached("vpn_active", 3, _vpn_active)


def _vpn_active():
    if IS_MAC:
        out = subprocess.run(["netstat", "-rn", "-f", "inet"], capture_output=True, text=True).stdout
        defaults = [ln.split()[-1] for ln in out.splitlines() if ln.startswith("default")]
        return bool(defaults) and defaults[0].startswith("utun")
    try:
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout
    except OSError:
        return False                                  # no `ip` (a minimal Linux image): can't tell, so assume no tunnel
    return any(t in out for t in ("dev tun", "dev wg"))


def proton_allows_lan():
    """True/False for ProtonVPN's own "Allow LAN connections" setting, None if it can't be read.

    Read-only: mc never changes another app's settings, routes or firewall. With the setting off, Proton's tunnel
    captures local-network traffic and friends on the same Wi-Fi can't reach this machine.
    """
    if not IS_MAC:
        return None
    import plistlib
    try:   # `defaults export` reads through macOS's preferences daemon (the plist file itself isn't current)
        out = subprocess.run(["defaults", "export", "ch.protonvpn.mac", "-"], capture_output=True, timeout=10)
        prefs = plistlib.loads(out.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    for key, value in prefs.items():
        if key.startswith("excludeLocalNetworks"):
            text = (value.decode(errors="replace") if isinstance(value, bytes) else str(value)).strip('" ').lower()
            return text in ("on", "true", "1", "yes")
    return None


def copy_to_clipboard(text):
    for tool in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(tool[0]):
            subprocess.run(tool, input=text, text=True)
            return True
    return False


def notify(title, message):
    """Desktop notification; silently does nothing where unsupported."""
    if IS_MAC:
        script = f'display notification {json_str(message)} with title {json_str(title)}'
        subprocess.run(["osascript", "-e", script], capture_output=True)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", title, message], capture_output=True)


def json_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def total_ram_gb():
    return psutil.virtual_memory().total // 2**30


def java_target():
    """(os, arch) names as used by the Adoptium API."""
    os_name = "mac" if IS_MAC else "linux"
    arch = "aarch64" if _platform.machine() in ("arm64", "aarch64") else "x64"
    return os_name, arch


def java_bins(runtime_dir):
    """All java binaries inside extracted JRE folders (macOS bundle or Linux layout)."""
    yield from Path(runtime_dir).glob("*/Contents/Home/bin/java")
    yield from Path(runtime_dir).glob("*/bin/java")


def tmux_path():
    return shutil.which("tmux") or ("/opt/homebrew/bin/tmux" if IS_MAC else "/usr/bin/tmux")
