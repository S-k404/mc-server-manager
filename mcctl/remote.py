"""Run `mc` on another machine (a Linux VPS) over SSH, and copy servers to it.

Remotes live in the tool-wide settings under "remotes": {name: {host, user, path, port}}, where
`path` is the remote copy of this folder (it must have `mc` installed, e.g. via ./install.sh).
"""
import shlex
import shutil
import subprocess

from . import core
from .core import Fail, die, ok

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def load():
    return core.setting("remotes", {}) or {}


def get(name):
    r = load().get(name)
    if r is None:
        known = ", ".join(load()) or "none yet"
        die(f"No remote called '{name}' (known: {known}). Add one: mc remote add {name} user@host")
    return r


def add(name, target, path="~/minecraft", port=22):
    user, _, host = target.rpartition("@")
    if not host:
        die("Use the form user@host (or just host).")
    remotes = load()
    remotes[name] = {"host": host, "user": user or None, "path": path, "port": port}
    core.save_setting("remotes", remotes)
    ok(f"Remote '{name}' saved. Try it: mc -r {name} status")


def remove(name):
    remotes = load()
    if remotes.pop(name, None) is None:
        die(f"No remote called '{name}'.")
    core.save_setting("remotes", remotes)
    ok(f"Remote '{name}' forgotten (nothing on the VPS was touched).")


def dest(r):
    return f"{r['user']}@{r['host']}" if r.get("user") else r["host"]


def ssh_base(r):
    return ["ssh", *SSH_OPTS, "-p", str(r.get("port", 22)), dest(r)]


def remote_command(r, argv, tty=False):
    """The ssh argv that runs `mc <argv>` in the remote's folder."""
    inner = f"cd {r['path']} && ./mc " + " ".join(shlex.quote(a) for a in argv)
    base = ssh_base(r)
    if tty:                       # dashboard / console / logs -f need a terminal
        base.insert(1, "-t")
    return base + [inner]


def run_mc(name, argv):
    """Run an mc command remotely, streaming output. Returns the exit code."""
    r = get(name)
    interactive = not argv or argv[0] in ("dash", "console", "res") or "-f" in argv or "-w" in argv
    return subprocess.call(remote_command(r, argv, tty=interactive))


def deploy(server, name, start=False):
    """Copy a stopped server's folder to the remote with rsync. Never deletes anything remotely."""
    if not shutil.which("rsync"):
        raise Fail("rsync is required for mc deploy.")
    if server.state() not in ("stopped", "crashed"):
        raise Fail(f"Stop '{server.name}' first (mc stop) so the world is copied consistently.")
    r = get(name)
    target = f"{dest(r)}:{r['path']}/servers/"
    cmd = ["rsync", "-az", "--exclude", "logs/", "--exclude", "crash-reports/",
           "-e", " ".join(["ssh", *SSH_OPTS, "-p", str(r.get("port", 22))]), str(server.dir), target]
    if subprocess.call(cmd) != 0:
        raise Fail("rsync failed; nothing was removed on either side.")
    ok(f"Copied '{server.name}' to {name}:{r['path']}/servers/{server.name}")
    if server.docker:  # the remote runs it with its own Java, not through this machine's Docker
        if run_mc(name, ["-s", server.name, "runtime", "native"]) != 0:
            raise Fail("Copied, but couldn't switch it to native on the remote: mc -r %s -s %s runtime native" % (name, server.name))
    if start:
        if run_mc(name, ["-s", server.name, "start"]) != 0:
            raise Fail("Copied, but starting it on the remote failed. Check: mc -r %s logs" % name)


CODE_FILES = ["mc", "install.sh", "pyproject.toml", "uv.lock", "mcctl"]


def setup(name):
    """Copy the mc tool itself to the remote (no servers, no worlds) and install its environment there."""
    if not shutil.which("rsync"):
        raise Fail("rsync is required for mc remote setup.")
    r = get(name)
    ssh = " ".join(["ssh", *SSH_OPTS, "-p", str(r.get("port", 22))])
    if subprocess.call([*ssh_base(r), f"mkdir -p {r['path']}"]) != 0:
        raise Fail(f"Could not reach {dest(r)} over SSH (key login required).")
    cmd = ["rsync", "-az", "--exclude", "__pycache__/", "-e", ssh, *[str(core.ROOT / f) for f in CODE_FILES],
           f"{dest(r)}:{r['path']}/"]
    if subprocess.call(cmd) != 0:
        raise Fail("rsync failed.")
    if subprocess.call([*ssh_base(r), f"cd {r['path']} && ./install.sh"]) != 0:
        raise Fail("install.sh failed on the remote (it needs tmux and curl/python3).")
    ok(f"mc is installed on {name}. Next: mc deploy {name} --start | mc -r {name} agent install")
