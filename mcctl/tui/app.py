"""`mc` dashboard: everything you can do from the command line, with menus and keys."""
import asyncio
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.suggester import Suggester
from textual.css.query import NoMatches
from textual.markup import escape
from textual.theme import Theme
from textual.containers import Horizontal, Vertical
from textual.widgets import (Button, Footer, Header, Input, Label, ListItem, ListView, Select, Switch,
                             TabbedContent, TabPane)

from .. import agent, alerts, backups, core, countdown, datapacks, files, support, game, modnotes, moddoctor, mods, network, share, platform, players, remote, servers, settings_schema, speedtest, upgrade, vpn
from .. import container as dk
from ..core import SERVERS, Fail, all_servers, default_server_name, save_setting, setting
from ..resources import Sampler
from .files_pane import FilesPane
from .modals import AskText, Confirm, NewServer, PathPicker, ShareChoice, TextResult
from .more_panes import GamePane, NetworkPane, StatusBar
from .widgets import CatBuddy, put
from .panes import (PRIVATE_WORDS, STATE_STYLE, BackupsPane, ConsolePane, ModsPane, OverviewPane, PlayersPane, ResourcesPane,
                    SettingsPane, wid)

TABS = ["console", "overview", "players", "game", "files", "mods", "backups", "network", "settings", "resources"]
TAB_LABELS = {"console": "Live", "overview": "Overview", "players": "Players", "game": "Game", "files": "Files",
              "mods": "Mods", "backups": "Backups", "network": "Network", "settings": "Settings",
              "resources": "Resources"}
MC_THEME = Theme(
    name="mc-night", dark=True,
    primary="#5ac46f", secondary="#4aa3df", accent="#f2b84b", success="#5ac46f", warning="#f0a23a", error="#ef5d5d",
    foreground="#e6edf3", background="#0e1318", surface="#151c23", panel="#1c252d", boost="#ffffff0a",
    variables={"block-cursor-foreground": "#0e1318", "input-selection-background": "#f2b84b55",
               "footer-key-foreground": "#f2b84b", "button-color-foreground": "#0e1318"},
)
CONSOLE_POLL = 0.1   # seconds between checks of latest.log (a size check; the file is only read when it grew)
BLURRED_REFRESH = 8.0   # slow pace while the terminal isn't focused: nobody is watching
REFRESH = 1.0   # seconds between dashboard samples (system + server CPU/RAM); the game is asked every GAME_TTL
HISTORY = 300   # samples kept for sparklines (~5 min at 1 s)


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# mc commands that need a real terminal; the dashboard has a better way for each
# Every tab except the first is built after the first paint (the one you opened last, first): the Live tab alone is about a
# tenth of the widgets, and styling 500 widgets is where the dashboard spent its first second.
LAZY_PANES = {"overview": OverviewPane, "players": PlayersPane, "game": GamePane, "files": FilesPane, "mods": ModsPane,
              "backups": BackupsPane, "network": NetworkPane, "resources": ResourcesPane,
              "settings": SettingsPane}                      # the biggest (one widget per setting) last


class NotMounted:
    """What `Dashboard.pane(name)` returns for a tab that hasn't been built yet: every call does nothing. The pane is loaded with
    the current server the moment it is built, so nothing is lost."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

    def __bool__(self):
        return False


NOT_MOUNTED = NotMounted()

INTERACTIVE_ONLY = {
    "dash": "You're already in the dashboard.",
    "console": "This is the console.",
    "vpn setup": "Use the Network tab -> Set up VPN key...",
    "create": "Press n (or Ctrl+P -> New server).",
}
SERVER_COMMANDS = ["list", "say ", "time set day", "time set night", "weather clear", "weather rain", "difficulty ",
                   "gamemode creative ", "gamemode survival ", "tp ", "give ", "op ", "deop ", "kick ", "ban ", "pardon ",
                   "whitelist add ", "whitelist remove ", "whitelist list", "seed", "save-all", "worldborder get",
                   "setworldspawn", "gamerule ", "effect give ", "xp add ", "kill ", "stop"]
_mc_commands = None


def mc_commands():
    """Every `mc` command path ('start', 'game time', ...), read from the real CLI so the list can't drift."""
    global _mc_commands
    if _mc_commands is None:
        import typer
        from ..cli import app as cli_app
        out = []

        def walk(cmd, prefix):
            for name, sub in sorted(getattr(cmd, "commands", {}).items()):
                if getattr(sub, "hidden", False):
                    continue
                path = f"{prefix} {name}".strip()
                out.append((path, (sub.help or "").split("\n")[0]))
                walk(sub, path)
        walk(typer.main.get_command(cli_app), "")
        _mc_commands = out
    return _mc_commands


def mc_top_level():
    return {path.split(" ")[0] for path, _ in mc_commands()}


HELP_TEXT = (
    "Type a Minecraft command and press Enter (list, say hi, time set day, give Steve diamond...).\n"
    "Start with / to run an mc command instead: /start  /stop  /restart  /backup  /game time night  /cracked on  /doctor\n"
    "Tab completes the grey suggestion | Up/Down history | Ctrl+P = every action | 1-9,0 = tabs | q = quit.\n"
    "Copy: drag with the mouse over any text (it is copied the moment you let go) | c copies a table row | Esc clears.\n"
    "Anything that asks a question (delete, kill, restore) has a button or Ctrl+P entry with a confirmation."
)


class ConsoleSuggester(Suggester):
    def __init__(self):
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value):
        if not value:
            return None
        if value.startswith("/"):
            pool = ["/" + path for path, _ in mc_commands()]
        else:
            pool = SERVER_COMMANDS
        return next((p for p in pool if p.startswith(value) and p != value), None)


@dataclass
class Snapshot:
    system: object
    servers: dict
    vpn: bool
    crash: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    net: dict = field(default_factory=dict)   # tunnel / Docker / VPN status, refreshed every few seconds


# `[ok]` and `[x]` would be read as markup tags and vanish: escape them
GOOD_MARK = "[green]" + escape("[ok]") + "[/]"
BAD_MARK = "[red]" + escape("[x]") + "[/]"


# `[ok]` and `[x]` would be read as markup tags and vanish: escape them
GOOD_MARK = "[green]" + escape("[ok]") + "[/]"
BAD_MARK = "[red]" + escape("[x]") + "[/]"


class TuiReporter(core.Reporter):
    """Routes messages from shared service code into the dashboard (from any thread)."""

    def __init__(self, app):
        self.app = app

    def _call(self, fn, *args, **kwargs):
        try:
            if threading.current_thread() is threading.main_thread():
                fn(*args, **kwargs)
            else:
                self.app.call_from_thread(fn, *args, **kwargs)
        except (NoMatches, RuntimeError):
            pass            # the dashboard is closing: a late message has nowhere to go

    def ok(self, msg):
        self._call(self.app.activity, "[ok] " + msg, "green")
        self._call(self.app.notify, msg)

    def warn(self, msg):
        self._call(self.app.activity, "! " + msg, "yellow")
        self._call(self.app.notify, msg, severity="warning")

    def info(self, msg):
        self._call(self.app.activity, msg, "")

    def heading(self, msg):
        self._call(self.app.activity, msg, "bold")

    def progress(self, msg):
        pass


