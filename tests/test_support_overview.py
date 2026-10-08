"""Diagnostics redaction and the redesigned Overview (tiles, alert banner, events, all-servers table)."""
import asyncio


from mcctl import agent, events, support
from mcctl.tui import app as tui
from test_tui import ready


def test_support_bundle_strips_secrets(make_server, monkeypatch):
    s = make_server()
    s.log_path.parent.mkdir(parents=True, exist_ok=True)
    key = "A" * 43 + "="
    s.log_path.write_text("[10:00:00] [Server thread/INFO]: hello\n"
                          f"[10:00:01] [Server thread/INFO]: WIREGUARD_PRIVATE_KEY={key}\n"
                          "[10:00:02] [Server thread/INFO]: Steve issued server command: /register hunter2 hunter2\n"
                          "[10:00:03] [Server thread/INFO]: token=abc123 url https://discord.com/api/webhooks/1/secretpart\n")
    events.add(s.name, "start", "started")
    text = support.bundle()
    assert "hello" in text and s.name in text and "== Health check ==" in text and "start" in text
    for secret in (key, "hunter2", "abc123", "secretpart"):
        assert secret not in text
    assert "<key>" in text and "<discord webhook>" in text and "/register <hidden>" in text


def test_redact_keeps_normal_text():
    assert support.redact("Steve joined the game") == "Steve joined the game"
    assert "~" in support.redact(str(support.Path.home()) + "/x")


def test_overview_shows_tiles_events_alerts_and_the_fleet(make_server):
    make_server("pytest-a")
    make_server("pytest-b", port=25570)
    events.add("pytest-a", "backup", "world.zip (1.0 MB, verified)")
    events.add("pytest-a", "crash", "ran out of memory")
    agent.save_state({"alerts": {"lag:pytest-a": {"active": True, "announced": True},
                                 "ram:pytest-b": {"active": True, "announced": True}}, "vpn_lost": False})

    async def go():
        app = tui.Dashboard(target="pytest-a")
        async with app.run_test(size=(150, 60)) as pilot:
            await ready(app, pilot)
            app.action_tab("overview")
            await pilot.pause(1.5)
            pane = app.pane("overview")
            assert "Status" in app.query_one("#o-status").border_title and "stopped" in str(app.query_one("#o-status").content)
            banner = app.query_one("#ov-alerts")
            assert banner.has_class("show") and "pytest-a" in str(app.query_one("#ov-alerts-text").content)
            assert "pytest-b" not in str(app.query_one("#ov-alerts-text").content)      # another server's alert
            lines = [ln.text for ln in app.query_one("#ov-events").lines]
            assert any("ran out of memory" in ln for ln in lines) and any("verified" in ln for ln in lines)
            fleet = app.query_one("#ov-fleet")
            assert fleet.display and fleet.row_count == 2
            assert pane.selected_server() in ("pytest-a", "pytest-b")
            app.query_one("#ov-address").display                                          # exists; disabled without an address
            assert app.query_one("#ov-address").disabled
    asyncio.run(go())


def test_single_server_hides_the_fleet_table(make_server):
    make_server("pytest-solo")

    async def go():
        app = tui.Dashboard(target="pytest-solo")
        async with app.run_test(size=(150, 60)) as pilot:
            await ready(app, pilot)
            app.action_tab("overview")
            await pilot.pause(1.2)
            assert not app.query_one("#ov-fleet").display
            await pilot.press("f5")
            await pilot.pause(0.3)
    asyncio.run(go())


def test_narrow_terminal_gets_the_narrow_class(make_server):
    make_server("pytest-narrow")

    async def go():
        app = tui.Dashboard(target="pytest-narrow")
        async with app.run_test(size=(100, 40)) as pilot:
            await ready(app, pilot)
            await pilot.pause(0.3)
            assert app.screen.has_class("-narrow")
        app2 = tui.Dashboard(target="pytest-narrow")
        async with app2.run_test(size=(150, 40)) as pilot:
            await ready(app2, pilot)
            await pilot.pause(0.3)
            assert not app2.screen.has_class("-narrow")
    asyncio.run(go())
