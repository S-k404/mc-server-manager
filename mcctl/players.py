"""Ops, whitelist, chat, moderation, and EasyAuth passwords."""
import hashlib
import re
import urllib.error
import urllib.parse

from . import game
from .atomic import write_json_atomic
from .core import die, http, info, ok, parse_player_list, warn


def offline_uuid(name):
    """Same as Java's UUID.nameUUIDFromBytes("OfflinePlayer:" + name)."""
    b = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    b[6] = (b[6] & 0x0F) | 0x30
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def uuid_for(s, name):
    if s.prop("online-mode", "true") == "false":
        return name, offline_uuid(name)
    try:
        d = http(f"https://api.mojang.com/users/profiles/minecraft/{urllib.parse.quote(name)}")
    except urllib.error.HTTPError:
        die(f"'{name}' is not a Minecraft Java account (check spelling).")
    u = d["id"]
    return d["name"], f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}"


NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def valid_name(name):
    """A player name safe to put in a console command (no spaces, newlines or selectors like @a)."""
    if not NAME_RE.match(name or ""):
        die(f"'{name}' isn't a valid player name (letters, digits and _, up to 16).")
    return name


def clean_text(text):
    """Free text for chat/kick reasons: control characters (e.g. newlines) are dropped so it stays one command."""
    return CONTROL_RE.sub(" ", text).strip()


def _edit_list(s, file, fn):
    write_json_atomic(s.dir / file, fn(s.json_list(file)))


def _apply(s, kind, add, names):
    if not names:
        die("Give at least one player name.")
    for raw in names:
        valid_name(raw)
        if s.accepting():
            verb = {("op", True): "op", ("op", False): "deop",
                    ("whitelist", True): "whitelist add", ("whitelist", False): "whitelist remove"}[(kind, add)]
            s.run_and_show(f"{verb} {raw}")
            continue
        name, uid = uuid_for(s, raw)
        if kind == "op":
            file, entry = "ops.json", {"uuid": uid, "name": name, "level": 4, "bypassesPlayerLimit": False}
        else:
            file, entry = "whitelist.json", {"uuid": uid, "name": name}
        _edit_list(s, file, lambda d: [e for e in d if e["uuid"] != uid] + ([entry] if add else []))
        ok(f"{'Added' if add else 'Removed'} {name} {'to' if add else 'from'} {file}")


def op(s, names): _apply(s, "op", True, names)
def deop(s, names): _apply(s, "op", False, names)
def whitelist_add(s, names): _apply(s, "whitelist", True, names)
def whitelist_remove(s, names): _apply(s, "whitelist", False, names)


def whitelist_enabled(s, on):
    from .settings_schema import apply
    apply(s, "white-list", "true" if on else "false")
    s.set_prop("enforce-whitelist", "true" if on else "false")


def online(s):
    if not s.accepting():
        return []
    parsed = parse_player_list(s.run("list"))
    return parsed[1] if parsed else []


def roster(s):
    """Everyone the server knows about: {name: {online, op, whitelisted, banned, last_left}}."""
    on = set(online(s))
    ops = {e["name"] for e in s.json_list("ops.json")}
    wl = {e["name"] for e in s.json_list("whitelist.json")}
    banned = {e["name"] for e in s.json_list("banned-players.json")}
    known = {e["name"] for e in s.json_list("usercache.json")}
    last_left = {}
    if s.log_path.exists():
        for line in s.log_path.read_text(errors="replace").splitlines():
            m = re.search(r"\[([\d:]+)\].*: (\w+) left the game", line)
            if m:
                last_left[m.group(2)] = m.group(1)
    names = sorted(on | ops | wl | banned | known, key=str.lower)
    return {n: {"online": n in on, "op": n in ops, "whitelisted": n in wl, "banned": n in banned,
                "last_left": last_left.get(n)} for n in names}


def say(s, message):
    s.require_up()
    s.run_and_show(f"say {clean_text(message)}")


def kick(s, name, reason=""):
    s.require_up()
    s.run_and_show(f"kick {valid_name(name)} {clean_text(reason)}".strip())


def ban(s, name, reason=""):
    s.require_up()
    s.run_and_show(f"ban {valid_name(name)} {clean_text(reason)}".strip())


def unban(s, name):
    s.require_up()
    s.run_and_show(f"pardon {valid_name(name)}")


def _need_easyauth(s):
    if not s.has_mod("easyauth"):
        die(f"'{s.name}' doesn't use EasyAuth logins (add it with: mc mods add easyauth).")
    s.require_up()


def password_reset(s, name):
    _need_easyauth(s)
    s.run_and_show(f"auth remove {name}")
    info(f"  {name} can now set a new password with /register <pw> <pw>")


def password_set(s, name, password):
    _need_easyauth(s)
    s.run_and_show(f"auth update {name} {password}")


# ---------- cracked (offline) accounts ----------
def cracked_allowed(s):
    return s.prop("online-mode", "true") == "false"


