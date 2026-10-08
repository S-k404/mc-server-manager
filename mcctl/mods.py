"""Mods (or Paper plugins) from Modrinth, matched to each server's Minecraft version + loader, tracked in mods.lock.json."""
import json
import urllib.error
import urllib.parse
from dataclasses import asdict, dataclass

from .atomic import write_json_atomic
from .core import Fail, die, download, http, info, ok, sha1, warn

MODRINTH = "https://api.modrinth.com/v2"
LOCK = "mods.lock.json"


@dataclass
class ModEntry:
    project_id: str
    slug: str
    title: str
    version_id: str
    version_number: str
    filename: str
    sha1: str
    client_side: str = "unknown"
    server_side: str = "unknown"
    pinned: bool = False

    @property
    def players_need_it(self):
        return self.client_side == "required"


def require_modded(s):
    if not s.loader_info.content:
        die(f"'{s.name}' is a vanilla server and can't load mods or plugins. "
            "Create a Fabric or Paper one: mc create <name> --loader fabric")
    s.mods_dir.mkdir(exist_ok=True)


# ---------- lockfile ----------
def read_lock(s):
    p = s.dir / LOCK
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}, []
    return {k: ModEntry(**v) for k, v in data.get("mods", {}).items()}, data.get("manual", [])


def write_lock(s, entries, manual):
    p = s.dir / LOCK
    data = {"schema": 1, "mc_version": s.version, "loader": s.loader,
            "mods": {k: asdict(v) for k, v in sorted(entries.items(), key=lambda kv: kv[1].slug)},
            "manual": sorted(manual)}
    write_json_atomic(p, data)


def sync(s):
    """Rebuild the lockfile from the jars actually in mods/. Returns (entries, manual filenames)."""
    require_modded(s)
    old, _ = read_lock(s)
    jars = {sha1(p): p for p in sorted(s.mods_dir.glob("*.jar"))}
    known_by_sha = {e.sha1: e for e in old.values()}
    unknown = [h for h in jars if h not in known_by_sha]
    looked_up = {}
    if unknown:
        try:
            looked_up = http(f"{MODRINTH}/version_files", {"hashes": unknown, "algorithm": "sha1"})
        except (Fail, urllib.error.HTTPError):
            looked_up = {}
    projects = project_info({v["project_id"] for v in looked_up.values()})
    entries, manual = {}, []
    for h, path in jars.items():
        if h in known_by_sha:
            e = known_by_sha[h]
            e.filename = path.name
            entries[e.project_id] = e
        elif h in looked_up:
            v = looked_up[h]
            p = projects.get(v["project_id"], {})
            pinned = old.get(v["project_id"]).pinned if v["project_id"] in old else False
            entries[v["project_id"]] = ModEntry(
                project_id=v["project_id"], slug=p.get("slug", v["project_id"]), title=p.get("title", path.stem),
                version_id=v["id"], version_number=v["version_number"], filename=path.name, sha1=h,
                client_side=p.get("client_side", "unknown"), server_side=p.get("server_side", "unknown"),
                pinned=pinned)
        else:
            manual.append(path.name)
    write_lock(s, entries, manual)
    return entries, manual


def project_info(ids):
    if not ids:
        return {}
    q = urllib.parse.quote(json.dumps(sorted(ids)))
    return {p["id"]: p for p in http(f"{MODRINTH}/projects?ids={q}")}


# ---------- queries ----------
def search(s, query, limit=15):
    require_modded(s)
    info_ = s.loader_info
    facets = json.dumps([[f"versions:{s.version}"], [f"categories:{n}" for n in info_.modrinth],
                         ["server_side!=unsupported"], [f"project_type:{info_.project_type}"]])
    q = urllib.parse.urlencode({"query": query, "facets": facets, "limit": limit})
    return http(f"{MODRINTH}/search?{q}")["hits"]


def latest_version(project, mc_version, loader):
    """Newest build for this Minecraft version. `loader` is one Modrinth loader name or a list of them."""
    names = [loader] if isinstance(loader, str) else list(loader)
    params = urllib.parse.urlencode({"loaders": json.dumps(names), "game_versions": json.dumps([mc_version])})
    vs = http(f"{MODRINTH}/project/{urllib.parse.quote(project)}/version?{params}")
    releases = [v for v in vs if v["version_type"] == "release"]
    return (releases or vs or [None])[0]


def primary_file(v):
    return next((f for f in v["files"] if f["primary"]), v["files"][0])


def updates_available(s):
    """{project_id: new Modrinth version} for unpinned mods with a newer build."""
    entries, _ = read_lock(s)
    if not entries:
        entries, _ = sync(s)
    hashes = [e.sha1 for e in entries.values() if not e.pinned]
    if not hashes:
        return {}
    latest = http(f"{MODRINTH}/version_files/update",
                  {"hashes": hashes, "algorithm": "sha1", "loaders": list(s.loader_info.modrinth),
                   "game_versions": [s.version]})
    by_sha = {e.sha1: e for e in entries.values()}
    return {by_sha[h].project_id: v for h, v in latest.items() if primary_file(v)["hashes"]["sha1"] != h}


# ---------- changes ----------
def install_version(s, v, title):
    f = primary_file(v)
    download(f["url"], s.mods_dir / f["filename"], f["hashes"]["sha1"])
    ok(f"Installed {title} {v['version_number']}")
    return f["filename"]


