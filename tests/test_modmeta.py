"""Reading mod/plugin metadata out of jars, and judging version requirements. Pure: no server, no network."""
import io
import json
import zipfile

import pytest

from mcctl import modmeta


def jar(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def write(tmp_path, name, files):
    p = tmp_path / name
    p.write_bytes(jar(files))
    return p


FABRIC = ("fabric.mod.json",)


def test_fabric_metadata(tmp_path):
    inner = jar({"fabric.mod.json": json.dumps({"id": "fabric-resource-loader-v0", "version": "1.2"})})
    p = write(tmp_path, "cloth.jar", {
        "fabric.mod.json": json.dumps({
            "id": "cloth-config", "name": "Cloth Config", "version": "11.1",
            "depends": {"fabric-api": ">=0.9", "minecraft": ["~1.21", "~1.20"], "java": ">=21"},
            "breaks": {"badmod": "*"}, "provides": ["cloth-old", {"id": "cloth-new", "version": "3"}],
            "environment": "*", "jars": [{"file": "META-INF/jars/inner.jar"}]}),
        "META-INF/jars/inner.jar": inner})
    m = modmeta.inspect(p, FABRIC)
    assert (m.id, m.name, m.version, m.kind) == ("cloth-config", "Cloth Config", "11.1", "fabric.mod.json")
    assert {n.id: n.range for n in m.needs} == {"fabric-api": ">=0.9", "minecraft": "~1.21 || ~1.20", "java": ">=21"}
    assert [b.id for b in m.breaks] == ["badmod"]
    # a bundled jar counts as something this jar provides
    assert m.ids() == {"cloth-config": "11.1", "cloth-old": "11.1", "cloth-new": "3", "fabric-resource-loader-v0": "1.2"}


def test_quilt_metadata_and_optional_dependencies(tmp_path):
    p = write(tmp_path, "q.jar", {"quilt.mod.json": json.dumps({
        "schema_version": 1,
        "quilt_loader": {"group": "org.x", "id": "qmod", "version": "2",
                         "depends": ["quilt_loader", {"id": "qsl", "versions": ">=1"}, {"id": "maybe", "optional": True},
                                     {"id": "org.x:grp", "versions": {"any": [">=1 <2", ">=3"]}}]},
        "minecraft": {"environment": "dedicated_server"}})})
    m = modmeta.inspect(p, ("quilt.mod.json", "fabric.mod.json"))
    assert m.id == "qmod" and m.environment == "server"
    by_id = {n.id: n for n in m.needs}
    assert by_id["maybe"].required is False and by_id["qsl"].required is True
    assert by_id["grp"].range == ">=1 <2 || >=3"            # group prefix dropped, "any" becomes alternatives


def test_forge_toml_sides_and_incompatible(tmp_path):
    toml = '''modLoader="javafml"
[[mods]]
modId="addon"
version="${file.jarVersion}"
displayName="Create Addon"
[[dependencies.addon]]
modId="create"
type="required"
versionRange="[0.5.1,)"
[[dependencies.addon]]
modId="minecraft"
mandatory=true
versionRange="[1.20.1,1.21)"
[[dependencies.addon]]
modId="shaderstuff"
type="incompatible"
versionRange="*"
[[dependencies.addon]]
modId="playeronly"
mandatory=true
side="CLIENT"
[[dependencies.addon]]
modId="nice"
type="optional"
'''
    p = write(tmp_path, "f.jar", {"META-INF/mods.toml": toml,
                                  "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\nImplementation-Version: 2.3.4\n"})
    m = modmeta.inspect(p, ("META-INF/mods.toml",))
    assert m.version == "2.3.4"                              # ${file.jarVersion} comes from the manifest
    assert [(n.id, n.style) for n in m.needs] == [("create", "maven"), ("minecraft", "maven")]
    assert [b.id for b in m.breaks] == ["shaderstuff"]       # client-only and optional entries are not needs


def test_neoforge_accepts_old_mods_toml_but_prefers_its_own(tmp_path):
    new = '[[mods]]\nmodId="new"\nversion="1"\n'
    old = '[[mods]]\nmodId="old"\nversion="1"\n'
    p = write(tmp_path, "n.jar", {"META-INF/neoforge.mods.toml": new, "META-INF/mods.toml": old})
    assert modmeta.inspect(p, ("META-INF/neoforge.mods.toml", "META-INF/mods.toml")).id == "new"
    p2 = write(tmp_path, "o.jar", {"META-INF/mods.toml": old})
    assert modmeta.inspect(p2, ("META-INF/neoforge.mods.toml", "META-INF/mods.toml")).id == "old"


def test_bukkit_plugin_yml(tmp_path):
    y = "name: Foo\nversion: '1.2'\napi-version: 1.20\ndepend: [Vault, ProtocolLib]\nsoftdepend:\n  - PlaceholderAPI\n"
    m = modmeta.inspect(write(tmp_path, "p.jar", {"plugin.yml": y}), ("plugin.yml", "paper-plugin.yml"))
    assert (m.id, m.name, m.version) == ("foo", "Foo", "1.2")
    needs = {n.id: n for n in m.needs}
    assert needs["vault"].required and needs["protocollib"].required and not needs["placeholderapi"].required
    assert needs["minecraft"].range == ">=1.20"              # api-version is a Minecraft version requirement


def test_paper_plugin_yml_wins_over_plugin_yml(tmp_path):
    y = ("name: Bar\nversion: 3\ndependencies:\n  server:\n    Vault:\n      load: BEFORE\n      required: true\n"
         "    Opt:\n      required: false\n  bootstrap:\n    Boot: {}\n")
    m = modmeta.inspect(write(tmp_path, "pp.jar", {"paper-plugin.yml": y, "plugin.yml": "name: Old\n"}),
                        ("plugin.yml", "paper-plugin.yml"))
    assert m.id == "bar"
    assert {n.id: n.required for n in m.needs} == {"vault": True, "opt": False, "boot": True}


def test_wrong_loader_unreadable_and_broken_jars(tmp_path):
    forge = write(tmp_path, "forge.jar", {"META-INF/mods.toml": '[[mods]]\nmodId="x"\n'})
    m = modmeta.inspect(forge, FABRIC)
    assert m.kind == "" and m.foreign == ("Forge",) and not m.problem
    nothing = modmeta.inspect(write(tmp_path, "lib.jar", {"a/B.class": "x"}), FABRIC)
    assert nothing.kind == "" and nothing.foreign == ()
    (tmp_path / "z.jar").write_bytes(b"not a zip")
    assert "not a readable jar" in modmeta.inspect(tmp_path / "z.jar", FABRIC).problem
    bad = modmeta.inspect(write(tmp_path, "bad.jar", {"fabric.mod.json": "{oops"}), FABRIC)
    assert "unreadable" in bad.problem and bad.kind == "fabric.mod.json"


def test_yaml_subset():
    y = modmeta.parse_yaml("# c\nname: 'A b'  # trailing\nlist:\n- x\n- y\nflow: [a, \"b\"]\nnest:\n  k:\n    on: true\nempty:\n")
    assert y == {"name": "A b", "list": ["x", "y"], "flow": ["a", "b"], "nest": {"k": {"on": True}}, "empty": None}


@pytest.mark.parametrize("version,rng,style,want", [
    ("1.21.4", "~1.21", "semver", True), ("1.22", "~1.21", "semver", False), ("1.21", "1.21", "semver", True),
    ("1.21.1", "1.21", "semver", False), ("26.3", ">=26.3", "semver", True), ("26.3", ">=1.21 <1.22", "semver", False),
    ("1.21.4", "1.21.x", "semver", True), ("1.20.4", "1.21.x", "semver", False), ("1.21", "1.21.x", "semver", True),
    ("1.21.4", ">=1.21 <1.22 || >=1.20 <1.20.5", "semver", True), ("1.21.4", "^1.20", "semver", True),
    ("2.0", "^1.20", "semver", False), ("0.2.5", "^0.2.3", "semver", True), ("0.3.0", "^0.2.3", "semver", False),
    ("21", ">=21", "semver", True), ("17", ">=21", "semver", False), ("1.21", ">= 1.20", "semver", True),
    ("1.20.1", "[1.20,1.21)", "maven", True), ("1.21", "[1.20,1.21)", "maven", False), ("1.5", "[1,)", "maven", True),
    ("0.9", "[1,)", "maven", False), ("1.2", "[1.2]", "maven", True), ("3", "[1,2),[3,4)", "maven", True),
    ("5", "[1,2),[3,4)", "maven", False), ("1", "1.0", "maven", True), ("1.0", "*", "semver", True),
    # can't tell: never a complaint
    ("25w14a", "~1.21", "semver", None), ("1.21", "weird!!", "semver", None), ("1.21", "[a,b)", "maven", None),
    ("1.21", "?", "semver", None),
])
def test_satisfies(version, rng, style, want):
    assert modmeta.satisfies(version, rng, style) is want


def test_parse_version():
    assert modmeta.parse_version("1.21.4") == (1, 21, 4)
    assert modmeta.parse_version("v2.0+build.5") == (2, 0)
    assert modmeta.parse_version("0.100.1-beta") == (0, 100, 1)
    assert modmeta.parse_version("25w14a") is None
