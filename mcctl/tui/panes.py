"""Dashboard tabs. Each pane gets `load(server)` when the selected server changes and
`update_stats(snapshot)` every refresh. All real work goes through the shared service modules."""
import time
import re
from collections import deque
from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual import on
from textual.css.query import NoMatches
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Select, Sparkline, Static, Switch

from .. import backups, countdown, datapacks, events, metrics, settings_schema
from ..resources import fmt_uptime
from .widgets import (BAD, BLUE, GOOD, MUTED, WARN, CopyLog, CopyTable, HeatSparkline, Rows, Section, Tile, core_strip, lag_color,
                      level_color, put,
                      stacked_bar)

STATE_STYLE = {"running": "green", "starting": "yellow", "stopped": "grey50", "crashed": "red"}


def bar(pct, width=24):
    pct = max(0.0, min(100.0, pct or 0.0))
    filled = int(round(pct / 100 * width))
    color = "green" if pct < 60 else "yellow" if pct < 85 else "red"
    t = Text()
    t.append("█" * filled, style=color)
    t.append("░" * (width - filled), style="grey35")
    t.append(f" {pct:3.0f}%")
    return t


class Pane(Vertical):
    server = None
    ready = True                        # False while the pane finishes its own setup (see setup_when_built)

    def setup_when_built(self, setup):
        """Run `setup`, which looks up this pane's children. A pane built after startup can get its Mount event a moment before
        all its children are there, so ask again shortly until they are. `setup` must be safe to run twice."""
        self.ready = False

        def attempt():
            try:
                setup()
            except NoMatches:
                self.set_timer(0.02, attempt)
                return
            self.ready = True
        attempt()

    def load(self, server):
        self.server = server

    def update_stats(self, snap):
        pass


# ---------------------------------------------------------------- overview
ALERT_WORDS = {"lag": "lagging", "ram": "RAM almost full", "disk": "disk space low"}
EVENT_STYLE = {"start": "green", "stop": "grey62", "restart": "yellow", "kill": "red", "backup": BLUE, "crash": "red",
               "join": "cyan", "leave": "grey62", "alert": "yellow", "vpn": BLUE, "info": "grey70"}


