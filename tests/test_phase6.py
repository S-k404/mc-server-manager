"""Phase 6: the status bar's network end, the first-run question, the VPN's exit city, and the latency shown next to the address."""
import asyncio
import json
import os
import stat
import time

import pytest

from mcctl import container as dk
from mcctl import core, platform, share, speedtest, vpn
from mcctl.core import Fail
from mcctl.tui import app as tui
from mcctl.tui.modals import AskText, ShareChoice
from mcctl.tui.more_panes import net_segments
from test_share import LAN, forwarding, mode, rendered_lines, run_cli

KEY = "A" * 43 + "="
ADDRESS = "203.0.113.2:54321"


@pytest.fixture
def on_wifi(monkeypatch):
    """A Wi-Fi address and no Mac-wide VPN (what the dashboard tests need to behave the same on any machine)."""
    monkeypatch.setattr(platform, "lan_ip", lambda: LAN)
    monkeypatch.setattr(platform, "vpn_active", lambda: False)


def text_of(segments):
    return " ".join(t for t, _ in segments)


# ---------- the exit city ----------
def test_setup_saves_a_city_next_to_the_country_and_keeps_it(tmp_path):
    vpn.setup(KEY, "Netherlands", "Amsterdam")
    text = vpn.ENV_FILE.read_text()
    assert "SERVER_COUNTRIES=Netherlands\n" in text and "SERVER_CITIES=Amsterdam\n" in text and "WIREGUARD_PRIVATE_KEY=" + KEY in text
    assert stat.S_IMODE(os.stat(vpn.ENV_FILE).st_mode) == 0o600
    assert vpn.place() == ("Netherlands", "Amsterdam")
    vpn.setup(KEY)                                                   # nothing said: what was chosen stays
    assert vpn.place() == ("Netherlands", "Amsterdam")
    vpn.setup(KEY, None, "any")                                      # 'any' (or empty) means no preference
    assert vpn.place() == ("Netherlands", None) and "SERVER_CITIES" not in vpn.ENV_FILE.read_text()


def test_no_country_is_assumed_until_one_is_chosen(capsys):
    assert vpn.place() == (None, None)                              # nothing saved yet, and nobody's home country is guessed
    assert vpn.describe_place(None, None) == "any country"
    vpn.setup(KEY)
    text = vpn.ENV_FILE.read_text()
    assert "SERVER_COUNTRIES" not in text and "WIREGUARD_PRIVATE_KEY=" + KEY in text      # gluetun then picks any server
    out = capsys.readouterr().out
    assert "exit: any country" in out and "mc vpn setup --country" in out                 # and it says how to choose
    vpn.setup(KEY, "Germany")
    assert vpn.place() == ("Germany", None) and "SERVER_COUNTRIES=Germany\n" in vpn.ENV_FILE.read_text()
    vpn.setup(KEY, "any")                                           # 'any' goes back to no preference, like the city
    assert vpn.place() == (None, None) and "SERVER_COUNTRIES" not in vpn.ENV_FILE.read_text()
    assert vpn.describe_place("Germany", None) == "Germany" and vpn.describe_place("Germany", "Germany") == "Germany, Germany"
    assert vpn.describe_place("Netherlands", "Amsterdam") == "Amsterdam, Netherlands"


def test_changing_only_the_place_can_clear_the_country(capsys):
    vpn.setup(KEY, "Netherlands", "Amsterdam")
    assert vpn.set_place("any", None) == (None, "Amsterdam")           # the city stays, the country goes
    assert "SERVER_COUNTRIES" not in vpn.ENV_FILE.read_text() and "SERVER_CITIES=Amsterdam\n" in vpn.ENV_FILE.read_text()
    assert vpn.set_place("Germany", "any") == ("Germany", None)
    assert "The VPN will exit in Germany (any city)" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["Amsterdam\nWIREGUARD_PRIVATE_KEY=x", "Amsterdam=1", "123", "-Amsterdam", "x" * 80, "Amster;dam"])
