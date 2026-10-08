"""Background agent: autostart, crash restarts, schedules, notifications.

Runs as a launchd LaunchAgent (`mc agent install`) and loops every TICK seconds. Everything it does
is driven by fields in each server's instance.json (autostart, keep_alive, schedules), so the CLI
and dashboard configure it simply by changing settings.
"""
import fcntl
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import alerts, backups, countdown, events, metrics, platform, servers, share, voice, vpn
from . import container as dk
from .atomic import write_atomic, write_json_atomic
from .core import ROOT, Fail, all_servers, die, info, ok, session_names, setting, warn

TICK = 30
IDLE_TICK = 120   # nothing running, nothing wanted running, no VPN to keep up: no need to look every 30 s
LABEL = "local.mcctl.agent"
# Before this tool was published its launchd job was named after the login (com.<login>.mc-agent). An agent still
# installed under that name is stopped and removed by install/uninstall, so a Mac never ends up with two.
LEGACY_LABELS = (f"com.{Path.home().name}.mc-agent",)
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST = LAUNCH_DIR / f"{LABEL}.plist"
UNIT_NAME = "mc-agent.service"
UNIT = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
STATE_FILE = ROOT / ".mc-agent.json"
LOG_FILE = ROOT / "logs" / "agent.log"
MAX_CRASHES = 3                      # within CRASH_WINDOW -> stop restarting
CRASH_WINDOW = 15 * 60


# ---------- schedules ----------
def parse_every(text):
    """'6h', '90m', '1d' -> seconds."""
    m = re.fullmatch(r"(\d+)\s*([mhd])", text.strip().lower())
    if not m:
        die("Use a duration like 30m, 6h or 1d.")
    n, unit = int(m.group(1)), m.group(2)
    secs = n * {"m": 60, "h": 3600, "d": 86400}[unit]
    if secs < 600:
        die("Backups more often than every 10 minutes would just fill the disk.")
    return secs


def parse_at(text):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text.strip())
    if not m:
        die("Use a 24-hour time like 05:00.")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def describe(schedules):
    out = []
    if schedules.get("backup_every"):
        out.append(f"backup every {fmt_secs(schedules['backup_every'])}")
    if schedules.get("restart_at"):
        out.append(f"restart daily at {schedules['restart_at']}")
    return out


def fmt_secs(s):
    return f"{s // 86400}d" if s % 86400 == 0 else f"{s // 3600}h" if s % 3600 == 0 else f"{s // 60}m"


def set_schedule(s, kind, value):
    sched = dict(s.inst.schedules)
    if kind == "backup":
        if value in (None, "off"):
            sched.pop("backup_every", None)
        else:
            sched["backup_every"] = parse_every(value)
    elif kind == "restart":
        if value in (None, "off"):
            sched.pop("restart_at", None)
        else:
            sched["restart_at"] = parse_at(value)
    else:
        die("Schedule kind must be backup or restart.")
    s.update(schedules=sched)
    ok(f"'{s.name}' schedule: {', '.join(describe(sched)) or 'none'}")
    if sched and not agent_installed():
        warn("Schedules run from the background agent. Turn it on with: mc agent install")


# ---------- state ----------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    write_json_atomic(STATE_FILE, state)


def log(msg):
    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def notify(title, msg):
    log(f"{title}: {msg}")
    if setting("notifications", True):
        platform.notify(title, msg)


