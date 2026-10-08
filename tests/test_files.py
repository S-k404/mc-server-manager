"""Safe file editing (Files tab): confinement, validation, versions, atomic writes, no-overwrite imports."""
import json
import os

import pytest

from mcctl import files
from mcctl.core import Fail


@pytest.fixture
def srv(make_server):
    s = make_server()
    (s.dir / "config").mkdir()
    (s.dir / "config" / "mod.toml").write_text("a = 1\n")
    return s


def test_paths_are_confined_to_the_server_folder(srv, tmp_path):
    assert files.resolve(srv, "server.properties") == (srv.dir / "server.properties").resolve()
    for bad in ("../outside.txt", "/etc/passwd", "config/../../x"):
        with pytest.raises(Fail, match="outside"):
            files.resolve(srv, bad)
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    os.symlink(secret, srv.dir / "link.txt")                     # a symlink pointing out of the folder
    with pytest.raises(Fail, match="outside"):
        files.read_text(srv, "link.txt")
    with pytest.raises(Fail, match="outside"):
        files.save_text(srv, "link.txt", "overwritten")
    assert secret.read_text() == "nope"


def test_only_small_text_files_are_editable(srv):
    (srv.dir / "blob.bin").write_bytes(b"\x00\x01\x02" * 10)
    (srv.dir / "huge.txt").write_text("x" * (files.MAX_EDIT_BYTES + 1))
    (srv.dir / "latin.txt").write_bytes("café".encode("utf-8"))
    assert files.is_text(srv.dir / "config" / "mod.toml") and files.is_text(srv.dir / "latin.txt")
    assert not files.is_text(srv.dir / "blob.bin") and not files.is_text(srv.dir / "huge.txt")
    assert not files.is_text(srv.dir / "config")
    with pytest.raises(Fail, match="small text file"):
        files.read_text(srv, "blob.bin")


def test_save_keeps_previous_version_and_writes_atomically(srv):
    p = srv.dir / "config" / "mod.toml"
    os.chmod(p, 0o640)
    assert files.save_text(srv, "config/mod.toml", "a = 2\n") == "saved"
    assert p.read_text() == "a = 2\n" and (p.stat().st_mode & 0o777) == 0o640     # mode preserved
    assert not list(srv.dir.rglob("*.mc-tmp"))                                     # no temp file left behind
    versions = files.list_versions(srv, "config/mod.toml")
    assert len(versions) == 1 and versions[0].read_text() == "a = 1\n"
    files.save_text(srv, "config/mod.toml", "a = 3\n")
    assert [v.read_text() for v in files.list_versions(srv, "config/mod.toml")] == ["a = 2\n", "a = 1\n"]


def test_json_must_stay_valid_and_nothing_is_written_when_it_is_not(srv):
    (srv.dir / "ops.json").write_text("[]")
    with pytest.raises(Fail, match="valid JSON"):
        files.save_text(srv, "ops.json", "[ broken")
    assert (srv.dir / "ops.json").read_text() == "[]"
    assert not (srv.backups_dir / "file-edits").exists()
    files.save_text(srv, "ops.json", json.dumps([{"name": "Steve"}]))
    assert json.loads((srv.dir / "ops.json").read_text())[0]["name"] == "Steve"


def test_instance_json_is_validated_against_the_schema(srv):
    good = json.loads(srv.cfg_path.read_text())
    bad = {**good, "port": 99999}
    with pytest.raises(Fail, match="isn't valid"):
        files.save_text(srv, "instance.json", json.dumps(bad))
    assert json.loads(srv.cfg_path.read_text())["port"] == good["port"]
    files.save_text(srv, "instance.json", json.dumps({**good, "ram_gb": 3}))
    assert srv.inst.ram_gb == 3


def test_files_the_running_server_rewrites_cannot_be_saved_while_it_runs(srv, monkeypatch):
    monkeypatch.setattr(type(srv), "up", lambda self: True)
    with pytest.raises(Fail, match="Stop"):
        files.save_text(srv, "server.properties", "motd=x\n")
    assert files.save_text(srv, "config/mod.toml", "a = 9\n") == "restart"        # others are fine; restart needed
    monkeypatch.setattr(type(srv), "up", lambda self: False)
    files.save_text(srv, "server.properties", "motd=x\n")


def test_mc_state_folder_is_off_limits(srv):
    (srv.dir / ".mc").mkdir()
    (srv.dir / ".mc" / "exit").write_text("0")
    with pytest.raises(Fail, match="mc's own state"):
        files.save_text(srv, ".mc/exit", "1")


def test_import_file_copies_without_overwriting(srv, tmp_path):
    src = tmp_path / "datapack.zip"
    src.write_bytes(b"PK data")
    dest = files.import_file(srv, src, "config")
    assert dest == (srv.dir / "config" / "datapack.zip").resolve() and dest.read_bytes() == b"PK data"
    with pytest.raises(Fail, match="already exists"):
        files.import_file(srv, src, "config")
    with pytest.raises(Fail, match="outside"):
        files.import_file(srv, src, "../")
    with pytest.raises(Fail, match="isn't a file"):
        files.import_file(srv, tmp_path, "config")


def test_language_detection():
    assert files.language("a/b.json") == "json" and files.language("x.YML") == "yaml"
    assert files.language("server.properties") is None
