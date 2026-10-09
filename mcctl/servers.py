"""Server lifecycle: start/stop/kill, crash detection, and create/clone/delete."""
import os
import re
import shutil
import time
from datetime import datetime

from . import backups, config, events, loaders, mods, platform, provision, share, voice
from . import container as dk
from .core import (BACKUPS, NAME_RE, SERVERS, Server, all_servers, die, free_port, get_server, info, ok,
                   port_open, progress, save_setting, setting, tmux, used_ports, warn)
from .platform import total_ram_gb

# Runs the server, then records its exit code so we can tell a crash from a clean stop.
RUNNER = 'mkdir -p .mc; rm -f .mc/exit; "$@"; echo $? > .mc/exit'


# ---------- lifecycle ----------
def start(s, wait=True):
    with s.op_lock("starting"):               # held while launching, not while waiting for it to come up
        if s.up():
            return ok(f"'{s.name}' is already running.")
        others = used_ports(exclude=s.name)
        if s.port in others:
            die(f"Port {s.port} belongs to another server. Change it: mc -s {s.name} set server-port <n>")
        if port_open("127.0.0.1", s.port, 0.5):
            die(f"Port {s.port} is in use by another program.")
        s.ensure_rcon(others)
        inst = s.inst
        running = [x for x in all_servers() if x.up()]
        need = sum(x.inst.ram_gb for x in running) + inst.ram_gb
        if need > total_ram_gb() - 4:
            warn(f"Running servers could use up to {need} GB of {total_ram_gb()} GB RAM.")
        s.state_dir.mkdir(exist_ok=True)
        share.apply_bind(s)                                  # hidden / local mode: where the server listens (native servers)
        try:
            moved = voice.ensure_port(s)                     # Simple Voice Chat: a UDP port no other server (or program) holds
            if moved:
                info(f"  {moved}")
            voice.sync(s)                                    # ... and tell players where audio goes, before the mod reads it
        except Exception as e:                               # noqa: BLE001 - never let this keep a server from starting
            warn(f"Couldn't update the voice chat address: {e}")
        if s.docker:
            launch_container(s)
        else:
            java = provision.ensure_java(inst.java_major)
            (s.state_dir / "started").write_text(str(time.time()))
            (s.state_dir / "exit").unlink(missing_ok=True)
            tmux("new-session", "-d", "-s", s.session, "-c", str(s.dir), "-x", "220", "-y", "50",
                 "/bin/sh", "-c", RUNNER, "mc-run", *s.launch_argv(java))
        s.update(desired="running")
        events.add(s.name, "start", "started")
    if not wait:
        return ok(f"'{s.name}' is starting.")
    progress(f"Starting '{s.name}'")
    for _ in range(240):
        if not s.up():
            progress("\n")
            summary = crash_summary(s)
            die(f"'{s.name}' crashed while starting. {summary['headline']}  (details: mc -s {s.name} crash)")
        if s.listening(0.5):
            progress("\n")
            return ok(f"'{s.name}' is up on port {s.port}" + (" (Docker)." if s.docker else "."))
        progress(".")
        time.sleep(1)
    progress("\n")
    warn(f"Still starting after 4 minutes. Check: mc -s {s.name} console")


def stop(s, backup=None, wait=True):
    with s.op_lock("stopping"):
        s.update(desired="stopped")
        if not s.up():
            return ok(f"'{s.name}' is not running.")
        inst = s.inst
        if (inst.autobackup if backup is None else backup) and s.accepting():
            backups.make_backup(s)
            backups.prune(s, inst.backup_keep, quiet=True)
        try:
            s.run("stop", wait=0.5)
        except Exception:
            s.send("stop")
        if not wait:
            return ok(f"'{s.name}' is stopping.")
        progress(f"Stopping '{s.name}' (saving world)")
        for _ in range(120):
            if not s.up():
                progress("\n")
                events.add(s.name, "stop", "stopped")
                return ok(f"'{s.name}' stopped.")
            progress(".")
            time.sleep(1)
        progress("\n")
        die(f"'{s.name}' did not stop within 2 minutes. Force it with: mc -s {s.name} kill")


def restart(s):
    stop(s)
    start(s)


def kill(s):
    """Force-stop a hung server (unsaved progress since the last autosave is lost)."""
    s.update(desired="stopped")
    if not s.up():
        return ok(f"'{s.name}' is not running.")
    if s.docker:
        dk.remove(s.container)  # rm -f: stops it for good, so the restart policy can't bring it back
    else:
        tmux("kill-session", "-t", f"={s.session}")
    events.add(s.name, "kill", "force-stopped")
    ok(f"'{s.name}' force-stopped.")


