"""Status bar plus the Game and Network tabs: everything `mc game ...` and `mc vpn ...` do, as buttons and forms."""
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual import work
from textual.widgets import Button, Input, Select, Static

from .. import game, network, share, speedtest, vpn
from ..resources import fmt_uptime
from .modals import AskText, TextResult
from .panes import STATE_STYLE, Pane
from .widgets import CopyLog, Section, put

MODES = [(m.capitalize(), m) for m in game.MODES]


# ---------------------------------------------------------------- status bar (always visible)
class StatusBar(Static):
    """One line that stays on screen on every tab: state, players, lag, memory, uptime, internet address and VPN."""

    def show(self, server, snap):
        if server is None:
            return put(self, "No servers yet. Press n to create one.")
        st = snap.servers.get(server.name) if snap else None
        inst = server.inst
        t = Text()
        state = st.state if st else server.state()
        t.append("* ", style=STATE_STYLE.get(state, "white"))
        t.append(f"{server.name} ", style="bold")
        t.append(state.upper(), style=STATE_STYLE.get(state, "white"))
        t.append(f"  {server.label} | {inst.runtime} | :{server.port}", style="grey62")
        if st and state == "running":
            t.append(f"  │  {len(st.players)}/{st.max_players or '?'} players", style="cyan")
            if st.tps is not None:
                t.append(f"  {st.tps:.0f} TPS", style="green" if st.tps >= 18 else "yellow" if st.tps >= 12 else "red")
                t.append(f" ({st.mspt:.1f} ms)", style="grey62")
            if st.ram_gb is not None:
                t.append(f"  │  RAM {st.ram_gb:.1f}/{st.ram_max_gb}G", style="white")
            if st.cpu_percent is not None:
                t.append(f"  CPU {st.cpu_percent:.0f}%", style="white")
            if st.uptime_s:
                t.append(f"  │  up {fmt_uptime(st.uptime_s)}", style="grey62")
        net = getattr(snap, "net", None) or {}
        for i, (text, style) in enumerate(net_segments(net, bool(snap and snap.vpn))):
            t.append("  │  " if i == 0 else "  ", style="grey62")
            t.append(text, style=style)
        put(self, t)


def net_segments(net, mac_vpn):
    """The network end of the status bar as [(text, style)]: how friends find the server (the share mode and the address it
    allows), the sign-in proxy, and the Mac-wide VPN when it matters."""
    mode, public = net.get("share_mode"), net.get("public")
    docker = net.get("tunnel") == "docker"
    if docker and public:
        internet = (f"internet * {public}", "green")
    elif docker and net.get("pending"):
        internet = ("internet o checking...", "grey62")             # Docker hasn't answered yet: don't call it down
    elif docker:
        internet = ("internet o " + ("no port yet" if net.get("tunnel_ok") else "VPN down"), "yellow")
    else:
        internet = ("LAN only", "grey50")
    if mode == "hidden":
        out = [("hidden", "bold")]
        out.append(internet if docker else ("no internet address: nobody can join (set up the VPN)", "bold red"))
    elif mode == "local":
        out = [(f"local {net.get('share_host') or 'name not ready'}", "cyan")]
        if docker:
            out.append(internet)
    else:
        out = [internet, ("share: not set", "grey62")]
    if docker:
        out.append(("proxy up", "green") if net.get("proxy_up") else ("proxy down", "yellow"))
    if mac_vpn:
        if net.get("proton_lan") is False and mode != "hidden":       # hidden has no Wi-Fi friends to block
            out.append(("Mac VPN on: LAN blocked", "bold red"))
        else:
            out.append(("Mac VPN on", "yellow"))
    return out


