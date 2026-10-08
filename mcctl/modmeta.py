"""What a mod or plugin jar says about itself: its id, version, what it needs, what it can't run with.

Pure (no network, no server): fabric.mod.json, quilt.mod.json, (neoforge.)mods.toml and plugin.yml / paper-plugin.yml.
`satisfies` judges version requirements with a small matcher and answers True, False, or None when the syntax is one it
doesn't understand: unknown never produces a complaint.
"""
import io
import json
import re
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_META = 2 * 1024 * 1024          # a metadata file bigger than this is not real metadata
MAX_NESTED = 64 * 1024 * 1024       # nor is a bundled jar worth opening past this size
MAX_JARS = 300                      # bundled jars read per mod (a mod can't make us open thousands)
MAX_DEPTH = 3

# which metadata file belongs to which loader family, and which one wins when a jar carries several
PREFER = ("quilt.mod.json", "fabric.mod.json", "META-INF/neoforge.mods.toml", "META-INF/mods.toml",
          "paper-plugin.yml", "plugin.yml")
FAMILY = {"quilt.mod.json": "Quilt", "fabric.mod.json": "Fabric", "META-INF/neoforge.mods.toml": "NeoForge",
          "META-INF/mods.toml": "Forge", "paper-plugin.yml": "Paper", "plugin.yml": "Paper/Bukkit"}


@dataclass(frozen=True)
class Need:
    id: str
    range: str = "*"                # raw requirement; " || " separates alternatives
    required: bool = True
    style: str = "semver"           # "semver" (Fabric, Quilt, Paper) or "maven" (Forge, NeoForge)


@dataclass
class ModMeta:
    file: str
    kind: str = ""                  # metadata file that was read; "" when the jar has none for this loader
    id: str = ""
    name: str = ""
    version: str = ""
    description: str = ""           # the mod's own text about itself (often one line)
    needs: list = field(default_factory=list)        # Need
    breaks: list = field(default_factory=list)       # Need: jars that can't run next to this one
    provides: list = field(default_factory=list)     # (id, version) this jar also counts as
    environment: str = "*"          # "*" | "client" | "server"
    nested: list = field(default_factory=list)       # ModMeta of the jars bundled inside this one
    foreign: tuple = ()             # metadata files of other loaders found instead (wrong loader)
    problem: str = ""               # why it couldn't be read

    @property
    def title(self):
        return self.name or self.id or self.file

    def ids(self):
        """{id: version} for everything this jar makes available: itself, what it provides, what it bundles."""
        out = {}
        if self.id:
            out[self.id] = self.version
        out.update(dict(self.provides))
        for n in self.nested:
            out.update(n.ids())
        return out


# ---------- reading a jar ----------
def inspect(path, markers, _depth=0):
    """Read one jar. `markers` are the metadata files the server's loader accepts (loaders.Loader.markers)."""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as z:
            return _read(z, ModMeta(file=path.name), markers, _depth)
    except (zipfile.BadZipFile, OSError) as e:
        return ModMeta(file=path.name, problem=f"not a readable jar ({type(e).__name__})")


def _inspect_bytes(data, name, markers, depth):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return _read(z, ModMeta(file=name), markers, depth)
    except (zipfile.BadZipFile, OSError):
        return None


def _read(z, meta, markers, depth):
    names = set(z.namelist())
    use = next((m for m in PREFER if m in markers and m in names), None)
    if use is None:
        meta.foreign = tuple(FAMILY[m] for m in PREFER if m in names)
        return meta
    meta.kind = use
    try:
        info = z.getinfo(use)
        if info.file_size > MAX_META:
            raise ValueError("metadata file is too large")
        text = z.read(use).decode("utf-8-sig", errors="replace")
        nested_paths = PARSERS[use](text, meta, z)
    except (ValueError, KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError) as e:
        meta.problem = f"{use} is unreadable ({e})"
        return meta
    if depth < MAX_DEPTH:
        paths = [p for p in nested_paths if p in names]
        if use in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
            paths += [n for n in names if n.startswith("META-INF/jarjar/") and n.endswith(".jar") and n not in paths]
        for p in paths[:MAX_JARS]:
            if z.getinfo(p).file_size <= MAX_NESTED:
                child = _inspect_bytes(z.read(p), Path(p).name, markers, depth + 1)
                if child is not None and child.kind:
                    meta.nested.append(child)
    return meta


# ---------- Fabric / Quilt ----------
def _text(value):
    """A version requirement as one string: a list means any of them."""
    if value is None:
        return "*"
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " || ".join(_text(v) for v in value)
    if isinstance(value, dict):                           # Quilt: {"any": [...]} or {"all": [...]}
        if "all" in value:
            return " ".join(_text(v) for v in value["all"])
        if "any" in value:
            return _text(value["any"])
    return "?"                                            # a shape we don't know: satisfies() answers None


