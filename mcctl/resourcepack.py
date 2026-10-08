"""Server resource pack: `resource-pack` + `resource-pack-sha1` in server.properties, filled in for you.

Players are offered the pack when they join. The sha1 is computed by downloading the file once (so it is always right),
and a 250 MB cap keeps a wrong link from filling the disk.
"""
import tempfile
import urllib.parse
from pathlib import Path

from .core import die, download, ok, sha1, warn

MAX_MB = 250


def current(s):
    return {"url": s.prop("resource-pack"), "sha1": s.prop("resource-pack-sha1"),
            "required": s.prop("require-resource-pack") == "true", "prompt": s.prop("resource-pack-prompt")}


def set_pack(s, url, required=None):
    p = urllib.parse.urlparse(url or "")
    if p.scheme not in ("http", "https") or not p.hostname or any(c in url for c in "\r\n \t"):
        die("Give a direct http(s) link to the pack's .zip (no spaces).")
    with tempfile.TemporaryDirectory() as tmp:
        f = download(url, Path(tmp) / "pack.zip")
        size = f.stat().st_size / 1e6
        if size > MAX_MB:
            die(f"That file is {size:.0f} MB; resource packs over {MAX_MB} MB can't be served.")
        import zipfile
        if not zipfile.is_zipfile(f) or "pack.mcmeta" not in zipfile.ZipFile(f).namelist():
            die("That link isn't a resource pack (a .zip with pack.mcmeta at its top).")
        digest = sha1(f)
    s.set_prop("resource-pack", url)
    s.set_prop("resource-pack-sha1", digest)
    if required is not None:
        s.set_prop("require-resource-pack", "true" if required else "false")
    ok(f"Resource pack set ({size:.1f} MB, sha1 {digest[:10]}...). Players are offered it when they join.")
    if s.up():
        warn("server.properties changed: restart for it to apply.")
    return digest


def clear(s):
    s.set_prop("resource-pack", "")
    s.set_prop("resource-pack-sha1", "")
    s.set_prop("require-resource-pack", "false")
    ok("Resource pack removed from the server.")
    if s.up():
        warn("Restart for it to apply.")
