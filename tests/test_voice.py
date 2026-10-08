"""Simple Voice Chat through the internet address: the voice_host line, the restart, the forwarder's UDP listener, the modes."""

import pytest

from mcctl import container as dk, core, countdown, platform, players, share, vpn, voice
from test_moddoctor import fabric_jar, put

ADDRESS = "203.0.113.5:43318"


def voice_server(make_server, name="pytest-voice", config="# Simple Voice Chat server config\nport=24454\nvoice_host=\n", **inst):
    s = make_server(name, **inst)
    put(s, "voicechat.jar", fabric_jar("voicechat", "2.5"))
    if config is not None:
        cfg = s.dir / "config" / "voicechat"
        cfg.mkdir(parents=True)
        (cfg / "voicechat-server.properties").write_text(config)
    return s


@pytest.fixture
def through_vpn(monkeypatch):
    """Internet play is on for the server named in `name`, the Mac's own VPN is on, and Proton gave an address."""
    def make(name, address=ADDRESS, mac_vpn=True, mode=None):
        monkeypatch.setattr(vpn, "enabled", lambda: True)
        monkeypatch.setattr(vpn, "forward_server", lambda: name)
        monkeypatch.setattr(vpn, "public_now", lambda: address)
        monkeypatch.setattr(platform, "vpn_active", lambda: mac_vpn)
        monkeypatch.setattr(share, "mode", lambda: mode)
    return make


def voice_host(s):
    return voice.props(s).get("voice_host")


# ---------- where audio should go ----------
@pytest.mark.parametrize("mode, mac_vpn, expected", [
    ("hidden", False, "vpn"),     # privacy: nobody reaches the Wi-Fi, so everyone arrives through the address
    ("hidden", True, "vpn"),
    ("local", True, "vpn"),       # the Mac's own VPN blocks the Wi-Fi: everyone arrives through the address
    ("local", False, "direct"),   # LAN play: audio straight to this Mac
    (None, True, "vpn"),          # default mode, same rule
    (None, False, "direct"),
])
def test_route_follows_the_share_mode_and_the_macs_own_vpn(make_server, through_vpn, mode, mac_vpn, expected):
    s = voice_server(make_server)
    through_vpn(s.name, mac_vpn=mac_vpn, mode=mode)
    assert voice.route(s) == expected


def test_a_server_the_address_does_not_lead_to_is_always_direct(make_server, through_vpn):
    s = voice_server(make_server)
    through_vpn("some-other-server", mode="hidden")
    assert voice.route(s) == "direct"


def test_no_internet_play_means_direct(make_server, monkeypatch):
    s = voice_server(make_server)
    monkeypatch.setattr(vpn, "enabled", lambda: False)
    assert voice.route(s) == "direct" and voice.wanted(s) == ""


# ---------- the voice_host line ----------
def test_sync_writes_the_internet_address_into_the_mods_config(make_server, through_vpn):
    s = voice_server(make_server)
    through_vpn(s.name)
    assert "set to " + ADDRESS in voice.sync(s)
    assert voice_host(s) == ADDRESS
    assert "# Simple Voice Chat server config" in voice.config_file(s).read_text()      # the mod's own comments stay
    assert voice.sync(s) == ""                                                         # nothing more to do


def test_sync_adds_the_line_when_the_config_has_none(make_server, through_vpn):
    s = voice_server(make_server, config="port=24454\nbind_address=\n")
    through_vpn(s.name)
    voice.sync(s)
    assert voice_host(s) == ADDRESS and voice.props(s)["bind_address"] == ""


def test_sync_follows_the_address_when_the_vpn_reconnects(make_server, through_vpn):
    s = voice_server(make_server)
    through_vpn(s.name)
    voice.sync(s)
    through_vpn(s.name, address="203.0.113.9:50001")
    assert "203.0.113.9:50001" in voice.sync(s) and voice_host(s) == "203.0.113.9:50001"


def test_sync_takes_its_own_line_out_when_audio_should_go_direct_again(make_server, through_vpn):
    s = voice_server(make_server)
    through_vpn(s.name)
    voice.sync(s)
    through_vpn(s.name, mac_vpn=False)                                                  # the Mac's VPN went off: LAN play
    assert "cleared" in voice.sync(s) and voice_host(s) == ""