# ---------- one reconcile pass ----------
def tick(state, first=False, now=None):
    now = now or time.time()
    state["heartbeat"] = now
    current = all_servers()
    names = {s.name for s in current}
    for gone in [n for n in state.get("servers", {}) if n not in names]:
        state["servers"].pop(gone)                       # deleted or renamed: don't keep its history forever
    for key in [k for k in state.get("alerts", {}) if k.partition(":")[2] and k.partition(":")[2] not in names]:
        state["alerts"].pop(key)
    for s in current:
        st = state.setdefault("servers", {}).setdefault(s.name, {})
        if s.busy():                                     # a backup, restore, upgrade or a person's stop is running: next pass
            continue
        try:
            reconcile(s, st, first, now)
        except Fail as e:
            log(f"{s.name}: {e}")
        except Exception as e:  # never let one server kill the agent
            log(f"{s.name}: unexpected {type(e).__name__}: {e}")
    try:
        record_metrics(state, now)
    except Exception as e:
        log(f"metrics: unexpected {type(e).__name__}: {e}")
    try:
        healed = vpn.heal()
        if healed:
            log(f"vpn: {healed}")
    except Exception as e:
        log(f"vpn: unexpected {type(e).__name__}: {e}")
    try:
        for s in current:
            voice.maintain(s, log)                      # Simple Voice Chat: its address follows the internet address
    except Exception as e:
        log(f"voice: unexpected {type(e).__name__}: {e}")
    try:
        changed = share.bonjour_ensure()            # local mode: keep the invite name published, re-publish if the IP changed
        if changed:
            log(f"share: {changed}")
    except Exception as e:
        log(f"share: unexpected {type(e).__name__}: {e}")
    return state


def reconcile(s, st, first, now):
    inst = s.inst
    if s.docker and not dk.available():
        return  # Docker is down or restarting: we can't tell what state the server is in, so change nothing
    state = s.state()

    # 1. autostart at login
    if first and inst.autostart and state == "stopped":
        log(f"{s.name}: autostart")
        servers.start(s)
        return

    # 2a. machine rebooted / power lost while it was running (no exit code recorded) -> bring it back
    if first and inst.keep_alive and inst.desired == "running" and state == "stopped" and s.last_exit() is None:
        log(f"{s.name}: was running before this machine restarted; starting again")
        servers.start(s)
        return

    # 2b. stopped from inside the game (/stop) -> respect it
    if inst.desired == "running" and state == "stopped":
        s.update(desired="stopped")
        return

    # 3. crash handling
    if state == "crashed":
        crashes = [t for t in st.get("crashes", []) if now - t < CRASH_WINDOW]
        if st.get("crash_seen") != s.started_at():
            st["crash_seen"] = s.started_at()
            crashes.append(now)
            headline = servers.crash_summary(s)["headline"]
            events.add(s.name, "crash", headline)
            notify(f"Minecraft '{s.name}' crashed", headline)
        st["crashes"] = crashes
        if inst.keep_alive:
            if len(crashes) > MAX_CRASHES:
                if not st.get("gave_up"):
                    st["gave_up"] = True
                    notify(f"'{s.name}' keeps crashing", f"Not restarting again. See: mc -s {s.name} crash")
                return
            log(f"{s.name}: keep-alive restart (crash {len(crashes)})")
            servers.start(s)
        return
    if state == "running":
        st.pop("gave_up", None)

    # 4. schedules (only while running)
    if state != "running":
        return
    sched = inst.schedules
    every = sched.get("backup_every")
    if every and now - st.get("last_backup", 0) >= every:
        made = backups.make_backup(s)
        if backups.status(made) != "verified":
            alerts.deliver("alert", s.name, f"The scheduled backup of '{s.name}' failed verification ({made.name}).", log)
        backups.prune(s, inst.backup_keep, quiet=True)
        st["last_backup"] = now
        log(f"{s.name}: scheduled backup")
        if inst.backup_push_to:
            try:
                backups.push(s)
            except Fail as e:
                alerts.deliver("alert", s.name, f"Couldn't copy the backup of '{s.name}' to {inst.backup_push_to}: {e}", log)
    at = sched.get("restart_at")
    if at:
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        hh, mm = map(int, at.split(":"))
        due = datetime.fromtimestamp(now).replace(hour=hh, minute=mm, second=0)
        if st.get("last_restart_day") != today and due.timestamp() <= now < due.timestamp() + 15 * 60:
            st["last_restart_day"] = today
            log(f"{s.name}: scheduled restart (60 s countdown)")
            countdown.schedule(s, "restart", 60)      # its own process: warns the players without stalling the agent

    # 5. join notifications
    watch_joins(s, st)