def test_a_place_name_that_could_change_the_env_file_is_refused(bad):
    vpn.setup(KEY, "Netherlands", "Amsterdam")
    before = vpn.ENV_FILE.read_text()
    with pytest.raises(Fail, match="city name"):
        vpn.set_place(None, bad)
    with pytest.raises(Fail, match="country name"):
        vpn.set_place(bad, None)
    with pytest.raises(Fail, match="city name"):
        vpn.setup(KEY, None, bad)
    assert vpn.ENV_FILE.read_text() == before                        # a refused name never touches the file
    with pytest.raises(Fail):
        vpn._clean_place("   ", "city")                              # (a blank city given to set_place/setup means 'any' instead)


def test_ordinary_and_non_english_place_names_are_accepted():
    for name in ("Amsterdam", "New Delhi", "Sao Paulo", "Zurich", "St. John's", "Frankfurt am Main", "Zürich"):
        assert vpn._clean_place(name, "city") == name
    assert vpn._clean_place("  New    Delhi ", "city") == "New Delhi"


def test_set_place_changes_only_the_place_and_needs_a_saved_key():
    with pytest.raises(Fail, match="mc vpn setup"):
        vpn.set_place(None, "Rotterdam")
    vpn.setup(KEY, "Netherlands", "Amsterdam")
    assert vpn.set_place(None, "Rotterdam") == ("Netherlands", "Rotterdam")
    assert "WIREGUARD_PRIVATE_KEY=" + KEY in vpn.ENV_FILE.read_text()
    assert stat.S_IMODE(os.stat(vpn.ENV_FILE).st_mode) == 0o600
    assert vpn.set_place("Germany", None) == ("Germany", "Rotterdam")  # a city is kept unless it is changed on purpose
    assert vpn.set_place(None, "any") == ("Germany", None)


def test_set_place_refuses_an_env_file_without_a_key():
    vpn.setup(KEY)
    vpn.ENV_FILE.write_text("WIREGUARD_PRIVATE_KEY=\nSERVER_COUNTRIES=Netherlands\n")
    with pytest.raises(Fail, match="no key"):
        vpn.set_place(None, "Amsterdam")


def test_the_city_reaches_the_vpn_container_through_its_env_file(make_server, monkeypatch):
    make_server("a")
    vpn.setup(KEY, "Netherlands", "Amsterdam")
    ran = []
    monkeypatch.setattr(dk, "require", lambda: None)
    monkeypatch.setattr(dk, "ensure_network", lambda: None)
    monkeypatch.setattr(dk, "pull", lambda image, say=None: None)
    monkeypatch.setattr(dk, "remove", lambda name: None)
    monkeypatch.setattr(dk, "host_gateway_ip", lambda: "192.168.65.254")
    monkeypatch.setattr(dk, "run", lambda *a: ran.append(a))
    monkeypatch.setattr(dk, "available", lambda: False)
    vpn.start(wait=False)
    gluetun = next(a for a in ran if a[a.index("--name") + 1] == vpn.VPN)
    env_files = [gluetun[i + 1] for i, a in enumerate(gluetun) if a == "--env-file"]
    assert str(vpn.ENV_FILE) in env_files and "SERVER_CITIES=Amsterdam" in vpn.ENV_FILE.read_text()
    assert "Amsterdam" not in " ".join(gluetun)                        # a place is never typed on the command line


def test_cli_setup_with_only_a_city_reuses_the_saved_key_and_never_prompts():
    vpn.setup(KEY, "Netherlands")
    result = run_cli("vpn", "setup", "--city", "Amsterdam")
    assert result.exit_code == 0 and "ProtonVPN WireGuard private key" not in result.output
    assert vpn.place() == ("Netherlands", "Amsterdam") and "WIREGUARD_PRIVATE_KEY=" + KEY in vpn.ENV_FILE.read_text()
    assert run_cli("vpn", "setup", "--city", "any").exit_code == 0 and vpn.place() == ("Netherlands", None)


def test_cli_setup_for_the_first_time_asks_for_the_key_and_a_country():
    from typer.testing import CliRunner
    from mcctl.cli import app
    result = CliRunner().invoke(app, ["vpn", "setup", "--city", "Amsterdam"], input=KEY + "\nNetherlands\n")
    assert result.exit_code == 0, result.output
    assert "Exit country" in result.output
    assert vpn.configured() and vpn.place() == ("Netherlands", "Amsterdam") and vpn.enabled()