def launch_container(s):
    """Run the server in a container: the same servers/<name>/ folder, bind-mounted at /data.

    Java comes from the eclipse-temurin image for the server's Java version, so nothing about the folder changes
    and `mc` can move a server between native and Docker with `mc runtime`.
    """
    dk.require()
    inst = s.inst
    need = inst.ram_gb + 1.5
    have = dk.vm_memory_gb()
    if have < need:
        die(f"Docker only has {have:.1f} GB of memory but '{s.name}' needs about {need:g} GB (heap + overhead). "
            "Raise it in Docker Desktop -> Settings -> Resources -> Memory, or lower: mc set ram_gb <n>")
    image = f"eclipse-temurin:{inst.java_major}-jre"
    dk.ensure_network()
    dk.pull(image, say=ok)
    dk.remove(s.container)  # a leftover exited container from the last run
    port, rcon = s.port, inst.rcon_port
    args = ["--name", s.container, "--network", dk.NETWORK, "--label", f"mc.server={s.name}",
            "-i", "-w", "/data", "-v", f"{s.dir}:/data",
            "-p", f"{share.publish_host()}{port}:{port}",               # hidden mode: the game port on this machine only
            "-p", f"127.0.0.1:{rcon}:{rcon}",                           # RCON only on this machine
            "--memory", f"{int((inst.ram_gb + 1.5) * 1024)}m", "--stop-timeout", "120",
            *dk.HARDENING, "--pids-limit", "4096",
            # keep_alive: Docker retries crashes (non-zero exits) itself; a clean /stop stays stopped
            "--restart", "on-failure:3" if inst.keep_alive else "no",
            # Docker publishes the port before the server is ready, so readiness comes from this check
            "--health-cmd", f"bash -c '</dev/tcp/127.0.0.1/{port}'", "--health-interval", "10s",
            "--health-timeout", "5s", "--health-retries", "3", "--health-start-period", "300s"]
    if platform.IS_LINUX:  # on a Mac, Docker Desktop maps file ownership itself
        args += ["--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"]
    dk.run(*args, image, "java", *s.java_flags())


# ---------- crashes ----------
SUSPECT_PATTERNS = [
    re.compile(r"([a-z0-9_\-]+)\.mixins?\.json"),
    re.compile(r"Mod ID: '([^']+)'"),
    re.compile(r"[Mm]od '([a-z0-9_\-]+)'"),
    re.compile(r"Suspected Mods?: ([^\n]+)"),
    re.compile(r"provided by '([a-z0-9_\-]+)'"),
]
NOT_SUSPECTS = {"minecraft", "java", "fabricloader", "fabric-loader", "mixinextras", "none", "unknown", "fabric"}


EXIT_MEANINGS = {
    137: "Killed (exit 137): usually the Mac ran out of memory, or it was force-stopped.",
    143: "Terminated (exit 143): something asked it to quit.",
    130: "Interrupted (exit 130).",
    1: "Java exited with an error (exit 1).",
}


def crash_summary(s):
    """Best-effort explanation of the last crash."""
    started = s.started_at() or 0
    reports = sorted((s.dir / "crash-reports").glob("*.txt"), key=lambda p: p.stat().st_mtime) \
        if (s.dir / "crash-reports").exists() else []
    report = reports[-1] if reports and reports[-1].stat().st_mtime >= started - 5 else None
    text = report.read_text(errors="replace") if report else ""
    log_tail = []
    if s.log_path.exists():
        lines = s.log_path.read_text(errors="replace").splitlines()
        log_tail = [ln for ln in lines[-400:] if re.search(r"ERROR|FATAL|Exception|Caused by", ln)][-15:]
    haystack = text + "\n" + "\n".join(log_tail)
    suspects = []
    for pat in SUSPECT_PATTERNS:
        for m in pat.findall(haystack):
            for name in re.split(r"[,\s]+", m):
                name = name.strip().strip("'\"()").lower()
                if name and name not in NOT_SUSPECTS and name not in suspects:
                    suspects.append(name)
    desc = re.search(r"Description: (.+)", text)
    code = s.last_exit()
    if desc:
        headline = desc.group(1).strip()
    elif log_tail:
        headline = log_tail[-1].split("]: ", 1)[-1]
    else:
        headline = EXIT_MEANINGS.get(code, f"Exited with code {code}; no error in the logs." if code else
                                     "No error found in logs.")
    if suspects:
        headline += f"  Suspected mod(s): {', '.join(suspects[:4])}"
    return {"exit": s.last_exit(), "report": report, "headline": headline, "suspects": suspects, "tail": log_tail}


