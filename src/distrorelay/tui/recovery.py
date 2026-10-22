"""Menu Pulihkan (M2c fase B): kembalikan folder ESP dan entri boot dari backup, lewat `RestoreFlow`."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, OptionList, SelectionList, Static
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from .. import render
from ..windows.workflow import RESTORE_WORD, RestoreFlow
from .common import StepScreen


class RecoveryScreen(StepScreen):
    STEP = "Pulihkan dari backup"
    HELP = (
        "Pilih backup (diverifikasi ulang), lalu centang folder ESP dan/atau entri boot yang mau dikembalikan. "
        "File yang sudah ada dengan isi berbeda tidak ditimpa, dan milik Windows tidak disentuh "
        "(untuk Windows pakai docs/RECOVERY.md)."
    )
    SUB_TITLE = "Pulihkan: sistem baru diubah setelah PULIHKAN diketik"
    NEXT_LABEL = "Pulihkan"

    def __init__(self, flow: RestoreFlow) -> None:
        super().__init__(flow)
        self.changed = False

    def compose_body(self) -> ComposeResult:
        with Horizontal(classes="body"):
            with Vertical(id="backup-pane"):
                yield Static(Text("1. Backup", style="bold"))
                yield OptionList(id="backups")
            with VerticalScroll(id="items-pane"):
                yield Static(Text("2. Yang dipulihkan", style="bold"))
                yield Static(id="verify")
                yield Static(Text("Folder/file ESP", style="bold"))
                yield SelectionList[str](id="esp-items")
                yield Static(Text("Entri boot firmware (dibuat ulang di urutan terakhir)", style="bold"))
                yield SelectionList[str](id="boot-entries")
                yield Static(id="result")
        yield Input(placeholder=f"Ketik {RESTORE_WORD} untuk menjalankan", id="word", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).display = False
        self.query_one("#next", Button).variant = "error"
        backups = self.query_one("#backups", OptionList)
        found = self.flow.backups()
        for backup in found:
            backups.add_option(Option(backup.describe(), id=backup.backup_id))
        if not found:
            backups.add_option(Option("Belum ada backup", disabled=True))
        backups.focus()

    @on(OptionList.OptionSelected, "#backups")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        if self.busy or event.option.id is None:
            return
        backup_id = event.option.id
        self.background(lambda: self.flow.load(backup_id), self._loaded, f"Memverifikasi backup {backup_id}…")

    def _loaded(self, checks) -> None:
        esp_items = self.query_one("#esp-items", SelectionList)
        entries = self.query_one("#boot-entries", SelectionList)
        esp_items.clear_options()
        entries.clear_options()
        word = self.query_one("#word", Input)
        failed = [c for c in checks if not c.ok]
        if failed:
            self.query_one("#verify", Static).update(render.verifications_text("Backup tidak valid", failed))
            word.disabled = True
            self.set_status("Backup ini tidak bisa dipakai.", "bold red")
            self.refresh_next()
            return
        self.query_one("#verify", Static).update(Text(f"Backup {self.flow.backup_id} lolos verifikasi.", style="green"))
        for choice in self.flow.esp_choices():
            esp_items.add_option(_selection(choice))
        for choice in self.flow.entry_choices():
            entries.add_option(_selection(choice))
        word.disabled = False
        self.set_status("Centang yang mau dipulihkan, lalu ketik PULIHKAN.")
        self.refresh_next()

    @on(SelectionList.SelectedChanged)
    @on(Input.Changed, "#word")
    def _changed(self) -> None:
        self.refresh_next()

    @on(Input.Submitted, "#word")
    def _submitted(self) -> None:
        if not self.busy and not self.query_one("#next", Button).disabled:
            self.next()

    def _selected(self) -> tuple[list[str], list[str]]:
        return (
            list(self.query_one("#esp-items", SelectionList).selected),
            list(self.query_one("#boot-entries", SelectionList).selected),
        )

    def refresh_next(self) -> None:
        esp_items, entries = self._selected()
        self.set_next(
            self.flow.backup_id is not None and bool(esp_items or entries) and bool(self.query_one("#word", Input).value)
        )

    def next(self) -> None:
        esp_items, entries = self._selected()
        problem = self.flow.select(esp_items, entries)
        if problem:
            self.set_status(problem, "bold red")
            return
        ticket = self.flow.authorize(self.query_one("#word", Input).value)
        if ticket is None:
            self.set_status(f"Ketik {RESTORE_WORD} persis (huruf besar). Tidak ada yang diubah.", "bold red")
            return
        self.app.quit_blocker = "Pemulihan sedang berjalan; tunggu sampai selesai."
        self.changed = True
        self.background(lambda: self.flow.run(ticket, progress=self.progress), self._done, "Memulihkan…")

    def _done(self, results) -> None:
        self.app.quit_blocker = None
        self.query_one("#result", Static).update(render.verifications_text("Pemulihan", results))
        self.query_one("#word", Input).value = ""
        if all(r.ok for r in results):
            self.set_status("Pemulihan selesai.", "bold green")
        else:
            self.set_status("Ada yang gagal dipulihkan: lihat hasil di bawah.", "bold red")

    def _job_failed(self, exc: Exception) -> None:
        self.app.quit_blocker = None
        super()._job_failed(exc)

    def action_back(self) -> None:
        if not self.busy:
            self.app.back_to_dashboard(rescan=self.changed)


def _selection(choice) -> Selection:
    label = choice.label if not choice.blocked else f"{choice.label}  — {choice.blocked}"
    return Selection(label, choice.key, False, disabled=bool(choice.blocked))
