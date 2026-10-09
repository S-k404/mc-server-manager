"""The tiny local model (Ollama in Docker) and the notes it writes about a new mod. A local HTTP server stands in for Ollama."""
import asyncio
import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mcctl import container as dk
from mcctl import core, llm, modnotes, mods
from mcctl.core import Fail
from test_modmeta import jar


class FakeOllama:
    """Answers /api/tags, /api/chat and /api/pull like Ollama, and remembers every request."""

    def __init__(self, models=(), answer=None, chat_status=200, pull_lines=()):
        self.models, self.answer, self.chat_status, self.pull_lines = list(models), answer, chat_status, list(pull_lines)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, None))
                self._send(200, {"models": [{"name": m} for m in outer.models]})

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(("POST", self.path, data))
                if self.path == "/api/chat":
                    if outer.chat_status != 200:
                        return self._send(outer.chat_status, {"error": "model 'nope' not found"})
                    return self._send(200, {"message": {"role": "assistant", "content": json.dumps(outer.answer)}})
                self._send(200, b"".join(json.dumps(line).encode() + b"\n" for line in outer.pull_lines))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def chats(self):
        return [r[2] for r in self.requests if r[1] == "/api/chat"]


@pytest.fixture
def ollama(monkeypatch):
    made = []

    def make(**kw):
        fake = FakeOllama(**kw)
        made.append(fake)
        monkeypatch.setattr(llm, "PORT", fake.port)
        return fake
    yield make
    for fake in made:
        fake.close()


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(modnotes, "CACHE", tmp_path / "modnotes.json")      # never share answers between tests


# ---------- talking to the model ----------
def test_ready_is_false_when_nothing_listens_and_true_when_ollama_answers(ollama, monkeypatch):
    monkeypatch.setattr(llm, "PORT", 1)                                      # nothing listens on port 1
    assert llm.ready() is False
    ollama()
    assert llm.ready() is True