def test_a_voice_host_somebody_wrote_by_hand_is_never_touched(make_server, through_vpn):
    s = voice_server(make_server, config="port=24454\nvoice_host=voice.example.org:24454\n")
    through_vpn(s.name)
    assert voice.sync(s) == "" and voice_host(s) == "voice.example.org:24454"
    through_vpn(s.name, mac_vpn=False)
    assert voice.sync(s) == "" and voice_host(s) == "voice.example.org:24454"


def test_nothing_happens_before_the_mod_has_written_its_config_or_the_address_is_known(make_server, through_vpn):
    s = voice_server(make_server, config=None)
    through_vpn(s.name)
    assert voice.sync(s) == ""                                                          # the mod writes its config on first start
    s2 = voice_server(make_server, name="pytest-voice2")
    through_vpn(s2.name, address=None)
    assert voice.sync(s2) == "" and voice_host(s2) == ""


def test_servers_without_the_mod_are_ignored(make_server, through_vpn):
    s = make_server("plain")
    through_vpn(s.name)
    assert not voice.installed(s) and voice.sync(s) == ""


def test_the_mods_udp_port_comes_from_its_config(make_server):
    assert voice.udp_port(voice_server(make_server)) == 24454
    assert voice.udp_port(voice_server(make_server, name="a", config="port=25000\n")) == 25000
    assert voice.udp_port(voice_server(make_server, name="b", config="port=-1\n")) == 25565     # shares Minecraft's own port
    assert voice.udp_port(voice_server(make_server, name="c", config="port=oops\n")) == 24454


# ---------- the agent restarts the server once, when nobody is on ----------
@pytest.fixture
def running(monkeypatch):
    state = {"up": True, "online": [], "scheduled": []}
    cls = core.Server
    monkeypatch.setattr(cls, "up", lambda self, sessions=None: state["up"])
    monkeypatch.setattr(cls, "accepting", lambda self: state["up"])
    monkeypatch.setattr(cls, "busy", lambda self: False)
    monkeypatch.setattr(players, "online", lambda s: state["online"])
    monkeypatch.setattr(countdown, "pending", lambda s: None)
    monkeypatch.setattr(countdown, "schedule", lambda s, action, delay, **kw: state["scheduled"].append((s.name, action, delay)))
    return state


def test_maintain_restarts_a_running_server_for_a_new_address_only_after_it_has_settled_and_the_room_is_empty(make_server, through_vpn, running):
    s = voice_server(make_server)
    through_vpn(s.name)
    now = 1_000_000.0
    first = voice.maintain(s, log=lambda m: None, now=now)
    assert "set to" in first and running["scheduled"] == [] and voice.pending_restart(s)
    assert voice.maintain(s, log=lambda m: None, now=now + 30) == "" and running["scheduled"] == []       # still settling (the VPN may flap)
    running["online"] = ["Steve"]
    voice.maintain(s, log=lambda m: None, now=now + 600)
    assert running["scheduled"] == [] and voice.pending_restart(s)                                         # a player is on: wait
    running["online"] = []
    done = voice.maintain(s, log=lambda m: None, now=now + 900)
    assert running["scheduled"] == [(s.name, "restart", "15s")] and "restarting" in done and not voice.pending_restart(s)
    assert voice.maintain(s, log=lambda m: None, now=now + 1200) == "" and len(running["scheduled"]) == 1  # once, not every pass


def test_a_stopped_server_needs_no_restart_it_reads_the_line_when_it_starts(make_server, through_vpn, running):
    s = voice_server(make_server)
    through_vpn(s.name)
    running["up"] = False
    assert "set to" in voice.maintain(s, log=lambda m: None, now=1_000_000.0)
    assert not voice.pending_restart(s) and running["scheduled"] == []


def test_a_server_that_is_still_starting_is_not_restarted(make_server, through_vpn, running, monkeypatch):
    s = voice_server(make_server)
    through_vpn(s.name)
    voice.maintain(s, log=lambda m: None, now=1_000_000.0)
    monkeypatch.setattr(core.Server, "accepting", lambda self: False)
    voice.maintain(s, log=lambda m: None, now=1_001_000.0)
    assert running["scheduled"] == [] and voice.pending_restart(s)


