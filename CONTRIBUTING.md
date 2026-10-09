# Contributing

Thanks for wanting to help. This page covers how to set up, test and send a change. Please also read the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to help

- **Report a bug.** Open an issue with your operating system and `python3 --version` and, if it is about a server, `mc support` (it strips passwords,
  keys and addresses, but read it before you paste it).
- **Try it on a system it was not made on.** It is developed on macOS (Apple Silicon); the tests also run on Ubuntu in CI. Other Linux
  distributions are expected to work but nobody has checked.
- **Pick an open issue.** Say you are on it in the issue first, so two people do not build the same thing.
- **Improve the docs.** Anything you had to guess is a documentation bug.

Security problems go through [SECURITY.md](SECURITY.md), not a public issue.

## Set up

You need `git`, `tmux` and [uv](https://docs.astral.sh/uv/) (`./install.sh` installs uv for you).

```sh
git clone https://github.com/S-k404/mc-server-manager.git
cd mc-server-manager
./install.sh                    # creates .venv/ and puts `mc` on your PATH
uv run pytest -q                # a few minutes: the dashboard tests drive the real interface
```

To try a change without touching your own servers, point `mc` at a scratch folder: `MC_HOME=/tmp/mc-try mc create t --loader vanilla`.
The tests do exactly that.

## Tests

- `uv run pytest -q` runs everything. `uv run pytest tests/test_core.py -q -k backup` runs part of it.
- The suite is **offline**: it makes no network calls and starts no real server, so it needs no Docker, Java or tmux.
- A test never touches real data: `tests/conftest.py` points `MC_HOME` at a temporary folder.
- CI re-runs a failing test up to twice (`--reruns 2`), because the dashboard tests drive a real terminal interface against deadlines that a slow shared runner can miss. A real bug still
  fails all three times. Locally the suite runs strict; if a dashboard test fails only now and then, make it wait for a condition instead of a delay.
- Dashboard tests wait with `ready(app, pilot)`, never with a fixed sleep.
- **Code that depends on the operating system** is tested by patching the facts it reads (`platform.IS_MAC`...), so one machine
  covers both systems, as `tests/test_agent_label.py` does for launchd.
- Add a test with every behaviour change. A bug fix starts with a test that fails.

## Style and checks

```sh
uv run ruff check                                   # lint (the rules are in pyproject.toml)
uvx --from shellcheck-py shellcheck -s sh mc install.sh
```

CI runs both, and the tests on Ubuntu and macOS.

## Conventions

The ones that matter most:

1. **One service layer.** The CLI (`cli.py`) and the dashboard (`tui/`) call the same functions; do not copy logic into the dashboard.
2. **Code that depends on the operating system lives in `mcctl/platform.py`.** A feature that works on one system only must say so
   and fail with a message, never a traceback.
3. **Plain ASCII** in everything under `mcctl/` (`tests/test_ascii.py` enforces it). Marks are `[ok]` `[x]` `[!]`, arrows are `->`.
4. **Never delete a world or a server.** Move it into `backups/`.
5. **Writes are atomic** (`write_atomic`, `write_json_atomic`); a server operation that changes files runs inside
   `with s.op_lock("what")`; big operations call `core.require_disk` first.
6. **Validate anything a person typed** before it reaches a server console or a shell (`players.valid_name`, `game.player`, ...).
7. **Verify downloads** by the checksum their source publishes (`core.download(url, dest, sha1_expected=...)`, or `sha256_expected`).
   Use `core.http` and `core.download` for network calls rather than `urllib` directly.
8. **`mc` never changes another app's settings, routes or firewall.** It reads them and says what to switch.
9. **No personal data in the repository.** No home folders, hostnames, real player names, locations or addresses, in code, docs,
   tests or screenshots; test data uses fictional names. Run `uv run python scripts/make_screenshots.py` for pictures: it builds a
   made-up demo.
10. A new command: run `mc docs` to regenerate [docs/COMMANDS.md](docs/COMMANDS.md). A new editable setting: define it in
    `mcctl/settings_schema.py` so `mc set` and the dashboard both get it.

## Commits and pull requests

- Keep a pull request to one change, and say what it does and why. Link the issue it closes.
- Write commit messages in the imperative (`Add ...`, `Fix ...`).
- Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md).
- CI must be green. If it is red for a reason that is not yours, say so on the pull request.

## Releasing (maintainers)

1. Move the **Unreleased** entries in `CHANGELOG.md` under a new version heading with today's date.
2. Set `version` in `pyproject.toml`, and the number in `UA` in `mcctl/core.py` (the user agent sent to Modrinth and others).
3. Tag it (`git tag vX.Y.Z`), push the tag, and create a GitHub release from the changelog entry.
