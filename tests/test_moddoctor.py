"""Mod doctor: what a mod needs vs what is installed, what the last log says, and the safe automatic fixes."""
import json
import re
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from mcctl import core, events, moddoctor, mods
from test_modmeta import jar


def fabric_jar(ident, version="1.0", depends=None, **extra):
    meta = {"id": ident, "name": ident.title(), "version": version, "depends": depends or {}, **extra}
    return jar({"fabric.mod.json": json.dumps(meta)})


def put(s, filename, data):
    s.mods_dir.mkdir(exist_ok=True)
    (s.mods_dir / filename).write_bytes(data)
    return filename


def kinds(findings):
    return sorted((f.kind, f.mod, f.need) for f in findings)


# ---------- checking the jars ----------
def test_missing_dependency_is_found_with_steps_and_an_automatic_fix(make_server):
    s = make_server()
    put(s, "cloth.jar", fabric_jar("cloth-config", depends={"fabric-api": ">=0.9", "minecraft": "~26.3"}))
    found = moddoctor.check(s)
    assert kinds(found) == [("missing", "cloth.jar", "fabric-api")]
    f = found[0]
    assert f.level == "error" and f.action == ("install", "fabric-api")
    assert "fabric-api" in f.message and ">=0.9" in f.message
    assert any("mods search fabric-api" in step for step in f.steps)
    assert any("add-jar" in step for step in f.steps)


def test_mixinextras_comes_with_fabric_loader_so_lithium_is_not_missing_it(make_server):
    """Live false alarm: the doctor said Lithium lacked mixinextras on a server that was running fine. Fabric Loader ships it."""
    s = make_server()                                                   # fabric
    put(s, "lithium.jar", fabric_jar("lithium", depends={"mixinextras": ">=0.5.5", "minecraft": "~26.3"}))
    assert moddoctor.check(s) == []
    put(s, "modernfix.jar", fabric_jar("mixinextras", version="0.5.4"))        # a jar's own older copy (live: ModernFix 0.5.4 vs >=0.5.5):
    assert moddoctor.check(s) == []                                            # the loader's newer one is the one that is used


def test_mixinextras_is_still_missing_on_forge(make_server):
    s = make_server(loader="forge")
    put(s, "lithium.jar", jar({"META-INF/mods.toml": ('modLoader="javafml"\n[[mods]]\nmodId="lithium"\nversion="1"\n'
                                                       '[[dependencies.lithium]]\nmodId="mixinextras"\nmandatory=true\n'
                                                       'versionRange="[0.5.5,)"\nside="BOTH"\n')}))
    assert [(f.kind, f.need) for f in moddoctor.check(s)] == [("missing", "mixinextras")]


def test_nothing_to_report_when_everything_is_present(make_server):
    s = make_server()
    inner = fabric_jar("fabric-resource-loader-v0")
    put(s, "api.jar", jar({"fabric.mod.json": json.dumps({"id": "fabric-api", "version": "0.100", "provides": ["fabric"],
                                                          "jars": [{"file": "META-INF/jars/rl.jar"}]}),
                           "META-INF/jars/rl.jar": inner}))
    put(s, "a.jar", fabric_jar("a", depends={"fabric-api": "*", "fabric": "*", "fabricloader": ">=0.15",
                                              "fabric-resource-loader-v0": "*", "java": ">=21", "minecraft": ">=26.3"}))
    assert moddoctor.check(s) == []                       # provides, bundled jars and the loader itself all count


def test_optional_and_own_dependencies_are_not_complaints(make_server):
    s = make_server()
    put(s, "a.jar", fabric_jar("a", depends={"a": "*"}, recommends={"nice-to-have": "*"}, suggests={"other": "*"}))
    assert moddoctor.check(s) == []


def test_wrong_version_of_a_library(make_server):
    s = make_server()
    put(s, "lib.jar", fabric_jar("lib", version="1.0"))
    put(s, "a.jar", fabric_jar("a", depends={"lib": ">=2.0"}))
    [f] = moddoctor.check(s)
    assert (f.kind, f.mod, f.need) == ("version", "a.jar", "lib") and "1.0" in f.message
    assert f.action == ()                                  # neither jar came from Modrinth: nothing mc can update