def add(s, names, _seen=None, notes=False):
    """Install mods (+ their required dependencies). `notes=True` (the CLI and dashboard) then has the local model read the
    page of each mod you asked for, if `mc llm start` was run; dependencies and starter mods are not read."""
    require_modded(s)
    if not names:
        die("Which mod? Find one with: mc mods search <words>")
    top = _seen is None
    requested = []
    seen = _seen if _seen is not None else set(read_lock(s)[0]) | set(sync(s)[0])
    for name in names:
        try:
            p = http(f"{MODRINTH}/project/{urllib.parse.quote(name)}")
        except urllib.error.HTTPError:
            warn(f"No Modrinth project '{name}'. Try: mc mods search {name}")
            continue
        if p["id"] in seen:
            ok(f"{p['title']} already installed.")
            continue
        if p.get("server_side") == "unsupported":
            warn(f"{p['title']} is client-only; it does nothing on a server. Skipped.")
            continue
        v = latest_version(p["id"], s.version, s.loader_info.modrinth)
        if not v:
            warn(f"{p['title']} has no {s.label} build yet.")
            continue
        install_version(s, v, p["title"])
        seen.add(p["id"])
        requested.append(p)
        if p.get("client_side") == "required":
            warn(f"{p['title']} must also be installed by every player.")
        deps = [d["project_id"] for d in v["dependencies"]
                if d["dependency_type"] == "required" and d.get("project_id") and d["project_id"] not in seen]
        if deps:
            info(f"  installing {len(deps)} required dependenc{'y' if len(deps) == 1 else 'ies'}...")
            add(s, deps, seen)
    if top:
        sync(s)
        if notes and requested:
            from . import modnotes
            modnotes.after_install([modnotes.item_from_project(p) for p in requested])


def find_entry(s, name):
    entries, manual = sync(s)
    for e in entries.values():
        if name in (e.slug, e.filename, e.project_id) or name.lower() == e.title.lower():
            return e, None
    for f in manual:
        if name in (f, f.removesuffix(".jar")):
            return None, f
    die(f"'{name}' is not installed. See: mc mods")


def add_jar(s, src):
    """Copy a mod/plugin .jar you downloaded yourself (CurseForge, GitHub, ...) into mods/ or plugins/. Never overwrites."""
    import os
    import shutil
    import zipfile
    from pathlib import Path
    require_modded(s)
    src = Path(os.path.expanduser(str(src))).resolve()
    if not src.is_file() or src.suffix.lower() != ".jar":
        die(f"{src.name}: pick a mod .jar file.")
    try:
        with zipfile.ZipFile(src) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile:
        die(f"{src.name} isn't a valid jar.")
    info_ = s.loader_info
    if not names & set(info_.markers):
        die(f"{src.name} isn't a {info_.label} {'plugin' if info_.content == 'plugins' else 'mod'} "
            f"(no {' or '.join(info_.markers)} inside). On CurseForge pick the {info_.label} file for "
            f"Minecraft {s.inst.mc_version}.")
    dest = s.mods_dir / src.name
    if dest.exists():
        die(f"{src.name} is already in {info_.content}/ (mc never overwrites; remove it first).")
    s.mods_dir.mkdir(exist_ok=True)
    shutil.copy2(src, dest)
    sync(s)
    ok(f"Added {src.name} (kept as a manual jar: mc can't auto-update it).")
    from . import moddoctor
    about = [f for f in moddoctor.check(s) if dest.name in (f.mod, f.need)]
    if moddoctor.problems(about):
        warn(f"{src.name} needs more before the server will start:")
    elif about:
        info(f"{src.name}: things to know")
    else:
        warn("A mod built for another Minecraft version can stop the server from starting; check its page says "
             f"{s.inst.mc_version}.")
    moddoctor.say(s, about)
    from . import modmeta, modnotes
    meta = modmeta.inspect(dest, s.loader_info.markers)
    modnotes.after_install([{"title": meta.title, "text": modnotes.page_text(meta.description)}])
    return dest.name


def remove(s, names):
    require_modded(s)
    if not names:
        die("Which mod? See installed ones with: mc mods")
    from . import moddoctor
    before = {moddoctor.key(f) for f in moddoctor.check(s)}
    for name in names:
        e, manual_file = find_entry(s, name)
        filename = e.filename if e else manual_file
        (s.mods_dir / filename).unlink(missing_ok=True)
        ok(f"Removed {e.title if e else filename}")
    sync(s)
    broke = [f for f in moddoctor.check(s) if f.level == "error" and moddoctor.key(f) not in before]
    if broke:
        warn("That leaves other mods without something they need:")
        moddoctor.say(s, broke)


def update(s):
    require_modded(s)
    pending = updates_available(s)
    entries, _ = read_lock(s)
    for pid, v in pending.items():
        old = entries[pid]
        new_name = install_version(s, v, old.title)
        if new_name != old.filename:
            (s.mods_dir / old.filename).unlink(missing_ok=True)
    pinned = [e.title for e in entries.values() if e.pinned]
    sync(s)
    ok(f"'{s.name}': all mods up to date." if not pending else f"'{s.name}': updated {len(pending)} mod(s).")
    if pinned:
        info(f"  pinned (not updated): {', '.join(pinned)}")
    return len(pending)


def pin(s, name, pinned=True):
    e, manual_file = find_entry(s, name)
    if not e:
        die(f"{manual_file} isn't from Modrinth, so it's never auto-updated anyway.")
    entries, manual = read_lock(s)
    entries[e.project_id].pinned = pinned
    write_lock(s, entries, manual)
    ok(f"{e.title} {'pinned at ' + e.version_number if pinned else 'unpinned'}.")