# ---------------------------------------------------------------- game
class GamePane(Pane):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.players = ()
        self.feed_size = -1

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(id="gm-info", classes="card")
            with Section("WORLD", subtitle="time, weather, border"):
                with Horizontal(classes="buttons"):
                    for label, bid in (("Day", "day"), ("Noon", "noon"), ("Night", "night"), ("Midnight", "midnight")):
                        yield Button(label, id=f"gm-time-{bid}")
                    for label, bid in (("Clear", "clear"), ("Rain", "rain"), ("Thunder", "thunder")):
                        yield Button(label, id=f"gm-weather-{bid}")
                    yield Button("Save world", id="gm-save")
                with Horizontal(classes="inline-form"):
                    yield Input(placeholder="world border size (blocks)", id="gm-border", type="integer")
                    yield Button("Set border", id="gm-border-set")
                    yield Button("Show border", id="gm-border-show")
                    yield Button("Seed", id="gm-seed")
            with Section("PLAYERS", subtitle="game mode, teleport, give items"):
                with Horizontal(classes="inline-form"):
                    yield Select([], prompt="Player", id="gm-player", allow_blank=True)
                    yield Select(MODES, value="survival", allow_blank=False, id="gm-mode")
                    yield Button("Set mode", id="gm-mode-set")
                    yield Button("Where?", id="gm-where")
                with Horizontal(classes="inline-form"):
                    yield Select([], prompt="...teleport to", id="gm-target", allow_blank=True)
                    yield Button("Teleport", id="gm-tp")
                    yield Input(placeholder="x y z", id="gm-coords")
                    yield Button("Teleport to coordinates", id="gm-tp-xyz")
                with Horizontal(classes="inline-form"):
                    yield Input(placeholder="item, e.g. diamond or minecraft:diamond_sword", id="gm-item")
                    yield Input(value="1", id="gm-count", type="integer")
                    yield Button("Give", id="gm-give")
            with Section("RESULT"):
                yield CopyLog(id="gm-out", wrap=True, markup=False, max_lines=200)
            with Section("RECENT ACTIVITY", subtitle="joins, chat, advancements, deaths"):
                yield CopyLog(id="gm-feed", wrap=True, markup=False, max_lines=200)

    def load(self, server):
        super().load(server)
        self.feed_size = -1
        self.refresh_info()

    # ---------- refresh ----------
    def update_stats(self, snap):
        s = self.server
        if s is None:
            return
        st = snap.servers.get(s.name)
        names = tuple(st.players) if st and st.state == "running" else ()
        if names != self.players:
            self.players = names
            options = [(n, n) for n in names]
            for sel in ("#gm-player", "#gm-target"):
                widget = self.query_one(sel, Select)
                widget.set_options(options)
        size = s.log_size()
        if size != self.feed_size:
            self.feed_size = size
            self.refresh_feed()

    @work(thread=True, exclusive=True, group="game-info")
    def refresh_info(self):
        s = self.server
        if s is None:
            return
        d = game.overview(s)           # asks the server over RCON, so it runs off the UI thread
        lines = [f"[b]{s.name}[/b]  {d['version']}  |  {d['difficulty']}  |  default mode {d['mode']}  |  "
                 f"{d['world_mb']} MB on disk"]
        lines.append(f"Seed {d['seed']}  |  day {d['day']}  |  {d['border']}" if "seed" in d
                     else "[grey62]Start the server to see seed, day and border.[/]")
        self.app.call_from_thread(self.query_one("#gm-info", Static).update, "\n".join(lines))

    def refresh_feed(self):
        log = self.query_one("#gm-feed", CopyLog)
        log.clear()
        colors = {"join": "green", "leave": "grey50", "chat": "white", "advancement": "yellow", "death": "red"}
        for when, kind, text in game.feed(self.server, 60):
            log.write(Text(f"{when}  {text}", style=colors[kind]))

    # ---------- actions ----------
    def show(self, label):
        def after(result):
            text = result if isinstance(result, str) else str(result) if result is not None else "done"
            log = self.query_one("#gm-out", CopyLog)
            log.write(Text(f"> {label}", style="bold magenta"))
            for line in (text or "(no reply)").splitlines():
                log.write(Text(f"  {line}", style="magenta"))
            self.refresh_info()
        return after

    def run(self, label, fn, *args):
        self.app.run_task(label, fn, self.server, *args, after=self.show(label))

    def chosen(self, selector):
        value = self.query_one(selector, Select).value
        if value is Select.NULL or value is None:
            self.app.notify("Pick a player first (they must be online).", severity="warning")
            return None
        return value

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id or ""
        if not bid.startswith("gm-") or self.server is None:
            return
        event.stop()
        if bid.startswith("gm-time-"):
            return self.run(f"Time: {bid[8:]}", game.set_time, bid[8:])
        if bid.startswith("gm-weather-"):
            return self.run(f"Weather: {bid[11:]}", game.set_weather, bid[11:])
        if bid == "gm-save":
            return self.run("Save world", game.save)
        if bid == "gm-seed":
            return self.run("Seed", game.seed)
        if bid == "gm-border-show":
            return self.run("World border", game.border)
        if bid == "gm-border-set":
            size = self.query_one("#gm-border", Input).value.strip()
            return self.run(f"Border {size}", game.border, size or None)
        player = None
        if bid in ("gm-mode-set", "gm-where", "gm-tp", "gm-tp-xyz", "gm-give"):
            player = self.chosen("#gm-player")
            if player is None:
                return
        if bid == "gm-mode-set":
            self.run(f"{player} -> {self.query_one('#gm-mode', Select).value}", game.gamemode,
                     self.query_one("#gm-mode", Select).value, player)
        elif bid == "gm-where":
            self.app.run_task(f"Where is {player}", game.where, self.server, player, after=self.show_where(player))
        elif bid == "gm-tp":
            target = self.chosen("#gm-target")
            if target:
                self.run(f"Teleport {player} -> {target}", game.teleport, player, (target,))
        elif bid == "gm-tp-xyz":
            coords = tuple(self.query_one("#gm-coords", Input).value.replace(",", " ").split())
            self.run(f"Teleport {player} -> {' '.join(coords)}", game.teleport, player, coords)
        elif bid == "gm-give":
            item = self.query_one("#gm-item", Input).value.strip()
            count = self.query_one("#gm-count", Input).value.strip() or "1"
            self.run(f"Give {player} {count} x {item}", game.give, player, item, count)

    def show_where(self, player):
        def fmt(pos):
            if not pos:
                return f"{player} isn't online."
            return f"{player}: {pos['x']:.0f}, {pos['y']:.0f}, {pos['z']:.0f}  ({pos['dimension']})"
        after = self.show(f"Where is {player}")
        return lambda pos: after(fmt(pos))


