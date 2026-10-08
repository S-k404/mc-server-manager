"""The dashboard boots headless, shows the status bar on every tab, and the console prompt routes commands."""
import asyncio

from textual.widgets import Input, RichLog, TabbedContent, TextArea

from mcctl.tui import app as tui


def run(coro):
    return asyncio.run(coro)


async def ready(app, pilot, seconds=15):
    """Wait until the dashboard has composed, taken its first sample and built every tab (a fixed pause flaked on cold starts)."""
    for _ in range(int(seconds / 0.1)):
        await pilot.pause(0.1)
        if app.snapshot is not None and app.query("#statusbar") and app.panes_ready:
            return
    raise AssertionError("dashboard did not become ready")


def test_command_list_comes_from_the_real_cli():
    top = tui.mc_top_level()
    assert {"start", "stop", "game", "vpn", "cracked", "fly", "runtime", "doctor"} <= top
    assert "console" in tui.INTERACTIVE_ONLY and "vpn setup" in tui.INTERACTIVE_ONLY


def test_suggester_completes_mc_and_server_commands():
    sug = tui.ConsoleSuggester()
    assert run(sug.get_suggestion("/game t")) == "/game time"
    assert run(sug.get_suggestion("time se")) == "time set day"
    assert run(sug.get_suggestion("zzz")) is None and run(sug.get_suggestion("")) is None


def test_dashboard_boots_with_live_tab_first_and_status_bar(make_server):
    s = make_server("pytest-ui")

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            await ready(app, pilot)
            tabs = app.query_one("#tabs", TabbedContent)
            assert tabs.active == "tab-console"
            assert {"tab-game", "tab-files", "tab-network"} <= {p.id for p in tabs.query("TabPane")}
            bar = str(app.query_one("#statusbar").content)
            assert "pytest-ui" in bar and "STOPPED" in bar
            tabs.active = "tab-files"                        # the bar stays on screen on every tab
            await pilot.pause(0.3)
            assert app.query_one("#statusbar").display

            tabs.active = "tab-console"
            await pilot.pause(0.3)
            inp = app.query_one("#console-input", Input)
            inp.focus()
            inp.value = "/help"
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert any("Tab completes" in str(ln.text) for ln in app.query_one("#console-log", RichLog).lines)

            from mcctl.tui.files_pane import FilesPane
            fp = app.query_one("#pane-files", FilesPane)
            fp.open_file(s.dir / "server.properties")
            await pilot.pause(0.5)
            editor = app.query_one("#files-editor", TextArea)
            assert not fp.dirty and "server-port" in editor.text
            editor.insert("# note\n", (0, 0))
            await pilot.pause(0.3)
            assert fp.dirty
            fp.save()
            assert (s.dir / "server.properties").read_text().startswith("# note")
            assert not fp.dirty
    run(go())


def test_text_result_survives_logs_with_ansi_and_brackets():
    """Regression: VPN container logs ('\\x1b[0mNone ...', '[12:00:00] [Server thread/INFO]') crashed the dialog."""
    import asyncio
    from textual.app import App
    from mcctl.tui.modals import TextResult

    nasty = "\x1b[0mNone \x1b[3msecret_path\x1b[0m\x1b[2m=\x1b[0mNone version=1.0.12\n[12:00:00] [Server thread/INFO]: hi [/x]"

    async def go():
        for markup in (False, True):
            app = App()
            async with app.run_test(size=(100, 40)) as pilot:
                app.push_screen(TextResult("logs", nasty, markup=markup))
                await pilot.pause(0.3)
                text = str(app.screen.query_one("VerticalScroll > Static").render())
                assert "secret_path=None" in text and "\x1b" not in text and "[Server thread/INFO]" in text
    asyncio.run(go())