class OverviewPane(Pane):
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Horizontal(classes="tiles"):
                for name in ("status", "players", "perf", "net"):
                    yield Tile(id=f"o-{name}", classes="tile")
            with Horizontal(id="ov-alerts", classes="banner alert"):
                yield Static("", id="ov-alerts-text", classes="banner-text")
            with Horizontal(id="ov-pending", classes="banner"):
                yield Static("", id="ov-pending-text", classes="banner-text")
                yield Button("Cancel countdown", id="ov-cancel", variant="warning")
            with Horizontal(classes="buttons"):
                yield Button("Start", id="ov-start", variant="success")
                yield Button("Stop", id="ov-stop", variant="error")
                yield Button("Restart", id="ov-restart")
                yield Button("Restart in...", id="ov-restart-in")
                yield Button("Backup", id="ov-backup")
            with Horizontal(classes="buttons"):
                yield Button("Copy address", id="ov-address")
                yield Button("Copy invite", id="ov-invite")
                yield Button("Health check", id="ov-doctor")
            yield Static(id="ov-info", classes="card")
            with Section("RECENT EVENTS"):
                yield CopyLog(id="ov-events", wrap=True, markup=False, max_lines=60)
            with Section("ACTIVITY", subtitle="what the dashboard just did"):
                yield CopyLog(id="activity", wrap=True, markup=False, max_lines=500)
            with Section("ALL SERVERS", id="ov-fleet-section"):
                yield CopyTable(id="ov-fleet", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="ov-fleet-buttons", classes="buttons"):
                    yield Button("Start selected", id="ov-f-start", variant="success")
                    yield Button("Stop selected", id="ov-f-stop", variant="error")
                    yield Button("Restart selected", id="ov-f-restart")

    _events_key = None
    _backup_cache = None

    def on_mount(self):
        self.setup_when_built(self._setup)

    def _setup(self):
        t = self.query_one("#ov-fleet", CopyTable)
        if not t.columns:
            t.add_columns("Server", "State", "Players", "TPS", "RAM", "Up", "Last backup", "Internet")

    def load(self, server):
        super().load(server)
        self._events_key = None

    def active_alerts(self, name):
        from .. import agent
        book = agent.load_state().get("alerts", {})
        out = []
        for key, e in book.items():
            kind, _, who = key.partition(":")
            if e.get("active") and e.get("announced") and kind in ALERT_WORDS and who in ("", name):
                out.append(f"'{who}' {ALERT_WORDS[kind]}" if who else ALERT_WORDS[kind])
        if agent.load_state().get("vpn_lost"):
            out.append("internet address lost")
        return out

    def last_backups(self):
        now = time.monotonic()
        if self._backup_cache is None or now - self._backup_cache[0] > 15:
            from ..core import all_servers
            out = {}
            for srv in all_servers():
                newest = next(iter(backups.list_backups(srv)), None)
                out[srv.name] = ("never", None) if newest is None else (
                    datetime.fromtimestamp(newest.stat().st_mtime).strftime("%d %b %H:%M"), backups.status(newest))
            self._backup_cache = (now, out)
        return self._backup_cache[1]

    def update_stats(self, snap):
        s = self.server
        if s is None:
            self.query_one("#ov-info", Static).update("No servers yet. Press n to create one.")
            return
        st = snap.servers.get(s.name)
        if st is None:
            return
        inst = s.inst
        up = st.state in ("running", "starting")
        col = {"running": GOOD, "starting": WARN, "crashed": BAD}.get(st.state, "grey62")
        self.query_one("#o-status", Tile).show(
            "Status", f"* {st.state}", None,
            f"Minecraft {inst.mc_version} | {inst.loader.capitalize()}" + (f" | up {fmt_uptime(st.uptime_s)}" if st.uptime_s else ""), col)
        if st.state == "running" and st.max_players:
            self.query_one("#o-players", Tile).show("Players", f"{len(st.players)}/{st.max_players}",
                                                    len(st.players) / st.max_players * 100,
                                                    ", ".join(st.players) or "nobody online", BLUE)
        else:
            self.query_one("#o-players", Tile).show("Players", "-", None, "", "grey62")
        if st.state == "running" and st.tps is not None:
            tcol = lag_color(st.tps)
            self.query_one("#o-perf", Tile).show(
                "Performance", f"{st.tps:.1f} TPS", st.tps / 20 * 100,
                f"{st.mspt:.1f} ms/tick | CPU {st.cpu_percent or 0:.0f}% | RAM {st.ram_gb or 0:.1f}/{inst.ram_gb} GB", tcol)
        else:
            self.query_one("#o-perf", Tile).show("Performance", "-", None, "start the server to measure", "grey62")
        from .. import network, vpn
        addr = network.display_address(s)                  # what the share mode allows: never the Wi-Fi address in hidden mode
        public = addr["internet"]
        lan = f"Wi-Fi {network.wifi_label(addr)}"
        if public:
            self.query_one("#o-net", Tile).show("Internet", public, None, lan, GOOD)
        elif vpn.enabled() and vpn.forward_server() == s.name:
            self.query_one("#o-net", Tile).show("Internet", "no port yet", None, lan + " | see Network tab", WARN)
        else:
            self.query_one("#o-net", Tile).show("Internet", "LAN only" if not vpn.enabled() else "not this server", None, lan,
                                                "grey62")
        alerts = self.active_alerts(s.name)
        banner = self.query_one("#ov-alerts")
        banner.set_class(bool(alerts), "show")
        if alerts:
            self.query_one("#ov-alerts-text", Static).update("[!] " + " | ".join(alerts) + "   (mc alerts)")
        plan = countdown.pending(s)
        pend = self.query_one("#ov-pending")
        pend.set_class(bool(plan), "show")
        if plan:
            left = max(0, int(plan["at"] - time.time()))
            self.query_one("#ov-pending-text", Static).update(
                f"{plan['action'].capitalize()} in {left // 60}:{left % 60:02d}: players are warned in chat as it gets close")
        info = Rows()
        if st.state == "crashed" and snap.crash.get(s.name):
            info.add_row("Crash", Text(snap.crash[s.name], style="red"))
        info.add_row("Join", f"this Mac: {addr['this_mac']}    Wi-Fi: {network.wifi_label(addr)}")
        flags = [f for f, on in (("autostart", inst.autostart), ("keep-alive", inst.keep_alive),
                                 ("backup on stop", inst.autobackup)) if on]
        info.add_row("Options", ", ".join(flags) or "-")
        info.add_row("Automation", self.automation_text(inst))
        info.add_row("Accounts", "offline allowed (EasyAuth logins)" if s.prop("online-mode") == "false"
                     else "paid Mojang accounts only")
        if snap.vpn:
            info.add_row("Network", Text("A VPN app is on: Wi-Fi friends can't connect", style="yellow"))
        self.query_one("#ov-info", Static).update(info.text())
        self.query_one("#ov-start", Button).disabled = up
        self.query_one("#ov-stop", Button).disabled = not up
        self.query_one("#ov-restart", Button).disabled = not up
        self.query_one("#ov-restart-in", Button).disabled = not up
        self.query_one("#ov-address", Button).disabled = not public
        self.refresh_events(s)
        self.refresh_fleet(snap)

    def refresh_events(self, s):
        rows = events.recent(12, server=s.name)
        key = (s.name, tuple((e["ts"], e["text"]) for e in rows))
        if key == self._events_key:
            return
        self._events_key = key
        log = self.query_one("#ov-events", CopyLog)
        log.clear()
        for e in rows:
            when = datetime.fromtimestamp(e["ts"]).strftime("%d %b %H:%M:%S")
            log.write(Text(f"{when}  {e['kind']:<8} {e['text']}", style=EVENT_STYLE.get(e["kind"], "grey70")))
        if not rows:
            log.write(Text("Nothing yet: starts, stops, backups, crashes, joins and alerts will appear here.", style="grey62"))

    def refresh_fleet(self, snap):
        many = len(snap.servers) > 1
        for w in ("#ov-fleet-section", "#ov-fleet", "#ov-fleet-buttons"):
            self.query_one(w).display = many
        if not many:
            return
        from .. import vpn
        t = self.query_one("#ov-fleet", CopyTable)
        keep = t.cursor_row
        t.clear()
        backups_info = self.last_backups()
        for name, x in snap.servers.items():
            when, status = backups_info.get(name, ("-", None))
            mark = Text(when, style=BAD if status == "corrupt" else "grey70")
            public = vpn.public_address() if vpn.enabled() and vpn.forward_server() == name else ""
            t.add_row(name, Text(x.state, style=STATE_STYLE.get(x.state, "")),
                      f"{len(x.players)}/{x.max_players}" if x.max_players else "-", f"{x.tps:.1f}" if x.tps else "-",
                      f"{x.ram_gb:.1f}/{x.ram_max_gb} GB" if x.ram_gb is not None else "-", fmt_uptime(x.uptime_s),
                      mark, public or "", key=name)
        if t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))

    def selected_server(self):
        t = self.query_one("#ov-fleet", CopyTable)
        if t.row_count == 0:
            return None
        return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value

    def automation_text(self, inst):
        from .. import agent
        if not agent.agent_installed():
            bits = [Text("agent not installed", style="yellow")]
        else:
            age = agent.heartbeat_age()
            bits = [Text("agent running")] if age is not None and age < agent.TICK * 3 else \
                   [Text("agent not checking in", style="yellow")]
        bits += [Text(d) for d in agent.describe(inst.schedules)]
        return Text(" | ").join(bits) if bits else Text("-")

    def add_activity(self, text, style=""):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.query_one("#activity", CopyLog).write(Text(f"{stamp}  {text}", style=style))


# ---------------------------------------------------------------- resources
def rate(kbs):
    return f"{kbs / 1024:.1f} MB/s" if kbs >= 1024 else f"{kbs:.0f} KB/s"


