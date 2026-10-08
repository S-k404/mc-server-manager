"""A tiny language model that runs in Docker (Ollama) and reads a mod's page for you (see modnotes.py).

One container, `minecraft-llm`, published on this Mac's loopback only (127.0.0.1:11434), with its models in the Docker volume
`minecraft-llm-models`. It is off until `mc llm start`; after that it restarts with Docker (`--restart unless-stopped`) and
Ollama unloads the model from memory by itself a few minutes after the last question, so it costs almost nothing while idle.
Ollama checks every model layer by its sha256 when pulling. mc only ever sends it text and shows what comes back as plain text.
Model names are Ollama's (`ollama.com/library`); `mc llm model <name>` changes the one in use.
"""
import json
import urllib.error
import urllib.request

from . import container as dk
from .core import Fail, die, info, ok, save_setting, setting

CONTAINER = "minecraft-llm"
IMAGE = "ollama/ollama:latest"
VOLUME = "minecraft-llm-models"
HOST, PORT = "127.0.0.1", 11434
DEFAULT_MODEL = "qwen2.5:1.5b"          # about 1 GB; follows JSON instructions well for its size. Not checked against the registry.
MEMORY = "4g"                           # a cap, not a reservation


def model():
    return setting("llm_model") or DEFAULT_MODEL


def enabled():
    """True once `mc llm start` has been run (and not stopped on purpose)."""
    return setting("llm") == "docker"


def notes_on():
    """Should a newly installed mod's page be read? Needs the model enabled; `mc llm notes off` turns just this off."""
    return enabled() and bool(setting("mod_notes", True))


def _url(path):
    return f"http://{HOST}:{PORT}{path}"


def _call(path, data=None, timeout=10):
    """JSON in, JSON out. Dies with a plain message when Ollama isn't there or answers with an error."""
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(_url(path), data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("error", "")
        except ValueError:
            detail = ""
        die(f"The local model answered with an error ({e.code}){': ' + detail if detail else ''}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        die(f"Can't reach the local model ({getattr(e, 'reason', e)}). Start it: mc llm start")
    except ValueError:
        die("The local model sent something that isn't JSON.")


def ready():
    """Is Ollama answering? Never raises (a status check must not fail because Docker or the model is absent)."""
    try:
        with urllib.request.urlopen(_url("/api/tags"), timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return False


def models():
    """Names of the models already downloaded."""
    return [m.get("name", "") for m in _call("/api/tags").get("models", [])]


def has_model(name=None):
    name = name or model()
    have = models()
    return name in have or (":" not in name and f"{name}:latest" in have)


def chat(system, user, schema=None, timeout=120):
    """Ask the model once and return its answer text. `schema` (a JSON schema) makes Ollama return matching JSON."""
    reply = _call("/api/chat", {
        "model": model(), "stream": False, "format": schema or "json",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0, "num_ctx": 4096}}, timeout=timeout)
    return (reply.get("message") or {}).get("content", "")


# ---------- lifecycle ----------
def pull_model(name=None):
    """Download a model into the volume (a few hundred MB to ~1 GB; Ollama verifies every layer's sha256)."""
    name = name or model()
    info(f"Downloading the model '{name}' (this happens once)...")
    req = urllib.request.Request(_url("/api/pull"), data=json.dumps({"model": name, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    last = -1
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            for line in r:
                msg = json.loads(line)
                if msg.get("error"):
                    die(f"Ollama couldn't pull '{name}': {msg['error']}. Pick another model: mc llm model <name>")
                if msg.get("total") and msg.get("completed") is not None:
                    pct = int(msg["completed"] * 100 / msg["total"]) // 20 * 20          # report every 20 %
                    if pct != last:
                        last = pct
                        info(f"  {msg.get('status', 'downloading')}: {pct}%")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        die(f"Lost the connection to the local model while downloading ({getattr(e, 'reason', e)}).")
    ok(f"Model '{name}' is ready.")


def start(wait=True):
    """Start the container (pulling its image the first time) and make sure the model is downloaded."""
    dk.require()
    dk.pull(IMAGE, say=ok)
    dk.remove(CONTAINER)
    dk.run("--name", CONTAINER, "--restart", "unless-stopped", *dk.HARDENING, "--memory", MEMORY,
           "-v", f"{VOLUME}:/root/.ollama", "-p", f"{HOST}:{PORT}:{PORT}", IMAGE)
    save_setting("llm", "docker")
    if not wait:
        return
    import time
    for _ in range(60):
        if ready():
            break
        time.sleep(1)
    else:
        die("The local model didn't answer within a minute. See: docker logs " + CONTAINER)
    ok(f"Local model server running ({HOST}:{PORT}, this Mac only).")
    if not has_model():
        pull_model()


def stop():
    dk.require()
    dk.remove(CONTAINER)
    save_setting("llm", "off")
    ok("Local model stopped. Its downloaded models stay in the Docker volume.")


def set_model(name):
    name = name.strip()
    if not name or any(c.isspace() for c in name):
        raise Fail("A model name looks like qwen2.5:1.5b or llama3.2:1b (see ollama.com/library).")
    save_setting("llm_model", name)
    ok(f"Mod notes will use '{name}'.")
    if ready() and not has_model(name):
        pull_model(name)


def status():
    out = {"docker": dk.available(), "enabled": enabled(), "model": model(), "notes": bool(setting("mod_notes", True))}
    out["running"] = out["docker"] and dk.state(CONTAINER) == "running"
    out["ready"] = out["running"] and ready()
    out["model_present"] = out["ready"] and has_model()
    return out
