"""An occasional cat after a command finishes. Silence it with `mc cat off` or MC_NO_CAT=1."""
import os
import random
import sys

from .core import save_setting, setting

CATS = [
    (" /\\_/\\\n( o.o )  all quiet on the server\n > ^ <", "cyan"),
    (" /\\_/\\\n( -.- )  zzz... (the world is saving)\n > ^ <", "blue"),
    (" /\\_/\\\n( ^.^ )  good block, human\n > ^ <", "green"),
    (" /\\_/\\  /\n( o.o )/   i did not push the creeper\n > ^ <", "yellow"),
    ("  |\\__/,|   (`\\\n  |_ _  |.--.) )\n  ( T   )     /\n (((^_(((/(((_/", "magenta"),
]
CHANCE = 0.12


def enabled():
    return setting("cats", True) and not os.environ.get("MC_NO_CAT") and sys.stdout.isatty()


def show(console, index=None):
    art, color = CATS[index] if index is not None else random.choice(CATS)
    console.print(f"[{color}]{art}[/]", markup=True, highlight=False)


def maybe(console):
    """Occasionally print a cat, but never after an error and never into a pipe."""
    if sys.exc_info()[0] is None and enabled() and random.random() < CHANCE:
        console.print()
        show(console)


def set_enabled(on):
    save_setting("cats", on)
