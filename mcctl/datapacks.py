"""Datapacks: add a .zip or folder to the world's `datapacks/` folder (used by `mc datapack` and the Files tab).

A datapack must have `pack.mcmeta` at its top level. Nothing is overwritten. A running server loads the pack
straight away with `/reload`; a stopped one picks it up on the next start.
"""
import json
import os
import shutil
import urllib.error
import urllib.parse
import zipfile
from pathlib import Path

from . import mods
from .atomic import write_json_atomic
from .core import die, download, ok, warn

LOCK = "datapacks.lock.json"


def packs_dir(s):
    return s.world_dir / "datapacks"


def _check(src):
    """Raise (die) unless src is a zip or folder with pack.mcmeta at its top level."""
    if src.is_dir():
        if not (src / "pack.mcmeta").is_file():
            die(f"{src.name} has no pack.mcmeta at its top level, so it isn't a datapack "
                "(open the folder that contains pack.mcmeta).")
    elif src.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(src) as z:
                names = z.namelist()
        except zipfile.BadZipFile:
            die(f"{src.name} isn't a valid zip file.")
        if "pack.mcmeta" not in names:
            inner = next((n for n in names if n.endswith("pack.mcmeta")), None)
            die(f"{src.name} has no pack.mcmeta at its top level"
                + (f" (it is inside '{inner.split('/')[0]}/': unzip it and zip the contents instead)." if inner else "."))
    else:
        die(f"{src.name}: pick a datapack .zip or a datapack folder.")


def list_packs(s):
    d = packs_dir(s)
    return sorted((p.name for p in d.iterdir() if not p.name.startswith(".")), key=str.lower) if d.is_dir() else []


def add(s, src):
    """Copy a datapack into the world. Returns the installed name."""
    src = Path(os.path.expanduser(str(src))).resolve()
    if not src.exists():
        die(f"{src} not found.")
    _check(src)
    dest_dir = packs_dir(s)
    if not dest_dir.parent.is_dir():
        die(f"'{s.name}' has no world yet. Start it once so the world exists, then add the datapack.")
    dest = dest_dir / src.name
    if dest.exists():
        die(f"{src.name} is already in '{s.name}' (mc never overwrites; remove it first).")
    dest_dir.mkdir(exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)
    ok(f"Added datapack {src.name} to '{s.name}'.")
    if s.accepting():
        s.run("reload")
        ok("Ran /reload: the datapack is active now.")
    else:
        warn("The server isn't running; the datapack loads on the next start.")
    return dest.name


def remove(s, name):
    """Move a datapack out of the world into backups/ (never deleted)."""
    target = packs_dir(s) / name
    if not target.exists() or target.parent != packs_dir(s):
        die(f"No datapack named {name} in '{s.name}'. See: mc datapack")
    lock = read_lock(s)
    if any(e["filename"] == name for e in lock.values()):
        write_lock(s, {k: e for k, e in lock.items() if e["filename"] != name})
    keep = s.backups_dir / "datapacks"
    keep.mkdir(parents=True, exist_ok=True)
    dest = keep / name
    n = 1
    while dest.exists():
        n += 1
        dest = keep / f"{target.stem}-{n}{target.suffix}"
    shutil.move(str(target), str(dest))
    ok(f"Moved {name} to backups/{s.name}/datapacks/.")
    if s.accepting():
        s.run("reload")
    return dest


# ---------- Modrinth datapacks ----------
def read_lock(s):
    try:
        return json.loads((s.dir / LOCK).read_text()).get("packs", {})
    except (OSError, ValueError):
        return {}


def write_lock(s, packs):
    write_json_atomic(s.dir / LOCK, {"schema": 1, "mc_version": s.version, "packs": packs})


def search(s, query, limit=15):
    """Datapacks on Modrinth that exist for this server's Minecraft version."""
    facets = json.dumps([[f"versions:{s.version}"], ["project_type:datapack"]])
    q = urllib.parse.urlencode({"query": query, "facets": facets, "limit": limit})
    return mods.http(f"{mods.MODRINTH}/search?{q}")["hits"]


