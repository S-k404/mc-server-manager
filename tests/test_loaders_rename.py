"""More loaders (Quilt, NeoForge, Forge, Paper), richer `create`, renaming servers/worlds, worlds that aren't called 'world'."""
import zipfile

import pytest

from mcctl import backups, core, loaders, mods, provision, servers
from mcctl.core import Fail


def mk_world(s, name=None, dims=False):
    """A fake world folder (level.dat + a region file); Paper-style sibling dimension folders when dims=True."""
    name = name or s.world_name
    for suffix in ("", "_nether", "_the_end") if dims else ("",):
        d = s.dir / (name + suffix)
        d.mkdir(parents=True, exist_ok=True)
        (d / "region.mca").write_bytes(b"x" * 2048)
        if not suffix:
            (d / "level.dat").write_bytes(b"dat")


# ---------- launch arguments / flags ----------
def test_launch_args_per_loader(make_server):
    s = make_server("a", loader="fabric")
    assert s.java_flags()[-3:] == ["-jar", "fabric-server-launch.jar", "nogui"]
    s = make_server("q", port=25570, loader="quilt")
    assert s.java_flags()[-3:] == ["-jar", "quilt-server-launch.jar", "nogui"]
    s = make_server("p", port=25571, loader="paper")
    assert s.java_flags()[-3:] == ["-jar", "server.jar", "nogui"]
    s = make_server("n", port=25572, loader="neoforge", loader_version="26.3.0.42-beta")
    assert s.java_flags()[-2:] == ["@libraries/net/neoforged/neoforge/26.3.0.42-beta/unix_args.txt", "nogui"]
    s = make_server("f", port=25573, loader="forge", loader_version="66.0.9")
    assert s.java_flags()[-2:] == ["@libraries/net/minecraftforge/forge/26.3-66.0.9/unix_args.txt", "nogui"]


def test_gc_flags_scale_with_heap_and_offer_zgc():
    small, big, z = loaders.gc_flags(4), loaders.gc_flags(16), loaders.gc_flags(4, "zgc")
    assert small[:2] == ["-Xms4G", "-Xmx4G"] and "-XX:G1HeapRegionSize=8M" in small and "-XX:+UseG1GC" in small
    assert "-XX:G1HeapRegionSize=16M" in big and "-XX:G1NewSizePercent=40" in big
    assert "-XX:+UseZGC" in z and not any("G1" in f for f in z)


def test_gc_setting_is_validated_and_saved(make_server):
    from mcctl import settings_schema
    s = make_server()
    settings_schema.apply(s, "gc", "zgc")
    assert s.inst.gc == "zgc" and "-XX:+UseZGC" in s.java_flags()
    with pytest.raises(Fail):
        settings_schema.apply(s, "gc", "serial")


# ---------- version mapping ----------
def test_neoforge_versions_map_to_minecraft_versions():
    assert provision.neoforge_mc("21.1.172") == "1.21.1"
    assert provision.neoforge_mc("21.0.5") == "1.21"
    assert provision.neoforge_mc("20.4.237") == "1.20.4"
    assert provision.neoforge_mc("26.3.0.42-beta") == "26.3"
    assert provision.neoforge_mc("26.1.2.7") == "26.1.2"


def test_neoforge_prefers_stable_and_highest_build(monkeypatch):
    monkeypatch.setattr(provision, "http", lambda url, data=None: {"versions": [
        "26.3.0.9-beta", "26.3.0.42-beta", "26.2.0.88", "26.2.0.9", "26.2.0.100-beta"]})
    assert provision.neoforge_version("26.2") == "26.2.0.88"          # stable beats a newer beta
    assert provision.neoforge_version("26.3") == "26.3.0.42-beta"     # only betas exist: take the newest
    with pytest.raises(Fail):
        provision.neoforge_version("25.9")


