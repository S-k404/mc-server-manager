"""Version upgrades and Modrinth modpacks (.mrpack)."""
import json
import shutil
import tempfile
import urllib.error
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import backups, mods, provision
from .core import die, download, http, info, need_gb, ok, require_disk, warn


# ---------- upgrades ----------
@dataclass
class UpgradePlan:
    server: object
    target: str
    java_major: int
    loader_version: str | None
    rows: list = field(default_factory=list)     # (ModEntry, new Modrinth version or None)
    manual: list = field(default_factory=list)
    downgrade: bool = False

    @property
    def missing(self):
        return [e for e, v in self.rows if v is None]


def version_order():
    """Release ids oldest -> newest (snapshots excluded)."""
    return [v["id"] for v in reversed(provision.manifest()["versions"]) if v["type"] == "release"]


def plan(s, target=None):
    target = target or provision.latest_release()
    inst = s.inst
    order = version_order()
    if target not in order:
        die(f"'{target}' is not a Minecraft release.")
    downgrade = inst.mc_version in order and order.index(target) < order.index(inst.mc_version)
    _, _, java_major = provision.version_info(target)
    if inst.loader != "vanilla" and inst.loader not in provision.compatible_loaders(target):
        die(f"{s.loader_info.label} has no build for Minecraft {target} yet. "
            f"Pick an older version, or wait for it: mc -s {s.name} upgrade --check")
    loader_version = provision.loader_version(inst.loader, target)
    rows, manual = [], []
    if s.loader_info.content:
        entries, manual = mods.sync(s)
        for e in sorted(entries.values(), key=lambda e: e.slug):
            rows.append((e, mods.latest_version(e.project_id, target, s.loader_info.modrinth)))
    return UpgradePlan(s, target, java_major, loader_version, rows, manual, downgrade)


def apply(p, drop_missing=False, allow_downgrade=False):
    s, inst = p.server, p.server.inst
    if p.downgrade and not allow_downgrade:
        die("That's an older version. Opening a world in an older version can corrupt it.")
    if p.missing and not drop_missing:
        die(f"No {p.target} build yet for: {', '.join(e.title for e in p.missing)}. "
            "Wait for updates, or upgrade without them (--drop-missing).")
    s.require_stopped("upgrade it")
    with s.op_lock("upgrading"):
        mods_bytes = sum(f.stat().st_size for f in s.mods_dir.rglob("*") if f.is_file()) if s.mods_dir.exists() else 0
        require_disk(s.backups_dir, need_gb(backups.world_bytes(s), mods_bytes), "upgrade (it backs up the world and the mods first)")
        old = inst.mc_version
        info(f"Upgrading '{s.name}' {old} -> {p.target}")
        made = backups.make_backup(s, label=f"pre-upgrade-{old}")
        if backups.status(made) != "verified":
            die("The pre-upgrade backup failed verification, so nothing was changed. Fix the world or free disk space, then retry.")
        if s.mods_dir.exists():
            keep = s.backups_dir / f"mods-{old}-{datetime.now():%Y%m%d-%H%M%S}"
            shutil.copytree(s.mods_dir, keep)
            ok(f"Old mods kept in backups/{s.name}/{keep.name}")
        with tempfile.TemporaryDirectory(dir=s.dir) as tmp:
            provision.install_server_files(Path(tmp), p.target, inst.loader, p.loader_version)
            for f in Path(tmp).iterdir():
                if f.is_dir():                       # installers (Quilt, Forge, NeoForge) add to libraries/, never replace it
                    shutil.copytree(f, s.dir / f.name, dirs_exist_ok=True)
                else:
                    f.replace(s.dir / f.name)
        provision.ensure_java(p.java_major)
        for e, v in p.rows:
            if v is None:
                (s.mods_dir / e.filename).unlink(missing_ok=True)
                warn(f"Removed {e.title} (no {p.target} build).")
                continue
            new_name = mods.install_version(s, v, e.title)
            if new_name != e.filename:
                (s.mods_dir / e.filename).unlink(missing_ok=True)
        for f in p.manual:
            warn(f"{f} is a manual jar: check it supports {p.target} yourself.")
        s.update(mc_version=p.target, loader_version=p.loader_version, java_major=p.java_major)
        if s.loader_info.content:
            mods.sync(s)
        ok(f"'{s.name}' is now on Minecraft {p.target}. Start it to finish converting the world.")


