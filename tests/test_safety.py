"""Safety: atomic writes, one operation at a time per server, disk space before big writes, a quiet error handler with a log,
and moving players' data when a server switches between paid and cracked accounts."""
import json
import os
import stat
import subprocess
import sys
import threading
import time
import types
import zipfile
from pathlib import Path

import pytest

from mcctl import agent, atomic, backups, cli, config, core, errlog, players, servers, uuidmigrate
from mcctl.core import Busy, Fail

REPO = Path(__file__).resolve().parent.parent
HOLDER = ("import sys, time\nfrom mcctl import core\ns = core.Server(sys.argv[1])\n"
          "with s.op_lock('backing up', wait=5):\n    print('held', flush=True)\n    time.sleep(float(sys.argv[2]))\n")


def hold(name, seconds=60):
    """A separate process that holds the server's operation lock (like a backup running somewhere else)."""
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, name, str(seconds)], stdout=subprocess.PIPE, text=True, cwd=REPO)
    assert proc.stdout.readline().strip() == "held"
    return proc


@pytest.fixture
def holders():
    procs = []
    yield lambda name, seconds=60: procs.append(hold(name, seconds)) or procs[-1]
    for p in procs:
        p.kill()
        p.wait()


@pytest.fixture
def quick(monkeypatch):
    """Operations give up on a held lock after 0.2 s instead of 10 s."""
    monkeypatch.setattr(core.Server, "op_lock",
                        lambda self, what, wait=0.2: core.file_lock(self.dir / ".mc-lock", what, wait, label=f"'{self.name}'"))


def low_disk(monkeypatch, gb=0.5):
    monkeypatch.setattr(core.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=int(gb * 2**30), total=2**40, used=0))


