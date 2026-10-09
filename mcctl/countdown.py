"""Restart/stop with an in-game countdown ("Server restarting in 5 minutes"), cancellable.

The agent ticks every 30 s, which is too coarse for 10-second warnings, so each countdown is its own small detached
process (`python -m mcctl _countdown <server> <id>`). Its plan lives in `servers/<name>/.mc/pending.json`; deleting that
file (mc cancel / the dashboard's Cancel button) stops the countdown. A countdown is lost if the Mac restarts mid-way.
"""
import json
import re
import secrets
import subprocess
import sys
import time

from . import events, servers
from .atomic import write_json_atomic
from .core import ROOT, die, ok

ACTIONS = {"restart": "restarting", "stop": "stopping"}
WARN_AT = (600, 300, 120, 60, 30, 15, 10, 5, 3, 2, 1)     # seconds left at which the players are told
MIN_SECONDS, MAX_SECONDS = 5, 24 * 3600


def parse_delay(text):
    """'90s', '5m', '2h' (or plain minutes like '5') -> seconds."""
    m = re.fullmatch(r"(\d+)\s*([smh]?)", str(text).strip().lower())
    if not m:
        die("Use a delay like 30s, 5m or 1h.")
    secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "": 60}[m.group(2)]
    if not MIN_SECONDS <= secs <= MAX_SECONDS:
        die(f"The delay must be between {MIN_SECONDS} seconds and 24 hours.")
    return secs


def _path(s):
    return s.state_dir / "pending.json"


def pending(s):
    """{'action', 'at', 'id'} while a countdown is running for this server, else None."""
    try:
        data = json.loads(_path(s).read_text())
    except (OSError, ValueError):
        return None
    return data if data.get("action") in ACTIONS and data.get("at", 0) > 0 else None


def label(seconds):
    seconds = int(round(seconds))
    if seconds >= 60:
        m, r = divmod(seconds, 60)
        return f"{m} minute{'s' if m != 1 else ''}" + (f" {r} seconds" if r else "")
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def schedule(s, action, seconds, spawn=True, now=None):
    """Start a countdown. Returns the plan. spawn=False only writes the plan (tests)."""
    if action not in ACTIONS:
        die("Countdown actions are restart and stop.")
    if isinstance(seconds, str):
        seconds = parse_delay(seconds)
    if pending(s):
        die(f"'{s.name}' already has a countdown ({pending(s)['action']}). Cancel it first: mc -s {s.name} cancel")
    plan = {"action": action, "at": (now or time.time()) + seconds, "id": secrets.token_hex(4)}
    s.state_dir.mkdir(exist_ok=True)
    write_json_atomic(_path(s), plan, indent=None)
    events.add(s.name, action, f"{action} scheduled in {label(seconds)}")
    if spawn:
        subprocess.Popen([sys.executable, "-m", "mcctl", "_countdown", s.name, plan["id"]], cwd=ROOT,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    ok(f"'{s.name}' will {action} in {label(seconds)}. Cancel: mc -s {s.name} cancel")
    return plan


def cancel(s, say=True):
    plan = pending(s)
    if not plan:
        die(f"'{s.name}' has no countdown running.")
    _path(s).unlink(missing_ok=True)
    events.add(s.name, "info", f"{plan['action']} cancelled")
    if say and s.accepting():
        s.run(f"say The {plan['action']} was cancelled.")
    ok(f"Cancelled the {plan['action']} of '{s.name}'.")


def announce(s, action, seconds):
    if s.accepting():
        s.run(f"say Server {ACTIONS[action]} in {label(seconds)}")


def run(s, plan_id, sleep=time.sleep, clock=time.time, do=None):
    """The countdown loop (runs in the detached process). Returns 'done', or 'cancelled' if the plan went away."""
    plan = pending(s)
    if not plan or plan["id"] != plan_id:
        return "cancelled"
    left = plan["at"] - clock()
    told = {t for t in WARN_AT if t >= left}          # thresholds already behind us (a 5 minute countdown skips "10 minutes")
    if left > 0:
        announce(s, plan["action"], left)
    while True:
        plan = pending(s)
        if not plan or plan["id"] != plan_id:
            return "cancelled"
        left = plan["at"] - clock()
        due = [t for t in WARN_AT if left <= t and t not in told]
        if due and left > 0:
            announce(s, plan["action"], min(due))
        told.update(due)
        if left <= 0:
            break
        sleep(min(1.0, left))
    _path(s).unlink(missing_ok=True)
    (do or {"restart": servers.restart, "stop": servers.stop}[plan["action"]])(s)
    return "done"
