"""Small widgets that make dashboard text selectable and copyable.

Textual can select text in labels, but not in `RichLog` (our console/activity logs), `DataTable`, or anything
rendered from a Rich `Table`. These subclasses add that, so drag-select + Ctrl+C (or just drag: the app copies
on mouse-up) works everywhere text is shown.
"""
import time

from rich.segment import Segment
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.strip import Strip
from textual.widgets import DataTable, RichLog, Static


class CopyLog(RichLog):
    """A RichLog whose lines can be selected with the mouse and copied."""

    def get_selection(self, selection):
        text = "\n".join(strip.text.rstrip() for strip in self.lines)
        return selection.extract(text), "\n"

    def selection_updated(self, selection):
        self.refresh()

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        # the offset metadata is what lets Textual map a mouse position to a character
        strip = super().render_line(y).apply_offsets(scroll_x, scroll_y + y)
        selection = self.text_selection
        if selection is None:
            return strip
        span = selection.get_span(scroll_y + y)
        if span is None:
            return strip
        width = strip.cell_length
        start, end = span
        start = max(0, start - scroll_x)
        end = width if end == -1 else min(width, end - scroll_x)
        end = min(end, max(len(strip.text.rstrip()), start + 1))   # don't paint the empty space after the text
        if end <= start:
            return strip
        style = self.screen.get_component_rich_style("screen--selection")
        mid = Strip([Segment(seg.text, (seg.style + style) if seg.style else style, seg.control)
                     for seg in strip.crop(start, end)], end - start)   # selection colours win over the line's own
        return Strip.join([strip.crop(0, start), mid, strip.crop(end, width)])

    def all_text(self):
        return "\n".join(strip.text.rstrip() for strip in self.lines)


class CopyTable(DataTable):
    """A DataTable where `c` copies the highlighted row (tab-separated, so it pastes into a spreadsheet)."""

    BINDINGS = [Binding("c", "copy_row", "Copy row", show=False)]

    def action_copy_row(self):
        if self.row_count == 0 or self.cursor_type == "none":
            return
        row = self.get_row_at(self.cursor_row)
        text = "\t".join(c.plain if isinstance(c, Text) else str(c) for c in row).strip()
        if text:
            self.app.copy_to_clipboard(text)
            self.app.notify(text if len(text) < 60 else text[:57] + "...", title="Copied row", timeout=3)


def kv_text(rows, gap=3):
    """Aligned 'label  value' lines as one Text (selectable, unlike a Rich Table). Values may be str or Text."""
    width = max((len(k) for k, _ in rows), default=0)
    out = Text(no_wrap=False)
    for i, (key, value) in enumerate(rows):
        if i:
            out.append("\n")
        out.append(key.ljust(width + gap), style="bold")
        out.append_text(value if isinstance(value, Text) else Text(str(value)))
    return out


class Rows:
    """Collects `add_row(label, value)` calls and renders them as selectable, aligned text."""

    def __init__(self):
        self.rows = []

    def add_row(self, label, value, note=None):
        if note:
            value = Text.assemble(value if isinstance(value, Text) else Text(str(value)), "   ", (str(note), "grey70"))
        self.rows.append((label, value))

    def text(self):
        return kv_text(self.rows)


def _shape(content):
    """(number of lines, widest line in cells): what decides how big an auto-sized widget has to be."""
    from rich.cells import cell_len
    text = content.plain if isinstance(content, Text) else str(content)
    lines = text.split("\n")
    return len(lines), max(cell_len(line) for line in lines)


def put(widget, content, layout=None):
    """widget.update(content) without the work when nothing changed.

    Identical content is skipped. The layout pass (Static.update does a whole-screen one by default) is skipped too when
    the new content is exactly the same size as the last, which is the common case for a number ticking over. Anything
    that could change the size, including the first content, lays out normally; pass layout= to force either way."""
    last = getattr(widget, "_put_last", None)
    if last is not None and last == content:
        return
    if layout is None:
        try:
            layout = last is None or _shape(last) != _shape(content)
        except Exception:                                   # not text (a Rich table, say): always lay out
            layout = True
    widget._put_last = content
    widget.update(content, layout=layout)


