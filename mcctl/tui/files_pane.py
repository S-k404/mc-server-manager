"""Files tab: browse the server folder, edit text files in place, and import/export worlds with a path chooser."""
import subprocess

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.widgets import Button, DirectoryTree, Label, Static, TextArea

from .. import backups, datapacks, files, platform, upgrade
from ..core import Fail
from .modals import Confirm, PathPicker, TextResult
from .panes import Pane


class ServerTree(DirectoryTree):
    """The server folder without mc's own state."""

    def filter_paths(self, paths):
        return [p for p in paths if p.name not in files.HIDDEN_NAMES and not p.name.endswith(".mc-tmp")]


class FilesPane(Pane):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.current = None       # Path of the file open in the editor
        self.original = ""        # its text as last loaded/saved, so "modified" means "differs from this"
        self.dirty = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="files-body"):
            with Vertical(id="files-left"):
                yield Label("Server folder", classes="section-title")
                yield ServerTree(".", id="files-tree")
                with Vertical(id="files-actions"):
                    yield Button("Import world...", id="fl-import-world")
                    yield Button("Export world...", id="fl-export-world")
                    yield Button("Export client pack...", id="fl-export-pack")
                    yield Button("Add a file here...", id="fl-add-file")
                    yield Button("Add datapack...", id="fl-add-datapack")
                    yield Button("Reveal in Finder", id="fl-reveal")
            with Vertical(id="files-right"):
                yield Label("Select a file on the left to edit it", id="files-title", classes="section-title")
                yield TextArea("", id="files-editor", read_only=True, show_line_numbers=True, soft_wrap=False)
                with Horizontal(classes="buttons"):
                    yield Button("Save  (Ctrl+S)", id="fl-save", variant="primary", disabled=True)
                    yield Button("Revert", id="fl-revert", disabled=True)
                    yield Button("Earlier versions...", id="fl-versions", disabled=True)
                yield Static("", id="files-status", classes="hint")

    # ---------- loading ----------
    def load(self, server):
        super().load(server)
        if server is None:
            return
        self.query_one("#files-tree", ServerTree).path = server.dir
        self._close_file("Select a file on the left to edit it")

    def _close_file(self, title):
        self.current, self.dirty, self.original = None, False, ""
        editor = self.query_one("#files-editor", TextArea)
        editor.load_text("")
        editor.read_only = True
        self.query_one("#files-title", Label).update(title)
        self._buttons(False)

    def _buttons(self, editable):
        self.query_one("#fl-save", Button).disabled = not (editable and self.dirty)
        self.query_one("#fl-revert", Button).disabled = not (editable and self.dirty)
        self.query_one("#fl-versions", Button).disabled = not editable

    def status(self, text):
        self.query_one("#files-status", Static).update(text)

    # ---------- opening a file ----------
    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected):
        event.stop()
        path = event.path
        if self.dirty and path != self.current:
            self.app.push_screen(Confirm(f"Discard unsaved changes to {self.current.name}?", yes="Discard", danger=True),
                                 lambda yes: yes and self.open_file(path))
        else:
            self.open_file(path)

    def open_file(self, path):
        s = self.server
        if s is None:
            return
        try:
            text = files.read_text(s, path)
        except Fail as e:
            self._close_file(f"{path.name}")
            self.status(f"[yellow]{escape(str(e))}[/]")
            return
        editor = self.query_one("#files-editor", TextArea)
        editor.load_text(text)
        lang = files.language(path)
        editor.language = lang if lang in editor.available_languages else None
        editor.read_only = False
        self.current, self.dirty, self.original = path, False, editor.text   # editor.text normalises line endings
        self.query_one("#files-title", Label).update(files.relative(s, path))
        note = ""
        if path.name in files.SERVER_OWNED and s.up():
            note = f"'{s.name}' is running: stop it to save this file (the server rewrites it itself)."
        self.status(note or "Edit and press Ctrl+S to save. The previous version is always kept.")
        self._buttons(True)

    def on_text_area_changed(self, event: TextArea.Changed):
        if self.current is None:
            return
        dirty = event.text_area.text != self.original
        if dirty != self.dirty:
            self.dirty = dirty
            name = files.relative(self.server, self.current)
            self.query_one("#files-title", Label).update(name + ("  *" if dirty else ""))
            self._buttons(True)

    # ---------- saving ----------
    def save(self):
        s, path = self.server, self.current
        if s is None or path is None:
            return self.app.notify("Open a file first.", severity="warning")
        text = self.query_one("#files-editor", TextArea).text
        try:
            result = files.save_text(s, path, text)
        except Fail as e:
            self.status(f"[red]{escape(str(e))}[/]")
            return self.app.notify(str(e), title="Not saved", severity="error", timeout=8)
        self.dirty, self.original = False, text
        self.query_one("#files-title", Label).update(files.relative(s, path))
        self._buttons(True)
        self.status("Saved." + (" Restart the server for it to take effect." if result == "restart" else ""))
        self.app.notify(f"Saved {path.name}" + (" (restart to apply)" if result == "restart" else ""))

    def revert(self):
        if self.current is not None:
            self.open_file(self.current)

    # ---------- buttons ----------
    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id
        if not bid or not bid.startswith("fl-"):
            return
        event.stop()
        s = self.server
        if s is None:
            return
        app = self.app
        if bid == "fl-save":
            self.save()
        elif bid == "fl-revert":
            self.revert()
        elif bid == "fl-versions":
            self.show_versions()
        elif bid == "fl-reveal":
            self.reveal()
        elif bid == "fl-import-world":
            def chosen(path):
                if not path:
                    return
                app.push_screen(
                    Confirm(f"Replace the world of '{s.name}' with {path}?\nThe current world is kept in backups/.",
                            yes="Import", danger=True),
                    lambda yes: yes and app.run_task("Import world", backups.world_import, s, path,
                                                     after=lambda _: self.refresh_tree()))
            app.push_screen(PathPicker("Import a world", "world", hint="Pick a world folder or a .zip. "
                                       "The server must be stopped."), chosen)
        elif bid == "fl-export-world":
            app.push_screen(PathPicker("Export the world to...", "folder", start="~/Downloads",
                                       hint="A zip of the world is written into the folder you pick."),
                            lambda folder: folder and app.run_task("Export world", backups.world_export, s, folder))
        elif bid == "fl-export-pack":
            app.push_screen(PathPicker("Export the client pack to...", "folder", start="~/Downloads",
                                       hint="A .mrpack of the mods players need (import it in Modrinth App / Prism)."),
                            lambda folder: folder and app.run_task("Export client pack", upgrade.export_client_pack,
                                                                   s, folder))
        elif bid == "fl-add-datapack":
            self.add_datapack()
        elif bid == "fl-add-file":
            into = self.selected_folder()
            app.push_screen(PathPicker("Copy which file into the server?", "file",
                                       hint=f"It is copied into {files.relative(s, into) or 'the server folder'}. "
                                            "Existing files are never overwritten."),
                            lambda src: src and app.run_task("Add file", files.import_file, s, src, into,
                                                             after=lambda _: self.refresh_tree()))

    def add_datapack(self):
        s = self.server
        if s is None:
            return
        self.app.push_screen(
            PathPicker("Add a datapack", "datapack",
                       hint="Pick a datapack .zip or a datapack folder (it has pack.mcmeta at its top). "
                            "A running server runs /reload."),
            lambda src: src and self.app.run_task("Add datapack", datapacks.add, s, src,
                                                  after=lambda _: (self.refresh_tree(),
                                                                   self.app.pane("mods").fill_datapacks())))

    def selected_folder(self):
        tree = self.query_one("#files-tree", ServerTree)
        node = tree.cursor_node
        if node is not None and node.data is not None:
            p = node.data.path
            return p if p.is_dir() else p.parent
        return self.server.dir

    def refresh_tree(self):
        self.query_one("#files-tree", ServerTree).reload()

    def show_versions(self):
        if self.current is None:
            return
        versions = files.list_versions(self.server, self.current)
        body = "\n".join(f"  {v.name}" for v in versions) or "No earlier versions yet: one is kept every time you save."
        self.app.push_screen(TextResult(f"Earlier versions of {self.current.name}",
                                        body + f"\n\nThey live in backups/{self.server.name}/file-edits/ "
                                               "(copy one back over the file to undo).", markup=False))

    def reveal(self):
        target = self.current or self.selected_folder()
        cmd = (["open", "-R", str(target)] if platform.IS_MAC
               else ["xdg-open", str(target if target.is_dir() else target.parent)])
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.app.notify("Couldn't open a file manager here.", severity="warning")
