"""Pop-up dialogs: confirm, new server, health check."""
import re
from pathlib import Path

from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.errors import NoWidget
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, DirectoryTree, Input, Label, Select, Static, Switch

from .. import share


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def safe_content(body, markup=True):
    """Text for a Static that can never crash the dialog: ANSI codes are stripped, and a body that is not valid
    markup (container logs, '[12:00:00] [Server thread/INFO]' lines) is shown as plain text instead."""
    from textual.content import Content
    from textual.markup import MarkupError
    body = ANSI_RE.sub("", str(body))
    if markup:
        try:
            Content.from_markup(body)
            return body
        except MarkupError:
            pass
    return Text(body)


class Confirm(ModalScreen[bool]):
    """Yes/no question. Enter = yes, Esc = no."""

    BINDINGS = [("escape", "no", "Cancel"), ("enter", "yes", "Confirm")]

    def __init__(self, question, yes="Yes", danger=False):
        super().__init__()
        self.question, self.yes_label, self.danger = question, yes, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question, classes="dialog-title")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.yes_label, variant="error" if self.danger else "primary", id="yes")
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id == "yes")

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class ShareChoice(ModalScreen[str | None]):
    """First-run question: how will friends find the server? Answers 'hidden', 'local', or None (not now)."""

    BINDINGS = [("escape", "later", "Not now")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label("How should friends find your server?", classes="dialog-title")
            yield Static("Hidden: " + share.MODES["hidden"], classes="hint")
            yield Static("Local name: " + share.MODES["local"], classes="hint")
            yield Static("Hidden needs a ProtonVPN WireGuard key, so the next dialog asks for it. "
                         "You can change this any time in the Network tab (8).", classes="hint")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Hidden", variant="primary", id="hidden")
                yield Button("Local name", id="local")
                yield Button("Not now", id="later")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id if event.button.id in ("hidden", "local") else None)

    def action_later(self):
        self.dismiss(None)