def test_wrong_version_is_fixable_when_a_modrinth_jar_is_involved(make_server):
    s = make_server()
    put(s, "lib.jar", fabric_jar("lib", version="1.0"))
    put(s, "a.jar", fabric_jar("a", depends={"lib": ">=2.0"}))
    (s.dir / "mods.lock.json").write_text(json.dumps({"schema": 1, "manual": [], "mods": {"p": {
        "project_id": "p", "slug": "lib", "title": "Lib", "version_id": "v", "version_number": "1.0",
        "filename": "lib.jar", "sha1": "x"}}}))
    [f] = moddoctor.check(s)
    assert f.action == ("update",)
    entries, manual = mods.read_lock(s)
    entries["p"].pinned = True                             # a pinned mod is the user's decision: leave it
    mods.write_lock(s, entries, manual)
    assert moddoctor.check(s)[0].action == ()


def test_built_for_another_minecraft_or_java(make_server):
    s = make_server()                                      # Minecraft 26.3, Java 25
    put(s, "old.jar", fabric_jar("old", depends={"minecraft": "~1.20", "java": ">=17"}))
    put(s, "newjava.jar", fabric_jar("newjava", depends={"java": ">=99"}))
    by_kind = {f.kind: f for f in moddoctor.check(s)}
    assert set(by_kind) == {"game", "java"}
    assert "1.20" in by_kind["game"].message and "26.3" in by_kind["game"].message
    assert "Java 99" in by_kind["java"].message or "Java >=99" in by_kind["java"].message
    assert any("add-jar" in step for step in by_kind["game"].steps)    # not from Modrinth: download another build


def test_snapshot_version_never_produces_a_complaint(make_server):
    s = make_server(mc_version="25w14a")
    put(s, "a.jar", fabric_jar("a", depends={"minecraft": "~1.21"}))
    assert moddoctor.check(s) == []


def test_conflicts_are_reported_once(make_server):
    s = make_server()
    put(s, "a.jar", fabric_jar("a", breaks={"b": "*"}))
    put(s, "b.jar", fabric_jar("b", breaks={"a": "*"}))
    [f] = moddoctor.check(s)
    assert f.kind == "conflict" and f.level == "error" and f.action == ()
    assert "mods remove a.jar" in f.steps[0] and "mods remove b.jar" in f.steps[0]


def test_break_range_only_matches_the_named_versions(make_server):
    s = make_server()
    put(s, "a.jar", fabric_jar("a", breaks={"b": "<2.0"}))
    put(s, "b.jar", fabric_jar("b", version="2.5"))
    assert moddoctor.check(s) == []


def test_duplicate_mod_keeps_the_newer_jar(make_server):
    s = make_server()
    put(s, "sodium-1.0.jar", fabric_jar("sodium", "1.0"))
    put(s, "sodium-1.2.jar", fabric_jar("sodium", "1.2"))
    [f] = moddoctor.check(s)
    assert f.kind == "duplicate" and f.mod == "sodium-1.0.jar" and f.action == ("aside", "sodium-1.0.jar")


def test_client_only_corrupt_wrong_loader_and_plain_library_jars(make_server):
    s = make_server()
    put(s, "client.jar", fabric_jar("clientmod", environment="client"))
    put(s, "broken.jar", b"not a zip")
    put(s, "forge.jar", jar({"META-INF/mods.toml": '[[mods]]\nmodId="x"\n'}))
    put(s, "lib.jar", jar({"some/Lib.class": "x"}))
    by_mod = {f.mod: f for f in moddoctor.check(s)}
    assert by_mod["client.jar"].kind == "client" and by_mod["client.jar"].action == ("aside", "client.jar")
    assert by_mod["broken.jar"].kind == "corrupt" and by_mod["broken.jar"].level == "error"
    assert by_mod["forge.jar"].kind == "loader" and "Forge" in by_mod["forge.jar"].message
    assert by_mod["forge.jar"].action == ("aside", "forge.jar")
    assert by_mod["lib.jar"].kind == "loader" and by_mod["lib.jar"].action == ()      # might be a library: leave it


def test_forge_family_is_stricter_about_foreign_jars(make_server):
    s = make_server(loader="neoforge", mc_version="1.21.1")
    put(s, "fabric.jar", fabric_jar("x"))
    assert moddoctor.check(s)[0].level == "error"          # NeoForge refuses to start with a jar that has no mods.toml


def test_players_need_it_and_setup_notes(make_server):
    s = make_server()
    put(s, "voice.jar", fabric_jar("voicechat"))
    put(s, "hud.jar", fabric_jar("hud"))
    (s.dir / "mods.lock.json").write_text(json.dumps({"schema": 1, "manual": [], "mods": {"p": {
        "project_id": "p", "slug": "hud", "title": "Hud", "version_id": "v", "version_number": "1",
        "filename": "hud.jar", "sha1": "x", "client_side": "required"}}}))
    found = {f.kind: f for f in moddoctor.check(s)}
    assert found["setup"].level == "note" and "24454" in found["setup"].message
    assert found["players"].level == "note" and "pack export" in found["players"].steps[0]
    assert moddoctor.problems(moddoctor.check(s)) == []     # notes are not problems