def set_cracked(s, on, insecure=False, migrate=True, dry_run=False):
    """Let non-Mojang ("cracked") accounts join, or lock the server back to paid accounts.

    Offline mode lets anyone type any name, including an op's, so turning it on also installs EasyAuth (password
    logins). On a vanilla server that can't run mods it's refused unless `insecure` is set explicitly.

    Switching modes gives every player a new UUID, so their saved data (inventory, stats, advancements, ops/whitelist
    entries) is moved to it (see uuidmigrate). That needs the server stopped; `migrate=False` skips it, `dry_run=True`
    only shows what would move.
    """
    from . import mods, settings_schema, uuidmigrate
    changing = on != (s.prop("online-mode", "true") == "false")             # is the mode really about to change?
    if dry_run:
        moves = uuidmigrate.plan(s, to_offline=on) if changing else None
        for line in (uuidmigrate.describe(moves) if moves else ["Nothing to move: no player data would change IDs."]):
            info(line)
        return
    with s.op_lock("changing cracked-account mode"):
        if on and s.loader != "fabric" and not insecure:
            die("This is a vanilla server, so it can't run a login mod: with cracked accounts allowed anyone could join "
                "as an op. Use a Fabric server (mc create NAME --loader fabric), or force it knowingly with --insecure.")
        moves = uuidmigrate.plan(s, to_offline=on) if migrate and changing else None
        if moves and s.up():
            die(f"Stop '{s.name}' first: switching accounts moves the players' saved data to their new IDs, which must not "
                f"happen under a running server. Run: mc -s {s.name} stop   (preview: mc -s {s.name} cracked "
                f"{'on' if on else 'off'} --dry-run)")
        if on:
            if s.loader == "fabric" and not s.has_mod("easyauth"):
                mods.add(s, ["easyauth"])
            if s.loader == "fabric" and not s.has_mod("skinrestorer"):
                try:
                    mods.add(s, ["skinrestorer"])   # nice to have: cracked players get their real skin
                except Exception as e:
                    warn(f"Couldn't add SkinRestorer ({e}); skins are optional.")
        if moves:
            for line in uuidmigrate.describe(moves):
                info(line)
            uuidmigrate.apply(s, moves)
        elif changing and migrate:
            info("No saved player data to move.")
        if on:
            settings_schema.apply(s, "online-mode", "false")
            s.set_prop("enforce-secure-profile", "false")
            if not s.prop("white-list") == "true":
                warn("Whitelist is off: anyone with the address can register an account. Turn it on: mc whitelist on")
            info("Cracked players must /register <password> <password> the first time, then /login <password>.")
        else:
            settings_schema.apply(s, "online-mode", "true")
            s.set_prop("enforce-secure-profile", "true")
            warn("Only paid Minecraft accounts can join now; existing cracked players are locked out.")
        if s.up():
            info(f"Restart to apply: mc -s {s.name} restart")


def tell(s, name, message):
    """Private message to one online player."""
    s.require_up()
    text = clean_text(message)
    if not text:
        die("Type a message to send.")
    s.run_and_show(f"tell {valid_name(name)} {text}")


def banned(s):
    """[{'name', 'reason', 'created', 'source'}] from banned-players.json."""
    return [{"name": e.get("name", "?"), "reason": e.get("reason", ""), "created": e.get("created", ""),
             "source": e.get("source", "")} for e in s.json_list("banned-players.json")]


def whitelist_online(s):
    """Whitelist everyone who is online right now (handy for letting the friends who are already in stay in)."""
    names = online(s)
    if not names:
        return warn("Nobody is online.")
    whitelist_add(s, names)


def find_uuid(s, name):
    """A player's UUID without any network call: from the server's own user cache, else the offline-mode UUID."""
    for e in s.json_list("usercache.json"):
        if e.get("name", "").lower() == name.lower() and e.get("uuid"):
            return e["uuid"]
    return offline_uuid(name) if s.prop("online-mode", "true") == "false" else None


def details(s, name):
    """Everything the server knows about one player (works while stopped; position/online need it running)."""
    name = valid_name(name)
    uuid = find_uuid(s, name)
    ban = next((b for b in banned(s) if b["name"].lower() == name.lower()), None)
    on = name in online(s)
    out = {"name": name, "uuid": uuid, "online": on,
           "op": any(e.get("name", "").lower() == name.lower() for e in s.json_list("ops.json")),
           "whitelisted": any(e.get("name", "").lower() == name.lower() for e in s.json_list("whitelist.json")),
           "ban": ban, "stats": game.player_stats(s, uuid) if uuid else None, "position": None, "last_seen": None}
    if s.log_path.exists():
        for line in game.tail_text(s.log_path).splitlines():
            m = re.search(r"\[([\d:]+)\].*: " + re.escape(name) + r" left the game", line)
            if m:
                out["last_seen"] = m.group(1)
    if on:
        try:
            out["position"] = game.where(s, name)
        except Exception:
            pass
    return out


def format_details(d):
    st = d["stats"]
    lines = [d["name"] + ("  (online)" if d["online"] else ""),
             f"UUID {d['uuid'] or 'unknown (online-mode accounts need to have joined once)'}",
             "Roles: " + (", ".join(x for x, on in (("operator", d["op"]), ("whitelisted", d["whitelisted"]),
                                                    ("BANNED", bool(d["ban"]))) if on) or "none")]
    if d["ban"]:
        lines.append(f"Ban reason: {d['ban']['reason'] or '(none given)'}  |  by {d['ban']['source'] or '?'}  |  {d['ban']['created'] or ''}".rstrip())
    if st:
        lines += ["", f"Playtime      {game.format_stat('playtime', st['playtime'])}",
                  f"Deaths        {st['deaths']}", f"Mobs killed   {st['mobs']}",
                  f"Walked        {game.format_stat('walked', st['walked'])}", f"Jumps         {st['jumps']:,}",
                  f"Advancements  {st['advancements']}"]
    else:
        lines.append("\nNo stats yet.")
    if d["position"]:
        pos = d["position"]
        lines.append(f"\nNow at {pos['x']:.0f}, {pos['y']:.0f}, {pos['z']:.0f} ({pos['dimension']})")
    elif d["last_seen"]:
        lines.append(f"\nLast left the game at {d['last_seen']}")
    return "\n".join(lines)