# ---------------------------------------------------------------- network
class NetworkPane(Pane):
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Section("PLAY TOGETHER", subtitle="how friends find your server"):
                yield Static(id="net-share", classes="card")
                with Horizontal(classes="buttons"):
                    yield Button("Hidden", id="net-share-hidden")
                    yield Button("Local name", id="net-share-local")
                    yield Button("Copy invite", id="net-share-copy", variant="primary")
                yield Static("Hidden: friends join only through the Docker VPN address. It adds roughly 60-120 ms, the address changes "
                             "when the VPN reconnects, every player appears from the forwarder's address (IP bans don't apply) and voice "
                             "chat can't work. Local name: friends type a name instead of the number; that hides it from casual view "
                             "only, anyone on the Wi-Fi can still find this Mac. A change applies when the server next starts.",
                             classes="hint")
            yield Static(id="net-card", classes="card")
            with Section("INTERNET PLAY", subtitle="Docker VPN + port forwarding"):
                with Horizontal(classes="buttons"):
                    yield Button("Start VPN", id="net-start", variant="success")
                    yield Button("Stop", id="net-stop", variant="error")
                    yield Button("Set up VPN key...", id="net-setup")
                    yield Button("Exit city...", id="net-city")
                    yield Button("Copy internet address", id="net-address")
                    yield Button("Logs...", id="net-logs")
                yield Static("Needs a paid Proton plan: make the WireGuard config with 'NAT-PMP (Port Forwarding)' ticked. Only the "
                             "Docker VPN is on the VPN, so the Mac stays off it and LAN play keeps working. mc never changes your "
                             "VPN app's settings. The address changes whenever the VPN reconnects. Exit city: a nearer city means less lag; "
                             "compare with Test latency.", classes="hint")
            with Section("SIGN-IN PROXY", subtitle="Microsoft sign-in without a Mac-wide VPN"):
                with Horizontal(classes="buttons"):
                    yield Button("Show launcher settings", id="net-proxy")
                    yield Button("Copy password", id="net-proxy-copy")
                    yield Button("Test sign-in", id="net-auth", variant="primary")
                yield Static("If your Wi-Fi blocks Minecraft/Microsoft sign-in, sign in through the Docker VPN instead of turning on a "
                             "Mac-wide VPN (which would block friends on the Wi-Fi). Enter the settings in Prism Launcher: Settings -> "
                             "Proxy. Only the launcher's sign-in uses it; the game still connects to servers directly.",
                             classes="hint")
            with Section("CHECKS"):
                with Horizontal(classes="buttons"):
                    yield Button("Health check", id="net-doctor")
                    yield Button("Copy invite", id="net-invite")
            with Section("SPEED TEST", subtitle="latency to the server and to each connected player"):
                with Horizontal(classes="buttons"):
                    yield Button("Test latency", id="net-speed", variant="primary")
                    yield Button("Test internet speed", id="net-speed-internet")
                yield CopyLog(id="net-speed-out", wrap=True, markup=False, max_lines=80)
                yield Static("Latency pings each address people join by and reads the connection time of every player online. "
                             "The internet test takes about 20 seconds and uses bandwidth (macOS only).", classes="hint")

    def update_stats(self, snap):
        net = getattr(snap, "net", None)
        s = self.server
        if not net or s is None:
            return
        t = Text()
        t.append("Mac VPN app: ", style="bold")
        if snap.vpn:
            t.append("ON", style="yellow")
            if net.get("proton_lan") is False:
                t.append("  - LAN friends are blocked: ProtonVPN -> Settings -> Connection -> Allow LAN connections",
                         style="red")
            elif net.get("proton_lan"):
                t.append("  (LAN allowed)", style="green")
        else:
            t.append("off", style="green")
        t.append("\nDocker: ", style="bold")
        t.append("running" if net.get("docker") else "not running", style="green" if net.get("docker") else "grey50")
        t.append("\nInternet play: ", style="bold")
        if net.get("tunnel") == "off":
            t.append("off (set up the VPN key to turn it on)", style="grey50")
        elif net.get("public"):
            t.append(f"{net['public']}", style="bold green")
            t.append(f"  -> '{net.get('forward_server')}'")
            if net.get("public_lag"):
                t.append(f"\nLatency to it: {net['public_lag']}", style="grey62")
        elif net.get("tunnel_ok"):
            t.append("VPN is up but Proton gave no forwarded port (needs a NAT-PMP key, paid plan)", style="yellow")
        else:
            t.append("VPN not running", style="yellow")
        if net.get("exit"):
            t.append(f"\nVPN exit: {net['exit']}")
        t.append(f"\nJoin on this Wi-Fi: {network.wifi_label(network.display_address(s))}", style="cyan")
        self.query_one("#net-card", Static).update(t)
        self.query_one("#net-share", Static).update(self.share_card(snap, net, s))

    def share_card(self, snap, net, s):
        """The share mode in plain words, with a cat that matches it and a checklist that fills in live."""
        mode = net.get("share_mode")
        face, words = {"hidden": ("( -_- )", "hidden: nobody can see you"),
                       "local": ("( o.o )", f"local name: {share.host()}")}.get(mode, ("( o.o )", "pick how friends find you"))
        t = Text()
        t.append(" /\\_/\\\n", style="grey62")
        t.append(f"{face}  {words}\n", style="bold")
        t.append(" > ^ <\n", style="grey62")
        if mode is None:
            t.append("\nNot chosen yet: the invite shows this Mac's Wi-Fi address. Pick Hidden or Local name.", style="yellow")
            return t
        applied = not share.needs_restart(s)
        if mode == "hidden":
            steps = [(bool(net.get("proxy_up")), "Sign-in proxy is up (mc vpn proxy)"),
                     (bool(net.get("tunnel_ok")), "Docker VPN is up"),
                     (bool(net.get("public")), f"Internet address ready: {net.get('public')}" if net.get("public")
                      else "Internet address not ready yet (needs a NAT-PMP key: mc vpn)"),
                     (applied, "The server listens on this Mac only" if applied else f"Restart '{s.name}' to apply hidden mode")]
        else:
            vpn_ok = not snap.vpn or net.get("proton_lan") is True
            steps = [(bool(net.get("bonjour")), f"Name published: {share.host()}" if net.get("bonjour")
                      else "Name not published yet (the agent keeps it alive)"),
                     (applied, "The server listens on the Wi-Fi" if applied else f"Restart '{s.name}' to apply local mode"),
                     (vpn_ok, "Mac VPN is off: Wi-Fi friends can connect" if not snap.vpn
                      else "Mac VPN is on and allows the local network" if vpn_ok
                      else "Mac VPN is on: Wi-Fi friends are blocked")]
        for good, text in steps:
            t.append("\n")
            t.append("[ok] " if good else "[ ] ", style="green" if good else "yellow")
            t.append(text)
        return t

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id or ""
        if not bid.startswith("net-"):
            return
        event.stop()
        app = self.app
        if bid == "net-start":
            if vpn.enabled():
                app.run_task("Start VPN", vpn.start)
            else:
                app.notify("Set up the VPN key first (Set up VPN key button).", severity="warning")
        elif bid == "net-stop":
            app.run_task("Stop VPN", vpn.stop)
        elif bid == "net-address":
            def copy(addr):
                if not addr:
                    return app.notify("No internet address yet. Is the VPN running with port forwarding?",
                                      severity="warning")
                app.copy_to_clipboard(addr)
                app.notify(addr, title="Copied internet address")
            app.run_task("Internet address", vpn.public_address, True, after=copy)
        elif bid == "net-setup":
            self.setup_flow()
        elif bid == "net-city":
            self.city_flow()
        elif bid == "net-logs":
            which = [("vpn", vpn.VPN), ("forward", vpn.FORWARD)]
            app.run_task("Fetch logs", lambda: "\n\n".join(f"== {n} ==\n{vpn.logs(c, 25)}" for n, c in which),
                         after=lambda text: app.push_screen(TextResult("VPN logs", text, markup=False)))
        elif bid in ("net-speed", "net-speed-internet"):
            self.run_speedtest(internet=bid == "net-speed-internet")
        elif bid in ("net-share-hidden", "net-share-local"):
            self.switch_share(bid.removeprefix("net-share-"))
        elif bid == "net-share-copy":
            app.action_invite()
        elif bid == "net-proxy":
            app.run_task("Sign-in proxy", vpn.proxy_info,
                         after=lambda info: app.push_screen(TextResult("Launcher sign-in proxy",
                                                                       "\n".join(vpn.proxy_help(info)), markup=False)))
        elif bid == "net-proxy-copy":
            def copy_password(info):
                if not info["password"]:
                    return app.notify("Start the VPN first (Start VPN).", severity="warning")
                app.copy_to_clipboard(info["password"])
                app.notify("Paste it into your launcher's proxy password field.", title="Copied the proxy password")
            app.run_task("Sign-in proxy", vpn.proxy_info, after=copy_password)
        elif bid == "net-auth":
            def show(checks):
                lines, group = [], None
                for c in checks:
                    if c.group != group:
                        group = c.group
                        lines += ["", group]
                    lines.append(("[ok] " if c.good else "[x] ") + c.message)
                    if c.fix:
                        lines.append(f"      -> {c.fix}")
                app.push_screen(TextResult("Sign-in test", "\n".join(lines).strip(), markup=False))
            app.run_task("Test sign-in", network.auth_checks, after=show)
        elif bid == "net-doctor":
            app.action_doctor()
        elif bid == "net-invite":
            app.action_invite()

    def switch_share(self, mode):
        """Choose hidden or local. Used by the two buttons and by the first-run question."""
        app = self.app

        def switched(restart):
            app._net = None                                    # show the new mode now, not after the 8 s cache
            if restart:
                app.notify(f"Restart {', '.join(restart)} to apply it (Ctrl+P: restart in...).", severity="warning", timeout=10)
        app.run_task(f"Share mode: {mode}", share.set_mode, mode, after=switched)

    def run_speedtest(self, internet):
        app, s = self.app, self.server
        if s is None:
            return app.notify("Create a server first.", severity="warning")
        out = self.query_one("#net-speed-out", CopyLog)
        out.clear()
        out.write(Text("Testing... this takes a few seconds." + (" The internet part takes about 20." if internet else ""),
                       style="grey62"))
        say = lambda text: app.call_from_thread(app.activity, text)

        def show(report):
            out.clear()
            for line in speedtest.format_report(report).splitlines():
                out.write(Text(line))
            app._net = None                                    # the latency next to the address is new
        app.run_task("Speed test", speedtest.run, s, 6, internet, say, after=show)

    def setup_flow(self):
        app = self.app
        saved_country, saved_city = vpn.place()

        def got_city(key, country, city):
            app.run_task("Save VPN key", vpn.setup, key, country, city or "any")      # empty = any city

        def got_country(key, country):
            if country:
                app.push_screen(AskText("Exit city (optional)", "e.g. Amsterdam; empty for any", value=saved_city or "",
                                        hint="A nearer city means less lag. You can change it later (Exit city button)."),
                                lambda city: got_city(key, country, city))

        def got_key(key):
            if key:
                app.push_screen(AskText("Exit country", "e.g. Netherlands; 'any' for no preference", value=saved_country or "",
                                        hint="A country near your friends means less lag. Needs P2P servers (paid plan) there."),
                                lambda country: got_country(key, country))
        app.push_screen(AskText("ProtonVPN WireGuard private key", "44 characters ending in =", secret=True,
                                hint="Proton account -> VPN -> WireGuard configuration, with NAT-PMP (Port Forwarding) ticked. Saved owner-only."),
                        got_key)

    def city_flow(self):
        """Change the VPN's exit city without typing the key again."""
        app = self.app
        if not vpn.configured():
            return app.notify("Set up the VPN key first (Set up VPN key button).", severity="warning")
        country, city = vpn.place()
        app.push_screen(AskText("Exit city", "e.g. Amsterdam; 'any' for no preference", value=city or "",
                                hint=f"Now: {vpn.describe_place(country, city)}. A nearer city means less lag. "
                                     "It applies the next time the VPN starts."),
                        lambda value: value and app.run_task("Exit city", vpn.set_place, None, value))