def test_vanilla_and_missing_folder(make_server):
    assert moddoctor.check(make_server(name="v", loader="vanilla")) == []
    assert moddoctor.check(make_server(name="empty")) == []


def test_paper_plugins_use_plugin_names_case_insensitively(make_server):
    s = make_server(name="p", loader="paper", mc_version="1.21.1")
    put(s, "Shop.jar", jar({"plugin.yml": "name: Shop\nversion: 1\ndepend: [Vault]\napi-version: 1.21\n"}))
    assert kinds(moddoctor.check(s)) == [("missing", "Shop.jar", "vault")]
    put(s, "Vault.jar", jar({"plugin.yml": "name: Vault\nversion: 1.7\n"}))
    assert moddoctor.check(s) == []
    put(s, "Future.jar", jar({"plugin.yml": "name: Future\nversion: 1\napi-version: 1.99\n"}))
    assert kinds(moddoctor.check(s)) == [("game", "Future.jar", "minecraft")]


def test_exclude_shows_what_a_removal_would_break(make_server):
    s = make_server()
    put(s, "lib.jar", fabric_jar("lib"))
    put(s, "a.jar", fabric_jar("a", depends={"lib": "*"}))
    assert moddoctor.check(s) == []
    assert kinds(moddoctor.check(s, exclude={"lib.jar"})) == [("missing", "a.jar", "lib")]


def test_preview_a_jar_before_installing_it(make_server, tmp_path):
    s = make_server()
    put(s, "lib.jar", fabric_jar("lib"))
    needy = tmp_path / "needy.jar"
    needy.write_bytes(fabric_jar("needy", depends={"lib": "*", "other-lib": "*"}))
    meta, new = moddoctor.preview(s, needy)
    assert meta.id == "needy"
    assert kinds(new) == [("missing", "needy.jar", "other-lib")]     # lib is installed, other-lib is not
    fine = tmp_path / "fine.jar"
    fine.write_bytes(fabric_jar("fine", depends={"lib": "*"}))
    assert moddoctor.preview(s, fine)[1] == []
    assert not (s.mods_dir / "needy.jar").exists()                    # previewing installs nothing


# ---------- reading the last log ----------
FABRIC_LOG = """[22:15:01] [main/ERROR]: Incompatible mods found!
net.fabricmc.loader.impl.FormattedException: Some of your mods are incompatible with the game or each other!
A potential solution has been determined, this may resolve your problem:
\t - Install fabric-api, version 0.92.0 or later.
\t - Remove mod 'Sodium' (sodium).
More details:
\t - Mod 'Cloth Config' (cloth-config) 11.1 requires version 0.92.0 or later of fabric-api, which is missing!
\t - Mod 'Other' (other) 1.0 requires any version of some-lib, which is missing!
"""


def write_log(s, text):
    (s.dir / "logs").mkdir(exist_ok=True)
    s.log_path.write_text(text)


def test_fabric_log_is_turned_into_findings(make_server):
    s = make_server()
    put(s, "cloth.jar", fabric_jar("cloth-config"))
    write_log(s, FABRIC_LOG)
    found = moddoctor.diagnose(s)
    missing = {(f.mod, f.need) for f in found if f.kind == "missing"}
    assert ("cloth.jar", "fabric-api") in missing and ("other", "some-lib") in missing   # jar known -> file name
    assert all(f.source == "log" for f in found)
    conflict = next(f for f in found if f.kind == "conflict")
    assert "Fabric suggests: Remove mod 'Sodium' (sodium)." in conflict.steps          # Fabric's own wording is kept
    assert not any(f.need == "fabric-api" and not f.mod for f in found)               # "Install fabric-api" not repeated


def test_log_and_jar_findings_are_not_reported_twice(make_server):
    s = make_server()
    put(s, "cloth.jar", fabric_jar("cloth-config", depends={"fabric-api": "*"}))
    write_log(s, FABRIC_LOG)
    found = moddoctor.collect(s)
    assert len([f for f in found if (f.kind, f.mod, f.need) == ("missing", "cloth.jar", "fabric-api")]) == 1
    assert [f.source for f in found if f.need == "fabric-api"] == ["jars"]          # the installed file wins
    only_log = [f for f in found if f.source == "log" and f.kind == "missing"]
    assert [f.need for f in only_log] == ["some-lib"]
    assert moddoctor.lines(s, only_log)[0][1].endswith("(seen in the last server log)  (mc can fix this)")


