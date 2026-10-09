"""Typed, validated settings shared by `mc set` and the dashboard Settings tab.

Three kinds of setting:
  * PROPS     - server.properties keys (some can be pushed live with a console command)
  * GAMERULES - world rules, always live via /gamerule (names differ across versions)
  * INSTANCE  - mc's own per-server options in instance.json
Add new settings here and both interfaces pick them up.
"""
import re
from dataclasses import dataclass

from .core import Fail, ok, warn
from .platform import total_ram_gb


@dataclass
class Setting:
    key: str
    label: str
    kind: str                    # bool | int | choice | text
    section: str
    help: str = ""
    choices: tuple = ()
    min: int | None = None
    max: int | None = None
    live: str | None = None      # console command template applied while running, e.g. "difficulty {v}"
    names: tuple = ()            # gamerules: candidate names, newest first
    default: str | None = None

    def normalize(self, raw):
        """Validate user input; return the canonical string value or raise Fail."""
        v = str(raw).strip()
        if self.kind == "bool":
            low = v.lower()
            if low in ("true", "on", "yes", "1", "y"):
                return "true"
            if low in ("false", "off", "no", "0", "n"):
                return "false"
            raise Fail(f"{self.label}: use on/off (true/false).")
        if self.kind == "int":
            if not re.fullmatch(r"-?\d+", v):
                raise Fail(f"{self.label}: must be a whole number.")
            n = int(v)
            if self.min is not None and n < self.min or self.max is not None and n > self.max:
                raise Fail(f"{self.label}: must be between {self.min} and {self.max}.")
            return str(n)
        if self.kind == "choice":
            if v.lower() not in self.choices:
                raise Fail(f"{self.label}: choose one of {', '.join(self.choices)}.")
            return v.lower()
        if "\n" in v:
            raise Fail(f"{self.label}: must be a single line.")
        return v


def P(key, label, kind, section, **kw):
    return Setting(key, label, kind, section, **kw)


PROPS = [
    P("motd", "Server name (MOTD)", "text", "General", help="Shown in the multiplayer list."),
    P("max-players", "Max players", "int", "General", min=1, max=1000),
    P("difficulty", "Difficulty", "choice", "Gameplay", choices=("peaceful", "easy", "normal", "hard"),
      live="difficulty {v}"),
    P("gamemode", "Default game mode", "choice", "Gameplay",
      choices=("survival", "creative", "adventure", "spectator"), live="defaultgamemode {v}"),
    P("force-gamemode", "Force game mode on join", "bool", "Gameplay"),
    P("hardcore", "Hardcore", "bool", "Gameplay", help="One life; world deleted on death in singleplayer."),
    P("pvp", "PvP", "bool", "Gameplay"),   # property in older versions; a game rule in 26.x
    P("allow-flight", "Allow flight (mods/elytra glitches)", "bool", "Gameplay",
      help="Off kicks players the server thinks are flying."),
    P("spawn-protection", "Spawn protection radius", "int", "World", min=0, max=1000),
    P("view-distance", "View distance (chunks)", "int", "Performance", min=2, max=32,
      help="Lower = less network + RAM use."),
    P("simulation-distance", "Simulation distance (chunks)", "int", "Performance", min=2, max=32,
      help="Lower = less CPU use."),
    P("player-idle-timeout", "Kick idle players after (min, 0=never)", "int", "Players", min=0, max=1440,
      live="setidletimeout {v}"),
    P("white-list", "Whitelist", "bool", "Players", live="whitelist {onoff}"),
    P("enforce-whitelist", "Kick non-whitelisted when enabling", "bool", "Players"),
    P("online-mode", "Require paid (Mojang) accounts", "bool", "Players",
      help="Off allows offline/cracked accounts; keep EasyAuth installed then."),
    P("server-port", "Game port", "int", "Network", min=1024, max=65535),
    P("resource-pack", "Resource pack URL", "text", "Resource pack",
      help="Direct link to a .zip. Easier: mc pack set <url> (it fills in the checksum too)."),
    P("resource-pack-sha1", "Resource pack checksum (sha1)", "text", "Resource pack"),
    P("require-resource-pack", "Kick players who refuse the pack", "bool", "Resource pack"),
    P("resource-pack-prompt", "Message shown with the pack offer", "text", "Resource pack"),
]