def with_world(s, files=None):
    (s.world_dir / "playerdata").mkdir(parents=True, exist_ok=True)
    (s.world_dir / "level.dat").write_bytes(b"level")
    for rel, data in (files or {}).items():
        (s.world_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (s.world_dir / rel).write_bytes(data)
    return s


# ---------- 1. atomic writes ----------
def test_atomic_write_replaces_whole_files_and_leaves_nothing_behind(tmp_path):
    p = tmp_path / "a.json"
    core.write_json_atomic(p, {"x": 1})
    assert json.loads(p.read_text()) == {"x": 1} and p.read_text().endswith("\n")
    assert stat.S_IMODE(p.stat().st_mode) == 0o644                          # a new file: readable, not private
    p.chmod(0o640)
    core.write_json_atomic(p, {"x": 2})
    assert stat.S_IMODE(p.stat().st_mode) == 0o640                          # an existing file keeps its permissions
    core.write_atomic(tmp_path / "secret", b"k", mode=0o600)
    assert stat.S_IMODE((tmp_path / "secret").stat().st_mode) == 0o600
    assert sorted(f.name for f in tmp_path.iterdir()) == ["a.json", "secret"]     # no temp files left


def test_a_failed_write_keeps_the_old_file_and_cleans_up(tmp_path, monkeypatch):
    p = tmp_path / "ops.json"
    p.write_text('["old"]')
    monkeypatch.setattr(atomic.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        core.write_json_atomic(p, ["new"])
    assert p.read_text() == '["old"]'                                       # never half a file, never the new one
    assert [f.name for f in tmp_path.iterdir()] == ["ops.json"]


def test_every_state_file_is_written_atomically(make_server, monkeypatch):
    """A crash between 'start writing' and 'done' must leave the old content: each of these goes through atomic.write_atomic."""
    s = make_server()
    calls = []
    real = atomic.write_atomic
    monkeypatch.setattr(atomic, "write_atomic", lambda path, data, mode=None: calls.append(Path(path).name) or real(path, data, mode))
    core.save_setting("k", 1)
    s.update(autobackup=True)
    players._edit_list(s, "ops.json", lambda items: items + [{"name": "A"}])
    from mcctl import countdown, mods
    countdown.schedule(s, "restart", 600, spawn=False)
    mods.write_lock(s, {}, [])
    assert {".mc-settings.json", "instance.json", "ops.json", "mods.lock.json", "pending.json"} <= set(calls)   # pending.json = countdown


def test_two_writers_never_lose_each_others_settings(monkeypatch):
    real = core.write_json_atomic
    monkeypatch.setattr(core, "write_json_atomic", lambda *a, **k: (time.sleep(0.15), real(*a, **k))[1])   # widen the race
    threads = [threading.Thread(target=core.save_setting, args=(k, k)) for k in "abcd"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {k: core.setting(k) for k in "abcd"} == {k: k for k in "abcd"}


def test_two_updates_to_one_server_never_lose_a_field(make_server, monkeypatch):
    s = make_server()
    real = config.save
    monkeypatch.setattr(config, "save", lambda path, inst: (time.sleep(0.15), real(path, inst))[1])
    changes = [{"autobackup": True}, {"keep_alive": True}, {"autostart": True}]
    threads = [threading.Thread(target=s.update, kwargs=c) for c in changes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    inst = s.inst
    assert inst.autobackup and inst.keep_alive and inst.autostart
    assert s.update(ram_gb=3).ram_gb == 3                                    # update() still returns the new settings


# ---------- 2. one operation at a time ----------
def test_the_lock_is_reentrant_and_records_who_holds_it(make_server):
    s = make_server()
    with s.op_lock("restarting"):
        with s.op_lock("stopping"):                                          # stop() inside restart(), backup() inside stop()
            holder = json.loads((s.dir / ".mc-lock").read_text())
            assert holder["what"] == "restarting" and holder["pid"] == os.getpid()
        assert s.busy() is None                                              # the holder itself is never told it is busy
    assert (s.dir / ".mc-lock").read_text() == ""


def test_a_second_process_waits_then_is_told_what_is_running(make_server, holders):
    s = make_server()
    holders(s.name)
    assert s.busy() == "backing up"
    t0 = time.monotonic()
    with pytest.raises(Busy, match=r"Another operation is already running on 'pytest-a': backing up, started \d+ s ago"):
        with s.op_lock("stopping", wait=0.4):
            raise AssertionError("must not get the lock")
    assert 0.35 <= time.monotonic() - t0 < 3


def test_a_waiter_gets_the_lock_when_it_is_released_in_time(make_server):
    s = make_server()
    got = []
    started = threading.Event()

    def other():
        with s.op_lock("backing up"):
            started.set()
            time.sleep(0.5)
    t = threading.Thread(target=other)
    t.start()
    started.wait()
    with s.op_lock("stopping", wait=5):
        got.append(True)
    t.join()
    assert got == [True]


def test_a_killed_holder_never_leaves_a_stale_lock(make_server, holders):
    s = make_server()
    proc = holders(s.name)
    assert s.busy()
    proc.kill()                                                              # crash, power loss, kill -9: no cleanup runs
    proc.wait()
    assert s.busy() is None
    with s.op_lock("stopping", wait=1):                                      # the OS released it with the process
        pass


def test_a_server_that_vanished_is_a_clear_error(make_server):
    s = make_server()
    import shutil
    shutil.rmtree(s.dir)
    with pytest.raises(Fail, match="doesn't exist any more"):
        with s.op_lock("stopping"):
            pass


def test_every_operation_takes_the_lock(make_server, holders, quick, tmp_path, monkeypatch):
    s = with_world(make_server())
    zipped = backups.make_backup(s)                                          # something to restore / import
    p = types.SimpleNamespace(server=s, downgrade=False, missing=[], target="27.1")
    from mcctl import upgrade
    holders(s.name)
    ops = {
        "start": lambda: servers.start(s), "stop": lambda: servers.stop(s), "backup": lambda: backups.make_backup(s),
        "restore": lambda: backups.restore(s, "1"), "import": lambda: backups.world_import(s, zipped),
        "rename": lambda: servers.rename(s.name, "other"), "rename world": lambda: servers.rename_world(s, "newworld"),
        "delete": lambda: servers.delete(s.name), "upgrade": lambda: upgrade.apply(p),
        "cracked": lambda: players.set_cracked(s, True, insecure=True),
    }
    for name, op in ops.items():
        with pytest.raises(Busy):
            op()
        assert s.dir.exists() and (s.world_dir / "level.dat").exists(), name   # and it changed nothing


def test_starting_holds_the_lock_to_launch_but_not_while_waiting_for_the_server(make_server, monkeypatch):
    s = make_server()
    seen = {}
    state = {"up": False}
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: state["up"])
    monkeypatch.setattr(servers.provision, "ensure_java", lambda major: "java")
    monkeypatch.setattr(core.Server, "launch_argv", lambda self, java: ["java"])
    monkeypatch.setattr(core.Server, "ensure_rcon", lambda self, others=(): None)
    monkeypatch.setattr(core.Server, "listening", lambda self, timeout=0.5: False)

    def tmux(*args, **kw):
        seen["launching"] = str(s.dir / ".mc-lock") in core._held.__dict__.get("locks", {})
        state["up"] = True
    monkeypatch.setattr(servers, "tmux", tmux)

    def sleep(seconds):
        seen["waiting"] = str(s.dir / ".mc-lock") in core._held.__dict__.get("locks", {})
        raise Fail("stop here")
    monkeypatch.setattr(servers.time, "sleep", sleep)
    with pytest.raises(Fail, match="stop here"):
        servers.start(s)
    assert seen == {"launching": True, "waiting": False}                     # a `mc stop` is never locked out of a slow start


def test_the_agent_skips_a_server_that_is_busy_and_looks_after_the_others(make_server, holders, monkeypatch):
    busy = make_server("pytest-busy")
    make_server("pytest-free", port=25570)
    holders(busy.name)
    reconciled = []
    monkeypatch.setattr(agent, "reconcile", lambda s, st, first, now: reconciled.append(s.name))
    monkeypatch.setattr(agent, "record_metrics", lambda state, now: None)
    monkeypatch.setattr(agent.vpn, "heal", lambda: None)
    monkeypatch.setattr(agent.share, "bonjour_ensure", lambda: None)
    agent.tick({})
    assert reconciled == ["pytest-free"]


# ---------- 3. disk space ----------
def test_how_much_room_an_operation_needs():
    assert core.need_gb(0) == 2.0 and core.need_gb(1 * 2**30) == 2.0         # never less than 2 GB
    assert core.need_gb(10 * 2**30) == pytest.approx(12.0)                   # otherwise 1.2 x what it writes
    assert core.need_gb(5 * 2**30, 5 * 2**30) == pytest.approx(12.0)


def test_require_disk_refuses_with_both_numbers(tmp_path, monkeypatch):
    low_disk(monkeypatch, 1.5)
    with pytest.raises(Fail, match=r"Not enough free disk space to back up 'x': 1\.5 GB free, about 2\.0 GB needed"):
        core.require_disk(tmp_path, 2.0, "back up 'x'")
    low_disk(monkeypatch, 50)
    core.require_disk(tmp_path / "not" / "made" / "yet", 12.0, "x")          # a folder that doesn't exist yet: its parent counts


def test_a_backup_refuses_before_touching_anything_when_the_disk_is_nearly_full(make_server, monkeypatch):
    s = with_world(make_server())
    ran = []
    monkeypatch.setattr(core.Server, "accepting", lambda self: True)
    monkeypatch.setattr(core.Server, "run", lambda self, cmd, wait=1.5: ran.append(cmd) or "")
    low_disk(monkeypatch)
    with pytest.raises(Fail, match="Not enough free disk space to back up 'pytest-a'"):
        backups.make_backup(s)
    assert ran == [] and list(s.backups_dir.glob("*.zip")) == []              # no save-off was sent, no half zip written


def test_restore_import_upgrade_and_downloads_check_the_disk_too(make_server, monkeypatch, tmp_path):
    s = with_world(make_server())
    zipped = backups.make_backup(s)
    low_disk(monkeypatch)
    with pytest.raises(Fail, match="restore this backup"):
        backups.restore(s, "1")
    with pytest.raises(Fail, match="import this world"):
        backups.world_import(s, zipped)
    from mcctl import upgrade
    with pytest.raises(Fail, match="upgrade"):
        upgrade.apply(types.SimpleNamespace(server=s, downgrade=False, missing=[], target="27.1"))
    with pytest.raises(Fail, match="download x.jar"):
        core.download("https://example.invalid/x.jar", tmp_path / "x.jar")
    assert not list(tmp_path.glob("x.jar*"))                                  # not even a .part file
    assert (s.world_dir / "level.dat").exists()                               # the live world was never touched


def test_sizes_used_for_the_checks(make_server, tmp_path):
    s = with_world(make_server(), {"region/r.0.0.mca": b"x" * 1000})
    assert backups.world_bytes(s) == 1000 + len(b"level")
    z = tmp_path / "w.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("world/level.dat", b"a" * 300)
    assert backups.unpacked_bytes(z) == 300 and backups.unpacked_bytes(s.world_dir) == backups.world_bytes(s)
    assert backups.unpacked_bytes(tmp_path / "missing") == 0


# ---------- 4. the error handler ----------
@pytest.fixture
def crash(monkeypatch, tmp_path):
    monkeypatch.setattr(errlog, "LOG", tmp_path / "logs" / "mc-errors.log")

    def raise_(exc):
        monkeypatch.setattr(cli, "app", lambda args, prog_name: (_ for _ in ()).throw(exc))
    return raise_


def test_a_bug_shows_one_short_message_and_goes_to_the_log(crash, capsys):
    crash(RuntimeError("boom with [brackets] and https://discord.com/api/webhooks/1/secret"))
    with pytest.raises(SystemExit) as e:
        cli.run(["pw", "set", "alice", "hunter2"])
    out = capsys.readouterr().out
    assert e.value.code == 1
    assert "[x] Something went wrong: RuntimeError: boom with [brackets]" in out and "mc-errors.log" in out
    assert "Traceback" not in out and "MC_DEBUG=1" in out                    # a person gets a sentence, not a stack
    log = errlog.LOG.read_text()
    assert "Traceback (most recent call last)" in log and "RuntimeError: boom with [brackets]" in log
    assert "mc pw set ..." in log and "hunter2" not in log and "alice" not in log      # command words only, never arguments
    assert "api/webhooks/1/secret" not in log                                  # secrets are redacted


def test_mc_debug_still_logs_and_then_shows_the_traceback(crash, monkeypatch):
    crash(ValueError("bug"))
    monkeypatch.setenv("MC_DEBUG", "1")
    with pytest.raises(ValueError, match="bug"):
        cli.run(["status"])
    assert "ValueError: bug" in errlog.LOG.read_text()


def test_ordinary_failures_and_ctrl_c_and_a_closed_pipe_are_not_bugs(crash, capsys):
    crash(Fail("That name is taken."))
    with pytest.raises(SystemExit) as e:
        cli.run(["status"])
    assert e.value.code == 1 and "[x] That name is taken." in capsys.readouterr().out and not errlog.LOG.exists()
    crash(KeyboardInterrupt())
    with pytest.raises(SystemExit) as e:
        cli.run(["status"])
    assert e.value.code == 130
    crash(BrokenPipeError())
    with pytest.raises(SystemExit):
        cli.run(["status"])
    assert not errlog.LOG.exists()                                             # none of them is logged as an error


def test_the_error_log_rotates_at_one_megabyte(crash, monkeypatch):
    errlog.LOG.parent.mkdir()
    errlog.LOG.write_text("x" * (errlog.MAX_BYTES + 10))
    Path(f"{errlog.LOG}.1").write_text("older")
    errlog.record(RuntimeError("new"), ["status"])
    assert errlog.LOG.read_text().count("RuntimeError: new") == 1 and len(errlog.LOG.read_text()) < 5000
    assert Path(f"{errlog.LOG}.1").read_text().startswith("xxx") and Path(f"{errlog.LOG}.2").read_text() == "older"


def test_the_error_handler_itself_never_raises(monkeypatch, tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    monkeypatch.setattr(errlog, "LOG", blocked / "logs" / "mc-errors.log")     # can't be created: still no second error
    assert errlog.record(RuntimeError("x"), []) == errlog.LOG


# ---------- 5. players' data when the account mode changes ----------
ALICE_ONLINE = "11111111-2222-3333-4444-555555555555"
BOB_ONLINE = "66666666-7777-8888-9999-000000000000"


def paid_server(make_server, name="pytest-a"):
    """An online-mode server with two players who have saved data under their paid-account UUIDs."""
    s = make_server(name, props={"online-mode": "true"})
    with_world(s, {f"playerdata/{ALICE_ONLINE}.dat": b"alice-inv", f"playerdata/{ALICE_ONLINE}.dat_old": b"alice-old",
                   f"stats/{ALICE_ONLINE}.json": b"{}", f"advancements/{ALICE_ONLINE}.json": b"{}",
                   f"playerdata/{BOB_ONLINE}.dat": b"bob-inv"})
    (s.dir / "usercache.json").write_text(json.dumps([{"name": "Alice", "uuid": ALICE_ONLINE}, {"name": "Bob", "uuid": BOB_ONLINE}]))
    (s.dir / "ops.json").write_text(json.dumps([{"uuid": ALICE_ONLINE, "name": "Alice", "level": 4}]))
    (s.dir / "whitelist.json").write_text(json.dumps([{"uuid": ALICE_ONLINE, "name": "Alice"}, {"uuid": BOB_ONLINE, "name": "Bob"}]))
    (s.dir / "banned-players.json").write_text(json.dumps([{"uuid": BOB_ONLINE, "name": "bob", "reason": "x"}]))
    return s


def quiet_mods(monkeypatch):
    from mcctl import mods
    monkeypatch.setattr(mods, "add", lambda s, names, *a, **k: None)
    monkeypatch.setattr(core.Server, "has_mod", lambda self, prefix: True)


def test_going_cracked_moves_saved_data_to_the_new_ids_after_a_backup(make_server, monkeypatch):
    s = paid_server(make_server)
    quiet_mods(monkeypatch)
    alice, bob = players.offline_uuid("Alice"), players.offline_uuid("Bob")
    players.set_cracked(s, True)
    for folder, ext, data in (("playerdata", ".dat", b"alice-inv"), ("playerdata", ".dat_old", b"alice-old"),
                              ("stats", ".json", b"{}"), ("advancements", ".json", b"{}")):
        assert (s.world_dir / folder / f"{alice}{ext}").read_bytes() == data
        assert not (s.world_dir / folder / f"{ALICE_ONLINE}{ext}").exists()
    assert (s.world_dir / "playerdata" / f"{bob}.dat").read_bytes() == b"bob-inv"
    assert json.loads((s.dir / "ops.json").read_text()) == [{"uuid": alice, "name": "Alice", "level": 4}]
    assert {e["name"]: e["uuid"] for e in json.loads((s.dir / "whitelist.json").read_text())} == {"Alice": alice, "Bob": bob}
    assert json.loads((s.dir / "banned-players.json").read_text())[0]["uuid"] == bob          # name case doesn't matter
    assert {e["name"]: e["uuid"] for e in json.loads((s.dir / "usercache.json").read_text())} == {"Alice": alice, "Bob": bob}
    assert s.prop("online-mode") == "false"
    assert list(s.backups_dir.glob("pre-uuid-migration-*.zip")) and (next(s.backups_dir.glob("uuid-migration-*")) / "ops.json").exists()
    players.set_cracked(s, True)                                              # again: already offline, nothing moves twice
    assert (s.world_dir / "playerdata" / f"{alice}.dat").read_bytes() == b"alice-inv"


def test_switching_back_to_paid_uses_the_recorded_account_id_or_a_lookup(make_server, monkeypatch):
    s = paid_server(make_server)
    quiet_mods(monkeypatch)
    players.set_cracked(s, True)
    alice, bob = players.offline_uuid("Alice"), players.offline_uuid("Bob")
    # the lists now hold the offline ids, so the paid ids must come from a lookup
    looked = []
    monkeypatch.setattr(uuidmigrate, "http", lambda url: looked.append(url) or {"id": {"alice": ALICE_ONLINE, "bob": BOB_ONLINE}[
        url.rsplit("/", 1)[1].lower()].replace("-", "")})
    players.set_cracked(s, False)
    assert (s.world_dir / "playerdata" / f"{ALICE_ONLINE}.dat").read_bytes() == b"alice-inv"
    assert not (s.world_dir / "playerdata" / f"{alice}.dat").exists() and not (s.world_dir / "playerdata" / f"{bob}.dat").exists()
    assert json.loads((s.dir / "ops.json").read_text())[0]["uuid"] == ALICE_ONLINE and s.prop("online-mode") == "true"
    assert len(looked) == 2 and looked[0].startswith("https://api.mojang.com/users/profiles/minecraft/")


def test_an_unreachable_lookup_leaves_the_data_alone_and_is_tried_once(make_server, monkeypatch):
    s = make_server(props={"online-mode": "false"})
    alice = players.offline_uuid("Alice")
    with_world(s, {f"playerdata/{alice}.dat": b"inv"})
    (s.dir / "whitelist.json").write_text(json.dumps([{"uuid": alice, "name": "Alice"}, {"uuid": players.offline_uuid("Bob"), "name": "Bob"}]))
    calls = []
    monkeypatch.setattr(uuidmigrate, "http", lambda url: calls.append(url) or (_ for _ in ()).throw(Fail("Network error")))
    quiet_mods(monkeypatch)
    plan = uuidmigrate.plan(s, to_offline=False)
    assert plan.moves == [] and sorted(plan.unknown) == ["Alice", "Bob"]                    # both are on the lists: both would be locked out
    assert len(calls) == 1                                                                   # gave up after one failure, not one per player
    assert [line for line in uuidmigrate.describe(plan) if "no paid account ID found" in line] != []


def test_a_running_server_is_never_changed_under_its_feet(make_server, monkeypatch):
    s = paid_server(make_server)
    quiet_mods(monkeypatch)
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: True)
    with pytest.raises(Fail, match=r"Stop 'pytest-a' first.*--dry-run"):
        players.set_cracked(s, True)
    assert s.prop("online-mode") == "true" and (s.world_dir / "playerdata" / f"{ALICE_ONLINE}.dat").exists()
    assert not list(s.backups_dir.glob("*")) if s.backups_dir.exists() else True


def test_a_dry_run_shows_the_plan_and_changes_nothing(make_server, monkeypatch, capsys):
    s = paid_server(make_server)
    quiet_mods(monkeypatch)
    players.set_cracked(s, True, dry_run=True)
    out = capsys.readouterr().out
    assert "Alice: 4 file(s), 3 list entries" in out and "Bob: 1 file(s), 3 list entries" in out
    assert s.prop("online-mode") == "true" and (s.world_dir / "playerdata" / f"{ALICE_ONLINE}.dat").exists()
    assert not s.backups_dir.exists() or not list(s.backups_dir.glob("*"))


def test_no_migrate_switches_the_mode_and_leaves_the_data(make_server, monkeypatch):
    s = paid_server(make_server)
    quiet_mods(monkeypatch)
    players.set_cracked(s, True, migrate=False)
    assert s.prop("online-mode") == "false" and (s.world_dir / "playerdata" / f"{ALICE_ONLINE}.dat").exists()


def test_nothing_is_ever_overwritten(make_server, monkeypatch):
    s = paid_server(make_server)
    quiet_mods(monkeypatch)
    alice = players.offline_uuid("Alice")
    (s.world_dir / "playerdata" / f"{alice}.dat").write_bytes(b"already-here")
    plan = uuidmigrate.plan(s, to_offline=True)
    assert [c.name for c in plan.conflicts] == [f"{alice}.dat"]
    players.set_cracked(s, True)
    assert (s.world_dir / "playerdata" / f"{alice}.dat").read_bytes() == b"already-here"        # kept as it was
    assert (s.world_dir / "playerdata" / f"{ALICE_ONLINE}.dat").read_bytes() == b"alice-inv"     # and the source stayed too


def test_a_backup_that_does_not_verify_stops_everything(make_server, monkeypatch):
    s = paid_server(make_server)
    quiet_mods(monkeypatch)
    monkeypatch.setattr(backups, "status", lambda p: "corrupt")
    with pytest.raises(Fail, match="failed verification, so nothing was changed"):
        players.set_cracked(s, True)
    assert (s.world_dir / "playerdata" / f"{ALICE_ONLINE}.dat").exists() and s.prop("online-mode") == "true"


def test_a_server_with_no_player_data_just_switches(make_server, monkeypatch, capsys):
    s = make_server(props={"online-mode": "true"})
    quiet_mods(monkeypatch)
    players.set_cracked(s, True)
    assert s.prop("online-mode") == "false" and "No saved player data to move" in capsys.readouterr().out


def test_the_cracked_command_has_the_new_options(make_server, monkeypatch):
    from typer.testing import CliRunner
    s = paid_server(make_server)
    quiet_mods(monkeypatch)
    r = CliRunner().invoke(cli.app, ["-s", s.name, "cracked", "on", "--dry-run"])
    assert r.exit_code == 0 and "Alice: 4 file(s)" in r.output
    r = CliRunner().invoke(cli.app, ["-s", s.name, "cracked", "on", "--no-migrate"])
    assert r.exit_code == 0 and s.prop("online-mode") == "false"


# ---------- housekeeping: lock files are mc's own business ----------
def test_lock_files_are_hidden_from_the_file_browser_and_left_out_of_clones(make_server):
    from mcctl import files
    s = make_server()
    with s.op_lock("backing up"):
        s.update(autobackup=True)
    (s.dir / "mods").mkdir()
    (s.dir / "mods" / "a.jar").write_bytes(b"x")
    assert {".mc-lock", ".mc-config.lock"} <= files.HIDDEN_NAMES
    assert (s.dir / ".mc-lock").exists() and (s.dir / ".mc-config.lock").exists()
    (s.dir / ".mc-lock").write_text("marker-lock")
    (s.dir / ".mc-config.lock").write_text("marker-config")
    servers.clone(s.name, "pytest-copy", port=25577)
    copy = core.Server("pytest-copy")
    assert (copy.dir / "mods" / "a.jar").exists()                          # the real content came along
    assert not (copy.dir / ".mc-lock").exists()                            # the source's operation lock did not
    # the copy makes its own settings lock when it saves its settings, but the source's contents never travelled
    assert "marker" not in (copy.dir / ".mc-config.lock").read_text()
