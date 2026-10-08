"""Mod doctor: find a mod that needs something this server doesn't have, say what to do about it, fix what is safe to fix.

  check(s)     compares what every jar in mods/ says it needs (modmeta) with what is installed
  diagnose(s)  reads the last server log / crash report for the loader's own complaint ("Mod X requires Y")
  fix(s)       installs a missing library (Modrinth, verified to really be that mod), updates mods that are built for
               another Minecraft version, and moves useless jars aside into backups/ (never deletes)

Findings carry plain steps for doing it by hand; `action` is what `fix` can do on its own.
"""
import re
import shutil
import tempfile
import time
import urllib.error
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import events, modmeta, mods, vpn
from .core import Fail, download, info, ok, warn
from .game import tail_text

# ids that name the game or the loader, not a mod you could install
PLATFORM = {"minecraft", "java", "fabricloader", "fabric-loader", "quilt_loader", "quilt-loader", "forge", "neoforge",
            "javafml", "lowcodefml", "mclanguage", "paper", "bukkit", "spigot"}

# libraries the loader itself ships: Fabric Loader 0.15+ (and Quilt) carry MixinExtras, so a mod that requires it is not missing it
# unless the loader is older (a mod's jar that provides it is still checked normally). Not so on Forge, where it is a separate mod.
LOADER_LIBRARIES = {"fabric": {"mixinextras"}, "quilt": {"mixinextras"}}

# what a Modrinth reply that is down, blocked, or shaped differently than expected raises
ODD_REPLY = (Fail, urllib.error.URLError, KeyError, ValueError, TypeError, AttributeError)

# mods that install fine and still need a step from you (checked by mod id)
SETUP = {
    "bluemap": "BlueMap writes core.conf on its first start with accept-download: false. Set it to true and restart so it "
               "can fetch the Minecraft resources; the map is served on port 8100.",
    "dynmap": "Dynmap serves its map on port 8123 (see configuration.txt): open it if players should see the map.",
}


@dataclass(frozen=True)
class Finding:
    kind: str               # missing | version | game | java | conflict | duplicate | loader | corrupt | client | mixin | players | setup
    level: str              # error: the server won't start (or this mod won't work) | warn: pointless or risky | note: to know
    mod: str                # the jar it is about; "" when only the log names the problem
    message: str
    steps: tuple = ()       # what to do by hand
    action: tuple = ()      # what fix() does on its own: ("install", id) | ("aside", jar) | ("update",)
    need: str = ""          # the id that is missing / clashing
    source: str = "jars"    # "jars" (installed files) or "log" (the last server log)


def key(f):
    return f.kind, f.mod, f.need


def _req(need):
    return "" if need.range in ("", "*") else f" ({need.range})"


# ---------- reading the mods folder ----------
def scan(s, extra=(), exclude=()):
    """ModMeta for every jar in mods/ (or plugins/), plus any `extra` jars not installed yet."""
    d = s.mods_dir
    jars = [p for p in sorted(d.glob("*.jar")) if p.name not in exclude] if d.exists() else []
    return [modmeta.inspect(p, s.loader_info.markers) for p in [*jars, *map(Path, extra)]]


def _lock(s):
    entries, _ = mods.read_lock(s)
    return {e.filename: e for e in entries.values()}


def check(s, extra=(), exclude=()):
    """Everything wrong (or worth knowing) about the jars alone, most serious first."""
    if not s.loader_info.content:
        return []
    metas = scan(s, extra, exclude)
    provided = {}
    for m in metas:
        for i, v in m.ids().items():
            provided.setdefault(i, []).append((v, m.file))
    lock = _lock(s)
    out = _duplicates(s, metas)
    for m in metas:
        out += _one_jar(s, m, provided, lock)
    return sorted(_unique(out), key=lambda f: ("error", "warn", "note").index(f.level))


def _duplicates(s, metas):
    out, by_id = [], {}
    for m in metas:
        if m.kind and m.id and not m.problem:
            by_id.setdefault(m.id, []).append(m)
    for group in by_id.values():
        if len(group) < 2:
            continue
        keep = max(group, key=lambda m: (modmeta.parse_version(m.version) or (), m.file))
        for m in group:
            if m is not keep:
                out.append(Finding(
                    "duplicate", "error", m.file, f"{m.title} is installed twice: {m.file} and {keep.file}.",
                    (f"Keep the newer one and remove the other: mc -s {s.name} mods remove {m.file}",),
                    ("aside", m.file), keep.file))
    return out