def test_help_range_toggle_and_remembered_prefs(make_server):
    from mcctl import core, metrics
    s = make_server("pytest-prefs")
    now = __import__("time").time()
    for i in range(10):
        metrics.append({"ts": now - 600 + i * 60, "sys": {"cpu": 10 + i, "ram": 50, "net": 1.0},
                        "servers": {s.name: {"cpu": 1, "ram": 2.0, "mspt": 3.0, "players": 0}}})

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            await ready(app, pilot)
            await pilot.press("question_mark")
            await pilot.pause(0.5)
            assert app.screen.__class__.__name__ == "TextResult"
            stamps = []
            orig = app.apply_snapshot
            app.apply_snapshot = lambda snap: (stamps.append(1), orig(snap))[1]
            await pilot.pause(2.5)                                    # regression: refreshes while a dialog is open
            assert len(stamps) >= 2 and app.query_one("#statusbar") is not None
            app.apply_snapshot = orig
            await pilot.press("escape")
            await pilot.press("]")                                   # next tab
            await pilot.pause(0.3)
            assert app.query_one("#tabs", TabbedContent).active == "tab-overview"
            app.action_tab("resources")
            await pilot.pause(0.5)
            await pilot.click("#res-r-1h")
            await pilot.pause(0.5)
            pane = app.pane("resources")
            assert pane.range == "1h" and app.query_one("#res-r-1h").has_class("active")
            assert len(pane.history(app.snapshot, "sys", "cpu")) > 1   # read from the agent's on-disk history
            app.theme = "nord"
            await pilot.pause(0.2)
        assert core.setting("dash_tab") == "resources" and core.setting("dash_theme") == "nord"
        app2 = tui.Dashboard()
        async with app2.run_test(size=(150, 50)) as pilot:
            await ready(app2, pilot)
            assert app2.query_one("#tabs", TabbedContent).active == "tab-resources" and app2.theme == "nord"
    run(go())


def test_console_filters_pause_clear_and_remembered_history(make_server):
    from mcctl import core
    s = make_server("pytest-console")
    s.log_path.parent.mkdir(parents=True, exist_ok=True)
    s.log_path.write_text("[10:00:00] [Server thread/INFO]: Starting\n"
                          "[10:00:01] [Server thread/WARN]: Can't keep up!\n"
                          "[10:00:02] [Server thread/INFO]: Steve joined the game\n"
                          "[10:00:03] [Server thread/INFO]: <Steve> hello there\n"
                          "[10:00:04] [Server thread/ERROR]: Something broke\n")

    def shown(app):
        return [ln.text for ln in app.query_one("#console-log").lines]

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            await ready(app, pilot)
            assert len(shown(app)) == 5
            await pilot.click("#cf-warn")
            await pilot.pause(0.3)
            assert [t for t in shown(app) if t.strip()] == [
                "[10:00:01] [Server thread/WARN]: Can't keep up!", "[10:00:04] [Server thread/ERROR]: Something broke"]
            await pilot.click("#cf-chat")
            await pilot.pause(0.3)
            assert any("<Steve> hello there" in t for t in shown(app)) and not any("Starting" in t for t in shown(app))
            await pilot.click("#cf-all")
            await pilot.pause(0.2)
            app.query_one("#console-filter").value = "joined"
            await pilot.pause(0.3)
            assert [t for t in shown(app) if t.strip()] == ["[10:00:02] [Server thread/INFO]: Steve joined the game"]
            app.query_one("#console-filter").value = ""
            await pilot.pause(0.2)
            assert len(shown(app)) == 5
            await pilot.click("#cf-pause")
            await pilot.pause(0.2)
            assert app.pane("console").paused and app.query_one("#console-log").auto_scroll is False
            await pilot.click("#cf-pause")
            assert app.query_one("#console-log").auto_scroll is True
            copied = []
            app.copy_to_clipboard = lambda t: copied.append(t)
            await pilot.click("#cf-copy")
            await pilot.pause(0.2)
            assert copied and "Steve joined the game" in copied[0]
            await pilot.click("#cf-clear")
            await pilot.pause(0.2)
            assert not [t for t in shown(app) if t.strip()]
            await pilot.press("3")                       # regression: a clicked button kept focus and tab keys did nothing
            await pilot.pause(0.4)
            assert app.query_one("#tabs", TabbedContent).active == "tab-players"
            await pilot.press("right_square_bracket")
            await pilot.pause(0.4)
            assert app.query_one("#tabs", TabbedContent).active == "tab-game"
            app.action_tab("console")
            await pilot.pause(0.4)
            inp = app.query_one("#console-input")
            inp.focus()
            for text in ("say hi", "/password set Steve hunter2"):
                inp.value = text
                await pilot.press("enter")
                await pilot.pause(0.2)
        history = core.setting("console_history")
        assert "say hi" in history and not any("hunter2" in h for h in history)      # passwords are never remembered
        app2 = tui.Dashboard(target=s.name)
        async with app2.run_test(size=(150, 50)) as pilot:
            await ready(app2, pilot)
            assert app2.query_one("#console-input").history[-1] == "say hi"
    run(go())


