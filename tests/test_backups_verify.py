"""Backup verification, retention (count / age / size), off-machine copy and restore preview."""
import os
import time
import zipfile

import pytest

from mcctl import backups, events
from mcctl.core import Fail


def world(s, mb=0.01):
    w = s.dir / "world"
    w.mkdir(exist_ok=True)
    (w / "level.dat").write_bytes(b"x")
    (w / "region.bin").write_bytes(os.urandom(int(mb * 1e6)))


def fake_zip(s, name, age_days=0, files=None, size=None):
    s.backups_dir.mkdir(parents=True, exist_ok=True)
    p = s.backups_dir / name
    with zipfile.ZipFile(p, "w") as z:
        for fname, data in (files or {"world/level.dat": b"x", "world/r.bin": os.urandom(size or 100)}).items():
            z.writestr(fname, data)
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


def test_make_backup_verifies_and_records_a_sidecar(make_server):
    s = make_server()
    world(s)
    p = backups.make_backup(s)
    assert backups.status(p) == "verified"
    side = backups.sidecar(p)
    assert side.exists() and '"ok": true' in side.read_text()
    assert backups.sidecar(p) not in backups.list_backups(s)                       # sidecars aren't backups
    assert "verified" in events.recent(1)[0]["text"]


def test_verify_detects_corrupt_empty_and_non_zip(make_server):
    s = make_server()
    good = fake_zip(s, "world-1.zip")
    assert backups.verify(good)["ok"]
    no_level = fake_zip(s, "world-2.zip", files={"world/other.txt": b"x"})
    assert backups.verify(no_level)["problem"] == "no level.dat inside" and backups.status(no_level) == "corrupt"
    junk = s.backups_dir / "world-3.zip"
    junk.write_text("not a zip")
    assert not backups.verify(junk)["ok"] and backups.status(junk) == "corrupt"
    damaged = fake_zip(s, "world-4.zip", size=5000)
    raw = bytearray(damaged.read_bytes())
    raw[200:260] = b"\x00" * 60                                                     # flip bytes inside the compressed data
    damaged.write_bytes(bytes(raw))
    assert backups.status(damaged) == "unverified" and not backups.verify(damaged)["ok"]


def test_status_goes_back_to_unverified_when_the_file_changes(make_server):
    s = make_server()
    p = fake_zip(s, "world-1.zip")
    backups.verify(p)
    assert backups.status(p) == "verified"
    p.write_bytes(p.read_bytes() + b"tail")
    assert backups.status(p) == "unverified"
    assert backups.status(fake_zip(s, "world-2.zip")) == "unverified"               # never checked (an older backup)
    assert backups.verify_all(s)["world-2.zip"] == "verified"


def test_prune_by_count_age_and_size_never_drops_the_newest(make_server):
    s = make_server(backup_keep=3)
    for i in range(6):
        backups.verify(fake_zip(s, f"world-2026100{i}-000000.zip", age_days=(6 - i) * 2))
    assert backups.prune(s, quiet=True) == 3
    assert [p.name for p in backups.list_backups(s)][:1] == ["world-20261005-000000.zip"]
    assert not backups.sidecar(s.backups_dir / "world-20261000-000000.zip").exists()  # sidecar removed with the zip
    s.update(backup_keep=10, backup_keep_days=5)                                     # kept ones are now 6, 4 and 2 days old
    assert backups.prune(s, quiet=True) == 1
    s.update(backup_keep_days=1)
    backups.prune(s, quiet=True)
    assert [p.name for p in backups.list_backups(s)] == ["world-20261005-000000.zip"]   # only the newest survives
    big = make_server("big", backup_keep=10, backup_max_gb=1)
    for i in range(3):
        fake_zip(big, f"world-{i}.zip", age_days=3 - i, size=100)
    big.update(backup_max_gb=1)
    assert backups.prune(big, quiet=True) == 0                                       # tiny files: under 1 GB


def test_prune_keeps_the_newest_verified_backup_even_if_newer_ones_are_corrupt(make_server):
    s = make_server(backup_keep=1)
    old_good = fake_zip(s, "world-1.zip", age_days=5)
    backups.verify(old_good)
    bad = s.backups_dir / "world-2.zip"
    bad.write_text("broken")
    backups.verify(bad)
    backups.prune(s, quiet=True)
    names = {p.name for p in backups.list_backups(s)}
    assert names == {"world-1.zip", "world-2.zip"}                                   # don't throw away the only good copy


def test_restore_preview_text(make_server):
    s = make_server()
    world(s, mb=0.2)
    fake_zip(s, "world-1.zip")
    d = backups.describe(s, "1")
    assert d["name"] == "world-1.zip" and d["files"] == 2 and d["status"] == "unverified" and not d["running"]
    text = backups.describe_text(d)
    assert "world-1.zip" in text and "unverified" in text and "moved into backups/" in text and "stopped" in text
    backups.verify(s.backups_dir / "world-1.zip")
    assert "Check: verified" in backups.describe_text(backups.describe(s, "world-1.zip"))
    with pytest.raises(Fail):
        backups.describe(s, "nope")


def test_push_copies_the_newest_verified_backup_with_rsync(make_server, monkeypatch):
    from mcctl import remote
    s = make_server()
    good = fake_zip(s, "world-1.zip", age_days=2)
    backups.verify(good)
    junk = s.backups_dir / "world-2.zip"
    junk.write_text("broken")
    remote.add("vps", "me@host.example", path="~/mc")
    calls = []
    monkeypatch.setattr(backups.shutil, "which", lambda x: "/usr/bin/rsync")
    monkeypatch.setattr(backups.subprocess, "call", lambda argv: calls.append(argv) or 0)
    with pytest.raises(Fail, match="Say where"):
        backups.push(s)
    assert backups.push(s, "vps") == "world-1.zip"                                   # skips the newer corrupt one
    assert any("mkdir -p" in a[-1] and "offsite" in a[-1] for a in calls if a[0] == "ssh")
    # "~" must stay unquoted or the remote shell makes a literal "~" folder and rsync finds nothing at the real one
    assert any(a[-1].startswith("mkdir -p ~/mc/backups/") for a in calls if a[0] == "ssh")
    rsync = next(a for a in calls if a[0] == "rsync")
    assert "--delete" not in rsync and rsync[-2].endswith("world-1.zip") and rsync[-1].startswith("me@host.example:")
    s.update(backup_push_to="vps")
    assert backups.push(s) == "world-1.zip"


def test_push_refuses_when_nothing_verifiable(make_server, monkeypatch):
    from mcctl import remote
    s = make_server()
    remote.add("vps", "host.example")
    monkeypatch.setattr(backups.shutil, "which", lambda x: "/usr/bin/rsync")
    with pytest.raises(Fail, match="no backups"):
        backups.push(s, "vps")
    (s.backups_dir).mkdir(parents=True, exist_ok=True)
    (s.backups_dir / "world-1.zip").write_text("broken")
    with pytest.raises(Fail, match="verification"):
        backups.push(s, "vps")


def test_backup_settings_validate(make_server):
    from mcctl import settings_schema
    s = make_server()
    settings_schema.apply(s, "backup_keep_days", "14")
    settings_schema.apply(s, "backup_max_gb", "20")
    assert (s.inst.backup_keep_days, s.inst.backup_max_gb) == (14, 20)
    with pytest.raises(Fail):
        settings_schema.apply(s, "backup_keep_days", "-1")
    with pytest.raises(Fail, match="No remote"):
        settings_schema.apply(s, "backup_push_to", "nowhere")
    settings_schema.apply(s, "backup_push_to", "off")
    assert s.inst.backup_push_to == ""
