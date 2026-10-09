"""World backups, restores, imports and exports. Nothing here deletes a world outright."""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path

from . import events
from .atomic import write_json_atomic
from .core import ROOT, Fail, die, info, need_gb, ok, require_disk, warn


def world_bytes(s):
    """Size of everything a backup of this server would contain."""
    return sum(p.stat().st_size for w in s.world_dirs() for p in w.rglob("*") if p.is_file())


def unpacked_bytes(src):
    """How much room a world .zip or folder needs once it is unpacked (0 when it is neither: _unpack says so later)."""
    src = Path(src)
    if src.suffix == ".zip" and src.is_file():
        try:
            with zipfile.ZipFile(src) as z:
                return sum(i.file_size for i in z.infolist())
        except zipfile.BadZipFile:
            return 0
    if src.is_dir():
        return sum(p.stat().st_size for p in src.rglob("*") if p.is_file())
    return 0


def make_backup(s, label="world"):
    with s.op_lock("backing up"):
        s.backups_dir.mkdir(parents=True, exist_ok=True)
        require_disk(s.backups_dir, need_gb(world_bytes(s)), f"back up '{s.name}'")
        running = s.accepting()
        if running:
            s.run("save-off")
            s.run("save-all flush", wait=3)
        name = s.backups_dir / f"{label}-{datetime.now():%Y%m%d-%H%M%S}.zip"
        try:
            with zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED) as z:
                for world in s.world_dirs():
                    for p in world.rglob("*"):
                        if p.is_file() and p.name != "session.lock":
                            z.write(p, p.relative_to(s.dir))
        finally:
            if running:
                s.run("save-on")
        result = verify(name)
    if result["ok"]:
        ok(f"'{s.name}' backed up and verified: {name.relative_to(ROOT)} ({name.stat().st_size / 1e6:.1f} MB)")
        events.add(s.name, "backup", f"{name.name} ({name.stat().st_size / 1e6:.1f} MB, verified)")
    else:
        warn(f"'{s.name}' backup {name.name} FAILED verification: {result['problem']}. Don't rely on it.")
        events.add(s.name, "alert", f"backup {name.name} failed verification: {result['problem']}")
    return name


def list_backups(s):
    if not s.backups_dir.exists():
        return []
    items = [p for p in s.backups_dir.iterdir()
             if p.suffix == ".zip" or (p.is_dir() and (p / "level.dat").exists())]
    return sorted(items, key=lambda p: p.stat().st_mtime, reverse=True)


def size_mb(p):
    total = p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return total / 1e6


# ---------- verification ----------
def sidecar(p):
    return p.with_name(p.name + ".json")


def verify(p, record=True):
    """Check a backup is a readable zip with a world in it. Records the result next to it (<name>.zip.json)."""
    p = Path(p)
    out = {"ok": False, "files": 0, "problem": None, "size": 0, "sha1": None, "verified_at": time.time()}
    try:
        if p.is_dir():
            out["ok"] = (p / "level.dat").exists()
            out["files"] = sum(1 for f in p.rglob("*") if f.is_file())
            out["problem"] = None if out["ok"] else "no level.dat"
        else:
            out["size"] = p.stat().st_size
            with zipfile.ZipFile(p) as z:
                bad = z.testzip()
                names = z.namelist()
            out["files"] = len(names)
            if bad:
                out["problem"] = f"corrupt entry {bad}"
            elif not any(n.endswith("level.dat") for n in names):
                out["problem"] = "no level.dat inside"
            else:
                out["ok"] = True
            h = hashlib.sha1()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            out["sha1"] = h.hexdigest()
    except (OSError, zipfile.BadZipFile) as e:
        out["problem"] = str(e) or type(e).__name__
    if record and p.is_file():
        try:
            write_json_atomic(sidecar(p), out, indent=None)
        except OSError:
            pass
    return out


def status(p):
    """'verified', 'corrupt' or 'unverified' (older backups, or the file changed since it was checked)."""
    p = Path(p)
    if p.is_dir():
        return "verified" if (p / "level.dat").exists() else "corrupt"
    try:
        data = json.loads(sidecar(p).read_text())
    except (OSError, ValueError):
        return "unverified"
    if data.get("size") != p.stat().st_size:
        return "unverified"
    return "verified" if data.get("ok") else "corrupt"