# ---------- dashboard "stat tile" building blocks ----------
GOOD, WARN, BAD, MUTED, BLUE = "#5ac46f", "#f0a23a", "#ef5d5d", "#2c3944", "#4aa3df"
YELLOW = "#f2d04a"
GLYPHS = "▁▂▃▄▅▆▇█"


def mix(a, b, f):
    """Blend two #rrggbb colours: f=0 is a, f=1 is b."""
    f = max(0.0, min(1.0, f))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * f):02x}" for x, y in zip(ca, cb))


def level_color(pct, warn=75, bad=90):
    """Smooth green -> yellow -> red: green up to half of `warn`, exactly yellow at `warn`, exactly red from `bad`."""
    pct = max(0.0, pct or 0.0)
    start = warn * 0.5
    if pct <= start:
        return GOOD
    if pct < warn:
        return mix(GOOD, YELLOW, (pct - start) / (warn - start))
    if pct < bad:
        return mix(YELLOW, BAD, (pct - warn) / (bad - warn))
    return BAD


def lag_color(tps):
    """TPS 20 is perfect (green), 18 starts to be yellow, 12 and below is red."""
    return level_color((20 - (tps or 0)) * 5, 10, 40)


def line_bar(pct, width, color=None):
    """A thin horizontal bar: coloured part + dim track."""
    pct = max(0.0, min(100.0, pct or 0.0))
    width = max(4, width)
    filled = round(pct / 100 * width)
    return Text.assemble(("━" * filled, color or level_color(pct)), ("━" * (width - filled), MUTED))


def stacked_bar(parts, width):
    """parts: [(percent, colour)] drawn left to right on one dim track (e.g. Minecraft / everything else / free)."""
    width = max(8, width)
    out, used = Text(), 0
    total = 0.0
    for pct, color in parts:
        total += max(0.0, pct)
        end = min(width, round(total / 100 * width))
        if end > used:
            out.append("━" * (end - used), style=color)
            used = end
    out.append("━" * (width - used), style=MUTED)
    return out


def core_strip(per_cpu):
    """One coloured glyph per core, taller = busier."""
    t = Text()
    for p in per_cpu:
        t.append(GLYPHS[min(7, int(max(0.0, p) / 100 * 8))] + " ", style=level_color(p, 60, 85))
    return t


class Tile(Static):
    """A small card: title on the border, one big number, an optional bar and a grey subtitle.
    The bar glides to each new value (about 30 fps) instead of jumping once a second."""

    def on_mount(self):
        self._timer = self.set_interval(1 / 20, self._step, pause=True)

    def show(self, title, value, pct=None, sub="", color=None, note=""):
        previous = getattr(self, "_args", None)
        self._args = (title, value, pct, sub, color, note)
        shown = getattr(self, "_shown", None)
        smooth = getattr(self.app, "animation_level", "full") != "none"
        if pct is None or shown is None or not smooth or previous is None:
            self._shown = pct
        glide = pct is not None and self._shown is not None and abs(self._shown - pct) > 3
        if not glide and pct is not None:
            self._shown = pct                       # glide only for a real change; a point or two of jitter just snaps
        self._draw()
        timer = getattr(self, "_timer", None)
        if timer and glide:
            timer.resume()

    def _step(self):
        pct = self._args[2] if getattr(self, "_args", None) else None
        if pct is None or self._shown is None:
            self._shown = pct
            self._timer.pause()
            return
        self._shown += (pct - self._shown) * 0.4
        if abs(self._shown - pct) < 1:
            self._shown = pct
            self._timer.pause()
        self._draw()

    def _draw(self):
        title, value, pct, sub, color, note = self._args
        self.border_title = title
        width = max(8, self.size.width - 4) if self.size.width else 16
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(str(value), style=f"bold {color or 'white'}")
        if note:
            t.append(f"  {note}", style="grey62")
        t.append("\n")
        if pct is not None:
            t.append_text(line_bar(self._shown if self._shown is not None else pct, width, color))
        t.append("\n")
        t.append(sub, style="grey62")
        put(self, t)


