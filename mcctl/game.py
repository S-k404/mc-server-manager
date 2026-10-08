"""In-game features: time, weather, game modes, teleports, items, world border, player locations, stats, activity feed.

Everything runs through `Server.run()`. Anything typed by a person is validated here before it reaches the console,
so a name or item can never smuggle a second command into the server.
"""
import json
import re

from .core import die, dir_size, info, strip_log_prefix

NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
ITEM_RE = re.compile(r"^[a-z0-9_.-]+(:[a-z0-9_./-]+)?$")
TIMES = {"day": 1000, "noon": 6000, "night": 13000, "midnight": 18000, "sunrise": 23000, "sunset": 12000}
MODES = ("survival", "creative", "adventure", "spectator")
WEATHERS = ("clear", "rain", "thunder")
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
STAT_FILES = {
    "playtime": ("minecraft:custom", "minecraft:play_time", "ticks"),
    "deaths": ("minecraft:custom", "minecraft:deaths", "count"),
    "mobs": ("minecraft:custom", "minecraft:mob_kills", "count"),
    "walked": ("minecraft:custom", "minecraft:walk_one_cm", "cm"),
    "jumps": ("minecraft:custom", "minecraft:jump", "count"),
}


# ---------- validation ----------
def player(name):
    if not NAME_RE.match(name or ""):
        die(f"'{name}' isn't a valid player name (letters, digits and _, up to 16).")
    return name


def item(name):
    name = (name or "").lower()
    if not ITEM_RE.match(name):
        die(f"'{name}' isn't a valid item id (e.g. diamond or minecraft:diamond_sword).")
    return name


def number(value, what, low=None, high=None, integer=False):
    v = str(value)
    if not NUM_RE.match(v) or (integer and "." in v):
        die(f"{what} must be a {'whole ' if integer else ''}number.")
    n = float(v)
    if low is not None and n < low or high is not None and n > high:
        die(f"{what} must be between {low} and {high}.")
    return int(n) if integer else n


def _send(s, command):
    s.require_up()
    reply = s.run(command)
    for line in reply.splitlines() or ["(no reply)"]:
        info(line)
    return reply


# ---------- world ----------
def set_time(s, value):
    v = str(value).lower()
    if v in TIMES:
        return _send(s, f"time set {v}")
    return _send(s, f"time set {number(v, 'Time', 0, 24000, integer=True)}")


def set_weather(s, kind, minutes=None):
    if kind not in WEATHERS:
        die(f"Weather must be one of: {', '.join(WEATHERS)}.")
    if minutes is None:
        return _send(s, f"weather {kind}")
    return _send(s, f"weather {kind} {number(minutes, 'Duration (minutes)', 1, 1000, integer=True) * 60}")


def seed(s):
    return _send(s, "seed")


def save(s):
    s.require_up()
    s.run("save-all flush", wait=3)
    info("World saved to disk.")


def border(s, size=None, seconds=0, center=None):
    """Show the world border, or set its diameter (optionally over `seconds`) and/or centre (x, z)."""
    s.require_up()
    if size is None and center is None:
        return _send(s, "worldborder get")
    if center is not None:
        x, z = (number(c, "Centre", -29999984, 29999984) for c in center)
        s.run(f"worldborder center {x} {z}")
    if size is not None:
        n = number(size, "Border size", 1, 59999968, integer=True)
        secs = number(seconds, "Seconds", 0, 86400, integer=True)
        return _send(s, f"worldborder set {n} {secs}" if secs else f"worldborder set {n}")


def set_spawn(s, x, y, z):
    x, y, z = (number(v, c, -29999984, 29999984, integer=True) for v, c in ((x, "X"), (y, "Y"), (z, "Z")))
    return _send(s, f"setworldspawn {x} {y} {z}")


# ---------- players ----------
def gamemode(s, mode, name):
    if mode not in MODES:
        die(f"Game mode must be one of: {', '.join(MODES)}.")
    return _send(s, f"gamemode {mode} {player(name)}")


def teleport(s, who, target=(), ):
    """tp WHO to a player (target = ['Name']) or to coordinates (target = ['x', 'y', 'z'])."""
    who = player(who)
    if len(target) == 1:
        return _send(s, f"tp {who} {player(target[0])}")
    if len(target) == 3:
        x, y, z = (number(t, "Coordinates", -29999984, 29999984) for t in target)
        return _send(s, f"tp {who} {x} {y} {z}")
    die("Teleport to a player (mc game tp A B) or to coordinates (mc game tp A 100 64 -200).")


def give(s, name, thing, count=1):
    return _send(s, f"give {player(name)} {item(thing)} {number(count, 'Count', 1, 6400, integer=True)}")


POS_RE = re.compile(r"\[\s*(-?[\d.]+)d,\s*(-?[\d.]+)d,\s*(-?[\d.]+)d\s*\]")
DIM_RE = re.compile(r'"?minecraft:(\w+)"?\s*$')


def where(s, name):
    """{'x','y','z','dimension'} for an online player, or None if they're not online."""
    name = player(name)
    s.require_up()
    pos = POS_RE.search(s.run(f"data get entity {name} Pos"))
    if not pos:
        return None
    dim = DIM_RE.search(s.run(f"data get entity {name} Dimension").strip())
    return {"x": float(pos.group(1)), "y": float(pos.group(2)), "z": float(pos.group(3)),
            "dimension": dim.group(1) if dim else "overworld"}