def test_forge_and_bukkit_logs(make_server):
    s = make_server(name="f", loader="forge", mc_version="1.20.1")
    write_log(s, "Missing or unsupported mandatory dependencies:\n"
                 "\tMod ID: 'create', Requested by: 'addon', Expected range: '[0.5.1,)', Actual version: '[MISSING]'\n"
                 "\tMod ID: 'flywheel', Requested by: 'addon', Expected range: '[0.6,)', Actual version: '0.5.0'\n")
    got = {(f.kind, f.need) for f in moddoctor.diagnose(s)}
    assert got == {("missing", "create"), ("version", "flywheel")}
    p = make_server(name="p", loader="paper", mc_version="1.21.1")
    write_log(p, "[Server thread/ERROR]: Could not load 'plugins/Shop.jar' in folder 'plugins'\n"
                 "org.bukkit.plugin.UnknownDependencyException: Vault\n\tat org.bukkit...\n"
                 "[Server thread/ERROR]: Unknown/missing dependency plugins: [WorldGuard, Essentials]. "
                 "Please download and install these plugins to run 'Claims'.\n")
    assert {(f.mod, f.need) for f in moddoctor.diagnose(p)} == {
        ("Shop", "vault"), ("Claims", "worldguard"), ("Claims", "essentials")}


def test_java_version_error_names_the_jar_and_the_needed_java(make_server):
    s = make_server()
    put(s, "newjava.jar", jar({"fabric.mod.json": json.dumps({"id": "nj", "version": "1"}), "com/foo/Bar.class": "x"}))
    write_log(s, "java.lang.UnsupportedClassVersionError: com/foo/Bar has been compiled by a more recent version of "
                 "the Java Runtime (class file version 69.0), this version of the Java Runtime only recognizes class "
                 "file versions up to 65.0\n")
    [f] = moddoctor.diagnose(s)
    assert f.kind == "java" and f.mod == "newjava.jar" and "Java 25" in f.message and "runs Java 21" in f.message


def test_mixin_failure_points_at_the_mod_and_levels_follow_the_log(make_server):
    s = make_server()
    put(s, "x.jar", fabric_jar("xmod"))
    write_log(s, "[22:00:00] [main/ERROR]: Mixin apply for mod xmod failed xmod.mixins.json:FooMixin from mod xmod\n")
    [f] = moddoctor.diagnose(s)
    assert (f.kind, f.mod, f.level) == ("mixin", "x.jar", "error")
    write_log(s, "[22:00:00] [main/WARN]: Mixin apply for mod xmod failed xmod.mixins.json:FooMixin from mod xmod\n")
    assert moddoctor.diagnose(s)[0].level == "warn"        # a warning in the log must not be called a crash


def test_crash_report_is_only_read_when_the_log_names_it(make_server):
    s = make_server()
    crashes = s.dir / "crash-reports"
    crashes.mkdir()
    (crashes / "crash-old.txt").write_text(FABRIC_LOG)
    write_log(s, "[22:00:00] [main/INFO]: all fine\n")
    assert moddoctor.diagnose(s) == []                     # an old report from some earlier run is not this run's problem
    write_log(s, "This crash report has been saved to: ./crash-reports/crash-old.txt\n")
    assert moddoctor.diagnose(s)


def test_no_log_no_findings(make_server):
    assert moddoctor.diagnose(make_server()) == []


# ---------- fixing ----------
def fake_modrinth(monkeypatch, projects):
    """projects: {slug: (filename, jar bytes)}. Search returns every slug, in order."""
    blobs = {}

    def http(url, data=None):
        if "/search?" in url:
            return {"hits": [{"slug": slug} for slug in projects]}
        if "/version_files" in url:
            return {}
        m = re.search(r"/project/([^/?]+)/version", url)
        slug = urllib.parse.unquote(m.group(1))
        if slug not in projects:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        filename, blob = projects[slug]
        blobs[f"https://cdn.test/{filename}"] = blob
        return [{"version_type": "release", "version_number": "1", "dependencies": [], "files": [
            {"primary": True, "url": f"https://cdn.test/{filename}", "filename": filename, "hashes": {"sha1": "ignored"}}]}]

    def download(url, dest, sha1_expected=None, sha256_expected=None):
        Path(dest).write_bytes(blobs[url])
        return Path(dest)

    monkeypatch.setattr(mods, "http", http)
    monkeypatch.setattr(moddoctor, "download", download)