def test_cli_setup_accepts_an_empty_country_and_then_nobody_s_home_country_is_assumed():
    from typer.testing import CliRunner
    from mcctl.cli import app
    result = CliRunner().invoke(app, ["vpn", "setup"], input=KEY + "\n\n")
    assert result.exit_code == 0, result.output
    assert vpn.configured() and vpn.place() == (None, None)
    assert "mc vpn setup --country" in " ".join(result.output.split())            # and it says how to choose later


def test_cli_setup_does_not_ask_for_a_country_when_one_is_given_or_saved():
    from typer.testing import CliRunner
    from mcctl.cli import app
    result = CliRunner().invoke(app, ["vpn", "setup", "--country", "Germany"], input=KEY + "\n")
    assert result.exit_code == 0 and "Exit country" not in result.output and vpn.place() == ("Germany", None)
    result = CliRunner().invoke(app, ["vpn", "setup"], input=KEY + "\n")           # Germany is saved: not asked again
    assert result.exit_code == 0 and "Exit country" not in result.output and vpn.place() == ("Germany", None)


def test_vpn_start_says_how_to_choose_a_country_when_none_is_chosen(make_server, monkeypatch, capsys):
    make_server("a")
    vpn.setup(KEY)
    for name, value in (("require", lambda: None), ("ensure_network", lambda: None), ("pull", lambda image, say=None: None),
                        ("remove", lambda name: None), ("host_gateway_ip", lambda: "192.168.65.254"),
                        ("run", lambda *a: None), ("available", lambda: False), ("refresh", lambda *a, **k: None)):
        monkeypatch.setattr(dk, name, value)
    capsys.readouterr()
    vpn.start(wait=False)
    assert "mc vpn setup --country" in " ".join(capsys.readouterr().out.split())


def test_cli_setup_refuses_a_bad_city_and_leaves_the_file_alone():
    vpn.setup(KEY, "Netherlands", "Amsterdam")
    before = vpn.ENV_FILE.read_text()
    result = run_cli("vpn", "setup", "--city", "Amsterdam=evil")
    assert result.exit_code != 0 and isinstance(result.exception, Fail)
    assert vpn.ENV_FILE.read_text() == before


def test_cli_vpn_status_names_the_exit_and_the_latency(monkeypatch):
    vpn.setup(KEY, "Netherlands", "Amsterdam")
    vpn.remember_latency(ADDRESS, 45)
    monkeypatch.setattr(vpn, "status", lambda: {
        "docker": True, "configured": True, "containers": {vpn.VPN: "running", vpn.FORWARD: "running"},
        "vpn_health": "healthy", "forward_server": "pytest-a", "public": ADDRESS, "proxy_up": True,
        "exit": "Amsterdam, NL (203.0.113.2)"})
    out = " ".join(run_cli("vpn").output.split())                  # one line: the terminal may wrap a long status line
    assert "set to exit in Amsterdam, Netherlands" in out and ADDRESS in out and "45 ms (good)" in out


# ---------- latency next to the address ----------
def test_a_measured_latency_is_remembered_for_that_address_only():
    assert vpn.last_latency() is None
    vpn.remember_latency(ADDRESS, 45.04)
    assert vpn.last_latency(ADDRESS)["ms"] == 45.0
    assert vpn.last_latency("203.0.113.9:1111") is None              # the port/IP changed: the old number says nothing now
    assert vpn.last_latency()["ms"] == 45.0                          # asking without an address takes whatever was last measured


def test_an_old_or_odd_latency_record_is_ignored():
    vpn.LATENCY_FILE.write_text(json.dumps({"address": ADDRESS, "ms": 45, "at": time.time() - 2 * 86400}))
    assert vpn.last_latency(ADDRESS) is None                         # more than a day old
    vpn.LATENCY_FILE.write_text(json.dumps({"address": ADDRESS, "ms": 45, "at": time.time() + 3600}))
    assert vpn.last_latency(ADDRESS) is None                         # from the future (clock change)
    for junk in ("not json", "[]", json.dumps({"address": ADDRESS}), json.dumps({"ms": "x", "at": 1})):
        vpn.LATENCY_FILE.write_text(junk)
        assert vpn.last_latency(ADDRESS) is None
    vpn.remember_latency(ADDRESS, "nonsense")                        # a bad value must not raise (best effort)


