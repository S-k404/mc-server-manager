"""Moving players' saved data when a server switches between paid (online) and cracked (offline) accounts.

A player's UUID depends on the mode: an online-mode server uses the UUID Mojang gave the account; an offline-mode server
(`mc cracked on`) uses the version-3 UUID of "OfflinePlayer:<name>". Flip the mode and every player has a new UUID, so the
inventory, stats and advancements stored under the old one, and their ops/whitelist/ban entries, silently stop applying to them.

This renames those files and rewrites those lists to the new UUIDs. It never overwrites a file, backs the world up first, and
`plan()` changes nothing so a person can read it before agreeing. Needs the server stopped.
"""
import shutil
import urllib.error
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from . import backups, players
from .atomic import write_json_atomic
from .core import Fail, die, http, info, ok, warn

PLAYER_FILES = (("playerdata", ".dat"), ("playerdata", ".dat_old"), ("stats", ".json"), ("advancements", ".json"))
LISTS = ("ops.json", "whitelist.json", "banned-players.json", "usercache.json")
MOJANG = "https://api.mojang.com/users/profiles/minecraft/"


@dataclass
class Move:
    name: str
    src: str
    dst: str
    files: list = field(default_factory=list)        # [(from path, to path)]
    entries: int = 0                                 # ops / whitelist / ban / user cache entries that name this player


@dataclass
class Plan:
    to_offline: bool
    moves: list = field(default_factory=list)
    unknown: list = field(default_factory=list)      # names whose new UUID can't be found
    conflicts: list = field(default_factory=list)    # target files that already exist (left alone)

    def __bool__(self):
        return bool(self.moves)


def _norm(value):
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def known_players(s):
    """{lowercase name: {"name", "uuids": set}} from the server's own lists: who it has seen, and under which UUIDs."""
    out = {}
    for file in LISTS:
        for e in s.json_list(file):
            name, ident = e.get("name"), _norm(e.get("uuid"))
            if name and ident:
                out.setdefault(name.lower(), {"name": name, "uuids": set()})["uuids"].add(ident)
    return out


def mojang_lookup():
    """A function name -> the account's real UUID (or None). It gives up for good after the first network failure, so a blocked
    network costs one wait, not one per player."""
    state = {"down": False}

    def look(name):
        if state["down"]:
            return None
        try:
            data = http(MOJANG + urllib.parse.quote(name))
        except urllib.error.HTTPError:
            return None                              # 404: no such account
        except Fail:
            state["down"] = True
            return None
        return _norm(data.get("id")) if isinstance(data, dict) else None
    return look


def plan(s, to_offline, lookup=None):
    """What switching to offline (`to_offline=True`) or back to online would move. Changes nothing."""
    lookup = lookup or mojang_lookup()
    out = Plan(to_offline)
    world = s.world_dir
    for p in known_players(s).values():
        name, offline = p["name"], players.offline_uuid(p["name"])
        recorded = p["uuids"] - {offline}
        if to_offline:
            sources, dst = recorded, offline
        else:
            sources = {offline}
            dst = next(iter(sorted(recorded)), None) or lookup(name)
            if dst is None:
                if any((world / d / f"{offline}{ext}").exists() for d, ext in PLAYER_FILES) or offline in p["uuids"]:
                    out.unknown.append(name)
                continue
        for src in sorted(sources):
            move = Move(name, src, dst)
            for folder, ext in PLAYER_FILES:
                a, b = world / folder / f"{src}{ext}", world / folder / f"{dst}{ext}"
                if a.exists():
                    if b.exists():
                        out.conflicts.append(b)               # the new name is taken: never overwrite
                    else:
                        move.files.append((a, b))
            move.entries = sum(1 for f in LISTS for e in s.json_list(f)
                               if (e.get("name") or "").lower() == name.lower() and _norm(e.get("uuid")) == src)
            if move.files or move.entries:
                out.moves.append(move)
    return out


def describe(plan_):
    """Plain text lines for a person."""
    way = "to offline (cracked) accounts" if plan_.to_offline else "back to paid accounts"
    lines = [f"Switching {way} gives these players new IDs, so their saved data has to move with them:"]
    for m in plan_.moves:
        lines.append(f"  {m.name}: {len(m.files)} file(s), {m.entries} list entr{'y' if m.entries == 1 else 'ies'}")
    for name in plan_.unknown:
        lines.append(f"  {name}: no paid account ID found (not reachable, or no such account); left as is")
    for target in plan_.conflicts:
        lines.append(f"  {target.name} already exists; left as is")
    return lines


def apply(s, plan_):
    """Back the world up, then rename the files and rewrite the lists. Needs the server stopped."""
    if not plan_.moves:
        return
    s.require_stopped("move players' data")
    made = backups.make_backup(s, label="pre-uuid-migration")
    if backups.status(made) != "verified":
        die("The backup before moving players' data failed verification, so nothing was changed. Fix the world or free disk "
            "space, then retry.")
    keep = s.backups_dir / f"uuid-migration-{datetime.now():%Y%m%d-%H%M%S}"
    keep.mkdir(parents=True, exist_ok=True)
    for f in LISTS:
        if (s.dir / f).exists():
            shutil.copy2(s.dir / f, keep / f)             # the world zip doesn't hold these
    swap = {m.src: m for m in plan_.moves}
    for m in plan_.moves:
        for a, b in m.files:
            if b.exists():                                # appeared since the plan: never overwrite
                warn(f"{b.name} already exists; left {a.name} alone.")
                continue
            a.rename(b)
    for f in LISTS:
        entries = s.json_list(f)
        changed = False
        for e in entries:
            m = swap.get(_norm(e.get("uuid")))
            if m and (e.get("name") or "").lower() == m.name.lower():
                e["uuid"], changed = m.dst, True
        if changed:
            write_json_atomic(s.dir / f, entries)
    ok(f"Moved the saved data of {len(plan_.moves)} player(s) to their new IDs. Lists and a backup are in backups/{s.name}/.")
    info(f"  (before: {made.name}; list files: {keep.name})")