# ---------- create / clone / delete ----------
def check_new_name(name):
    if not NAME_RE.match(name or ""):
        die("Server names: lowercase letters, numbers, - and _ (e.g. survival, creative-2).")
    if (SERVERS / name).exists():
        die(f"A server named '{name}' already exists.")


WORLD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")
# server.properties a person may set while creating; each goes through the same validation as `mc set`
CREATE_PROPS = ("motd", "difficulty", "gamemode", "max-players", "hardcore", "pvp", "view-distance",
                "simulation-distance", "level-seed", "level-type", "spawn-protection")


def check_world_name(name):
    if not WORLD_RE.match(name or "") or name.endswith((".", " ")):
        die("World names: letters, numbers, spaces, - _ and . (up to 40 characters, e.g. survival-world).")


def create(name, version=None, loader="fabric", port=None, ram=4, offline=False, whitelist=True,
           starter_mods=True, loader_version=None, props=None, world_name=None, gc="g1", runtime="native"):
    """Make a server. `props` are server.properties values to set up front (validated like `mc set`)."""
    check_new_name(name)
    if loader not in config.LOADERS:
        die(f"Loader must be one of: {', '.join(config.LOADERS)}.")
    if gc not in config.GCS:
        die(f"Garbage collector must be one of: {', '.join(config.GCS)}.")
    if world_name:
        check_world_name(world_name)
    props = {k: v for k, v in (props or {}).items() if v not in (None, "")}
    from . import settings_schema
    clean = {}
    for k, v in props.items():
        setting_ = settings_schema.BY_KEY.get(k)
        if k not in CREATE_PROPS or setting_ is None and k not in ("level-seed", "level-type"):
            die(f"'{k}' can't be set while creating. Set it afterwards with: mc set {k} <value>")
        clean[k] = setting_.normalize(v) if setting_ else str(v).strip()
        if "\n" in clean[k]:
            die(f"{k}: must be a single line.")
    if runtime == "docker":
        dk.require()
    version = version or provision.latest_release()
    if loader != "vanilla" and loader not in provision.compatible_loaders(version):
        die(f"{loaders.get(loader).label} has no build for Minecraft {version} yet. "
            f"Compatible: {', '.join(provision.compatible_loaders(version))}")
    port = port or free_port()
    if port in used_ports():
        die(f"Port {port} belongs to another server.")
    existing = all_servers()
    if setting("default") is None and len(existing) == 1:
        save_setting("default", existing[0].name)  # keep the old implicit default

    info(f"Creating '{name}': Minecraft {version} {loaders.get(loader).label}, port {port}")
    s = Server(name)
    s.dir.mkdir(parents=True)
    try:
        java_major, loader_version = provision.install_server_files(s.dir, version, loader, loader_version)
        provision.ensure_java(java_major)
        config.save(s.cfg_path, config.Instance(
            name=name, mc_version=version, loader=loader, loader_version=loader_version,
            java_major=java_major, port=port, ram_gb=ram, gc=gc, runtime=runtime,
            created=datetime.now().isoformat(timespec="seconds")))
        (s.dir / "eula.txt").write_text("eula=true\n")
        wl = "true" if whitelist else "false"
        base = {"server-port": port, "motd": f"\\u00a7a{name}", "white-list": wl, "enforce-whitelist": wl,
                "online-mode": "false" if offline else "true",
                "enforce-secure-profile": "false" if offline else "true"}
        base.update(clean)
        if world_name:
            base["level-name"] = world_name
        for k, v in base.items():
            s.set_prop(k, v)
        s.ensure_rcon(used_ports(exclude=name))
        info_ = loaders.get(loader)
        if info_.content:
            s.mods_dir.mkdir(exist_ok=True)
        if loader == "fabric":
            if starter_mods:
                mods.add(s, ["fabric-api", "lithium"] + (["easyauth", "skinrestorer"] if offline else []))
            elif offline:
                mods.add(s, ["easyauth"])  # offline accounts are never safe without logins
        elif offline:
            warn(f"{info_.label} has no login mod here, so offline accounts are unprotected: anyone can join as an "
                 "op's name. Keep the whitelist on, or use Fabric (mc cracked on adds logins).")
    except BaseException:
        shutil.rmtree(s.dir, ignore_errors=True)
        raise
    ok(f"Created '{name}'. Creating a server means you accept the Minecraft EULA (aka.ms/MinecraftEULA).")
    return s