def watch_joins(s, st):
    """Log joins/leaves as events (always) and send a notification for joins when `notify_joins` is on."""
    if not s.log_path.exists():
        return
    size = s.log_path.stat().st_size
    pos = st.get("log_pos", size)
    if size < pos:
        pos = 0
    if size > pos:
        with open(s.log_path, "rb") as f:
            f.seek(pos)
            for line in f.read().decode(errors="replace").splitlines():
                m = re.search(r": (\w+) (joined|left) the game", line)
                if not m:
                    continue
                who, what = m.groups()
                events.add(s.name, "join" if what == "joined" else "leave", f"{who} {what}")
                if what == "joined" and setting("notify_joins", False):
                    notify(f"{who} joined '{s.name}'", "Minecraft")
                alerts.send_webhook(f"{who} {what} '{s.name}'", joins=True)
    st["log_pos"] = size


_sampler = None


def record_metrics(state, now):
    """Append one resource sample (Mac + every server) for the dashboard's 1 h / 24 h graphs; prune once a day."""
    global _sampler
    from .resources import Sampler
    if _sampler is None:
        _sampler = Sampler()
        return                      # the first sample after a (re)start has no CPU baseline yet
    row = metrics.row(_sampler, all_servers(), now)
    metrics.append(row)
    if now - state.get("metrics_pruned", 0) > 86400:
        metrics.prune(now)
        state["metrics_pruned"] = now
    if alerts.enabled():
        for level, server, text in alerts.check(state, row, all_servers(), now) + alerts.check_vpn(state, now):
            alerts.deliver(level, server, text, log)


# ---------- loop ----------
def code_stamp():
    """Changes whenever mcctl's source does, so a long-running agent can notice it's out of date."""
    return max((p.stat().st_mtime for p in Path(__file__).parent.glob("*.py")), default=0)


def next_delay():
    """Seconds until the next pass: the normal tick while anything is running or wanted running (crash restarts,
    scheduled backups, alerts, the VPN), a slower one when the whole system is idle."""
    try:
        if setting("vpn_desired"):
            return TICK
        names = session_names()
        for s in all_servers():
            if s.up(names) or s.inst.desired == "running":
                return TICK
    except Exception:
        return TICK
    return IDLE_TICK


def run_forever():
    """Entry point for launchd. Holds a lock so only one agent runs."""
    lock = open(ROOT / ".mc-agent.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("mc agent is already running.", file=sys.stderr)
        sys.exit(0)
    log("agent started")
    first = not load_state().get("reloaded", False)   # a reload for new code isn't a login: skip autostart
    stamp = code_stamp()
    while True:
        state = tick(load_state(), first=first)
        state.pop("reloaded", None)
        first = False
        if code_stamp() != stamp:
            # An agent running old code can misread servers it doesn't know about (it once marked a Docker
            # server as stopped). Exit cleanly; launchd/systemd restart us on the new code.
            log("mcctl code changed; restarting the agent")
            state["reloaded"] = True
            save_state(state)
            sys.exit(0)
        save_state(state)
        time.sleep(next_delay())


# ---------- install as a login service ----------
def legacy_plists():
    """(label, plist path) of every earlier launchd name this agent may still be installed under."""
    return [(label, LAUNCH_DIR / f"{label}.plist") for label in LEGACY_LABELS]


def legacy_installed():
    """True while the agent is installed only under an earlier launchd name (mc agent install moves it)."""
    return platform.IS_MAC and not PLIST.exists() and any(plist.exists() for _, plist in legacy_plists())


def _launchd_label():
    """The label the agent is loaded under: the current one, or an earlier one until it is moved."""
    if PLIST.exists():
        return LABEL
    return next((label for label, plist in legacy_plists() if plist.exists()), LABEL)


def _remove_legacy():
    """Boot out and delete an agent installed under an earlier label. True if there was one."""
    found = False
    for label, plist in legacy_plists():
        loaded = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True).returncode == 0
        found = found or loaded or plist.exists()
        plist.unlink(missing_ok=True)
    return found