def test_the_latency_sentence_says_how_good_and_how_old():
    assert speedtest.address_latency(ADDRESS) is None
    vpn.remember_latency(ADDRESS, 45)
    assert speedtest.address_latency(ADDRESS) == "45 ms (good), measured just now"
    vpn.LATENCY_FILE.write_text(json.dumps({"address": ADDRESS, "ms": 130, "at": time.time() - 185}))
    assert speedtest.address_latency(ADDRESS) == "130 ms (laggy), measured 3m ago"


def test_the_speed_test_remembers_the_internet_row_only_and_only_when_it_answered(make_server, on_wifi, monkeypatch):
    s = make_server()
    monkeypatch.setattr(core.Server, "accepting", lambda self: True)
    monkeypatch.setattr(speedtest, "server_tick", lambda srv: None)
    monkeypatch.setattr(speedtest, "players_latency", lambda srv: [])
    monkeypatch.setattr(speedtest, "addresses", lambda srv: [("this Mac", "127.0.0.1", 25565, None),
                                                              (speedtest.INTERNET, "203.0.113.2", 54321, None)])
    answers = {"127.0.0.1": 2.0, "203.0.113.2": 45.0}
    monkeypatch.setattr(speedtest, "measure_address", lambda label, host, port, samples=8, timeout=2.0: {
        "label": label, "address": f"{host}:{port}", "samples": samples, "ok": 1,
        "ping": {"avg": answers[host], "min": 1, "max": 3, "jitter": 0}, "connect": None})
    speedtest.run(s, 1)
    assert vpn.last_latency(ADDRESS)["ms"] == 45.0                   # not the 2 ms to this Mac
    vpn.LATENCY_FILE.unlink()
    monkeypatch.setattr(speedtest, "measure_address", lambda label, host, port, samples=8, timeout=2.0: {
        "label": label, "address": f"{host}:{port}", "samples": samples, "ok": 0, "error": "timed out"})
    speedtest.run(s, 1)
    assert vpn.last_latency() is None                                # no answer, nothing to remember


def test_cli_address_shows_the_last_latency_test(monkeypatch):
    monkeypatch.setattr(vpn, "public_address", lambda live=False: ADDRESS)
    monkeypatch.setattr(platform, "copy_to_clipboard", lambda text: False)
    assert "Last latency test" not in run_cli("vpn", "address").output
    vpn.remember_latency(ADDRESS, 45)
    assert "Last latency test: 45 ms (good)" in run_cli("vpn", "address").output
    vpn.remember_latency("203.0.113.9:1", 45)                          # measured against an older address
    assert "Last latency test" not in run_cli("vpn", "address").output


# ---------- the status bar's network end ----------
def test_status_bar_hidden_mode_shows_the_vpn_address_and_the_proxy():
    net = {"tunnel": "docker", "tunnel_ok": True, "public": ADDRESS, "proxy_up": True, "share_mode": "hidden"}
    assert net_segments(net, False) == [("hidden", "bold"), (f"internet * {ADDRESS}", "green"), ("proxy up", "green")]
    net["proxy_up"] = False
    assert ("proxy down", "yellow") in net_segments(net, False)


def test_status_bar_hidden_mode_without_a_vpn_says_nobody_can_join():
    out = net_segments({"tunnel": "off", "share_mode": "hidden"}, False)
    assert "nobody can join" in text_of(out) and "proxy" not in text_of(out)
    waiting = net_segments({"tunnel": "docker", "tunnel_ok": True, "share_mode": "hidden"}, False)
    assert "no port yet" in text_of(waiting) and "proxy down" in text_of(waiting)
    down = net_segments({"tunnel": "docker", "tunnel_ok": False, "share_mode": "hidden"}, False)
    assert "VPN down" in text_of(down)


