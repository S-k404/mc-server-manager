"""instance.json: the per-server config file, with validation and schema migrations.

Bump SCHEMA and add a step to MIGRATIONS whenever the format changes, so old servers keep working.
"""
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import loaders
from .atomic import write_json_atomic

SCHEMA = 3
LOADERS = loaders.KEYS
GCS = ("g1", "zgc")
RUNTIMES = ("native", "docker")


class ConfigError(Exception):
    pass


@dataclass
class Instance:
    name: str
    mc_version: str
    loader: str = "fabric"
    loader_version: str | None = None
    java_major: int = 21
    port: int = 25565
    rcon_port: int | None = None
    ram_gb: int = 4
    gc: str = "g1"                    # garbage collector: g1 (tuned, default) | zgc (near-zero pauses, more memory)
    runtime: str = "native"           # where it runs: native (tmux + local Java) | docker (container, same folder)
    desired: str = "stopped"          # what the user asked for: running | stopped
    autostart: bool = False           # start when the Mac logs in (agent)
    keep_alive: bool = False          # restart after a crash (agent)
    autobackup: bool = False          # back up on every stop
    backup_keep: int = 10
    backup_keep_days: int = 0         # also drop backups older than this many days (0 = off; the newest is always kept)
    backup_max_gb: int = 0            # also keep total backup size under this (0 = off)
    backup_push_to: str = ""          # name of a saved remote (mc remote) that gets a copy of every scheduled backup
    schedules: dict = field(default_factory=dict)   # e.g. {"backup_every_hours": 6, "restart_at": "05:00"}
    created: str = ""
    schema: int = SCHEMA
    extra: dict = field(default_factory=dict)       # unknown keys, preserved on save

    def validate(self):
        if self.loader not in LOADERS:
            raise ConfigError(f"loader must be one of {LOADERS}, not {self.loader!r}")
        if self.gc not in GCS:
            raise ConfigError(f"gc must be one of {GCS}, not {self.gc!r}")
        if self.runtime not in RUNTIMES:
            raise ConfigError(f"runtime must be one of {RUNTIMES}, not {self.runtime!r}")
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"port {self.port} out of range")
        if self.rcon_port is not None and not 1 <= self.rcon_port <= 65535:
            raise ConfigError(f"rcon_port {self.rcon_port} out of range")
        if self.ram_gb < 1:
            raise ConfigError("ram_gb must be at least 1")
        if self.desired not in ("running", "stopped"):
            raise ConfigError(f"desired must be running or stopped, not {self.desired!r}")
        if self.backup_keep < 1:
            raise ConfigError("backup_keep must be at least 1")
        if self.backup_keep_days < 0 or self.backup_max_gb < 0:
            raise ConfigError("backup_keep_days and backup_max_gb can't be negative")
        return self


def _v1_to_v2(d):
    d.setdefault("desired", "stopped")
    d["schema"] = 2
    return d


def _v2_to_v3(d):
    d.setdefault("runtime", "native")
    d["schema"] = 3
    return d


MIGRATIONS = {1: _v1_to_v2, 2: _v2_to_v3}
KNOWN = {f.name for f in fields(Instance)} - {"extra"}


def migrate(data):
    """Upgrade a raw dict to the current schema. Returns (data, changed)."""
    version = data.get("schema", 1)
    changed = False
    while version < SCHEMA:
        data = MIGRATIONS[version](data)
        version = data["schema"]
        changed = True
    return data, changed


def load(path):
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except ValueError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}") from e
    raw, changed = migrate(raw)
    known = {k: v for k, v in raw.items() if k in KNOWN}
    extra = {k: v for k, v in raw.items() if k not in KNOWN}
    try:
        inst = Instance(**known, extra=extra).validate()
    except TypeError as e:
        raise ConfigError(f"{path}: {e}") from e
    if changed:
        save(path, inst)
    return inst


def save(path, inst):
    inst.validate()
    data = {k: v for k, v in asdict(inst).items() if k != "extra"}
    data.update(inst.extra)
    write_json_atomic(path, data)
