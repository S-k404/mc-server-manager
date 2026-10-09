"""`mc` command line (Typer + Rich). Running `mc` with no command opens the dashboard."""
import os
import sys
from typing import List, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import alerts, backups, cat, errlog, resourcepack, share as sharing, support, container, core, countdown, datapacks, events, game, llm, loaders, metrics, modconfig, moddoctor, modmeta, modnotes, mods, network, platform, players, provision, repair, servers, settings_schema, voice, vpn
from . import speedtest as speed
from .core import TMUX, Fail, all_servers, default_server_name, die, get_server
from .resources import Sampler, fmt_uptime

console = Console(highlight=False)
# `[ok]` and `[x]` would be read as Rich markup tags and vanish: escape them
GOOD = "[green]" + escape("[ok]") + "[/]"
FAIL = "[red]" + escape("[x]") + "[/]"
OK, BAD = escape("[ok]"), escape("[x]")
# `[ok]` and `[x]` would be read as Rich markup tags and vanish: escape them
GOOD = "[green]" + escape("[ok]") + "[/]"
FAIL = "[red]" + escape("[x]") + "[/]"
OK, BAD = escape("[ok]"), escape("[x]")
STATE = {"target": None}


class RichReporter(core.Reporter):
    def ok(self, msg): console.print(f"{GOOD} {msg}")
    def warn(self, msg): console.print(f"[yellow]![/] {msg}")
    def info(self, msg): console.print(msg, markup=False)
    def heading(self, msg): console.print(f"[bold cyan]{msg}[/]")
    def progress(self, msg): console.print(msg, end="", markup=False)


core.set_reporter(RichReporter())


def complete_servers(incomplete: str):
    return [s.name for s in all_servers() if s.name.startswith(incomplete)] + (["all"] if "all".startswith(incomplete) else [])


app = typer.Typer(add_completion=True, no_args_is_help=False, rich_markup_mode="rich",
                  pretty_exceptions_enable=False, context_settings={"help_option_names": ["-h", "--help"]},
                  help="Manage your Minecraft servers. Run [bold]mc[/] alone for the dashboard.")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context,
         server: Optional[str] = typer.Option(None, "-s", "--server", help="Server to act on (name, a,b, or all).",
                                              autocompletion=complete_servers)):
    STATE["target"] = server
    if ctx.invoked_subcommand is None:
        dash()
    elif ctx.invoked_subcommand in CAT_AFTER:
        ctx.call_on_close(lambda: cat.maybe(console))


CAT_AFTER = {"game", "servers", "ls", "start", "stop", "restart", "up", "down", "status", "backup", "vpn", "doctor"}


# ---------- target resolution ----------
def targets(many=False):
    target = STATE["target"] or os.environ.get("MC_SERVER") or default_server_name()
    if target is None:
        names = ", ".join(s.name for s in all_servers())
        die(f"Which server? Add -s <name> or set a default with `mc use <name>`. Servers: {names}"
            if names else "No servers yet. Create one: mc create")
    if target == "all" or "," in target:
        if not many:
            die("That command works on one server at a time; use -s <name>.")
        return all_servers() if target == "all" else [get_server(n) for n in target.split(",")]
    return [get_server(target)]


def one():
    return targets()[0]


def each(fn, many=True):
    for i, s in enumerate(targets(many)):
        if i:
            console.print()
        fn(s)


STATE_STYLE = {"running": "green", "starting": "yellow", "stopped": "dim", "crashed": "red"}


def state_text(state):
    return f"[{STATE_STYLE.get(state, 'white')}]{state}[/]"


# ---------- servers ----------
@app.command("servers")
def servers_cmd(names: bool = typer.Option(False, "--names", hidden=True)):
    """List all servers with version, port, RAM, state and players."""
    all_ = all_servers()
    if names:
        print("\n".join(s.name for s in all_))
        return
    if not all_:
        return console.print("No servers yet. Create one: [bold]mc create[/]")
    default = default_server_name()
    t = Table(box=None, header_style="bold cyan", pad_edge=False)
    for col in ("", "NAME", "VERSION", "PORT", "RAM", "STATE", "PLAYERS"):
        t.add_column(col)
    for s in all_:
        st = s.state()
        count = "-"
        if st == "running":
            count = str(len(players.online(s)))
        t.add_row("[cyan]*[/]" if s.name == default else "", s.name + (" [dim](docker)[/]" if s.docker else ""),
                  s.label, str(s.port),
                  f"{s.inst.ram_gb}G", state_text(st), count)
    console.print(t)
    if len(all_) > 1:
        console.print("\n[dim]* = default. Change with `mc use <name>`, or add -s <name> to any command.[/]")


app.command("ls", hidden=True)(servers_cmd)


@app.command()
def use(name: str = typer.Argument(..., autocompletion=complete_servers)):
    """Pick the default server."""
    servers.use(name)


@app.command()
def create(name: Optional[str] = typer.Argument(None),
           version: Optional[str] = typer.Option(None, help="Minecraft version (default: latest). List: mc versions"),
           loader: str = typer.Option("fabric", help="fabric, quilt, neoforge, forge, paper or vanilla. List: mc loaders"),
           port: Optional[int] = typer.Option(None), ram: int = typer.Option(4, help="Max RAM in GB."),
           offline: Optional[bool] = typer.Option(None, "--offline/--online", help="Allow offline (cracked) accounts."),
           whitelist: bool = typer.Option(True, "--whitelist/--no-whitelist"),
           mrpack: Optional[str] = typer.Option(None, help="Build from a Modrinth modpack (.mrpack file or slug)."),
           world_name: Optional[str] = typer.Option(None, "--world-name", help="Folder / name of the world (default: world)."),
           seed: Optional[str] = typer.Option(None, help="World seed."),
           motd: Optional[str] = typer.Option(None, help="Name shown in the multiplayer list."),
           difficulty: Optional[str] = typer.Option(None, help="peaceful, easy, normal or hard."),
           gamemode: Optional[str] = typer.Option(None, help="survival, creative, adventure or spectator."),
           max_players: Optional[int] = typer.Option(None, "--max-players"),
           view_distance: Optional[int] = typer.Option(None, "--view-distance"),
           hardcore: Optional[bool] = typer.Option(None, "--hardcore/--no-hardcore"),
           gc: str = typer.Option("g1", help="Garbage collector: g1 (tuned, default) or zgc (near-zero pauses, more memory)."),
           docker: bool = typer.Option(False, "--docker", help="Run it in a container instead of on this Mac.")):
    """Create a new server (asks questions if you leave things out)."""
    if mrpack:
        if name is None:
            name = typer.prompt("Name for the modpack server")
        from . import upgrade
        s = upgrade.create_from_mrpack(name, mrpack, port, ram if ram != 4 else None, bool(offline))
        console.print(f"  Start it: [bold]mc -s {s.name} start[/]")
        return
    if name is None:
        console.print("[bold cyan]New server[/]  (press Enter to accept the [default])")
        name = typer.prompt("Name (e.g. survival)")
        latest = provision.latest_release()
        version = typer.prompt("Minecraft version (mc versions lists them)", default=version or latest)
        version = None if version == latest else version
        ok_loaders = provision.compatible_loaders(version or latest)
        console.print("  Loaders for this version: " + ", ".join(
            f"[bold]{k}[/] ({loaders.get(k).label})" for k in ok_loaders))
        loader = typer.prompt("Loader", default=loader if loader in ok_loaders else ok_loaders[0])
        ram = typer.prompt("Max RAM in GB", default=ram, type=int)
        if offline is None:
            offline = typer.confirm("Allow offline (non-Mojang) accounts?", default=True)
        if world_name is None:
            world_name = typer.prompt("World name", default="world")
    props = {"motd": motd, "difficulty": difficulty, "gamemode": gamemode, "max-players": max_players,
             "view-distance": view_distance, "level-seed": seed,
             "hardcore": None if hardcore is None else str(hardcore).lower()}
    s = servers.create(name, version, loader, port, ram, bool(offline), whitelist, props=props,
                       world_name=None if world_name == "world" else world_name, gc=gc,
                       runtime="docker" if docker else "native")
    console.print(f"  Start it: [bold]mc -s {s.name} start[/]    Make it default: [bold]mc use {s.name}[/]")


@app.command()
def versions(snapshots: bool = typer.Option(False, "--snapshots", help="Include snapshots and release candidates."),
             loader: Optional[str] = typer.Option(None, help="Only versions this loader supports."),
             count: int = typer.Option(25, "-n", help="How many to show.")):
    """Minecraft versions you can create a server on (newest first), with the loaders that support each."""
    if loader and loader not in loaders.KEYS:
        raise typer.BadParameter(f"loader must be one of: {', '.join(loaders.KEYS)}")
    sup = provision.supported_versions(loader) if loader else None
    shown = [v for v in provision.release_versions(snapshots) if sup is None or v in sup][:count]
    t = Table(box=None, header_style="bold cyan")
    t.add_column("VERSION")
    t.add_column("LOADERS")
    for v in shown:
        t.add_row(v, ", ".join(provision.compatible_loaders(v)))
    console.print(t)
    console.print("[dim]Create one: mc create <name> --version <version> --loader <loader>[/]")


@app.command("loaders")
def loaders_cmd():
    """What you can run: Fabric, Quilt, NeoForge, Forge, Paper or vanilla, and what each is for."""
    t = Table(box=None, header_style="bold cyan")
    for col in ("LOADER", "ADD-ONS", "WHAT IT IS"):
        t.add_column(col)
    for k, info_ in loaders.LOADERS.items():
        t.add_row(k, info_.content or "none", info_.blurb)
    console.print(t)


@app.command()
def rename(new: str = typer.Argument(..., help="New server name (lowercase letters, numbers, - and _)."),
           yes: bool = typer.Option(False, "--yes", "-y")):
    """Rename the selected server (its folder, backups and settings move with it). Stop it first."""
    s = one()
    if not yes:
        typer.confirm(f"Rename '{s.name}' to '{new}'?", abort=True)
    servers.rename(s.name, new)