def _one_jar(s, m, provided, lock):
    name = s.name
    if m.problem:
        what = "is not a valid jar" if not m.kind else "has broken mod information"
        return [Finding("corrupt", "error", m.file, f"{m.file} {what}: {m.problem}.",
                        (f"Download it again, then: mc -s {name} mods add-jar <file>",), ("aside", m.file))]
    if not m.kind:
        if m.foreign:
            level = "error" if s.loader in ("forge", "neoforge") else "warn"
            return [Finding("loader", level, m.file, f"{m.file} is built for {' / '.join(m.foreign)}, not {s.loader_info.label}; "
                            "this server can't use it.",
                            (f"Download the {s.loader_info.label} version of the mod, then: mc -s {name} mods add-jar <file>",),
                            ("aside", m.file))]
        return [Finding("loader", "warn", m.file, f"{m.file} has no {s.loader_info.label} mod information inside, so {s.loader_info.label} "
                        "ignores it (a library, or the wrong download).",
                        (f"If you don't need it: mc -s {name} mods remove {m.file}",))]
    out = []
    updatable = lock.get(m.file) is not None and not lock[m.file].pinned
    own = m.ids()
    source = "Hangar or SpigotMC" if s.loader == "paper" else "CurseForge"
    for need in m.needs:
        if not need.required or need.id in own:
            continue
        if need.id in ("minecraft", "java"):
            have = s.version if need.id == "minecraft" else str(s.inst.java_major)
            if modmeta.satisfies(have, need.range, need.style) is False:
                out.append(_platform(s, m, need, have, updatable))
            continue
        if need.id in PLATFORM:
            continue
        have = provided.get(need.id)
        if not have and need.id in LOADER_LIBRARIES.get(s.loader, ()):
            continue
        if not have:
            out.append(Finding(
                "missing", "error", m.file, f"{m.title} needs {need.id}{_req(need)}, which is not installed.",
                (f"Search for it: mc -s {name} mods search {need.id}, then: mc -s {name} mods add <slug>",
                 f"If only {source} has it, download the {s.loader_info.label} file for Minecraft {s.version} and: "
                 f"mc -s {name} mods add-jar <file>"),
                ("install", need.id), need.id))
        elif need.id in LOADER_LIBRARIES.get(s.loader, ()):
            continue                    # a jar's own older copy: the loader carries a newer one, and the newest is the one used
        elif all(modmeta.satisfies(v, need.range, need.style) is False for v, _ in have):
            v, jar = have[0]
            other = lock.get(jar) is not None and not lock[jar].pinned
            out.append(Finding(
                "version", "error", m.file,
                f"{m.title} needs {need.id}{_req(need)}, but version {v or '?'} is installed ({jar}).",
                (f"Update whichever is out of date: mc -s {name} mods update",
                 f"A pinned or manual jar needs a newer download: mc -s {name} mods add-jar <file>"),
                ("update",) if updatable or other else (), need.id))
    for b in m.breaks:
        for v, jar in provided.get(b.id, []):
            if jar != m.file and modmeta.satisfies(v, b.range, b.style) is True:
                a, z = sorted((m.file, jar))
                out.append(Finding(
                    "conflict", "error", a, f"{m.title} can't run together with {b.id} ({jar}).",
                    (f"Remove one of them: mc -s {name} mods remove {a}   or   mc -s {name} mods remove {z}",),
                    need=z))
    if m.environment == "client":
        out.append(Finding("client", "warn", m.file, f"{m.title} is a client-side mod: on a server it does nothing "
                           "and some of them crash it.", (f"Remove it: mc -s {name} mods remove {m.file}",),
                           ("aside", m.file)))
    entry = lock.get(m.file)
    if entry and entry.players_need_it:
        out.append(Finding(
            "players", "note", m.file, f"Players need {entry.title} too: every player must install it on their side.",
            (f"Give them the matching versions as a client pack: mc -s {name} pack export",)))
    if m.id in SETUP:
        out.append(Finding("setup", "note", m.file, SETUP[m.id]))
    if m.id == "voicechat":
        out += _voicechat(s, m)
    return out


