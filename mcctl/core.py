"""Shared paths, reporting, settings, HTTP helpers, and the Server model."""
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, loaders, platform
from .atomic import write_atomic, write_json_atomic  # noqa: F401  (re-exported: core.write_json_atomic)
from .rcon import Rcon, RconError

ROOT = Path(os.environ.get("MC_HOME") or Path(__file__).resolve().parent.parent)
SERVERS = ROOT / "servers"
BACKUPS = ROOT / "backups"
RUNTIME = ROOT / "runtime"
TUNNEL_DIR = ROOT / "tunnel"
SETTINGS_FILE = ROOT / ".mc-settings.json"
TMUX = platform.tmux_path()
UA = "mc-server-manager/3.0 (+https://github.com/S-k404/mc-server-manager)"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


# ---------- reporting ----------
class Fail(Exception):
    """Abort the current action with a user-facing message."""


class Busy(Fail):
    """Another operation already holds the lock (a backup is running, the agent is mid-restart, ...)."""


def die(msg):
    raise Fail(msg)


class Reporter:
    """Where progress/results go. The CLI prints with Rich; the dashboard swaps in its own."""

    def ok(self, msg): print("[ok] " + msg)
    def warn(self, msg): print("! " + msg)
    def info(self, msg): print(msg)
    def heading(self, msg): print(msg)
    def progress(self, msg): print(msg, end="", flush=True)


_reporter = Reporter()


def set_reporter(r):
    global _reporter
    _reporter = r


def ok(msg): _reporter.ok(msg)
def warn(msg): _reporter.warn(msg)
def info(msg): _reporter.info(msg)
def head(msg): _reporter.heading(msg)
def progress(msg): _reporter.progress(msg)


# ---------- safe writes, locks, disk space ----------
_held = threading.local()


def _holder(path):
    try:
        return json.loads(Path(path).read_text() or "{}")
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def file_lock(path, what, wait=10.0, label="this"):
    """An exclusive lock on `path` (flock: released by the OS even if the process dies, so there is never a stale lock).
    Waits up to `wait` seconds, then raises Busy naming what holds it. Re-entrant for the same thread, so stop() inside
    restart(), or a backup inside stop(), is fine."""
    path = Path(path)
    held = _held.__dict__.setdefault("locks", {})
    if str(path) in held:
        held[str(path)][1] += 1
        try:
            yield
        finally:
            held[str(path)][1] -= 1
        return
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except FileNotFoundError:
        raise Fail(f"{label} doesn't exist any more (its folder is gone).") from None
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    h = _holder(path)
                    since = f", started {int(time.time() - h['since'])} s ago" if h.get("since") else ""
                    raise Busy(f"Another operation is already running on {label}: {h.get('what', 'something')}{since}. "
                               "Try again when it finishes.") from None
                time.sleep(0.2)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "what": what, "since": time.time()}).encode())
        held[str(path)] = [fd, 1]
        try:
            yield
        finally:
            del held[str(path)]
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
    finally:
        os.close(fd)