def test_status_bar_local_mode_shows_the_name_and_the_internet_address_when_there_is_one():
    assert net_segments({"tunnel": "off", "share_mode": "local", "share_host": "mc-ab12.local"}, False) \
        == [("local mc-ab12.local", "cyan")]
    out = net_segments({"tunnel": "docker", "public": ADDRESS, "share_mode": "local", "share_host": "mc-ab12.local",
                        "proxy_up": True}, False)
    assert text_of(out) == f"local mc-ab12.local internet * {ADDRESS} proxy up"
    assert "name not ready" in text_of(net_segments({"tunnel": "off", "share_mode": "local"}, False))


def test_status_bar_with_no_mode_chosen_is_as_it_was_plus_a_hint():
    assert net_segments({"tunnel": "off"}, False) == [("LAN only", "grey50"), ("share: not set", "grey62")]
    out = net_segments({"tunnel": "docker", "tunnel_ok": True, "public": ADDRESS}, False)
    assert text_of(out).startswith(f"internet * {ADDRESS} share: not set")


def test_status_bar_mac_vpn_warns_about_the_lan_only_where_it_matters():
    on = {"tunnel": "off", "proton_lan": False}
    assert ("Mac VPN on: LAN blocked", "bold red") in net_segments({**on, "share_mode": "local", "share_host": "mc-ab12.local"}, True)
    assert ("Mac VPN on: LAN blocked", "bold red") in net_segments(on, True)
    hidden = net_segments({**on, "tunnel": "docker", "share_mode": "hidden"}, True)
    assert ("Mac VPN on", "yellow") in hidden and "LAN blocked" not in text_of(hidden)    # hidden has no Wi-Fi friends to block
    allowed = net_segments({**on, "proton_lan": True}, True)
    assert ("Mac VPN on", "yellow") in allowed and "LAN blocked" not in text_of(allowed)
    assert "Mac VPN" not in text_of(net_segments(on, False))


# ---------- dashboard ----------
async def until(pilot, check, seconds=15):
    for _ in range(int(seconds / 0.1)):
        await pilot.pause(0.1)
        if check():
            return
    raise AssertionError("the dashboard did not get there")


async def open_dialog(app, pilot):
    """Wait until an AskText dialog is on screen with all its widgets mounted (they arrive in batches)."""
    await until(pilot, lambda: isinstance(app.screen, AskText) and app.screen.query("#ask-input") and app.screen.query("#ok"), 5)
    return app.screen


async def answer(app, pilot, text=None):
    """Type into the open AskText dialog (or leave it as it is), press OK, and wait until the dialog is gone."""
    dialog = await open_dialog(app, pilot)
    if text is not None:
        dialog.query_one("#ask-input").value = text
    dialog.query_one("#ok").press()
    await until(pilot, lambda: app.screen is not dialog, 5)


def docker_up(monkeypatch, proxy_up=True):
    forwarding(monkeypatch)
    monkeypatch.setattr(dk, "available", lambda: True)
    monkeypatch.setattr(vpn, "status", lambda: {
        "docker": True, "configured": True, "vpn_health": "healthy", "containers": {vpn.VPN: "running", vpn.FORWARD: "running"},
        "forward_server": "pytest-a", "public": ADDRESS, "exit": "Amsterdam, NL", "proxy_up": proxy_up})


def test_the_status_bar_on_every_tab_shows_the_mode_the_address_and_the_proxy(make_server, on_wifi, monkeypatch):
    from test_tui import ready
    s = make_server("pytest-bar")
    docker_up(monkeypatch)
    mode("hidden")

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(180, 50)) as pilot:
            await ready(app, pilot)
            await until(pilot, lambda: app._net is not None and app._net[1].get("public"))
            for tab in ("console", "network", "settings"):
                app.action_tab(tab)
                await pilot.pause(1.2)
                bar = next(line for line in rendered_lines(app) if "pytest-bar" in line and "STOPPED" in line)
                assert "hidden" in bar and f"internet * {ADDRESS}" in bar and "proxy up" in bar, (tab, bar)
                assert LAN not in bar and "share:" not in bar
    asyncio.run(go())