@app.command()
def clone(source: str = typer.Argument(..., autocompletion=complete_servers), new: str = typer.Argument(...),
          port: Optional[int] = None, with_world: bool = typer.Option(False, "--with-world")):
    """Copy a server's mods and settings into a new server."""
    servers.clone(source, new, port, with_world)


@app.command()
def delete(name: str = typer.Argument(..., autocompletion=complete_servers),
           yes: bool = typer.Option(False, "--yes", "-y")):
    """Move a server into backups/ (never erased)."""
    if not yes:
        typer.confirm(f"Move '{name}' and its world into backups/?", abort=True)
    servers.delete(name)


# ---------- run ----------
def which_arg():
    return typer.Argument(None, metavar="[SERVER|all]", autocompletion=complete_servers,
                          help="A server name, a,b or all (same as -s). Default: the current server.")


def pick(which):
    if which:
        STATE["target"] = which


@app.command()
def start(which: Optional[str] = which_arg()):
    """Start the server (several can run at once): mc start, mc start all."""
    pick(which)
    each(servers.start)
    if len(targets(True)) == 1:
        info()


@app.command()
def stop(which: Optional[str] = which_arg(),
         no_backup: bool = typer.Option(False, "--no-backup", help="Skip the auto-backup."),
         in_: Optional[str] = typer.Option(None, "--in", help="Warn players and stop after a delay: 30s, 5m, 1h.")):
    """Stop and save: mc stop, mc stop all. A server that won't stop doesn't keep the others running."""
    pick(which)
    if in_:
        return each(lambda s: countdown.schedule(s, "stop", in_))
    failed = []
    for i, s in enumerate(targets(True)):
        if i:
            console.print()
        try:
            servers.stop(s, backup=False if no_backup else None)
        except Fail as e:
            failed.append(f"{s.name}: {e}")
            console.print(Text("[x] " + failed[-1], style="red"))
    if failed:
        die("Not everything stopped. " + " | ".join(failed))


@app.command()
def restart(which: Optional[str] = which_arg(),
            in_: Optional[str] = typer.Option(None, "--in", help="Warn players and restart after a delay: 30s, 5m, 1h.")):
    """Stop then start: mc restart, mc restart all."""
    pick(which)
    if in_:
        return each(lambda s: countdown.schedule(s, "restart", in_))
    each(servers.restart)


@app.command("cancel")
def cancel_cmd():
    """Cancel a restart/stop countdown (mc restart --in 5m)."""
    each(countdown.cancel)


@app.command("_countdown", hidden=True)
def countdown_run(name: str, plan_id: str):
    """(internal) Runs a countdown started by `mc restart --in`."""
    countdown.run(get_server(name), plan_id)


@app.command()
def kill(yes: bool = typer.Option(False, "--yes", "-y")):
    """Force-stop a frozen server (loses progress since the last autosave)."""
    s = one()
    if not yes:
        typer.confirm(f"Force-stop '{s.name}'?", abort=True)
    servers.kill(s)


@app.command()
def status():
    """State, players, mods and settings at a glance."""
    def show(s):
        inst = s.inst
        st = s.state()
        console.print(f"[bold cyan]{s.name}[/]  {s.label} | port {s.port} | {inst.ram_gb} GB | {state_text(st)}")
        if st == "running":
            on = players.online(s)
            console.print(f"  players: {len(on)} online" + (f" ({', '.join(on)})" if on else ""))
        if st == "crashed":
            console.print(f"  [red]{servers.crash_summary(s)['headline']}[/]")
        n = len(list(s.mods_dir.glob('*.jar'))) if s.mods_dir.exists() else 0
        flags = [f"{n} mods", "whitelist " + ("on" if s.prop("white-list") == "true" else "off"),
                 "offline accounts OK" if s.prop("online-mode") == "false" else "paid accounts only"]
        if inst.autostart: flags.append("autostart")
        if inst.keep_alive: flags.append("keep-alive")
        if inst.autobackup: flags.append("backup on stop")
        console.print("  " + " | ".join(flags))
    each(show)
    console.print("[dim]Tip: run [bold]mc[/bold] alone for the live screen: status, console, files, game and network together.[/]")


@app.command()
def info():
    """Addresses players use to join (what your share mode allows: see mc share mode)."""
    s = one()
    a = network.display_address(s)
    console.print(f"[bold cyan]Join '{s.name}'[/]\n  This Mac:   {a['this_mac']}")
    if a["wifi"]:
        reach = core.port_open(platform.lan_ip(), s.port) if s.accepting() and not platform.vpn_active() else None
        note = "" if reach is None else ("  [green](reachable)[/]" if reach else "  [yellow](not reachable: see mc doctor)[/]")
        console.print(f"  Same Wi-Fi: {a['wifi']}{note}")
    elif a["note"]:
        console.print(f"  Same Wi-Fi: [dim]{a['note']}[/]")
    if a["internet"]:
        console.print(f"  From anywhere: {a['internet']}")


@app.command("console")
def console_cmd():
    """Live server console (type commands; Ctrl-b then d to leave it running)."""
    s = one()
    if not s.up():
        die(f"'{s.name}' is not running.  (Tip: `mc` opens the live screen, which also lets you start it.)")
    console.print("[dim]Tip: plain [bold]mc[/bold] shows this console next to live status, players, files and more.[/]")
    if s.docker:
        console.print(f"Opening '{s.name}' console (Docker). Type commands; leave it running with [bold]Ctrl-c[/].")
        console.print(core.dk_tail(s), markup=False)
        path = container.docker_path()
        os.execv(path, [path, "attach", "--sig-proxy=false", s.container])
    console.print(f"Opening '{s.name}' console. Leave it running with [bold]Ctrl-b then d[/].")
    os.execv(TMUX, [TMUX, "attach", "-t", f"={s.session}"])


@app.command("runtime")
def runtime_cmd(where: Optional[str] = typer.Argument(None, metavar="[native|docker]")):
    """Show or change where a server runs: natively (tmux) or in a Docker container. Same folder either way."""
    s = one()
    if where is None:
        return console.print(f"'{s.name}' runs: [bold]{s.runtime}[/]"
                             + (f"  (container {s.container})" if s.docker else ""))
    settings_schema.apply(s, "runtime", where)


@app.command()
def logs(lines: int = typer.Argument(40), follow: bool = typer.Option(False, "-f", "--follow")):
    """Show the server log."""
    s = one()
    if not s.log_path.exists():
        die("No log yet.")
    args = ["tail", "-n", str(lines)] + (["-f"] if follow else []) + [str(s.log_path)]
    os.execv("/usr/bin/tail", args)


@app.command("cracked")
def cracked_cmd(state: Optional[str] = typer.Argument(None, metavar="[on|off]"),
                insecure: bool = typer.Option(False, "--insecure", help="Allow it on a server without a login mod (unsafe)."),
                no_migrate: bool = typer.Option(False, "--no-migrate", help="Don't move players' saved data to their new IDs."),
                dry_run: bool = typer.Option(False, "--dry-run", help="Only show what would move; change nothing.")):
    """Let non-Mojang ("cracked") accounts join: on (adds EasyAuth logins) / off. No argument shows the state."""
    s = one()
    if state is None:
        on = players.cracked_allowed(s)
        return console.print(f"'{s.name}': cracked/offline accounts [bold]{'allowed' if on else 'not allowed'}[/]"
                             + ("  (EasyAuth logins on)" if on and s.has_mod("easyauth") else
                                "  [red](no login mod: unsafe!)[/]" if on else ""))
    if state not in ("on", "off"):
        die("Use: mc cracked on | off")
    players.set_cracked(s, state == "on", insecure, migrate=not no_migrate, dry_run=dry_run)


@app.command("fly")
def fly_cmd(state: Optional[str] = typer.Argument(None, metavar="[on|off]")):
    """Allow or forbid flying (server allow-flight). No argument shows the state."""
    s = one()
    if state is None:
        return console.print(f"'{s.name}': flying is [bold]{'allowed' if s.prop('allow-flight') == 'true' else 'not allowed'}[/]"
                             " (creative mode can always fly)")
    if state not in ("on", "off"):
        die("Use: mc fly on | off")
    game.set_flight(s, state == "on")


@app.command("cmd")
def cmd_(command: List[str] = typer.Argument(..., help='e.g. mc cmd time set day')):
    """Run any server console command and show the reply."""
    s = one()
    s.require_up()
    s.run_and_show(" ".join(command))


@app.command()
def crash():
    """Explain why the server last crashed."""
    s = one()
    c = servers.crash_summary(s)
    console.print(f"[bold cyan]{s.name}[/] last exit code: {c['exit']}")
    console.print(f"[red]{c['headline']}[/]")
    if c["report"]:
        console.print(f"Crash report: {c['report']}")
    for line in c["tail"]:
        console.print("  " + line, markup=False, style="dim")
    found = [f for f in moddoctor.collect(s) if f.level != "note"]
    if found:
        console.print("\nMods that may explain it", style="bold cyan", markup=False)
        show_findings(s, found)


# ---------- resources ----------
def bar(pct, width=20):
    pct = max(0.0, min(100.0, pct or 0.0))
    filled = int(round(pct / 100 * width))
    color = "green" if pct < 60 else "yellow" if pct < 85 else "red"
    return f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/] {pct:4.0f}%"


