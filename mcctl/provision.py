"""Java runtimes and Minecraft / loader version lookups and downloads (Fabric, Quilt, NeoForge, Forge, Paper)."""
import re
import subprocess
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import loaders, platform
from .core import RUNTIME, Fail, die, download, http, http_text, info, ok

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_META = "https://meta.fabricmc.net/v2"
QUILT_META = "https://meta.quiltmc.org/v3"
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
NEOFORGE_VERSIONS = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
INSTALLER_TIMEOUT = 900


# ---------- Java ----------
def installed_javas():
    """Map major version -> java binary for runtimes under runtime/."""
    found = {}
    for java in platform.java_bins(RUNTIME):
        release = java.parent.parent / "release"
        m = re.search(r'JAVA_VERSION="(\d+)', release.read_text()) if release.exists() else None
        if m:
            found[int(m.group(1))] = java
    return found


def ensure_java(major):
    java = installed_javas().get(major)
    if java:
        return java
    os_name, arch = platform.java_target()
    url = f"https://api.adoptium.net/v3/binary/latest/{major}/ga/{os_name}/{arch}/jre/hotspot/normal/eclipse"
    info(f"  downloading Java {major} runtime...")
    RUNTIME.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = download(url, f"{tmp}/jre.tar.gz")
        with tarfile.open(archive) as t:
            t.extractall(RUNTIME, filter="data")
    java = installed_javas().get(major)
    if not java:
        die(f"Java {major} download did not produce a usable runtime.")
    ok(f"Java {major} installed")
    return java


# ---------- Minecraft ----------
def manifest():
    return http(MOJANG_MANIFEST)


def latest_release():
    return manifest()["latest"]["release"]


def version_info(version):
    """Return (server_url, sha1, java_major) for a Minecraft version."""
    entry = next((v for v in manifest()["versions"] if v["id"] == version), None)
    if not entry:
        die(f"Minecraft version '{version}' does not exist.")
    meta = http(entry["url"])
    server = meta.get("downloads", {}).get("server")
    if not server:
        die(f"Minecraft {version} has no official server download.")
    return server["url"], server["sha1"], meta.get("javaVersion", {}).get("majorVersion", 21)


# ---------- Fabric ----------
def fabric_loader(version):
    loaders = http(f"{FABRIC_META}/versions/loader/{version}")
    if not loaders:
        die(f"Fabric does not support Minecraft {version} yet.")
    stable = [x for x in loaders if x["loader"].get("stable")]
    return (stable or loaders)[0]["loader"]["version"]


def fabric_installer():
    installers = http(f"{FABRIC_META}/versions/installer")
    stable = [x for x in installers if x.get("stable")]
    return (stable or installers)[0]["version"]


# ---------- Quilt ----------
def quilt_loader(version):
    if version not in supported_versions("quilt"):
        die(f"Quilt does not support Minecraft {version} yet.")
    loaders_ = http(f"{QUILT_META}/versions/loader")
    stable = [x for x in loaders_ if not re.search(r"alpha|beta|rc|pre", x["version"])]
    return (stable or loaders_)[0]["version"]


def quilt_installer():
    installers = http(f"{QUILT_META}/versions/installer")
    return installers[0]


# ---------- Paper ----------
def paper_build(version):
    """(build id, download url, sha256, channel) of the best Paper build: stable if there is one, else the newest."""
    import urllib.error
    try:
        builds = http(f"{PAPER_API}/versions/{version}/builds")
    except urllib.error.HTTPError:
        die(f"Paper does not support Minecraft {version} yet.")
    if not builds:
        die(f"Paper has no build for Minecraft {version} yet.")
    best = next((b for b in builds if b.get("channel") in ("STABLE", "RECOMMENDED")), builds[0])
    d = best["downloads"]["server:default"]
    return str(best["id"]), d["url"], d["checksums"]["sha256"], best.get("channel", "")