def lock_busy(path):
    """What currently holds the lock on `path` (a sentence), or None when it is free. Never holds it afterwards."""
    path = Path(path)
    if str(path) in _held.__dict__.get("locks", {}) or not path.exists():
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            h = _holder(path)
            return h.get("what") or "something"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def free_gb(path):
    """Free disk space (GB) where `path` would be written; walks up to the nearest folder that exists."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 2**30


def need_gb(*sizes_bytes):
    """How much free space an operation should have: 1.2 times the data it writes, but never less than 2 GB."""
    return max(2.0, 1.2 * sum(sizes_bytes) / 2**30)


def require_disk(path, need, what):
    """Refuse (before touching anything) when `path` has less than `need` GB free."""
    free = free_gb(path)
    if free < need:
        die(f"Not enough free disk space to {what}: {free:.1f} GB free, about {need:.1f} GB needed. "
            "Free some space (or move the mc folder to a bigger disk) and try again.")


# ---------- tool-wide settings ----------
def setting(key, default=None):
    try:
        return json.loads(SETTINGS_FILE.read_text()).get(key, default)
    except (OSError, ValueError):
        return default


def save_setting(key, value):
    with file_lock(ROOT / ".mc-settings.lock", "saving a setting", wait=5, label="the settings file"):
        try:
            data = json.loads(SETTINGS_FILE.read_text())
        except (OSError, ValueError):
            data = {}
        data[key] = value
        write_json_atomic(SETTINGS_FILE, data)


# ---------- system ----------
_SESSIONS = (0.0, frozenset())      # when tmux was last asked about every session, and what it said
SESSIONS_TTL = 0.8                  # seconds; anything that starts or ends a session clears it at once
SESSION_CHANGES = {"new-session", "kill-session", "kill-server", "rename-session", "new", "kill-ses"}


def tmux(*args, capture=False):
    global _SESSIONS
    if args and args[0] in SESSION_CHANGES:
        _SESSIONS = (0.0, frozenset())              # the next question must ask tmux again
    return subprocess.run([TMUX, *args], capture_output=capture, text=True)


def session_names():
    """Names of every tmux session, in one call (the dashboard asks about all servers at once instead of one tmux each)."""
    global _SESSIONS
    r = tmux("list-sessions", "-F", "#{session_name}", capture=True)
    names = set(r.stdout.split()) if r.returncode == 0 else set()
    _SESSIONS = (time.monotonic(), frozenset(names))
    return names


def recent_session_names():
    """Like session_names() but a burst of questions (a settings list asks 'is it running?' dozens of times) shares one tmux call."""
    at, names = _SESSIONS
    if time.monotonic() - at < SESSIONS_TTL:
        return names
    return session_names()


def dir_size(path):
    """Bytes in every file under `path` (symlinks not followed). os.scandir keeps the type and size from the directory read, which
    makes this several times faster than walking with pathlib on a world of tens of thousands of files."""
    total, stack = 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for e in entries:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def port_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


# ---------- http ----------
def http(url, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError) as e:
        die(f"Network error reaching {urllib.parse.urlparse(url).netloc}: {getattr(e, 'reason', e)}")


def http_text(url):
    """A small text file (e.g. a maven `.sha1`), stripped; dies with a clear message when it can't be fetched."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode().strip()
    except (urllib.error.URLError, TimeoutError) as e:
        die(f"Network error reaching {urllib.parse.urlparse(url).netloc}: {getattr(e, 'reason', e)}")


def download(url, dest, sha1_expected=None, sha256_expected=None):
    """Download to dest atomically; verify sha1 and/or sha256 when given."""
    dest = Path(dest)
    require_disk(dest.parent, 2.0, f"download {dest.name}")
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except (urllib.error.URLError, TimeoutError) as e:
        tmp.unlink(missing_ok=True)
        die(f"Download failed for {dest.name}: {getattr(e, 'reason', e)}")
    if sha1_expected and sha1(tmp) != sha1_expected.lower() or sha256_expected and sha256(tmp) != sha256_expected.lower():
        tmp.unlink()
        die(f"Checksum mismatch downloading {dest.name}; nothing installed.")
    tmp.rename(dest)
    return dest


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def strip_log_prefix(line):
    return re.sub(r"^\[[\d:]+\] \[[^\]]+\]: ", "", line)


PLAYER_LIST_RE = re.compile(r"(?:of a max of (\d+) )?players online:?[ \t]*(.*)")


def parse_player_list(reply):
    """(max_players or None, [names]) from a `list` reply, or None if it isn't one.

    Stays on the reply's own line: a console-fallback reply is several log lines, and an empty list
    must not pick up the next line as a player name."""
    m = PLAYER_LIST_RE.search(reply)
    if not m:
        return None
    return (int(m.group(1)) if m.group(1) else None), [n.strip() for n in m.group(2).split(",") if n.strip()]


# ---------- servers ----------
_rcon_clients = {}
_rcon_lock = threading.Lock()