class ResourcesPane(Pane):
    """Two separate panels: the whole Mac, and the Minecraft server(s) on it."""

    RANGE_LABELS = {"5m": "Live | 5 min", "1h": "Last hour", "24h": "Last 24 hours"}

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Horizontal(classes="range-row"):
                yield Label("Graphs:", classes="range-label")
                for key, label in self.RANGE_LABELS.items():
                    yield Button(label, id=f"res-r-{key}", classes="range" + (" active" if key == "5m" else ""))
            with Vertical(classes="panel", id="res-sys-panel"):
                with Horizontal(classes="tiles"):
                    for name in ("cpu", "ram", "disk", "net"):
                        yield Tile(id=f"t-{name}", classes="tile")
                yield Static(id="res-cores")
                with Horizontal(classes="sparks"):
                    for key, title in (("cpu", "CPU"), ("ram", "RAM"), ("net", "Network down")):
                        with Vertical():
                            yield Label(title, id=f"sl-{key}", classes="spark-label")
                            yield HeatSparkline(id=f"spark-{key}", classes="heat")
            with Vertical(classes="panel", id="res-srv-panel"):
                yield Static(id="res-share")
                with Horizontal(classes="tiles"):
                    for name in ("scpu", "sram", "lag", "players"):
                        yield Tile(id=f"t-{name}", classes="tile")
                yield Static(id="res-server")
                with Horizontal(classes="sparks"):
                    for key, title in (("scpu", "Server CPU"), ("sram", "Server RAM"), ("mspt", "Lag")):
                        with Vertical():
                            yield Label(title, id=f"sl-{key}", classes="spark-label")
                            yield HeatSparkline(id=f"spark-{key}", classes="heat")
                yield Label("All servers", classes="sub-title")
                yield CopyTable(id="res-servers", cursor_type="none", zebra_stripes=True)

    range = "5m"
    _hist = None          # (fetched at, range, {key: values}) so 1 h / 24 h graphs read the history file every ~15 s, not every sample

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id or ""
        if not bid.startswith("res-r-"):
            return
        event.stop()
        self.range = bid.removeprefix("res-r-")
        self._hist = None
        for b in self.query("Button.range"):
            b.set_class(b.id == bid, "active")
        if getattr(self, "_snap", None):
            self.update_stats(self._snap)

    def history(self, snap, kind, key, name=None):
        """Values for one graph: the live in-memory samples for 5 min, the agent's on-disk history for 1 h / 24 h."""
        if self.range == "5m":
            live = snap.history.get(key if kind == "sys" else f"{key}:{name}", [])
            return list(live)
        now = time.monotonic()
        if self._hist is None or self._hist[1] != self.range or now - self._hist[0] > 15:
            self._hist = (now, self.range, {})
        cache = self._hist[2]
        k = (kind, key, name)
        if k not in cache:
            cache[k] = metrics.series(kind, key, metrics.RANGES[self.range], name)
        return cache[k]

    def on_mount(self):
        self.setup_when_built(self._setup)

    def _setup(self):
        self.query_one("#res-sys-panel").border_title = "SYSTEM | this Mac (everything running on it)"
        self.query_one("#res-srv-panel").border_title = "MINECRAFT | server usage only"
        t = self.query_one("#res-servers", CopyTable)
        if not t.columns:
            t.add_columns("Server", "State", "CPU", "RAM", "TPS", "ms/tick", "P95", "Players", "Uptime", "World")

    def update_stats(self, snap):
        self._snap = snap
        self.update_system(snap)
        self.update_server(snap)
        t = self.query_one("#res-servers", CopyTable)
        t.clear()
        for name, x in snap.servers.items():
            t.add_row(name, Text(x.state, style=STATE_STYLE.get(x.state, "")),
                      f"{x.cpu_percent:.0f}%" if x.cpu_percent is not None else "-",
                      f"{x.ram_gb:.1f}/{x.ram_max_gb} GB" if x.ram_gb is not None else f"-/{x.ram_max_gb} GB",
                      f"{x.tps:.1f}" if x.tps else "-", f"{x.mspt:.1f}" if x.mspt else "-",
                      f"{x.p95:.1f}" if x.p95 else "-",
                      f"{len(x.players)}/{x.max_players}" if x.max_players else "-",
                      fmt_uptime(x.uptime_s), f"{x.world_mb:.0f} MB" if x.world_mb else "-")

    TILE_TITLES = {"cpu": "CPU", "ram": "RAM", "disk": "Disk", "net": "Network", "scpu": "Server CPU",
                   "sram": "Server RAM", "lag": "Lag", "players": "Players"}

    def tile(self, name, *args, **kw):
        self.query_one(f"#t-{name}", Tile).show(self.TILE_TITLES[name], *args, **kw)

    def spark(self, key, title, values, now="", **look):
        note = now if self.range == "5m" or len(values) > 1 else "no history yet (agent: mc agent install)"
        put(self.query_one(f"#sl-{key}", Label), Text.assemble((title, "bold"), ("  " + note, "grey62")))
        self.query_one(f"#spark-{key}", HeatSparkline).set(values, **look)

    # ---------- the whole Mac ----------
    def update_system(self, snap):
        sysd = snap.system
        used_disk = 100 - sysd.disk_free_gb / sysd.disk_total_gb * 100
        self.tile("cpu", f"{sysd.cpu_percent:.0f}%", sysd.cpu_percent, f"{len(sysd.per_cpu)} cores | load {sysd.load[0]:.1f}",
                  level_color(sysd.cpu_percent, 70, 90))
        self.tile("ram", f"{sysd.ram_used_gb:.1f} GB", sysd.ram_percent,
                  f"of {sysd.ram_total_gb:.0f} GB" + (f" | swap {sysd.swap_used_gb:.1f} GB" if sysd.swap_used_gb > 0.1 else ""),
                  level_color(sysd.ram_percent, 80, 92), note=f"{sysd.ram_percent:.0f}%")
        self.tile("disk", f"{sysd.disk_free_gb:.0f} GB free", used_disk, f"of {sysd.disk_total_gb:.0f} GB",
                  level_color(used_disk, 85, 95), note=f"{used_disk:.0f}% used")
        extra = ""
        if sysd.battery is not None:
            extra = f" | battery {sysd.battery}%" + (", on battery" if sysd.on_battery else "")
        self.tile("net", f"down {rate(sysd.net_down_kbs)}", None,
                  f"up {rate(sysd.net_up_kbs)} | VPN {'on' if snap.vpn else 'off'}{extra}", BLUE)
        per = sysd.per_cpu
        strip = Text.assemble(("Per core  ", "bold"), core_strip(per), (f" busiest {max(per, default=0):.0f}%", "grey62"))
        put(self.query_one("#res-cores", Static), strip)
        self.spark("cpu", "CPU", self.history(snap, "sys", "cpu"), f"{sysd.cpu_percent:.0f}%",
                   top=100, color_fn=lambda v: level_color(v, 70, 90))
        self.spark("ram", "RAM", self.history(snap, "sys", "ram"), f"{sysd.ram_percent:.0f}%",
                   top=100, color_fn=lambda v: level_color(v, 80, 92))
        self.spark("net", "Network down", self.history(snap, "sys", "net"), rate(sysd.net_down_kbs), color=BLUE)

    # ---------- Minecraft only ----------
    def update_server(self, snap):
        sysd = snap.system
        s = self.server
        panel = self.query_one("#res-srv-panel")
        running = [x for x in snap.servers.values() if x.state in ("running", "starting")]
        srv_cpu = sum(x.cpu_percent or 0 for x in running)
        srv_ram = sum(x.ram_gb or 0 for x in running)
        other_cpu = max(0.0, sysd.cpu_percent - srv_cpu)
        other_ram = max(0.0, sysd.ram_used_gb - srv_ram)
        width = max(20, self.query_one("#res-share").size.width - 8)
        allowed = sum(x.ram_max_gb or 0 for x in running)
        share = Text()
        for label, mc, other, total, unit in (("CPU", srv_cpu, other_cpu, 100.0, "%"),
                                              ("RAM", srv_ram, other_ram, sysd.ram_total_gb, " GB")):
            fmt = (lambda v: f"{v:.0f}{unit}") if unit == "%" else (lambda v: f"{v:.1f}{unit}")
            share.append(f"{label}  ", style="bold")
            share.append_text(stacked_bar([(mc / total * 100, GOOD), (other / total * 100, BLUE)], width))
            share.append("\n     ")
            share.append("# ", style=GOOD)
            share.append(f"Minecraft {fmt(mc)}   ")
            share.append("# ", style=BLUE)
            share.append(f"everything else {fmt(other)}   ")
            share.append("# ", style=MUTED)
            share.append(f"free {fmt(max(0.0, total - mc - other))}", style="grey62")
            share.append("\n" if label == "CPU" else "")
            if label == "CPU":
                share.append("\n")
        share.append(f"\n{len(running)} server{'s' if len(running) != 1 else ''} running | {allowed} GB RAM allowed"
                     + (" | over-committed!" if allowed > sysd.ram_total_gb - 4 else ""), style="grey62")
        put(self.query_one("#res-share", Static), share)

        st = snap.servers.get(s.name) if s else None
        if st is None:
            panel.border_title = "MINECRAFT | server usage only"
            return
        panel.border_title = f"MINECRAFT | '{st.name}' (selected server)"
        up = st.state in ("running", "starting")
        cores = max(len(sysd.per_cpu), 1)
        if up and st.cpu_percent is not None:
            self.tile("scpu", f"{st.cpu_percent:.0f}%", st.cpu_percent * 3, f"{st.cpu_percent * cores:.0f}% of one core",
                      level_color(st.cpu_percent, 15, 30), note="of the Mac")
        else:
            self.tile("scpu", "-", None, "not running", "grey62")
        if up and st.ram_gb is not None:
            limit = st.ram_max_gb or 1
            pct = st.ram_gb / limit * 100
            self.tile("sram", f"{st.ram_gb:.1f} GB", pct, f"{st.ram_gb / sysd.ram_total_gb * 100:.0f}% of the Mac",
                      level_color(pct, 85, 95), note=f"of {limit} GB")
        else:
            self.tile("sram", "-", None, f"{st.ram_max_gb} GB allowed", "grey62")
        if up and st.tps is not None:
            col = lag_color(st.tps)
            self.tile("lag", f"{st.tps:.1f} TPS", st.tps / 20 * 100, f"{st.mspt:.1f} ms/tick"
                      + (f" | p95 {st.p95:.1f}" if st.p95 else ""), col, note="20 is perfect")
        else:
            self.tile("lag", "-", None, "start the server to measure", "grey62")
        if up and st.max_players:
            self.tile("players", f"{len(st.players)}/{st.max_players}", len(st.players) / st.max_players * 100,
                      ", ".join(st.players) or "nobody online", BLUE, note="online")
        else:
            self.tile("players", "-", None, "", "grey62")
        info = Text()
        info.append("* ", style=STATE_STYLE.get(st.state, "white"))
        info.append(st.state, style=STATE_STYLE.get(st.state, "white"))
        bits = ([f"up {fmt_uptime(st.uptime_s)}"] if st.uptime_s else []) + \
               ([f"{st.threads} threads"] if st.threads else []) + \
               ([f"world {st.world_mb:.0f} MB"] if st.world_mb else [])
        info.append("   " + " | ".join(bits), style="grey62")
        put(self.query_one("#res-server", Static), info)
        cpu_hist = self.history(snap, "server", "cpu", st.name)
        ram_hist = self.history(snap, "server", "ram", st.name)
        limit = st.ram_max_gb or 1
        self.spark("scpu", "Server CPU", cpu_hist, f"{st.cpu_percent or 0:.0f}%",
                   top=max(30, max(cpu_hist, default=0)), color_fn=lambda v: level_color(v, 15, 30))
        self.spark("sram", "Server RAM", ram_hist, f"{st.ram_gb or 0:.1f} GB",
                   top=limit, color_fn=lambda v: level_color(v / limit * 100, 85, 95))
        self.spark("mspt", "Lag ms/tick", self.history(snap, "server", "mspt", st.name), f"{st.mspt or 0:.1f}",
                   top=50, color_fn=lambda v: level_color(v / 50 * 100, 40, 80))      # 50 ms is the whole tick budget


