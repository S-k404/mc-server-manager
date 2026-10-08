"""Restart/stop countdowns: warnings at the right moments, cancellable, never blocking the agent."""
import pytest

from mcctl import agent, countdown, events
from mcctl.core import Fail


def clockwork(start=1000.0):
    t = [start]
    return (lambda: t[0]), (lambda d: t.__setitem__(0, t[0] + d)), t


def test_parse_delay_and_label():
    assert countdown.parse_delay("90s") == 90 and countdown.parse_delay("5m") == 300 and countdown.parse_delay("2h") == 7200
    assert countdown.parse_delay("5") == 300                       # plain numbers are minutes
    for bad in ("", "abc", "2s", "30h", "-5m"):
        with pytest.raises(Fail):
            countdown.parse_delay(bad)
    assert countdown.label(300) == "5 minutes" and countdown.label(60) == "1 minute" and countdown.label(90) == "1 minute 30 seconds"
    assert countdown.label(10) == "10 seconds" and countdown.label(1) == "1 second"


def test_countdown_warns_then_acts(make_server, monkeypatch):
    s = make_server()
    plan = countdown.schedule(s, "restart", 300, spawn=False, now=1000.0)
    said = []
    monkeypatch.setattr(countdown, "announce", lambda srv, action, secs: said.append(secs))
    clock, sleep, _ = clockwork()
    did = []
    assert countdown.run(s, plan["id"], sleep=sleep, clock=clock, do=lambda srv: did.append(srv.name)) == "done"
    assert said == [300, 120, 60, 30, 15, 10, 5, 3, 2, 1]          # no "10 minutes" for a 5 minute countdown
    assert did == [s.name] and countdown.pending(s) is None
    assert [e["kind"] for e in events.recent(5, server=s.name)] == ["restart"]


def test_short_countdown_announces_its_own_length_once(make_server, monkeypatch):
    s = make_server()
    plan = countdown.schedule(s, "stop", 45, spawn=False, now=1000.0)
    said = []
    monkeypatch.setattr(countdown, "announce", lambda srv, action, secs: said.append(secs))
    clock, sleep, _ = clockwork()
    countdown.run(s, plan["id"], sleep=sleep, clock=clock, do=lambda srv: None)
    assert said[0] == 45 and said[1:] == [30, 15, 10, 5, 3, 2, 1]


def test_cancel_stops_the_countdown_and_nothing_runs(make_server, monkeypatch):
    s = make_server()
    plan = countdown.schedule(s, "restart", 120, spawn=False, now=1000.0)
    monkeypatch.setattr(countdown, "announce", lambda *a: None)
    clock, sleep, t = clockwork()
    did = []

    def cancelling_sleep(d):
        sleep(d)
        if t[0] > 1010:
            countdown.cancel(s, say=False)
    assert countdown.run(s, plan["id"], sleep=cancelling_sleep, clock=clock, do=lambda srv: did.append(1)) == "cancelled"
    assert not did and countdown.pending(s) is None
    with pytest.raises(Fail, match="no countdown"):
        countdown.cancel(s)


def test_one_countdown_at_a_time_and_stale_ids_do_nothing(make_server):
    s = make_server()
    plan = countdown.schedule(s, "restart", 60, spawn=False)
    with pytest.raises(Fail, match="already"):
        countdown.schedule(s, "stop", 60, spawn=False)
    assert countdown.run(s, "not-the-id") == "cancelled" and countdown.pending(s)["id"] == plan["id"]
    with pytest.raises(Fail):
        countdown.schedule(s, "explode", 60, spawn=False)


def test_agent_scheduled_restart_uses_a_countdown_instead_of_sleeping(make_server, monkeypatch):
    from datetime import datetime
    s = make_server(schedules={"restart_at": "05:00"})
    monkeypatch.setattr(agent.servers, "start", lambda *a, **k: None)
    monkeypatch.setattr(type(s), "state", lambda self: "running")
    monkeypatch.setattr(agent.backups, "make_backup", lambda *a, **k: None)
    scheduled = []
    monkeypatch.setattr(agent.countdown, "schedule", lambda srv, action, secs: scheduled.append((srv.name, action, secs)))
    monkeypatch.setattr(agent.time, "sleep", lambda d: (_ for _ in ()).throw(AssertionError("the agent must not sleep")))
    now = datetime.now().replace(hour=5, minute=2, second=0).timestamp()
    state = {}
    agent.reconcile(s, state.setdefault("servers", {}).setdefault(s.name, {}), False, now)
    assert scheduled == [(s.name, "restart", 60)]