class CatBuddy(Static):
    """A little ASCII cat in the sidebar. It blinks, wags its tail, sleeps when the server is stopped, gets
    startled by a crash and is happy when players are online. Click it to pet it."""

    TAILS = (" )", " ~", " (", " ~")
    CAPTIONS = {"running": "all quiet on the server", "starting": "waking up the world...",
                "stopped": "zzz... (stopped)", "crashed": "the creeper did it!", "players": "friends are here!"}

    def on_mount(self):
        self.frame, self.state, self.players, self.petted_until = 0, "stopped", 0, 0.0
        self._timer = self.set_interval(0.4, self._tick)
        self._tick()

    def sleepy(self, on):
        """Stop animating while nobody is looking (terminal unfocused); resume when they are."""
        if on:
            self._timer.pause()
        else:
            self._timer.resume()

    def mood(self, state, players=0):
        self.state, self.players = state, players

    def on_click(self):
        self.petted_until = time.monotonic() + 2.5
        self._tick()

    def _tick(self):
        self.frame += 1
        f = self.frame
        petted = time.monotonic() < self.petted_until
        if petted:
            eyes, caption, extra, color = "^.^", "purr...", " <3" if f % 2 else "<3 ", "#f28fad"
        elif self.state == "crashed":
            eyes, caption, extra, color = "O.O", self.CAPTIONS["crashed"], "!", BAD
        elif self.state in ("stopped",):
            eyes, caption, color = "-.-", self.CAPTIONS["stopped"], "#7a8a99"
            extra = ("z", "zZ", "zZz")[f % 3]
        elif self.state == "starting":
            eyes, caption, extra, color = ("o.o", "O.o")[f % 2], self.CAPTIONS["starting"], "", WARN
        else:
            happy = self.players > 0
            blink = f % 11 == 0
            eyes = "-.-" if blink else ("^.^" if happy else "o.o")
            caption = self.CAPTIONS["players"] if happy else self.CAPTIONS["running"]
            extra, color = "", GOOD if happy else "#f2b84b"
        tail = self.TAILS[f % 4] if self.state not in ("stopped", "crashed") else "  "
        art = Text(no_wrap=True, overflow="ellipsis")
        art.append(f" /\\_/\\  {extra if self.state == 'stopped' else ''}\n", style=color)
        art.append(f"( {eyes} ){extra if self.state not in ('stopped',) else ''}\n", style=color)
        art.append(f" > ^ <{tail}\n", style=color)
        art.append(caption, style="grey62")
        put(self, art)


class Section(Vertical):
    """A titled, bordered group of related controls, so each job on a busy tab reads as its own block.

    `kind` colours the frame: "online" (solid green: fetched from the internet) or "file" (dashed amber: from a file on
    this Mac); anything else is the neutral frame."""

    def __init__(self, title, *children, subtitle="", kind="", **kw):
        super().__init__(*children, **kw)
        self.add_class("section")
        if kind:
            self.add_class(f"section-{kind}")
        self.border_title = title
        self.border_subtitle = subtitle


class HeatSparkline(Static):
    """A two-row bar graph whose bars shift green -> yellow -> red with how high each value is.

    `top` is the value that fills the graph (default: the largest value shown); `color_fn(value)` picks each bar's colour
    (default: a fixed `color`, or green -> red by height)."""

    def set(self, data, top=None, color_fn=None, color=None):
        self._cfg = (list(data) or [0], top, color_fn, color)
        self._draw()

    def on_resize(self, event):
        self._draw()

    def _draw(self):
        data, top, color_fn, color = getattr(self, "_cfg", ([0], None, None, None))
        width = max(8, self.size.width or 40)
        if len(data) > width:                                        # more samples than columns: each column shows its peak
            step = len(data) / width
            cells = [max(data[int(i * step):max(int((i + 1) * step), int(i * step) + 1)]) for i in range(width)]
        else:
            cells = [None] * (width - len(data)) + data              # newest on the right
        peak = float(top or max([c for c in cells if c is not None] + [1e-9]))
        upper, lower = Text(no_wrap=True), Text(no_wrap=True)
        for v in cells:
            if v is None:
                upper.append(" ")
                lower.append(" ")
                continue
            frac = max(0.0, min(1.0, v / peak))
            eighths = round(frac * 16)
            col = color or (color_fn(v) if color_fn else level_color(frac * 100, 50, 85))
            lower.append("█" if eighths > 8 else GLYPHS[max(0, eighths - 1)], style=col if eighths else MUTED)
            upper.append(GLYPHS[eighths - 9] if eighths > 8 else " ", style=col)
        put(self, Text("\n").join([upper, lower]))