def verify_all(s):
    """Verify every backup of a server that hasn't been checked yet. Returns {name: status}."""
    out = {}
    for p in list_backups(s):
        if status(p) == "unverified":
            verify(p)
        out[p.name] = status(p)
    return out


# ---------- retention ----------
def prune(s, keep=None, quiet=False):
    """Delete old world-*.zip backups: beyond the newest `keep`, older than backup_keep_days, or past backup_max_gb.
    The newest backup and the newest verified one are never deleted."""
    inst = s.inst
    keep = inst.backup_keep if keep is None else keep
    zips = [p for p in list_backups(s) if p.suffix == ".zip" and p.name.startswith("world-")]   # newest first
    doomed = list(zips[keep:])
    rest = zips[:keep]
    if inst.backup_keep_days:
        cutoff = time.time() - inst.backup_keep_days * 86400
        doomed += [p for p in rest[1:] if p.stat().st_mtime < cutoff]
        rest = [p for p in rest if p not in doomed]
    if inst.backup_max_gb:
        total = sum(p.stat().st_size for p in rest)
        while total > inst.backup_max_gb * 2**30 and len(rest) > 1:
            gone = rest.pop()
            total -= gone.stat().st_size
            doomed.append(gone)
    newest_ok = next((p for p in zips if status(p) == "verified"), None)
    doomed = [p for p in doomed if p != newest_ok and p != (zips[0] if zips else None)]
    for p in doomed:
        p.unlink(missing_ok=True)
        sidecar(p).unlink(missing_ok=True)
    if not quiet:
        ok(f"'{s.name}': kept {len(zips) - len(doomed)} backups, removed {len(doomed)}.")
    return len(doomed)


# ---------- off-machine copy ----------
def push(s, remote_name=None):
    """Copy the newest verified backup to a saved remote (rsync over ssh; never deletes anything there)."""
    from . import remote
    name = remote_name or s.inst.backup_push_to
    if not name:
        die("Say where: mc backup push <remote>, or set one for every scheduled backup: mc set backup_push_to <remote>")
    if not shutil.which("rsync"):
        raise Fail("rsync is required to copy backups to another machine.")
    r = remote.get(name)
    zips = [p for p in list_backups(s) if p.suffix == ".zip"]
    chosen = next((p for p in zips if status(p) == "verified"), None) or (zips[0] if zips else None)
    if chosen is None:
        die(f"'{s.name}' has no backups yet. Make one: mc -s {s.name} backup")
    if status(chosen) == "unverified":
        verify(chosen)
    if status(chosen) != "verified":
        raise Fail(f"{chosen.name} didn't pass verification, so it was not copied.")
    folder = f"{r['path']}/backups/{s.name}/offsite"
    # Don't quote the whole path: the remote path usually starts with "~", which a quoted word stops the shell expanding
    # (it made a literal "~" folder and then rsync found nothing at the real one).
    if subprocess.call([*remote.ssh_base(r), f"mkdir -p {r['path']}/backups/{shlex.quote(s.name)}/offsite"]) != 0:
        raise Fail(f"Couldn't reach {name}. Check: mc -r {name} status")
    ssh = " ".join(["ssh", *remote.SSH_OPTS, "-p", str(r.get("port", 22))])
    if subprocess.call(["rsync", "-a", "-e", ssh, str(chosen), f"{remote.dest(r)}:{folder}/"]) != 0:
        raise Fail("rsync failed; nothing was removed on either side.")
    ok(f"Copied {chosen.name} to {name}:{folder}/")
    events.add(s.name, "backup", f"{chosen.name} copied to {name}")
    return chosen.name


# ---------- restore preview ----------
def describe(s, key):
    """What restoring this backup would do, for the confirmation dialog."""
    p = pick(s, key)
    now_mb = sum(f.stat().st_size for w in s.world_dirs() for f in w.rglob("*") if f.is_file()) / 1e6
    if p.is_file():
        try:
            with zipfile.ZipFile(p) as z:
                files = len(z.namelist())
        except (OSError, zipfile.BadZipFile):
            files = 0
    else:
        files = sum(1 for f in p.rglob("*") if f.is_file())
    size = size_mb(p)
    return {"name": p.name, "when": datetime.fromtimestamp(p.stat().st_mtime), "size_mb": size, "files": files,
            "world_now_mb": now_mb, "delta_mb": size - now_mb, "status": status(p), "running": s.up()}


