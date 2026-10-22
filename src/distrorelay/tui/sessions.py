"""Journal eksekusi (read-only): sesi yang selesai, dihentikan, gagal, atau terputus."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from .. import render


class SessionsScreen(Screen):
    SUB_TITLE = "Journal eksekusi (read-only)"
    BINDINGS = [Binding("escape", "app.pop_screen", "Kembali"), Binding("q", "app.pop_screen", "Kembali")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(render.journal_text(self.app.sessions()), id="journal")
        yield Footer()