def res_history(window):
    seconds = metrics.RANGES.get(window)
    if not seconds:
        die("Use --history 5m, 1h or 24h.")
    rows = metrics.load(seconds)
    if len(rows) < 2:
        return console.print("[yellow]![/] No history yet. The background agent records it every 30 s: mc agent install")
    console.print(f"[bold cyan]Last {window}[/] ({len(rows)} samples)")
    lines = [("Mac CPU %", "sys", "cpu", None), ("Mac RAM %", "sys", "ram", None)]
    for s in all_servers():
        lines += [(f"{s.name} CPU %", "server", "cpu", s.name), (f"{s.name} RAM GB", "server", "ram", s.name),
                  (f"{s.name} ms/tick", "server", "mspt", s.name), (f"{s.name} players", "server", "players", s.name)]
    for label, kind, key, name in lines:
        vals = metrics.series(kind, key, seconds, name, points=60)
        if vals:
            console.print(f"{label:<22} {metrics.spark(vals)}  min {min(vals):.1f}  avg {sum(vals) / len(vals):.1f}  "
                          f"max {max(vals):.1f}", markup=False)


alerts_app = typer.Typer(help="Alerts (lag, RAM, disk, internet address) by Mac notification and optional Discord webhook.")
app.add_typer(alerts_app, name="alerts")


@alerts_app.callback(invoke_without_command=True)
def alerts_default(ctx: typer.Context):
    if ctx.invoked_subcommand is not None:
        return
    st = alerts.status()
    on = lambda v: "[green]on[/]" if v else "[dim]off[/]"
    console.print(f"Alerts {on(st['alerts'])} | Mac notifications {on(st['notifications'])} | "
                  f"Discord webhook {'[green]set[/]' if st['webhook'] else '[dim]not set[/]'} | join/leave to Discord {on(st['joins'])}")
    console.print("Rules: lag (< 15 TPS for 1 min), RAM (> 90 % of allowed for 2 min), disk (< 5 GB free), internet address lost/changed.")
    rows = events.recent(8, kinds={"alert", "info"})
    for e in rows:
        from datetime import datetime
        console.print(f"  {datetime.fromtimestamp(e['ts']):%d %b %H:%M}  {e['text']}", markup=False)
    if not rows:
        console.print("[dim]No alerts so far.[/]")


@alerts_app.command("on")
def alerts_on(): core.save_setting("alerts", True); console.print(f"{GOOD} Alerts on.")


@alerts_app.command("off")
def alerts_off(): core.save_setting("alerts", False); console.print("Alerts off.")


@alerts_app.command("webhook")
def alerts_webhook(url: str = typer.Argument(..., help="Discord webhook URL, or 'off' to remove it.")):
    """Send alerts to a Discord channel (the URL is a secret: stored owner-only, never shown)."""
    alerts.set_webhook(url)


@alerts_app.command("joins")
def alerts_joins(state: str = typer.Argument(..., metavar="[on|off]")):
    """Also post joins/leaves to the Discord webhook."""
    core.save_setting("webhook_joins", state == "on")
    console.print(f"Join/leave posts to Discord: {state}.")


@alerts_app.command("test")
def alerts_test():
    """Send a test alert through every channel."""
    alerts.deliver("info", None, "This is a test from mc. Alerts are working.")
    console.print(f"{GOOD} Sent. Discord: " + ("delivered" if alerts.send_webhook("mc test") else "not set / failed") + ".")


@app.command("support")
def support_cmd(lines: int = typer.Option(40, "-n", help="Log lines per server.")):
    """Everything useful for a bug report in one block (copied to the clipboard; passwords and keys are stripped)."""
    text = support.bundle(lines)
    console.print(text, markup=False)
    if platform.copy_to_clipboard(text):
        console.print(f"\n{GOOD} Copied to the clipboard.")


@app.command("docs", hidden=True)
def docs_cmd():
    """(maintenance) Regenerate docs/COMMANDS.md from the CLI itself."""
    import typer.main
    root = typer.main.get_command(app)
    rows = []

    def usage(cmd):
        parts = []
        for p in cmd.params:
            if p.param_type_name == "argument":
                name = p.name.rstrip("_").replace("_", " ").replace(" ", "_")
                parts.append(f"<{name}>" if p.required else f"[{name}]")
        return " ".join(parts)

    def walk(cmd, prefix):
        for name, sub in sorted(getattr(cmd, "commands", {}).items()):
            if getattr(sub, "hidden", False):
                continue
            path = f"{prefix} {name}".strip()
            rows.append((f"mc {path} {usage(sub)}".strip(), (sub.help or "").split("\n")[0].strip()))
            walk(sub, path)
    walk(root, "")
    target = core.ROOT / "docs" / "COMMANDS.md"
    intro = target.read_text().split("```")[0] if target.exists() else "# Every `mc` command\n\n"
    body = "\n".join(f"{a:<36} {b}".rstrip() for a, b in rows)
    target.write_text(f"{intro}```\n{body}\n```\n")
    console.print(f"{GOOD} Wrote {target.relative_to(core.ROOT)} ({len(rows)} commands).")


@app.command("events")
def events_cmd(n: int = typer.Option(25, "-n", help="How many events."),
               all_servers_: bool = typer.Option(False, "--all", help="Every server, not just the selected one.")):
    """What happened recently: starts, stops, backups, crashes, joins, alerts."""
    from datetime import datetime
    name = None if all_servers_ or (STATE["target"] in ("all",)) else (STATE["target"] or default_server_name())
    rows = events.recent(n, server=name)
    if not rows:
        return console.print("No events yet.")
    for e in rows:
        when = datetime.fromtimestamp(e["ts"]).strftime("%d %b %H:%M:%S")
        console.print(f"{when}  {(e.get('server') or '-'):<10} {e['kind']:<8} {e['text']}", markup=False)


@app.command("res")
def res(watch: bool = typer.Option(False, "-w", "--watch", help="Refresh every 2s (Ctrl-C to quit)."),
        history: Optional[str] = typer.Option(None, "--history", help="Graphs from the agent's history: 5m, 1h or 24h.")):
    """System + server resource usage (CPU, RAM, disk, network, lag)."""
    import time
    if history:
        return res_history(history)
    sampler = Sampler()

    def render():
        sysd = sampler.system()
        t = Table.grid(padding=(0, 2))
        t.add_row("[bold]CPU[/]", bar(sysd.cpu_percent), f"{len(sysd.per_cpu)} cores | load {sysd.load[0]:.1f}")
        t.add_row("[bold]RAM[/]", bar(sysd.ram_percent), f"{sysd.ram_used_gb:.1f} / {sysd.ram_total_gb:.0f} GB")
        t.add_row("[bold]Disk[/]", bar(100 - sysd.disk_free_gb / sysd.disk_total_gb * 100),
                  f"{sysd.disk_free_gb:.0f} GB free")
        t.add_row("[bold]Net[/]", f"up {sysd.net_up_kbs:7.1f} KB/s   down {sysd.net_down_kbs:7.1f} KB/s", "")
        if sysd.battery is not None:
            t.add_row("[bold]Battery[/]", f"{sysd.battery}%" + (" [yellow](on battery)[/]" if sysd.on_battery else ""), "")
        st = Table(box=None, header_style="bold cyan")
        for col in ("SERVER", "STATE", "CPU", "RAM", "TPS", "MSPT", "PLAYERS", "UPTIME", "WORLD"):
            st.add_column(col)
        for s in all_servers():
            x = sampler.server(s)
            st.add_row(s.name, state_text(x.state),
                       f"{x.cpu_percent:.0f}%" if x.cpu_percent is not None else "-",
                       f"{x.ram_gb:.1f}/{x.ram_max_gb} GB" if x.ram_gb is not None else f"-/{x.ram_max_gb} GB",
                       f"{x.tps:.1f}" if x.tps else "-", f"{x.mspt:.1f}" if x.mspt else "-",
                       f"{len(x.players)}/{x.max_players}" if x.max_players else "-",
                       fmt_uptime(x.uptime_s), f"{x.world_mb:.0f} MB" if x.world_mb else "-")
        out = Table.grid()
        out.add_row("[bold cyan]This Mac[/]")
        out.add_row(t)
        out.add_row("")
        out.add_row(st)
        return out

    if not watch:
        sampler.system()
        time.sleep(1)  # let CPU % and network rates accumulate
        console.print(render())
        return
    from rich.live import Live
    with Live(render(), console=console, refresh_per_second=1) as live:
        try:
            while True:
                time.sleep(2)
                live.update(render())
        except KeyboardInterrupt:
            pass


app.command("perf", hidden=True)(res)
app.command("top", hidden=True)(res)


# ---------- settings ----------
@app.command("set")
def set_cmd(key: Optional[str] = typer.Argument(None, help="Setting name (see `mc set`)."),
            value: Optional[str] = typer.Argument(None)):
    """Show all settings, or change one: mc set difficulty hard."""
    s = one()
    if key is None:
        running = s.accepting()
        for section, group in _sections(s):
            t = Table(box=None, title=f"[bold cyan]{section}[/]", title_justify="left", show_header=False)
            t.add_column(style="bold", min_width=20)
            t.add_column(min_width=10)
            t.add_column(style="green", min_width=4)
            t.add_column(style="dim")
            for st in group:
                val = settings_schema.current(s, st, running)
                live = "live" if st.live or st in settings_schema.GAMERULES else ""
                t.add_row(st.key, "-" if val is None else val, live, st.label)
            console.print(t)
        if not running:
            console.print("[dim]Game rules show when the server is running.[/]")
        console.print("[dim]Change with: mc set <name> <value>   ('live' = applies without restart)[/]")
        return
    if value is None:
        st = settings_schema.find(key, s)
        val = settings_schema.current(s, st) if st else s.prop(key, None)
        console.print(f"{key} = {val}" + (f"   [dim]{st.label}; choices: {', '.join(st.choices)}[/]" if st and st.choices else ""))
        return
    result = settings_schema.apply(s, key, value)
    if result == "restart":
        console.print(f"[yellow]Restart to apply:[/] mc -s {s.name} restart")


def _sections(s):
    groups = {}
    for st in settings_schema.props_for(s):
        groups.setdefault(st.section, []).append(st)
    groups["mc options"] = settings_schema.INSTANCE
    if s.accepting():
        groups["Game rules (live)"] = settings_schema.GAMERULES
    return groups.items()