class NewServer(ModalScreen[dict | None]):
    """Form for `mc create`: pick a version from a list, then only the server software that supports it.

    The version and loader lists come from the live version manifests (a worker thread, so the dialog opens at once);
    offline, it falls back to "latest release" and every loader."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    RAM_CHOICES = (1, 2, 3, 4, 6, 8, 10, 12, 16, 24, 32)
    DIFFICULTIES = (("Server default", ""), ("Peaceful", "peaceful"), ("Easy", "easy"), ("Normal", "normal"), ("Hard", "hard"))
    GAMEMODES = (("Server default", ""), ("Survival", "survival"), ("Creative", "creative"),
                 ("Adventure", "adventure"), ("Spectator", "spectator"))

    def __init__(self):
        super().__init__()
        self._want, self._shown = {}, {}       # what each dropdown should list / what it lists now (see refill)

    def on_mouse_down(self, event: events.MouseDown):
        """A click on anything that can't take focus (a label, the title, the backdrop) used to leave a dropdown open."""
        opened = [s for s in self.query(Select) if s.expanded]
        if not opened:
            return
        try:
            target, _ = self.get_widget_at(event.screen_x, event.screen_y)
        except NoWidget:
            target = None
        for select in opened:
            if target is None or select not in target.ancestors_with_self:
                select.expanded = False
                select.focus()

    def compose(self) -> ComposeResult:
        from .. import loaders
        from ..platform import total_ram_gb
        rams = [r for r in self.RAM_CHOICES if r <= max(1, total_ram_gb() - 4)] or [1]
        with Vertical(classes="dialog wide"):
            yield Label("New server", classes="dialog-title")
            with VerticalScroll(id="new-scroll"):
                with Grid(classes="form-grid"):
                    yield Label("Name")
                    yield Input(placeholder="e.g. survival, creative-2", id="name")
                    yield Label("Minecraft version")
                    yield Select([("Latest release (loading the list...)", "")], value="", allow_blank=False, id="version")
                    yield Label("Include snapshots")
                    yield Switch(value=False, id="snapshots")
                    yield Label("Server software")
                    yield Select([(info.label, info.key) for info in loaders.LOADERS.values()], value="fabric",
                                 allow_blank=False, id="loader")
                    yield Label("Memory (RAM)")
                    yield Select([(f"{r} GB", r) for r in rams], value=4 if 4 in rams else rams[-1],
                                 allow_blank=False, id="ram")
                    yield Label("Allow offline accounts")
                    yield Switch(value=True, id="offline")
                    yield Label("Whitelist")
                    yield Switch(value=True, id="whitelist")
                yield Static("", id="loader-hint", classes="hint")
                with Collapsible(title="Customise the world, rules and performance", collapsed=True, id="advanced"):
                    with Grid(classes="form-grid"):
                        yield Label("World name")
                        yield Input(placeholder="world", id="world-name")
                        yield Label("World seed")
                        yield Input(placeholder="random", id="seed")
                        yield Label("Name in the server list")
                        yield Input(placeholder="same as the server name", id="motd")
                        yield Label("Difficulty")
                        yield Select(list(self.DIFFICULTIES), value="", allow_blank=False, id="difficulty")
                        yield Label("Game mode")
                        yield Select(list(self.GAMEMODES), value="", allow_blank=False, id="gamemode")
                        yield Label("Max players")
                        yield Input(placeholder="20", id="max-players")
                        yield Label("View distance (chunks)")
                        yield Input(placeholder="10", id="view-distance")
                        yield Label("Hardcore")
                        yield Switch(value=False, id="hardcore")
                        yield Label("Port")
                        yield Input(placeholder="auto", id="port")
                        yield Label("Garbage collector")
                        yield Select([("Tuned G1 (recommended)", "g1"), ("ZGC (tiny pauses, more memory)", "zgc")],
                                     value="g1", allow_blank=False, id="gc")
                        yield Label("Runs in")
                        yield Select([("This Mac", "native"), ("Docker container", "docker")], value="native",
                                     allow_blank=False, id="runtime")
            yield Static("", id="form-error", classes="error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Create", variant="primary", id="create")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#name", Input).focus()
        self.load_versions(False)
        self.show_hint()

    # ---------- version + loader lists ----------
    @work(thread=True, exclusive=True, group="new-versions")
    def load_versions(self, snapshots):
        from .. import provision
        try:
            versions = provision.release_versions(snapshots)
        except Exception:
            versions = []
        self.app.call_from_thread(self.fill_versions, versions)

    def refill(self, select, options, value):
        """Replace a dropdown's options and selection, once the user isn't in the middle of choosing from it.

        Skips a refill that would change nothing, and waits while the list is open (swapping the options under the mouse
        closed it or moved the highlight). A Select also doesn't redraw when its value is unchanged, so it would keep
        showing the old label ("loading the list..."); pointing it at another option first forces the redraw."""
        self._want[select.id] = (options, value)
        self.apply_refill(select)

    def apply_refill(self, select):
        want = self._want.get(select.id)
        if want is None or want == self._shown.get(select.id):
            return
        if select.expanded:
            self.set_timer(0.25, lambda: self.apply_refill(select))
            return
        options, value = want
        with self.prevent(Select.Changed):
            select.set_options(options)
            other = next((v for _, v in options if v != value), None)
            if other is not None:
                select.value = other
            select.value = value
        self._shown[select.id] = want

    def fill_versions(self, versions):
        select = self.query_one("#version", Select)
        label = f"Latest release ({versions[0]})" if versions else "Latest release (couldn't load the list)"
        keep = select.value if select.value in versions else ""          # toggling snapshots keeps what you picked
        self.refill(select, [(label, ""), *[(v, v) for v in versions]], keep)
        self.refresh_loaders()

    @work(thread=True, exclusive=True, group="new-loaders")
    def refresh_loaders(self):
        from .. import loaders, provision
        version = self.query_one("#version", Select).value or None
        try:
            version = version or provision.latest_release()
            usable = provision.compatible_loaders(version)
        except Exception:
            return self.app.call_from_thread(self.fill_loaders, list(loaders.KEYS), {})
        self.app.call_from_thread(self.fill_loaders, usable, {})             # show the list at once...
        try:
            notes = {k: provision.loader_note(k, version) for k in usable if k in ("neoforge", "paper")}
        except Exception:
            return
        self.app.call_from_thread(self.fill_loaders, usable, notes)           # ...then add the "(beta)" cautions

    def fill_loaders(self, usable, notes):
        from .. import loaders
        select = self.query_one("#loader", Select)
        keep = select.value if select.value in usable else usable[0]
        self.notes = notes
        self.refill(select, [(loaders.get(k).label + (" (beta)" if notes.get(k) else ""), k) for k in usable], keep)
        self.show_hint()

    def show_hint(self):
        from .. import loaders
        key = self.query_one("#loader", Select).value
        if key is Select.NULL:
            return
        info = loaders.get(key)
        text = info.blurb
        if note := getattr(self, "notes", {}).get(key):
            text += f"  [!] {note[0].upper()}{note[1:]}."
        if self.query_one("#offline", Switch).value and not info.login_mods:
            text += f"  [!] {info.label} has no login mod here, so offline accounts are unprotected."
        self.query_one("#loader-hint", Static).update(text)

    @on(Select.Changed, "#version")
    def version_changed(self, event):
        self.refresh_loaders()

    @on(Select.Changed, "#loader")
    def loader_changed(self, event):
        self.show_hint()

    @on(Switch.Changed, "#offline")
    def offline_changed(self, event):
        self.show_hint()

    @on(Switch.Changed, "#snapshots")
    def snapshots_changed(self, event):
        self.load_versions(event.value)

    # ---------- submit ----------
    def fail(self, message):
        self.query_one("#form-error", Static).update(message)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            return self.dismiss(None)
        from ..core import NAME_RE
        from ..servers import WORLD_RE
        text = lambda wid: self.query_one(f"#{wid}", Input).value.strip()
        name = text("name")
        if not NAME_RE.match(name):
            return self.fail("Name: lowercase letters, numbers, - and _ (e.g. survival, creative-2).")
        world = text("world-name")
        if world and (not WORLD_RE.match(world) or world.endswith((".", " "))):
            return self.fail("World name: letters, numbers, spaces, - _ and . (up to 40 characters).")
        numbers = {}
        for wid, label, low, high in (("port", "Port", 1024, 65535), ("max-players", "Max players", 1, 1000),
                                      ("view-distance", "View distance", 2, 32)):
            raw = text(wid)
            if raw and (not raw.isdigit() or not low <= int(raw) <= high):
                return self.fail(f"{label}: a whole number from {low} to {high}.")
            numbers[wid] = int(raw) if raw else None
        value = lambda wid: self.query_one(f"#{wid}", Select).value
        self.dismiss({
            "name": name, "version": value("version") or None, "loader": value("loader"), "ram": value("ram"),
            "offline": self.query_one("#offline", Switch).value, "whitelist": self.query_one("#whitelist", Switch).value,
            "port": numbers["port"], "world_name": world or None, "gc": value("gc"), "runtime": value("runtime"),
            "props": {"motd": text("motd"), "level-seed": text("seed"), "difficulty": value("difficulty"),
                      "gamemode": value("gamemode"), "max-players": numbers["max-players"],
                      "view-distance": numbers["view-distance"],
                      "hardcore": "true" if self.query_one("#hardcore", Switch).value else ""},
        })

    def on_input_submitted(self, event: Input.Submitted):
        self.query_one("#create", Button).press()

    def action_cancel(self):
        self.dismiss(None)