# ---------- NeoForge / Forge ----------
def _numbers(v):
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0]))


def neoforge_mc(v):
    """The Minecraft version a NeoForge version is for: 21.1.172 -> 1.21.1, 26.3.0.42-beta -> 26.3."""
    parts = v.split("-")[0].split(".")
    if int(parts[0]) >= 26:
        mc = ".".join(parts[:-1])
        return mc[:-2] if mc.count(".") == 2 and mc.endswith(".0") else mc
    return "1." + parts[0] + ("" if parts[1] == "0" else "." + parts[1])


def neoforge_version(mc):
    cands = [v for v in http(NEOFORGE_VERSIONS)["versions"] if neoforge_mc(v) == mc]
    if not cands:
        die(f"NeoForge does not support Minecraft {mc} yet.")
    stable = [v for v in cands if "beta" not in v and "alpha" not in v]
    return max(stable or cands, key=_numbers)


def forge_version(mc):
    promos = http(FORGE_PROMOS)["promos"]
    v = promos.get(f"{mc}-recommended") or promos.get(f"{mc}-latest")
    if not v:
        die(f"Forge does not support Minecraft {mc} yet.")
    return v


def at_least(version, floor):
    return _numbers(version) >= _numbers(floor)


# ---------- what each loader supports ----------
_supported = {}
_supported_lock = threading.Lock()


def _fetch_supported(loader):
    if loader == "fabric":
        return {v["version"] for v in http(f"{FABRIC_META}/versions/game")}
    if loader == "quilt":
        return {v["version"] for v in http(f"{QUILT_META}/versions/game")}
    if loader == "paper":
        groups = http(PAPER_API)["versions"]
        return {v for vs in groups.values() for v in vs} if isinstance(groups, dict) else set(groups)
    if loader == "neoforge":
        return {neoforge_mc(v) for v in http(NEOFORGE_VERSIONS)["versions"] if at_least(neoforge_mc(v), "1.20.2")}
    if loader == "forge":
        keys = http(FORGE_PROMOS)["promos"]
        return {k.rsplit("-", 1)[0] for k in keys if k.endswith(("-latest", "-recommended"))
                and at_least(k.rsplit("-", 1)[0], "1.17")}
    return None                                            # vanilla: everything


def supported_versions(loader, ttl=900):
    """Minecraft versions this loader has a build for (None = all). Cached, so the create dialog stays quick."""
    with _supported_lock:
        hit = _supported.get(loader)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    try:
        value = _fetch_supported(loader)
    except Exception:                                      # network down: say "unknown" rather than "unsupported"
        return None
    with _supported_lock:
        _supported[loader] = (time.monotonic(), value)
    return value


def release_versions(snapshots=False):
    """Minecraft versions, newest first."""
    return [v["id"] for v in manifest()["versions"] if snapshots or v["type"] == "release"]


def compatible_loaders(version):
    """Loader keys (in menu order) that can run this Minecraft version. A loader whose list can't be fetched is kept."""
    with ThreadPoolExecutor(max_workers=len(loaders.KEYS)) as pool:
        sets = dict(zip(loaders.KEYS, pool.map(supported_versions, loaders.KEYS)))
    return [k for k in loaders.KEYS if sets[k] is None or version in sets[k]]


def loader_version(loader, version):
    """The loader build mc would install for this Minecraft version (None for vanilla)."""
    if loader == "fabric":
        return fabric_loader(version)
    if loader == "quilt":
        return quilt_loader(version)
    if loader == "neoforge":
        return neoforge_version(version)
    if loader == "forge":
        return forge_version(version)
    if loader == "paper":
        return paper_build(version)[0]
    return None


def loader_note(loader, version):
    """A short caution for the dialog: '' when the build is stable."""
    try:
        if loader == "neoforge" and "beta" in neoforge_version(version):
            return "only beta builds exist for this version"
        if loader == "paper" and paper_build(version)[3] not in ("STABLE", "RECOMMENDED"):
            return "only experimental builds exist for this version"
    except (Fail, SystemExit):
        return ""
    return ""


