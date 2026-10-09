"""After a mod is installed, have the tiny local model (llm.py) read its page and say what it does and what setup it needs.

Small models make things up, so nothing it says is taken on trust:
  - a setup step is shown only if the model quotes a sentence that really is on the page (checked here, in plain code);
  - a port is shown only if that number really appears on the page;
  - the summary is one sentence, labelled as machine-written;
  - whether players must install the mod too is never asked of the model: Modrinth states it (mods.py).
Anyone can write a mod page, so the page is untrusted text: the model's answer is only ever displayed, never executed or
passed on to a command.
"""
import hashlib
import json
import re
import urllib.error
import urllib.parse

from . import llm, modmeta, mods
from .atomic import write_json_atomic
from .core import ROOT, Fail, die, info

MIN_TEXT = 150                   # a page shorter than this has nothing worth reading
HEAD, BUDGET = 1500, 5500        # characters: always the opening of the page, then the paragraphs about setup, up to this
CACHE = ROOT / ".mc-modnotes.json"
CACHE_ENTRIES = 200

SYSTEM = (
    "You help the owner of a Minecraft server. Read the mod page and answer ONLY from what the page says; never guess. "
    "Reply as JSON with: summary (one short sentence: what the mod does), setup (a list of things the server owner must "
    "do AFTER installing, each with do: a short instruction and quote: one sentence copied word for word from the page "
    "that says it), ports (network ports the page says must be opened, like 'UDP 24454'). Setup means editing a config "
    "file, opening a port, running a command, or installing something else. If the page says nothing about setup, "
    "setup is an empty list.")
SCHEMA = {"type": "object", "required": ["summary", "setup", "ports"], "properties": {
    "summary": {"type": "string"},
    "setup": {"type": "array", "items": {"type": "object", "required": ["do", "quote"],
                                         "properties": {"do": {"type": "string"}, "quote": {"type": "string"}}}},
    "ports": {"type": "array", "items": {"type": "string"}}}}
ABOUT_SETUP = re.compile(r"(?i)\b(set ?up|install|config|configur|port|require|depend|command|client|firewall|forward|"
                         r"open|allow|enable|restart|\.properties|\.json|\.toml|\.ya?ml)")


# ---------- the page ----------
def _worth_keeping(line):
    """Drop table scaffolding and lone pipes (nav bars, icon tables) but keep rows that say something (`| port | 24454 |`)."""
    s = line.strip()
    if not s:
        return True                                    # blank lines are what separates paragraphs
    if re.fullmatch(r"[|:\-\s]+", s):
        return False                                   # a lone pipe, a table separator row, a rule
    if s.startswith("|"):
        return len([c for c in s.strip("|").split("|") if re.search(r"\w", c)]) >= 2
    return True


def paragraphs(text):
    """Markdown/HTML page -> plain paragraphs (images, links, tags, bare URLs and table scaffolding removed)."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text or "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = "\n".join(line for line in text.splitlines() if _worth_keeping(line))
    paras = (re.sub(r"[ \t]+", " ", p).strip() for p in re.split(r"\n\s*\n", text.replace("\r", "")))
    return [p for p in paras if len(p) >= 3]


def page_text(description, body=""):
    """What the model is shown: the start of the page, then (within the budget) the paragraphs that talk about setup."""
    paras = paragraphs(f"{description or ''}\n\n{body or ''}")
    chosen, used = [], 0
    for p in paras:
        if chosen and used + len(p) > HEAD:
            break
        chosen.append(p[:BUDGET])
        used += len(chosen[-1])
    for p in paras[len(chosen):]:
        if ABOUT_SETUP.search(p) and used + len(p) <= BUDGET:
            chosen.append(p)
            used += len(p)
    return "\n\n".join(chosen)


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[`*_>#]", "", str(text))).strip().lower().rstrip(".")


# ---------- asking, and checking the answer ----------
def check(raw, text):
    """Turn the model's JSON into {summary, steps, ports, dropped}, keeping only what the page itself backs up."""
    try:
        data = json.loads(raw)
    except ValueError:
        die("The local model's answer wasn't usable. Try again, or pick a different model: mc llm model <name>")
    if not isinstance(data, dict):
        die("The local model's answer wasn't usable.")
    page = _norm(text)
    numbers = set(re.findall(r"\b\d{2,5}\b", text))
    steps, dropped = [], 0
    for item in data.get("setup") or []:
        do, quote = (item.get("do"), item.get("quote")) if isinstance(item, dict) else (None, None)
        if isinstance(do, str) and isinstance(quote, str) and do.strip() and len(_norm(quote)) >= 12 \
                and _norm(quote) in page:
            if do.strip() not in [s["do"] for s in steps]:
                steps.append({"do": " ".join(do.split())[:200], "quote": " ".join(quote.split())})
        else:
            dropped += 1
    ports = []
    for item in data.get("ports") or []:
        m = re.fullmatch(r"(?i)\s*(tcp/udp|udp/tcp|tcp|udp)?[\s:/-]*(?:port)?[\s:/-]*(\d{2,5})\s*", item) if isinstance(item, str) else None
        if m and m.group(2) in numbers and 0 < int(m.group(2)) < 65536:
            label = f"{m.group(1).upper()} {m.group(2)}" if m.group(1) else f"port {m.group(2)}"
            if label not in ports:
                ports.append(label)
        else:
            dropped += 1
    summary = data.get("summary")
    summary = " ".join(re.sub(r"[\x00-\x1f]", " ", summary).split())[:200] if isinstance(summary, str) else ""
    return {"summary": summary, "steps": steps[:6], "ports": ports[:4], "dropped": dropped}