class TextResult(ModalScreen[None]):
    """Scrollable read-only result (health check, crash details)."""

    BINDINGS = [("escape", "close", "Close"), ("enter", "close", "Close")]

    def __init__(self, title, body, markup=True):
        super().__init__()
        self.title_text, self.body, self.markup = title, body, markup

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide tall"):
            yield Label(self.title_text, classes="dialog-title")
            with VerticalScroll():
                yield Static(safe_content(self.body, self.markup))
            with Horizontal(classes="dialog-buttons"):
                yield Button("Close", variant="primary", id="close")

    def on_button_pressed(self, event):
        self.dismiss(None)

    def action_close(self):
        self.dismiss(None)


class PathPicker(ModalScreen[str | None]):
    """Pick a file or folder: click in the tree, or type/paste a path (~ works). Enter on a folder opens it.

    mode: "folder" (a directory), "file" (a file), "world" (a world folder or .zip).
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title, mode="folder", start=None, hint=""):
        super().__init__()
        self.title_text, self.mode, self.hint = title, mode, hint
        self.start = Path(start or Path.home()).expanduser()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide tall"):
            yield Label(self.title_text, classes="dialog-title")
            if self.hint:
                yield Static(self.hint, classes="hint")
            yield Input(value=str(self.start), placeholder="type or paste a path, then Enter", id="picker-path")
            yield PickerTree(self.start, self.mode, id="picker-tree")
            yield Static("", id="picker-error", classes="error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Choose", variant="primary", id="choose")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#picker-tree").focus()

    def _show(self, path):
        self.query_one("#picker-path", Input).value = str(path)
        self.query_one("#picker-error", Static).update("")

    def on_directory_tree_directory_selected(self, event):
        self._show(event.path)

    def on_directory_tree_file_selected(self, event):
        self._show(event.path)

    def on_input_submitted(self, event: Input.Submitted):
        path = Path(event.value).expanduser()
        if path.is_dir() and self.mode != "folder":     # Enter on a folder browses into it
            tree = self.query_one("#picker-tree", PickerTree)
            tree.path = path
            return
        self._choose()

    def _choose(self):
        raw = self.query_one("#picker-path", Input).value.strip()
        path = Path(raw).expanduser()
        error = None
        if not raw or not path.exists():
            error = "That path doesn't exist."
        elif self.mode == "folder" and not path.is_dir():
            error = "Pick a folder."
        elif self.mode == "file" and not path.is_file():
            error = "Pick a file."
        elif self.mode == "world" and not (path.is_dir() or path.suffix.lower() == ".zip"):
            error = "Pick a world folder or a .zip."
        elif self.mode == "datapack" and not (path.is_dir() or path.suffix.lower() == ".zip"):
            error = "Pick a datapack .zip or folder."
        elif self.mode == "jar" and path.suffix.lower() != ".jar":
            error = "Pick a mod .jar file."
        if error:
            return self.query_one("#picker-error", Static).update(error)
        self.dismiss(str(path.resolve()))

    def on_button_pressed(self, event: Button.Pressed):
        self._choose() if event.button.id == "choose" else self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)


class PickerTree(DirectoryTree):
    """Directory tree without dotfiles; folder mode lists folders only."""

    def __init__(self, path, mode, **kw):
        super().__init__(path, **kw)
        self.mode = mode

    def filter_paths(self, paths):
        paths = [p for p in paths if not p.name.startswith(".")]
        return [p for p in paths if p.is_dir()] if self.mode == "folder" else paths


class AskText(ModalScreen[str | None]):
    """One line of text (a key, a name). `secret` hides what's typed."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title, label="", secret=False, value="", hint=""):
        super().__init__()
        self.title_text, self.label, self.secret, self.value, self.hint = title, label, secret, value, hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self.title_text, classes="dialog-title")
            if self.hint:
                yield Static(self.hint, classes="hint")
            yield Input(value=self.value, placeholder=self.label, password=self.secret, id="ask-input")
            with Horizontal(classes="dialog-buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#ask-input", Input).focus()

    def on_input_submitted(self, event):
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(self.query_one("#ask-input", Input).value.strip() or None if event.button.id == "ok" else None)

    def action_cancel(self):
        self.dismiss(None)