# ---------- the forwarder ----------
def test_the_forwarder_also_passes_udp_on_protons_port_to_the_mod(make_server, monkeypatch):
    make_server("a", port=25565)
    monkeypatch.setattr(vpn, "forward_server", lambda: "a")
    monkeypatch.setattr(vpn, "_voice_target", lambda: 24454)
    script = vpn._forward_script("192.168.65.254", forwarded=43318)
    assert "socat -T 60 UDP4-LISTEN:43318,fork,reuseaddr UDP4:192.168.65.254:24454" in script
    assert "UDP4" not in vpn._forward_script("192.168.65.254")                          # nothing until Proton's port is known
    monkeypatch.setattr(vpn, "_voice_target", lambda: None)
    assert "UDP4" not in vpn._forward_script("192.168.65.254", forwarded=43318)         # no mod, no UDP listener


def test_the_forwarder_is_labelled_with_the_voice_port_so_heal_notices_changes(make_server, monkeypatch):
    make_server("a", port=25565)
    monkeypatch.setattr(vpn, "forward_server", lambda: "a")
    monkeypatch.setattr(vpn, "_voice_target", lambda: 24454)
    ran = []
    monkeypatch.setattr(dk, "remove", lambda name: None)
    monkeypatch.setattr(dk, "run", lambda *args: ran.append(args))
    vpn._start_dependents(ip="192.168.65.254", forwarded=43318)
    assert "mc.udp=24454" in ran[0] and "mc.forwarded=43318" in ran[0]


def test_heal_restarts_the_forwarder_when_voice_chat_appears_after_it_started(monkeypatch):
    vpn.setup("p" * 43 + "=")
    core.save_setting("vpn_desired", True)
    monkeypatch.setattr(dk, "available", lambda: True)
    started = {"State": {"Status": "running", "StartedAt": "2026-10-02T15:00:00Z"}}
    infos = {vpn.VPN: started, vpn.FORWARD: {**started, "Config": {"Labels": {"mc.forwarded": "43318", "mc.udp": ""}}}}
    monkeypatch.setattr(dk, "inspect", lambda name, fresh=False: infos[name])
    monkeypatch.setattr(vpn, "forwarded_port", lambda: 43318)
    monkeypatch.setattr(vpn, "_voice_target", lambda: 24454)
    redone = []
    monkeypatch.setattr(vpn, "_start_dependents", lambda ip=None, forwarded=None: redone.append(forwarded))
    assert vpn.heal() == "pointed the forwarder at Proton's current port" and redone == [43318]


# ---------- the doctor knows the routes ----------
def test_the_doctor_does_not_call_a_loopback_bind_an_error_when_audio_comes_through_the_forwarder(make_server, through_vpn):
    from mcctl import moddoctor
    s = voice_server(make_server, config="port=24454\nbind_address=\nvoice_host=\n")
    s.set_prop("server-ip", "127.0.0.1")                                   # hidden mode's bind
    through_vpn(s.name, mode="hidden")
    found = [f for f in moddoctor.check(s) if f.mod == "voicechat.jar"]
    assert not [f for f in found if f.kind == "voicechat" and f.level == "error"]
    assert any("goes through the internet address" in f.message for f in found)
    through_vpn(s.name, mac_vpn=False, mode="local")                       # LAN play with a loopback bind: still nobody can reach it
    found = [f for f in moddoctor.check(s) if f.mod == "voicechat.jar"]
    assert any(f.kind == "voicechat" and f.level == "error" for f in found)
    assert any("straight to this Mac" in f.message for f in found)


def test_a_settle_time_of_zero_restarts_at_once(make_server, through_vpn, running):
    s = voice_server(make_server)
    through_vpn(s.name)
    now = 1_000_000.0
    done = voice.maintain(s, log=lambda m: None, now=now, settle=0)
    assert running["scheduled"] == [(s.name, "restart", "15s")] and "restarting" in done


def test_a_settle_time_of_zero_still_waits_for_an_empty_room(make_server, through_vpn, running):
    s = voice_server(make_server)
    through_vpn(s.name)
    running["online"] = ["Steve"]
    voice.maintain(s, log=lambda m: None, now=1_000_000.0, settle=0)
    assert running["scheduled"] == [] and voice.pending_restart(s)


# ---------- a UDP port nothing else holds ----------
def only_running(monkeypatch, *names):
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: self.name in names)