def test_mods_tab_lists_datapacks_and_opens_a_mods_config(make_server):
    import json
    import zipfile
    s = make_server("pytest-packs")
    (s.dir / "world" / "datapacks").mkdir(parents=True)
    with zipfile.ZipFile(s.dir / "world" / "datapacks" / "cool.zip", "w") as z:
        z.writestr("pack.mcmeta", "{}")
    (s.dir / "world" / "datapacks" / "loose-folder").mkdir()
    (s.dir / "config").mkdir()
    (s.dir / "config" / "lithium.json").write_text("{}")
    s.mods_dir.mkdir(exist_ok=True)
    (s.dir / "mods.lock.json").write_text(json.dumps({"schema": 1, "mods": {}, "manual": []}))

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 60)) as pilot:
            await ready(app, pilot)
            app.action_tab("mods")
            await pilot.pause(0.5)
            pane = app.pane("mods")
            names = {r for r in (pane.query_one("#dp-table").get_row_at(i)[0] for i in range(pane.query_one("#dp-table").row_count))}
            assert names == {"cool.zip", "loose-folder"}
            files_pane = app.pane("files")
            opened = []
            files_pane.open_file = lambda p: opened.append(p.name)
            pane.query_one("#mods-table").add_row("Lithium", "1", "", "", "", key="lithium")
            app.query_one("#mods-config").press()
            await pilot.pause(0.4)
            assert opened == ["lithium.json"] and app.query_one("#tabs", TabbedContent).active == "tab-files"
    run(go())


def _cell_style(app, x, y):
    strips = app.screen._compositor.render_strips()
    if not 0 <= y < len(strips):
        return None
    pos = 0
    for seg in strips[y]:
        if pos <= x < pos + len(seg.text):
            return seg.style
        pos += len(seg.text)


def _invisible(style):
    return bool(style and style.bgcolor and style.color and style.bgcolor.get_truecolor() == style.color.get_truecolor())