def test_forge_prefers_recommended(monkeypatch):
    monkeypatch.setattr(provision, "http", lambda url, data=None: {"promos": {
        "26.2-latest": "65.1.3", "26.2-recommended": "65.1.0", "26.3-latest": "66.0.9"}})
    assert provision.forge_version("26.2") == "65.1.0"
    assert provision.forge_version("26.3") == "66.0.9"


def test_paper_build_prefers_stable_channel(monkeypatch):
    def dl(n):
        return {"server:default": {"url": f"https://x/{n}.jar", "checksums": {"sha256": "ab" * 32}}}
    monkeypatch.setattr(provision, "http", lambda url, data=None: [
        {"id": 3, "channel": "BETA", "downloads": dl(3)}, {"id": 2, "channel": "STABLE", "downloads": dl(2)}])
    assert provision.paper_build("26.3")[0] == "2"
    monkeypatch.setattr(provision, "http", lambda url, data=None: [{"id": 5, "channel": "BETA", "downloads": dl(5)}])
    assert provision.paper_build("26.3")[0] == "5" and provision.paper_build("26.3")[3] == "BETA"


def test_compatible_loaders_follow_each_loaders_version_list(monkeypatch):
    table = {"fabric": {"26.3", "1.21.1"}, "quilt": {"26.3"}, "paper": {"1.21.1"}, "neoforge": {"26.3"},
             "forge": set(), "vanilla": None}
    monkeypatch.setattr(provision, "supported_versions", lambda loader: table[loader])
    assert provision.compatible_loaders("26.3") == ["fabric", "quilt", "neoforge", "vanilla"]
    assert provision.compatible_loaders("1.21.1") == ["fabric", "paper", "vanilla"]
    table["quilt"] = None                      # list couldn't be fetched: don't hide the loader
    assert "quilt" in provision.compatible_loaders("1.21.1")


