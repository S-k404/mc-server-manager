"""System and per-server resource usage (psutil), plus tick-time (lag) via the server console."""
import re
import threading
import time
from dataclasses import dataclass, field

import psutil

from . import container as dk
from .core import ROOT, dir_size, parse_player_list

TICK_RE = re.compile(r"Average time per tick: ([\d.]+)ms")
PCT_RE = re.compile(r"P50: ([\d.]+)ms P95: ([\d.]+)ms P99: ([\d.]+)ms")
GAME_TTL = 2.0   # seconds between console queries (tick time, player list); CPU/RAM/system are sampled every refresh


@dataclass
class SystemStats:
    cpu_percent: float
    per_cpu: list
    ram_used_gb: float
    ram_total_gb: float
    ram_percent: float
    swap_used_gb: float
    disk_free_gb: float
    disk_total_gb: float
    net_up_kbs: float
    net_down_kbs: float
    battery: int | None
    on_battery: bool
    load: tuple


@dataclass
class ServerStats:
    name: str
    state: str
    cpu_percent: float | None = None
    ram_gb: float | None = None
    ram_max_gb: int | None = None
    threads: int | None = None
    uptime_s: float | None = None
    mspt: float | None = None
    tps: float | None = None
    p95: float | None = None
    world_mb: float | None = None
    players: list = field(default_factory=list)
    max_players: int | None = None


class Sampler:
    """Keeps state between samples so CPU % and network rates are meaningful."""

    def __init__(self, background_world=False):
        self.background_world = background_world     # the dashboard: never make the first sample wait for a world to be measured
        self._world_busy = set()
        self._procs = {}
        self._cpu = {}   # container name -> (monotonic time, cpu microseconds)
        self._net = None
        self._world_cache = {}
        self._game = {}   # server name -> (monotonic time, mspt, tps, p95, max_players, players)
        self._cpu_prev = psutil.cpu_times(percpu=True)
        self._cpu_lock = threading.Lock()

    def cpu_per_core(self):
        """Busy % per core since the previous call. psutil.cpu_percent keeps its state per thread, and the dashboard
        samples from a fresh worker thread each time (so it always read 0%); this keeps the state on the Sampler."""
        with self._cpu_lock:
            prev, self._cpu_prev = self._cpu_prev, psutil.cpu_times(percpu=True)
            now = self._cpu_prev
        if len(prev) != len(now):
            return [0.0] * len(now)
        out = []
        for a, b in zip(prev, now):
            total = sum(b) - sum(a)
            busy = total - (b.idle - a.idle)
            out.append(max(0.0, min(100.0, busy / total * 100)) if total > 0 else 0.0)
        return out

    def system(self):
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(str(ROOT))
        net = psutil.net_io_counters()
        now = time.monotonic()
        up = down = 0.0
        if self._net:
            t0, sent0, recv0 = self._net
            dt = max(now - t0, 1e-6)
            up, down = (net.bytes_sent - sent0) / dt / 1024, (net.bytes_recv - recv0) / dt / 1024
        self._net = (now, net.bytes_sent, net.bytes_recv)
        per = self.cpu_per_core()
        bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        try:
            load = psutil.getloadavg()
        except OSError:
            load = (0, 0, 0)
        return SystemStats(
            cpu_percent=sum(per) / len(per) if per else 0.0, per_cpu=per,
            ram_used_gb=(vm.total - vm.available) / 2**30, ram_total_gb=vm.total / 2**30, ram_percent=vm.percent,
            swap_used_gb=psutil.swap_memory().used / 2**30,
            disk_free_gb=disk.free / 2**30, disk_total_gb=disk.total / 2**30,
            net_up_kbs=up, net_down_kbs=down,
            battery=int(bat.percent) if bat else None, on_battery=bool(bat and not bat.power_plugged),
            load=load,
        )

    def _container_stats(self, s, st):
        """CPU/RAM/threads for a Docker-runtime server, from the container's cgroup."""
        use = dk.usage(s.container)
        started = s.started_at()
        if started:
            st.uptime_s = time.time() - started
        if not use:
            return
        mem, usec, pids = use
        now = time.monotonic()
        prev = self._cpu.get(s.name)
        self._cpu[s.name] = (now, usec)
        if prev and now > prev[0]:
            st.cpu_percent = (usec - prev[1]) / 1e6 / (now - prev[0]) * 100 / psutil.cpu_count()
        st.ram_gb = mem / 2**30
        st.threads = pids

    def server(self, s, with_game=True, sessions=None):
        """Stats for one server. with_game=False skips console queries (faster). `sessions`: see core.session_names()."""
        state = s.state(sessions)
        st = ServerStats(name=s.name, state=state, ram_max_gb=s.inst.ram_gb)
        if state in ("running", "starting") and s.docker:
            self._container_stats(s, st)
        elif state in ("running", "starting"):
            proc = self._procs.get(s.name)
            if proc is None or not proc.is_running():       # only ask tmux for the Java pid when we don't already have it
                pid = s.java_pid()
                if pid:
                    proc = self._procs[s.name] = psutil.Process(pid)
                    proc.cpu_percent()  # prime
            if proc:
                try:
                    with proc.oneshot():
                        st.cpu_percent = proc.cpu_percent() / psutil.cpu_count()
                        st.ram_gb = proc.memory_info().rss / 2**30
                        st.threads = proc.num_threads()
                        st.uptime_s = time.time() - proc.create_time()
                except psutil.Error:
                    self._procs.pop(s.name, None)
        if with_game and state == "running":
            self._game_stats(s, st)
        else:
            self._game.pop(s.name, None)
        st.world_mb = self.world_size_mb(s)
        return st

    def _game_stats(self, s, st):
        """Tick time + players over the console, at most every GAME_TTL seconds (it opens a console connection each time)."""
        cached = self._game.get(s.name)
        if cached and time.monotonic() - cached[0] < GAME_TTL:
            _, st.mspt, st.tps, st.p95, st.max_players, players = cached
            st.players = list(players)
            return
        reply = s.run("tick query")
        m = TICK_RE.search(reply)
        if m:
            st.mspt = float(m.group(1))
            st.tps = min(20.0, 1000.0 / st.mspt) if st.mspt > 0 else 20.0
        m = PCT_RE.search(reply)
        if m:
            st.p95 = float(m.group(2))
        parsed = parse_player_list(s.run("list"))
        if parsed and parsed[0]:
            st.max_players, st.players = parsed[0], parsed[1]
        self._game[s.name] = (time.monotonic(), st.mspt, st.tps, st.p95, st.max_players, tuple(st.players))

    def world_size_mb(self, s, ttl=300):
        """The world's size on disk in MB. Measured at most every `ttl` seconds; with `background_world` the measuring happens on a
        side thread and the number from the last measurement (0 before the first one) is returned at once."""
        cached = self._world_cache.get(s.name)
        if cached and time.monotonic() - cached[0] < ttl:
            return cached[1]
        if not self.background_world:
            return self._measure_world(s)
        if s.name not in self._world_busy:
            self._world_busy.add(s.name)
            threading.Thread(target=self._measure_world, args=(s,), daemon=True, name=f"world-size-{s.name}").start()
        return cached[1] if cached else 0.0

    def _measure_world(self, s):
        try:
            size = sum(dir_size(w) for w in s.world_dirs()) / 1e6
            self._world_cache[s.name] = (time.monotonic(), size)
            return size
        except OSError:
            return 0.0
        finally:
            self._world_busy.discard(s.name)


def fmt_uptime(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"
