"""Write a file so a crash, a full disk or a reader at the wrong moment never sees half of it. No dependencies on the rest of mc:
config.py and core.py both use it (core re-exports it as core.write_atomic / core.write_json_atomic)."""
import contextlib
import json
import os
import stat
import tempfile
from pathlib import Path


def write_atomic(path, data, mode=None):
    """Write `data` (str or bytes): a temp file in the same folder, flushed to disk, then renamed over the target. An existing
    file keeps its permissions; a new one gets `mode` (default 0o644)."""
    path = Path(path)
    if mode is None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_json_atomic(path, data, indent=2, mode=None):
    write_atomic(path, json.dumps(data, indent=indent) + "\n", mode)