def where_everyone(s):
    from . import players
    return {n: where(s, n) for n in players.online(s)}


# ---------- stats ----------
def names_by_uuid(s):
    return {e["uuid"]: e["name"] for e in s.json_list("usercache.json") if "uuid" in e and "name" in e}


def _stat_dirs(s):
    level = s.prop("level-name", "world")
    return [s.dir / level / "stats"]


def leaderboard(s, stat="playtime", top=10):
    """[(name, value)] best first, read from the world's stats files (works while the server is stopped)."""
    if stat not in STAT_FILES:
        die(f"Stat must be one of: {', '.join(STAT_FILES)}.")
    group, key, _ = STAT_FILES[stat]
    names = names_by_uuid(s)
    rows = []
    for d in _stat_dirs(s):
        for f in d.glob("*.json") if d.exists() else []:
            try:
                value = json.loads(f.read_text()).get("stats", {}).get(group, {}).get(key, 0)
            except (OSError, ValueError):
                continue
            rows.append((names.get(f.stem, f.stem[:8]), value))
    return sorted(rows, key=lambda r: -r[1])[:top]


def player_stats(s, uuid):
    """{'playtime': ticks, 'deaths': n, 'mobs': n, 'walked': cm, 'jumps': n, 'advancements': n} for one player (zeros if unknown)."""
    out = {k: 0 for k in STAT_FILES}
    out["advancements"] = 0
    for d in _stat_dirs(s):
        f = d / f"{uuid}.json"
        if f.exists():
            try:
                stats = json.loads(f.read_text()).get("stats", {})
            except (OSError, ValueError):
                continue
            for stat, (group, key, _) in STAT_FILES.items():
                out[stat] = stats.get(group, {}).get(key, 0)
    adv = s.dir / s.prop("level-name", "world") / "advancements" / f"{uuid}.json"
    try:
        data = json.loads(adv.read_text())
        out["advancements"] = sum(1 for k, v in data.items() if isinstance(v, dict) and v.get("done")
                                  and not k.startswith("minecraft:recipes/"))
    except (OSError, ValueError):
        pass
    return out


def format_stat(stat, value):
    unit = STAT_FILES[stat][2]
    if unit == "ticks":
        hours, rem = divmod(value // 20, 3600)
        return f"{hours}h {rem // 60}m"
    if unit == "cm":
        return f"{value / 100000:.1f} km"
    return f"{value:,}"


# ---------- activity feed ----------
ADVANCEMENT_RE = re.compile(r"^(\w+) has (?:made the advancement|reached the goal|completed the challenge) (.+)$")


def classify(line, known):
    """('join'|'leave'|'chat'|'advancement'|'death', text) for one log line, else None."""
    m = re.match(r"^\[([\d:]+)\] \[[^\]]+\]: (.*)$", line)
    if not m:
        return None
    when, text = m.groups()
    text = text.replace("[Not Secure] ", "")
    if re.match(r"^\w+ (joined|left) the game$", text):
        return when, ("join" if "joined" in text else "leave"), text
    if re.match(r"^<\w+> ", text):
        return when, "chat", text
    adv = ADVANCEMENT_RE.match(text)
    if adv:
        return when, "advancement", text
    first = text.split(" ", 1)[0]
    if first in known and not text.startswith(f"{first} lost connection") and "logged in with entity id" not in text \
            and "moved too quickly" not in text and "UUID of player" not in text:
        return when, "death", text
    return None


TAIL_BYTES = 512 * 1024   # a long-running server's latest.log can be huge; the feed only needs the end


def tail_text(path, nbytes=TAIL_BYTES):
    """The last `nbytes` of a text file, starting on a line boundary."""
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        start = max(0, size - nbytes)
        f.seek(start)
        data = f.read().decode(errors="replace")
    return data.split("\n", 1)[1] if start and "\n" in data else data


def feed(s, lines=25):
    """Recent joins, leaves, chat, advancements and deaths from the server log (newest last)."""
    if not s.log_path.exists():
        return []
    known = {e["name"] for e in s.json_list("usercache.json") if "name" in e}
    events = [classify(ln, known) for ln in tail_text(s.log_path).splitlines()]
    return [e for e in events if e][-lines:]


# ---------- overview ----------
def overview(s):
    """A one-screen summary of the world. Live values need the server running."""
    from . import players
    data = {"version": s.label, "difficulty": s.prop("difficulty", "?"), "mode": s.prop("gamemode", "?"),
            "hardcore": s.prop("hardcore") == "true", "view": s.prop("view-distance", "?")}
    world = s.dir / s.prop("level-name", "world")
    data["world_mb"] = round(dir_size(world) / 2**20, 1) if world.exists() else 0
    if s.accepting():
        who = players.online(s)
        data["online"] = who
        data["seed"] = (re.search(r"\[(-?\d+)\]", s.run("seed")) or [None, "?"])[1]
        day = re.search(r"(\d+)", s.run("time query day"))
        data["day"] = int(day.group(1)) if day else None
        data["border"] = strip_log_prefix(s.run("worldborder get")).strip()
    return data


# ---------- flying ----------
def set_flight(s, on):
    """Server-wide allow-flight: off kicks players the server thinks are flying (creative mode is never kicked)."""
    from . import settings_schema
    settings_schema.apply(s, "allow-flight", "true" if on else "false")
    if s.up():
        info(f"Restart to apply: mc -s {s.name} restart")
