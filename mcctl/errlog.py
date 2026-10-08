"""Unexpected errors (bugs, not "that name is taken"): a short message on screen, the full traceback in logs/mc-errors.log.

The log rotates at 1 MB (mc-errors.log.1 to .3 are older ones). Secrets are removed with support.redact, and only the command
words are logged, never its arguments (a password can be one). `MC_DEBUG=1` shows the traceback on screen as well.
"""
import contextlib
import os
import sys
import time
import traceback

from .core import ROOT

LOG = ROOT / "logs" / "mc-errors.log"
MAX_BYTES = 1_000_000
KEEP = 3


def _rotate():
    try:
        if LOG.stat().st_size <= MAX_BYTES:
            return
    except OSError:
        return
    for n in range(KEEP, 1, -1):
        with contextlib.suppress(OSError):
            os.replace(f"{LOG}.{n - 1}", f"{LOG}.{n}")
    with contextlib.suppress(OSError):
        os.replace(LOG, f"{LOG}.1")


def record(exc, argv=()):
    """Append the traceback of `exc` to the error log and return the log's path. Never raises (an error handler that fails
    would hide the real error)."""
    from .support import redact
    words = " ".join(list(argv)[:2]) + (" ..." if len(argv) > 2 else "")
    text = (f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} | mc {words} | python {sys.version.split()[0]} | {sys.platform}\n"
            + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) + "\n")
    with contextlib.suppress(OSError):
        LOG.parent.mkdir(exist_ok=True)
        _rotate()
        with open(LOG, "a") as f:
            f.write(redact(text))
    return LOG
