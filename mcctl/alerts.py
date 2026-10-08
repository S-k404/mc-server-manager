"""Alerts: tell the owner when something needs attention, once, and again when it's fixed.

The background agent calls `check()` every tick with the latest resource sample. Rules (all debounced):
  lag        a server is under 15 TPS for 60 s          ram     a server uses > 90 % of its allowed RAM for 2 min
  disk       the Mac has < 5 GB free                    vpn     the internet address was lost / changed (every 5 min)
Each alert is sent once, repeated at most every 30 minutes, and followed by a "resolved" note. Delivery: a Mac
notification, the event log, and an optional Discord webhook. The webhook URL is a secret: it lives only in the
settings file (mode 600) and is never printed or logged.
"""
import json
import shutil
import urllib.error
import urllib.parse
import urllib.request

from . import core, events, platform, vpn
from .core import ROOT, all_servers, die, save_setting, setting

COOLDOWN = 30 * 60        # an alert that stays bad isn't repeated more often than this
CLEAR_AFTER = 60          # seconds of "fine" before an alert counts as resolved
LAG_TPS, RAM_FRACTION, DISK_GB = 15.0, 0.9, 5.0
WEBHOOK_HOSTS = ("discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com")
ALLOW_HTTP = False        # tests only
VPN_CHECK_EVERY = 300


def enabled():
    return setting("alerts", True)


# ---------- debouncing ----------
def step(book, key, bad, for_s, now, text_bad, text_ok):
    """Advance one rule. Returns [('alert'|'resolved', text)] to deliver (usually empty)."""
    e = book.setdefault(key, {})
    out = []
    if bad:
        e.pop("ok_since", None)
        e.setdefault("bad_since", now)
        if not e.get("active") and now - e["bad_since"] >= for_s:
            e["active"] = True
            if e.get("last_sent") is None or now - e["last_sent"] >= COOLDOWN:
                e["last_sent"], e["announced"] = now, True
                out.append(("alert", text_bad))
    else:
        e.pop("bad_since", None)
        if e.get("active"):
            e.setdefault("ok_since", now)
            if now - e["ok_since"] >= CLEAR_AFTER:
                if e.pop("announced", False):
                    out.append(("resolved", text_ok))
                e["active"] = False
                e.pop("ok_since", None)
    return out


def check(state, row, servers, now, disk_free_gb=None):
    """Evaluate every rule for one resource sample. Returns [(level, server_name|None, text)]."""
    book = state.setdefault("alerts", {})
    out = []
    for s in servers:
        r = row.get("servers", {}).get(s.name)
        if not r:
            continue
        tps = min(20.0, 1000.0 / r["mspt"]) if r.get("mspt") else None
        out += [(lvl, s.name, txt) for lvl, txt in step(
            book, f"lag:{s.name}", tps is not None and tps < LAG_TPS, 60, now,
            f"'{s.name}' is lagging: {tps or 0:.0f} TPS ({r.get('mspt', 0):.0f} ms/tick)",
            f"'{s.name}' is running smoothly again")]
        limit = s.inst.ram_gb
        out += [(lvl, s.name, txt) for lvl, txt in step(
            book, f"ram:{s.name}", bool(r.get("ram")) and r["ram"] / limit > RAM_FRACTION, 120, now,
            f"'{s.name}' is using {r.get('ram', 0):.1f} of its {limit} GB of RAM. Raise it: mc -s {s.name} ram <gb>",
            f"'{s.name}' RAM use is back to normal")]
    free = disk_free_gb if disk_free_gb is not None else shutil.disk_usage(ROOT).free / 2**30
    out += [(lvl, None, txt) for lvl, txt in step(
        book, "disk", free < DISK_GB, 0, now, f"Only {free:.1f} GB of disk left on this Mac",
        "Disk space is back above the limit")]
    return out


def check_vpn(state, now, address=None):
    """Internet address lost or changed (the forwarded port changes whenever the VPN reconnects)."""
    if not vpn.enabled() or now - state.get("vpn_checked", 0) < VPN_CHECK_EVERY:
        return []
    state["vpn_checked"] = now
    addr = address if address is not None else vpn.public_address(live=True)
    last, lost = state.get("vpn_address"), state.get("vpn_lost", False)
    out = []
    if addr:
        if lost:
            out.append(("resolved", None, f"The internet address is back: {addr}"))
        elif last and addr != last:
            text = f"The internet address changed to {addr}. Send friends the new one (mc share)."
            from . import network, share
            target = next((x for x in all_servers() if x.name == vpn.forward_server()), None)
            if share.mode() and target:                  # the full invite, ready to forward (it shows only what the mode allows)
                text += "\n" + network.invite(target)
            out.append(("info", None, text))
        state["vpn_address"], state["vpn_lost"] = addr, False
    elif last and not lost:
        out.append(("alert", None, "The internet address was lost: friends can't join from outside. Check: mc vpn"))
        state["vpn_lost"] = True
    return out


# ---------- delivery ----------
def deliver(level, server, text, log=None):
    """Mac notification + event log + webhook. Never raises."""
    title = {"alert": "[!] mc alert", "resolved": "[ok] mc resolved", "info": "mc"}.get(level, "mc")
    if log:
        log(f"{title}: {text}")
    events.add(server, "alert" if level != "info" else "info", f"{level}: {text}")
    try:
        if setting("notifications", True):
            platform.notify(title, text)
    except Exception:
        pass
    send_webhook(f"{title}: {text}")


def webhook_ok(url):
    p = urllib.parse.urlparse(url or "")
    return bool(p.hostname) and (p.scheme == "https" or (ALLOW_HTTP and p.scheme == "http")) \
        and (p.hostname in WEBHOOK_HOSTS or ALLOW_HTTP)


def set_webhook(url):
    if url in (None, "", "off"):
        save_setting("webhook_url", None)
        return core.ok("Discord webhook removed.")
    if not webhook_ok(url):
        die("That isn't a Discord webhook URL (https://discord.com/api/webhooks/...).")
    save_setting("webhook_url", url)
    try:
        core.SETTINGS_FILE.chmod(0o600)               # the URL is a secret
    except OSError:
        pass
    core.ok("Discord webhook saved (it is never shown again). Try it: mc alerts test")


def send_webhook(text, joins=False):
    """POST to the saved webhook. Returns True on success; failures are swallowed (an alert must not crash the agent)."""
    url = setting("webhook_url")
    if not url or not webhook_ok(url) or (joins and not setting("webhook_joins", False)):
        return False
    body = json.dumps({"content": text[:1900], "username": "mc"}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": core.UA})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def status():
    return {"alerts": enabled(), "webhook": bool(setting("webhook_url")), "joins": bool(setting("webhook_joins", False)),
            "notifications": bool(setting("notifications", True))}
