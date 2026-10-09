"""install.sh picks a Python only if it is new enough, and links `mc` where it is told. It runs for real against stub programs."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"
TOOLS = ("sh", "dirname", "ln", "mkdir", "echo")             # what the script needs from the system; nothing else is on PATH


@pytest.fixture
def box(tmp_path):
    """A folder holding a copy of install.sh and a fake `mc`, a PATH with only stubs and the few tools it needs, and a log of uv's arguments."""
    repo, stubs = tmp_path / "repo", tmp_path / "bin"
    repo.mkdir()
    stubs.mkdir()
    shutil.copy(SCRIPT, repo / "install.sh")
    (repo / "mc").write_text("#!/bin/sh\n")
    for tool in TOOLS:
        if found := shutil.which(tool):
            (stubs / tool).symlink_to(found)

    def python(name, new_enough):
        stub = stubs / name                                   # answers `-c 'sys.exit(sys.version_info < (3, 12))'` like the real thing
        stub.write_text(f"#!/bin/sh\nexit {0 if new_enough else 1}\n")
        stub.chmod(0o755)
        return stub

    uv = stubs / "uv"
    uv.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/uv-args"\n')
    uv.chmod(0o755)

    def run():
        env = {"PATH": str(stubs), "HOME": str(tmp_path / "home"), "MC_BIN_DIR": str(tmp_path / "links")}
        done = subprocess.run(["sh", str(repo / "install.sh")], capture_output=True, text=True, env=env)
        args = (tmp_path / "uv-args").read_text().strip() if (tmp_path / "uv-args").exists() else None
        return done, args

    return type("Box", (), {"python": staticmethod(python), "run": staticmethod(run), "stubs": stubs, "links": tmp_path / "links", "repo": repo})


def test_the_newest_python_that_is_new_enough_is_used(box):
    box.python("python3.14", True)
    box.python("python3.13", True)
    box.python("python3", True)
    done, args = box.run()
    assert done.returncode == 0, done.stderr
    assert args == f"sync --python {box.stubs / 'python3.14'}"


def test_a_python_that_is_too_old_is_skipped(box):
    box.python("python3.14", False)          # (a stub that says "older than 3.12")
    box.python("python3.12", True)
    box.python("python3", False)
    assert box.run()[1] == f"sync --python {box.stubs / 'python3.12'}"


def test_with_only_an_old_python_uv_is_left_to_fetch_one(box):
    box.python("python3", False)             # macOS's own 3.9: pinning it would make `uv sync` fail
    done, args = box.run()
    assert done.returncode == 0, done.stderr
    assert args == "sync"


def test_with_no_python_at_all_uv_is_left_to_fetch_one(box):
    assert box.run()[1] == "sync"


def test_mc_is_linked_where_it_is_told_and_tmux_is_warned_about(box):
    done, _ = box.run()
    link = box.links / "mc"
    assert link.is_symlink() and os.readlink(link) == str(box.repo / "mc")
    assert "tmux is required" in done.stderr                 # no tmux among the stubs
    assert f"mc installed in {box.links}" in done.stdout