def _cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def notes_for(title, text):
    """Notes for one mod page. The same page read by the same model is answered from the cache."""
    key = hashlib.sha1(f"{llm.model()}\n{text}".encode()).hexdigest()
    cache = _cache()
    if key in cache:
        return cache[key]
    notes = check(llm.chat(SYSTEM, f"Mod: {title}\n\nPage:\n{text}", schema=SCHEMA), text)
    cache[key] = notes
    try:
        write_json_atomic(CACHE, dict(list(cache.items())[-CACHE_ENTRIES:]), indent=None)
    except OSError:
        pass                                           # a cache that can't be written only costs a second question
    return notes


# ---------- showing ----------
def lines(title, notes):
    """Plain text lines (the mod's own words and the model's are never read as markup)."""
    out = [f"Notes on {title} (a tiny local model read the mod page; it can be wrong):"]
    if notes["summary"]:
        out.append(f"  {notes['summary']}")
    if notes["steps"]:
        out.append("  To do after installing:")
        for step in notes["steps"]:
            quote = step["quote"] if len(step["quote"]) <= 110 else step["quote"][:107] + "..."
            out += [f"    - {step['do']}", f'      the page says: "{quote}"']
    else:
        out.append("  The page mentions no setup steps.")
    if notes["ports"]:
        out.append("  Ports on the page: " + ", ".join(notes["ports"]))
    if notes["dropped"]:
        out.append(f"  ({notes['dropped']} suggestion(s) left out because the page doesn't actually say them.)")
    return out


def item_from_project(p):
    return {"title": p.get("title") or p.get("slug") or "this mod", "text": page_text(p.get("description"), p.get("body"))}


def after_install(items):
    """Print notes for freshly installed mods. Silent unless the local model is on; never fails an install."""
    if not llm.notes_on():
        return
    if not llm.ready():
        info("Mod notes are on but the local model isn't running: mc llm start")
        return
    for item in items:
        if len(item["text"]) < MIN_TEXT:
            continue
        info(f"Reading the {item['title']} page with {llm.model()}...")
        try:
            for line in lines(item["title"], notes_for(item["title"], item["text"])):
                info(line)
        except Fail as e:
            info(f"  couldn't read it: {e}")
            break                                      # a timeout on one page would repeat on the next


# ---------- on demand ----------
def _project(name):
    try:
        return mods.http(f"{mods.MODRINTH}/project/{urllib.parse.quote(name)}")
    except urllib.error.HTTPError:
        return None


def find_page(s, name):
    """The page text of `name`: a Modrinth slug, an installed mod, or a manual jar's own description."""
    p = _project(name)
    if p is None:
        entries, manual = mods.read_lock(s)
        e = next((e for e in entries.values()
                  if name in (e.slug, e.filename, e.project_id) or name.lower() == e.title.lower()), None)
        p = _project(e.project_id) if e else None
        if p is None:
            jar = next((f for f in manual if name in (f, f.removesuffix(".jar"))), None)
            if jar is None:
                die(f"'{name}' isn't on Modrinth or installed here. Try the Modrinth slug (mc mods search ...).")
            meta = modmeta.inspect(s.mods_dir / jar, s.loader_info.markers)
            return {"title": meta.title, "text": page_text(meta.description)}
    return item_from_project(p)


def explain(s, name):
    """Lines describing one mod, for `mc mods explain` and the dashboard."""
    if not llm.enabled():
        die("The local model is off. Start it once (about 1 GB, runs in Docker): mc llm start")
    if not llm.ready():
        die("The local model isn't answering. Start it: mc llm start")
    item = find_page(s, name)
    if len(item["text"]) < MIN_TEXT:
        die(f"{item['title']} has no page text to read.")
    return lines(item["title"], notes_for(item["title"], item["text"]))
