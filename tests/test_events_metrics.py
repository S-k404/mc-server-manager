"""Event log, metrics history, and the hooks that feed them."""
import time
from types import SimpleNamespace as NS

from mcctl import agent, backups, events, metrics


def test_events_roundtrip_filter_and_order():
    events.add("a", "start", "started", when=100)
    events.add("b", "stop", "stopped", when=101)
    events.add("a", "backup", "world.zip", when=102)
    assert [e["kind"] for e in events.recent(10)] == ["start", "stop", "backup"]
    assert [e["kind"] for e in events.recent(10, server="a")] == ["start", "backup"]
    assert [e["kind"] for e in events.recent(10, kinds={"stop"})] == ["stop"]
    assert [e["kind"] for e in events.recent(2)] == ["stop", "backup"]            # newest two, oldest first
    events.path().write_text(events.path().read_text() + "not json\n")
    assert len(events.recent(10)) == 3                                            # a corrupt line is skipped


def test_events_file_is_trimmed(monkeypatch):
    monkeypatch.setattr(events, "MAX_BYTES", 2000)
    monkeypatch.setattr(events, "KEEP", 10)
    for i in range(100):
        events.add("a", "info", f"event {i}", when=i)
    rows = events.recent(1000)
    assert 0 < len(rows) < 100 and rows[-1]["text"] == "event 99"


def test_backup_writes_an_event(make_server):
    s = make_server()
    (s.dir / "world").mkdir()
    (s.dir / "world" / "level.dat").write_bytes(b"x")
    backups.make_backup(s)
    assert [e["kind"] for e in events.recent(5, server=s.name)] == ["backup"]


def test_joins_and_leaves_are_logged_even_without_notifications(make_server, monkeypatch):
    s = make_server()
    s.log_path.parent.mkdir(parents=True, exist_ok=True)
    s.log_path.write_text("")
    st = {"log_pos": 0}
    s.log_path.write_text("[12:00:00] [Server thread/INFO]: Steve joined the game\n"
                          "[12:05:00] [Server thread/INFO]: Steve left the game\n")
    sent = []
    monkeypatch.setattr(agent, "notify", lambda *a: sent.append(a))
    agent.watch_joins(s, st)
    assert [e["kind"] for e in events.recent(5, server=s.name)] == ["join", "leave"] and not sent   # notify_joins is off
    assert st["log_pos"] == s.log_path.stat().st_size


def fake_sampler(cpu, ram):
    return NS(system=lambda: NS(cpu_percent=cpu, ram_percent=ram, net_down_kbs=5.0),
              server=lambda s: NS(cpu_percent=cpu / 2, ram_gb=2.5, mspt=3.0, players=["a", "b"]))


def test_metrics_series_buckets_and_carries_gaps(make_server):
    s = make_server()
    now = 10_000.0
    for i, cpu in enumerate((10, 20, 30, 40)):
        metrics.append(metrics.row(fake_sampler(cpu, 50), [s], now - 3600 + 900 * i + 1))
    cpu = metrics.series("sys", "cpu", 3600, points=4, now=now)
    assert cpu == [10, 20, 30, 40]
    assert metrics.series("server", "players", 3600, s.name, points=4, now=now) == [2, 2, 2, 2]
    sparse = metrics.series("sys", "cpu", 3600, points=8, now=now)             # empty buckets repeat the last value
    assert sparse[0] == 10 and sparse[-1] == 40 and len(sparse) == 8
    assert metrics.series("sys", "cpu", 3600, points=4, now=now + 99999) == []   # nothing that recent


def test_metrics_prune_and_spark(make_server):
    s = make_server()
    now = 5_000_000.0
    metrics.append(metrics.row(fake_sampler(1, 1), [s], now - 40 * 86400))
    metrics.append(metrics.row(fake_sampler(2, 2), [s], now - 86400))
    metrics.prune(now)
    assert len(metrics.load(10**9, now)) == 1
    assert metrics.spark([]) == "" and metrics.spark([1, 1, 1]) == "▁▁▁"
    line = metrics.spark(list(range(200)), width=20)
    assert len(line) == 20 and line[0] == "▁" and line[-1] == "█"


def test_agent_records_metrics_after_the_first_baseline_tick(make_server, monkeypatch):
    make_server()
    monkeypatch.setattr(agent, "_sampler", None)
    state = {}
    agent.record_metrics(state, time.time())            # first call only creates the baseline
    assert metrics.load(10**9) == []
    agent.record_metrics(state, time.time())
    rows = metrics.load(10**9)
    assert len(rows) == 1 and "pytest-a" in rows[0]["servers"]
