"""`mc mods config`: look at every mod's config file and repair what is safely repairable.

What it checks, and why it stops there:
  * a strict config (`.json`, `.toml`) that cannot be read, or any config that is empty, makes the mod fall back to defaults or
    fail to start. The fix moves the file aside into `backups/<server>/config-removed/` (never deletes), and the mod writes a
    fresh default the next time the server starts. `.json5`, `.properties`, `.cfg` and `.yml` are not syntax-checked: their
    dialects are too loose to tell "odd" from "broken", and moving a working config away would be worse than leaving it.
  * Simple Voice Chat gets a sense check (its UDP port must not be another server's, `voice.ensure_port`).
Settings that depend on the server (`server-ip`, the share mode, the internet address) are handled where they live (`voice.sync`,
`share.apply_bind`), not copied here. A running server's files are only reported: fixing needs it stopped.
"""
import json
import re
import shutil
import time
import tomllib
from dataclasses import dataclass

from . import moddoctor, voice
from .core import BACKUPS, Fail

STRICT = {".json", ".toml"}                      # formats where "cannot be parsed" really means broken
LOOSE = {".json5", ".properties", ".cfg", ".conf", ".yml", ".yaml", ".ini"}
MAX_BYTES = 2_000_000                            # a config this large is data, not settings


@dataclass
class Item:
    file: str                                    # relative to the server's folder
    mod: str                                     # the jar it belongs to, or ""
    level: str                                   # error | note | ok
    message: str
    steps: tuple = ()
    action: tuple = ()                           # ("aside", relpath) or ("voiceport",)


def config_root(s):
    return s.mods_dir if s.loader_info.content == "plugins" else s.dir / "config"


def config_files(s):
    """Config files under the config folder (three levels deep), skipping hidden ones and mc's own leftovers."""
    root = config_root(s)
    if not root.exists():
        return []
    found = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if (not p.is_file() or len(rel.parts) > 3 or any(part.startswith(".") for part in rel.parts)
                or p.suffix.lower() not in STRICT | LOOSE or p.name.endswith((".bak", ".old", ".tmp"))):
            continue
        found.append(p)
    return found


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def owner(path, root, metas):
    """The jar a config file most likely belongs to: its name or its folder's name matches the mod's id, name or jar."""
    parts = path.relative_to(root).parts
    names = {_norm(parts[0].rsplit(".", 1)[0]), _norm(path.stem)}
    for m in metas:
        keys = {_norm(i) for i in m.ids()} | {_norm(m.title), _norm(m.file.rsplit(".", 1)[0])}
        if any(n and (n in keys or any(k.startswith(n) or n.startswith(k) for k in keys if len(k) > 3)) for n in names):
            return m.file
    return ""


_COMMENTS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)


def _lenient_json(text):
    """JSON with // and /* */ comments and trailing commas, which several mods' own parsers accept."""
    text = _COMMENTS.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))


def parse_error(path):
    """'' when the file reads fine (or its format is too loose to judge), else one plain sentence."""
    try:
        data = path.read_bytes()
    except OSError as e:
        return f"can't be read ({e.strerror or e})"
    if len(data) > MAX_BYTES:
        return ""
    text = data.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return "is empty"
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            try:
                json.loads(text)
            except ValueError:
                _lenient_json(text)
        elif suffix == ".toml":
            tomllib.loads(text)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        return f"isn't valid {suffix[1:].upper()}: {e}"
    return ""


def check(s):
    """[Item] for every config file of this server (problems first), plus the sense checks for the mods that have them."""
    root = config_root(s)
    metas = moddoctor.scan(s) if s.mods_dir.exists() else []
    items = []
    for p in config_files(s):
        rel = str(p.relative_to(s.dir))
        mod = owner(p, root, metas)
        problem = parse_error(p)
        if problem:
            items.append(Item(rel, mod, "error", f"{rel} {problem}.",
                              (f"The mod can't use it. mc can move it aside (nothing is deleted) so the mod writes a fresh default: "
                               f"mc -s {s.name} mods config --fix   (stop the server first)",), ("aside", rel)))
        else:
            items.append(Item(rel, mod, "ok", f"{rel} reads fine."))
    if voice.installed(s):
        conf = voice.props(s)
        if conf is None:
            items.append(Item("config/voicechat/voicechat-server.properties", "", "note",
                              "Simple Voice Chat hasn't written its config yet: start the server once."))
        else:
            port = voice.udp_port(s)
            raw = conf.get("port", str(voice.DEFAULT_PORT))
            rival = [o.name for o in voice.servers_using_voice() if o.name != s.name and o.up() and voice.udp_port(o) == port]
            if raw.lstrip("-").isdigit() and int(raw) != -1 and not (1024 <= int(raw) <= 65535):
                items.append(Item("voicechat-server.properties", "", "error", f"Voice chat's port {raw} is outside 1024-65535.",
                                  (f"mc can pick a free one: mc -s {s.name} mods config --fix",), ("voiceport",)))
            elif rival:
                items.append(Item("voicechat-server.properties", "", "error",
                                  f"Voice chat's UDP port {port} is also used by the running server '{rival[0]}': one of them never starts voice chat.",
                                  (f"mc can give this one its own port: mc -s {s.name} mods config --fix",), ("voiceport",)))
            elif not s.up() and not voice.udp_free(port, "" if conf.get("bind_address", "*") == "*" else conf["bind_address"]):
                items.append(Item("voicechat-server.properties", "", "error",
                                  f"Voice chat's UDP port {port} is held by another program on this Mac.",
                                  (f"mc can pick a free one: mc -s {s.name} mods config --fix",), ("voiceport",)))
            elif s.up() and voice.log_failure(s):
                items.append(Item("voicechat-server.properties", "", "error", "The server log says voice chat could not open its UDP port: "
                                  + voice.log_failure(s), (f"Stop the server, then: mc -s {s.name} mods config --fix",), ("voiceport",)))
    return sorted(items, key=lambda i: ("error", "note", "ok").index(i.level))


def problems(items):
    return [i for i in items if i.level == "error"]


def fix(s, items=None, say=print):
    """Repair what check() found. The server must be stopped for anything that touches files. Returns the sentences it did."""
    items = items if items is not None else check(s)
    todo = [i for i in problems(items) if i.action]
    if not todo:
        return []
    if s.up():
        raise Fail(f"'{s.name}' is running. Stop it first (mc -s {s.name} stop), then run this again.")
    done = []
    for item in todo:
        if item.action[0] == "aside":
            src = s.dir / item.action[1]
            if not src.exists():
                continue
            dest_dir = BACKUPS / s.name / "config-removed"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{item.action[1].replace('/', '__')}"
            shutil.move(str(src), str(dest))
            done.append(f"moved {item.action[1]} aside to {dest.relative_to(BACKUPS.parent)} ({item.message.split(' ', 1)[1].rstrip('.')}); "
                        f"the mod writes a fresh one at the next start")
        elif item.action[0] == "voiceport":
            note = voice.ensure_port(s)
            if note:
                done.append(note)
    for line in done:
        say(line)
    return done