class ServerItem(ListItem):
    def __init__(self, name):
        super().__init__(Label(name, classes="srv-label"))
        self.server_name = name

    def show(self, st):
        dot = Text("* ", style=STATE_STYLE.get(st.state, "white"))
        line2 = st.state
        if st.state == "running":
            line2 = f"{len(st.players)}/{st.max_players or '?'} players | {st.tps or 0:.0f} TPS"
        put(self.query_one(Label), Text.assemble(dot, (self.server_name, "bold"), "\n  ", (line2, "grey74")))


class Dashboard(App):
    CSS_PATH = Path(__file__).with_name("app.tcss")
    TITLE = "mc"
    SUB_TITLE = "Minecraft servers"
    BINDINGS = [
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("r", "restart", "Restart"),
        Binding("b", "backup", "Backup"),
        Binding("n", "new_server", "New server"),
        Binding("f2", "rename_server", "Rename server", show=False),
        Binding("i", "invite", "Copy invite", show=False),
        Binding("d", "doctor", "Health check", show=False),
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help"),
        Binding("f5", "refresh", "Refresh", show=False),
        Binding("right_square_bracket", "next_tab(1)", "Next tab", show=False),
        Binding("left_square_bracket", "next_tab(-1)", "Previous tab", show=False),
        *[Binding(str((i + 1) % 10), f"tab('{t}')", TAB_LABELS[t], show=False) for i, t in enumerate(TABS)],
        Binding("ctrl+s", "save_file", "Save file", show=False),
    ]

    def __init__(self, target=None, first_run=False):
        super().__init__()
        self.first_run = first_run          # `mc` sets it: only a real launch asks the first-run question
        remembered = setting("dash_server")
        names = [s.name for s in all_servers()]
        self.current = (target if target and target != "all" else
                        remembered if remembered in names else default_server_name())
        self.sampler = Sampler(background_world=True)
        self.history = {"cpu": deque(maxlen=HISTORY), "ram": deque(maxlen=HISTORY), "net": deque(maxlen=HISTORY)}
        self.snapshot = None
        self.mod_updates = {}
        self._sampling = False
        self._mounted = {}                  # lazy pane name -> built
        self._pane_locks = {}
        self.panes_ready = False            # every tab built (the first paint doesn't wait for this)
        self._early_activity = []           # lines for the Overview's feed that arrived before it was built

    # ---------- layout ----------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Label("SERVERS", classes="sidebar-title")
                yield ListView(id="server-list")
                yield CatBuddy(id="cat")
                yield Label("n  new server\nCtrl+P  all actions\nDrag text to copy", classes="sidebar-help")
            with Vertical(id="main"):
                yield StatusBar(id="statusbar")
                with TabbedContent(id="tabs", initial="tab-console"):
                    with TabPane("1 Live", id="tab-console"):
                        yield ConsolePane(id="pane-console")
                    for i, name in enumerate(TABS[1:], start=2):
                        yield TabPane(f"{i % 10} {TAB_LABELS[name]}", id=f"tab-{name}")      # built by ensure_pane
        yield Footer()

    async def on_mount(self):
        self.register_theme(MC_THEME)
        saved = setting("dash_theme")
        self.theme = saved if saved in self.available_themes else "mc-night"
        self.query_one("#cat").display = bool(setting("cats", True))
        core.set_reporter(TuiReporter(self))
        self.query_one("#console-input").suggester = ConsoleSuggester()
        tab = setting("dash_tab")
        if tab in TABS:
            await self.ensure_pane(tab)             # the tab you left open last time is ready before it is shown
            self.action_tab(tab)
        self.rebuild_server_list()
        self.select_server(self.current)
        self._pace, self._poll_n, self._refresh_timer = REFRESH, 0, None
        self.set_pace(REFRESH)
        self.set_interval(CONSOLE_POLL, self._poll_console)
        self.refresh_stats()
        self.call_after_refresh(self._mount_rest)

    async def _mount_rest(self):
        """After the first paint: build the other tabs, cheap ones first, giving the screen a turn between them. The numbers in the
        status bar come first: building waits (at most half a second) for the first sample to be drawn."""
        for _ in range(25):
            if self.snapshot is not None:
                break
            await asyncio.sleep(0.02)
        for name in LAZY_PANES:
            await self.ensure_pane(name)
            await asyncio.sleep(0.01)
        self.panes_ready = True
        if self.first_run:
            self.first_run_wizard()                 # its answer drives the Network tab, which exists by now

    async def ensure_pane(self, name):
        """Build a tab's pane if it isn't built yet, and give it the current server and the latest numbers."""
        if name not in LAZY_PANES or self._mounted.get(name):
            return
        async with self._pane_locks.setdefault(name, asyncio.Lock()):
            if self._mounted.get(name):
                return
            try:
                pane = LAZY_PANES[name](id=f"pane-{name}")
                await self.query_one(f"#tab-{name}", TabPane).mount(pane)
                for _ in range(300):                # its own setup (table columns) finishes a moment after it is mounted
                    if pane.ready:
                        break
                    await asyncio.sleep(0.01)
            except NoMatches:
                return                              # the dashboard is closing
            self._mounted[name] = True
            try:
                self._fill_pane(name)
            except NoMatches:
                return                              # the dashboard closed while it was being built

    def _fill_pane(self, name):
        s = self.server
        pane = self.pane(name)
        pane.load(s)
        if name == "overview":
            for text, style in self._early_activity:
                pane.add_activity(text, style)
            self._early_activity = []
        if s:
            {"settings": self.load_settings, "players": self.load_players, "mods": self.load_mods}.get(name, lambda _s: None)(s)
        if self.snapshot:
            self.update_visible_pane()

    def pane_built(self, name):
        return name not in LAZY_PANES or self._mounted.get(name, False)

    def first_run_wizard(self):
        """Once, when no share mode has been chosen: ask how friends should find the server. 'Not now' counts as an answer, so
        this never nags; the Network tab keeps offering it."""
        if share.mode() or setting("share_wizard_done") or not all_servers():
            return
        save_setting("share_wizard_done", True)

        def chosen(choice):
            if choice is None:
                return self.notify("Pick it any time in the Network tab (8).", title="How friends find you", timeout=6)
            net = self.pane("network")
            net.switch_share(choice)
            if choice == "hidden" and not vpn.configured():
                net.setup_flow()                 # hidden has nothing to show friends until the VPN has a key
        self.push_screen(ShareChoice(), chosen)

    # ---------- idle behaviour: do almost nothing unless someone is looking ----------
    def set_pace(self, seconds):
        """How often to sample. Fast while the terminal is focused, slow when it isn't (the cat sleeps too)."""
        if self._refresh_timer is not None:
            if seconds == self._pace:
                return
            self._refresh_timer.stop()
        self._pace = seconds
        self._refresh_timer = self.set_interval(seconds, self.refresh_stats)
        try:
            self.query_one("#cat", CatBuddy).sleepy(seconds > REFRESH)
        except NoMatches:
            pass

    def on_unmount(self):
        core.set_reporter(core.Reporter())      # service code goes back to printing once the dashboard is gone

    def on_app_blur(self, event: events.AppBlur):
        self.set_pace(BLURRED_REFRESH)

    def on_app_focus(self, event: events.AppFocus):
        self.set_pace(REFRESH)
        self.refresh_stats()

    # ---------- copying ----------
    def copy_to_clipboard(self, text):
        """OSC 52 (most terminals) plus the system clipboard, because macOS Terminal ignores OSC 52."""
        super().copy_to_clipboard(text)
        try:
            platform.copy_to_clipboard(text)
        except Exception:
            pass

    def on_mouse_up(self, event):
        """Dragging over text copies it (no Ctrl+C needed; Ctrl+C also works)."""
        def copy():
            text = self.screen.get_selected_text()
            if text and text.strip():
                self.copy_to_clipboard(text)
                n = len(text)
                self.notify(f"{n} character{'s' if n != 1 else ''} copied", title="Copied", timeout=2)
        self.call_after_refresh(copy)

    # App.query/query_one search the *active* screen, so while a dialog is open the 1 s refresh couldn't find
    # #statusbar (and every pane) and crashed the dashboard. Always search the main screen instead.
    def _main_screen(self):
        stack = self.screen_stack
        return stack[0] if stack else None

    def query(self, selector=None):
        main = self._main_screen()
        return main.query(selector) if main is not None else super().query(selector)

    def query_one(self, selector, expect_type=None):
        main = self._main_screen()
        if main is None:
            return super().query_one(selector, expect_type) if expect_type else super().query_one(selector)
        return main.query_one(selector, expect_type) if expect_type else main.query_one(selector)

    def watch_theme(self, name):
        if self.is_running:
            save_setting("dash_theme", name)       # the theme you pick in Ctrl+P -> "Change theme" is remembered

    def action_next_tab(self, step):
        active = self.query_one("#tabs", TabbedContent).active.removeprefix("tab-")
        i = TABS.index(active) if active in TABS else 0
        self.action_tab(TABS[(i + int(step)) % len(TABS)])

    def action_refresh(self):
        if self.current:
            self.select_server(self.current)
        self.refresh_stats()
        self.notify("Refreshed.", timeout=2)

    def on_resize(self, event):
        self.screen.set_class(event.size.width < 110, "-narrow")      # tiles wrap 2x2 on small terminals

    def action_toggle_alerts(self):
        on = not alerts.enabled()
        save_setting("alerts", on)
        self.notify("Alerts on." if on else "Alerts off.")

    def action_set_webhook(self):
        self.push_screen(AskText("Discord webhook URL", "https://discord.com/api/webhooks/...", secret=True,
                                 hint="Discord -> channel settings -> Integrations -> Webhooks -> Copy URL. Type 'off' to remove it. "
                                      "Stored owner-only and never shown again."),
                         lambda url: url and self.run_task("Save Discord webhook", alerts.set_webhook, url))

    def action_test_alert(self):
        self.run_task("Test alert", alerts.deliver, "info", None, "This is a test from mc. Alerts are working.",
                      after=lambda _: self.notify("Test sent (check Notification Centre / Discord)."))

    def action_support(self):
        def copy(text):
            self.copy_to_clipboard(text)
            self.push_screen(TextResult("Diagnostics (copied to the clipboard)", text, markup=False))
        self.run_task("Collect diagnostics", support.bundle, 40, after=copy)

    def action_help(self):
        keys = ["Keys", "  s start | x stop | r restart | b backup | n new server | i copy invite | d health check | q quit",
                "  1-9, 0 jump to a tab | [ and ] previous / next tab | ? this help | Ctrl+P every action (themes too)",
                "  Ctrl+S saves the open file (Files tab) | c copies the highlighted table row | Esc clears a selection",
                "", "Copying", "  Drag the mouse over any text: it is copied the moment you let go.",
                "", "Console (tab 1)", "  " + HELP_TEXT.replace("\n", "\n  ")]
        self.push_screen(TextResult("Keys & tips", "\n".join(keys), markup=False))

    def _poll_console(self):
        # Every 0.1 s while a server is up and you're looking; once a second when it's stopped; rarely when unfocused.
        self._poll_n += 1
        st = self.snapshot.servers.get(self.current) if self.snapshot else None
        every = 1 if st and st.state in ("running", "starting") else 10
        if self._pace > REFRESH:
            every *= 5
        if self._poll_n % every:
            return
        try:
            self.pane("console").poll()
        except NoMatches:
            pass            # the screen is being torn down (quitting): a timer tick can land after the panes are gone

    def pane(self, name):
        if name in LAZY_PANES and not self._mounted.get(name):
            return NOT_MOUNTED                  # not built yet (or still being built: its children aren't all there): it loads when it is
        return self.query_one(f"#pane-{name}")

    @property
    def server(self):
        return core.Server(self.current) if self.current else None

    def activity(self, text, style=""):
        pane = self.pane("overview")
        if pane is NOT_MOUNTED:
            self._early_activity.append((text, style))
            return
        pane.add_activity(text, style)

    # ---------- servers list ----------
    def rebuild_server_list(self):
        lv = self.query_one("#server-list", ListView)
        names = [s.name for s in all_servers()]
        if names == [i.server_name for i in lv.query(ServerItem)]:
            return   # nothing added or removed: keep the list (and the selection) as it is
        lv.clear()
        for n in names:
            lv.append(ServerItem(n))
        if self.current not in names:
            self.current = names[0] if names else None
        if self.current:
            lv.index = names.index(self.current)

    def select_server(self, name):
        self.current = name
        s = self.server
        self.sub_title = f"{name}" if name else "no servers yet"
        for p in ("overview", "console", "settings", "backups", "game", "files", "network", "resources", "players", "mods"):
            self.pane(p).load(s)                # a tab that isn't built yet ignores this and loads when it is built
        self.mod_updates = {}
        if s:
            if self.pane_built("settings"):
                self.load_settings(s)
            if self.pane_built("players"):
                self.load_players(s)
            if self.pane_built("mods"):
                self.load_mods(s)
        if self.snapshot:
            self.apply_snapshot(self.snapshot)

    @on(ListView.Highlighted, "#server-list")
    def server_highlighted(self, event: ListView.Highlighted):
        # A rebuilt list still sends "highlighted" for entries that were just renamed or deleted: ignore those.
        if (event.item is not None and event.item.server_name != self.current
                and (SERVERS / event.item.server_name / "instance.json").exists()):
            self.select_server(event.item.server_name)
            save_setting("dash_server", event.item.server_name)

    # ---------- background refresh ----------
    @work(thread=True, group="stats")
    def refresh_stats(self):
        if self._sampling:     # the previous sample is still running (slow Docker/console): don't pile up
            return
        self._sampling = True
        try:
            self._sample()
        finally:
            self._sampling = False

    def _sample(self):
        sysd = self.sampler.system()
        stats, crash = {}, {}
        sessions = core.session_names()                  # one tmux call for every server, not one each
        for s in all_servers():
            try:
                stats[s.name] = st = self.sampler.server(s, sessions=sessions)
                if st.state == "crashed":
                    crash[s.name] = servers.crash_summary(s)["headline"]
            except Exception:  # a broken server must not freeze the dashboard
                continue
        h = self.history
        h["cpu"].append(sysd.cpu_percent)
        h["ram"].append(sysd.ram_percent)
        h["net"].append(sysd.net_down_kbs)
        for name, st in stats.items():
            h.setdefault(f"mspt:{name}", deque(maxlen=HISTORY)).append(st.mspt or 0)
            h.setdefault(f"cpu:{name}", deque(maxlen=HISTORY)).append(st.cpu_percent or 0)
            h.setdefault(f"ram:{name}", deque(maxlen=HISTORY)).append(st.ram_gb or 0)
        snap = Snapshot(sysd, stats, platform.vpn_active(), crash, h, self.net_status())
        self.call_from_thread(self.apply_snapshot, snap)

    def net_status(self):
        """Tunnel/Docker/VPN state for the status bar and Network tab. The Docker calls are slow (seconds on a Mac), so they run on a
        side thread: this returns the last answer (a quick local one the very first time) and the real answer is drawn when it lands."""
        now = time.monotonic()
        cached = getattr(self, "_net", None)
        if cached and now - cached[0] < 8:
            return cached[1]
        if not getattr(self, "_net_busy", False):
            self._net_busy = True
            threading.Thread(target=self._refresh_net, daemon=True, name="net-status").start()
        return cached[1] if cached else self._net_quick()

    def _net_quick(self):
        """What can be said without asking Docker: the share mode, the Bonjour name, whether internet play is switched on."""
        mode = share.mode()
        return {"docker": None, "tunnel": "docker" if vpn.enabled() else "off", "tunnel_ok": False, "pending": True,
                "proton_lan": None, "share_mode": mode, "bonjour": share.bonjour_alive(),
                "share_host": share.host() if mode == "local" else None}

    def _refresh_net(self):
        try:
            info = self._compute_net()
        except Exception:                                  # noqa: BLE001 - never let an odd Docker answer break the dashboard
            info = {**self._net_quick(), "pending": False}
        self._net = (time.monotonic(), info)
        self._net_busy = False
        try:
            self.call_from_thread(self._net_arrived, info)
        except RuntimeError:
            pass                                           # the dashboard closed while Docker was answering

    def _net_arrived(self, info):
        if self.snapshot is not None:
            self.apply_snapshot(replace(self.snapshot, net=info))

    def _compute_net(self):
        info = {"docker": dk.available(), "tunnel": "off", "tunnel_ok": False,
                "proton_lan": platform.proton_allows_lan() if platform.vpn_active() else None,
                "share_mode": share.mode(), "bonjour": share.bonjour_alive(),
                "share_host": share.host() if share.mode() == "local" else None}
        try:
            if vpn.enabled():
                info["tunnel"] = "docker"
                if info["docker"]:
                    st = vpn.status()
                    info["tunnel_ok"] = (st.get("vpn_health") == "healthy"
                                         and st.get("containers", {}).get(vpn.VPN) == "running")
                    info["exit"] = st.get("exit")
                    info["public"] = st.get("public")
                    info["public_lag"] = speedtest.address_latency(info["public"]) if info["public"] else None
                    info["proxy_up"] = st.get("proxy_up")
                    info["forward_server"] = st.get("forward_server")
        except Exception:
            pass   # never let a slow/odd Docker answer break the refresh
        return info

    def apply_snapshot(self, snap):
        try:
            self._apply_snapshot(snap)
        except NoMatches:
            pass            # sampled just as the dashboard was closing
        except FileNotFoundError:
            pass            # a server's folder moved (rename, delete) between sampling and drawing: the next refresh redraws

    def _apply_snapshot(self, snap):
        self.snapshot = snap
        for item in self.query(ServerItem):
            st = snap.servers.get(item.server_name)
            if st:
                item.show(st)
        sysd = snap.system
        self.title = (f"mc | CPU {sysd.cpu_percent:.0f}% | RAM {sysd.ram_used_gb:.0f}/{sysd.ram_total_gb:.0f} GB"
                      + (" | VPN ON" if snap.vpn else ""))
        self.query_one("#statusbar", StatusBar).show(self.server, snap)
        mine = snap.servers.get(self.current)
        if mine:
            self.query_one("#cat", CatBuddy).mood(mine.state, len(mine.players))
        self.update_visible_pane(snap)

    def update_visible_pane(self, snap=None):
        """Recompute and redraw only the tab you're looking at; the others catch up the moment you open them."""
        snap = snap or self.snapshot
        active = self.query_one("#tabs", TabbedContent).active.removeprefix("tab-")
        if snap and active in ("overview", "resources", "game", "network"):
            self.pane(active).update_stats(snap)

    # ---------- generic action runner ----------
    @work(thread=True, group="actions")
    def run_task(self, label, fn, *args, after=None):
        self.call_from_thread(self.activity, f"-> {label}", "bold")
        try:
            result = fn(*args)
        except Fail as e:
            self.call_from_thread(self.notify, str(e), title=label, severity="error", timeout=8)
            self.call_from_thread(self.activity, f"[x] {e}", "red")
            return
        except Exception as e:  # unexpected: show instead of crashing the UI
            self.call_from_thread(self.notify, f"{type(e).__name__}: {e}", title=label, severity="error", timeout=10)
            return
        if after:
            self.call_from_thread(after, result)
        self.call_from_thread(self.refresh_stats)

    def need_server(self):
        if not self.current:
            self.notify("Create a server first (press n).", severity="warning")
            return None
        return self.server

    # ---------- key actions ----------
    def action_tab(self, name):
        # A focused widget in the tab we are leaving (a button you just clicked) makes TabbedContent jump straight back
        # to its tab, so number keys / [ ] silently did nothing. Let go of the focus first.
        self.screen.set_focus(None)
        self.query_one("#tabs", TabbedContent).active = f"tab-{name}"

    def action_save_file(self):
        if self.query_one("#tabs", TabbedContent).active == "tab-files":
            self.pane("files").save()

    def action_start(self):
        if s := self.need_server():
            self.run_task(f"Start {s.name}", servers.start, s)

    def action_stop(self):
        if s := self.need_server():
            self.run_task(f"Stop {s.name}", servers.stop, s)

    def action_restart(self):
        if s := self.need_server():
            self.run_task(f"Restart {s.name}", servers.restart, s,
                            after=lambda _: self._hide_restart_banner())

    def _hide_restart_banner(self):
        try:
            self.query_one("#restart-banner").remove_class("show")
        except NoMatches:
            pass                                    # the Settings tab isn't built yet: nothing to hide

    def action_backup(self):
        if s := self.need_server():
            self.run_task(f"Backup {s.name}", backups.make_backup, s,
                            after=lambda _: self.pane("backups").refresh_list())

    def action_invite(self):
        if s := self.need_server():
            msg = network.invite(s)
            platform.copy_to_clipboard(msg)
            self.notify("Invite copied to clipboard.")
            self.activity(msg)

    def action_kill(self):
        if s := self.need_server():
            self.push_screen(Confirm(f"Force-stop '{s.name}'? Progress since the last autosave is lost.",
                                     "Force stop", danger=True),
                             lambda yes: yes and self.run_task(f"Kill {s.name}", servers.kill, s))

    def action_doctor(self):
        def show(checks):
            lines, group = [], None
            for c in checks:
                if c.group != group:
                    group = c.group
                    lines.append(f"\n[bold cyan]{group}[/]")
                lines.append((GOOD_MARK + " " if c.good else "[yellow]![/] ") + c.message)
                if c.fix:
                    lines.append(f"    [grey62]-> {c.fix}[/]")
            self.push_screen(TextResult("Health check", "\n".join(lines).strip()))
        self.run_task("Health check", network.doctor, all_servers(), after=show)

    def action_upgrade_plan(self):
        if s := self.need_server():
            def show(p):
                lines = [f"{s.name}: {s.inst.mc_version} -> {p.target} (Java {p.java_major})"]
                if p.downgrade:
                    lines.append("[yellow]This is a downgrade.[/]")
                lines += [f"  {GOOD_MARK if v else BAD_MARK} {e.slug}" for e, v in p.rows]
                lines += [f"  [yellow]manual:[/] {m}" for m in p.manual]
                lines.append("\nApply from the terminal: mc upgrade")
                self.push_screen(TextResult("Upgrade plan", "\n".join(lines)))
            self.run_task("Check for upgrade", upgrade.plan, s, after=show)

    def _game(self, label, fn, *args):
        if s := self.need_server():
            self.run_task(label, fn, s, *args)

    def action_world_info(self):
        if s := self.need_server():
            def show(d):
                lines = [f"{d['version']}  |  {d['difficulty']}  |  default mode {d['mode']}",
                         f"World on disk: {d['world_mb']} MB  |  view distance {d['view']}"]
                if "seed" in d:
                    lines += [f"Seed {d['seed']}  |  day {d['day']}", d["border"],
                              "Online: " + (", ".join(d["online"]) or "nobody")]
                self.push_screen(TextResult(f"World: {s.name}", "\n".join(lines)))
            self.run_task("World info", game.overview, s, after=show)

    def action_leaderboard(self, stat="playtime"):
        if s := self.need_server():
            def show(rows):
                lines = [f"{i}. {who}  {game.format_stat(stat, v)}" for i, (who, v) in enumerate(rows, 1)]
                self.push_screen(TextResult(f"Top {stat}", "\n".join(lines) or "No stats yet.", markup=False))
            self.run_task(f"Leaderboard ({stat})", game.leaderboard, s, stat, 10, after=show)

    def action_export_pack(self):
        if s := self.need_server():
            self.run_task("Export client pack", upgrade.export_client_pack, s,
                          after=lambda dest: self.notify(f"Wrote {dest}"))

    def deploy_to(self, name):
        if s := self.need_server():
            self.run_task(f"Deploy {s.name} to {name}", remote.deploy, s, name,
                          after=lambda _: self.notify(f"Copied {s.name} to {name}"))

    def action_toggle_agent(self):
        fn = agent.uninstall if agent.agent_installed() else agent.install
        self.run_task("Background agent", fn, after=lambda _: self.refresh_stats())

    def ask_countdown(self, action):
        if s := self.need_server():
            self.push_screen(AskText(f"{action.capitalize()} in...", "e.g. 30s, 5m, 1h", value="5m",
                                     hint="Players are warned in chat as the time gets close. You can cancel any time."),
                             lambda v: v and self.run_task(f"{action.capitalize()} '{s.name}' in {v}",
                                                           countdown.schedule, s, action, v))

    def action_cancel_countdown(self):
        if s := self.need_server():
            self.run_task("Cancel countdown", countdown.cancel, s)

    def action_address(self):
        addr = vpn.public_address()
        if not addr:
            return self.notify("No internet address yet (Network tab).", severity="warning")
        self.copy_to_clipboard(addr)
        self.notify(addr, title="Copied internet address")

    def fleet_action(self, what):
        name = self.pane("overview").selected_server()
        if not name:
            return self.notify("Pick a server in the table first.", severity="warning")
        target = core.Server(name)
        fn = {"start": servers.start, "stop": servers.stop, "restart": servers.restart}[what]
        self.run_task(f"{what.capitalize()} {name}", fn, target)

    def action_toggle_cat(self):
        on = not setting("cats", True)
        save_setting("cats", on)
        self.query_one("#cat").display = on
        self.notify("The cat is back." if on else "The cat left. (Ctrl+P -> Cat: show brings it back)")

    def action_new_server(self):
        def create(form):
            if not form:
                return
            def done(s):
                self.rebuild_server_list()
                self.select_server(s.name)
            self.run_task(f"Create {form['name']}", self.create_from_form, form, after=done)
        self.push_screen(NewServer(), create)

    @staticmethod
    def create_from_form(form):
        return servers.create(form["name"], form["version"], form["loader"], form["port"], form["ram"],
                              form["offline"], form["whitelist"], props=form["props"], world_name=form["world_name"],
                              gc=form["gc"], runtime=form["runtime"])

    def action_rename_server(self):
        if not (s := self.need_server()):
            return
        if s.up():
            return self.notify(f"Stop '{s.name}' first: renaming moves its folder.", severity="warning")

        def go(new):
            if not new or new == s.name:
                return

            def done(renamed):
                self.current = renamed.name
                self.rebuild_server_list()
                self._switch(renamed.name)
            self.run_task(f"Rename {s.name} -> {new}", servers.rename, s.name, new, after=done)
        self.push_screen(AskText(f"Rename server '{s.name}'", "new-name", value=s.name,
                                 hint="Lowercase letters, numbers, - and _. The folder, backups and settings move with it."), go)

    def action_rename_world(self):
        if not (s := self.need_server()):
            return
        if s.up():
            return self.notify(f"Stop '{s.name}' first: renaming moves the world folder.", severity="warning")

        def go(new):
            if not new or new == s.world_name:
                return
            self.run_task(f"Rename world {s.world_name} -> {new}", servers.rename_world, s, new,
                            after=lambda _: self.select_server(s.name))
        self.push_screen(AskText(f"Rename the world of '{s.name}'", "world", value=s.world_name,
                                 hint="Letters, numbers, spaces, - _ and . The world folder is renamed; nothing is deleted."), go)

    def action_crash_details(self):
        if s := self.need_server():
            c = servers.crash_summary(s)
            body = f"Exit code: {c['exit']}\n\n[red]{escape(str(c['headline']))}[/]\n"
            if c["report"]:
                body += f"\nCrash report: {escape(str(c['report']))}\n"
            body += "\n" + escape("\n".join(c["tail"]))
            if found := [f for f in moddoctor.collect(s) if f.level != "note"]:
                body += "\n\nMods that may explain it\n" + escape(
                    "\n".join(text for _, text in moddoctor.lines(s, found)))
            self.push_screen(TextResult(f"Why '{s.name}' crashed", body))

    # ---------- command palette ----------
    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        cur = self.current
        if cur:
            yield SystemCommand(f"Start {cur}", "Start the selected server", self.action_start)
            yield SystemCommand(f"Stop {cur}", "Stop and save", self.action_stop)
            yield SystemCommand(f"Restart {cur}", "Stop then start", self.action_restart)
            yield SystemCommand(f"Force-stop {cur}", "Kill a frozen server", self.action_kill)
            yield SystemCommand(f"Restart {cur} in...", "Warn players in chat, then restart", lambda: self.ask_countdown("restart"))
            yield SystemCommand(f"Stop {cur} in...", "Warn players in chat, then stop", lambda: self.ask_countdown("stop"))
            yield SystemCommand("Cancel the countdown", "Cancel a pending restart/stop", self.action_cancel_countdown)
            yield SystemCommand(f"Back up {cur}", "Zip the world into backups/", self.action_backup)
            yield SystemCommand("Copy invite message", "For friends on your Wi-Fi", self.action_invite)
            yield SystemCommand("Why did it crash?", "Crash details for the selected server", self.action_crash_details)
        if cur:
            yield SystemCommand("Cracked accounts: allow", "Offline accounts can join (installs EasyAuth logins)",
                                lambda: self._game("Allow cracked accounts", players.set_cracked, True))
            yield SystemCommand("Cracked accounts: block", "Paid Minecraft accounts only",
                                lambda: self._game("Block cracked accounts", players.set_cracked, False))
            yield SystemCommand("Flying: allow", "allow-flight on (restart to apply)",
                                lambda: self._game("Allow flying", game.set_flight, True))
            yield SystemCommand("Flying: forbid", "allow-flight off (restart to apply)",
                                lambda: self._game("Forbid flying", game.set_flight, False))
            yield SystemCommand("World info", "Seed, day, border, who's online", self.action_world_info)
            yield SystemCommand("Leaderboard: playtime", "Most time played", self.action_leaderboard)
            yield SystemCommand("Leaderboard: deaths", "Most deaths", lambda: self.action_leaderboard("deaths"))
            yield SystemCommand("Time: day", "Set the time to day", lambda: self._game("Time: day", game.set_time, "day"))
            yield SystemCommand("Time: night", "Set the time to night",
                                lambda: self._game("Time: night", game.set_time, "night"))
            yield SystemCommand("Weather: clear", "Stop the rain", lambda: self._game("Weather: clear", game.set_weather, "clear"))
            yield SystemCommand("Save world now", "Flush the world to disk (no backup)",
                                lambda: self._game("Save world", game.save))
            yield SystemCommand("Add datapack...", "Copy a datapack .zip/folder into the world",
                                self.pane("files").add_datapack)
            yield SystemCommand("Check for upgrade", "Plan a Minecraft version upgrade (nothing changes)",
                                self.action_upgrade_plan)
            yield SystemCommand("Export client pack", "Write a .mrpack of client mods to ~/Downloads",
                                self.action_export_pack)
        if cur:
            for rname in remote.load():
                yield SystemCommand(f"Deploy {cur} to {rname}", "rsync a stopped server to the VPS (nothing deleted there)",
                                    lambda n=rname: self.deploy_to(n))
        yield SystemCommand("Uninstall background agent" if agent.agent_installed() else "Install background agent",
                            "Autostart, keep-alive and schedules", self.action_toggle_agent)
        yield SystemCommand("Cat: hide" if setting("cats", True) else "Cat: show", "The ASCII cat in the sidebar",
                            self.action_toggle_cat)
        yield SystemCommand("New server", "Create another server", self.action_new_server)
        yield SystemCommand("Rename server...", "Change this server's name (F2)", self.action_rename_server)
        yield SystemCommand("Rename world...", "Change the name of this server's world folder", self.action_rename_world)
        yield SystemCommand("Health check", "Lag, VPN, firewall, ports, security", self.action_doctor)
        yield SystemCommand("Alerts: off" if alerts.enabled() else "Alerts: on", "Lag, RAM, disk and internet-address alerts",
                            self.action_toggle_alerts)
        yield SystemCommand("Discord webhook: set...", "Send alerts to a Discord channel (the URL is kept secret)",
                            self.action_set_webhook)
        yield SystemCommand("Send a test alert", "Mac notification + event log + Discord (if set)", self.action_test_alert)
        yield SystemCommand("Copy diagnostics", "Versions, health check, events and log tails, secrets removed", self.action_support)
        for t in TABS:
            yield SystemCommand(f"Go to {t.capitalize()}", f"Open the {t} tab", lambda t=t: self.action_tab(t))
        for s in all_servers():
            if s.name != cur:
                yield SystemCommand(f"Switch to {s.name}", f"{s.label}, port {s.port}",
                                    lambda n=s.name: self._switch(n))

    def _switch(self, name):
        lv = self.query_one("#server-list", ListView)
        names = [i.server_name for i in self.query(ServerItem)]
        lv.index = names.index(name)
        self.select_server(name)

    # ---------- buttons ----------
    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id or ""
        s = self.server
        simple = {"ov-start": self.action_start, "ov-stop": self.action_stop, "ov-restart": self.action_restart,
                  "ov-backup": self.action_backup, "ov-invite": self.action_invite, "ov-doctor": self.action_doctor,
                  "set-restart": self.action_restart, "bk-create": self.action_backup,
                  "set-rename-server": self.action_rename_server, "set-rename-world": self.action_rename_world,
                  "ov-restart-in": lambda: self.ask_countdown("restart"), "ov-cancel": self.action_cancel_countdown,
                  "ov-address": self.action_address, "ov-f-start": lambda: self.fleet_action("start"),
                  "ov-f-stop": lambda: self.fleet_action("stop"), "ov-f-restart": lambda: self.fleet_action("restart")}
        if bid in simple:
            return simple[bid]()
        if s is None:
            return
        if bid.startswith("pl-"):
            return self.player_button(bid, s)
        if bid.startswith("mods-"):
            return self.mods_button(bid, s)
        if bid == "bk-restore":
            return self.restore_backup(s)
        if bid == "bk-verify":
            self.run_task("Verify backups", backups.verify_all, s, after=lambda _: self.pane("backups").refresh_list())
        if bid == "bk-push":
            self.run_task("Copy backup offsite", backups.push, s)
        if bid == "bk-export":
            self.run_task("Export world", backups.world_export, s)
        if bid == "bk-prune":
            self.run_task("Delete old backups", backups.prune, s, 10,
                            after=lambda _: self.pane("backups").refresh_list())

    # ---------- console ----------
    @on(Input.Submitted, "#console-input")
    def console_submit(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.remember(text)
        if text and not PRIVATE_WORDS.search(text):            # remembered across sessions, but never passwords
            save_setting("console_history", event.input.history[-100:])
        event.input.value = ""
        if not text or not (s := self.need_server()):
            return
        console = self.pane("console")
        word = text.lstrip("/").split(" ", 1)[0]
        if text.startswith("mc ") or (text.startswith("/") and word in mc_top_level()):
            return self.run_mc_command(s, text.removeprefix("mc ").lstrip("/"))
        if text in ("/help", "help", "?"):
            return console.write_line(HELP_TEXT, "cyan")
        cmd = text.lstrip("/")          # /time set day (Minecraft style) is a server command too
        if not s.accepting():
            return self.notify(f"'{s.name}' is not running. Type /start to start it.", severity="warning")
        self.send_console(s, cmd)

    @work(thread=True, group="console")
    def send_console(self, s, cmd):
        try:
            reply = s.run(cmd)
        except Exception as e:
            reply = f"error: {e}"
        self.call_from_thread(self.pane("console").show_reply, cmd, reply)

    @work(thread=True, group="mc-commands")
    def run_mc_command(self, s, line):
        """Run an `mc` command exactly as the terminal would (same code, no duplicate logic) and stream its output."""
        console = self.pane("console")
        try:
            argv = shlex.split(line)
        except ValueError as e:
            return self.call_from_thread(console.write_line, f"[x] {e}", "red")
        head = " ".join(argv[:2]) if argv[:1] == ["vpn"] else (argv[0] if argv else "")
        if head in INTERACTIVE_ONLY:
            return self.call_from_thread(console.write_line, f"[x] {INTERACTIVE_ONLY[head]}", "yellow")
        self.call_from_thread(console.write_line, f"$ mc {line}", "bold magenta")
        env = {**os.environ, "MC_NO_CAT": "1", "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "110"}
        try:
            proc = subprocess.Popen([sys.executable, "-m", "mcctl", "-s", s.name, *argv], cwd=core.ROOT, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace")
            for raw in proc.stdout:
                text = ANSI_RE.sub("", raw).rstrip()
                if text:
                    self.call_from_thread(console.write_line, "  " + text)
            code = proc.wait(timeout=600)
        except Exception as e:
            return self.call_from_thread(console.write_line, f"[x] {type(e).__name__}: {e}", "red")
        if code:
            self.call_from_thread(console.write_line, "  (it asked a question or failed; use the buttons or the "
                                  "Ctrl+P menu for confirmations)" if code == 1 else f"  exit {code}", "grey62")
        self.call_from_thread(self.rebuild_server_list)
        self.call_from_thread(self.refresh_stats)

    # ---------- players ----------
    @work(thread=True, exclusive=True, group="players")
    def load_players(self, s):
        try:
            roster = players.roster(s)
        except Exception:
            roster = {}
        if s.name == self.current:
            self.call_from_thread(self.pane("players").fill, roster)

    def player_button(self, bid, s):
        name = self.query_one("#pl-name", Input).value.strip()
        reason = self.query_one("#pl-reason", Input).value.strip()
        if bid == "pl-say-go":
            text = self.query_one("#pl-say", Input).value.strip()
            if not text:
                return self.notify("Type what to broadcast.", severity="warning")
            return self.run_task("Broadcast", players.say, s, text,
                                 after=lambda _: setattr(self.query_one("#pl-say", Input), "value", ""))
        if bid == "pl-bans":
            def show(rows):
                lines = [f"{b['name']}  |  {b['reason'] or '(no reason)'}  |  {b['created']}" for b in rows]
                self.push_screen(TextResult(f"Banned on '{s.name}'", "\n".join(lines) or "Nobody is banned.", markup=False))
            return self.run_task("Ban list", players.banned, s, after=show)
        if bid == "pl-latency":
            def show(rows):
                body = "\n".join(speedtest.format_players(rows))
                self.push_screen(TextResult(f"Players on '{s.name}': connection round trip", body + (
                    "\n\nThis is the time a packet takes between this Mac and each player (measured by the connection "
                    "itself). Under 60 ms feels good; over 100 ms players notice lag." if rows else ""), markup=False))
            if not s.accepting():
                return self.notify(f"'{s.name}' is not running.", severity="warning")
            return self.run_task("Player latency", speedtest.players_latency, s, after=show)
        if bid == "pl-wl-online":
            return self.run_task("Whitelist everyone online", players.whitelist_online, s,
                                 after=lambda _: self.load_players(s))
        if not name:
            return self.notify("Type a player name or pick a row first.", severity="warning")
        refresh = lambda _: self.load_players(s)
        actions = {
            "pl-wl-add": ("Whitelist " + name, players.whitelist_add, (s, [name])),
            "pl-wl-remove": ("Un-whitelist " + name, players.whitelist_remove, (s, [name])),
            "pl-op": ("Op " + name, players.op, (s, [name])),
            "pl-deop": ("De-op " + name, players.deop, (s, [name])),
            "pl-kick": ("Kick " + name, players.kick, (s, name, reason)),
            "pl-unban": ("Unban " + name, players.unban, (s, name)),
            "pl-pass": ("Reset password for " + name, players.password_reset, (s, name)),
        }
        if bid == "pl-details":
            return self.run_task(f"Details of {name}", players.details, s, name,
                                 after=lambda d: self.push_screen(TextResult(name, players.format_details(d), markup=False)))
        if bid == "pl-msg-go":
            text = self.query_one("#pl-msg", Input).value.strip()
            return self.run_task(f"Message {name}", players.tell, s, name, text,
                                 after=lambda _: setattr(self.query_one("#pl-msg", Input), "value", ""))
        if bid == "pl-ban":
            return self.push_screen(Confirm(f"Ban {name} from '{s.name}'?", "Ban", danger=True),
                                    lambda yes: yes and self.run_task("Ban " + name, players.ban, s, name, reason,
                                                                        after=refresh))
        if bid in actions:
            label, fn, args = actions[bid]
            self.run_task(label, fn, *args, after=refresh)

    # ---------- settings ----------
    @work(thread=True, exclusive=True, group="settings")
    def load_settings(self, s):
        values = {}
        accepting = s.accepting()                                     # once, not once per game rule
        for st in settings_schema.props_for(s) + settings_schema.INSTANCE + settings_schema.GAMERULES:
            try:
                values[st.key] = settings_schema.current(s, st, accepting)
            except Exception:
                values[st.key] = None
        if s.name == self.current:
            self.call_from_thread(self.pane("settings").set_values, values)

    def apply_setting(self, key, value, revert):
        s = self.server
        if not s:
            return

        def after(result):
            if result == "restart":
                self.pane("settings").needs_restart()
                self.notify("Saved. Restart to apply (button at the top).", severity="warning")
            elif key in ("ram_gb", "autobackup"):
                self.pane("backups").refresh_list()

        def go():
            try:
                result = settings_schema.apply(s, key, value)
            except Fail as e:
                self.call_from_thread(self.notify, str(e), severity="error")
                self.call_from_thread(revert)
                return
            self.call_from_thread(after, result)
        self.run_worker(go, thread=True, group="actions")

    def _setting_key(self, widget_id):
        for st in settings_schema.PROPS + settings_schema.INSTANCE + settings_schema.GAMERULES:
            if wid(st.key) == widget_id:
                return st.key
        return None

    @on(Switch.Changed)
    def switch_changed(self, event: Switch.Changed):
        if event.switch.id == "bk-auto":
            if s := self.server:
                self.run_task("Auto-backup", backups.set_auto, s, event.value)
            return
        key = self._setting_key(event.switch.id)
        if key:
            sw = event.switch
            def revert():
                with sw.prevent(Switch.Changed):
                    sw.value = not event.value
            self.apply_setting(key, "true" if event.value else "false", revert)

    @on(Select.Changed)
    def select_changed(self, event: Select.Changed):
        key = self._setting_key(event.select.id)
        if key and event.value is not Select.NULL:
            self.apply_setting(key, event.value, lambda: self.load_settings(self.server))

    @on(Input.Submitted, ".setting-input")
    def setting_submitted(self, event: Input.Submitted):
        if event.input.id in ("sched-backup", "sched-restart"):
            return self.schedule_submitted(event.input.id.removeprefix("sched-"), event.value.strip())
        key = self._setting_key(event.input.id)
        if key:
            self.apply_setting(key, event.value, lambda: self.load_settings(self.server))

    def schedule_submitted(self, kind, value):
        if s := self.server:
            self.run_task(f"Schedule {kind}", agent.set_schedule, s, kind, value or None,
                          after=lambda _: self.load_settings(s))

    # ---------- mods ----------
    @work(thread=True, exclusive=True, group="mods")
    def load_mods(self, s, check_updates=False):
        if s.loader == "vanilla":
            self.call_from_thread(self.pane("mods").fill, {}, [])
            self.call_from_thread(self.query_one("#mods-hint").update,
                                  "Vanilla server: no mods. Create a Fabric server to use mods.")
            return
        try:
            entries, manual = mods.sync(s)
            if check_updates:
                self.mod_updates = mods.updates_available(s)
        except Fail as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        if s.name == self.current:
            self.call_from_thread(self.pane("mods").fill, entries, manual, self.mod_updates)
            if check_updates:
                n = len(self.mod_updates)
                self.call_from_thread(self.notify, f"{n} update(s) available." if n else "All mods are up to date.")

    def mods_button(self, bid, s):
        pane = self.pane("mods")
        reload = lambda _: self.load_mods(s)
        if bid == "mods-check":
            self.load_mods(s, check_updates=True)
        elif bid == "mods-update":
            self.run_task("Update mods", mods.update, s, after=reload)
        elif bid == "mods-add-jar":
            self.push_screen(PathPicker("Add a mod .jar", "jar", start="~/Downloads",
                                        hint="For mods that aren't on Modrinth (e.g. downloaded from CurseForge). "
                                             "Pick the Fabric file for this server's Minecraft version."),
                             lambda src: src and self.run_task("Add mod jar", mods.add_jar, s, src, after=reload))
        elif bid == "mods-doctor":
            self.run_task("Check mods", moddoctor.collect, s,
                          after=lambda found: self.push_screen(TextResult("Mod problems", self.doctor_text(s, found),
                                                                          markup=False)))
        elif bid == "mods-explain":
            name = pane.selected("mods-table")
            if not name:
                return self.notify("Pick an installed mod first.", severity="warning")
            self.run_task(f"Read the {name} page", modnotes.explain, s, name,
                          after=lambda out: self.push_screen(TextResult(f"About {name}", "\n".join(out), markup=False)))
        elif bid == "mods-fix":
            def fixed(result):
                done, left = result
                body = "\n".join([*(f"Done: {d}" for d in done), *([""] if done else []), self.doctor_text(s, left)])
                self.push_screen(TextResult("Mod problems", body, markup=False))
                self.load_mods(s)
            self.push_screen(Confirm(f"Let mc fix mod problems on '{s.name}'? It installs missing libraries, updates mods "
                                     "built for another Minecraft version, and moves useless jars into backups/ "
                                     "(nothing is deleted). The server must be stopped.", "Fix"),
                             lambda yes: yes and self.run_task("Fix mods", moddoctor.fix, s, after=fixed))
        elif bid == "mods-config":
            slug = pane.selected("mods-table")
            if not slug:
                return self.notify("Pick an installed mod first.", severity="warning")
            found = files.find_configs(s, slug.removesuffix(".jar"))
            if not found:
                return self.notify(f"No config files for {slug} in config/ (it may not have any, or the server hasn't "
                                   "created them yet).", severity="warning")
            self.action_tab("files")
            self.pane("files").open_file(found[0])
            if len(found) > 1:
                self.notify(f"{len(found)} config files match; opened {found[0].name}. Others are in the tree under config/.")
        elif bid == "mods-dp-search":
            self.search_datapacks(s, self.query_one("#dp-query", Input).value.strip())
        elif bid == "mods-dp-install":
            slug = pane.selected("dp-results")
            if not slug:
                return self.notify("Search, then pick a datapack in the results.", severity="warning")
            self.run_task(f"Install datapack {slug}", datapacks.install, s, [slug],
                          after=lambda _: pane.fill_datapacks())
        elif bid == "mods-dp-update":
            self.run_task("Update datapacks", datapacks.update, s, after=lambda _: pane.fill_datapacks())
        elif bid == "mods-dp-remove":
            name = pane.selected("dp-table")
            if not name:
                return self.notify("Pick an installed datapack first.", severity="warning")
            self.push_screen(Confirm(f"Remove datapack {escape(name)}? It is moved into backups/, not deleted.", "Remove",
                                     danger=True),
                             lambda yes: yes and self.run_task(f"Remove {name}", datapacks.remove, s, name,
                                                               after=lambda _: pane.fill_datapacks()))
        elif bid == "mods-dp-add":
            self.pane("files").add_datapack()
        elif bid == "mods-search":
            self.search_mods(s, self.query_one("#mods-query", Input).value.strip())
        elif bid == "mods-install":
            slug = pane.selected("mods-results")
            if not slug:
                return self.notify("Search, then pick a mod in the results.", severity="warning")
            self.run_task(f"Install {slug}", lambda: mods.add(s, [slug], notes=True), after=reload)
        elif bid in ("mods-remove", "mods-pin"):
            slug = pane.selected("mods-table")
            if not slug:
                return self.notify("Pick an installed mod first.", severity="warning")
            if bid == "mods-remove":
                self.push_screen(Confirm(f"Remove {slug} from '{s.name}'?", "Remove", danger=True),
                                 lambda yes: yes and self.run_task(f"Remove {slug}", mods.remove, s, [slug],
                                                                     after=reload))
            else:
                entries, _ = mods.read_lock(s)
                pinned = next((e.pinned for e in entries.values() if e.slug == slug), False)
                self.run_task(f"{'Unpin' if pinned else 'Pin'} {slug}", mods.pin, s, slug, not pinned, after=reload)

    @staticmethod
    def doctor_text(s, found):
        return "\n".join([moddoctor.headline(found), "", *(text for _, text in moddoctor.lines(s, found))])

    @on(Input.Submitted, "#dp-query")
    def dp_query_submitted(self, event):
        if s := self.server:
            self.search_datapacks(s, event.value.strip())

    @work(thread=True, exclusive=True, group="dp-search")
    def search_datapacks(self, s, query):
        if not query:
            return
        try:
            hits = datapacks.search(s, query)
        except Fail as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        self.call_from_thread(self.pane("mods").fill_dp_results, hits)
        if not hits:
            self.call_from_thread(self.notify, "No datapacks found for that.", severity="warning")

    @on(Input.Submitted, "#mods-query")
    def mods_query_submitted(self, event):
        if s := self.server:
            self.search_mods(s, event.value.strip())

    @work(thread=True, exclusive=True, group="mod-search")
    def search_mods(self, s, query):
        if not query:
            return
        try:
            hits = mods.search(s, query)
        except Fail as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        self.call_from_thread(self.pane("mods").fill_results, hits)
        if not hits:
            self.call_from_thread(self.notify, "No compatible mods found.", severity="warning")

    # ---------- backups ----------
    def restore_backup(self, s):
        name = self.pane("backups").selected()
        if not name:
            return self.notify("Pick a backup first.", severity="warning")
        was_running = s.up()
        try:
            question = f"Replace the world of '{s.name}'?\n\n" + escape(backups.describe_text(backups.describe(s, name)))
        except Fail as e:
            return self.notify(str(e), severity="error")

        def go():
            if was_running:
                servers.stop(s)
            backups.restore(s, name)
            if was_running:
                servers.start(s)
        self.push_screen(Confirm(question, "Restore", danger=True),
                         lambda yes: yes and self.run_task(f"Restore {name}", go,
                                                             after=lambda _: self.pane("backups").refresh_list()))

    # ---------- tabs ----------
    @on(TabbedContent.TabActivated)
    async def tab_changed(self, event):
        if event.pane.id:
            save_setting("dash_tab", event.pane.id.removeprefix("tab-"))
            await self.ensure_pane(event.pane.id.removeprefix("tab-"))     # opened before the background build got to it
        # No opacity fade here: animating a pane's opacity left half-faded backgrounds cached on its buttons (labels vanished).
        if self.snapshot:
            self.update_visible_pane()
        if event.pane.id == "tab-players" and self.server:
            self.load_players(self.server)
        if event.pane.id == "tab-settings" and self.server:
            self.load_settings(self.server)
        if event.pane.id == "tab-backups":
            self.pane("backups").refresh_list()


def run(target=None):
    Dashboard(target, first_run=True).run()