class Server:
    """One server instance living in servers/<name>/."""

    def __init__(self, name):
        self.name = name
        self.dir = SERVERS / name
        self.cfg_path = self.dir / "instance.json"
        self.state_dir = self.dir / ".mc"

    # ----- config -----
    @property
    def inst(self):
        return config.load(self.cfg_path)

    def update(self, **changes):
        with file_lock(self.dir / ".mc-config.lock", "saving settings", wait=5, label=f"'{self.name}'"):
            inst = self.inst                     # read, change and write under one lock: the agent and the CLI both do this
            for k, v in changes.items():
                setattr(inst, k, v)
            config.save(self.cfg_path, inst)
        return inst

    def op_lock(self, what, wait=10.0):
        """Hold this server's operation lock: only one of start / stop / backup / restore / upgrade / rename / import runs at a
        time, whether it came from the CLI, the dashboard or the agent. Raises Busy (a Fail) after `wait` seconds."""
        return file_lock(self.dir / ".mc-lock", what, wait, label=f"'{self.name}'")

    def busy(self):
        """What is running on this server right now (a sentence), or None."""
        return lock_busy(self.dir / ".mc-lock")

    @property
    def version(self):
        return self.inst.mc_version

    @property
    def loader(self):
        return self.inst.loader

    @property
    def label(self):
        inst = self.inst
        return f"{inst.mc_version} {inst.loader.capitalize()}"

    # ----- server.properties -----
    def props(self):
        p = self.dir / "server.properties"
        out = {}
        if p.exists():
            for line in p.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out[k] = v
        return out

    def prop(self, key, default=""):
        return self.props().get(key, default)

    def has_prop(self, key):
        return key in self.props()

    def set_prop(self, key, value):
        p = self.dir / "server.properties"
        text = p.read_text() if p.exists() else ""
        line = f"{key}={value}"
        if re.search(rf"^{re.escape(key)}=", text, re.M):
            text = re.sub(rf"^{re.escape(key)}=.*$", lambda m: line, text, flags=re.M)
        else:
            text += ("" if text.endswith("\n") or not text else "\n") + line + "\n"
        p.write_text(text)

    @property
    def port(self):
        return int(self.prop("server-port", str(self.inst.port)))

    # ----- process & state -----
    @property
    def session(self):
        return f"mc-{self.name}"

    @property
    def runtime(self):
        return self.inst.runtime

    @property
    def docker(self):
        return self.runtime == "docker"

    @property
    def container(self):
        return f"minecraftvps-{self.name}"

    def up(self, sessions=None):
        """Process alive: the tmux session exists, or the container is running/restarting.
        `sessions` (from session_names()) lets a caller that checks many servers share one tmux call."""
        if self.docker:
            from . import container as dk
            return dk.state(self.container) in ("running", "restarting")
        return self.session in (sessions if sessions is not None else recent_session_names())

    def listening(self, timeout=0.5):
        """The server answers on its port. For Docker that's the container's own healthcheck, because Docker
        publishes the port on the host before the server inside is ready."""
        if self.docker:
            from . import container as dk
            return dk.health(self.container) == "healthy"
        return port_open("127.0.0.1", self.port, timeout)

    def accepting(self):
        return self.up() and self.listening(1.5)

    def last_exit(self):
        if self.docker:
            from . import container as dk
            return dk.exit_code(self.container)
        p = self.state_dir / "exit"
        try:
            return int(p.read_text().strip())
        except (OSError, ValueError):
            return None

    def started_at(self):
        if self.docker:
            from . import container as dk
            return dk.started_at(self.container)
        p = self.state_dir / "started"
        try:
            return float(p.read_text().strip())
        except (OSError, ValueError):
            return None

    def state(self, sessions=None):
        """running | starting | stopped | crashed"""
        if self.up(sessions):
            return "running" if self.listening(0.5) else "starting"
        code = self.last_exit()
        if self.inst.desired == "running" and code not in (0, None):
            return "crashed"
        return "stopped"

    def java_flags(self):
        inst = self.inst
        return [*loaders.gc_flags(inst.ram_gb, inst.gc), "--enable-native-access=ALL-UNNAMED", *loaders.launch_args(inst)]

    def launch_argv(self, java):
        return [*platform.keep_awake_prefix(), str(java), *self.java_flags()]

    def java_pid(self):
        if self.docker:
            return None  # the process lives in the container's own PID namespace; see resources.py
        out = tmux("list-panes", "-t", f"={self.session}:", "-F", "#{pane_pid}", capture=True).stdout.split()
        if not out:
            return None
        import psutil
        try:
            root = psutil.Process(int(out[0]))
            for p in [root, *root.children(recursive=True)]:
                if p.name().startswith("java"):
                    return p.pid
        except psutil.Error:
            return None
        return None

    # ----- RCON -----
    def ensure_rcon(self, used_ports=()):
        """Make sure RCON is enabled with a strong password on a free port."""
        p = self.props()
        port = self.inst.rcon_port or int(p.get("rcon.port", 0) or 0)
        if not port or port == 25575 and self.port != 25565 or port in used_ports:
            port = self.port + 10
            while port in used_ports:
                port += 1
        if p.get("enable-rcon") != "true":
            self.set_prop("enable-rcon", "true")
        if len(p.get("rcon.password", "")) < 20:
            self.set_prop("rcon.password", secrets.token_urlsafe(24))
        if p.get("rcon.port") != str(port):
            self.set_prop("rcon.port", port)
        if p.get("broadcast-rcon-to-ops") != "false":
            self.set_prop("broadcast-rcon-to-ops", "false")
        if self.inst.rcon_port != port:
            self.update(rcon_port=port)

    def rcon(self):
        p = self.props()
        if p.get("enable-rcon") != "true" or not p.get("rcon.password"):
            return None
        key = (self.name, int(p.get("rcon.port", 25575)), p["rcon.password"])
        with _rcon_lock:
            client = _rcon_clients.get(key)
            if client is None:
                client = _rcon_clients[key] = Rcon("127.0.0.1", key[1], key[2])
            return client

    # ----- console -----
    @property
    def log_path(self):
        return self.dir / "logs" / "latest.log"

    def log_size(self):
        return self.log_path.stat().st_size if self.log_path.exists() else 0

    def send(self, command):
        """Type a command into the server console (no reply)."""
        if self.docker:
            from . import container as dk
            dk.send_stdin(self.container, command)
            return
        tmux("send-keys", "-t", f"={self.session}:", "-l", command)
        tmux("send-keys", "-t", f"={self.session}:", "Enter")

    def run(self, command, wait=1.5):
        """Run a console command and return its reply text."""
        client = self.rcon()
        if client and self.accepting():
            try:
                # Minecraft shares one RCON output buffer between connections, so commands from
                # different mc processes must take turns or their replies get mixed together.
                self.state_dir.mkdir(exist_ok=True)
                with open(self.state_dir / "rcon.lock", "w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    return client.command(command)
            except RconError as e:
                if os.environ.get("MC_DEBUG"):
                    print(f"[debug] RCON failed for {command!r}: {e}; using console", flush=True)
                # RCON not up yet (still starting): fall back to the console
        start = self.log_size()
        self.send(command)
        time.sleep(wait)
        if not self.log_path.exists():
            return ""
        with open(self.log_path, "rb") as f:
            f.seek(start)
            lines = f.read().decode(errors="replace").strip().splitlines()
        return "\n".join(strip_log_prefix(ln) for ln in lines)

    def run_and_show(self, command, wait=1.5):
        reply = self.run(command, wait)
        for line in reply.splitlines() or ["(no reply)"]:
            info(line)
        return reply

    def require_up(self):
        if not self.accepting():
            die(f"Server '{self.name}' is not running. Start it: mc -s {self.name} start")

    def require_stopped(self, action):
        if self.up():
            die(f"Stop '{self.name}' before you {action}: mc -s {self.name} stop")

    # ----- paths -----
    @property
    def loader_info(self):
        return loaders.get(self.inst.loader)

    @property
    def mods_dir(self):
        """Where add-ons live: mods/ (Fabric, Quilt, Forge, NeoForge) or plugins/ (Paper)."""
        return self.dir / (self.loader_info.content or "mods")

    @property
    def world_name(self):
        return self.prop("level-name") or "world"

    @property
    def world_dir(self):
        return self.dir / self.world_name

    def world_dirs(self):
        """Every folder that is part of the world on disk (Paper keeps the Nether and End in sibling folders)."""
        dirs = [self.world_dir]
        if self.loader_info.separate_dimensions:
            dirs += [self.dir / f"{self.world_name}{suffix}" for suffix in ("_nether", "_the_end")]
        return [d for d in dirs if d.exists()]

    @property
    def backups_dir(self):
        return BACKUPS / self.name

    def json_list(self, file):
        p = self.dir / file
        try:
            return json.loads(p.read_text()) if p.exists() else []
        except ValueError:
            return []

    def has_mod(self, prefix):
        return self.mods_dir.exists() and any(self.mods_dir.glob(f"{prefix}*.jar"))


def dk_tail(s, lines=30):
    """Last lines of a Docker-runtime server's output, to show before attaching to its console."""
    from . import container
    r = container.docker("logs", "--tail", str(lines), s.container)
    return (r.stdout + r.stderr).rstrip()


def all_servers():
    if not SERVERS.exists():
        return []
    return [Server(p.name) for p in sorted(SERVERS.iterdir()) if (p / "instance.json").exists()]


def get_server(name):
    s = Server(name)
    if not s.cfg_path.exists():
        names = ", ".join(x.name for x in all_servers()) or "none yet"
        die(f"No server named '{name}'. Servers: {names}")
    return s


def default_server_name():
    servers = all_servers()
    chosen = setting("default")
    if chosen and any(s.name == chosen for s in servers):
        return chosen
    if len(servers) == 1:
        return servers[0].name
    return None


def used_ports(exclude=None):
    """Game and RCON ports claimed by servers (other than `exclude`)."""
    ports = set()
    for s in all_servers():
        if s.name == exclude:
            continue
        ports.add(s.port)
        rp = s.inst.rcon_port or s.prop("rcon.port")
        if rp:
            ports.add(int(rp))
    return ports


def free_port(start=25565, extra=()):
    taken = used_ports() | set(extra)
    port = start
    while port in taken or port + 10 in taken or port_open("127.0.0.1", port, 0.3):
        port += 1
    return port
