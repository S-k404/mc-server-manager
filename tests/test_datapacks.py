"""Datapack and manual mod-jar import: validation, no overwrite, never touches the running server when stopped."""
import zipfile

import pytest

from mcctl import datapacks, mods
from mcctl.core import Fail


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def world(s):
    (s.dir / "world").mkdir(exist_ok=True)


def test_add_zip_and_folder(make_server, tmp_path):
    s = make_server()
    world(s)
    z = make_zip(tmp_path / "cool.zip", {"pack.mcmeta": "{}"})
    assert datapacks.add(s, z) == "cool.zip"
    folder = tmp_path / "folderpack"
    folder.mkdir()
    (folder / "pack.mcmeta").write_text("{}")
    assert datapacks.add(s, folder) == "folderpack"
    assert datapacks.list_packs(s) == ["cool.zip", "folderpack"]


def test_rejects_non_datapacks_and_duplicates(make_server, tmp_path):
    s = make_server()
    world(s)
    nested = make_zip(tmp_path / "nested.zip", {"inner/pack.mcmeta": "{}"})
    with pytest.raises(Fail, match="unzip"):
        datapacks.add(s, nested)
    with pytest.raises(Fail):
        datapacks.add(s, tmp_path / "missing.zip")
    notzip = tmp_path / "bad.zip"
    notzip.write_text("nope")
    with pytest.raises(Fail, match="valid zip"):
        datapacks.add(s, notzip)
    good = make_zip(tmp_path / "ok.zip", {"pack.mcmeta": "{}"})
    datapacks.add(s, good)
    with pytest.raises(Fail, match="already"):
        datapacks.add(s, good)


def test_needs_a_world_and_remove_keeps_a_copy(make_server, tmp_path):
    s = make_server()
    good = make_zip(tmp_path / "ok.zip", {"pack.mcmeta": "{}"})
    with pytest.raises(Fail, match="no world"):
        datapacks.add(s, good)
    world(s)
    datapacks.add(s, good)
    datapacks.remove(s, "ok.zip")
    assert datapacks.list_packs(s) == []
    assert (s.backups_dir / "datapacks" / "ok.zip").exists()


def test_add_mod_jar(make_server, tmp_path):
    s = make_server()
    s.mods_dir.mkdir(exist_ok=True)
    jar = make_zip(tmp_path / "cf-mod.jar", {"fabric.mod.json": "{}"})
    assert mods.add_jar(s, jar) == "cf-mod.jar"
    assert (s.mods_dir / "cf-mod.jar").exists()
    with pytest.raises(Fail, match="already"):
        mods.add_jar(s, jar)
    forge = make_zip(tmp_path / "forge.jar", {"META-INF/mods.toml": ""})
    with pytest.raises(Fail, match="Fabric"):
        mods.add_jar(s, forge)
    with pytest.raises(Fail, match=".jar"):
        mods.add_jar(s, tmp_path / "x.zip")


# ---------- Modrinth datapacks, resource pack, config finder ----------
import json  # noqa: E402

from mcctl import files, resourcepack  # noqa: E402


def modrinth(monkeypatch, projects, versions, updates=None):
    def fake_http(url, body=None):
        if "/search" in url:
            return {"hits": [{"slug": "cool-pack", "title": "Cool", "downloads": 5, "description": "x"}]}
        if "/version_files/update" in url:
            return updates or {}
        if "/version?" in url:
            slug = url.split("/project/")[1].split("/version")[0]
            return versions.get(slug, [])
        slug = url.split("/project/")[1]
        if slug not in projects:
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "no", {}, None)
        return projects[slug]
    monkeypatch.setattr(mods, "http", fake_http)
    downloaded = []
    monkeypatch.setattr(datapacks, "download", lambda url, dest, sha=None: (downloaded.append((url, dest.name, sha)),
                                                                           dest.write_bytes(b"zip"))[0])
    return downloaded


PROJECT = {"id": "P1", "slug": "cool-pack", "title": "Cool Pack", "project_type": "datapack"}


def version(num="1.0", name="cool.zip", sha="aa"):
    return {"id": "V" + num, "version_number": num, "version_type": "release",
            "files": [{"primary": True, "url": "https://cdn/" + name, "filename": name, "hashes": {"sha1": sha}}]}