# ---------- mods that need more than installing ----------
def _properties(path):
    """The key=value pairs of a .properties file, or None when it can't be read."""
    out = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip()
    except OSError:
        return None
    return out


def _udp_ports(s):
    """UDP ports the running (native) server process listens on, or None when that can't be told."""
    if s.docker:
        return None
    try:
        import psutil
        pid = s.java_pid()
        return {c.laddr.port for c in psutil.Process(pid).net_connections(kind="udp") if c.laddr} if pid else None
    except Exception:                                  # noqa: BLE001 - AccessDenied, NoSuchProcess, no psutil support: "can't tell"
        return None


def _voicechat(s, m):
    """Simple Voice Chat. Read from the mod's ServerConfig.java: audio always travels over UDP on its own `port` (24454 unless
    changed); port=-1 shares Minecraft's port, which the mod itself warns may crash the server; a blank bind_address follows
    server-ip. Players need the mod on their side too."""
    name = s.name
    cfg_dir = s.mods_dir / "voicechat" if s.loader_info.content == "plugins" else s.dir / "config" / "voicechat"
    props = _properties(cfg_dir / "voicechat-server.properties")
    if props is None:
        return [Finding("setup", "note", m.file, "Simple Voice Chat needs setup: start the server once so it writes "
                        "voicechat-server.properties, then make UDP port 24454 (its default) reachable by players. "
                        "Players need the mod installed too.")]
    raw_port = props.get("port", "24454")                  # a config without the line means the default
    port = int(raw_port) if raw_port.lstrip("-").isdigit() else 24454
    if port == -1:
        out = [Finding("setup", "note", m.file, "Simple Voice Chat shares Minecraft's own port (port=-1 in its config). "
                       "The mod warns this may crash the server, because the query protocol also uses UDP there.",
                       (f"Give it its own port in voicechat-server.properties (the default is 24454), then restart: "
                        f"mc -s {name} restart",))]
    else:
        out = [Finding("setup", "note", m.file, f"Simple Voice Chat: players connect to UDP port {port} (separate from "
                       "Minecraft's port) and need the mod installed on their side too.")]
    from . import voice                                    # imported here: voice asks this module to read jars
    through_vpn = voice.route(s) == "vpn"
    server_ip = s.prop("server-ip")
    if not props.get("bind_address") and server_ip and not (through_vpn and server_ip.startswith("127.")):
        loopback = server_ip.startswith("127.") or server_ip == "localhost"
        out.append(Finding("voicechat", "error" if loopback else "warn", m.file,
                           f"Voice chat only listens on {server_ip}: its bind_address is blank, so it follows server-ip "
                           "from server.properties" + (", and nobody else can reach it." if loopback else "."),
                           ("Set bind_address=* in voicechat-server.properties (the mod's own comment: '*' means every "
                            f"address), then restart: mc -s {name} restart",), need="bind_address"))
    if s.docker:
        out.append(Finding("voicechat", "warn", m.file, "This server runs in Docker, which here only publishes Minecraft's "
                           f"own port, so voice chat's UDP port {port} isn't reachable from outside.",
                           (f"Run it natively to use voice chat: mc -s {name} runtime native",), need="docker"))
    elif port > 0 and s.state() == "running":
        listening = _udp_ports(s)
        if listening is not None and port not in listening:
            out.append(Finding("voicechat", "warn", m.file, f"The server is running but nothing listens on UDP port {port}: "
                               "Simple Voice Chat's voice server did not start.",
                               (f"Look for 'voicechat' lines in the server log: mc -s {name} logs",
                                "The mod may need a different port, or the port may be taken by another program."),
                               need="listening"))
    if through_vpn:
        out.append(Finding("setup", "note", m.file, "Voice chat goes through the internet address: the VPN's forwarder passes "
                           "UDP on Proton's port to the mod, and voice_host in its config tells players that address (mc keeps it "
                           "current and restarts the server when nobody is on it). Check it: mc vpn voice",
                           need="proton"))                # a distinct key: findings with the same (kind, jar, need) merge
    elif vpn.enabled() and vpn.forward_server() == name:
        out.append(Finding("setup", "note", m.file, "Voice chat goes straight to this Mac, so it works for players on the same "
                           "Wi-Fi (with this Mac's own VPN off) but not through the internet address. It is routed through that "
                           "address by itself in hidden mode, or while this Mac's own VPN is on.",
                           need="proton"))
    return out