def test_the_network_tab_shows_the_latency_next_to_the_address(make_server, on_wifi, monkeypatch):
    from test_tui import ready
    s = make_server("pytest-lag")
    docker_up(monkeypatch)
    vpn.remember_latency(ADDRESS, 45)

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(170, 60)) as pilot:
            await ready(app, pilot)
            app.action_tab("network")
            await until(pilot, lambda: any("Latency to it: 45 ms (good)" in line for line in rendered_lines(app)))
            assert any(ADDRESS in line for line in rendered_lines(app))
    asyncio.run(go())


# ---------- the first-run question ----------
def wizard_run(server, answer_id=None, first_run=True, key=None):
    """Open the dashboard; report whether the question was asked, and what is on screen once it is answered (by pressing the
    button `answer_id`, or the key `key`)."""
    seen = {}

    async def go():
        app = tui.Dashboard(target=server, first_run=first_run)
        async with app.run_test(size=(150, 50)) as pilot:
            await until(pilot, lambda: app.snapshot is not None)
            await pilot.pause(0.5)
            seen["asked"] = isinstance(app.screen, ShareChoice)
            if seen["asked"] and answer_id:
                app.screen.query_one(f"#{answer_id}").press()
                await pilot.pause(0.8)
            if seen["asked"] and key:
                await pilot.press(key)
                await pilot.pause(0.8)
            seen["screen"] = type(app.screen).__name__
    asyncio.run(go())
    return seen


@pytest.fixture
def chosen(monkeypatch):
    picked = []
    monkeypatch.setattr(share, "set_mode", lambda new, say=None: picked.append(new) or [])
    return picked


def test_first_run_asks_once_and_local_name_is_applied(make_server, on_wifi, chosen):
    make_server("pytest-wiz")
    seen = wizard_run("pytest-wiz", "local")
    assert seen["asked"] and chosen == ["local"] and seen["screen"] not in ("ShareChoice", "AskText")
    assert core.setting("share_wizard_done") is True
    assert wizard_run("pytest-wiz")["asked"] is False                  # asked once, never again
    assert chosen == ["local"]


def test_choosing_hidden_without_a_vpn_key_opens_the_key_dialog(make_server, on_wifi, chosen):
    make_server("pytest-wiz")
    seen = wizard_run("pytest-wiz", "hidden")
    assert chosen == ["hidden"] and seen["screen"] == "AskText"


def test_choosing_hidden_with_a_vpn_key_does_not_ask_for_it_again(make_server, on_wifi, chosen):
    make_server("pytest-wiz")
    vpn.setup(KEY)
    seen = wizard_run("pytest-wiz", "hidden")
    assert chosen == ["hidden"] and seen["screen"] not in ("ShareChoice", "AskText")


@pytest.mark.parametrize("how", [{"answer_id": "later"}, {"key": "escape"}])
def test_not_now_changes_nothing_and_does_not_nag(make_server, on_wifi, chosen, how):
    make_server("pytest-wiz")
    seen = wizard_run("pytest-wiz", **how)
    assert seen["asked"] and seen["screen"] not in ("ShareChoice", "AskText")
    assert chosen == [] and share.mode() is None
    assert core.setting("share_wizard_done") is True
    assert wizard_run("pytest-wiz")["asked"] is False


def test_the_question_is_only_asked_when_it_makes_sense(make_server, on_wifi, chosen):
    make_server("pytest-wiz")
    assert wizard_run("pytest-wiz", first_run=False)["asked"] is False  # the plain dashboard (and every other test) is never interrupted
    assert core.setting("share_wizard_done") is not True
    mode("local")
    assert wizard_run("pytest-wiz")["asked"] is False                   # already chosen
    assert core.setting("share_wizard_done") is not True                # and nothing is recorded: it was never needed
    core.save_setting("share_mode", None)
    core.save_setting("share_wizard_done", None)
    import shutil
    shutil.rmtree(core.SERVERS / "pytest-wiz")
    assert wizard_run(None)["asked"] is False                           # no servers: nothing to share yet
    assert core.setting("share_wizard_done") is not True                # so it asks next time, once there is one


def test_mc_itself_starts_the_dashboard_with_the_question_on(monkeypatch):
    made = {}

    class Fake:
        def __init__(self, target=None, first_run=False):
            made.update(target=target, first_run=first_run)

        def run(self):
            made["ran"] = True
    monkeypatch.setattr(tui, "Dashboard", Fake)
    tui.run("main")
    assert made == {"target": "main", "first_run": True, "ran": True}


