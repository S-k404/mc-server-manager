"""Resource history on disk, written by the background agent so graphs cover the time the dashboard was closed.

One JSON line per agent tick (~30 s) in `.mc-metrics.jsonl`:
  {"ts", "sys": {"cpu", "ram", "net"}, "servers": {name: {"cpu", "ram", "mspt", "players"}}}
`series()` averages those rows into at most `points` buckets for the sparklines. Kept for RETENTION_DAYS.
"""
import json
import os
import time

from . import core

RETENTION_DAYS = 30
RANGES = {"5m": 300, "1h": 3600, "24h": 86400}


def path():
    return core.ROOT / ".mc-metrics.jsonl"


def row(sampler, servers, now=None):
    """One sample of the Mac and every server (uses the Sampler, so CPU % and rates are deltas since its last call)."""
    sysd = sampler.system()
    out = {"ts": now or time.time(), "sys": {"cpu": round(sysd.cpu_percent, 1), "ram": round(sysd.ram_percent, 1),
                                              "net": round(sysd.net_down_kbs, 1)}, "servers": {}}
    for s in servers:
        try:
            st = sampler.server(s)
        except Exception:      # a broken server must not stop the history of the others
            continue
        out["servers"][s.name] = {"cpu": round(st.cpu_percent or 0, 1), "ram": round(st.ram_gb or 0, 2),
                                  "mspt": round(st.mspt or 0, 2), "players": len(st.players)}
    return out


def append(r):
    try:
        fd = os.open(path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(r) + "\n")
    except OSError:
        pass


def load(seconds, now=None):
    now = now or time.time()
    rows = []
    try:
        with open(path()) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) >= now - seconds:
                    rows.append(r)
    except OSError:
        pass
    return rows


def series(kind, key, seconds, server=None, points=120, now=None):
    """Bucket-averaged values for one metric. kind='sys' (cpu|ram|net) or 'server' (cpu|ram|mspt|players)."""
    now = now or time.time()
    rows = load(seconds, now)
    buckets = [[] for _ in range(points)]
    for r in rows:
        src = r.get("sys", {}) if kind == "sys" else r.get("servers", {}).get(server)
        if not src or key not in src:
            continue
        i = min(points - 1, int((r["ts"] - (now - seconds)) / seconds * points))
        buckets[i].append(src[key])
    vals = [sum(b) / len(b) if b else None for b in buckets]
    first = next((i for i, v in enumerate(vals) if v is not None), None)
    if first is None:
        return []
    out, last = [], 0.0
    for v in vals[first:]:
        last = v if v is not None else last      # carry the last value across gaps (agent restarts)
        out.append(last)
    return out


def prune(now=None):
    """Drop rows older than RETENTION_DAYS (rewrites the file; the agent calls this about once a day)."""
    cutoff = (now or time.time()) - RETENTION_DAYS * 86400
    p = path()
    try:
        keep = [ln for ln in p.read_text().splitlines() if _ts(ln) >= cutoff]
        tmp = p.with_suffix(".tmp")
        tmp.write_text("".join(ln + "\n" for ln in keep))
        os.replace(tmp, p)
    except OSError:
        pass


def _ts(line):
    try:
        return json.loads(line).get("ts", 0)
    except ValueError:
        return 0


GLYPHS = "▁▂▃▄▅▆▇█"


def spark(values, width=60):
    """Unicode sparkline of `values`, resampled to at most `width` characters."""
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [sum(values[int(i * step):max(int(i * step) + 1, int((i + 1) * step))]) /
                  len(values[int(i * step):max(int(i * step) + 1, int((i + 1) * step))]) for i in range(width)]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    return "".join(GLYPHS[min(7, int((v - lo) / span * 8))] for v in values)