def test_fix_installs_the_missing_library_and_the_one_it_needs(make_server, monkeypatch):
    s = make_server()
    put(s, "cloth.jar", fabric_jar("cloth-config", depends={"fabric-api": "*"}))
    fake_modrinth(monkeypatch, {
        "fabric-api": ("fabric-api-0.100.jar", fabric_jar("fabric-api", depends={"fabric-language": "*"})),
        "fabric-language": ("fabric-language-1.0.jar", fabric_jar("fabric-language"))})
    done, left = moddoctor.fix(s)
    assert (s.mods_dir / "fabric-api-0.100.jar").exists() and (s.mods_dir / "fabric-language-1.0.jar").exists()
    assert len(done) == 2 and left == []                   # the second round found what the first library needed


def test_fix_never_keeps_a_jar_that_is_not_the_mod_it_looked_for(make_server, monkeypatch):
    s = make_server()
    put(s, "a.jar", fabric_jar("a", depends={"cloth-config2": "*"}))
    fake_modrinth(monkeypatch, {"cloth-config": ("wrong.jar", fabric_jar("something-else")),
                                "other": ("right.jar", fabric_jar("cloth-config2"))})
    done, left = moddoctor.fix(s)
    assert not (s.mods_dir / "wrong.jar").exists()         # same slug family, different mod id: rejected
    assert (s.mods_dir / "right.jar").exists() and left == []


def test_fix_tells_you_when_nothing_on_modrinth_matches(make_server, monkeypatch):
    s = make_server()
    put(s, "a.jar", fabric_jar("a", depends={"curseforge-only": "*"}))
    fake_modrinth(monkeypatch, {})
    done, left = moddoctor.fix(s)
    assert done == [] and kinds(left) == [("missing", "a.jar", "curseforge-only")]
    assert any("add-jar" in step for step in left[0].steps)           # falls back to the by-hand steps


def test_fix_survives_modrinth_answering_with_something_unexpected(make_server, monkeypatch):
    s = make_server()
    offline = lambda url, data=None: (_ for _ in ()).throw(urllib.error.URLError("offline"))
    # no "hits" key / a list / not JSON-shaped / unreachable: each must degrade to "install it by hand"
    for reply in ({}, [], "<html>blocked</html>", offline):
        put(s, "needy.jar", fabric_jar("needy", depends={"lib": "*"}))
        put(s, "client.jar", fabric_jar("clientmod", environment="client"))
        monkeypatch.setattr(mods, "http", reply if callable(reply) else (lambda url, data=None, r=reply: r))
        done, left = moddoctor.fix(s)                                    # must not raise
        assert (s.mods_dir / "needy.jar").exists() and not (s.mods_dir / "client.jar").exists()
        assert kinds(left) == [("missing", "needy.jar", "lib")]          # still reported, with its by-hand steps
        assert any("add-jar" in step for step in left[0].steps)


def test_fix_moves_useless_jars_aside_and_never_deletes(make_server, monkeypatch):
    s = make_server()
    put(s, "sodium-1.0.jar", fabric_jar("sodium", "1.0"))
    put(s, "sodium-1.2.jar", fabric_jar("sodium", "1.2"))
    put(s, "client.jar", fabric_jar("clientmod", environment="client"))
    put(s, "broken.jar", b"junk")
    fake_modrinth(monkeypatch, {})
    done, left = moddoctor.fix(s)
    assert sorted(p.name for p in s.mods_dir.glob("*.jar")) == ["sodium-1.2.jar"]
    kept = sorted(p.name.split("-", 2)[2] for p in (s.backups_dir / "mods-removed").iterdir())
    assert kept == ["broken.jar", "client.jar", "sodium-1.0.jar"]       # all recoverable
    assert left == []
    logged = [json.loads(line)["text"] for line in events.path().read_text().splitlines()]
    assert any("moved sodium-1.0.jar" in text for text in logged)                    # and it is in the event log


def test_fix_updates_mods_built_for_another_version_and_stops_when_nothing_changes(make_server, monkeypatch):
    s = make_server()
    put(s, "lib.jar", fabric_jar("lib", "1.0"))
    put(s, "a.jar", fabric_jar("a", depends={"lib": ">=2.0"}))
    (s.dir / "mods.lock.json").write_text(json.dumps({"schema": 1, "manual": [], "mods": {"p": {
        "project_id": "p", "slug": "lib", "title": "Lib", "version_id": "v", "version_number": "1.0",
        "filename": "lib.jar", "sha1": "x"}}}))
    calls = []
    monkeypatch.setattr(mods, "update", lambda s_: calls.append(1) or 0)      # nothing newer exists
    done, left = moddoctor.fix(s)
    assert calls == [1] and done == []                     # tried once, did not loop
    assert kinds(left) == [("version", "a.jar", "lib")]


