"""Mod lockfile logic with Modrinth calls faked out."""
from mcctl import core, mods


def fake_http(table):
    def http(url, data=None):
        for key, value in table.items():
            if key in url:
                return value(data) if callable(value) else value
        raise AssertionError(f"unexpected request {url}")
    return http


def test_sync_tracks_modrinth_and_manual_jars(make_server, monkeypatch):
    s = make_server()
    s.mods_dir.mkdir()
    (s.mods_dir / "known.jar").write_bytes(b"known")
    (s.mods_dir / "custom.jar").write_bytes(b"custom")
    known_sha = core.sha1(s.mods_dir / "known.jar")
    monkeypatch.setattr(mods, "http", fake_http({
        "/version_files": lambda data: {known_sha: {"id": "v1", "project_id": "p1", "version_number": "1.0"}},
        "/projects?": [{"id": "p1", "slug": "known", "title": "Known Mod", "client_side": "optional",
                        "server_side": "required"}],
    }))
    entries, manual = mods.sync(s)
    assert entries["p1"].slug == "known" and entries["p1"].filename == "known.jar"
    assert manual == ["custom.jar"]
    # second sync uses the lockfile without asking Modrinth again for known jars
    monkeypatch.setattr(mods, "http", fake_http({"/version_files": {}, "/projects?": []}))
    entries2, _ = mods.sync(s)
    assert entries2["p1"].version_number == "1.0"


def test_pin_survives_resync(make_server, monkeypatch):
    s = make_server()
    s.mods_dir.mkdir()
    (s.mods_dir / "a.jar").write_bytes(b"a")
    sha = core.sha1(s.mods_dir / "a.jar")
    monkeypatch.setattr(mods, "http", fake_http({
        "/version_files": lambda d: {sha: {"id": "v", "project_id": "pa", "version_number": "2"}},
        "/projects?": [{"id": "pa", "slug": "a", "title": "A"}],
    }))
    mods.sync(s)
    mods.pin(s, "a")
    entries, _ = mods.sync(s)
    assert entries["pa"].pinned


def test_vanilla_server_refuses_mods(make_server):
    s = make_server(loader="vanilla")
    try:
        mods.sync(s)
    except core.Fail as e:
        assert "vanilla" in str(e)
    else:
        raise AssertionError("expected Fail")