def test_no_button_label_vanishes_and_a_focused_tab_stays_readable(make_server):
    """Regression: fading a tab's opacity left half-faded backgrounds cached on its buttons (Players/Overview/Mods showed
    coloured edges and no label), and a focused tab bar painted the active tab green on green."""
    from textual.widgets import Button, Tabs
    s = make_server("pytest-vis")

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(200, 52)) as pilot:
            await ready(app, pilot)
            for tab in ("players", "overview", "mods", "backups", "network"):
                app.action_tab(tab)
                await pilot.pause(0.6)
                for b in app.query(Button):
                    r = b.region
                    if not b.display or b.disabled or not b.visible or r.width == 0 or r.height == 0:
                        continue
                    style = _cell_style(app, r.x + r.width // 2, r.y + r.height // 2)
                    assert not _invisible(style), f"{tab}: button {b.id!r} label is the same colour as its background ({style})"
            app.action_tab("players")
            await pilot.pause(0.4)
            app.query_one(Tabs).focus()
            await pilot.pause(0.4)
            r = app.query("Tab.-active").first().region
            assert not _invisible(_cell_style(app, r.x + 3, r.y)), "active tab is unreadable while the tab bar has focus"
    run(go())


def _contrast(a, b):
    def lum(c):
        t = c.get_truecolor()
        f = lambda v: v / 255 / 12.92 if v / 255 <= 0.03928 else ((v / 255 + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(t.red) + 0.7152 * f(t.green) + 0.0722 * f(t.blue)
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_server_list_text_is_readable_and_sits_on_the_rows_own_background(make_server):
    """Regression: the highlighted row inherited dark cursor text (1.9:1 contrast) and a background transition left the
    labels on a slightly different colour from their row."""
    from textual.widgets import ListView
    make_server("pytest-one")
    make_server("pytest-two", port=25570)

    async def check(app, state):
        strips = app.screen._compositor.render_strips()
        for item in app.query(tui.ServerItem):
            r = item.region
            shown = "".join("".join(seg.text for seg in strips[r.y + dy])[r.x:r.x + r.width] for dy in range(r.height))
            assert item.server_name in shown, f"{state}: the name {item.server_name!r} is missing from its row ({shown.strip()!r})"
            x, row_bg = 0, None
            for seg in strips[r.y]:                                       # the row's own colour, read near its right edge
                if x <= r.x + r.width - 3 < x + len(seg.text):
                    row_bg = seg.style.bgcolor
                x += len(seg.text)
            assert row_bg is not None
            for dy in range(2):
                x = 0
                for seg in strips[r.y + dy]:
                    n = len(seg.text)
                    if seg.text.strip() and seg.style.color and seg.style.bgcolor and r.x + 3 <= x < r.x + r.width:
                        who = f"{state}: {item.server_name} {seg.text.strip()!r}"
                        assert seg.style.bgcolor.get_truecolor() == row_bg.get_truecolor(), f"{who}: label background differs from its row"
                        if seg.text.strip() != "*":                       # the state dot is decoration
                            assert _contrast(seg.style.color, seg.style.bgcolor) >= 4.5, f"{who}: text is hard to read"
                    x += n

    async def go():
        app = tui.Dashboard(target="pytest-one")
        async with app.run_test(size=(150, 40)) as pilot:
            await ready(app, pilot)
            await pilot.pause(0.5)
            app.screen.set_focus(None)
            await pilot.pause(0.4)
            await check(app, "unfocused")
            app.query_one("#server-list", ListView).focus()
            await pilot.pause(0.4)
            await check(app, "focused")
            await pilot.hover(".srv-label")
            await pilot.pause(0.6)
            await check(app, "hovered")
    run(go())


def test_new_server_dialog_lists_versions_filters_loaders_and_returns_every_option(make_server, monkeypatch):
    from textual.widgets import Button, Collapsible, Input, Select, Static, Switch
    from mcctl import provision
    from mcctl.tui.modals import NewServer
    monkeypatch.setattr(provision, "release_versions", lambda snapshots=False: (["26.4-snapshot-1"] if snapshots else []) + ["26.3", "1.21.1", "1.16.5"])
    monkeypatch.setattr(provision, "latest_release", lambda: "26.3")
    table = {"26.3": ["fabric", "quilt", "neoforge", "forge", "paper", "vanilla"], "1.21.1": ["fabric", "paper", "vanilla"],
             "1.16.5": ["fabric", "vanilla"], "26.4-snapshot-1": ["fabric", "vanilla"]}
    monkeypatch.setattr(provision, "compatible_loaders", lambda v: table[v])
    monkeypatch.setattr(provision, "loader_note", lambda k, v: "only beta builds exist for this version" if k == "neoforge" else "")
    result = []

    async def go():
        app = tui.Dashboard(target=None)
        async with app.run_test(size=(160, 60)) as pilot:
            await ready(app, pilot)
            app.push_screen(NewServer(), result.append)
            await pilot.pause(1.0)
            dlg = app.screen
            versions = dlg.query_one("#version", Select)
            assert [label for label, _ in versions._options][:3] == ["Latest release (26.3)", "26.3", "1.21.1"]
            assert str(versions.selection if hasattr(versions, "selection") else "") != "loading"
            loader_labels = [label for label, _ in dlg.query_one("#loader", Select)._options]
            assert loader_labels == ["Fabric", "Quilt", "NeoForge (beta)", "Forge", "Paper", "Vanilla"]
            versions.value = "1.16.5"                                   # an old version: Forge / NeoForge / Quilt / Paper drop out
            await pilot.pause(0.8)
            assert [v for _, v in dlg.query_one("#loader", Select)._options] == ["fabric", "vanilla"]
            dlg.query_one("#loader", Select).value = "vanilla"
            dlg.query_one("#offline", Switch).value = True
            await pilot.pause(0.3)
            assert "no login mod" in str(dlg.query_one("#loader-hint", Static).render()).lower()
            dlg.query_one("#snapshots", Switch).value = True            # snapshots join the list
            await pilot.pause(0.8)
            assert "26.4-snapshot-1" in [v for _, v in dlg.query_one("#version", Select)._options]

            dlg.query_one("#name", Input).value = "Bad Name!"           # invalid: the dialog stays open and says why
            dlg.query_one("#create", Button).press()
            await pilot.pause(0.3)
            assert result == [] and "lowercase" in str(dlg.query_one("#form-error", Static).render())

            dlg.query_one("#name", Input).value = "survival"
            dlg.query_one("#advanced", Collapsible).collapsed = False
            dlg.query_one("#version", Select).value = "26.3"
            await pilot.pause(0.8)
            dlg.query_one("#loader", Select).value = "paper"
            dlg.query_one("#world-name", Input).value = "arena"
            dlg.query_one("#seed", Input).value = "12345"
            dlg.query_one("#motd", Input).value = "Hello"
            dlg.query_one("#difficulty", Select).value = "hard"
            dlg.query_one("#max-players", Input).value = "8"
            dlg.query_one("#hardcore", Switch).value = True
            dlg.query_one("#gc", Select).value = "zgc"
            dlg.query_one("#ram", Select).value = 6
            dlg.query_one("#create", Button).press()
            await pilot.pause(0.5)
    run(go())
    assert result and result[0] == {
        "name": "survival", "version": "26.3", "loader": "paper", "ram": 6, "offline": True, "whitelist": True,
        "port": None, "world_name": "arena", "gc": "zgc", "runtime": "native",
        "props": {"motd": "Hello", "level-seed": "12345", "difficulty": "hard", "gamemode": "", "max-players": 8,
                  "view-distance": None, "hardcore": "true"}}


def test_dashboard_renames_a_server_and_its_world(make_server, monkeypatch):
    from textual.widgets import Button, Input
    from mcctl import core
    s = make_server("pytest-old")
    make_server("pytest-other", port=25570)
    (s.dir / "world").mkdir()
    (s.dir / "world" / "level.dat").write_bytes(b"x")

    async def go():
        app = tui.Dashboard(target="pytest-old")
        async with app.run_test(size=(160, 50)) as pilot:
            await ready(app, pilot)
            app.action_rename_world()
            await pilot.pause(0.5)
            app.screen.query_one("#ask-input", Input).value = "Lobby World"
            app.screen.query_one("#ok", Button).press()
            for _ in range(40):
                await pilot.pause(0.1)
                if (s.dir / "Lobby World").exists():
                    break
            assert (s.dir / "Lobby World" / "level.dat").exists() and s.prop("level-name") == "Lobby World"

            app.action_rename_server()
            await pilot.pause(0.5)
            app.screen.query_one("#ask-input", Input).value = "pytest-new"
            app.screen.query_one("#ok", Button).press()
            for _ in range(40):
                await pilot.pause(0.1)
                if (core.SERVERS / "pytest-new").exists():
                    break
            await pilot.pause(0.6)
            assert (core.SERVERS / "pytest-new" / "Lobby World" / "level.dat").exists() and not s.dir.exists()
            assert app.current == "pytest-new"
            assert "pytest-new" in [i.server_name for i in app.query(tui.ServerItem)]

            monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: True)          # a running server can't be renamed
            app.action_rename_server()
            await pilot.pause(0.4)
            assert not app.query("#ask-input")
    run(go())


def _mock_provision(monkeypatch, note_delay=0.0):
    import time
    from mcctl import provision
    monkeypatch.setattr(provision, "release_versions", lambda snapshots=False: (["26.4-snapshot-1"] if snapshots else []) + ["26.3", "1.21.1", "1.20.4"])
    monkeypatch.setattr(provision, "latest_release", lambda: "26.3")
    monkeypatch.setattr(provision, "compatible_loaders", lambda v: ["fabric", "quilt", "neoforge", "forge", "paper", "vanilla"])
    monkeypatch.setattr(provision, "loader_note", lambda k, v: (time.sleep(note_delay), "only beta builds exist" if k == "neoforge" else "")[1])


def test_new_server_dropdowns_open_pick_and_close_when_you_click_anywhere_else(monkeypatch):
    """Regression: clicking a label / the title / the backdrop left a dropdown open; only clicking another input closed it."""
    from textual.widgets import Select
    from mcctl.tui.modals import NewServer
    _mock_provision(monkeypatch)

    async def go():
        app = tui.Dashboard(target=None)
        async with app.run_test(size=(160, 60)) as pilot:
            await ready(app, pilot)
            app.push_screen(NewServer())
            await pilot.pause(1.0)
            dlg = app.screen
            version = dlg.query_one("#version", Select)
            for outside in (".dialog-title", ".form-grid Label", "#loader-hint", ".dialog-buttons", None):   # None = the backdrop
                await pilot.click("#version")
                await pilot.pause(0.2)
                assert version.expanded, "a click on the dropdown should open it"
                await pilot.click(outside) if outside else await pilot.click(offset=(1, 1))
                await pilot.pause(0.2)
                assert not version.expanded, f"clicking {outside or 'the backdrop'} should close the open dropdown"
            from textual.widgets._select import SelectOverlay
            await pilot.click("#version")                             # pick an option with the mouse
            await pilot.pause(0.2)
            await pilot.click(version.query_one(SelectOverlay), offset=(3, 3))   # row 0 is the border: Latest, 26.3, then "1.21.1"
            await pilot.pause(0.2)
            assert version.value == "1.21.1" and not version.expanded
            loader = dlg.query_one("#loader", Select)
            await pilot.click("#loader")                              # a second dropdown works after the first closed
            await pilot.pause(0.2)
            assert loader.expanded and not version.expanded
            await pilot.click(loader.query_one(SelectOverlay), offset=(3, 2))    # "Quilt"
            await pilot.pause(0.2)
            assert loader.value == "quilt" and not loader.expanded
    run(go())


def test_a_late_refill_does_not_close_or_reset_a_dropdown_you_are_using(monkeypatch):
    from textual.widgets import Select
    from textual.widgets._select import SelectOverlay
    from mcctl.tui.modals import NewServer
    _mock_provision(monkeypatch, note_delay=0.6)               # the "(beta)" cautions arrive ~1 s after the list

    async def go():
        app = tui.Dashboard(target=None)
        async with app.run_test(size=(160, 60)) as pilot:
            await ready(app, pilot)
            app.push_screen(NewServer())
            await pilot.pause(0.5)
            loader = app.screen.query_one("#loader", Select)
            await pilot.click("#loader")                               # open it while the second fill is still pending
            await pilot.pause(0.1)
            highlighted = loader.query_one(SelectOverlay).highlighted
            await pilot.pause(2.0)                                     # the cautions arrive; the open list must not move
            assert loader.expanded and loader.query_one(SelectOverlay).highlighted == highlighted
            await pilot.click(".dialog-title")                         # closing it lets the waiting refill land
            await pilot.pause(0.8)
            assert not loader.expanded
            assert "NeoForge (beta)" in [label for label, _ in loader._options]
    run(go())


def test_snapshots_switch_sits_with_the_version_and_keeps_your_choice(monkeypatch):
    from textual.widgets import Collapsible, Select, Switch
    from mcctl.tui.modals import NewServer
    _mock_provision(monkeypatch)

    async def go():
        app = tui.Dashboard(target=None)
        async with app.run_test(size=(160, 60)) as pilot:
            await ready(app, pilot)
            app.push_screen(NewServer())
            await pilot.pause(1.0)
            dlg = app.screen
            switch = dlg.query_one("#snapshots", Switch)
            assert not any(switch in c.walk_children() for c in dlg.query(Collapsible)), "snapshots belongs next to the version"
            version = dlg.query_one("#version", Select)
            assert switch.region.y - version.region.y <= 4                      # directly under the version selector
            version.value = "1.20.4"
            await pilot.pause(0.5)
            switch.value = True
            await pilot.pause(1.0)
            assert "26.4-snapshot-1" in [v for _, v in version._options] and version.value == "1.20.4"
    run(go())


def test_dropdown_list_stays_on_screen_in_a_short_terminal(monkeypatch):
    from textual.widgets import Collapsible, Select
    from textual.widgets._select import SelectOverlay
    from mcctl.tui.modals import NewServer
    _mock_provision(monkeypatch)

    async def go():
        app = tui.Dashboard(target=None)
        async with app.run_test(size=(100, 26)) as pilot:
            await ready(app, pilot)
            app.push_screen(NewServer())
            await pilot.pause(1.0)
            dlg = app.screen
            dlg.query_one("#advanced", Collapsible).collapsed = False
            await pilot.pause(0.4)
            runtime = dlg.query_one("#runtime", Select)
            runtime.scroll_visible()
            await pilot.pause(0.3)
            await pilot.click("#runtime")
            await pilot.pause(0.3)
            overlay = runtime.query_one(SelectOverlay)
            assert runtime.expanded and overlay.region.y >= 0 and overlay.region.bottom <= app.screen.size.height
            await pilot.click(overlay, offset=(3, 2))                 # second option: Docker container
            await pilot.pause(0.2)
            assert runtime.value == "docker" and not runtime.expanded
    run(go())


def test_warn_from_a_worker_thread_reaches_the_dashboard(make_server):
    """core.warn() from a dashboard task used to raise TypeError: the reporter's helper dropped keyword arguments."""
    import threading
    from mcctl import core
    s = make_server("pytest-warn")

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            await ready(app, pilot)
            errors = []

            def work():
                try:
                    core.warn("careful")
                    core.ok("fine")
                except Exception as e:      # noqa: BLE001 - the test reports whatever the reporter raised
                    errors.append(e)
            t = threading.Thread(target=work)
            t.start()
            while t.is_alive():
                await pilot.pause(0.05)
            assert errors == []
            assert any(n.severity == "warning" and n.message == "careful" for n in app._notifications)
    run(go())


def test_a_server_folder_vanishing_mid_refresh_does_not_crash_the_dashboard(make_server):
    """Renaming a server moves its folder; a refresh that was already drawing the old name used to die with
    FileNotFoundError (seen as a rare failure of the rename test)."""
    import shutil
    s = make_server("pytest-vanish")

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            await ready(app, pilot)
            snap = app.snapshot
            shutil.move(str(s.dir), str(s.dir.with_name("pytest-vanished")))     # the folder is gone, the dashboard still holds the old name
            app.apply_snapshot(snap)                                              # must not raise
            await pilot.pause(0.3)
            assert app.is_running
    run(go())
