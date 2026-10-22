"""Bagian bersama layar wizard (M2c fase B): judul langkah, status, tombol navigasi, kerja di thread.

Layar hanya bertanya dan menampilkan. Semua yang mengubah keadaan lewat `UninstallFlow` / `RestoreFlow`;
urutan dan syaratnya dipaksa di sana, bukan di sini.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

log = logging.getLogger("distrorelay")


def safe_call_from_thread(app, callback: Callable, *args: Any) -> None:
    """Kirim ke thread UI; diam saja kalau aplikasi sudah ditutup."""
    try:
        app.call_from_thread(callback, *args)
    except RuntimeError:
        pass


class StepScreen(Screen):
    """Satu langkah wizard. Esc / Kembali = langkah sebelumnya, Batal = kembali ke dashboard.

    Selama `busy` (ada kerja di thread), navigasi dikunci supaya layar tidak ditinggal di tengah langkah.
    """

    STEP = ""
    HELP = ""
    NEXT_LABEL = "Lanjut →"
    SUB_TITLE = "Uninstall: sistem baru diubah setelah HAPUS diketik"
    BINDINGS = [Binding("escape", "back", "Kembali"), Binding("ctrl+n", "next_step", "Lanjut")]

    def __init__(self, flow) -> None:
        super().__init__()
        self.flow = flow
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(Text(self.STEP, style="bold"), classes="step-title")
        if self.HELP:
            yield Static(Text(self.HELP), classes="help")
        yield from self.compose_body()
        yield Static("", id="status")
        with Horizontal(id="nav"):
            yield Button("← Kembali", id="back")
            yield Button("Batal", id="cancel")
            yield Button(self.NEXT_LABEL, id="next", variant="primary", disabled=True)
        yield Footer()

    def compose_body(self) -> ComposeResult:
        yield from ()

    # --- status & navigasi --------------------------------------------------
    def set_status(self, message: str | Text, style: str = "") -> None:
        self.query_one("#status", Static).update(message if isinstance(message, Text) else Text(message, style=style))

    def set_next(self, enabled: bool) -> None:
        self.query_one("#next", Button).disabled = not enabled or self.busy

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for button in self.query("#nav Button").results(Button):
            if busy:
                button.disabled = True
            elif button.id != "next":
                button.disabled = False
        if not busy:
            self.refresh_next()

    def refresh_next(self) -> None:
        """Hitung ulang apakah tombol Lanjut boleh ditekan. Diganti per layar."""

    @on(Button.Pressed, "#back")
    def _back_pressed(self) -> None:
        self.action_back()

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        if not self.busy:
            self.app.back_to_dashboard()

    @on(Button.Pressed, "#next")
    def _next_pressed(self) -> None:
        if not self.busy:
            self.next()

    def action_next_step(self) -> None:
        """Ctrl+N: sama dengan menekan tombol Lanjut, hanya kalau tombolnya aktif."""
        if not self.busy and not self.query_one("#next", Button).disabled:
            self.next()

    def action_back(self) -> None:
        if not self.busy:
            self.app.pop_screen()

    def next(self) -> None:
        """Lanjut ke langkah berikutnya. Diganti per layar."""

    # --- kerja di thread ------------------------------------------------------
    def progress(self, message: str) -> None:
        """Callback progress yang aman dipanggil dari thread kerja."""
        safe_call_from_thread(self.app, self.set_status, message)

    def background(self, job: Callable[[], Any], done: Callable[[Any], None], status: str) -> None:
        """Jalankan `job` di thread (scan, preflight, backup, gerbang), lalu `done(hasil)` di thread UI."""
        self.set_busy(True)
        self.set_status(status)

        def run() -> None:
            try:
                value = job()
            except Exception as exc:  # error apa pun ditampilkan, aplikasi tetap hidup
                log.exception("Langkah wizard gagal: %s", self.STEP)
                safe_call_from_thread(self.app, self._job_failed, exc)
                return
            safe_call_from_thread(self.app, self._job_done, done, value)

        self.run_worker(run, thread=True, exclusive=True, group="step", exit_on_error=False)

    async def _job_done(self, done: Callable[[Any], Any], value: Any) -> None:
        self.set_busy(False)
        result = done(value)
        if inspect.isawaitable(result):  # layar yang memasang ulang widget (await remove/mount)
            await result

    def _job_failed(self, exc: Exception) -> None:
        self.set_busy(False)
        self.set_status(f"Gagal: {type(exc).__name__}: {exc}", "bold red")