# ---------- the Network tab's setup dialogs ----------
def test_the_vpn_key_dialog_goes_on_to_country_and_city(make_server, on_wifi, monkeypatch):
    from test_tui import ready
    s = make_server("pytest-flow")
    saved = []
    monkeypatch.setattr(vpn, "setup", lambda key, country=None, city=None: saved.append((key, country, city)))

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            await ready(app, pilot)
            app.action_tab("network")
            await pilot.pause(0.4)
            app.query_one("#net-setup").press()
            await pilot.pause(0.4)
            await answer(app, pilot, KEY)
            assert (await open_dialog(app, pilot)).query_one("#ask-input").value == ""      # nothing saved: no country is assumed
            await answer(app, pilot, "Netherlands")
            await answer(app, pilot, "Amsterdam")
            await until(pilot, lambda: saved)
    asyncio.run(go())
    assert saved == [(KEY, "Netherlands", "Amsterdam")]


def test_the_saved_country_is_offered_in_the_key_dialog_not_retyped(make_server, on_wifi, monkeypatch):
    from test_tui import ready
    s = make_server("pytest-flow")
    vpn.ENV_FILE.write_text("WIREGUARD_PRIVATE_KEY=\nSERVER_COUNTRIES=Germany\n")
    monkeypatch.setattr(vpn, "setup", lambda key, country=None, city=None: None)

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            await ready(app, pilot)
            app.action_tab("network")
            await pilot.pause(0.4)
            app.query_one("#net-setup").press()
            await pilot.pause(0.4)
            await answer(app, pilot, KEY)
            assert (await open_dialog(app, pilot)).query_one("#ask-input").value == "Germany"
    asyncio.run(go())


def test_leaving_the_city_empty_means_any_city(make_server, on_wifi, monkeypatch):
    from test_tui import ready
    s = make_server("pytest-flow")
    saved = []
    monkeypatch.setattr(vpn, "setup", lambda key, country=None, city=None: saved.append((key, country, city)))

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            await ready(app, pilot)
            app.action_tab("network")
            await pilot.pause(0.4)
            app.query_one("#net-setup").press()
            await pilot.pause(0.4)
            await answer(app, pilot, KEY)
            await answer(app, pilot, "Netherlands")
            await answer(app, pilot)                                      # the city box is empty
            await until(pilot, lambda: saved)
    asyncio.run(go())
    assert saved == [(KEY, "Netherlands", "any")]


def test_exit_city_button_needs_a_key_then_changes_only_the_city(make_server, on_wifi, monkeypatch):
    from test_tui import ready
    s = make_server("pytest-city")
    changed = []
    monkeypatch.setattr(vpn, "set_place", lambda country=None, city=None: changed.append((country, city)) or (country, city))

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 50)) as pilot:
            await ready(app, pilot)
            app.action_tab("network")
            await pilot.pause(0.4)
            app.query_one("#net-city").press()                            # no key yet: a hint, no dialog
            await pilot.pause(0.4)
            assert not isinstance(app.screen, AskText) and changed == []
            vpn.setup(KEY, "Netherlands", "Amsterdam")
            app.query_one("#net-city").press()
            assert (await open_dialog(app, pilot)).query_one("#ask-input").value == "Amsterdam"   # the current city is offered
            await answer(app, pilot, "Rotterdam")
            await until(pilot, lambda: changed)
            app.query_one("#net-city").press()
            dialog = await open_dialog(app, pilot)
            dialog.query_one("#cancel").press()                           # cancel changes nothing
            await until(pilot, lambda: app.screen is not dialog, 5)
    asyncio.run(go())
    assert changed == [(None, "Rotterdam")]


# ---------- a minimal Linux image ----------
def test_a_machine_without_the_ip_command_is_simply_not_on_a_vpn(monkeypatch):
    def missing(*args, **kw):
        raise FileNotFoundError(2, "No such file or directory", "ip")
    monkeypatch.setattr(platform, "IS_MAC", False)
    monkeypatch.setattr(platform.subprocess, "run", missing)
    assert platform._vpn_active() is False                          # used to crash the dashboard's sampler