# ---------- modpack import ----------
def _safe_path(base, rel):
    """Resolve a pack-supplied path inside base, refusing anything that escapes it."""
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        die(f"Modpack contains an unsafe path: {rel}")
    return Path(base).joinpath(*pure.parts)


def fetch_mrpack(source, dest_dir):
    """source = local .mrpack path or a Modrinth modpack slug. Returns the local file."""
    p = Path(source).expanduser()
    if p.exists():
        return p
    try:
        versions = http(f"{mods.MODRINTH}/project/{urllib.parse.quote(source)}/version?"
                        + urllib.parse.urlencode({"loaders": json.dumps(["fabric"])}))
    except urllib.error.HTTPError:
        die(f"'{source}' is neither a file nor a Modrinth modpack.")
    if not versions:
        die(f"Modpack '{source}' has no Fabric versions.")
    f = mods.primary_file(versions[0])
    info(f"  downloading modpack {versions[0]['name']}...")
    return download(f["url"], Path(dest_dir) / f["filename"], f["hashes"]["sha1"])


def read_index(mrpack):
    with zipfile.ZipFile(mrpack) as z:
        try:
            return json.loads(z.read("modrinth.index.json"))
        except KeyError:
            die("Not a Modrinth modpack (no modrinth.index.json).")


def create_from_mrpack(name, source, port=None, ram=None, offline=False):
    from . import servers  # avoid import cycle
    with tempfile.TemporaryDirectory() as tmp:
        pack = fetch_mrpack(source, tmp)
        index = read_index(pack)
        deps = index.get("dependencies", {})
        if "fabric-loader" not in deps:
            die(f"Only Fabric modpacks are supported (this one needs: {', '.join(deps)}).")
        mc_version = deps["minecraft"]
        info(f"Modpack '{index.get('name')}' for Minecraft {mc_version} (Fabric {deps['fabric-loader']})")
        s = servers.create(name, mc_version, "fabric", port, ram or 6, offline, True, starter_mods=False,
                           loader_version=deps["fabric-loader"])
        try:
            server_files = [f for f in index.get("files", []) if f.get("env", {}).get("server") != "unsupported"]
            for f in server_files:
                dest = _safe_path(s.dir, f["path"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                download(f["downloads"][0], dest, f["hashes"]["sha1"])
            info(f"  installed {len(server_files)} server files from the pack")
            with zipfile.ZipFile(pack) as z:
                for prefix in ("overrides/", "server-overrides/"):
                    for member in z.namelist():
                        if member.startswith(prefix) and not member.endswith("/"):
                            dest = _safe_path(s.dir, member[len(prefix):])
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(z.read(member))
            mods.sync(s)
        except BaseException:
            shutil.rmtree(s.dir, ignore_errors=True)
            raise
    ok(f"'{name}' created from the modpack. Friends need the same client mods: share the pack itself.")
    return s


# ---------- modpack export (for friends' clients) ----------
def export_client_pack(s, folder="~/Downloads"):
    """Build a .mrpack of the mods players need (and optional client extras) for this server."""
    entries, _ = mods.sync(s)
    inst = s.inst
    files = []
    for e in sorted(entries.values(), key=lambda e: e.slug):
        if e.client_side == "unsupported":
            continue
        v = http(f"{mods.MODRINTH}/version/{e.version_id}")
        f = mods.primary_file(v)
        files.append({"path": f"mods/{f['filename']}", "hashes": {"sha1": f["hashes"]["sha1"],
                                                                 "sha512": f["hashes"]["sha512"]},
                      "env": {"client": "required" if e.client_side == "required" else "optional",
                              "server": "unsupported"},
                      "downloads": [f["url"]], "fileSize": f["size"]})
    index = {"formatVersion": 1, "game": "minecraft", "versionId": datetime.now().strftime("%Y.%m.%d"),
             "name": f"{s.name} (client mods)", "summary": f"Client mods for joining '{s.name}'",
             "files": files, "dependencies": {"minecraft": inst.mc_version,
                                              "fabric-loader": inst.loader_version or ""}}
    dest = Path(folder).expanduser() / f"{s.name}-client.mrpack"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("modrinth.index.json", json.dumps(index, indent=2))
    required = sum(1 for f in files if f["env"]["client"] == "required")
    ok(f"Client pack written to {dest} ({required} required, {len(files) - required} optional mods).")
    if not files:
        info("  No client mods needed: friends can join with plain Fabric or even vanilla.")
    return dest