def _fabric(text, meta, z):
    d = json.loads(text)
    meta.id, meta.version = str(d["id"]), str(d.get("version", ""))
    meta.name, meta.description = str(d.get("name") or ""), str(d.get("description") or "")
    meta.needs = [Need(k, _text(v)) for k, v in (d.get("depends") or {}).items()]
    meta.breaks = [Need(k, _text(v)) for k, v in (d.get("breaks") or {}).items()]
    meta.provides = [(p, meta.version) if isinstance(p, str) else (p["id"], str(p.get("version", meta.version)))
                     for p in d.get("provides") or []]
    meta.environment = d.get("environment", "*")
    return [j["file"] for j in d.get("jars") or [] if isinstance(j, dict) and "file" in j]


def _quilt_needs(items):
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append(Need(it.split(":")[-1]))
        else:
            out.append(Need(str(it["id"]).split(":")[-1], _text(it.get("versions")),
                            required=not it.get("optional") and "unless" not in it))
    return out


def _quilt(text, meta, z):
    d = json.loads(text)
    q = d["quilt_loader"]
    meta.id, meta.version = str(q["id"]).split(":")[-1], str(q.get("version", ""))
    meta.name = str((q.get("metadata") or {}).get("name") or "")
    meta.description = str((q.get("metadata") or {}).get("description") or "")
    meta.needs, meta.breaks = _quilt_needs(q.get("depends")), _quilt_needs(q.get("breaks"))
    meta.provides = [(p, meta.version) if isinstance(p, str) else (str(p["id"]).split(":")[-1],
                                                                  str(p.get("version", meta.version)))
                     for p in q.get("provides") or []]
    env = (d.get("minecraft") or {}).get("environment", "*")
    meta.environment = "server" if env == "dedicated_server" else env
    return [j if isinstance(j, str) else j["file"] for j in q.get("jars") or []]


# ---------- Forge / NeoForge ----------
def _manifest_version(z):
    try:
        for line in z.read("META-INF/MANIFEST.MF").decode(errors="replace").splitlines():
            if line.startswith("Implementation-Version:"):
                return line.split(":", 1)[1].strip()
    except KeyError:
        pass
    return ""


def _forge(text, meta, z):
    d = tomllib.loads(text)
    mods = d.get("mods") or []
    first = mods[0]
    meta.id, meta.name = str(first["modId"]), str(first.get("displayName") or "")
    meta.description = str(first.get("description") or "")
    version = str(first.get("version", ""))
    meta.version = _manifest_version(z) if version.startswith("${") else version
    meta.provides = [(str(m["modId"]), meta.version) for m in mods[1:]]
    for mod in mods:
        for dep in (d.get("dependencies") or {}).get(mod["modId"], []):
            if str(dep.get("side", "BOTH")).upper() == "CLIENT":
                continue                                  # only needed on the player's side
            need = Need(str(dep["modId"]), str(dep.get("versionRange", "*")), style="maven")
            kind = str(dep.get("type", "required" if dep.get("mandatory", True) else "optional")).lower()
            if kind == "incompatible":
                meta.breaks.append(need)
            elif kind == "required":
                meta.needs.append(need)
    return []


# ---------- Paper / Bukkit ----------
def _split_flow(text):
    return [p.strip() for p in text.split(",") if p.strip()]


def _scalar(t):
    t = t.strip()
    if t.startswith("[") and t.endswith("]"):
        return [_scalar(p) for p in _split_flow(t[1:-1])]
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    return {"true": True, "false": False}.get(t.lower(), t)


def _block(rows, i, indent):
    """Parse rows[i:] at this indent into a dict or a list. Returns (value, next row)."""
    if i < len(rows) and rows[i][1].startswith("- "):
        out = []
        while i < len(rows) and rows[i][0] == indent and rows[i][1].startswith("- "):
            out.append(_scalar(rows[i][1][2:]))
            i += 1
        return out, i
    out = {}
    while i < len(rows) and rows[i][0] == indent:
        key, sep, rest = rows[i][1].partition(":")
        i += 1
        if not sep:
            continue
        key, rest = key.strip().strip("'\""), rest.strip()
        if rest:
            out[key] = _scalar(rest)
        elif i < len(rows) and (rows[i][0] > indent or (rows[i][0] == indent and rows[i][1].startswith("- "))):
            out[key], i = _block(rows, i, rows[i][0])
        else:
            out[key] = None
    return out, i