def agent_installed():
    if platform.IS_MAC:
        return PLIST.exists() or any(plist.exists() for _, plist in legacy_plists())
    return UNIT.exists()


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def agent_running():
    if platform.IS_MAC:
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{_launchd_label()}"], capture_output=True, text=True)
        return r.returncode == 0 and "state = running" in r.stdout
    return _systemctl("is-active", UNIT_NAME).stdout.strip() == "active"


def heartbeat_age():
    hb = load_state().get("heartbeat")
    return None if hb is None else time.time() - hb


def launchd_last_exit():
    """Exit code launchd last saw for the agent job, or None if unknown."""
    if not platform.IS_MAC:
        return None
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{_launchd_label()}"], capture_output=True, text=True)
    m = re.search(r"last exit code = (-?\d+)", r.stdout)
    return int(m.group(1)) if m else None


def python_bin():
    py = ROOT / ".venv" / "bin" / "python3.14"
    return str(py if py.exists() else ROOT / ".venv" / "bin" / "python")


def unit_text():
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return (
        "[Unit]\nDescription=mc server manager agent\nAfter=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={python_bin()} -m mcctl agent run\n"
        f"WorkingDirectory={ROOT}\n"
        f"Environment=PYTHONPATH={ROOT}\nEnvironment=PATH={path}\n"
        f"StandardOutput=append:{LOG_FILE}\nStandardError=append:{LOG_FILE}\n"
        "Restart=always\nRestartSec=30\n\n[Install]\nWantedBy=default.target\n"
    )


def install_systemd():
    if not shutil.which("systemctl"):
        die("systemctl not found; run `mc agent run` under your own supervisor.")
    LOG_FILE.parent.mkdir(exist_ok=True)
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    UNIT.write_text(unit_text())
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", UNIT_NAME)
    if r.returncode != 0:
        die(f"systemctl refused to start the agent: {r.stderr.strip()}")
    ok("Background agent installed as a systemd user service (checks every 30 seconds).")
    info("  To keep it running after you log out (VPS): sudo loginctl enable-linger $USER")
    info("  Turn things on per server: mc set autostart on | mc set keep_alive on | mc schedule backup 6h")


def install():
    if not platform.IS_MAC:
        return install_systemd()
    LOG_FILE.parent.mkdir(exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        # Invoke the venv's python directly (not the `mc` shell script): launchd-spawned
        # processes don't inherit Terminal's TCC grant for ~/Documents, so the `/bin/sh` +
        # `cd`/`realpath` dance in `mc` fails with "Operation not permitted" before Python even
        # starts. One binary here means one thing to grant Full Disk Access to (see `mc agent`).
        "ProgramArguments": [python_bin(), "-m", "mcctl", "agent", "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "WorkingDirectory": str(ROOT),
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": str(ROOT),
        },
    }
    if PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    for label, _ in legacy_plists():          # stop an agent installed under an earlier name before the new one starts
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
    write_atomic(PLIST, plistlib.dumps(plist))
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"launchctl refused to load the agent: {r.stderr.strip()}")
    if _remove_legacy():
        info("  Moved the agent from its earlier name to " + LABEL + ".")
    ok("Background agent installed: it starts at login and checks your servers every 30 seconds.")
    info("  Turn things on per server: mc set autostart on | mc set keep_alive on | mc schedule backup 6h")


def uninstall():
    if platform.IS_MAC:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        PLIST.unlink(missing_ok=True)
        _remove_legacy()
    else:
        _systemctl("disable", "--now", UNIT_NAME)
        UNIT.unlink(missing_ok=True)
        _systemctl("daemon-reload")
    ok("Background agent removed. Servers keep running; nothing restarts them automatically now.")