def test_a_second_server_cannot_share_the_voice_port_of_one_that_is_running(make_server, monkeypatch):
    voice_server(make_server, name="alpha", port=25565)
    b = voice_server(make_server, name="beta", port=25566)
    only_running(monkeypatch, "alpha")
    monkeypatch.setattr(voice, "udp_free", lambda port, bind="": True)
    note = voice.ensure_port(b)
    assert "24454 can't be used (another server's voice chat has it): now 24455" in note
    assert voice.props(b)["port"] == "24455" and voice.udp_port(b) == 24455
    assert voice.ensure_port(b) == ""                                       # settled: nothing to do the next time


def test_two_stopped_servers_may_keep_the_same_port_until_the_second_one_starts(make_server, monkeypatch):
    a = voice_server(make_server, name="alpha", port=25565)
    b = voice_server(make_server, name="beta", port=25566)
    only_running(monkeypatch)
    monkeypatch.setattr(voice, "udp_free", lambda port, bind="": True)
    assert voice.ensure_port(a) == "" and voice.ensure_port(b) == ""        # nothing holds the port, so no config is touched


def test_a_port_that_something_else_holds_is_replaced_on_a_stopped_server(make_server, monkeypatch):
    s = voice_server(make_server)
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: False)
    monkeypatch.setattr(voice, "udp_free", lambda port, bind="": port != 24454)
    assert "it is in use): now 24455" in voice.ensure_port(s)
    assert voice.props(s)["port"] == "24455"


def test_a_free_port_and_the_shared_minecraft_port_are_left_alone(make_server, monkeypatch):
    s = voice_server(make_server)
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: False)
    monkeypatch.setattr(voice, "udp_free", lambda port, bind="": True)
    assert voice.ensure_port(s) == "" and voice.props(s)["port"] == "24454"
    shared = voice_server(make_server, name="shared", config="port=-1\n")
    monkeypatch.setattr(voice, "udp_free", lambda port, bind="": False)
    assert voice.ensure_port(shared) == "" and voice.props(shared)["port"] == "-1"


def test_a_running_server_moves_only_when_its_log_says_the_bind_failed(make_server, monkeypatch):
    s = voice_server(make_server)
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: True)
    monkeypatch.setattr(voice, "udp_free", lambda port, bind="": port != 24454)
    assert voice.ensure_port(s) == ""                                       # it is running and nothing is wrong: leave it
    (s.dir / "logs").mkdir(exist_ok=True)
    (s.dir / "logs" / "latest.log").write_text("[12:00:01] [Server thread/ERROR] (voicechat) Voice chat server could not start: "
                                                "Address already in use (Bind failed)\n")
    assert voice.log_failure(s).startswith("(voicechat) Voice chat server could not start") or "Address already in use" in voice.log_failure(s)
    assert "now 24455" in voice.ensure_port(s)


@pytest.mark.parametrize("line, found", [
    ("[Server thread/ERROR] (voicechat) Failed to bind to port 24454: Address already in use", True),
    ("[voicechat/ERROR] java.net.BindException: Address already in use", True),
    ("[Server thread/INFO] (voicechat) Voice chat server started at port 24454", False),
    ("[Server thread/ERROR] (lithium) Address already in use", False),            # another mod's problem is not voice chat's
])
def test_log_failure_only_recognises_voice_chat_bind_failures(make_server, line, found):
    s = voice_server(make_server)
    (s.dir / "logs").mkdir(exist_ok=True)
    (s.dir / "logs" / "latest.log").write_text(line + "\n")
    assert bool(voice.log_failure(s)) is found


def test_the_agent_moves_a_running_servers_voice_port_after_a_failed_bind_and_restarts_when_empty(make_server, through_vpn, running, monkeypatch):
    s = voice_server(make_server)
    through_vpn(s.name, mac_vpn=False)                                       # direct: no voice_host involved
    monkeypatch.setattr(voice, "udp_free", lambda port, bind="": port != 24454)
    (s.dir / "logs").mkdir(exist_ok=True)
    (s.dir / "logs" / "latest.log").write_text("[ERROR] (voicechat) Voice chat server could not start: Address already in use\n")
    now = 1_000_000.0
    done = voice.maintain(s, log=lambda m: None, now=now, settle=0)
    assert "now 24455" in done and "restarting" in done and running["scheduled"] == [(s.name, "restart", "15s")]
