"""System CPU must be measured the same way no matter which thread asks (the dashboard uses a new thread per refresh)."""
import threading

import psutil

from mcctl.resources import Sampler



class FakeTimes(tuple):
    """Behaves like psutil's cputimes: iterable fields plus .idle."""
    def __new__(cls, user, system, idle):
        obj = super().__new__(cls, (user, system, idle))
        obj.idle = idle
        return obj


def test_cpu_is_measured_across_threads(monkeypatch):
    samples = iter([[FakeTimes(0, 0, 0)] * 2,          # taken when the Sampler is created
                    [FakeTimes(30, 10, 60)] * 2,       # 100 ticks later: 40% busy
                    [FakeTimes(30, 10, 160)] * 2])     # 100 more ticks, all idle
    monkeypatch.setattr(psutil, "cpu_times", lambda percpu=False: next(samples))
    s = Sampler()
    assert s.cpu_per_core() == [40.0, 40.0]
    result = []
    t = threading.Thread(target=lambda: result.append(s.cpu_per_core()))   # a different thread than the one before
    t.start()
    t.join()
    assert result == [[0.0, 0.0]]


def test_cpu_handles_a_changed_core_count_and_no_elapsed_time(monkeypatch):
    seq = iter([[FakeTimes(1, 1, 1)], [FakeTimes(1, 1, 1)] * 2, [FakeTimes(1, 1, 1)] * 2])
    monkeypatch.setattr(psutil, "cpu_times", lambda percpu=False: next(seq))
    s = Sampler()
    assert s.cpu_per_core() == [0.0, 0.0]
    assert s.cpu_per_core() == [0.0, 0.0]


def test_game_is_asked_at_most_every_ttl_seconds(make_server, monkeypatch):
    """The dashboard samples every second, but tick/player queries open a console connection: only repeat after GAME_TTL."""
    from mcctl import core, resources
    s = make_server()
    calls = []

    def fake_run(self, cmd, wait=1.5):
        calls.append(cmd)
        return ("The average tick time is: Average time per tick: 4.0ms" if cmd == "tick query"
                else "There are 1 of a max of 20 players online: Steve")
    monkeypatch.setattr(core.Server, "run", fake_run)
    monkeypatch.setattr(core.Server, "state", lambda self, sessions=None: "running")
    monkeypatch.setattr(core.Server, "java_pid", lambda self: None)
    clock = [1000.0]
    monkeypatch.setattr(resources.time, "monotonic", lambda: clock[0])
    sampler = Sampler()
    first = sampler.server(s)
    assert calls == ["tick query", "list"] and first.mspt == 4.0 and first.players == ["Steve"] and first.max_players == 20
    clock[0] += 1.0                                                  # next dashboard refresh, 1 s later
    again = sampler.server(s)
    assert len(calls) == 2 and again.tps == first.tps and again.players == ["Steve"]   # served from the cache
    clock[0] += 1.5                                                  # now older than GAME_TTL
    sampler.server(s)
    assert len(calls) == 4
    monkeypatch.setattr(core.Server, "state", lambda self, sessions=None: "stopped")
    assert sampler.server(s).tps is None and s.name not in sampler._game     # stopped servers aren't remembered


def test_load_colours_shift_smoothly_from_green_through_yellow_to_red():
    from mcctl.tui.widgets import BAD, GOOD, YELLOW, lag_color, level_color
    assert level_color(0, 70, 90) == GOOD and level_color(35, 70, 90) == GOOD        # calm: plain green
    assert level_color(70, 70, 90) == YELLOW and level_color(90, 70, 90) == BAD        # the thresholds are exact stops
    assert level_color(100, 70, 90) == BAD
    ramp = [level_color(p, 70, 90) for p in range(0, 101, 5)]
    assert len(set(ramp)) >= 8, "it should blend, not jump between three colours"

    def red_minus_green(c):                      # green has more green than red; red the opposite: should only ever fall in green
        return int(c[1:3], 16) - int(c[3:5], 16)
    assert red_minus_green(level_color(55, 70, 90)) > red_minus_green(GOOD)           # greener -> yellower
    assert lag_color(20) == GOOD and lag_color(18) == YELLOW and lag_color(12) == BAD and lag_color(6) == BAD
    assert lag_color(15) not in (GOOD, YELLOW, BAD)                                    # between 18 and 12 it is a blend


def test_heat_sparkline_colours_each_bar_by_its_own_value():
    import asyncio
    from textual.app import App
    from mcctl.tui.widgets import GOOD, BAD, HeatSparkline, level_color

    class T(App):
        def compose(self):
            yield HeatSparkline(id="h")

    async def go():
        app = T()
        async with app.run_test(size=(60, 6)) as pilot:
            h = app.query_one("#h", HeatSparkline)
            h.set([5, 50, 100], top=100, color_fn=lambda v: level_color(v, 70, 90))
            await pilot.pause(0.2)
            strips = app.screen._compositor.render_strips()
            colours = []
            for seg in strips[1]:           # the lower row holds every bar
                if seg.text.strip():
                    colours.append(seg.style.color.get_truecolor().hex)
            assert colours[0] == GOOD and colours[-1] == BAD and len(set(colours)) == 3
    asyncio.run(go())


def test_sampling_many_servers_asks_tmux_once_and_caches_slow_lookups(make_server, monkeypatch):
    """The idle dashboard used to spawn ~6 subprocesses a second (a tmux per server, ipconfig x3, netstat)."""
    from mcctl import core, platform
    make_server("a")
    make_server("b", port=25570)
    spawned = []

    def fake_tmux(*args, capture=False):
        spawned.append(args)
        class R:
            returncode, stdout = 0, "mc-a\nother\n"
        return R()
    monkeypatch.setattr(core, "tmux", fake_tmux)
    names = core.session_names()
    assert names == {"mc-a", "other"} and len(spawned) == 1
    a, b = core.Server("a"), core.Server("b")
    assert a.up(names) and not b.up(names) and len(spawned) == 1            # answered from the one listing

    runs = []
    monkeypatch.setattr(platform, "_vpn_active", lambda: runs.append(1) or True)
    monkeypatch.setattr(platform, "_lan_ip", lambda: runs.append(2) or "10.0.0.5")
    platform._ttl_cache.clear()
    for _ in range(5):
        assert platform.vpn_active() and platform.lan_ip() == "10.0.0.5"
    assert sorted(runs) == [1, 2]                                           # each real lookup ran once, not five times
