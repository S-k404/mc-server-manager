"""One-paste diagnostics for bug reports: versions, servers, health checks, recent events and log tails, secrets stripped."""
import platform as pyplatform
import re
import sys
from datetime import datetime
from pathlib import Path

from . import events, game, network
from .core import ROOT, all_servers

REDACTIONS = [
    (re.compile(r"https://(?:[\w-]+\.)?discord(?:app)?\.com/api/webhooks/\S+"), "<discord webhook>"),
    (re.compile(r"\b[A-Za-z0-9+/]{43}="), "<key>"),                                       # WireGuard private keys
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)\S+"), r"\1\2<hidden>"),
    (re.compile(r"(?i)(/register|/login)\s+\S+(\s+\S+)?"), r"\1 <hidden>"),
    (re.compile(r"(?i)\b(https?://[^\s:/@]+):[^\s@/]+@"), r"\1:<hidden>@"),
    (re.compile(r"\b(?!127\.)(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b"), "<ip>"),   # LAN and public addresses              # user:password@host (sign-in proxy)             # EasyAuth passwords typed in chat
]


def redact(text):
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text.replace(str(Path.home()), "~")


def bundle(log_lines=40):
    out = [f"mc diagnostics | {datetime.now():%Y-%m-%d %H:%M}",
           f"python {sys.version.split()[0]} | {pyplatform.platform()}", f"folder: {ROOT}", ""]
    out.append("== Servers ==")
    servers = all_servers()
    for s in servers:
        inst = s.inst
        out.append(f"{s.name}: Minecraft {inst.mc_version} {inst.loader} | {inst.runtime} | port {s.port} | "
                   f"{inst.ram_gb} GB | {s.state()}")
    out += ["", "== Health check =="]
    try:
        for c in network.doctor(servers):
            out.append(f"[{'ok' if c.good else '!!'}] {c.group}: {c.message}" + (f"  -> {c.fix}" if c.fix else ""))
    except Exception as e:                            # a diagnostics bundle must never fail because a check did
        out.append(f"(health check failed: {type(e).__name__}: {e})")
    out += ["", "== Recent events =="]
    for e in events.recent(25):
        out.append(f"{datetime.fromtimestamp(e['ts']):%d %b %H:%M:%S}  {e.get('server') or '-':<10} {e['kind']:<8} {e['text']}")
    for s in servers:
        out += ["", f"== Last log lines: {s.name} =="]
        if s.log_path.exists():
            out += game.tail_text(s.log_path, 64 * 1024).splitlines()[-log_lines:]
        else:
            out.append("(no log yet)")
    return redact("\n".join(out))