def _reload(s):
    if s.accepting():
        s.run("reload")
        ok("Ran /reload: the datapack changes are active now.")
    else:
        warn("The server isn't running; datapack changes load on the next start.")


def install(s, names):
    """Install datapacks from Modrinth by slug or id (hash-verified, tracked in datapacks.lock.json)."""
    if not names:
        die("Which datapack? Find one with: mc datapack search <words>")
    dest_dir = packs_dir(s)
    if not dest_dir.parent.is_dir():
        die(f"'{s.name}' has no world yet. Start it once so the world exists, then add datapacks.")
    lock = read_lock(s)
    changed = False
    for name in names:
        try:
            p = mods.http(f"{mods.MODRINTH}/project/{urllib.parse.quote(name)}")
        except urllib.error.HTTPError:
            warn(f"No Modrinth project '{name}'. Try: mc datapack search {name}")
            continue
        if p.get("project_type") != "datapack":
            warn(f"{p['title']} is a {p.get('project_type')}, not a datapack." +
                 (" Add it with: mc mods add " + p["slug"] if p.get("project_type") == "mod" else ""))
            continue
        if p["id"] in lock:
            ok(f"{p['title']} is already installed.")
            continue
        v = mods.latest_version(p["id"], s.version, "datapack")
        if not v:
            warn(f"{p['title']} has no build for Minecraft {s.version} yet.")
            continue
        f = mods.primary_file(v)
        target = dest_dir / f["filename"]
        if target.exists():
            warn(f"{f['filename']} is already in the world; remove it first if you want the Modrinth copy.")
            continue
        dest_dir.mkdir(exist_ok=True)
        download(f["url"], target, f["hashes"]["sha1"])
        lock[p["id"]] = {"slug": p["slug"], "title": p["title"], "version_id": v["id"], "version_number": v["version_number"],
                         "filename": f["filename"], "sha1": f["hashes"]["sha1"]}
        changed = True
        ok(f"Installed datapack {p['title']} {v['version_number']}")
    if changed:
        write_lock(s, lock)
        _reload(s)


def update(s):
    """Update Modrinth-installed datapacks that have a newer build. Returns how many changed."""
    lock = read_lock(s)
    if not lock:
        return ok("No Modrinth datapacks to update.") or 0
    latest = mods.http(f"{mods.MODRINTH}/version_files/update",
                       {"hashes": [e["sha1"] for e in lock.values()], "algorithm": "sha1", "loaders": ["datapack"],
                        "game_versions": [s.version]})
    by_sha = {e["sha1"]: (pid, e) for pid, e in lock.items()}
    n = 0
    for old_sha, v in latest.items():
        f = mods.primary_file(v)
        if f["hashes"]["sha1"] == old_sha or old_sha not in by_sha:
            continue
        pid, e = by_sha[old_sha]
        target = packs_dir(s) / f["filename"]
        download(f["url"], target, f["hashes"]["sha1"])
        if e["filename"] != f["filename"]:
            remove(s, e["filename"])              # the old file goes to backups/, never deleted
            lock = read_lock(s)
        lock[pid] = {**e, "version_id": v["id"], "version_number": v["version_number"], "filename": f["filename"],
                     "sha1": f["hashes"]["sha1"]}
        write_lock(s, lock)
        ok(f"Updated {e['title']} to {v['version_number']}")
        n += 1
    if n:
        _reload(s)
    else:
        ok("All datapacks are up to date.")
    return n


def installed(s):
    """[{'name', 'title', 'version', 'source'}] for everything in the world's datapacks/ folder."""
    by_file = {e["filename"]: e for e in read_lock(s).values()}
    out = []
    for name in list_packs(s):
        e = by_file.get(name)
        out.append({"name": name, "title": e["title"] if e else name, "version": e["version_number"] if e else "",
                    "source": "Modrinth" if e else "manual"})
    return out
