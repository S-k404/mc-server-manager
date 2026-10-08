"""Browsing and editing a server's files safely (used by the dashboard's Files tab and `mc files`).

Rules: edits stay inside servers/<name>/ (symlinks that point outside are refused), only small text files are
editable, JSON must still parse, instance.json must still validate, the previous version of every edited file is kept in
backups/<name>/file-edits/, and files the running server rewrites itself can't be saved while it runs.
"""
import codecs
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from . import config
from .core import die, ok

MAX_EDIT_BYTES = 1024 * 1024
# The server rewrites these on its own (commands, shutdown), so edits made while it runs would be lost or clash.
SERVER_OWNED = {"server.properties", "ops.json", "whitelist.json", "banned-players.json", "banned-ips.json",
                "usercache.json"}
HIDDEN_NAMES = {".mc", ".mc-lock", ".mc-config.lock"}     # mc's own state (exit codes, rcon lock, operation and settings locks)
LANGUAGES = {".json": "json", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".xml": "xml", ".md": "markdown",
             ".sh": "bash", ".css": "css", ".js": "javascript"}


def resolve(s, path):
    """The real path of `path` if it is inside the server folder; otherwise a refusal."""
    root = s.dir.resolve()
    target = Path(path)
    target = (target if target.is_absolute() else root / target).resolve()
    if target != root and root not in target.parents:
        die("That path is outside the server folder.")
    return target


def relative(s, path):
    return str(Path(path).resolve().relative_to(s.dir.resolve()))


def language(path):
    return LANGUAGES.get(Path(path).suffix.lower())


def is_text(path):
    """True for a small file that looks like UTF-8 text."""
    p = Path(path)
    try:
        if not p.is_file() or p.stat().st_size > MAX_EDIT_BYTES:
            return False
        head = p.read_bytes()[:8192]
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)   # tolerates a character cut at the end
        return b"\x00" not in head
    except (OSError, UnicodeDecodeError):
        return False


def read_text(s, path):
    p = resolve(s, path)
    if not is_text(p):
        die(f"{p.name} isn't a small text file, so it can't be edited here "
            f"(limit {MAX_EDIT_BYTES // 1024} KB).")
    return p.read_text(encoding="utf-8", errors="replace")


def validate(s, p, text):
    """Raise a user-facing error if saving `text` would break the file."""
    if p.name == "instance.json":
        try:
            data = json.loads(text)
        except ValueError as e:
            die(f"instance.json must be valid JSON: {e}")
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "instance.json"
            probe.write_text(json.dumps(data))
            try:
                config.load(probe)
            except config.ConfigError as e:
                die(f"instance.json isn't valid: {e}")
    elif p.suffix.lower() == ".json":
        try:
            json.loads(text)
        except ValueError as e:
            die(f"{p.name} must be valid JSON (nothing saved): {e}")


def save_text(s, path, text):
    """Validate, keep the previous version, and write atomically. Returns 'restart' or 'saved'."""
    p = resolve(s, path)
    if any(part in HIDDEN_NAMES for part in p.relative_to(s.dir.resolve()).parts):
        die("That's mc's own state; it isn't edited here.")
    if p.name in SERVER_OWNED and s.up():
        die(f"Stop '{s.name}' before saving {p.name}: the running server rewrites it, so your edit would be lost "
            "(settings and players have their own tabs that apply live).")
    if p.exists() and not is_text(p):
        die(f"{p.name} isn't a small text file.")
    validate(s, p, text)
    if p.exists():
        keep = s.backups_dir / "file-edits"
        keep.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")   # microseconds: two saves in a second keep both
        (keep / f"{stamp}-{relative(s, p).replace(os.sep, '__')}").write_bytes(p.read_bytes())
    tmp = p.with_name(p.name + ".mc-tmp")
    tmp.write_text(text, encoding="utf-8")
    if p.exists():
        os.chmod(tmp, p.stat().st_mode & 0o7777)
    os.replace(tmp, p)
    ok(f"Saved {relative(s, p)} (previous version kept in backups/{s.name}/file-edits/)")
    return "restart" if s.up() else "saved"


def list_versions(s, path):
    """Earlier saved versions of a file, newest first."""
    keep = s.backups_dir / "file-edits"
    suffix = relative(s, resolve(s, path)).replace(os.sep, "__")
    return sorted(keep.glob(f"*-{suffix}"), reverse=True) if keep.exists() else []


def import_file(s, src, into):
    """Copy a file from anywhere into a folder of the server (never overwrites; names are kept)."""
    src = Path(os.path.expanduser(str(src))).resolve()
    if not src.is_file():
        die(f"{src} isn't a file.")
    folder = resolve(s, into)
    if not folder.is_dir():
        die("Pick a folder inside the server to copy into.")
    dest = folder / src.name
    if dest.exists():
        die(f"{relative(s, dest)} already exists; rename or remove it first (mc never overwrites silently).")
    dest.write_bytes(src.read_bytes())
    ok(f"Copied {src.name} into {relative(s, folder) or '.'}/")
    return dest


def find_configs(s, slug):
    """Text config files under config/ that belong to a mod (matched on its slug, ignoring - and _), best first."""
    import re
    cfg = s.dir / "config"
    key = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    if not key or not cfg.is_dir():
        return []
    hits = [p for p in cfg.rglob("*")
            if p.is_file() and key in re.sub(r"[^a-z0-9]", "", str(p.relative_to(cfg)).lower()) and is_text(p)]
    return sorted(hits, key=lambda p: (len(p.relative_to(cfg).parts), str(p)))