def test_fix_needs_a_stopped_server_and_a_modded_one(make_server, monkeypatch):
    s = make_server()
    monkeypatch.setattr(core.Server, "up", lambda self, sessions=None: True)
    with pytest.raises(core.Fail, match="Stop 'pytest-a'"):
        moddoctor.fix(s)
    with pytest.raises(core.Fail, match="vanilla"):
        moddoctor.fix(make_server(name="v", loader="vanilla"))


def test_log_findings_that_were_fixed_do_not_come_back_from_the_stale_log(make_server, monkeypatch):
    s = make_server()
    put(s, "cloth.jar", fabric_jar("cloth-config"))                       # the jar itself declares nothing
    write_log(s, FABRIC_LOG)                                              # but the last start complained
    fake_modrinth(monkeypatch, {"fabric-api": ("fabric-api.jar", fabric_jar("fabric-api"))})
    done, left = moddoctor.fix(s)
    assert (s.mods_dir / "fabric-api.jar").exists()
    assert not any(f.need == "fabric-api" for f in left)
    assert ("missing", "other", "some-lib") in {(f.kind, f.mod, f.need) for f in left}   # not found: still open


# ---------- hooks in the other commands ----------
def test_add_jar_says_what_the_new_mod_needs(make_server, monkeypatch, tmp_path, capsys):
    s = make_server()
    monkeypatch.setattr(mods, "http", lambda url, data=None: {})
    src = tmp_path / "needy-1.0.jar"
    src.write_bytes(fabric_jar("needy", depends={"fabric-api": "*"}))
    mods.add_jar(s, src)
    out = capsys.readouterr().out
    assert "needs more before the server will start" in out and "needs fabric-api" in out
    ok = tmp_path / "fine-1.0.jar"
    ok.write_bytes(fabric_jar("fine"))
    mods.add_jar(s, ok)
    assert "fine-1.0.jar needs more" not in capsys.readouterr().out


def test_remove_warns_about_mods_left_without_their_library(make_server, monkeypatch, capsys):
    s = make_server()
    put(s, "lib.jar", fabric_jar("lib"))
    put(s, "a.jar", fabric_jar("a", depends={"lib": "*"}))
    monkeypatch.setattr(mods, "http", lambda url, data=None: {})
    mods.remove(s, ["lib.jar"])
    out = capsys.readouterr().out
    assert not (s.mods_dir / "lib.jar").exists()
    assert "without something they need" in out and "A needs lib" in out


def test_remove_stays_quiet_when_nothing_depended_on_it(make_server, monkeypatch, capsys):
    s = make_server()
    put(s, "solo.jar", fabric_jar("solo"))
    monkeypatch.setattr(mods, "http", lambda url, data=None: {})
    mods.remove(s, ["solo.jar"])
    assert "without something" not in capsys.readouterr().out


def test_mc_doctor_reports_mod_problems(make_server, monkeypatch):
    from mcctl import network
    s = make_server()
    put(s, "a.jar", fabric_jar("a", depends={"missing-lib": "*"}))
    monkeypatch.setattr(network, "installed_javas", lambda: [25])
    monkeypatch.setattr(network.platform, "vpn_active", lambda: False)      # same stubs as test_docker.py
    monkeypatch.setattr(network.platform, "lan_ip", lambda: "10.0.0.5")
    checks = [c for c in network.doctor([s]) if c.message.startswith("mods:")]
    assert len(checks) == 1 and not checks[0].good and "A needs missing-lib" in checks[0].message
    assert "mods doctor --fix" in checks[0].fix


# ---------- the command line ----------
def run_cli(*args):
    from typer.testing import CliRunner
    from mcctl.cli import app
    return CliRunner().invoke(app, list(args))


def test_cli_doctor_prints_brackets_in_jar_names_literally(make_server):
    s = make_server()
    put(s, "[fabric] cool [1.21].jar", b"junk")             # Rich would swallow [fabric] and [1.21] as markup
    put(s, "needy.jar", fabric_jar("needy", depends={"lib": "*"}))
    r = run_cli("-s", s.name, "mods", "doctor")
    assert r.exit_code == 0, r.output
    assert "2 problem(s) found" in r.output and "[fabric] cool [1.21].jar is not a valid jar" in r.output
    assert "Needy needs lib" in r.output and "--fix" in r.output