def _platform(s, m, need, have, updatable):
    name = s.name
    if need.id == "java":
        return Finding("java", "error", m.file, f"{m.title} needs Java {need.range}; this server runs Java {have}.",
                       (f"Install an older build of {m.title} that supports Java {have}, or remove it: "
                        f"mc -s {name} mods remove {m.file}",), need=need.id)
    steps = ((f"Get a build made for {have}: mc -s {name} mods update",) if updatable else
             (f"Download a build of {m.title} made for Minecraft {have}, then: mc -s {name} mods remove {m.file} "
              f"and mc -s {name} mods add-jar <file>",)) + (f"Or remove it: mc -s {name} mods remove {m.file}",)
    return Finding("game", "error", m.file, f"{m.title} is built for Minecraft {need.range}; this server runs {have}.",
                   steps, ("update",) if updatable else (), need.id)


# ---------- reading the last log ----------
FABRIC_SOLUTION = re.compile(r"A potential solution has been determined[^\n]*\n(.*?)(?:\n\s*\n|More details|\Z)", re.S)
FABRIC_MISSING = re.compile(r"[Mm]od '([^']+)' \(([^)]+)\) \S+ requires .*? of (?:mod )?(?:'[^']*' \()?"
                            r"([A-Za-z0-9_.\-]+)\)?, which is missing")
FORGE_DEP = re.compile(r"Mod ID: '([^']+)', Requested by: '([^']+)', Expected range: '([^']*)', "
                       r"Actual version: '([^']*)'")
BUKKIT_MISSING = re.compile(r"Unknown/missing dependency plugins: \[([^\]]+)\]\. Please download and install "
                            r"these plugins to run '([^']+)'")
BUKKIT_UNKNOWN = re.compile(r"Could not load '(?:plugins/)?([^']+)'[^\n]*\n(?:[^\n]*\n){0,4}?[^\n]*"
                            r"UnknownDependencyException: ([^\n]+)")
JAVA_CLASS = re.compile(r"UnsupportedClassVersionError: ([\w/.$]+) has been compiled by a more recent version of the Java "
                        r"Runtime \(class file version (\d+)\.\d+\), this version of the Java Runtime only recognizes "
                        r"class file versions up to (\d+)")
MIXIN = re.compile(r"^.*Mixin apply for mod (\S+) failed.*$", re.M)


def _log_text(s):
    if not s.log_path.exists():
        return ""
    text = tail_text(s.log_path)
    crashes = s.dir / "crash-reports"
    if crashes.exists():
        newest = sorted(crashes.glob("*.txt"), key=lambda p: p.stat().st_mtime)[-1:]
        for rep in newest:
            if rep.name in text:                       # the log names it: it belongs to this run, not an old one
                text += "\n" + rep.read_text(errors="replace")[:200_000]
    return text


def _owner(s, entry):
    """The jar in mods/ that contains `entry` (a class file path), or ''."""
    entry = entry.replace(".", "/") + ".class" if not entry.endswith(".class") else entry
    for p in sorted(s.mods_dir.glob("*.jar")) if s.mods_dir.exists() else []:
        try:
            with zipfile.ZipFile(p) as z:
                if entry in z.namelist():
                    return p.name
        except (zipfile.BadZipFile, OSError):
            continue
    return ""