# ---------- installing ----------
def _maven_installer(url, dest):
    """Download an installer jar and check it against the .sha1 its maven repository publishes next to it."""
    expected = http_text(url + ".sha1").split()[0]
    return download(url, dest, expected)


def _run_installer(java, argv, cwd, what):
    info(f"  running the {what} installer (it downloads libraries; this can take a few minutes)...")
    try:
        r = subprocess.run([str(java), *argv], cwd=cwd, capture_output=True, text=True, timeout=INSTALLER_TIMEOUT)
    except subprocess.TimeoutExpired:
        die(f"The {what} installer took longer than {INSTALLER_TIMEOUT // 60} minutes; nothing was created.")
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-12:])
        die(f"The {what} installer failed (exit {r.returncode}):\n{tail}")


def install_server_files(dest, version, loader, loader_version=None):
    """Put a runnable server for this Minecraft version + loader into dest. Returns (java_major, loader_version).

    Every download is checked against a published hash; installers are run with the same Java the server will use."""
    dest = Path(dest)
    url, sha, java_major = version_info(version)
    if loader == "paper":
        build, purl, psha, channel = paper_build(version)
        info(f"  downloading Paper build {build}{'' if channel in ('STABLE', 'RECOMMENDED') else f' ({channel.lower()})'}...")
        download(purl, dest / "server.jar", sha256_expected=psha)
        return java_major, build
    info(f"  downloading Minecraft {version} server...")
    download(url, dest / "server.jar", sha)
    if loader == "fabric":
        loader_version = loader_version or fabric_loader(version)
        installer = fabric_installer()
        info(f"  downloading Fabric loader {loader_version}...")
        download(f"{FABRIC_META}/versions/loader/{version}/{loader_version}/{installer}/server/jar",
                 dest / "fabric-server-launch.jar")
    elif loader == "quilt":
        loader_version = loader_version or quilt_loader(version)
        inst = quilt_installer()
        # Quilt's meta API lists a wrong sha1 for its installer; the maven repository's own .sha1 matches the file.
        jar = _maven_installer(inst["url"], dest / "quilt-installer.jar")
        _run_installer(ensure_java(java_major), ["-jar", jar.name, "install", "server", version, loader_version,
                                                 "--install-dir=.", "--download-server"], dest, "Quilt")
        jar.unlink(missing_ok=True)
        if not (dest / "quilt-server-launch.jar").exists():
            die("The Quilt installer finished but did not create quilt-server-launch.jar.")
    elif loader == "neoforge":
        loader_version = loader_version or neoforge_version(version)
        jar = _maven_installer(f"{NEOFORGE_MAVEN}/{loader_version}/neoforge-{loader_version}-installer.jar",
                               dest / "neoforge-installer.jar")
        _run_installer(ensure_java(java_major), ["-jar", jar.name, "--installServer"], dest, "NeoForge")
        jar.unlink(missing_ok=True)
        if not (dest / "libraries/net/neoforged/neoforge" / loader_version / "unix_args.txt").exists():
            die("The NeoForge installer finished but did not create its launch files.")
    elif loader == "forge":
        loader_version = loader_version or forge_version(version)
        full = f"{version}-{loader_version}"
        jar = _maven_installer(f"{FORGE_MAVEN}/{full}/forge-{full}-installer.jar", dest / "forge-installer.jar")
        _run_installer(ensure_java(java_major), ["-jar", jar.name, "--installServer"], dest, "Forge")
        jar.unlink(missing_ok=True)
        if not (dest / "libraries/net/minecraftforge/forge" / full / "unix_args.txt").exists():
            die("The Forge installer finished but did not create its launch files.")
    else:
        loader_version = None
    return java_major, loader_version
