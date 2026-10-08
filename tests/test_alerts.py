"""Alert rules (debounce, cooldown, resolve), VPN address changes, and delivery incl. the Discord webhook."""
import http.server
import json
import threading

import pytest

from mcctl import alerts, core, events
from mcctl.core import Fail


def row(name, mspt=0.0, ram=0.0):
    return {"servers": {name: {"mspt": mspt, "ram": ram, "cpu": 1, "players": 0}}}


def texts(msgs):
    return [(lvl, txt) for lvl, _, txt in msgs]


def test_lag_alert_needs_a_minute_then_cooldown_then_resolves(make_server):
    s = make_server()
    state, laggy, fine = {}, row(s.name, mspt=100.0), row(s.name, mspt=5.0)         # 100 ms = 10 TPS
    assert alerts.check(state, laggy, [s], 0, disk_free_gb=100) == []               # just started being bad
    assert alerts.check(state, laggy, [s], 59, disk_free_gb=100) == []
    first = alerts.check(state, laggy, [s], 60, disk_free_gb=100)
    assert texts(first)[0][0] == "alert" and "10 TPS" in first[0][2] and first[0][1] == s.name
    assert alerts.check(state, laggy, [s], 300, disk_free_gb=100) == []              # still bad: not repeated
    assert alerts.check(state, fine, [s], 301, disk_free_gb=100) == []               # fine, but not for a minute yet
    done = alerts.check(state, fine, [s], 362, disk_free_gb=100)
    assert texts(done) == [("resolved", f"'{s.name}' is running smoothly again")]
    assert alerts.check(state, fine, [s], 500, disk_free_gb=100) == []


def test_a_blip_does_not_alert_and_unannounced_alerts_do_not_resolve(make_server):
    s = make_server()
    state = {}
    alerts.check(state, row(s.name, mspt=100), [s], 0, disk_free_gb=100)
    assert alerts.check(state, row(s.name, mspt=5), [s], 30, disk_free_gb=100) == []   # recovered within the minute
    assert state["alerts"][f"lag:{s.name}"].get("bad_since") is None


def test_flapping_inside_the_cooldown_is_silent(make_server):
    s = make_server()
    state, bad, ok = {}, row(s.name, mspt=100), row(s.name, mspt=5)
    alerts.check(state, bad, [s], 0, disk_free_gb=100)
    assert alerts.check(state, bad, [s], 60, disk_free_gb=100)                         # alert
    alerts.check(state, ok, [s], 100, disk_free_gb=100)
    assert alerts.check(state, ok, [s], 170, disk_free_gb=100)                         # resolved
    alerts.check(state, bad, [s], 200, disk_free_gb=100)
    assert alerts.check(state, bad, [s], 270, disk_free_gb=100) == []                  # inside the 30 min cooldown
    assert alerts.check(state, ok, [s], 300, disk_free_gb=100) == []
    assert alerts.check(state, ok, [s], 370, disk_free_gb=100) == []                   # silent alert → silent resolve


def test_ram_and_disk_rules(make_server):
    s = make_server(ram_gb=4)
    state = {}
    r = row(s.name, mspt=2, ram=3.8)                                                   # 95 % of 4 GB
    assert alerts.check(state, r, [s], 0, disk_free_gb=100) == []
    msgs = alerts.check(state, r, [s], 120, disk_free_gb=100)
    assert texts(msgs)[0][0] == "alert" and "3.8 of its 4 GB" in msgs[0][2]
    disk = alerts.check({}, row(s.name), [s], 0, disk_free_gb=2.0)                      # disk alerts immediately
    assert disk and disk[0][1] is None and "2.0 GB" in disk[0][2]
    assert alerts.check({}, row(s.name, ram=3.0), [s], 500, disk_free_gb=100) == []


def test_vpn_address_lost_changed_and_back(monkeypatch):
    monkeypatch.setattr(alerts.vpn, "enabled", lambda: True)
    state = {}
    assert alerts.check_vpn(state, 1000, "1.1.1.1:1000") == []                         # first sighting
    assert alerts.check_vpn(state, 1100, "9.9.9.9:9") == []                            # not checked again within 5 minutes
    changed = alerts.check_vpn(state, 1400, "2.2.2.2:2000")
    assert texts(changed)[0][0] == "info" and "2.2.2.2:2000" in changed[0][2]
    lost = alerts.check_vpn(state, 1800, "")
    assert texts(lost)[0][0] == "alert"
    assert alerts.check_vpn(state, 2200, "") == []                                      # not repeated while lost
    back = alerts.check_vpn(state, 2600, "3.3.3.3:3000")
    assert texts(back)[0][0] == "resolved"
    monkeypatch.setattr(alerts.vpn, "enabled", lambda: False)
    assert alerts.check_vpn({}, 99999, "x") == []


def test_webhook_url_must_be_discord_and_is_stored_owner_only():
    for bad in ("", "http://discord.com/api/webhooks/1/x", "https://evil.example/hook", "ftp://discord.com/x"):
        assert not alerts.webhook_ok(bad)
    assert alerts.webhook_ok("https://discord.com/api/webhooks/1/abc")
    with pytest.raises(Fail):
        alerts.set_webhook("https://evil.example/hook")
    alerts.set_webhook("https://discord.com/api/webhooks/1/abc")
    assert core.setting("webhook_url").endswith("/abc")
    assert oct(core.SETTINGS_FILE.stat().st_mode & 0o777) == "0o600"
    alerts.set_webhook("off")
    assert core.setting("webhook_url") is None


def test_deliver_notifies_logs_an_event_and_posts_to_the_webhook(monkeypatch):
    got = []

    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(alerts, "ALLOW_HTTP", True)
    core.save_setting("webhook_url", f"http://127.0.0.1:{server.server_port}/hook")
    notified = []
    monkeypatch.setattr(alerts.platform, "notify", lambda t, m: notified.append((t, m)))
    logged = []
    alerts.deliver("alert", "a", "'a' is lagging", log=logged.append)
    server.shutdown()
    assert notified == [("[!] mc alert", "'a' is lagging")] and logged
    assert got and "'a' is lagging" in got[0]["content"]
    assert events.recent(1)[0]["kind"] == "alert"


def test_deliver_never_raises_when_everything_fails(monkeypatch):
    monkeypatch.setattr(alerts.platform, "notify", lambda *a: (_ for _ in ()).throw(OSError("no display")))
    core.save_setting("webhook_url", "https://discord.com/api/webhooks/1/abc")
    monkeypatch.setattr(alerts.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    alerts.deliver("alert", None, "x")                                                  # must not raise
    assert not alerts.send_webhook("x")
    core.save_setting("webhook_joins", False)
    assert not alerts.send_webhook("joined", joins=True)                                # joins are opt-in