@app.command()
def ram(gb: Optional[int] = typer.Argument(None)):
    """Show or change the server's max RAM."""
    s = one()
    if gb is None:
        return console.print(f"'{s.name}' max RAM: {s.inst.ram_gb} GB (Mac has {platform.total_ram_gb()} GB)")
    if settings_schema.apply(s, "ram_gb", gb) == "restart":
        console.print(f"[yellow]Restart to apply:[/] mc -s {s.name} restart")


# ---------- players ----------
@app.command("players")
def players_cmd():
    """Everyone the server knows: online, ops, whitelist, bans."""
    s = one()
    roster = players.roster(s)
    t = Table(box=None, header_style="bold cyan")
    for col in ("", "PLAYER", "OP", "WHITELISTED", "BANNED", "LAST LEFT"):
        t.add_column(col)
    for n, r in roster.items():
        t.add_row("[green]*[/]" if r["online"] else "[dim]o[/]", n, "yes" if r["op"] else "",
                  "yes" if r["whitelisted"] else "", "[red]yes[/]" if r["banned"] else "", r["last_left"] or "")
    console.print(t if roster else "No players yet.")


@app.command()
def say(message: List[str]):
    """Broadcast a chat message."""
    each(lambda s: players.say(s, " ".join(message)))


@app.command()
def op(names: List[str]):
    """Make players operators."""
    players.op(one(), names)


@app.command()
def deop(names: List[str]):
    """Remove operator."""
    players.deop(one(), names)


@app.command()
def kick(name: str, reason: List[str] = typer.Argument(None)):
    """Kick a player."""
    players.kick(one(), name, " ".join(reason or []))


@app.command()
def ban(name: str, reason: List[str] = typer.Argument(None)):
    """Ban a player."""
    players.ban(one(), name, " ".join(reason or []))


@app.command()
def unban(name: str):
    """Unban a player."""
    players.unban(one(), name)


@app.command("tell")
def tell_cmd(name: str, message: List[str]):
    """Send a private message to one online player."""
    players.tell(one(), name, " ".join(message))


@app.command("player")
def player_cmd(name: str):
    """Everything known about one player: roles, ban, playtime, deaths, advancements, last seen."""
    console.print(players.format_details(players.details(one(), name)), markup=False)


@app.command("bans")
def bans_cmd():
    """Banned players with their reasons."""
    rows = players.banned(one())
    if not rows:
        return console.print("Nobody is banned.")
    for b in rows:
        console.print(f"{b['name']:<18} {b['reason'] or '(no reason)'}  |  {b['created']}", markup=False)


wl_app = typer.Typer(help="Whitelist: list, add, remove, on, off.")
app.add_typer(wl_app, name="whitelist")


@wl_app.callback(invoke_without_command=True)
def wl_default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        s = one()
        names = [e["name"] for e in s.json_list("whitelist.json")]
        state = "on" if s.prop("white-list") == "true" else "off"
        console.print(f"[bold cyan]'{s.name}' whitelist ({state}, {len(names)})[/]")
        for n in names:
            console.print(f"  {n}")


@wl_app.command("add")
def wl_add(names: List[str]): players.whitelist_add(one(), names)


@wl_app.command("remove")
def wl_remove(names: List[str]): players.whitelist_remove(one(), names)


@wl_app.command("online")
def wl_online(): players.whitelist_online(one())


@wl_app.command("on")
def wl_on(): players.whitelist_enabled(one(), True)


@wl_app.command("off")
def wl_off(): players.whitelist_enabled(one(), False)


dp_app = typer.Typer(help="Datapacks: list, add, remove.")
app.add_typer(dp_app, name="datapack")
app.add_typer(dp_app, name="datapacks", hidden=True)


@dp_app.callback(invoke_without_command=True)
def dp_default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        s = one()
        packs = datapacks.installed(s)
        console.print(f"[bold cyan]'{s.name}' datapacks ({len(packs)})[/]")
        for p in packs:
            console.print(f"  {p['name']}  [dim]{p['source']}{' ' + p['version'] if p['version'] else ''}[/]")
        if not packs:
            console.print("  none yet. Add one: mc datapack add <file.zip or folder>")


@dp_app.command("add")
def dp_add(path: str = typer.Argument(..., help="A datapack .zip or folder (pack.mcmeta at its top).")):
    """Add a datapack to the world (a running server runs /reload)."""
    datapacks.add(one(), path)


@dp_app.command("search")
def dp_search(query: List[str]):
    """Find datapacks on Modrinth that work on this server's version."""
    s = one()
    hits = datapacks.search(s, " ".join(query))
    if not hits:
        return console.print("No datapacks found for that.")
    for h in hits:
        console.print(f"[bold]{h['slug']}[/]  {h['title']}  [dim]{h['downloads']:,} downloads[/]")
        console.print(f"    {' '.join(h['description'].split())[:100]}", markup=False)
    console.print("[dim]Install with: mc datapack install <slug>[/]")


@dp_app.command("install")
def dp_install(names: List[str]):
    """Install datapacks from Modrinth (a running server runs /reload)."""
    datapacks.install(one(), names)


@dp_app.command("update")
def dp_update():
    """Update Modrinth datapacks that have a newer build."""
    datapacks.update(one())


@dp_app.command("remove")
def dp_remove(name: str):
    """Move a datapack out of the world into backups/."""
    datapacks.remove(one(), name)


pw_app = typer.Typer(help="EasyAuth login passwords.")
app.add_typer(pw_app, name="password")


@pw_app.command("reset")
def pw_reset(name: str):
    """Forgotten password: lets the player /register again."""
    players.password_reset(one(), name)


@pw_app.command("set")
def pw_set(name: str, password: str):
    """Set a player's login password."""
    players.password_set(one(), name, password)


# ---------- mods ----------
mods_app = typer.Typer(help="Mods from Modrinth (filtered to the server's version + loader).")
app.add_typer(mods_app, name="mods")
app.add_typer(mods_app, name="mod", hidden=True)


def side(client_side):
    return "[yellow]players need it too[/]" if client_side == "required" else "[green]server-only[/]"


@mods_app.callback(invoke_without_command=True)
def mods_default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        def show(s):
            entries, manual = mods.sync(s)
            t = Table(box=None, header_style="bold cyan", title=f"[bold cyan]'{s.name}' mods ({s.label})[/]",
                      title_justify="left")
            for col in ("MOD", "VERSION", "WHO NEEDS IT", ""):
                t.add_column(col)
            for e in sorted(entries.values(), key=lambda e: e.slug):
                t.add_row(e.slug, e.version_number, side(e.client_side), "pinned" if e.pinned else "")
            for f in manual:
                t.add_row(f, "[dim]manual jar[/]", "", "")
            console.print(t)
        each(show)


@mods_app.command("search")
def mods_search(query: List[str]):
    """Find mods that work on this server."""
    s = one()
    hits = mods.search(s, " ".join(query))
    if not hits:
        return console.print(f"No {s.label} server-compatible mods found.")
    t = Table(box=None, header_style="bold cyan", title="Install with: mc mods add <slug>", title_justify="left")
    for col in ("SLUG", "DOWNLOADS", "WHO NEEDS IT", "DESCRIPTION"):
        t.add_column(col, overflow="fold")
    for h in hits:
        t.add_row(f"[bold]{h['slug']}[/]", f"{h['downloads']:,}", side(h.get("client_side")),
                  " ".join(h["description"].split())[:70])
    console.print(t)


def _restart_hint(s):
    if s.up():
        console.print(f"[yellow]Restart for mod changes to take effect:[/] mc -s {s.name} restart")


@mods_app.command("add")
def mods_add(names: List[str]):
    """Install mods (+ required dependencies)."""
    s = one()
    mods.add(s, names, notes=True)
    _restart_hint(s)


@mods_app.command("add-jar")
def mods_add_jar(path: str = typer.Argument(..., help="A mod .jar you downloaded (e.g. from CurseForge).")):
    """Copy a downloaded Fabric mod .jar into mods/ (for mods that aren't on Modrinth)."""
    s = one()
    mods.add_jar(s, path)
    _restart_hint(s)


@mods_app.command("remove")
def mods_remove(names: List[str]):
    """Uninstall mods."""
    s = one()
    mods.remove(s, names)
    _restart_hint(s)


@mods_app.command("update")
def mods_update():
    """Update all unpinned mods."""
    def run(s):
        if mods.update(s):
            _restart_hint(s)
    each(run)


@mods_app.command("explain")
def mods_explain(name: str = typer.Argument(..., help="A Modrinth slug, or a mod/jar that is installed.")):
    """Have the tiny local model read a mod's page and say what it does and what setup it needs (needs: mc llm start)."""
    for line in modnotes.explain(one(), name):
        console.print(line, markup=False)


FINDING_STYLE = {"error": "red", "warn": "yellow", "note": "cyan", "fix": "green"}


def show_findings(s, found):
    for level, text in moddoctor.lines(s, found):          # text can hold [brackets] from jar names: never markup
        console.print(text, markup=False, style=FINDING_STYLE.get(level, ""))


@mods_app.command("config")
def mods_config(fix: bool = typer.Option(False, "--fix", help="Move unreadable or empty configs aside (into backups/, never deleted) so the mod writes a "
                                         "fresh one, and give Simple Voice Chat a free UDP port. The server must be stopped.")):
    """Look at every mod's config file: unreadable ones, empty ones, and Simple Voice Chat's port. --fix repairs them."""
    s = one()
    mods.require_modded(s)
    items = modconfig.check(s)
    marks = {"ok": ("[ok]", "green"), "error": ("[x]", "red"), "note": ("[i]", "cyan")}
    for item in items:
        mark, colour = marks[item.level]
        line = Text.assemble((mark, colour), " " + item.message + (f"  ({item.mod})" if item.mod and item.level == "ok" else ""))
        console.print(line, soft_wrap=True)
        for step in item.steps if not fix else ():
            console.print(Text("    -> " + step), soft_wrap=True)
    bad = modconfig.problems(items)
    if not items:
        console.print(Text("No config files yet: mods write theirs the first time the server starts."))
    if fix:
        done = modconfig.fix(s, items)
        for line in done:
            console.print(Text("[ok] " + line, style="green"), soft_wrap=True)
        if bad and not done:
            console.print(Text("Nothing here can be repaired automatically."), style="yellow")
    elif bad:
        console.print(Text(f"{len(bad)} problem(s). Repair them: mc -s {s.name} mods config --fix"), style="bold", soft_wrap=True)
        raise typer.Exit(1)