def _uncomment(line):
    """Cut a trailing `# comment` (a # after whitespace, outside quotes)."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and i and line[i - 1] in " \t":
            return line[:i].rstrip()
    return line


def parse_yaml(text):
    """The small YAML subset plugin descriptors use: nested maps, block and [flow] lists, plain/quoted scalars."""
    rows = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or raw.startswith(("---", "...")):
            continue
        raw = _uncomment(raw)
        rows.append((len(raw) - len(raw.lstrip(" ")), raw.strip()))
    return _block(rows, 0, rows[0][0])[0] if rows else {}


def _names(value):
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _bukkit(text, meta, z):
    d = parse_yaml(text)
    if not isinstance(d, dict) or "name" not in d:
        raise ValueError("no name")
    meta.id, meta.name, meta.version = str(d["name"]).lower(), str(d["name"]), str(d.get("version", ""))
    meta.description = str(d.get("description") or "")
    meta.provides = [(n.lower(), meta.version) for n in _names(d.get("provides"))]
    meta.needs = [Need(n.lower()) for n in _names(d.get("depend"))]
    meta.needs += [Need(n.lower(), required=False) for n in _names(d.get("softdepend"))]
    deps = d.get("dependencies")
    if isinstance(deps, dict):                            # paper-plugin.yml: dependencies: {server: {Name: {...}}}
        for group in ("server", "bootstrap"):
            for name, opts in (deps.get(group) or {}).items():
                required = not (isinstance(opts, dict) and opts.get("required") is False)
                meta.needs.append(Need(str(name).lower(), required=required))
    api = str(d.get("api-version", "")).strip()
    if parse_version(api):
        meta.needs.append(Need("minecraft", f">={api}"))
    return []


PARSERS = {"fabric.mod.json": _fabric, "quilt.mod.json": _quilt, "META-INF/neoforge.mods.toml": _forge,
           "META-INF/mods.toml": _forge, "plugin.yml": _bukkit, "paper-plugin.yml": _bukkit}


# ---------- versions ----------
def parse_version(v):
    """'1.21.4' -> (1, 21, 4). Build/pre-release suffixes are dropped; None when it isn't a plain number
    (snapshots like '25w14a'), so the caller can say "can't tell"."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:[-+_ ].*)?", str(v).strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _cmp(a, b):
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def _semver_token(v, tok):
    m = re.fullmatch(r"(>=|<=|>|<|=|~|\^)?(.+)", tok)
    op, rest = m.group(1) or "", m.group(2)
    if re.search(r"[xX*]", rest) and not op:              # 1.21.x
        prefix = []
        for part in rest.split("."):
            if part in ("x", "X", "*"):
                break
            if not part.isdigit():
                return None
            prefix.append(int(part))
        padded = v + (0,) * (len(prefix) - len(v))
        return padded[:len(prefix)] == tuple(prefix)
    t = parse_version(rest)
    if t is None:
        return None
    if op in ("", "="):
        return _cmp(v, t) == 0
    if op == ">=":
        return _cmp(v, t) >= 0
    if op == ">":
        return _cmp(v, t) > 0
    if op == "<=":
        return _cmp(v, t) <= 0
    if op == "<":
        return _cmp(v, t) < 0
    if op == "~":                                         # ~1.2.3 -> >=1.2.3 <1.3.0 ; ~1.2 -> <1.3 ; ~1 -> <2
        top = t[:2] if len(t) >= 2 else t[:1]
        upper = (top[0], top[1] + 1) if len(top) == 2 else (top[0] + 1,)
        return _cmp(v, t) >= 0 and _cmp(v, upper) < 0
    lead = next((i for i, x in enumerate(t) if x), len(t) - 1)      # ^: nothing left of the first non-zero changes
    upper = t[:lead] + (t[lead] + 1,)
    return _cmp(v, t) >= 0 and _cmp(v, upper) < 0


def _bound(v, text, inclusive, lower):
    t = parse_version(text)
    if t is None:
        return None
    c = _cmp(v, t)
    return (c > 0 or (c == 0 and inclusive)) if lower else (c < 0 or (c == 0 and inclusive))


def _maven(v, spec):
    spec = spec.strip()
    if not spec or spec == "*" or spec[0] not in "[(":
        return True                                       # a bare version is only a hint in Forge range syntax
    results = []
    for part in re.split(r"(?<=[\])]),\s*(?=[\[(])", spec):
        m = re.fullmatch(r"([\[(])\s*([^,\[\]()]*?)\s*(?:,\s*([^,\[\]()]*?)\s*)?([\])])", part.strip())
        if not m:
            results.append(None)
            continue
        lo_in, lo, hi, hi_in = m.group(1) == "[", m.group(2), m.group(3), m.group(4) == "]"
        if hi is None:                                    # [1.2] means exactly 1.2
            t = parse_version(lo)
            results.append(None if t is None else _cmp(v, t) == 0)
            continue
        parts = [_bound(v, lo, lo_in, True) if lo else True, _bound(v, hi, hi_in, False) if hi else True]
        results.append(None if None in parts else all(parts))
    return _any(results)


def _any(results):
    if True in results:
        return True
    return None if None in results else False


def satisfies(version, rng, style="semver"):
    """Is `version` allowed by the requirement? True / False / None (can't tell)."""
    v = parse_version(version)
    rng = str(rng if rng is not None else "*").strip()
    if rng in ("", "*"):
        return True
    if v is None or "?" in rng:
        return None
    if style == "maven":
        return _maven(v, rng)
    results = []
    for alt in rng.split("||"):
        alt = re.sub(r"(>=|<=|>|<|=|~|\^)\s+", r"\1", alt.strip())
        toks = alt.split()
        parts = [_semver_token(v, t) for t in toks] if toks else [True]
        results.append(None if None in parts else all(parts))
    return _any(results)