def test_paper_install_checks_the_sha256(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(provision, "version_info", lambda v: ("u", "s", 25))
    monkeypatch.setattr(provision, "paper_build", lambda v: ("143", "https://x/paper.jar", "ab" * 32, "BETA"))
    monkeypatch.setattr(provision, "download", lambda url, dest, sha1_expected=None, sha256_expected=None:
                        seen.update(url=url, dest=dest.name, sha256=sha256_expected))
    assert provision.install_server_files(tmp_path, "26.3", "paper") == (25, "143")
    assert seen == {"url": "https://x/paper.jar", "dest": "server.jar", "sha256": "ab" * 32}


# ---------- create ----------
@pytest.fixture
def fake_install(monkeypatch):
    def install(dest, version, loader, loader_version=None):
        (dest / "server.jar").write_bytes(b"jar")
        return 25, loader_version or ("1" if loader != "vanilla" else None)
    monkeypatch.setattr(provision, "install_server_files", install)
    monkeypatch.setattr(provision, "ensure_java", lambda major: None)
    monkeypatch.setattr(provision, "latest_release", lambda: "26.3")
    monkeypatch.setattr(provision, "compatible_loaders", lambda v: ["fabric", "quilt", "neoforge", "forge", "paper", "vanilla"])
    monkeypatch.setattr(servers.mods, "add", lambda *a, **k: None)


def test_create_applies_properties_world_name_and_gc(fake_install):
    s = servers.create("surv", "26.3", "paper", 25580, 6, offline=False, whitelist=False,
                       props={"motd": "Hi there", "difficulty": "hard", "max-players": "12", "level-seed": "12345",
                              "gamemode": None}, world_name="my-world", gc="zgc")
    assert s.prop("motd") == "Hi there" and s.prop("difficulty") == "hard" and s.prop("max-players") == "12"
    assert s.prop("level-seed") == "12345" and s.prop("level-name") == "my-world" and s.prop("white-list") == "false"
    assert s.inst.loader == "paper" and s.inst.gc == "zgc" and s.inst.ram_gb == 6
    assert s.mods_dir == s.dir / "plugins" and s.mods_dir.is_dir()


def test_create_rejects_bad_input_before_downloading(fake_install, monkeypatch):
    monkeypatch.setattr(provision, "install_server_files", lambda *a, **k: pytest.fail("downloaded"))
    with pytest.raises(Fail):
        servers.create("a", "26.3", "fabric", props={"difficulty": "brutal"})      # not a valid difficulty
    with pytest.raises(Fail):
        servers.create("b", "26.3", "fabric", props={"server-port": "1"})          # not settable here
    with pytest.raises(Fail):
        servers.create("c", "26.3", "fabric", world_name="../etc")
    with pytest.raises(Fail):
        servers.create("d", "26.3", "fabric", gc="serial")
    with pytest.raises(Fail):
        servers.create("e", "26.3", "sponge")
    assert not (core.SERVERS / "a").exists()


def test_create_refuses_a_loader_with_no_build_for_the_version(fake_install, monkeypatch):
    monkeypatch.setattr(provision, "compatible_loaders", lambda v: ["fabric", "vanilla"])
    with pytest.raises(Fail):
        servers.create("old", "1.16.5", "forge")
    assert not (core.SERVERS / "old").exists()


# ---------- rename server ----------
def test_rename_moves_folder_backups_and_pointers(make_server):
    s = make_server("old-name")
    mk_world(s)
    s.backups_dir.mkdir(parents=True)
    (s.backups_dir / "world-1.zip").write_bytes(b"z")
    core.save_setting("default", "old-name")
    core.save_setting("vpn_forward_server", "old-name")
    new = servers.rename("old-name", "new-name")
    assert new.name == "new-name" and new.inst.name == "new-name"
    assert not (core.SERVERS / "old-name").exists() and (core.SERVERS / "new-name" / "world" / "level.dat").exists()
    assert (core.BACKUPS / "new-name" / "world-1.zip").exists() and not (core.BACKUPS / "old-name").exists()
    assert core.setting("default") == "new-name" and core.setting("vpn_forward_server") == "new-name"


def test_rename_refuses_bad_names_clashes_and_running_servers(make_server, monkeypatch):
    make_server("one")
    make_server("two", port=25570)
    with pytest.raises(Fail):
        servers.rename("one", "two")                # taken
    with pytest.raises(Fail):
        servers.rename("one", "Bad Name!")
    (core.BACKUPS / "three").mkdir()
    with pytest.raises(Fail):
        servers.rename("one", "three")              # leftover backups folder would be mixed in
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: True)
    with pytest.raises(Fail):
        servers.rename("one", "uno")
    assert (core.SERVERS / "one").exists() and (core.SERVERS / "two").exists()


# ---------- rename world ----------
def test_rename_world_moves_folder_and_level_name(make_server):
    s = make_server()
    mk_world(s)
    servers.rename_world(s, "Survival World")
    assert s.prop("level-name") == "Survival World" and s.world_dir == s.dir / "Survival World"
    assert (s.dir / "Survival World" / "level.dat").exists() and not (s.dir / "world").exists()


def test_rename_world_moves_paper_dimension_folders_too(make_server):
    s = make_server(loader="paper")
    mk_world(s, dims=True)
    servers.rename_world(s, "lobby")
    assert {p.name for p in s.dir.iterdir() if p.name.startswith("lobby")} == {"lobby", "lobby_nether", "lobby_the_end"}
    assert not (s.dir / "world").exists() and not (s.dir / "world_nether").exists()


def test_rename_world_refuses_clashes_bad_names_and_running(make_server, monkeypatch):
    s = make_server()
    mk_world(s)
    (s.dir / "other").mkdir()
    with pytest.raises(Fail):
        servers.rename_world(s, "other")
    for bad in ("../x", "a/b", ".hidden", "", "x" * 41, "trailing."):
        with pytest.raises(Fail):
            servers.rename_world(s, bad)
    with pytest.raises(Fail):
        servers.rename_world(s, "world")            # same name
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: True)
    with pytest.raises(Fail):
        servers.rename_world(s, "fresh")
    assert (s.dir / "world" / "level.dat").exists()


