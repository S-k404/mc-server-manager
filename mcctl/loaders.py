"""The server software mc can create and run (Fabric, Quilt, NeoForge, Forge, Paper, vanilla).

Pure data and small helpers, no network: downloads and version lookups live in provision.py. Everything that used to
ask "is this Fabric?" asks this table instead, so a new loader is one entry here plus its installer in provision.py.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Loader:
    key: str                        # stored in instance.json
    label: str                      # shown in the create dialog
    blurb: str                      # one line of "what is this / who is it for"
    content: str = ""               # folder that holds add-ons: "mods", "plugins", or "" (none)
    modrinth: tuple = ()            # Modrinth loader names used to search / update add-ons
    markers: tuple = ()             # files inside an add-on jar that prove it is built for this loader
    separate_dimensions: bool = False   # keeps the Nether / End in <level>_nether / <level>_the_end (Bukkit family)
    login_mods: bool = False        # EasyAuth can run on it (needed for offline/cracked accounts)
    installer: bool = False         # set up by running the loader's installer jar
    project_type: str = "mod"       # Modrinth project type to search: mod | plugin


LOADERS = {
    "fabric": Loader("fabric", "Fabric", "Mods. Light and fast; the best-supported mod loader for new versions.",
                     "mods", ("fabric",), ("fabric.mod.json",), login_mods=True),
    "quilt": Loader("quilt", "Quilt", "Mods. Runs Fabric mods too, plus Quilt-only ones.",
                    "mods", ("quilt", "fabric"), ("quilt.mod.json", "fabric.mod.json"), login_mods=True, installer=True),
    "neoforge": Loader("neoforge", "NeoForge", "Mods. Big content mods and modpacks (the successor to Forge).",
                       "mods", ("neoforge",), ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"), installer=True),
    "forge": Loader("forge", "Forge", "Mods. The classic loader; older and large modpacks.",
                    "mods", ("forge",), ("META-INF/mods.toml",), installer=True),
    "paper": Loader("paper", "Paper", "Plugins. Highest performance for survival / minigame servers; no client mods.",
                    "plugins", ("paper", "bukkit", "spigot"), ("plugin.yml", "paper-plugin.yml"),
                    separate_dimensions=True, project_type="plugin"),
    "vanilla": Loader("vanilla", "Vanilla", "No mods or plugins. The official server, unchanged."),
}
KEYS = tuple(LOADERS)


def get(key):
    return LOADERS.get(key) or LOADERS["vanilla"]


def launch_args(inst):
    """Everything after the JVM flags: the jar or argument file that starts this loader, then `nogui`."""
    key = inst.loader
    if key == "fabric":
        return ["-jar", "fabric-server-launch.jar", "nogui"]
    if key == "quilt":
        return ["-jar", "quilt-server-launch.jar", "nogui"]
    if key == "neoforge":
        return [f"@libraries/net/neoforged/neoforge/{inst.loader_version}/unix_args.txt", "nogui"]
    if key == "forge":
        return [f"@libraries/net/minecraftforge/forge/{inst.mc_version}-{inst.loader_version}/unix_args.txt", "nogui"]
    return ["-jar", "server.jar", "nogui"]            # vanilla, and Paper (its jar is saved as server.jar)


def gc_flags(ram_gb, gc="g1"):
    """JVM memory + garbage-collector flags. Measured on this project's `modded` server with a Chunky pregeneration
    (same speed, 33% less total pause and a 3.6x shorter worst pause than plain G1; ZGC pauses ~1 ms but is ~25% slower
    and uses ~70% more memory, so it is opt-in)."""
    heap = [f"-Xms{ram_gb}G", f"-Xmx{ram_gb}G"]
    if gc == "zgc":
        return [*heap, "-XX:+UseZGC", "-XX:+DisableExplicitGC"]
    shared = ["-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
              "-XX:+UnlockExperimentalVMOptions", "-XX:+DisableExplicitGC", "-XX:G1HeapWastePercent=5",
              "-XX:G1MixedGCCountTarget=4", "-XX:G1MixedGCLiveThresholdPercent=90",
              "-XX:G1RSetUpdatingPauseTimePercent=5", "-XX:SurvivorRatio=32", "-XX:+PerfDisableSharedMem",
              "-XX:MaxTenuringThreshold=1"]
    if ram_gb >= 12:
        sized = ["-XX:G1NewSizePercent=40", "-XX:G1MaxNewSizePercent=50", "-XX:G1HeapRegionSize=16M",
                 "-XX:G1ReservePercent=15", "-XX:InitiatingHeapOccupancyPercent=20"]
    else:
        sized = ["-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40", "-XX:G1HeapRegionSize=8M",
                 "-XX:G1ReservePercent=20", "-XX:InitiatingHeapOccupancyPercent=15"]
    return [*heap, *shared, *sized]
