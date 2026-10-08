"""Regenerate the dashboard screenshots in docs/images/ from a made-up demo server.

Nothing real is shown. The script builds a throwaway MC_HOME with fictional servers, players and backups, pretends one server
is running, and drives the real dashboard headlessly; Textual exports each screen as an SVG.

    uv run python scripts/make_screenshots.py

Run it again after a visible change to the dashboard, and commit the new files.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "images"
SIZE = (132, 38)                                            # terminal columns x rows

HOME = Path(tempfile.mkdtemp(prefix="mc-screenshots-"))
os.environ["MC_HOME"] = str(HOME)                           # before mcctl is imported: nothing real is touched
os.environ["MC_NO_CAT"] = "1"
sys.path.insert(0, str(REPO))

from mcctl import agent, backups, core, events, platform, players, resources  # noqa: E402
from mcctl.tui import app as tui  # noqa: E402

LOG = """\
[18:02:11] [Server thread/INFO]: Starting minecraft server version 1.21.4
[18:02:13] [Server thread/INFO]: Loading Fabric Loader 0.16.9 with 4 mods: fabric-api, lithium, krypton, ferrite-core
[18:02:19] [Server thread/INFO]: Done (6.214s)! For help, type "help"
[18:05:42] [Server thread/INFO]: Alex joined the game
[18:06:03] [Server thread/INFO]: Sam joined the game
[18:09:17] [Server thread/INFO]: Alex has made the advancement [Stone Age]
[18:12:40] [Server thread/INFO]: Jordan joined the game
[18:14:55] [Server thread/INFO]: <Sam> anyone want to build the farm near spawn?
[18:15:08] [Server thread/INFO]: <Alex> yes! bringing the wheat
[18:21:31] [Server thread/INFO]: Jordan has made the advancement [Getting an Upgrade]
[18:30:02] [Server thread/INFO]: ThreadedAnvilChunkStorage: All dimensions are saved
"""


def build_demo():
    for sub in ("servers", "backups", "tunnel", "logs"):
        (HOME / sub).mkdir(exist_ok=True)
    for name, port, loader, running in (("survival", 25565, "fabric", True), ("creative", 25566, "paper", False)):
        d = HOME / "servers" / name
        (d / "logs").mkdir(parents=True)
        (d / "world").mkdir()
        (d / "instance.json").write_text(json.dumps({
            "name": name, "mc_version": "1.21.4", "loader": loader, "java_major": 21, "port": port, "ram_gb": 4,
            "desired": "running" if running else "stopped", "autostart": running, "keep_alive": running, "autobackup": running,
            "schedules": {"backup_every": 6 * 3600, "restart_at": "05:00"} if running else {}}))
        (d / "server.properties").write_text(f"server-port={port}\nmotd=Weekend builds\ndifficulty=normal\nwhite-list=true\n"
                                             "online-mode=true\nview-distance=10\nmax-players=10\n")
    s = core.Server("survival")
    (s.dir / "logs" / "latest.log").write_text(LOG)
    now = time.time()
    s.backups_dir.mkdir(parents=True, exist_ok=True)
    for hours_ago, mb in ((1, 24.5), (7, 24.1), (13, 23.6), (25, 22.9), (49, 21.4)):     # a small world, so the demo stays quick
        path = s.backups_dir / f"world-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now - hours_ago * 3600))}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("world/level.dat", os.urandom(1024))
            z.writestr("world/region/r.0.0.mca", os.urandom(int(mb * 1024 * 1024)))
        os.utime(path, (now - hours_ago * 3600, now - hours_ago * 3600))
        backups.verify(path)
    agent.save_state({"servers": {"survival": {"last_backup": now - 3600}}, "heartbeat": now})     # "next backup in 5h 00m"
    events.add("survival", "start", "started")
    events.add("survival", "backup", f"{backups.list_backups(s)[0].name} (verified)")
    for who in ("Alex", "Sam", "Jordan"):
        events.add("survival", "join", f"{who} joined")


def pretend_running():
    """One server up, with healthy numbers; everything else is the real dashboard code."""
    core.Server.state = lambda self, sessions=None: "running" if self.name == "survival" else "stopped"
    core.Server.up = lambda self, sessions=None: self.name == "survival"
    core.Server.listening = lambda self, timeout=1.5: self.name == "survival"
    core.Server.accepting = lambda self: self.name == "survival"
    players.online = lambda s: ["Alex", "Sam", "Jordan"] if s.name == "survival" else []
    platform.lan_ip = lambda: "192.168.1.20"
    agent.agent_installed = lambda: True
    agent.heartbeat_age = lambda: 6.0

    def server(self, s, with_game=True, sessions=None):
        if s.name != "survival":
            return resources.ServerStats(name=s.name, state="stopped", ram_max_gb=s.inst.ram_gb, world_mb=0.0)
        return resources.ServerStats(name=s.name, state="running", cpu_percent=7.5, ram_gb=2.4, ram_max_gb=4, threads=61,
                                     uptime_s=3 * 3600 + 1260, mspt=3.8, tps=20.0, p95=6.1, world_mb=24.6,
                                     players=["Alex", "Sam", "Jordan"], max_players=10)

    def system(self):
        return resources.SystemStats(cpu_percent=11.0, per_cpu=[18, 9, 14, 6, 11, 8, 12, 10], ram_used_gb=9.6, ram_total_gb=16.0,
                                     ram_percent=60.0, swap_used_gb=0.0, disk_free_gb=412.0, disk_total_gb=494.0, net_up_kbs=38.0,
                                     net_down_kbs=112.0, battery=None, on_battery=False, load=(0.9, 0.8, 0.7))
    resources.Sampler.server = server
    resources.Sampler.system = system


async def capture():
    OUT.mkdir(parents=True, exist_ok=True)
    app = tui.Dashboard(target="survival")
    async with app.run_test(size=SIZE) as pilot:
        for _ in range(150):                                                     # wait for the first sample
            await pilot.pause(0.1)
            if app.snapshot is not None and app.query("#statusbar"):
                break
        for tab, name in (("console", "dashboard-live"), ("overview", "dashboard-overview"), ("backups", "dashboard-backups")):
            app.action_tab(tab)
            await pilot.pause(1.8)
            app.save_screenshot(filename=f"{name}.svg", path=str(OUT))
            print("wrote", (OUT / f"{name}.svg").relative_to(REPO))


if __name__ == "__main__":
    build_demo()
    pretend_running()
    try:
        asyncio.run(capture())
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