def test_cli_doctor_on_a_jar_you_have_not_installed(make_server, tmp_path):
    s = make_server()
    p = tmp_path / "x.jar"
    p.write_bytes(fabric_jar("x", "2.0", depends={"lib": ">=1", "fabricloader": "*"}))
    r = run_cli("-s", s.name, "mods", "doctor", str(p))
    assert r.exit_code == 0, r.output
    assert "X 2.0 (x), a Fabric mod" in r.output and "Needs: lib >=1" in r.output and "needs lib" in r.output
    q = tmp_path / "ok.jar"
    q.write_bytes(fabric_jar("ok"))
    assert "Nothing missing" in run_cli("-s", s.name, "mods", "doctor", str(q)).output


def test_cli_doctor_on_a_clean_server_and_on_vanilla(make_server):
    s = make_server()
    put(s, "a.jar", fabric_jar("a"))
    assert "No missing mods" in run_cli("-s", s.name, "mods", "doctor").output
    v = make_server(name="v", loader="vanilla")
    r = run_cli("-s", v.name, "mods", "doctor")
    assert isinstance(r.exception, core.Fail) and "vanilla" in str(r.exception)   # main() prints a Fail as one red line


# ---------- the dashboard ----------
def test_mods_tab_finds_and_fixes_problems(make_server, monkeypatch):
    import asyncio
    from mcctl.tui import app as tui
    from mcctl.tui.modals import Confirm, TextResult
    from test_tui import ready
    s = make_server("pytest-doctor")
    put(s, "needy.jar", fabric_jar("needy", depends={"lib": "*"}))
    put(s, "client.jar", fabric_jar("clientmod", environment="client"))
    monkeypatch.setattr(mods, "http", lambda url, data=None: {})

    async def go():
        app = tui.Dashboard(target=s.name)
        async with app.run_test(size=(150, 60)) as pilot:
            await ready(app, pilot)
            app.action_tab("mods")
            await pilot.pause(0.5)
            app.query_one("#mods-doctor").press()
            for _ in range(30):
                await pilot.pause(0.1)
                if isinstance(app.screen, TextResult):
                    break
            assert isinstance(app.screen, TextResult)
            assert "2 problem(s) found" in app.screen.body and "Needy needs lib" in app.screen.body
            assert "[x]" in app.screen.body                       # shown literally: the dialog is not markup
            await pilot.press("escape")
            await pilot.pause(0.3)
            app.query_one("#mods-fix").press()
            await pilot.pause(0.3)
            assert isinstance(app.screen, Confirm)
            await pilot.press("enter")                            # fix: the client-only jar is moved aside
            for _ in range(50):
                await pilot.pause(0.1)
                if isinstance(app.screen, TextResult):
                    break
            assert isinstance(app.screen, TextResult)
            assert "moved client.jar aside" in app.screen.body and "Needy needs lib" in app.screen.body
            assert not (s.mods_dir / "client.jar").exists() and (s.mods_dir / "needy.jar").exists()
    asyncio.run(go())


# ---------- Simple Voice Chat (config semantics read from the mod's own ServerConfig.java) ----------
def voice_server(make_server, props=None, name="pytest-voice", **inst):
    s = make_server(name, **inst)
    put(s, "voicechat.jar", fabric_jar("voicechat", "2.5"))
    if props is not None:
        cfg = (s.mods_dir / "voicechat") if s.loader_info.content == "plugins" else (s.dir / "config" / "voicechat")
        cfg.mkdir(parents=True)
        (cfg / "voicechat-server.properties").write_text("# Simple Voice Chat server config\n" + "".join(
            f"{k}={v}\n" for k, v in props.items()))
    return s


def voice(findings, kind="setup"):
    return [f for f in findings if f.mod == "voicechat.jar" and f.kind == kind]


def test_voice_chat_before_its_config_exists(make_server):
    s = voice_server(make_server)
    [note] = voice(moddoctor.check(s))
    assert note.level == "note" and "start the server once" in note.message and "24454" in note.message


def test_voice_chat_names_the_configured_port(make_server):
    s = voice_server(make_server, {"port": "25000", "bind_address": ""})
    [note] = voice(moddoctor.check(s))
    assert "UDP port 25000" in note.message and "need the mod installed on their side" in note.message
    assert moddoctor.problems(moddoctor.check(s)) == []
    assert "24454" in voice(moddoctor.check(voice_server(make_server, {"bind_address": ""}, name="v2")))[0].message  # default


