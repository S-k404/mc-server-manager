## What this changes

<!-- What does it do, and why? Link the issue it closes (Closes #123). -->

## How it was tested

<!-- Tests you added or ran. For a change that depends on the operating system, say which systems you tried. -->

## Checklist

- [ ] `uv run pytest -q` and `uv run ruff check` pass
- [ ] New behaviour has a test (a bug fix starts with a test that fails)
- [ ] Anything OS-specific lives in `mcctl/platform.py`, and says so (never a traceback) where it works on one system only
- [ ] User-facing text in `mcctl/` is plain ASCII
- [ ] `CHANGELOG.md` has a line under **Unreleased**
- [ ] No personal data: no home folders, hostnames, real player names, addresses or secrets, in code, docs, tests or pictures