G = lambda names, label, kind="bool", **kw: Setting(names[0], label, kind, "Game rules", names=names, **kw)

GAMERULES = [
    G(("keep_inventory", "keepInventory"), "Keep inventory on death"),
    G(("pvp",), "PvP (gamerule)"),
    G(("mob_griefing", "mobGriefing"), "Mob griefing (creepers break blocks)"),
    G(("advance_time", "doDaylightCycle"), "Day/night cycle"),
    G(("advance_weather", "doWeatherCycle"), "Weather cycle"),
    G(("spawn_mobs", "doMobSpawning"), "Mob spawning"),
    G(("spawn_phantoms", "doInsomnia"), "Phantoms"),
    G(("immediate_respawn", "doImmediateRespawn"), "Skip death screen"),
    G(("natural_health_regeneration", "naturalRegeneration"), "Natural regeneration"),
    G(("show_death_messages", "showDeathMessages"), "Death messages"),
    G(("fall_damage", "fallDamage"), "Fall damage"),
    G(("tnt_explodes", "tntExplodes"), "TNT explodes"),
    G(("players_sleeping_percentage", "playersSleepingPercentage"), "Players needed to skip night (%)",
      "int", min=0, max=100),
    G(("random_tick_speed", "randomTickSpeed"), "Random tick speed (crop growth)", "int", min=0, max=4096),
    G(("respawn_radius", "spawnRadius"), "Respawn radius", "int", min=0, max=128),
]

INSTANCE = [
    Setting("ram_gb", "Max RAM (GB)", "int", "mc", min=1, max=max(1, total_ram_gb() - 4)),
    Setting("gc", "Garbage collector", "choice", "mc", choices=("g1", "zgc"),
            help="g1 = tuned default. zgc = near-zero pauses but ~25% slower bursts and more memory. Restart to apply."),
    Setting("runtime", "Runs in", "choice", "mc", choices=("native", "docker"),
            help="native = tmux + local Java; docker = a container using the same folder. Stop the server first."),
    Setting("autostart", "Start when Mac logs in", "bool", "mc", help="Needs `mc agent install`."),
    Setting("keep_alive", "Restart automatically after a crash", "bool", "mc", help="Needs `mc agent install`."),
    Setting("autobackup", "Back up on every stop", "bool", "mc"),
    Setting("backup_keep", "Backups to keep", "int", "mc", min=1, max=500),
    Setting("backup_keep_days", "Also delete backups older than (days)", "int", "mc", min=0, max=3650,
            help="0 = keep by count only. The newest backup is never deleted."),
    Setting("backup_max_gb", "Keep backups under (GB total)", "int", "mc", min=0, max=10000, help="0 = no size limit."),
    Setting("backup_push_to", "Copy backups to remote", "text", "mc",
            help="Name of a saved remote (mc remote list); every scheduled backup is copied there. Blank or 'off' = never."),
]

BY_KEY = {s.key: s for s in GAMERULES + INSTANCE}
BY_KEY.update({s.key: s for s in PROPS})   # properties win on name clashes (e.g. pvp)
for _g in GAMERULES:
    for _n in _g.names:
        BY_KEY.setdefault(_n, _g)


def find(key, server=None):
    """Look up a setting; with a server, prefer whichever form (property/game rule) it actually has."""
    s = BY_KEY.get(key)
    if s in PROPS and server is not None and (server.dir / "server.properties").exists() \
            and not server.has_prop(s.key):
        return next((g for g in GAMERULES if key in g.names), None)
    return s