def diagnose(s):
    """What the last server log (and crash report) complains about, as findings."""
    if not s.loader_info.content:
        return []
    text = _log_text(s)
    if not text:
        return []
    name = s.name
    by_id = {m.id: m.file for m in scan(s) if m.id}
    updatable = {f for f, e in _lock(s).items() if not e.pinned}
    jar_of = lambda ident: by_id.get(ident.lower()) or by_id.get(ident) or ident
    out = []

    def missing(requester, need_id, rng="*", have="[MISSING]"):
        jar = jar_of(requester)
        if have == "[MISSING]":
            out.append(Finding("missing", "error", jar, f"{requester} needs {need_id}, which is not installed.",
                               (f"Search for it: mc -s {name} mods search {need_id}",),
                               ("install", need_id), need_id, "log"))
        else:
            out.append(Finding("version", "error", jar, f"{requester} needs {need_id} {rng}, but version {have} is "
                               "installed.", (f"Update it: mc -s {name} mods update",),
                               ("update",), need_id, "log"))

    for m in FABRIC_MISSING.finditer(text):
        missing(m.group(2), m.group(3))
    for m in FORGE_DEP.finditer(text):
        missing(m.group(2), m.group(1), m.group(3), m.group(4))
    for m in BUKKIT_MISSING.finditer(text):
        for dep in re.split(r"[,\s]+", m.group(1).strip()):
            missing(m.group(2), dep.lower())
    for m in BUKKIT_UNKNOWN.finditer(text):
        for dep in re.split(r"[,\s]+", m.group(2).strip().strip("[]")):
            if dep:
                missing(m.group(1).removesuffix(".jar"), dep.lower())
    sol = FABRIC_SOLUTION.search(text)
    if sol:                                            # Fabric words its own fix; use it
        other = []
        for line in re.findall(r"^\s*-\s+(.+?)\s*$", sol.group(1), re.M):
            inst = re.match(r"Install (?:mod )?(?:'[^']*' \(([A-Za-z0-9_.\-]+)\)|'?([A-Za-z0-9_.\-]+)'?)[,.( ]", line + " ")
            need = inst and (inst.group(1) or inst.group(2))
            if inst and not any(f.need == need for f in out):
                out.append(Finding("missing", "error", "", f"Fabric says: {line}", (), ("install", need), need, "log"))
            elif not inst:
                other.append(f"Fabric suggests: {line}")
        if other:
            out.append(Finding("conflict", "error", "", "Fabric found mods that don't work together or with this "
                               "Minecraft version.", tuple(other), (), "fabric", "log"))
    for m in JAVA_CLASS.finditer(text):
        need, have = int(m.group(2)) - 44, int(m.group(3)) - 44
        jar = _owner(s, m.group(1))
        out.append(Finding("java", "error", jar, f"{jar or 'A mod'} needs Java {need} or newer; this server runs Java "
                           f"{have}.", ("Use an older build of that mod" + (f", or remove it: mc -s {name} mods remove "
                                                                           f"{jar}" if jar else ""),),
                           need=str(need), source="log"))
    for m in MIXIN.finditer(text):
        jar = jar_of(m.group(1))
        level = "error" if re.search(r"/(ERROR|FATAL)\]", m.group(0)) else "warn"
        out.append(Finding("mixin", level, jar, f"{m.group(1)} failed to hook into Minecraft: it doesn't match this "
                           "Minecraft version, or another mod changes the same code.",
                           (f"Update it: mc -s {name} mods update", f"If it still fails: mc -s {name} mods remove {jar}"),
                           ("update",) if jar in updatable else (), m.group(1), "log"))
    return out


def _unique(findings):
    seen, out = set(), []
    for f in findings:
        if key(f) not in seen:
            seen.add(key(f))
            out.append(f)
    return out


def collect(s):
    """Installed-jar findings plus what the last log complains about, without repeats."""
    return _unique([*check(s), *diagnose(s)])


def preview(s, path):
    """What would adding this jar need? Returns (its ModMeta, the findings that exist only because of it)."""
    path = Path(path).expanduser()
    mods.require_modded(s)
    meta = modmeta.inspect(path, s.loader_info.markers)
    before = {key(f) for f in check(s)}
    return meta, [f for f in check(s, extra=[path]) if key(f) not in before]


# ---------- fixing ----------
def _candidates(s, dep_id):
    out = []
    base = dep_id.replace("_", "-")
    for guess in (dep_id, base, re.sub(r"\d+$", "", base), re.sub(r"-(fabric|quilt|forge|neoforge)$", "", base)):
        if guess and guess not in out:
            out.append(guess)
    try:
        hits = mods.search(s, base.replace("-", " "), limit=5)
    except ODD_REPLY:
        hits = []                                      # no search help: the guesses above still get a try
    out += [h["slug"] for h in hits[:4] if h["slug"] not in out]
    return out[:8]