@mods_app.command("doctor")
def mods_doctor(
        jar: Optional[str] = typer.Argument(None, help="A mod .jar you haven't installed yet: what would it need?"),
        fix: bool = typer.Option(False, "--fix", help="Install missing libraries, update mods built for another "
                                 "Minecraft version, move useless jars aside (into backups/, never deleted).")):
    """Find mods that need something (a library, setup, another version) or clash, and say how to fix it."""
    s = one()
    mods.require_modded(s)
    if jar:
        meta, found = moddoctor.preview(s, jar)
        if meta.kind:
            needs = [n.id + ("" if n.range in ("", "*") else f" {n.range}") for n in meta.needs
                     if n.required and n.id not in moddoctor.PLATFORM]
            console.print(f"{meta.title} {meta.version} ({meta.id}), a {modmeta.FAMILY[meta.kind]} mod", markup=False)
            console.print("Needs: " + (", ".join(needs) or "no other mods"), markup=False)
        if not [f for f in found if f.level != "note"]:
            console.print("[ok] Nothing missing: it can be added as it is.", markup=False, style="green")
        show_findings(s, found)
    elif fix:
        done, left = moddoctor.fix(s)
        console.print(f"[ok] Fixed {len(done)} thing(s)." if done else "[i] Nothing could be fixed automatically.",
                      markup=False, style="green" if done else "yellow")
        if left:
            console.print("Still open:" if moddoctor.problems(left) else "To know:", markup=False, style="bold cyan")
            show_findings(s, left)
        elif done:
            console.print("[ok] No problems left. Start the server to try it.", markup=False, style="green")
    else:
        found = moddoctor.collect(s)
        console.print(moddoctor.headline(found), markup=False,
                      style="green" if not [f for f in found if f.level != "note"] else "yellow")
        show_findings(s, found)


@mods_app.command("pin")
def mods_pin(name: str):
    """Keep a mod at its current version."""
    mods.pin(one(), name, True)


@mods_app.command("unpin")
def mods_unpin(name: str):
    """Allow a pinned mod to update again."""
    mods.pin(one(), name, False)


# ---------- upgrades & packs ----------
@app.command()
def upgrade(version: Optional[str] = typer.Argument(None, help="Target version (default: latest release)."),
            check: bool = typer.Option(False, "--check", help="Only show what would happen."),
            drop_missing: bool = typer.Option(False, "--drop-missing", help="Remove mods with no build for it."),
            allow_downgrade: bool = typer.Option(False, "--allow-downgrade", hidden=True),
            yes: bool = typer.Option(False, "--yes", "-y")):
    """Move a server (and its mods) to another Minecraft version, safely."""
    from . import upgrade as up
    s = one()
    p = up.plan(s, version)
    inst = s.inst
    console.print(f"[bold cyan]{s.name}[/]: Minecraft {inst.mc_version} -> [bold]{p.target}[/]"
                  + (f" | {s.loader_info.label} {inst.loader_version} -> {p.loader_version}" if p.loader_version else "")
                  + f" | Java {inst.java_major} -> {p.java_major}")
    if p.downgrade:
        console.print("[red]That is a downgrade: worlds can break when opened in older versions.[/]")
    if p.rows:
        t = Table(box=None, header_style="bold cyan")
        for col in ("MOD", "NOW", f"FOR {p.target}"):
            t.add_column(col)
        for e, v in p.rows:
            t.add_row(e.title, e.version_number, v["version_number"] if v else "[red]not available yet[/]")
        console.print(t)
    for f in p.manual:
        console.print(f"[yellow]![/] {f}: manual jar, check it yourself")
    if check:
        verdict = ("[green]Ready to upgrade.[/]" if not p.missing else
                   f"[yellow]{len(p.missing)} mod(s) not ready yet.[/]")
        return console.print(verdict + f"  Run: mc -s {s.name} upgrade {p.target}")
    if not yes:
        typer.confirm(f"Upgrade '{s.name}' to {p.target}? (world + mods are backed up first)", abort=True)
    up.apply(p, drop_missing, allow_downgrade)


pack_app = typer.Typer(help="Modpacks for friends' game clients.")
app.add_typer(pack_app, name="pack")


@pack_app.callback(invoke_without_command=True)
def pack_default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        r = resourcepack.current(one())
        if not r["url"]:
            return console.print("No server resource pack. Set one: mc pack set <direct .zip link>")
        console.print(f"Resource pack: {r['url']}\n  sha1 {r['sha1'] or '(missing!)'} | "
                      f"{'required' if r['required'] else 'optional'}", markup=False)


@pack_app.command("set")
def pack_set(url: str, required: Optional[bool] = typer.Option(None, "--required/--optional", help="Kick players who refuse it.")):
    """Offer a resource pack to players when they join (downloads it once to compute the checksum)."""
    resourcepack.set_pack(one(), url, required)


@pack_app.command("clear")
def pack_clear():
    """Stop offering a resource pack."""
    resourcepack.clear(one())


@pack_app.command("export")
def pack_export(folder: str = typer.Argument("~/Downloads")):
    """Make a .mrpack of the client mods players need (import it in Modrinth App / Prism)."""
    from . import upgrade as up
    up.export_client_pack(one(), folder)


# ---------- backups & worlds ----------
backup_app = typer.Typer(help="World backups.")
app.add_typer(backup_app, name="backup")


@backup_app.callback(invoke_without_command=True)
def backup_default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        each(backups.make_backup)


@backup_app.command("list")
def backup_list():
    """List backups (newest first)."""
    from datetime import datetime
    s = one()
    items = backups.list_backups(s)
    t = Table(box=None, header_style="bold cyan", title=f"[bold cyan]'{s.name}' backups[/]", title_justify="left")
    for col in ("#", "NAME", "WHEN", "SIZE", "CHECK"):
        t.add_column(col)
    marks = {"verified": f"[green]{OK} verified[/]", "corrupt": f"[red]{BAD} corrupt[/]", "unverified": "[dim]? not checked[/]"}
    for i, p in enumerate(items, 1):
        t.add_row(str(i), p.name, datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b %H:%M"),
                  f"{backups.size_mb(p):.1f} MB", marks[backups.status(p)])
    console.print(t if items else "No backups yet. Make one: mc backup")
    if items:
        console.print("[dim]Restore with: mc backup restore <#>[/]")


@backup_app.command("restore")
def backup_restore(which: str, yes: bool = typer.Option(False, "--yes", "-y")):
    """Restore a backup (current world is kept in backups/)."""
    s = one()
    chosen = backups.pick(s, which)
    if not yes:
        console.print(backups.describe_text(backups.describe(s, chosen.name)), markup=False)
        typer.confirm(f"Replace '{s.name}' world with {chosen.name}?", abort=True)
    backups.restore(s, chosen.name)


@backup_app.command("verify")
def backup_verify(which: Optional[str] = typer.Argument(None, help="Backup # or name; default: every unchecked one.")):
    """Check backups are readable zips containing a world."""
    s = one()
    if which:
        p = backups.pick(s, which)
        r = backups.verify(p)
        console.print(f"{p.name}: " + (f"{GOOD} ({r['files']} files)" if r["ok"] else f"{FAIL} {r['problem']}"))
        return
    results = backups.verify_all(s)
    bad = [n for n, v in results.items() if v == "corrupt"]
    console.print(f"{len(results) - len(bad)} of {len(results)} backups verified." + (f" [red]Corrupt: {', '.join(bad)}[/]" if bad else ""))


@backup_app.command("push")
def backup_push(remote_name: Optional[str] = typer.Argument(None, help="Saved remote (mc remote list); default: backup_push_to.")):
    """Copy the newest verified backup to a remote machine (nothing is deleted there)."""
    backups.push(one(), remote_name)


@backup_app.command("prune")
def backup_prune(keep: int = typer.Argument(10)):
    """Delete old zip backups, keeping the newest N."""
    backups.prune(one(), keep)


@backup_app.command("auto")
def backup_auto(state: str = typer.Argument(..., help="on or off")):
    """Back up automatically on every stop."""
    backups.set_auto(one(), state == "on")


world_app = typer.Typer(help="Import, export or rename the world.")
app.add_typer(world_app, name="world")


@world_app.command("import")
def world_import(path: str):
    """Swap in a world from a .zip or folder (old one kept)."""
    backups.world_import(one(), path)


@world_app.command("rename")
def world_rename(new: str = typer.Argument(..., help="New world name, e.g. survival-world.")):
    """Rename the world (its folder and level-name). Stop the server first."""
    servers.rename_world(one(), new)


@world_app.command("export")
def world_export(folder: str = typer.Argument("~/Downloads")):
    """Zip the world to share it."""
    backups.world_export(one(), folder)


# ---------- health & sharing ----------
@app.command()
def doctor():
    """Check for lag, VPN, firewall, port and security problems."""
    tgt = targets(True) if STATE["target"] else all_servers()
    checks = network.doctor(tgt, auth=True)
    group = None
    for c in checks:
        if c.group != group:
            group = c.group
            console.print(f"[bold cyan]{group}[/]")
        console.print((f"{GOOD} " if c.good else "[yellow]![/] ") + c.message)
        if c.fix:
            console.print(f"    [dim]-> {c.fix}[/]")
    bad = sum(not c.good for c in checks)
    console.print("\n" + ("[green]All good![/]" if not bad else f"[yellow]{bad} thing(s) to look at above.[/]"))