def test_install_datapack_from_modrinth_verifies_hash_and_tracks_it(make_server, monkeypatch):
    s = make_server()
    world(s)
    downloaded = modrinth(monkeypatch, {"cool-pack": PROJECT, "a-mod": {**PROJECT, "slug": "a-mod", "id": "M", "project_type": "mod"}},
                          {"P1": [version()]})
    datapacks.install(s, ["cool-pack", "a-mod", "missing"])
    assert downloaded == [("https://cdn/cool.zip", "cool.zip", "aa")]            # sha1 passed to the verifying downloader
    assert [p["name"] for p in datapacks.installed(s)] == ["cool.zip"]
    assert datapacks.installed(s)[0] == {"name": "cool.zip", "title": "Cool Pack", "version": "1.0", "source": "Modrinth"}
    datapacks.install(s, ["cool-pack"])                                            # already installed: no second download
    assert len(downloaded) == 1
    assert json.loads((s.dir / datapacks.LOCK).read_text())["packs"]["P1"]["sha1"] == "aa"
    with pytest.raises(Fail):
        datapacks.install(s, [])


def test_install_needs_a_world_and_a_build_for_this_version(make_server, monkeypatch):
    s = make_server()
    modrinth(monkeypatch, {"cool-pack": PROJECT}, {"P1": []})
    with pytest.raises(Fail, match="no world"):
        datapacks.install(s, ["cool-pack"])
    world(s)
    datapacks.install(s, ["cool-pack"])                                            # no build: warns, installs nothing
    assert datapacks.list_packs(s) == []


def test_update_replaces_the_old_file_and_keeps_it_in_backups(make_server, monkeypatch):
    s = make_server()
    world(s)
    modrinth(monkeypatch, {"cool-pack": PROJECT}, {"P1": [version()]})
    datapacks.install(s, ["cool-pack"])
    modrinth(monkeypatch, {}, {}, updates={"aa": version("2.0", "cool-2.zip", "bb")})
    assert datapacks.update(s) == 1
    assert datapacks.list_packs(s) == ["cool-2.zip"]
    assert (s.backups_dir / "datapacks" / "cool.zip").exists()                     # never deleted
    assert datapacks.installed(s)[0]["version"] == "2.0"
    modrinth(monkeypatch, {}, {}, updates={"bb": version("2.0", "cool-2.zip", "bb")})
    assert datapacks.update(s) == 0                                                # already the newest


def test_removing_a_modrinth_pack_drops_it_from_the_lock(make_server, monkeypatch):
    s = make_server()
    world(s)
    modrinth(monkeypatch, {"cool-pack": PROJECT}, {"P1": [version()]})
    datapacks.install(s, ["cool-pack"])
    datapacks.remove(s, "cool.zip")
    assert datapacks.read_lock(s) == {}


def test_search_filters_to_datapacks_for_the_servers_version(make_server, monkeypatch):
    s = make_server()
    seen = []
    monkeypatch.setattr(mods, "http", lambda url, body=None: (seen.append(url), {"hits": []})[1])
    datapacks.search(s, "villagers")
    assert "project_type%3Adatapack" in seen[0] or "project_type:datapack" in seen[0].replace("%3A", ":")
    assert "26.3" in seen[0]


def test_resource_pack_downloads_checks_and_sets_both_properties(make_server, monkeypatch):
    s = make_server()
    pack = make_zip(s.dir / "src.zip", {"pack.mcmeta": "{}"})
    monkeypatch.setattr(resourcepack, "download", lambda url, dest: (dest.write_bytes(pack.read_bytes()), dest)[1])
    digest = resourcepack.set_pack(s, "https://example.com/pack.zip", required=True)
    cur = resourcepack.current(s)
    assert cur["url"] == "https://example.com/pack.zip" and cur["sha1"] == digest and cur["required"]
    for bad in ("", "ftp://x/y.zip", "https://x/y z.zip", "javascript:alert(1)"):
        with pytest.raises(Fail):
            resourcepack.set_pack(s, bad)
    notpack = make_zip(s.dir / "other.zip", {"readme.txt": "x"})
    monkeypatch.setattr(resourcepack, "download", lambda url, dest: (dest.write_bytes(notpack.read_bytes()), dest)[1])
    with pytest.raises(Fail, match="isn't a resource pack"):
        resourcepack.set_pack(s, "https://example.com/readme.zip")
    resourcepack.clear(s)
    assert resourcepack.current(s)["url"] == ""


def test_find_configs_matches_mod_slugs_loosely(make_server):
    s = make_server()
    cfg = s.dir / "config"
    (cfg / "lithium").mkdir(parents=True)
    (cfg / "lithium" / "options.json").write_text("{}")
    (cfg / "voicechat").mkdir()
    (cfg / "voicechat" / "voicechat-server.properties").write_text("a=b")
    (cfg / "fabric_api.toml").write_text("x=1")
    (cfg / "krypton.jar").write_bytes(b"\x00\x01")
    assert [p.name for p in files.find_configs(s, "lithium")] == ["options.json"]
    assert [p.name for p in files.find_configs(s, "voice-chat")] == ["voicechat-server.properties"]
    assert [p.name for p in files.find_configs(s, "fabric-api")] == ["fabric_api.toml"]
    assert files.find_configs(s, "krypton") == [] and files.find_configs(s, "") == []