# ---------------------------------------------------------------- console
class HistoryInput(Input):
    """Input with Up/Down command history."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.history, self.pos = [], 0

    def remember(self, value):
        if value and (not self.history or self.history[-1] != value):
            self.history.append(value)
        self.pos = len(self.history)

    def key_up(self):
        if self.history and self.pos > 0:
            self.pos -= 1
            self.value = self.history[self.pos]
            self.cursor_position = len(self.value)

    def key_down(self):
        if self.pos < len(self.history) - 1:
            self.pos += 1
            self.value = self.history[self.pos]
        else:
            self.pos, self.value = len(self.history), ""
        self.cursor_position = len(self.value)


class ConsoleInput(HistoryInput):
    """The console prompt: Tab accepts the grey completion (or moves on when there isn't one)."""

    BINDINGS = [Binding("tab", "complete", "Complete", show=False, priority=True)]

    def action_complete(self):
        if self._suggestion and self._suggestion != self.value:
            self.action_cursor_right()
        else:
            self.screen.focus_next()


NOISE = ("RCON Listener", "RCON Client")   # one pair per mc command; hide from the console view


def keep_line(line):
    return not any(n in line for n in NOISE)


def line_kind(line):
    """'error' | 'warn' | 'join' | 'chat' | 'info' for one server log line (drives colours and the console filters)."""
    if "ERROR" in line or "Exception" in line or "FATAL" in line:
        return "error"
    if "WARN" in line:
        return "warn"
    if "joined the game" in line or "left the game" in line:
        return "join"
    if "<" in line and ">" in line and "Server thread/INFO" in line:
        return "chat"
    return "info"


KIND_STYLE = {"error": "red", "warn": "yellow", "join": "cyan", "chat": "bright_white", "info": "grey70"}


def style_log_line(line):
    return Text(line, style=KIND_STYLE[line_kind(line)])


FILTERS = {"all": None, "warn": {"error", "warn", "mc"}, "chat": {"chat", "mc"}, "joins": {"join", "mc"}}
FILTER_LABELS = {"all": "All", "warn": "Warnings", "chat": "Chat", "joins": "Joins"}
PRIVATE_WORDS = re.compile(r"password|login|register", re.I)


class ConsolePane(Pane):
    """Live console: server log + our own output, with level chips, a text filter, pause, clear, copy and quick actions."""
    MAX_KEEP = 3000
    BINDINGS = [Binding("ctrl+l", "clear_console", "Clear console", show=False)]

    def compose(self) -> ComposeResult:
        with Horizontal(id="console-bar"):
            for key, label in FILTER_LABELS.items():
                yield Button(label, id=f"cf-{key}", classes="chip" + (" active" if key == "all" else ""))
            yield Input(placeholder="filter text...", id="console-filter")
            yield Button("Pause", id="cf-pause", classes="chip")
            yield Button("Clear", id="cf-clear", classes="chip")
            yield Button("Copy", id="cf-copy", classes="chip")
        yield CopyLog(id="console-log", wrap=True, markup=False, max_lines=self.MAX_KEEP)
        with Horizontal(id="console-quick"):
            yield Label("Quick:", classes="range-label")
            for bid, label in (("save", "Save world"), ("day", "Day"), ("clear", "Clear weather"), ("list", "Who's online")):
                yield Button(label, id=f"cq-{bid}", classes="chip")
        yield ConsoleInput(placeholder="server command + Enter  |  /start /stop /backup /game ... = mc commands  |  "
                                       "Tab completes  |  /help", id="console-input")

    def on_mount(self):
        self.items = deque(maxlen=self.MAX_KEEP)     # (Text, kind, plain): everything seen, so a filter change can re-render
        self.mode, self.text_filter, self.paused, self.pos = "all", "", False, 0
        from ..core import setting
        self.query_one("#console-input", ConsoleInput).history = [h for h in (setting("console_history", []) or [])
                                                                   if isinstance(h, str)][-100:]
        self.query_one("#console-input", ConsoleInput).pos = len(self.query_one("#console-input", ConsoleInput).history)

    # ---------- storing and showing lines ----------
    def visible(self, kind, plain):
        allowed = FILTERS[self.mode]
        if allowed is not None and kind not in allowed:
            return False
        return kind == "mc" or not self.text_filter or self.text_filter in plain.lower()

    def add(self, text, kind):
        self.items.append((text, kind, text.plain))
        if self.visible(kind, text.plain):
            self.query_one("#console-log", CopyLog).write(text)

    def rerender(self):
        log = self.query_one("#console-log", CopyLog)
        log.clear()
        for text, kind, plain in self.items:
            if self.visible(kind, plain):
                log.write(text)

    def load(self, server):
        super().load(server)
        self.items.clear()
        self.query_one("#console-log", CopyLog).clear()
        self.pos = 0
        if server and server.log_path.exists():
            data = server.log_path.read_bytes()
            lines = [ln for ln in data.decode(errors="replace").splitlines() if keep_line(ln)]
            for line in lines[-300:]:
                self.add(style_log_line(line), line_kind(line))
            self.pos = len(data)

    def poll(self):
        """Append new log lines (called on a timer)."""
        s = self.server
        if not s or not s.log_path.exists():
            return
        size = s.log_path.stat().st_size
        if size < self.pos:      # new log file after a restart
            self.pos = 0
        if size == self.pos:
            return
        with open(s.log_path, "rb") as f:
            f.seek(self.pos)
            chunk = f.read()
        self.pos = size
        for line in chunk.decode(errors="replace").splitlines():
            if keep_line(line):
                self.add(style_log_line(line), line_kind(line))

    def write_line(self, text, style="magenta"):
        self.add(Text(text, style=style), "mc")

    def show_reply(self, command, reply):
        self.add(Text(f"> {command}", style="bold magenta"), "mc")
        for line in (reply or "(no reply)").splitlines():
            self.add(Text(f"  {line}", style="magenta"), "mc")

    # ---------- toolbar ----------
    def action_clear_console(self):
        self.items.clear()
        self.query_one("#console-log", CopyLog).clear()

    @on(Input.Changed, "#console-filter")
    def filter_changed(self, event):
        event.stop()
        self.text_filter = event.value.strip().lower()
        self.rerender()

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id or ""
        if not (bid.startswith("cf-") or bid.startswith("cq-")):
            return
        event.stop()
        log = self.query_one("#console-log", CopyLog)
        if bid.startswith("cf-") and bid[3:] in FILTERS:
            self.mode = bid[3:]
            for b in self.query("#console-bar Button"):
                if b.id and b.id[3:] in FILTERS:
                    b.set_class(b.id == bid, "active")
            self.rerender()
        elif bid == "cf-pause":
            self.paused = not self.paused
            log.auto_scroll = not self.paused
            event.button.label = "Resume" if self.paused else "Pause"
            event.button.set_class(self.paused, "active")
            if not self.paused:
                log.scroll_end(animate=False)
        elif bid == "cf-clear":
            self.action_clear_console()
        elif bid == "cf-copy":
            text = log.all_text()
            if text.strip():
                self.app.copy_to_clipboard(text)
                self.app.notify(f"{len(text.splitlines())} lines copied", title="Console copied", timeout=3)
            else:
                self.app.notify("Nothing to copy.", severity="warning")
        elif bid.startswith("cq-") and self.server:
            from .. import game
            s, app = self.server, self.app
            {"save": lambda: app.run_task("Save world", game.save, s),
             "day": lambda: app.run_task("Time: day", game.set_time, s, "day"),
             "clear": lambda: app.run_task("Weather: clear", game.set_weather, s, "clear"),
             "list": lambda: app.send_console(s, "list")}[bid[3:]]()


# ---------------------------------------------------------------- players
class PlayersPane(Pane):
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Section("PLAYERS", subtitle="* online | o offline"):
                yield CopyTable(id="pl-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="pl-hint", classes="hint")
            with Section("SELECTED PLAYER", subtitle="pick a row above, or type a name"):
                with Horizontal(classes="inline-form"):
                    yield Input(placeholder="Player name (or pick a row above)", id="pl-name")
                    yield Input(placeholder="Reason (kick/ban, optional)", id="pl-reason")
                with Horizontal(classes="buttons"):
                    yield Button("Whitelist +", id="pl-wl-add", variant="success")
                    yield Button("Whitelist -", id="pl-wl-remove")
                    yield Button("Make op", id="pl-op")
                    yield Button("Remove op", id="pl-deop")
                    yield Button("Kick", id="pl-kick", variant="warning")
                    yield Button("Ban", id="pl-ban", variant="error")
                    yield Button("Unban", id="pl-unban")
                    yield Button("Reset password", id="pl-pass")
                with Horizontal(classes="buttons"):
                    yield Button("Details", id="pl-details", variant="primary")
                    yield Button("Latency", id="pl-latency")
                    yield Button("Bans...", id="pl-bans")
                    yield Button("Whitelist everyone online", id="pl-wl-online")
            with Section("CHAT", subtitle="talk to players from here"):
                with Horizontal(classes="inline-form"):
                    yield Input(placeholder="Broadcast to everyone in chat...", id="pl-say")
                    yield Button("Broadcast", id="pl-say-go")
                with Horizontal(classes="inline-form"):
                    yield Input(placeholder="Private message to the selected player...", id="pl-msg")
                    yield Button("Message", id="pl-msg-go")

    def on_mount(self):
        self.setup_when_built(self._setup)

    def _setup(self):
        t = self.query_one("#pl-table", CopyTable)
        if not t.columns:
            t.add_columns("", "Player", "Op", "Whitelisted", "Banned", "Last left")

    def fill(self, roster):
        t = self.query_one("#pl-table", CopyTable)
        t.clear()
        for n, r in roster.items():
            t.add_row(Text("*", style="green") if r["online"] else Text("o", style="grey50"), n,
                      "yes" if r["op"] else "", "yes" if r["whitelisted"] else "",
                      Text("yes", style="red") if r["banned"] else "", r["last_left"] or "", key=n)
        hint = "Online players are marked *." if roster else "No players yet. Add one by name below."
        if self.server and self.server.prop("white-list") == "true":
            hint += " Whitelist is ON: only listed players can join."
        self.query_one("#pl-hint", Static).update(hint)

    def on_data_table_row_highlighted(self, event: CopyTable.RowHighlighted):
        if event.data_table.id == "pl-table" and event.row_key is not None:
            self.query_one("#pl-name", Input).value = str(event.row_key.value)


# ---------------------------------------------------------------- settings
def wid(key):
    return "set-" + key.replace(".", "_")


class SettingsPane(Pane):
    def compose(self) -> ComposeResult:
        with Horizontal(id="restart-banner"):
            yield Static("Some changes need a restart to take effect.", classes="banner-text")
            yield Button("Restart now", id="set-restart", variant="warning")
        yield VerticalScroll(id="settings-form")

    def load(self, server):
        super().load(server)
        self.query_one("#restart-banner").remove_class("show")
        form = self.query_one("#settings-form", VerticalScroll)
        form.remove_children()
        if server is None:
            return
        groups = {}
        for s in settings_schema.props_for(server):
            groups.setdefault(s.section, []).append(s)
        groups["mc options"] = settings_schema.INSTANCE
        groups["Game rules (applied instantly; server must be running)"] = settings_schema.GAMERULES
        titles = {"mc options": "MC OPTIONS | how this server runs"}
        cards = [Section("NAMES", Static(Text.assemble("Server  ", (server.name, "bold"), "     World  ", (server.world_name, "bold"),
                                                    "     Software  ", (f"{server.loader_info.label} {server.version}", "bold"),
                                                    ("\nThe name in the multiplayer list is 'Server name (MOTD)' below. "
                                                     "Renaming needs the server stopped (F2 renames the server).", "grey50")),
                                      classes="hint"),
                         Horizontal(Button("Rename server...", id="set-rename-server"),
                                    Button("Rename world...", id="set-rename-world"), classes="buttons"))]
        for section, items in groups.items():
            cards.append(Section(titles.get(section, section.upper()), *[self.row(st) for st in items]))
        sched = server.inst.schedules
        from .. import agent
        cards.append(Section("AUTOMATION | background agent",
                             self.schedule_row("sched-backup", "Automatic backup every", "e.g. 6h, 30m, 1d, or off",
                                               agent.fmt_secs(sched["backup_every"]) if sched.get("backup_every") else ""),
                             self.schedule_row("sched-restart", "Daily restart at", "e.g. 05:00, or off",
                                               sched.get("restart_at", ""))))
        form.mount_all([Static("Switches and lists save immediately. For text/number boxes, press Enter to save.",
                               classes="hint"), *cards])

    def schedule_row(self, widget_id, label, placeholder, value):
        control = Input(id=widget_id, classes="setting-input", placeholder=placeholder, value=value)
        lbl = Label(Text.assemble(label, ("\n" + "needs: mc agent install", "grey50")), classes="setting-label")
        box = Horizontal(control, classes="setting-control")
        return Horizontal(lbl, box, Label("", classes="setting-tag"), classes="setting-row")

    def row(self, st):
        if st.kind == "bool":
            control = Switch(id=wid(st.key))
        elif st.kind == "choice":
            control = Select([(c.capitalize(), c) for c in st.choices], allow_blank=True, id=wid(st.key))
        else:
            control = Input(id=wid(st.key), classes="setting-input",
                            placeholder=f"{st.min}-{st.max}" if st.kind == "int" else "")
        live = st.live or st in settings_schema.GAMERULES
        tag = "live" if live else "restart" if st in settings_schema.PROPS else ""
        label = Label(Text.assemble(st.label, ("\n" + st.key, "grey50")), classes="setting-label")
        box = Horizontal(control, classes="setting-control")
        row = Horizontal(label, box, Label(tag, classes=f"setting-tag {tag}"), classes="setting-row")
        row.tooltip = st.help or None
        return row

    def set_values(self, values, attempt=0):
        """values: {key: str|None} loaded in a worker thread."""
        from textual.css.query import NoMatches
        try:
            self._apply_values(values)
        except NoMatches:        # the form was just rebuilt and a dropdown hasn't finished mounting yet: try again shortly
            if attempt < 10:
                self.set_timer(0.15, lambda: self.set_values(values, attempt + 1))

    def _apply_values(self, values):
        for key, v in values.items():
            try:
                w = self.query_one(f"#{wid(key)}")
            except Exception:
                continue
            if isinstance(w, Switch):
                with w.prevent(Switch.Changed):
                    w.value = v == "true"
                w.disabled = v is None
            elif isinstance(w, Select):
                with w.prevent(Select.Changed):
                    w.value = v if v else Select.NULL
            elif isinstance(w, Input):
                w.value = v or ""
                w.disabled = v is None and key in {g.key for g in settings_schema.GAMERULES}

    def needs_restart(self):
        self.query_one("#restart-banner").add_class("show")


# ---------------------------------------------------------------- mods
class ModsPane(Pane):
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Section("INSTALLED MODS", id="mods-sec-installed"):
                yield CopyTable(id="mods-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="buttons"):
                    yield Button("Check updates", id="mods-check")
                    yield Button("Update all", id="mods-update", variant="primary")
                    yield Button("Pin / unpin", id="mods-pin")
                    yield Button("Remove", id="mods-remove", variant="error")
                    yield Button("Open config", id="mods-config")
                with Horizontal(classes="buttons"):
                    yield Button("Find problems", id="mods-doctor")
                    yield Button("Fix problems", id="mods-fix")
                    yield Button("Explain (local AI)", id="mods-explain")
                hint = Static("", id="mods-hint", classes="hint")
                hint.display = False
                yield hint
            with Section("ADD MODS | online (Modrinth)", id="mods-sec-online", kind="online",
                         subtitle="only ones that work on this server are shown"):
                with Horizontal(classes="inline-form"):
                    yield Input(placeholder="Search, e.g. 'chunk pregen', 'backups', 'voice chat'", id="mods-query")
                    yield Button("Search", id="mods-search")
                    yield Button("Install selected", id="mods-install", variant="success")
                yield CopyTable(id="mods-results", cursor_type="row", zebra_stripes=True)
            with Section("ADD MODS | from a file", id="mods-sec-file", kind="file",
                         subtitle="CurseForge, GitHub or your own build"):
                with Horizontal(classes="buttons"):
                    yield Button("Add mod .jar...", id="mods-add-jar")
                yield Static("Pick a .jar you downloaded. It must be built for this server's software and Minecraft version; "
                             "mc can't auto-update files added this way.", classes="hint")
            with Section("INSTALLED DATAPACKS", id="mods-sec-dp", subtitle="work on every server, vanilla too"):
                yield CopyTable(id="dp-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="buttons"):
                    yield Button("Update packs", id="mods-dp-update", variant="primary")
                    yield Button("Remove", id="mods-dp-remove", variant="error")
                yield Static("Datapacks load into the world: a running server runs /reload, a stopped one picks them up on start.",
                             classes="hint")
            with Section("ADD DATAPACKS | online (Modrinth)", id="mods-sec-dp-online", kind="online"):
                with Horizontal(classes="inline-form"):
                    yield Input(placeholder="Find datapacks on Modrinth, e.g. 'villager', 'sleep', 'graves'", id="dp-query")
                    yield Button("Search", id="mods-dp-search")
                    yield Button("Install selected", id="mods-dp-install", variant="success")
                yield CopyTable(id="dp-results", cursor_type="row", zebra_stripes=True)
            with Section("ADD DATAPACKS | from a file", id="mods-sec-dp-file", kind="file",
                         subtitle="a .zip, or a folder with pack.mcmeta"):
                with Horizontal(classes="buttons"):
                    yield Button("Add from file...", id="mods-dp-add")

    def on_mount(self):
        self.setup_when_built(self._setup)

    def _setup(self):
        for table_id, columns in (("#mods-table", ("Mod", "Version", "Who needs it", "Pinned", "Update")),
                                  ("#mods-results", ("Mod", "Downloads", "Who needs it", "Description")),
                                  ("#dp-table", ("Datapack", "Version", "Source")),
                                  ("#dp-results", ("Datapack", "Downloads", "Description"))):
            t = self.query_one(table_id, CopyTable)
            if not t.columns:
                t.add_columns(*columns)

    def load(self, server):
        super().load(server)
        word = "PLUGINS" if server and server.loader_info.content == "plugins" else "MODS"
        self.query_one("#mods-sec-installed").border_title = f"INSTALLED {word}"
        self.query_one("#mods-sec-online").border_title = f"ADD {word} | online (Modrinth)"
        self.query_one("#mods-sec-file").border_title = f"ADD {word} | from a file"
        self.fill_datapacks()

    def fill(self, entries, manual, updates=None):
        t = self.query_one("#mods-table", CopyTable)
        t.clear()
        updates = updates or {}
        for e in sorted(entries.values(), key=lambda e: e.slug):
            new = updates.get(e.project_id)
            t.add_row(e.title, e.version_number,
                      Text("players too", style="yellow") if e.players_need_it else Text("server only", style="green"),
                      "yes" if e.pinned else "", Text(f"-> {new['version_number']}", style="cyan") if new else "",
                      key=e.slug)
        for f in manual:
            t.add_row(f, Text("manual jar", style="grey50"), "", "", "", key=f)
        hint = self.query_one("#mods-hint", Static)
        text = "Changes apply after a restart." if self.server and self.server.up() else ""
        hint.update(text)
        hint.display = bool(text)                                   # no empty gap at the bottom of the card

    def fill_results(self, hits):
        t = self.query_one("#mods-results", CopyTable)
        t.clear()
        for h in hits:
            t.add_row(h["title"], f"{h['downloads']:,}",
                      Text("players too", style="yellow") if h.get("client_side") == "required"
                      else Text("server only", style="green"),
                      " ".join(h["description"].split())[:80], key=h["slug"])

    def fill_datapacks(self):
        t = self.query_one("#dp-table", CopyTable)
        t.clear()
        if self.server is None:
            return
        for p in datapacks.installed(self.server):
            t.add_row(p["title"], p["version"], Text(p["source"], style="cyan" if p["source"] == "Modrinth" else "grey62"),
                      key=p["name"])

    def fill_dp_results(self, hits):
        t = self.query_one("#dp-results", CopyTable)
        t.clear()
        for h in hits:
            t.add_row(h["title"], f"{h['downloads']:,}", " ".join(h["description"].split())[:90], key=h["slug"])

    def selected(self, table_id):
        t = self.query_one(f"#{table_id}", CopyTable)
        if t.row_count == 0:
            return None
        return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value


# ---------------------------------------------------------------- backups
class BackupsPane(Pane):
    MARKS = {"verified": ("[ok] verified", GOOD), "corrupt": ("[x] corrupt", BAD), "unverified": ("? not checked", "grey62")}

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Section("BACKUPS", subtitle="newest first"):
                with Horizontal(classes="inline-form"):
                    yield Label("Back up automatically on every stop", classes="setting-label")
                    yield Switch(id="bk-auto")
                yield Static(id="bk-info", classes="card")
                yield CopyTable(id="bk-table", cursor_type="row", zebra_stripes=True)
                yield Label("Backup size (MB), oldest to newest", id="bk-trend-label", classes="spark-label")
                yield Sparkline([], id="bk-trend")
            with Section("ACTIONS"):
                with Horizontal(classes="buttons"):
                    yield Button("Backup now", id="bk-create", variant="primary")
                    yield Button("Restore selected", id="bk-restore", variant="warning")
                    yield Button("Verify all", id="bk-verify")
                    yield Button("Copy offsite", id="bk-push")
                    yield Button("Export world to Downloads", id="bk-export")
                    yield Button("Delete old (keep 10)", id="bk-prune")
                yield Static("Restoring keeps your current world in backups/ too, so nothing is lost.", classes="hint")

    def on_mount(self):
        self.setup_when_built(self._setup)

    def _setup(self):
        t = self.query_one("#bk-table", CopyTable)
        if not t.columns:
            t.add_columns("#", "Backup", "When", "Size", "Check")

    def load(self, server):
        super().load(server)
        self.refresh_list()

    def next_backup_text(self, s):
        from .. import agent
        every = s.inst.schedules.get("backup_every")
        if not every:
            return "no automatic backups (Settings -> Automation, then install the agent)"
        last = agent.load_state().get("servers", {}).get(s.name, {}).get("last_backup")
        if not last:
            return f"every {agent.fmt_secs(every)}, the first one runs soon (needs the agent: mc agent install)"
        left = max(0, int(last + every - time.time()))
        return f"every {agent.fmt_secs(every)}, next in {left // 3600}h {left % 3600 // 60:02d}m"

    def refresh_list(self):
        t = self.query_one("#bk-table", CopyTable)
        t.clear()
        s = self.server
        if not s:
            return
        sw = self.query_one("#bk-auto", Switch)
        with sw.prevent(Switch.Changed):
            sw.value = s.inst.autobackup
        items = backups.list_backups(s)
        for i, p in enumerate(items, 1):
            label, color = self.MARKS[backups.status(p)]
            t.add_row(str(i), p.name, datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b %Y %H:%M"),
                      f"{backups.size_mb(p):.1f} MB", Text(label, style=color), key=p.name)
        inst = s.inst
        info = Rows()
        info.add_row("Schedule", self.next_backup_text(s))
        keep = f"newest {inst.backup_keep}" + (f", up to {inst.backup_keep_days} days" if inst.backup_keep_days else "") \
            + (f", under {inst.backup_max_gb} GB" if inst.backup_max_gb else "")
        info.add_row("Keeping", keep)
        info.add_row("Offsite", f"copied to '{inst.backup_push_to}' after each scheduled backup" if inst.backup_push_to
                     else "not copied anywhere else (Settings -> Copy backups to remote)")
        bad = sum(backups.status(p) == "corrupt" for p in items)
        info.add_row("Health", Text(f"{bad} corrupt backup(s): make a new one" if bad else
                                    f"{sum(backups.status(p) == 'verified' for p in items)} of {len(items)} verified",
                                    style=BAD if bad else "grey70"))
        self.query_one("#bk-info", Static).update(info.text())
        sizes = [backups.size_mb(p) for p in reversed(items[:30])]
        self.query_one("#bk-trend", Sparkline).data = sizes or [0]
        for w in ("#bk-trend", "#bk-trend-label"):
            self.query_one(w).display = len(sizes) > 1         # a graph of one backup says nothing

    def selected(self):
        t = self.query_one("#bk-table", CopyTable)
        if t.row_count == 0:
            return None
        return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
