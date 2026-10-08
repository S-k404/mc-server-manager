"""Append-only event log: what happened to which server, and when (starts, stops, backups, crashes, joins, alerts).

Shown on the dashboard's Overview tab and by `mc events`. One JSON object per line in `.mc-events.jsonl`; the file is
trimmed to the newest KEEP lines once it passes MAX_BYTES. Writing never raises: a full disk must not break a start/stop.
"""
import json
import os
import time

from . import core

MAX_BYTES = 1_000_000
KEEP = 1500
KINDS = ("start", "stop", "restart", "kill", "backup", "crash", "join", "leave", "alert", "vpn", "info")


def path():
    return core.ROOT / ".mc-events.jsonl"


def add(server, kind, text, when=None):
    line = json.dumps({"ts": when or time.time(), "server": server, "kind": kind, "text": str(text)}) + "\n"
    try:
        p = path()
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        with os.fdopen(fd, "a") as f:
            f.write(line)
        if p.stat().st_size > MAX_BYTES:
            _trim(p)
    except OSError:
        pass


def _trim(p):
    try:
        lines = p.read_text().splitlines()[-KEEP:]
        tmp = p.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, p)
    except OSError:
        pass


def recent(n=20, server=None, kinds=None):
    """The newest `n` events (oldest first), optionally for one server and/or some kinds."""
    try:
        raw = path().read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(raw):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if (server is None or e.get("server") in (server, None)) and (not kinds or e.get("kind") in kinds):
            out.append(e)
            if len(out) >= n:
                break
    return out[::-1]
