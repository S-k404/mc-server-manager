"""`mc fix`: repair everything mc can, in order, and say what it did. Safe to run at any time.

The order matters, each step needs the one before it:
  1. Docker answers (the VPN and any Docker server need it). Opening Docker Desktop is left to the person: mc never starts or
     changes another app.
  2. Servers: a crashed server, or one that is meant to be running, is started; the server the internet address leads to is started
     too (this is an explicit repair; the background agent never starts a server that was stopped on purpose).
  3. Share mode: a name published again in local mode; a running server that needs a restart for the mode is reported, not restarted.
  4. The internet path: `vpn.fix` (trace, recreate the VPN, trace again).
  5. Voice chat: its address follows the internet address (`voice.maintain`).
  6. Mods: serious problems are counted and pointed at `mc mods doctor` (that can change jars, so it stays a separate, deliberate step).
Nothing here deletes anything or changes another app's settings.
"""
from . import container as dk
from . import modconfig, moddoctor, network, servers, share, voice, vpn
from .core import Fail, all_servers, default_server_name, info


def run(start_servers=True, say=info):
    """Returns [vpn.Step]: what was checked, what was done (ok=True), what still needs a person (ok=False) or can't be told (None)."""
    steps = []

    def add(ok, what, detail="", fix=""):
        steps.append(vpn.Step(ok, what, detail, fix))
        return ok

    every = all_servers()
    if not every:
        add(False, "A server exists", fix="mc create")
        return steps
    uses_docker = vpn.enabled() or any(s.docker for s in every)
    docker_ok = True
    if uses_docker:
        docker_ok = add(dk.available(), "Docker answers", fix="open Docker Desktop, wait for the whale icon to settle, then run: mc fix")
    target = vpn.forward_server() if vpn.enabled() else default_server_name()

    # 1b. mod configs, before any server starts: a broken one is moved aside (nothing is deleted) so the mod writes a fresh default
    for s in every:
        try:
            items = modconfig.check(s)
            bad = modconfig.problems(items)
            if not bad:
                continue
            if s.up():
                add(None, f"'{s.name}': {len(bad)} mod config problem(s)", bad[0].message, f"mc -s {s.name} stop, then: mc -s {s.name} mods config --fix")
                continue
            for line in modconfig.fix(s, items):
                add(True, f"Mod config on '{s.name}': " + line)
        except Exception as e:                             # noqa: BLE001 - one unreadable folder must not stop the repair
            add(None, f"Mod config on '{s.name}'", f"couldn't be checked: {type(e).__name__}: {e}")

    # 2. servers
    for s in every:
        if s.docker and not docker_ok:
            continue
        wanted_up = s.inst.desired == "running" or s.state() == "crashed" or (start_servers and s.name == target)
        if s.up():
            add(True, f"Server '{s.name}' is running")
        elif wanted_up:
            say(f"Starting '{s.name}'...")
            try:
                servers.start(s)
                add(True, f"Server '{s.name}' was stopped: started it")
            except Fail as e:
                add(False, f"Server '{s.name}' won't start", str(e), f"mc -s {s.name} crash")

    # 3. share mode
    try:
        note = share.bonjour_ensure()
        if note:
            add(True, "Local name", note)
        mode = share.mode()
        for s in every:
            if s.up() and share.needs_restart(s):
                add(None, f"'{s.name}' needs a restart for {mode} mode", "", f"mc -s {s.name} restart")
    except Exception as e:                                 # noqa: BLE001 - a missing dns-sd or a busy Docker must not stop the rest
        add(None, "Share mode", f"couldn't be checked: {type(e).__name__}: {e}")

    # 4. the internet path
    if vpn.enabled() and docker_ok:
        hops, actions = vpn.fix(start_servers=False, say=say)
        for a in actions:
            add(not a.startswith("could not"), "Did: " + a)       # a repair that failed is not a success
        bad = [h for h in hops if h.ok is False and not h.what.startswith("Joining")]
        if bad:
            steps.extend(bad)
            if hint := vpn.fix_hint(hops):
                add(False, "The internet path is still broken", hint)
        else:
            addr = vpn.public_address(live=True)
            add(True, "The internet path: every hop answers", addr or "")
            _signin(add)
    elif not vpn.enabled():
        add(True, "Internet play is off", "friends on your Wi-Fi can still join (mc vpn setup turns it on)")

    # 5. voice chat
    for s in voice.servers_using_voice():
        try:
            done = voice.maintain(s, log=lambda m: None, settle=0)     # an explicit repair: restart now if nobody is on
        except Exception as e:                             # noqa: BLE001
            add(None, f"Voice chat on '{s.name}'", f"couldn't be updated: {type(e).__name__}: {e}")
            continue
        route = "through the internet address" if voice.route(s) == "vpn" else "straight to this Mac"
        add(True, f"Voice chat on '{s.name}': audio goes {route}", done)

    # 6. mods
    for s in every:
        try:
            bad = moddoctor.problems(moddoctor.check(s))
        except Exception:                                  # noqa: BLE001 - a jar that can't be read isn't a reason to stop
            continue
        if bad:
            add(None, f"'{s.name}': {len(bad)} mod problem(s)", bad[0].message, f"mc -s {s.name} mods doctor")
    return steps


def _signin(add):
    """Can Minecraft sign in through the Docker VPN's proxy (the launcher's route on a Wi-Fi that blocks Microsoft)? Needs the
    five hosts signing in uses; `network.auth_checks` probes them (a few seconds when something is blocked)."""
    try:
        # only the proxy route, only the five sign-in hosts, and patiently: the tunnel is usually seconds old here
        checks = network.auth_checks(probe_fn=network.patient_probe, hosts=network.SIGNIN_HOSTS, direct=False)
    except Exception as e:                                  # noqa: BLE001 - a probe must never stop the repair
        return add(None, "Minecraft sign-in", f"couldn't be checked: {type(e).__name__}: {e}")
    proxied = [c for c in checks if c.group.endswith("through the Docker VPN proxy")
               and any(h in c.message for h in network.SIGNIN_HOSTS)]
    if not proxied:
        return add(None, "Minecraft sign-in through the launcher proxy", "not measured: the sign-in proxy isn't answering", "mc vpn proxy")
    blocked = [c for c in proxied if not c.good]
    if blocked:
        shown = "; ".join(c.message for c in blocked[:3]) + ("; ..." if len(blocked) > 3 else "")
        return add(False, "Minecraft sign-in through the launcher proxy",
                   f"{len(blocked)} of {len(proxied)} sign-in hosts didn't answer, even after a second try: {shown}",
                   "wait a minute and run: mc fix   (still failing: mc vpn logs vpn, and the launcher's proxy settings: mc vpn proxy)")
    return add(True, "Minecraft sign-in through the launcher proxy", "all the sign-in hosts answer (settings for the launcher: mc vpn proxy)")