# ---------- backups follow the real world folders ----------
def test_backup_includes_a_renamed_world_and_paper_dimensions(make_server):
    s = make_server(loader="paper")
    s.set_prop("level-name", "lobby")
    mk_world(s, dims=True)
    z = backups.make_backup(s)
    names = set(zipfile.ZipFile(z).namelist())
    assert {"lobby/level.dat", "lobby_nether/region.mca", "lobby_the_end/region.mca"} <= names
    assert backups.status(z) == "verified"


def test_restore_goes_into_the_current_world_name_and_keeps_the_old_world(make_server):
    s = make_server(loader="paper")
    mk_world(s, dims=True)
    z = backups.make_backup(s)                       # taken while the world was called "world"
    servers.rename_world(s, "arena")
    (s.dir / "arena" / "level.dat").write_bytes(b"changed")
    backups.restore(s, z.name)
    assert (s.dir / "arena" / "level.dat").read_bytes() == b"dat"
    assert (s.dir / "arena_nether" / "region.mca").exists() and (s.dir / "arena_the_end" / "region.mca").exists()
    kept = [p for p in s.backups_dir.iterdir() if p.name.startswith("world-before-restore")]
    assert kept and (kept[0] / "level.dat").read_bytes() == b"changed"      # the replaced world was moved, not deleted


# ---------- add-ons ----------
def test_add_jar_checks_the_loader_specific_marker(make_server, tmp_path):
    def jar(name, member):
        p = tmp_path / name
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(member, "x")
        return p
    paper = make_server("pp", port=25601, loader="paper")
    with pytest.raises(Fail):
        mods.add_jar(paper, jar("fab.jar", "fabric.mod.json"))                  # a Fabric mod can't go in plugins/
    quilt = make_server("qq", port=25602, loader="quilt")
    monkey_sync = mods.sync
    mods.sync = lambda s: ({}, [])
    try:
        assert mods.add_jar(quilt, jar("ok.jar", "fabric.mod.json")) == "ok.jar"  # Quilt runs Fabric mods
        assert mods.add_jar(paper, jar("plug.jar", "plugin.yml")) == "plug.jar"
        assert (paper.dir / "plugins" / "plug.jar").exists() and (quilt.dir / "mods" / "ok.jar").exists()
    finally:
        mods.sync = monkey_sync
    forge = make_server("ff", port=25603, loader="forge")
    with pytest.raises(Fail):
        mods.add_jar(forge, jar("notforge.jar", "plugin.yml"))


def test_mods_use_the_right_modrinth_loaders_and_project_type(make_server, monkeypatch):
    seen = {}
    monkeypatch.setattr(mods, "http", lambda url, data=None: seen.update(url=url) or {"hits": []})
    mods.search(make_server("sq", port=25604, loader="quilt"), "x")
    assert "categories%3Aquilt" in seen["url"] and "categories%3Afabric" in seen["url"] and "project_type%3Amod" in seen["url"]
    mods.search(make_server("sp", port=25605, loader="paper"), "x")
    assert "project_type%3Aplugin" in seen["url"] and "categories%3Apaper" in seen["url"]
    with pytest.raises(Fail):
        mods.search(make_server("sv", port=25606, loader="vanilla"), "x")


def test_clone_keeps_installer_made_libraries_but_not_the_world(make_server):
    for loader, keeps in (("neoforge", True), ("fabric", False)):
        src = make_server(f"src-{loader}", port=25610 if keeps else 25611, loader=loader, loader_version="1")
        (src.dir / "libraries").mkdir()
        (src.dir / "libraries" / "x.jar").write_bytes(b"x")
        mk_world(src)
        (src.dir / "config").mkdir()
        (src.dir / "config" / "a.toml").write_text("a=1")
        dst = servers.clone(src.name, f"dst-{loader}", port=25620 if keeps else 25621)
        assert (dst.dir / "libraries" / "x.jar").exists() is keeps
        assert (dst.dir / "config" / "a.toml").exists() and not (dst.dir / "world").exists()