def install_needed(s, dep_id):
    """Find the mod whose id is `dep_id` on Modrinth, check the downloaded jar really is it, install it.
    Returns the file name, or None when nothing on Modrinth provides that id."""
    for slug in _candidates(s, dep_id):
        try:
            v = mods.latest_version(slug, s.version, s.loader_info.modrinth)
        except urllib.error.HTTPError:
            continue
        if not v:
            continue
        f = mods.primary_file(v)
        dest = s.mods_dir / f["filename"]
        if dest.exists():
            continue
        with tempfile.TemporaryDirectory(prefix="mc-dep-") as tmp:
            got = download(f["url"], Path(tmp) / f["filename"], f["hashes"]["sha1"])
            if dep_id not in modmeta.inspect(got, s.loader_info.markers).ids():
                continue                               # a different mod with a similar name: don't keep it
            s.mods_dir.mkdir(exist_ok=True)
            shutil.move(str(got), dest)
        ok(f"Installed {f['filename']} (provides {dep_id})")
        return f["filename"]
    return None


def move_aside(s, filename, why):
    """Take a jar out of mods/ without deleting it; returns where it went."""
    dest_dir = s.backups_dir / "mods-removed"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{Path(filename).name}"
    shutil.move(str(s.mods_dir / Path(filename).name), dest)
    events.add(s.name, "info", f"moved {filename} out of {s.mods_dir.name}/ ({why})")
    return dest


def _run(s, action, why, done):
    """Carry out one action. Returns True when it changed something."""
    try:
        if action[0] == "install":
            got = install_needed(s, action[1])
            if got is None:
                warn(f"Couldn't find {action[1]} on Modrinth; install it by hand (see the steps).")
                return False
            done.append(f"installed {got} (needed: {action[1]})")
        elif action[0] == "aside":
            dest = move_aside(s, action[1], why)
            info(f"  moved {action[1]} aside: {dest}")
            done.append(f"moved {action[1]} aside (kept in {dest.parent})")
        else:
            n = mods.update(s)
            if not n:
                return False
            done.append(f"updated {n} mod(s)")
        return True
    except ODD_REPLY as e:
        # a network error, or Modrinth answering with something we don't understand: leave this one to the steps
        warn(str(e) if isinstance(e, Fail) else f"Modrinth gave an unexpected answer ({type(e).__name__}); "
             "install it by hand (see the steps).")
        return False


MAX_ROUNDS = 6                  # installing a library can reveal that it needs another one


def fix(s):
    """Do what can be done safely on its own. Returns (what was done, findings still open). The server must be stopped."""
    mods.require_modded(s)
    s.require_stopped("fix mods")
    found = collect(s)
    from_log = [f for f in found if f.source == "log"]
    done, tried, worked = [], set(), set()
    for _ in range(MAX_ROUNDS):
        todo = {}
        for f in found:
            if f.action and f.action not in tried:
                todo.setdefault(f.action, f.message)
        if not todo:
            break
        progress = False
        for action, why in todo.items():
            tried.add(action)
            if _run(s, action, why, done):
                worked.add(action)
                progress = True
        if not progress:
            break
        found = check(s)                               # the files now on disk are the truth; the old log is not
    if worked:
        try:
            mods.sync(s)                                   # refresh the lockfile; the jars are already in place
        except ODD_REPLY:
            pass
    left = check(s)
    keys = {key(f) for f in left}
    left += [f for f in from_log if key(f) not in keys and f.action not in worked]
    return done, left


# ---------- showing ----------
TAG = {"error": "[x]", "warn": "[!]", "note": "[i]"}
LOGGED = "  (seen in the last server log)"
CAN_FIX = "  (mc can fix this)"


def lines(s, findings):
    """(level, text) rows for a terminal or a dialog: plain ASCII, no markup."""
    out = []
    for f in findings:
        text = f"{TAG[f.level]} {f.message}{LOGGED if f.source == 'log' else ''}{CAN_FIX if f.action else ''}"
        out.append((f.level, text))
        out += [(f.level, f"      -> {step}") for step in f.steps]
    if any(f.action for f in findings):
        out += [("fix", f"Let mc fix what it can: mc -s {s.name} mods doctor --fix   (stop the server first)"),
                ("fix", "It installs missing libraries, updates mods built for another Minecraft version, and moves "
                        "useless jars into backups/ (nothing is deleted).")]
    return out


def headline(findings):
    n = len([f for f in findings if f.level != "note"])
    return "No missing mods, clashes or wrong versions found." if not n else f"{n} problem(s) found:"


def problems(findings):
    return [f for f in findings if f.level == "error"]


def say(s, findings):
    """Report findings through the shared reporter (CLI and dashboard both show these). Only info() is used: it never
    reads brackets in a jar name as markup."""
    for _, text in lines(s, findings):
        info(text)
