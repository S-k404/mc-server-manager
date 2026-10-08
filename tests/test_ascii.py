"""The tool's text stays plain ASCII: no emoji or symbols in its source or stylesheet. Box-drawing borders and the block
characters used by bars and sparklines (U+2500 to U+259F) are the only exception. Marks are [ok] [x] [!], arrows are ->."""
import pathlib

from mcctl import cli, core

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "mcctl"


def allowed(ch):
    return 32 <= ord(ch) < 127 or ch in "\n\t" or 0x2500 <= ord(ch) <= 0x259F


def test_mcctl_source_and_stylesheet_are_ascii():
    bad = []
    for path in sorted(SOURCE.rglob("*")):
        if path.suffix not in (".py", ".tcss"):
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            bad += [f"{path.relative_to(SOURCE.parent)}:{number}: U+{ord(ch):04X} {ch!r}" for ch in line if not allowed(ch)]
    assert not bad, f"{len(bad)} character(s) outside ASCII and box-drawing; first few:\n" + "\n".join(bad[:8])


def test_the_ascii_marks_survive_rich_markup(capsys):
    """`[ok]` and `[x]` look like markup tags: printed carelessly they vanish. Every mark must come out literally."""
    cli.RichReporter().ok("done")
    cli.console.print(f"{cli.FAIL} broke")
    cli.console.print(f"{cli.GOOD} fine")
    out = capsys.readouterr().out
    assert "[ok] done" in out and "[x] broke" in out and "[ok] fine" in out


def test_the_plain_reporter_and_dashboard_marks_are_literal(capsys):
    core.Reporter().ok("saved")
    assert "[ok] saved" in capsys.readouterr().out
    from textual.content import Content
    from mcctl.tui import app as tui
    assert "[ok]" in Content.from_markup(tui.GOOD_MARK).plain and "[x]" in Content.from_markup(tui.BAD_MARK).plain