@app.command()
def speedtest(samples: int = typer.Option(8, "-n", "--samples", min=1, max=60, help="Pings per address."),
              internet: bool = typer.Option(False, "--internet", help="Also test this Mac's internet speed (about 20 s, uses bandwidth)."),
              players_only: bool = typer.Option(False, "--players", help="Only the connected players' latency (instant).")):
    """Latency to the server, to each address people join by, and to each connected player."""
    s = one()
    if players_only:
        s.require_up()
        return console.print("\n".join(speed.format_players(speed.players_latency(s))), markup=False, highlight=False)
    report = speed.run(s, samples, internet, say=lambda text: console.print(f"[dim]{text}[/]"))
    console.print("\n" + speed.format_report(report), markup=False, highlight=False)


share_app = typer.Typer(help="Invite message for friends, and how they find your server (hidden, or by a local name).")
app.add_typer(share_app, name="share")


@share_app.callback(invoke_without_command=True)
def share_default(ctx: typer.Context,
                  show_ip: bool = typer.Option(False, "--show-ip", help="Show this Mac's Wi-Fi address even in hidden or local mode.")):
    """Copy an invite message for friends to the clipboard."""
    if ctx.invoked_subcommand is not None:
        return
    msg = network.invite(one(), live=True, show_ip=show_ip)
    console.print(msg, markup=False)
    if platform.copy_to_clipboard(msg):
        console.print(f"{GOOD} Copied to clipboard.")


@share_app.command("mode")
def share_mode(choice: Optional[str] = typer.Argument(None, metavar="[hidden|local]")):
    """Show or choose how friends find your server: hidden (only the Docker VPN address) or local (a name, not the number)."""
    if choice is None:
        current = sharing.mode()
        console.print(f"Share mode: {current or 'not chosen (the invite shows this Mac\'s Wi-Fi address)'}", markup=False)
        for name, text in sharing.MODES.items():
            console.print(f"  {name:<7}{text}", markup=False)
        return console.print("Choose one: mc share mode hidden|local", markup=False)
    restart = sharing.set_mode(choice, say=lambda msg: console.print(f"[ok] {msg}", markup=False, style="green"))
    for name in restart:
        console.print(f"Restart '{name}' to apply it: mc -s {name} restart   (warn players first: "
                      f"mc -s {name} restart --in 2m)", markup=False, style="yellow")