def test_chat_sends_the_documented_request_and_returns_the_answer(ollama):
    fake = ollama(answer={"summary": "x", "setup": [], "ports": []})
    out = llm.chat("be brief", "what is this?", schema={"type": "object"})
    assert json.loads(out) == {"summary": "x", "setup": [], "ports": []}
    [req] = fake.chats()
    assert req["model"] == llm.DEFAULT_MODEL and req["stream"] is False and req["format"] == {"type": "object"}
    assert req["options"] == {"temperature": 0, "num_ctx": 4096}
    assert req["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "what is this?"}]


def test_a_chosen_model_is_used_and_plain_json_is_the_default_format(ollama):
    fake = ollama(answer={})
    core.save_setting("llm_model", "llama3.2:1b")
    llm.chat("s", "u")
    assert fake.chats()[0]["model"] == "llama3.2:1b" and fake.chats()[0]["format"] == "json"


def test_errors_from_the_model_server_are_plain_messages(ollama, monkeypatch):
    ollama(chat_status=404)
    with pytest.raises(Fail, match="model 'nope' not found"):
        llm.chat("s", "u")
    monkeypatch.setattr(llm, "PORT", 1)
    with pytest.raises(Fail, match="mc llm start"):
        llm.chat("s", "u")


def test_model_presence_accepts_the_implicit_latest_tag(ollama):
    ollama(models=["qwen2.5:1.5b", "tinyllama:latest"])
    assert llm.has_model("qwen2.5:1.5b") and llm.has_model("tinyllama") and not llm.has_model("llama3.2:1b")


def test_pull_reports_progress_and_surfaces_a_registry_error(ollama, capsys):
    ollama(pull_lines=[{"status": "pulling manifest"}, {"status": "pulling abc", "total": 100, "completed": 10},
                       {"status": "pulling abc", "total": 100, "completed": 55}, {"status": "success"}])
    llm.pull_model("tiny:1b")
    out = capsys.readouterr().out
    assert "tiny:1b" in out and "pulling abc: 0%" in out and "pulling abc: 40%" in out and "is ready" in out
    ollama(pull_lines=[{"error": "pull model manifest: file does not exist"}])
    with pytest.raises(Fail, match="file does not exist.*mc llm model <name>"):
        llm.pull_model("nonexistent:9b")


# ---------- the container ----------
def docker_stubs(monkeypatch):
    ran, removed = [], []
    monkeypatch.setattr(dk, "require", lambda: None)
    monkeypatch.setattr(dk, "pull", lambda image, say=None: None)
    monkeypatch.setattr(dk, "remove", lambda name: removed.append(name))
    monkeypatch.setattr(dk, "run", lambda *a: ran.append(a))
    return ran, removed


def test_start_runs_ollama_on_loopback_with_its_models_in_a_volume(monkeypatch):
    ran, removed = docker_stubs(monkeypatch)
    pulled = []
    monkeypatch.setattr(llm, "ready", lambda: True)
    monkeypatch.setattr(llm, "has_model", lambda name=None: False)
    monkeypatch.setattr(llm, "pull_model", lambda name=None: pulled.append(name))
    llm.start()
    [args] = ran
    assert args[args.index("--name") + 1] == llm.CONTAINER and llm.CONTAINER in removed
    assert "127.0.0.1:11434:11434" in args                              # this Mac only, never the Wi-Fi
    assert f"{llm.VOLUME}:/root/.ollama" in args and "--cap-drop" in args and "ALL" in args
    assert args[args.index("--memory") + 1] == "4g" and args[args.index("--restart") + 1] == "unless-stopped"
    assert llm.enabled() and pulled == [None]                            # the default model is downloaded once


def test_start_skips_the_download_when_the_model_is_there_and_stop_keeps_the_volume(monkeypatch):
    ran, removed = docker_stubs(monkeypatch)
    pulled = []
    monkeypatch.setattr(llm, "ready", lambda: True)
    monkeypatch.setattr(llm, "has_model", lambda name=None: True)
    monkeypatch.setattr(llm, "pull_model", lambda name=None: pulled.append(name))
    llm.start()
    assert pulled == []
    llm.stop()
    assert not llm.enabled() and removed[-1] == llm.CONTAINER           # the container goes, the volume stays


def test_set_model_validates_the_name_and_pulls_it_only_if_the_server_is_up(monkeypatch):
    pulled = []
    monkeypatch.setattr(llm, "ready", lambda: False)
    monkeypatch.setattr(llm, "pull_model", lambda name=None: pulled.append(name))
    with pytest.raises(Fail):
        llm.set_model("two words")
    llm.set_model("llama3.2:1b")
    assert llm.model() == "llama3.2:1b" and pulled == []
    monkeypatch.setattr(llm, "ready", lambda: True)
    monkeypatch.setattr(llm, "has_model", lambda name=None: False)
    llm.set_model("qwen2.5:0.5b")
    assert pulled == ["qwen2.5:0.5b"]


def test_notes_need_the_model_to_be_on(monkeypatch):
    assert not llm.notes_on()
    core.save_setting("llm", "docker")
    assert llm.notes_on()
    core.save_setting("mod_notes", False)
    assert not llm.notes_on()


def test_status_survives_missing_docker(monkeypatch):
    monkeypatch.setattr(dk, "available", lambda: False)
    st = llm.status()
    assert st["docker"] is False and st["ready"] is False and st["model_present"] is False


# ---------- reading the page ----------
def test_page_text_drops_images_links_html_and_urls():
    page = ("![logo](https://x/y.png)\n\n<h1>Cool Mod</h1> adds **cool** things. See [the wiki](https://wiki.example/cool) "
            "or https://example.com/more for details.\n\n\n\n<img src='a.png'>")
    assert modnotes.paragraphs(page) == ["Cool Mod adds **cool** things. See the wiki or for details."]


def test_page_text_keeps_the_opening_and_the_setup_paragraphs_of_a_long_page():
    intro = ["Intro paragraph number %d, about nothing in particular. " % i * 3 for i in range(60)]
    setup = "To set up, open UDP port 24454 on your router and edit config/cool.toml, then restart the server."
    page = "\n\n".join(intro[:30] + [setup] + intro[30:])
    text = modnotes.page_text("Short summary.", page)
    assert text.startswith("Short summary.") and setup in text and len(text) <= modnotes.BUDGET + 200
    assert "Intro paragraph number 59" not in text                      # the padding after it was left out
    assert modnotes.page_text("Just this.") == "Just this."


# ---------- never trusting the model ----------
PAGE = ("Cool Voice adds proximity chat.\n\nYou must open UDP port 24454 on your firewall so players can connect. "
        "Players also need the mod installed on their client.\n\nEdit config/voice.properties to change the range.")


def good_answer():
    return {"summary": "Adds proximity voice chat.", "ports": ["UDP 24454"], "setup": [
        {"do": "Open UDP port 24454", "quote": "You must open UDP port 24454 on your firewall so players can connect."},
        {"do": "Edit the range", "quote": "Edit config/voice.properties to change the range."}]}


def test_check_keeps_what_the_page_backs_up():
    n = modnotes.check(json.dumps(good_answer()), PAGE)
    assert n["summary"] == "Adds proximity voice chat." and n["ports"] == ["UDP 24454"] and n["dropped"] == 0
    assert [s["do"] for s in n["steps"]] == ["Open UDP port 24454", "Edit the range"]


def test_check_drops_a_step_whose_quote_is_not_on_the_page():
    answer = good_answer()
    answer["setup"].append({"do": "Delete the world", "quote": "You must delete your world before installing this mod."})
    answer["setup"].append({"do": "Vague", "quote": "short"})
    n = modnotes.check(json.dumps(answer), PAGE)
    assert [s["do"] for s in n["steps"]] == ["Open UDP port 24454", "Edit the range"] and n["dropped"] == 2


def test_check_tolerates_markdown_and_spacing_in_a_quote_but_not_paraphrase():
    page = "**Note:**   you must   run `/voicechat setup` once as an operator after installing."
    ok = {"summary": "", "ports": [], "setup": [{"do": "Run it", "quote": "you must run /voicechat setup once as an operator after installing"}]}
    assert len(modnotes.check(json.dumps(ok), page)["steps"]) == 1
    bad = {"summary": "", "ports": [], "setup": [{"do": "Run it", "quote": "operators have to run the voicechat setup command"}]}
    assert modnotes.check(json.dumps(bad), page)["steps"] == []


def test_check_only_shows_ports_whose_number_is_on_the_page():
    answer = {"summary": "", "setup": [], "ports": ["UDP 24454", "TCP 25565", "port 8100", "banana", 80, "udp/tcp 9999"]}
    n = modnotes.check(json.dumps(answer), "It listens on UDP 24454 and also port 8100 for the map.")
    assert n["ports"] == ["UDP 24454", "port 8100"] and n["dropped"] == 4
    named = modnotes.check(json.dumps({"summary": "", "setup": [], "ports": ["TCP port 8100", "udp:24454"]}),
                           "tcp 8100 and udp 24454")
    assert named["ports"] == ["TCP 8100", "UDP 24454"]                      # natural phrasings are accepted


def test_check_cleans_the_summary_and_rejects_garbage():
    answer = {"summary": "Line one.\nLine\ttwo " + "x" * 400, "setup": [], "ports": []}
    s = modnotes.check(json.dumps(answer), PAGE)["summary"]
    assert "\n" not in s and "\t" not in s and len(s) == 200
    for junk in ("not json at all", "[1, 2]", "null"):
        with pytest.raises(Fail, match="wasn't usable"):
            modnotes.check(junk, PAGE)
    assert modnotes.check(json.dumps({"setup": "oops", "ports": None}), PAGE)["steps"] == []     # wrong types: nothing shown


def test_lines_are_plain_text_and_say_what_was_left_out():
    n = modnotes.check(json.dumps(good_answer() | {"ports": ["UDP 24454", "UDP 1"]}), PAGE)
    text = "\n".join(modnotes.lines("Cool Voice", n))
    assert "a tiny local model read the mod page; it can be wrong" in text
    assert "To do after installing:" in text and 'the page says: "You must open UDP port 24454' in text
    assert "Ports on the page: UDP 24454" in text and "1 suggestion(s) left out" in text
    empty = "\n".join(modnotes.lines("X", {"summary": "", "steps": [], "ports": [], "dropped": 0}))
    assert "mentions no setup steps" in empty


def test_the_same_page_is_asked_once(ollama):
    fake = ollama(answer=good_answer())
    first = modnotes.notes_for("Cool Voice", PAGE)
    assert modnotes.notes_for("Cool Voice", PAGE) == first and len(fake.chats()) == 1
    core.save_setting("llm_model", "llama3.2:1b")                          # another model reads it again
    modnotes.notes_for("Cool Voice", PAGE)
    assert len(fake.chats()) == 2


# ---------- when a mod is installed ----------
def item(title="Cool Voice", text=None):
    return {"title": title, "text": PAGE + " " * 0 if text is None else text}


def long_page():
    return PAGE + "\n\n" + "More about the mod and how it works in detail. " * 4


def test_nothing_happens_unless_the_model_was_started(ollama, capsys):
    fake = ollama(answer=good_answer())
    modnotes.after_install([item(text=long_page())])
    assert fake.requests == [] and capsys.readouterr().out == ""


def test_a_stopped_model_gets_one_hint_not_an_error(monkeypatch, capsys):
    core.save_setting("llm", "docker")
    monkeypatch.setattr(llm, "ready", lambda: False)
    modnotes.after_install([item(text=long_page())])
    assert "mc llm start" in capsys.readouterr().out


def test_after_an_install_the_page_is_read_and_the_notes_are_shown(ollama, capsys):
    ollama(answer=good_answer())
    core.save_setting("llm", "docker")
    modnotes.after_install([item(text=long_page()), item("Tiny", "too short")])
    out = capsys.readouterr().out
    assert "Reading the Cool Voice page with qwen2.5:1.5b" in out and "Notes on Cool Voice" in out
    assert "Open UDP port 24454" in out and "Tiny" not in out                # a page with nothing to read is skipped


def test_one_failing_page_stops_the_rest_but_never_the_install(ollama, capsys):
    ollama(chat_status=500)
    core.save_setting("llm", "docker")
    modnotes.after_install([item("One", long_page()), item("Two", long_page())])
    out = capsys.readouterr().out
    assert "couldn't read it" in out and "Reading the Two" not in out


def test_bracketed_text_from_a_page_is_printed_as_is(ollama, capsys):
    page = "Adds [fabric] things. Open port 9999 in [Settings] first. " + "x " * 100
    ollama(answer={"summary": "Adds [fabric] things.", "ports": [], "setup": [
        {"do": "Open [Settings] first", "quote": "Open port 9999 in [Settings] first."}]})
    core.save_setting("llm", "docker")
    modnotes.after_install([item("[fabric] Cool [1.21]", page)])
    out = capsys.readouterr().out
    assert "[fabric] Cool [1.21]" in out and "Open [Settings] first" in out


# ---------- hooks in add / add_jar ----------
def fake_modrinth(monkeypatch, projects, deps=None):
    """projects: {slug: project dict}. deps: {slug: [required dependency slugs]}."""
    deps = deps or {}

    def http(url, data=None):
        if "/version_files" in url:
            return {}
        if "/version?" in url:
            slug = url.split("/project/")[1].split("/")[0]
            return [{"version_type": "release", "version_number": "1", "id": "v-" + slug, "files": [
                {"primary": True, "url": "https://x/" + slug, "filename": slug + ".jar", "hashes": {"sha1": "x"}}],
                "dependencies": [{"dependency_type": "required", "project_id": d} for d in deps.get(slug, [])]}]
        slug = url.split("/project/")[1]
        if slug not in projects:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return projects[slug]
    monkeypatch.setattr(mods, "http", http)
    monkeypatch.setattr(mods, "download", lambda url, dest, sha1=None: dest.write_bytes(jar({"fabric.mod.json": json.dumps(
        {"id": dest.stem, "version": "1"})})))


def project(slug, body=""):
    return {"id": slug, "slug": slug, "title": slug.title(), "description": "d", "body": body,
            "client_side": "optional", "server_side": "required"}


def test_add_reads_only_the_mods_you_asked_for_and_only_when_told_to(make_server, monkeypatch):
    s = make_server()
    fake_modrinth(monkeypatch, {"voice": project("voice", "Big page. " * 40), "lib": project("lib", "A library. " * 40)},
                  deps={"voice": ["lib"]})
    seen = []
    monkeypatch.setattr(modnotes, "after_install", lambda items: seen.append([i["title"] for i in items]))
    mods.add(s, ["voice"], notes=True)
    assert (s.mods_dir / "lib.jar").exists() and seen == [["Voice"]]       # the dependency was installed, not read
    seen.clear()
    mods.add(make_server(name="other"), ["voice"])                          # starter mods and the like: no notes
    assert seen == []


def test_add_jar_reads_the_jars_own_description(make_server, monkeypatch, tmp_path):
    s = make_server()
    monkeypatch.setattr(mods, "http", lambda url, data=None: {})
    seen = []
    monkeypatch.setattr(modnotes, "after_install", lambda items: seen.extend(items))
    p = tmp_path / "voicechat-1.0.jar"
    p.write_bytes(jar({"fabric.mod.json": json.dumps({"id": "voicechat", "name": "Simple Voice Chat", "version": "1",
                                                      "description": "Proximity chat. " * 20})}))
    mods.add_jar(s, p)
    assert seen[0]["title"] == "Simple Voice Chat" and seen[0]["text"].startswith("Proximity chat.")


# ---------- on demand ----------
def test_explain_needs_the_model_and_a_page(make_server, monkeypatch, ollama):
    s = make_server()
    with pytest.raises(Fail, match="mc llm start"):
        modnotes.explain(s, "voice")
    core.save_setting("llm", "docker")
    monkeypatch.setattr(llm, "ready", lambda: False)
    with pytest.raises(Fail, match="isn't answering"):
        modnotes.explain(s, "voice")
    ollama(answer=good_answer())
    monkeypatch.setattr(llm, "ready", lambda: True)
    monkeypatch.setattr(mods, "http", lambda url, data=None: project("voice", ""))
    with pytest.raises(Fail, match="no page text to read"):
        modnotes.explain(s, "voice")


def test_explain_finds_a_modrinth_slug_an_installed_mod_and_a_manual_jar(make_server, monkeypatch, ollama):
    s = make_server()
    ollama(answer=good_answer())
    core.save_setting("llm", "docker")
    s.mods_dir.mkdir()
    (s.dir / "mods.lock.json").write_text(json.dumps({"schema": 1, "manual": ["hand.jar"], "mods": {"p1": {
        "project_id": "p1", "slug": "voice", "title": "Cool Voice", "version_id": "v", "version_number": "1",
        "filename": "cool-voice-1.jar", "sha1": "x"}}}))
    (s.mods_dir / "hand.jar").write_bytes(jar({"fabric.mod.json": json.dumps(
        {"id": "hand", "name": "Handmade", "version": "1", "description": PAGE})}))
    pages = {"voice": project("voice", PAGE), "p1": project("voice", PAGE)}

    def http(url, data=None):
        key = url.split("/project/")[1]
        if key not in pages:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return pages[key]
    monkeypatch.setattr(mods, "http", http)
    monkeypatch.setattr(llm, "ready", lambda: True)
    assert any("Notes on Voice" in line for line in modnotes.explain(s, "voice"))                 # a slug
    assert any("Notes on Voice" in line for line in modnotes.explain(s, "cool-voice-1.jar"))      # an installed file
    assert any("Notes on Handmade" in line for line in modnotes.explain(s, "hand"))               # a manual jar
    with pytest.raises(Fail, match="isn't on Modrinth or installed"):
        modnotes.explain(s, "nothing-like-this")


# ---------- command line and dashboard ----------
def run_cli(*args):
    from typer.testing import CliRunner
    from mcctl.cli import app
    return CliRunner().invoke(app, list(args))


def test_cli_llm_status_notes_and_model(monkeypatch):
    base = {"docker": True, "enabled": False, "model": "qwen2.5:1.5b", "notes": True, "running": False, "ready": False,
            "model_present": False}
    monkeypatch.setattr(llm, "status", lambda: base)
    assert "mc llm start" in run_cli("llm").output
    monkeypatch.setattr(llm, "status", lambda: {**base, "enabled": True, "running": True, "ready": True, "model_present": True})
    out = run_cli("llm").output
    assert "qwen2.5:1.5b" in out and "(downloaded)" in out and "Notes on new mods: on" in out
    assert run_cli("llm", "notes", "off").exit_code == 0 and core.setting("mod_notes") is False
    assert run_cli("llm", "model").output.strip() == "Model: qwen2.5:1.5b"
    assert run_cli("llm", "notes", "maybe").exit_code != 0


def test_cli_mods_explain_prints_the_notes(make_server, monkeypatch):
    s = make_server()
    monkeypatch.setattr(modnotes, "explain", lambda srv, name: [f"Notes on {name} [x]", "  plain [fabric] text"])
    r = run_cli("-s", s.name, "mods", "explain", "voice")
    assert r.exit_code == 0 and "Notes on voice [x]" in r.output and "plain [fabric] text" in r.output


def test_dashboard_mods_tab_explains_the_selected_mod(make_server, monkeypatch):
    from mcctl.tui import app as tui
    from mcctl.tui.modals import TextResult
    from test_tui import ready
    s = make_server("pytest-notes")
    s.mods_dir.mkdir(exist_ok=True)
    (s.dir / "mods.lock.json").write_text(json.dumps({"schema": 1, "mods": {}, "manual": []}))
    monkeypatch.setattr(modnotes, "explain", lambda srv, name: [f"Notes on {name}", "  Open UDP port 24454"])

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 60)) as pilot:
            await ready(app, pilot)
            app.action_tab("mods")
            await pilot.pause(0.5)
            app.query_one("#mods-explain").press()                          # nothing selected yet
            await pilot.pause(0.3)
            assert not isinstance(app.screen, TextResult)
            app.pane("mods").query_one("#mods-table").add_row("Voice", "1", "", "", "", key="voice")
            app.query_one("#mods-explain").press()
            for _ in range(40):
                await pilot.pause(0.1)
                if isinstance(app.screen, TextResult):
                    break
            assert isinstance(app.screen, TextResult) and "Open UDP port 24454" in app.screen.body
    asyncio.run(go())


def test_page_text_drops_nav_bars_and_icon_tables_but_keeps_settings_rows():
    page = ("Modrinth\n|\nCurseForge\n|\nWiki\n\n# Cool Voice\n\nA proximity voice chat.\n\n"
            "| Icon | Description |\n|:----:|-------------|\n| | You are talking |\n| | You are whispering |\n\n"
            "| Setting | Value |\n|---|---|\n| port | 24454 |\n\nRequires setup on the server.")
    text = modnotes.page_text("", page)
    lines = text.splitlines()
    assert "|" not in lines and "You are talking" not in text and "---" not in text    # pipes, empty rows, separators: gone
    assert "Modrinth" in lines and "| Setting | Value |" in lines                      # words and real table rows: kept
    assert "| port | 24454 |" in text and "Requires setup on the server." in text and "A proximity voice chat." in text
