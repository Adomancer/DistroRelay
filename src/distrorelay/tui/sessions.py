"""Journal eksekusi: sesi yang selesai, dihentikan, gagal, atau terputus. Sesi yang butuh perhatian bisa ditandai
tuntas di sini (ketik TUNTAS) kalau kondisinya sudah dicek sendiri."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import render
from ..windows.workflow import RESOLVE_WORD, mark_session_resolved


class SessionsScreen(Screen):
    SUB_TITLE = "Journal eksekusi"
    # Tanpa "q": huruf itu bisa sedang diketik di isian TUNTAS.
    BINDINGS = [Binding("escape", "app.pop_screen", "Kembali")]

    def __init__(self) -> None:
        super().__init__()
        self.selected: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(classes="body"):
            yield Static(id="journal")
        yield Static(
            Text(
                "Sesi yang butuh perhatian. Pilih satu (Enter), lalu ketik TUNTAS kalau kondisinya sudah kamu cek "
                "(scan ulang, Windows boot normal). Sesi itu lalu tidak lagi meminta LANJUT."
            ),
            id="resolve-help",
            classes="help",
        )
        yield OptionList(id="attention")
        yield Input(placeholder=f"Ketik {RESOLVE_WORD}", id="resolve-word", disabled=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self.query_one("#journal", Static).update(render.journal_text(self.app.sessions()))
        attention = self.app.unfinished_sessions()
        options = self.query_one("#attention", OptionList)
        options.clear_options()
        for session in attention:
            options.add_option(Option(f"{session.path.stem}  [{session.kind}]", id=session.path.stem))
        for widget_id in ("#resolve-help", "#attention", "#resolve-word"):
            self.query_one(widget_id).display = bool(attention)
        word = self.query_one("#resolve-word", Input)
        word.value, word.disabled, self.selected = "", True, None
        if attention:
            options.focus()

    def _status(self, message: str, style: str = "") -> None:
        self.query_one("#status", Static).update(Text(message, style=style))

    @on(OptionList.OptionSelected, "#attention")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.selected = event.option.id
        word = self.query_one("#resolve-word", Input)
        word.disabled = False
        word.focus()
        self._status(f"Sesi {self.selected} dipilih. Ketik {RESOLVE_WORD} lalu Enter.")

    @on(Input.Submitted, "#resolve-word")
    def _submitted(self, event: Input.Submitted) -> None:
        if self.selected is None:
            return
        problem = mark_session_resolved(self.app.deps().journal_dir, self.selected, event.value, "ditandai lewat TUI")
        if problem:
            self._status(problem, "bold red")
            return
        done = self.selected
        self._refresh()
        self._status(f"Sesi {done} ditandai tuntas.", "bold green")