COPY_SKIP = {"world", "logs", "crash-reports", "usercache.json", "session.lock", ".fabric", "libraries",
             "versions", ".mc", ".mc-lock", ".mc-config.lock"}


def clone(src_name, name, port=None, with_world=False):
    src = get_server(src_name)
    check_new_name(name)
    if with_world:
        src.require_stopped("copy its world")
    dst = Server(name)
    worlds = {d.name for d in src.world_dirs()} | {src.world_name}
    skip = (COPY_SKIP | worlds) - (worlds if with_world else set())
    if src.loader_info.installer:                  # Quilt / Forge / NeoForge: the installer made libraries/, nothing re-downloads it
        skip -= {"libraries", "versions"}
    shutil.copytree(src.dir, dst.dir, ignore=lambda d, names: [n for n in names if n in skip and d == str(src.dir)])
    port = port or free_port()
    dst.update(name=name, port=port, rcon_port=None, desired="stopped",
               created=datetime.now().isoformat(timespec="seconds"))
    dst.set_prop("server-port", port)
    dst.set_prop("motd", f"\\u00a7a{name}")
    dst.set_prop("rcon.password", "")
    dst.ensure_rcon(used_ports(exclude=name))
    ok(f"Cloned '{src.name}' -> '{name}' (port {port}, {'with' if with_world else 'fresh'} world).")
    return dst


def delete(name):
    s = get_server(name)
    s.require_stopped("delete it")
    with s.op_lock("deleting"):
        if s.docker:
            dk.remove(s.container)  # the exited container still points at the folder we're about to move
        BACKUPS.mkdir(exist_ok=True)
        dest = BACKUPS / f"deleted-{s.name}-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.move(str(s.dir), dest)
    if setting("default") == s.name:
        save_setting("default", None)
    ok(f"Moved '{s.name}' to backups/{dest.name} (delete that folder to free space).")


def rename(name, new):
    """Rename a server: its folder, its backups, and every pointer to it (default, VPN address, dashboard)."""
    s = get_server(name)
    check_new_name(new)
    s.require_stopped("rename it")
    kept = BACKUPS / new
    if kept.exists():
        die(f"backups/{new} already exists. Move or delete it first.")
    if s.docker:
        dk.remove(s.container)  # the exited container is named after the old server and mounts the old folder
    old_backups = s.backups_dir
    with s.op_lock("renaming"):                  # the lock file moves with the folder; our open handle stays valid
        shutil.move(str(s.dir), str(SERVERS / new))
        if old_backups.exists():
            shutil.move(str(old_backups), str(kept))
    Server(new).update(name=new)
    for key in ("default", "vpn_forward_server", "dash_server"):
        if setting(key) == name:
            save_setting(key, new)
    events.add(new, "info", f"renamed from '{name}'")
    ok(f"Renamed '{name}' to '{new}' (folder, backups and settings moved with it). "
       f"Use it with: mc -s {new} ...")
    return Server(new)


def rename_world(s, new):
    """Rename the world: the folder (and Paper's Nether / End folders) and `level-name`."""
    check_world_name(new)
    s.require_stopped("rename its world")
    old = s.world_name
    if new == old:
        die(f"The world is already called '{old}'.")
    pairs = [(d, s.dir / (new + d.name.removeprefix(old))) for d in s.world_dirs()]
    clash = [t for _, t in pairs if t.exists()] + ([s.dir / new] if (s.dir / new).exists() and not pairs else [])
    if clash:
        die(f"'{clash[0].name}' already exists in the server folder. Pick another name.")
    with s.op_lock("renaming the world"):
        for src, dst in pairs:
            shutil.move(str(src), str(dst))
        s.set_prop("level-name", new)
    events.add(s.name, "info", f"world renamed '{old}' to '{new}'")
    ok(f"World of '{s.name}' renamed '{old}' -> '{new}'." + ("" if pairs else " (No world folder yet; the server will make it on first start.)"))


def use(name):
    get_server(name)
    save_setting("default", name)
    ok(f"Default server is now '{name}'.")
