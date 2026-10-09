"""Thin wrapper around the docker CLI, shared by the VPN/tunnel stack (vpn.py) and the Docker server runtime."""
import functools
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

from .core import die

NETWORK = "minecraftvps"   # the private docker network ("fake VPS") everything joins
# Everything we run gets no Linux capabilities and can't gain any (gluetun is the one exception: it needs NET_ADMIN).
HARDENING = ("--cap-drop", "ALL", "--security-opt", "no-new-privileges")
DOCKER_PATHS = ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                "/Applications/Docker.app/Contents/Resources/bin/docker", "/usr/bin/docker")


@functools.cache
def _find_docker():
    return shutil.which("docker") or next((p for p in DOCKER_PATHS if os.path.exists(p)), None)


def docker_path():
    path = _find_docker()
    if path and not os.path.exists(path):   # Docker was uninstalled/moved since we looked
        _find_docker.cache_clear()
        path = _find_docker()
    return path


def docker(*args, env=None, input=None, timeout=None):
    """Run a docker command; returns CompletedProcess (never raises on a non-zero exit)."""
    path = docker_path()
    if not path:
        die("Docker isn't installed. Install Docker Desktop (or `apt install docker.io` on Linux).")
    full_env = {**os.environ, **env} if env else None
    return subprocess.run([path, *args], capture_output=True, text=True, env=full_env, input=input, timeout=timeout)


def quiet(*args, timeout=10):
    """Like docker() but for read-only status checks: returns None instead of raising when Docker is missing,
    stopped or hung, so `mc ls` still works on a machine without Docker."""
    path = docker_path()
    if not path:
        return None
    try:
        return subprocess.run([path, *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


_up_until = 0.0   # a successful `docker info` is trusted for a few seconds; failures are never cached


def available():
    """True when the docker CLI exists and its daemon answers."""
    global _up_until
    if time.monotonic() < _up_until:
        return True
    try:
        ok = docker_path() is not None and docker("info", "--format", "{{.ServerVersion}}", timeout=15).returncode == 0
    except subprocess.TimeoutExpired:
        return False
    if ok:
        _up_until = time.monotonic() + 5
    return ok


def require():
    if docker_path() is None:
        die("Docker isn't installed. Install Docker Desktop (or `apt install docker.io` on Linux).")
    if not available():
        die("Docker isn't running. Open Docker Desktop and wait for the whale icon to settle, then try again.")


def vm_memory_gb():
    """Memory the Docker engine (Docker Desktop's VM on a Mac) can hand out to containers."""
    out = docker("info", "--format", "{{.MemTotal}}").stdout.strip()
    return int(out) / 2**30 if out.isdigit() else 0.0


_cache = {}
CACHE_SECONDS = 1.0   # the dashboard polls state() constantly; one `docker inspect` per second is plenty


def inspect(name, fresh=False):
    hit = _cache.get(name)
    if hit and not fresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    r = quiet("inspect", name)
    try:
        data = json.loads(r.stdout)[0] if r is not None and r.returncode == 0 else None
    except (ValueError, IndexError):
        data = None
    _cache[name] = (time.monotonic(), data)
    return data


def forget(name):
    _cache.pop(name, None)


def state(name):
    """'missing', or the container state: running | restarting | exited | created | paused | dead."""
    info = inspect(name)
    return info["State"]["Status"] if info else "missing"


def health(name):
    """'healthy' | 'unhealthy' | 'starting' | 'none' (no healthcheck) | 'missing'."""
    info = inspect(name)
    if not info:
        return "missing"
    return info["State"].get("Health", {}).get("Status", "none")


def remove(name):
    quiet("rm", "-f", name)
    forget(name)


def exit_code(name):
    """Exit code of a stopped container, or None while it runs / doesn't exist."""
    info = inspect(name)
    if info and info["State"]["Status"] in ("exited", "dead"):
        return info["State"]["ExitCode"]
    return None


def started_at(name):
    """Unix time the container last started, or None."""
    info = inspect(name)
    raw = info["State"]["StartedAt"] if info else ""
    if not raw or raw.startswith("0001"):
        return None
    head, _, frac = raw.rstrip("Z").partition(".")   # docker gives nanoseconds; datetime wants microseconds
    stamp = f"{head}.{frac[:6]}" if frac else head
    return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp()


def send_stdin(name, text):
    """Type a line into the container's console (PID 1's stdin, which is the server console)."""
    r = docker("exec", "-i", name, "sh", "-c", "cat > /proc/1/fd/0", input=text + "\n", timeout=10)
    return r.returncode == 0


def usage(name):
    """(memory_bytes, cpu_microseconds, threads) read from the container's cgroup, or None."""
    r = docker("exec", name, "sh", "-c",
               "grep -E '^anon ' /sys/fs/cgroup/memory.stat; grep usage_usec /sys/fs/cgroup/cpu.stat; "
               "cat /sys/fs/cgroup/pids.current", timeout=10)
    if r.returncode != 0:
        return None
    try:
        lines = r.stdout.split("\n")
        return int(lines[0].split()[1]), int(lines[1].split()[1]), int(lines[2])
    except (IndexError, ValueError):
        return None


def ensure_network():
    if docker("network", "inspect", NETWORK).returncode != 0:
        r = docker("network", "create", NETWORK)
        if r.returncode != 0:
            die(f"Couldn't create docker network '{NETWORK}': {r.stderr.strip()}")


def pull(image, say=None):
    """Pull the image if it's not here yet (docker verifies layer digests itself)."""
    if docker("image", "inspect", image).returncode == 0:
        return
    if say:
        say(f"Pulling {image} (first time only)...")
    r = docker("pull", image)
    if r.returncode != 0:
        die(f"Couldn't pull {image}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'unknown error'}")


def refresh(image, say=None):
    """Pull again so an old cached image gets its newer build; a failed pull keeps the one we have."""
    r = docker("pull", "--quiet", image, timeout=300)
    if r.returncode != 0 and say:
        say(f"Couldn't check {image} for a newer build; using the one already here.")


def image_built(image):
    """When the image on this machine was built ('2025-06-01T10:00:00Z'), or ''."""
    r = quiet("image", "inspect", "--format", "{{.Created}}", image)
    return r.stdout.strip() if r is not None and r.returncode == 0 else ""


def run(*args):
    """`docker run -d ...`; dies with docker's message on failure."""
    r = docker("run", "-d", *args)
    if r.returncode != 0:
        die(f"docker run failed: {r.stderr.strip()}")
    return r.stdout.strip()


def host_gateway_ip():
    """The address containers use to reach services on this Mac/host."""
    r = docker("run", "--rm", "--add-host", "mchost:host-gateway", "--entrypoint", "sh", "alpine/socat:latest",
               "-c", "grep mchost /etc/hosts | cut -f1")
    ip = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else ""
    return ip or "192.168.65.254"