def describe_text(d):
    lines = [f"{d['name']}  |  {d['when']:%d %b %Y %H:%M}  |  {d['size_mb']:.1f} MB  |  {d['files']} files",
             f"Check: {d['status']}" + ("  (run Verify first if you want to be sure)" if d["status"] == "unverified" else ""),
             f"Your current world is about {d['world_now_mb']:.0f} MB; this backup is "
             f"{abs(d['delta_mb']):.0f} MB {'larger' if d['delta_mb'] >= 0 else 'smaller'} (compressed)."]
    if d["status"] == "corrupt":
        lines.insert(1, "WARNING: this backup failed verification and may not restore.")
    lines.append("The server will stop, restore, then start again." if d["running"] else "The server is stopped.")
    lines.append("Your current world is moved into backups/ first, so nothing is lost.")
    return "\n".join(lines)


def move_world_aside(s, reason):
    """Move the current world (and Paper's Nether / End folders) into backups/; nothing is deleted."""
    worlds = s.world_dirs()
    if worlds:
        s.backups_dir.mkdir(parents=True, exist_ok=True)
        dest = s.backups_dir / f"world-before-{reason}-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.move(str(worlds[0]), dest)
        for extra in worlds[1:]:
            shutil.move(str(extra), dest / extra.name)
        ok(f"Previous world kept at backups/{s.name}/{dest.name}")


def install_world_from(s, src_root):
    found = sorted(Path(src_root).rglob("level.dat"), key=lambda p: len(p.parts))
    if not found:
        die("No level.dat found; that is not a Minecraft world.")
    world_dir = found[0].parent
    (world_dir / "session.lock").unlink(missing_ok=True)
    if s.loader_info.separate_dimensions:      # Paper: <world>_nether and <world>_the_end sit next to the world
        for suffix in ("_nether", "_the_end"):
            sibling = world_dir.parent / (world_dir.name + suffix)
            if sibling.is_dir():
                shutil.move(str(sibling), s.dir / (s.world_name + suffix))
    shutil.move(str(world_dir), s.world_dir)


def _unpack(src, tmp):
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    if src.suffix == ".zip":
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp)
    elif src.is_dir():
        shutil.copytree(src, tmp / src.name)
    else:
        die("Give a world .zip or folder.")
    if not any(tmp.rglob("level.dat")):
        shutil.rmtree(tmp, ignore_errors=True)
        die("No level.dat inside; that is not a Minecraft world.")


def pick(s, key):
    items = list_backups(s)
    key = str(key)
    found = (items[int(key) - 1] if key.isdigit() and 0 < int(key) <= len(items)
             else next((p for p in items if p.name == key), None))
    if not found:
        die(f"No backup '{key}'. See: mc backup list")
    return found


def restore(s, key):
    chosen = pick(s, key)
    s.require_stopped("restore a backup")
    with s.op_lock("restoring a backup"):
        require_disk(s.backups_dir, need_gb(unpacked_bytes(chosen)), "restore this backup")
        tmp = s.backups_dir / ".restore-tmp"
        _unpack(chosen, tmp)
        move_world_aside(s, "restore")
        install_world_from(s, tmp)
        shutil.rmtree(tmp, ignore_errors=True)
    ok(f"Restored {chosen.name} into '{s.name}'.")


def set_auto(s, on):
    s.update(autobackup=on)
    ok(f"'{s.name}' backs up on every stop: {'on' if on else 'off'} (keeps newest {s.inst.backup_keep}).")


def world_import(s, path):
    src = Path(os.path.expanduser(str(path))).resolve()
    if not src.exists():
        die(f"{src} not found.")
    s.require_stopped("import a world")
    with s.op_lock("importing a world"):
        s.backups_dir.mkdir(parents=True, exist_ok=True)
        require_disk(s.backups_dir, need_gb(unpacked_bytes(src)), "import this world")
        tmp = s.backups_dir / ".import-tmp"
        _unpack(src, tmp)
        move_world_aside(s, "import")
        install_world_from(s, tmp)
        shutil.rmtree(tmp, ignore_errors=True)
    ok(f"Imported {src.name} into '{s.name}' (older worlds upgrade on first start).")


def world_export(s, folder="~/Downloads"):
    dest = Path(os.path.expanduser(str(folder))).resolve()
    z = make_backup(s, "export")
    out = dest / f"{s.name}-world-{datetime.now():%Y%m%d-%H%M}.zip"
    shutil.move(str(z), out)
    info(f"World exported to {out}")
    return out