def props_for(server):
    """Property settings this server's version actually has (all of them before first start)."""
    if not (server.dir / "server.properties").exists():
        return list(PROPS)
    have = server.props()
    return [p for p in PROPS if p.key in have]


# ---------- reading ----------
GAMERULE_REPLY = re.compile(r"(?:is currently set to|currently set to):?\s*(\S+)", re.I)


def read_gamerule(server, setting):
    """Current value, trying each candidate name. Returns (name, value) or (None, None)."""
    for name in setting.names:
        reply = server.run(f"gamerule {name}")
        m = GAMERULE_REPLY.search(reply)
        if m:
            return name, m.group(1).rstrip(".").lower()
    return None, None


def current(server, setting, accepting=None):
    """The value now. `accepting` lets a caller that reads a whole list ask `server.accepting()` once instead of once per game rule."""
    if setting in INSTANCE:
        v = getattr(server.inst, setting.key)
        return str(v).lower() if isinstance(v, bool) else str(v)
    if setting in GAMERULES:
        return read_gamerule(server, setting)[1] if (server.accepting() if accepting is None else accepting) else None
    return server.prop(setting.key, setting.default or "")


# ---------- writing ----------
def apply(server, key, raw):
    """Validate and save one setting. Returns 'live', 'restart', or 'saved'."""
    from .core import used_ports  # local import avoids a cycle at module load
    s = find(key, server)
    if s is None:
        if server.has_prop(key):        # unknown but real server.properties key: allow, unvalidated
            server.set_prop(key, raw)
            ok(f"{server.name}: {key}={raw}")
            return "restart" if server.up() else "saved"
        close = [k for k in BY_KEY if key.replace("_", "-") in k or k in key]
        raise Fail(f"Unknown setting '{key}'." + (f" Did you mean: {', '.join(close[:5])}?" if close else ""))
    v = s.normalize(raw)

    if s in INSTANCE:
        if s.key == "runtime":
            server.require_stopped("change where it runs")
            if v == "docker":
                from . import container as dk
                dk.require()
        if s.key == "backup_push_to":
            v = "" if v.lower() in ("", "off", "none") else v
            if v:
                from . import remote
                remote.get(v)                       # dies with the list of known remotes if there's no such name
        server.update(**{s.key: (v == "true") if s.kind == "bool" else int(v) if s.kind == "int" else v})
        ok(f"{server.name}: {s.label} = {v}")
        if s.key in ("autostart", "keep_alive") and v == "true":
            from . import agent  # local import avoids a cycle at module load
            if not agent.agent_installed():
                warn("This runs from the background agent, which isn't installed. Turn it on with: mc agent install")
        return "restart" if s.key in ("ram_gb", "gc") and server.up() else "saved"

    if s in GAMERULES:
        server.require_up()
        name, _ = read_gamerule(server, s)
        if not name:
            raise Fail(f"This Minecraft version has no '{s.label}' game rule.")
        server.run(f"gamerule {name} {v}")
        ok(f"{server.name}: {s.label} = {v} (applied now)")
        return "live"

    if s.key == "server-port":
        if int(v) in used_ports(exclude=server.name):
            raise Fail(f"Port {v} is used by another server.")
        server.update(port=int(v))
    if s.key == "online-mode" and v == "true":
        warn("Paid accounts only: offline (cracked) players will be locked out.")
    if s.key == "online-mode" and v == "false":
        server.set_prop("enforce-secure-profile", "false")
        if not server.has_mod("easyauth"):
            warn("Offline mode without EasyAuth lets anyone use an op's name. Add it: mc mods add easyauth")
    if server.prop(s.key) == v:
        ok(f"{server.name}: {s.label} is already {v}.")
        return "saved"
    server.set_prop(s.key, v)
    if server.accepting() and s.live:
        server.run(s.live.format(v=v, onoff="on" if v == "true" else "off"))
        ok(f"{server.name}: {s.label} = {v} (applied now)")
        return "live"
    ok(f"{server.name}: {s.label} = {v}")
    return "restart" if server.up() else "saved"