def test_voice_chat_on_minecrafts_own_port_is_flagged_with_the_mods_own_warning(make_server):
    s = voice_server(make_server, {"port": "-1"})
    [note] = voice(moddoctor.check(s))
    assert "shares Minecraft's own port" in note.message and "crash the server" in note.message
    assert "own port" in note.steps[0]


def test_blank_bind_address_follows_server_ip(make_server):
    loop = voice_server(make_server, {"port": "24454", "bind_address": ""}, name="a", )
    loop.set_prop("server-ip", "127.0.0.1")
    [bad] = voice(moddoctor.check(loop), "voicechat")
    assert bad.level == "error" and "only listens on 127.0.0.1" in bad.message and "nobody else can reach it" in bad.message
    assert "bind_address=*" in bad.steps[0]
    lan = voice_server(make_server, {"bind_address": ""}, name="b")
    lan.set_prop("server-ip", "192.0.2.5")
    assert voice(moddoctor.check(lan), "voicechat")[0].level == "warn"
    star = voice_server(make_server, {"bind_address": "*"}, name="c")
    star.set_prop("server-ip", "127.0.0.1")
    assert voice(moddoctor.check(star), "voicechat") == []                # an explicit '*' wins over server-ip
    plain = voice_server(make_server, {"bind_address": ""}, name="d")
    assert voice(moddoctor.check(plain), "voicechat") == []               # no server-ip: listens everywhere


def test_voice_chat_config_lives_in_plugins_on_paper(make_server):
    s = make_server("p", loader="paper", mc_version="1.21.1")
    put(s, "voicechat.jar", jar({"plugin.yml": "name: voicechat\nversion: 2.5\n"}))
    (s.mods_dir / "voicechat").mkdir()
    (s.mods_dir / "voicechat" / "voicechat-server.properties").write_text("port=24500\n")
    assert "UDP port 24500" in voice(moddoctor.check(s))[0].message


def test_running_server_with_nothing_listening_on_the_voice_port(make_server, monkeypatch):
    import os
    import socket
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    port = udp.getsockname()[1]
    try:
        s = voice_server(make_server, {"port": str(port), "bind_address": "*"})
        monkeypatch.setattr(core.Server, "state", lambda self, sessions=None: "running")
        monkeypatch.setattr(core.Server, "java_pid", lambda self: os.getpid())      # this process really holds the UDP socket
        assert voice(moddoctor.check(s), "voicechat") == []
        udp.close()                                                               # now nothing listens
        [bad] = voice(moddoctor.check(s), "voicechat")
        assert bad.level == "warn" and f"nothing listens on UDP port {port}" in bad.message and "mc -s pytest-voice logs" in bad.steps[0]
    finally:
        udp.close()


def test_a_server_that_is_not_running_is_not_asked_about_its_sockets(make_server, monkeypatch):
    s = voice_server(make_server, {"port": "24454", "bind_address": "*"})
    monkeypatch.setattr(core.Server, "state", lambda self, sessions=None: "stopped")
    monkeypatch.setattr(moddoctor, "_udp_ports", lambda srv: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert voice(moddoctor.check(s), "voicechat") == []


def test_voice_chat_cannot_work_in_a_docker_server_and_follows_the_route_through_the_internet_address(make_server, monkeypatch):
    s = voice_server(make_server, {"port": "24454", "bind_address": "*"}, runtime="docker")
    [bad] = voice(moddoctor.check(s), "voicechat")
    assert "only publishes Minecraft's own port" in bad.message and "runtime native" in bad.steps[0]
    n = voice_server(make_server, {"port": "24454", "bind_address": "*"}, name="native")
    monkeypatch.setattr(moddoctor.vpn, "enabled", lambda: True)
    monkeypatch.setattr(moddoctor.vpn, "forward_server", lambda: "native")
    from mcctl import platform as plat
    monkeypatch.setattr(plat, "vpn_active", lambda: True)               # the Mac's own VPN is on: everyone arrives through the address
    notes = [f.message for f in voice(moddoctor.check(n))]
    assert any("goes through the internet address" in m and "mc vpn voice" in m for m in notes)
    monkeypatch.setattr(plat, "vpn_active", lambda: False)              # LAN play: audio goes straight to this Mac
    notes = [f.message for f in voice(moddoctor.check(n))]
    assert any("straight to this Mac" in m and "not through the internet address" in m for m in notes)
    monkeypatch.setattr(moddoctor.vpn, "forward_server", lambda: "somebody-else")
    assert not any("internet address" in f.message for f in voice(moddoctor.check(n)))