# ---------- removed: playit tunnel ----------
@app.command("tunnel", hidden=True, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def tunnel_removed():
    """(removed) The playit.gg tunnel is gone."""
    die("The playit tunnel was removed. Internet play now uses ProtonVPN port forwarding: mc vpn")


# ---------- game features ----------
game_app = typer.Typer(help="In-game helpers: time, weather, modes, teleports, items, border, who's where, stats, feed.")
app.add_typer(game_app, name="game")


@game_app.command("time")
def game_time(value: str = typer.Argument(..., help="day | noon | night | midnight | sunrise | sunset | 0-24000")):
    """Set the time of day."""
    game.set_time(one(), value)


@game_app.command("weather")
def game_weather(kind: str = typer.Argument(..., help="clear | rain | thunder"),
                 minutes: Optional[int] = typer.Argument(None, help="How long (default: Minecraft's choice).")):
    """Change the weather."""
    game.set_weather(one(), kind, minutes)


@game_app.command("mode")
def game_mode(mode: str = typer.Argument(..., help="survival | creative | adventure | spectator"), name: str = typer.Argument(...)):
    """Set a player's game mode: mc game mode creative Steve"""
    game.gamemode(one(), mode, name)


@game_app.command("tp")
def game_tp(who: str, target: List[str] = typer.Argument(..., help="a player, or x y z")):
    """Teleport a player to another player or to coordinates."""
    game.teleport(one(), who, tuple(target))


@game_app.command("give")
def game_give(name: str, thing: str, count: int = typer.Argument(1)):
    """Give a player items: mc game give Steve diamond 16"""
    game.give(one(), name, thing, count)


@game_app.command("seed")
def game_seed():
    """Show the world seed."""
    game.seed(one())


@game_app.command("save")
def game_save():
    """Flush the world to disk right now (no backup)."""
    game.save(one())


@game_app.command("border")
def game_border(size: Optional[int] = typer.Argument(None, help="New diameter in blocks."),
                seconds: int = typer.Option(0, "--over", help="Shrink/grow gradually over this many seconds."),
                center: Optional[List[float]] = typer.Option(None, "--center", help="x z (give it twice: --center 0 --center 0)")):
    """Show the world border, or set its size."""
    game.border(one(), size, seconds, tuple(center) if center else None)


@game_app.command("spawn")
def game_spawn(x: int, y: int, z: int):
    """Set the world spawn point."""
    game.set_spawn(one(), x, y, z)


@game_app.command("where")
def game_where(name: Optional[str] = typer.Argument(None, help="A player; omit for everyone online.")):
    """Where players are (coordinates + dimension)."""
    s = one()
    found = {name: game.where(s, name)} if name else game.where_everyone(s)
    if not found:
        return console.print("Nobody is online.")
    for who, pos in found.items():
        console.print(f"[bold]{who}[/]  " + (f"{pos['x']:.0f}, {pos['y']:.0f}, {pos['z']:.0f}  [dim]{pos['dimension']}[/]"
                                           if pos else "[dim]not online[/]"))


@game_app.command("top")
def game_top(stat: str = typer.Argument("playtime", help="playtime | deaths | mobs | walked | jumps"),
             n: int = typer.Option(10, "-n")):
    """Leaderboard from the world's stats (works while the server is stopped)."""
    rows = game.leaderboard(one(), stat, n)
    if not rows:
        return console.print("No stats yet: players need to have played.")
    t = Table(box=None, header_style="bold cyan", pad_edge=False)
    for col in ("#", "PLAYER", stat.upper()):
        t.add_column(col)
    for i, (who, value) in enumerate(rows, 1):
        t.add_row(str(i), who, game.format_stat(stat, value))
    console.print(t)


FEED_STYLE = {"join": "green", "leave": "dim", "chat": "white", "advancement": "yellow", "death": "red"}


@game_app.command("feed")
def game_feed(lines: int = typer.Argument(25)):
    """Recent joins, leaves, chat, advancements and deaths."""
    events = game.feed(one(), lines)
    if not events:
        return console.print("Nothing yet.")
    for when, kind, text in events:
        console.print(f"[dim]{when}[/] [{FEED_STYLE[kind]}]{text}[/]", markup=True, highlight=False)


@game_app.command("info")
def game_info():
    """World summary: version, seed, day, border, difficulty, who's online."""
    s = one()
    d = game.overview(s)
    console.print(f"[bold cyan]{s.name}[/]  {d['version']}  |  {d['difficulty']}  |  default mode {d['mode']}"
                  + ("  |  [red]hardcore[/]" if d["hardcore"] else "") + "")
    console.print(f"World on disk: {d['world_mb']} MB  |  view distance {d['view']}")
    if "seed" in d:
        console.print(f"Seed {d['seed']}  |  day {d['day']}  |  {d['border']}")
        console.print("Online: " + (", ".join(d["online"]) or "nobody"))
    else:
        console.print("[dim]Start the server to see seed, day, border and who's online.[/]")


# ---------- docker vpn (internet play via ProtonVPN port forwarding) ----------
vpn_app = typer.Typer(help="Internet play: ProtonVPN port forwarding in Docker (paid Proton plan). Friends join the VPN's "
                           "IP and forwarded port; the Mac stays off the VPN so LAN play keeps working.")
app.add_typer(vpn_app, name="vpn")


@vpn_app.callback(invoke_without_command=True)
def vpn_default(ctx: typer.Context):
    if ctx.invoked_subcommand is not None:
        return
    vpn_status()


@vpn_app.command("status")
def vpn_status():
    """Show the VPN, the forwarder, the internet address and what is wrong (same as plain `mc vpn`)."""
    st = vpn.status()
    if not st["docker"]:
        return console.print("[yellow]![/] Docker isn't running (or isn't installed).")
    if not st["configured"]:
        return console.print("[yellow]![/] Not set up. Run: [bold]mc vpn setup[/]")
    names = {vpn.VPN: "VPN", vpn.FORWARD: "forwarder"}
    for name, label in names.items():
        stt = st["containers"][name]
        console.print(f"{label:<10} {state_text('running' if stt == 'running' else 'stopped')} ({name}: {stt})")
    country, city = vpn.place()
    console.print(f"VPN health {st['vpn_health']}" + (f" | exit {st['exit']}" if "exit" in st else "")
                  + f" | set to exit in {vpn.describe_place(country, city)}", markup=False)
    if st.get("public"):
        lag = speed.address_latency(st["public"])
        console.print(f"Friends join '{st['forward_server']}' at [bold]{st['public']}[/]" + (f" | {lag}" if lag else ""))
        if note := vpn.server_down_note():
            console.print(Text("! " + note, style="yellow"))
    elif st["containers"][vpn.VPN] == "running":
        console.print(f"[yellow]![/] {vpn.NEED_NATPMP}")
        console.print("gluetun said:" + ("" if (clues := vpn.forward_clues()) else " nothing about port forwarding (mc vpn logs vpn)"),
                      markup=False)
        for line in clues:
            console.print("  " + line, markup=False)
        console.print("Check the whole path with: mc vpn test", markup=False)
    if st["containers"][vpn.VPN] == "running":
        console.print(f"Sign-in proxy {state_text('running' if st.get('proxy_up') else 'stopped')} "
                      f"({vpn.PROXY_HOST}:{vpn.PROXY_PORT}; launcher settings: mc vpn proxy)")
        if st.get("vpn_health") != "healthy":
            console.print(Text("! The VPN isn't healthy. Repair it: mc fix"), style="yellow")


@vpn_app.command("setup")
def vpn_setup(key: Optional[str] = typer.Option(None, "--key", help="ProtonVPN WireGuard private key."),
              country: Optional[str] = typer.Option(None, "--country", help="Exit country (needs P2P servers; paid plan). "
                                                    "Default: the saved one, else Proton picks. 'any' clears it."),
              city: Optional[str] = typer.Option(None, "--city", help="Exit city inside that country, e.g. Amsterdam: a nearer "
                                                 "city means less lag. 'any' clears it.")):
    """Save your ProtonVPN WireGuard key (made with NAT-PMP / Port Forwarding ticked) and turn the Docker VPN on.
    With a key already saved, `--country` / `--city` alone just change where the VPN exits."""
    if key is None and vpn.configured() and (country or city is not None):
        vpn.set_place(country, city)
        return
    if key is None:
        key = typer.prompt("ProtonVPN WireGuard private key", hide_input=True)
        if country is None and not vpn.place()[0]:       # no country chosen yet: ask, rather than let Proton pick a far one
            country = typer.prompt("Exit country, near your friends for less lag (e.g. Netherlands; empty = Proton picks)",
                                   default="", show_default=False)
    vpn.setup(key, country, city)


@vpn_app.command("start")
def vpn_start(): vpn.start()


@vpn_app.command("stop")
def vpn_stop(): vpn.stop()


@vpn_app.command("address")
def vpn_address():
    """Show (and copy) the address friends use to join from the internet."""
    addr = vpn.public_address(live=True)
    if not addr:
        die("No internet address yet. Is the VPN running with port forwarding? See: mc vpn")
    console.print(f"{addr}  -> '{vpn.forward_server()}'", markup=False)
    if note := vpn.server_down_note():
        console.print(Text("! " + note, style="yellow"))
    if lag := speed.address_latency(addr):
        console.print(f"Last latency test: {lag}", markup=False)
    if platform.copy_to_clipboard(addr):
        console.print(f"{GOOD} Copied to clipboard.")


@vpn_app.command("proxy")
def vpn_proxy(copy: bool = typer.Option(False, "--copy", help="Copy the password to the clipboard.")):
    """Settings for your launcher's sign-in proxy (so Microsoft sign-in works without a Mac-wide VPN)."""
    info = vpn.proxy_info()
    for line in vpn.proxy_help(info):
        console.print(line, markup=False)
    if info["password"]:
        if copy and platform.copy_to_clipboard(info["password"]):
            console.print("[ok] Password copied to the clipboard.", markup=False, style="green")
        console.print("Check it works: mc net auth", markup=False)


@vpn_app.command("forward")
def vpn_forward(name: Optional[str] = typer.Argument(None, help="Server the internet address should lead to.")):
    """Choose which server the internet address leads to (Proton forwards one port, so one server is public)."""
    if name is None:
        return console.print(f"The internet address leads to '{vpn.forward_server()}'.", markup=False)
    vpn.set_forward_server(name)


def print_trace(steps, stop_line=True):
    """One hop per line (so grep and copy-paste keep the whole message); returns the first failing step, if any."""
    marks = {True: ("[ok]", "green"), False: ("[x]", "red"), None: ("[!]", "yellow")}
    for st in steps:
        mark, colour = marks[st.ok]
        line = Text.assemble((mark, colour), " " + st.what)
        if st.detail:
            line.append("  " + st.detail, style="dim")
        console.print(line, soft_wrap=True)
        if st.ok is False and st.fix:
            console.print(Text("    -> " + st.fix), soft_wrap=True)
    broken = next((st for st in steps if st.ok is False), None)
    if broken and stop_line:
        console.print(Text("Stops at: " + broken.what), style="bold", soft_wrap=True)
    return broken


@app.command("fix")
def fix_cmd(no_start: bool = typer.Option(False, "--no-start", help="Don't start the server the internet address leads to when it is stopped.")):
    """Repair everything mc can, in order: Docker, servers, share mode, the internet path, voice chat. Safe to run any time."""
    steps = repair.run(start_servers=not no_start, say=lambda m: console.print(Text(m)))
    broken = print_trace(steps, stop_line=False)
    todo = [st for st in steps if st.ok is False]
    if todo:
        console.print(Text(f"{len(todo)} thing(s) still need you; the lines marked [x] say what to do."), style="bold", soft_wrap=True)
        raise typer.Exit(1)
    target = vpn.forward_server() if vpn.enabled() else default_server_name()
    if target and (srv := next((x for x in all_servers() if x.name == target), None)) is not None:
        console.print()
        console.print(network.invite(srv, live=True), markup=False)
    return broken


@vpn_app.command("test")
def vpn_test():
    """Check every hop of the internet path (VPN, forwarded port, redirect, forwarder, server) and show where it stops."""
    if print_trace(vpn.trace()):
        raise typer.Exit(1)
    console.print(Text("Every hop answers. If a friend still can't join, send me this output and `mc logs 30` from the moment they try."),
                  soft_wrap=True)


@vpn_app.command("fix")
def vpn_fix(start_server: bool = typer.Option(False, "--start-server", help="Also start the server the address leads to if it is stopped.")):
    """Check the internet path and repair it: recreates the VPN when the tunnel, port or forwarder is bad (the address changes), then checks again."""
    steps, actions = vpn.fix(start_servers=start_server, say=lambda m: console.print(Text(m)))
    broken = print_trace(steps)
    for a in actions:
        console.print(Text("Did: " + a))
    if broken and broken.what.startswith("Joining"):
        broken = None            # a test from this Mac alone proves nothing (Proton -> Proton); the hops above are what count
    if broken:
        if hint := vpn.fix_hint(steps):
            console.print(Text(hint), soft_wrap=True)
        elif broken.what.startswith("Server '"):
            console.print(Text(f"Start it: mc -s {vpn.forward_server()} start   (or: mc vpn fix --start-server)"))
        raise typer.Exit(1)
    if (addr := vpn.public_address(live=True)):
        console.print(Text(f"Friends join: {addr}"), style="bold")


@vpn_app.command("voice")
def vpn_voice(restart: bool = typer.Option(False, "--restart", help="Restart a running server now (players are warned 15 seconds ahead) "
                                                                   "so Simple Voice Chat reads its new address.")):
    """Simple Voice Chat through the internet address: where audio goes and the address players are given. Rewrites it when it is stale."""
    mods = voice.servers_using_voice()
    if not mods:
        return console.print(Text("No server has Simple Voice Chat installed."))
    for s in mods:
        changed = voice.sync(s)
        route, vp = voice.route(s), voice.udp_port(s)
        conf, want = voice.props(s), voice.wanted(s)
        console.print(Text(f"{s.name}: audio goes " + ("through the internet address" if route == "vpn" else "straight to this Mac")
                           + f" (the mod's UDP port is {vp})"))
        if conf is None:
            console.print(Text("  its config file doesn't exist yet: start the server once"))
            continue
        console.print(Text(f"  voice_host: {conf.get('voice_host') or 'empty'}" + (f"   (just updated: {changed})" if changed else "")
                           + ("" if want in (None, conf.get('voice_host', '')) else f"   (should be {want or 'empty'}; mc leaves a hand-written value alone)")))
        pending = voice.pending_restart(s)
        if pending:
            console.print(Text("  the server restarts by itself once nobody is on it, so the mod reads the new address"))
        if restart and s.up() and (changed or pending):
            countdown.schedule(s, "restart", "15s")
    if route == "vpn":
        console.print(Text("Players need the Simple Voice Chat mod too. Check the whole path with: mc vpn test"))


@vpn_app.command("mtu")
def vpn_mtu(value: Optional[str] = typer.Argument(None, metavar="[SIZE|auto]",
                                                  help="Packet size of the container's tunnel, 1000-1420, or auto.")):
    """Show or set the container tunnel's packet size. A smaller one helps when this Mac's own VPN is on (a tunnel inside a tunnel)."""
    if value is not None:
        return vpn.set_mtu(value)
    now = vpn.mtu()
    console.print(Text(f"Packet size: {now if now else 'gluetun default (1400)'}"
                       + (" (chosen automatically: this Mac's own VPN is on)" if now == vpn.NESTED_MTU and core.setting("vpn_mtu") in (None, "auto") else "")))
    console.print(Text("Change it with: mc vpn mtu 1200   (then mc vpn start). Back to automatic: mc vpn mtu auto"))


@vpn_app.command("logs")
def vpn_logs(which: str = typer.Argument("vpn", help="vpn | forward"),
             lines: int = typer.Option(40, "-n")):
    """Show container logs (the forwarded port shows up in the vpn logs)."""
    name = {"vpn": vpn.VPN, "forward": vpn.FORWARD}.get(which)
    if not name:
        die("Choose one of: vpn, forward.")
    console.print(vpn.logs(name, lines), markup=False)


llm_app = typer.Typer(help="A tiny local language model, in Docker, that reads a new mod's page and tells you what setup "
                           "it needs. Off until `mc llm start`.")
app.add_typer(llm_app, name="llm")


@llm_app.callback(invoke_without_command=True)
def llm_default(ctx: typer.Context):
    if ctx.invoked_subcommand is not None:
        return
    st = llm.status()
    if not st["docker"]:
        return console.print("[yellow]![/] Docker isn't running (or isn't installed).")
    if not st["enabled"]:
        return console.print("Local model: off. Start it once (about 1 GB, runs in Docker): [bold]mc llm start[/]")
    console.print(f"Local model {state_text('running' if st['ready'] else 'stopped')}"
                  f"  ({llm.CONTAINER}, {llm.HOST}:{llm.PORT}, this Mac only)", markup=True)
    console.print(f"Model: {st['model']}  " + ("(downloaded)" if st["model_present"] else "(not downloaded yet: mc llm start)"),
                  markup=False)
    console.print("Notes on new mods: " + ("on" if st["notes"] else "off  (mc llm notes on)"), markup=False)


@llm_app.command("start")
def llm_start():
    """Start the model container (first time: downloads its image and the model, about 1 GB)."""
    llm.start()


@llm_app.command("stop")
def llm_stop():
    """Stop it. Downloaded models stay in the Docker volume."""
    llm.stop()


@llm_app.command("model")
def llm_model(name: Optional[str] = typer.Argument(None, help="e.g. qwen2.5:1.5b or llama3.2:1b")):
    """Show or change the model (see ollama.com/library; a small one is plenty)."""
    if name is None:
        return console.print(f"Model: {llm.model()}", markup=False)
    llm.set_model(name)


@llm_app.command("notes")
def llm_notes(state: str = typer.Argument(..., metavar="on|off")):
    """Turn the notes after each new mod install on or off (the model stays available for `mc mods explain`)."""
    if state not in ("on", "off"):
        die("Usage: mc llm notes on|off")
    core.save_setting("mod_notes", state == "on")
    console.print(f"Notes on new mods: {state}", markup=False)


net_app = typer.Typer(help="Network checks.")
app.add_typer(net_app, name="net")


@net_app.command("auth")
def net_auth():
    """Which Minecraft/Microsoft sign-in servers this network lets through, straight and via the Docker VPN proxy."""
    console.print("Testing the sign-in servers (a few seconds)...", markup=False)
    checks = network.auth_checks()
    group = None
    for c in checks:
        if c.group != group:
            group = c.group
            console.print(group, style="bold cyan", markup=False)
        console.print(("[ok] " if c.good else "[x] ") + c.message, markup=False, style="green" if c.good else "red")
        if c.fix:
            console.print(f"      -> {c.fix}", markup=False, style="dim")


@app.command("cat")
def cat_cmd(setting_: Optional[str] = typer.Argument(None, metavar="[on|off]")):
    """Show a cat. `mc cat off` / `mc cat on` turns the occasional cats after commands off/on."""
    if setting_ in ("on", "off"):
        cat.set_enabled(setting_ == "on")
        return console.print(f"Cats {setting_}." + (" (:" if setting_ == "on" else ""))
    cat.show(console)


@app.command()
def up():
    """Start server(s) + the Docker VPN (internet play) when it's set up."""
    each(servers.start)
    if vpn.enabled():
        vpn.start()
    else:
        console.print("[dim]Internet play is off (LAN works). Turn it on with: mc vpn setup[/]")


@app.command()
def down():
    """Stop server(s); stops the Docker VPN when nothing else is running."""
    each(servers.stop)
    if vpn.enabled() and not any(s.up() for s in all_servers()):
        vpn.stop()


# ---------- automation ----------
agent_app = typer.Typer(help="Background agent: autostart, crash restarts, schedules, notifications.")
app.add_typer(agent_app, name="agent")


@agent_app.callback(invoke_without_command=True)
def agent_default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        from . import agent
        age = agent.heartbeat_age()
        if not agent.agent_installed():
            console.print("[yellow]![/] Agent not installed. Turn it on with: [bold]mc agent install[/]")
        elif age is not None and age < agent.TICK * 3:
            console.print(f"{GOOD} Agent running (last check {age:.0f}s ago).")
        else:
            exit_code = agent.launchd_last_exit()
            extra = f" (launchd last exit code {exit_code})" if exit_code else ""
            console.print(f"[yellow]![/] Agent installed but not checking in{extra}. See: mc agent logs")
        if agent.legacy_installed():
            console.print("[yellow]![/] The agent is installed under its earlier name. Move it to the current one: [bold]mc agent install[/]")
        for s in all_servers():
            inst = s.inst
            bits = [b for b, on in (("autostart", inst.autostart), ("keep-alive", inst.keep_alive)) if on]
            bits += agent.describe(inst.schedules)
            console.print(f"  {s.name}: {', '.join(bits) or 'nothing automated'}")
        console.print(f"  notifications: crashes {'on' if core.setting('notifications', True) else 'off'}, "
                      f"player joins {'on' if core.setting('notify_joins', False) else 'off'}")


@agent_app.command("install")
def agent_install():
    """Start the agent now and at every login."""
    from . import agent
    agent.install()


@agent_app.command("uninstall")
def agent_uninstall():
    """Remove the agent."""
    from . import agent
    agent.uninstall()


@agent_app.command("run", hidden=True)
def agent_run():
    """Run the agent loop in the foreground (launchd uses this)."""
    from . import agent
    agent.run_forever()


@agent_app.command("logs")
def agent_logs(follow: bool = typer.Option(False, "-f", "--follow")):
    """Show what the agent has been doing."""
    from . import agent
    if not agent.LOG_FILE.exists():
        die("No agent log yet.")
    os.execv("/usr/bin/tail", ["tail", "-n", "40"] + (["-f"] if follow else []) + [str(agent.LOG_FILE)])


@agent_app.command("notify")
def agent_notify(what: str = typer.Argument(..., help="crashes or joins"), state: str = typer.Argument(..., help="on/off")):
    """Mac notifications for crashes or player joins."""
    key = {"crashes": "notifications", "joins": "notify_joins"}.get(what)
    if key is None or state not in ("on", "off"):
        die("Usage: mc agent notify crashes|joins on|off")
    core.save_setting(key, state == "on")
    console.print(f"{GOOD} {what} notifications {state}.")


@app.command()
def schedule(kind: Optional[str] = typer.Argument(None, help="backup or restart"),
             value: Optional[str] = typer.Argument(None, help="backup: 6h / 30m / 1d | restart: 05:00 | or off")):
    """Automatic backups / daily restarts: mc schedule backup 6h | mc schedule restart 05:00."""
    from . import agent
    s = one()
    if kind is None:
        items = agent.describe(s.inst.schedules)
        console.print(f"'{s.name}': " + (", ".join(items) if items else "no schedules")
                      + "\n[dim]Set: mc schedule backup 6h | mc schedule restart 05:00 | mc schedule backup off[/]")
        return
    agent.set_schedule(s, kind, value)


# ---------- remotes (Linux VPS over SSH) ----------
remote_app = typer.Typer(help="Remote machines (a VPS) you run mc on over SSH: mc -r vps status.")
app.add_typer(remote_app, name="remote")


@remote_app.callback(invoke_without_command=True)
def remote_default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        remote_list()


@remote_app.command("list")
def remote_list():
    """Saved remotes."""
    from . import remote
    items = remote.load()
    if not items:
        console.print("No remotes yet. Add one: [bold]mc remote add vps user@host[/]")
    for name, r in items.items():
        console.print(f"[bold]{name}[/]  {remote.dest(r)}:{r['port']}  {r['path']}")


@remote_app.command("add")
def remote_add(name: str, target: str = typer.Argument(..., help="user@host"),
               path: str = typer.Option("~/minecraft", help="Where mc lives on the remote."),
               port: int = typer.Option(22, help="SSH port.")):
    """Save a remote."""
    from . import remote
    remote.add(name, target, path, port)


@remote_app.command("remove")
def remote_remove(name: str):
    """Forget a remote (touches nothing on it)."""
    from . import remote
    remote.remove(name)


@remote_app.command("setup")
def remote_setup(name: str):
    """Install mc itself on the remote (copies the tool only, no worlds)."""
    from . import remote
    remote.setup(name)


@app.command()
def deploy(remote_name: str = typer.Argument(..., metavar="REMOTE", help="A saved remote (mc remote add)."),
           start: bool = typer.Option(False, "--start", help="Start it on the remote afterwards.")):
    """Copy a stopped server to a remote with rsync (never deletes anything remotely)."""
    from . import remote
    remote.deploy(one(), remote_name, start=start)


# ---------- dashboard ----------
@app.command()
def dash():
    """Open the interactive dashboard."""
    from .tui.app import run
    run(STATE["target"])


# ---------- entry ----------
def pop_server_flag(argv):
    """Allow -s NAME anywhere (e.g. `mc start -s main`) by moving it to the front."""
    out, target, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("-s", "--server") and i + 1 < len(argv):
            target, i = ["-s", argv[i + 1]], i + 2
            continue
        if a.startswith("--server="):
            target = ["-s", a.split("=", 1)[1]]
        else:
            out.append(a)
        i += 1
    return target + out


def pop_remote_flag(argv):
    """Pull `-r NAME` / `--remote NAME` out of argv -> (name or None, rest)."""
    for i, a in enumerate(argv):
        if a in ("-r", "--remote") and i + 1 < len(argv):
            return argv[i + 1], argv[:i] + argv[i + 2:]
        if a.startswith("--remote="):
            return a.split("=", 1)[1], argv[:i] + argv[i + 1:]
    return None, argv


def run(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not os.environ.get("_MC_COMPLETE"):
        name, rest = pop_remote_flag(argv)
        if name:
            from . import remote
            try:
                sys.exit(remote.run_mc(name, rest))
            except Fail as e:
                console.print(f"{FAIL} {e}")
                sys.exit(1)
    if not os.environ.get("_MC_COMPLETE"):
        argv = pop_server_flag(argv)
    try:
        app(args=argv, prog_name="mc")
    except (Fail, KeyboardInterrupt, BrokenPipeError, Exception) as e:
        handle_error(e, argv)


def handle_error(e, argv):
    """What `mc` shows (and where it keeps the details) for an error that escaped a command. Always exits."""
    if isinstance(e, Fail):
        console.print(f"{FAIL} {e}")
        sys.exit(1)
    if isinstance(e, KeyboardInterrupt):
        console.print()
        sys.exit(130)
    if isinstance(e, BrokenPipeError):                # `mc ... | head`: the reader left, nothing to report
        sys.exit(1)
    path = errlog.record(e, argv)                     # a bug: a short message for the person, the whole traceback in the log
    if os.environ.get("MC_DEBUG"):
        raise e
    console.print(f"{FAIL} Something went wrong: " + escape(f"{type(e).__name__}: {e}"))
    try:
        shown = path.relative_to(core.ROOT)
    except ValueError:
        shown = path
    console.print(escape(f"    Details are in {shown} (run with MC_DEBUG=1 to see the traceback here)."), style="dim")
    sys.exit(1)
