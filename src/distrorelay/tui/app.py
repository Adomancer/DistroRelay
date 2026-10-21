"""Dashboard scan read-only (M1)."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane, Tree

from .. import render
from ..paths import reports_dir
from ..windows.scanner import ScanReport, report_to_json, run_scan


class DistroRelayApp(App):
    TITLE = "DistroRelay"
    SUB_TITLE = "Scan read-only: tidak ada yang diubah"
    CSS = """
    #status { height: auto; padding: 0 1; background: $boost; }
    Tree, DataTable { height: 1fr; }
    #info { padding: 1 2; }
    """
    BINDINGS = [
        Binding("r", "rescan", "Scan ulang"),
        Binding("s", "save", "Simpan laporan JSON"),
        Binding("q", "quit", "Keluar"),
    ]

    def __init__(self, log_path: str | None = None, scan: Callable[..., ScanReport] = run_scan) -> None:
        super().__init__()
        self.report: ScanReport | None = None
        self.log_path = log_path
        self._run_scan = scan

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Menyiapkan scan…", id="status")
        with TabbedContent(initial="tab-summary"):
            with TabPane("Ringkasan", id="tab-summary"):
                yield Tree(Text("Hasil scan"), id="summary")
            with TabPane("Partisi", id="tab-partitions"):
                yield DataTable(id="partitions", zebra_stripes=True, cursor_type="row")
            with TabPane("Entri boot", id="tab-entries"):
                yield DataTable(id="entries", zebra_stripes=True, cursor_type="row")
            with TabPane("Isi ESP", id="tab-esp"):
                yield Tree(Text("ESP"), id="esp")
            with TabPane("Info & peringatan", id="tab-info"):
                with VerticalScroll():
                    yield Static(id="info")
        yield Footer()

    def on_mount(self) -> None:
        self.action_rescan()

    def action_rescan(self) -> None:
        self._set_status("Memulai scan…")
        self._scan()

    @work(thread=True, exclusive=True, group="scan")
    def _scan(self) -> None:
        try:
            report = self._run_scan(progress=lambda message: self.call_from_thread(self._set_status, message))
        except Exception as exc:  # jaga-jaga: run_scan sendiri sudah menangkap error per langkah
            self.call_from_thread(self._set_status, f"Scan gagal: {type(exc).__name__}: {exc}")
            return
        self.call_from_thread(self._show, report)

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(Text(message))

    def _show(self, report: ScanReport) -> None:
        self.report = report
        problems = [i for i in report.issues if i.severity != "info"]
        status = Text(f"Scan selesai {report.created_at}. ")
        if not report.is_admin:
            status.append("Bukan Administrator: sebagian data tidak terbaca. ", style="bold yellow")
        if problems:
            status.append(f"{len(problems)} langkah bermasalah (lihat tab Info). ", style="yellow")
        status.append("Tekan s untuk menyimpan laporan.", style="dim")
        self.query_one("#status", Static).update(status)

        _fill_tree(self.query_one("#summary", Tree), render.summary_nodes(report))
        _fill_table(self.query_one("#partitions", DataTable), render.PARTITION_COLUMNS, render.partition_rows(report))
        _fill_table(self.query_one("#entries", DataTable), render.ENTRY_COLUMNS, render.entry_rows(report))
        _fill_tree(self.query_one("#esp", Tree), render.esp_nodes(report))
        self.query_one("#info", Static).update(render.info_text(report, self.log_path))

    def action_save(self) -> None:
        if self.report is None:
            self.notify("Belum ada hasil scan", severity="warning")
            return
        directory = reports_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"scan-{datetime.now():%Y%m%d-%H%M%S}.json"
        path.write_text(report_to_json(self.report), encoding="utf-8")
        self.notify(str(path), title="Laporan disimpan", markup=False)


def _fill_tree(tree: Tree, nodes: list[render.Node]) -> None:
    tree.clear()
    tree.root.expand()

    def add(parent, node: render.Node) -> None:
        if node.children:
            branch = parent.add(node.label, expand=node.expanded)
            for child in node.children:
                add(branch, child)
        else:
            parent.add_leaf(node.label)

    for node in nodes:
        add(tree.root, node)


def _fill_table(table: DataTable, columns: tuple[str, ...], rows: list[tuple[Text, ...]]) -> None:
    table.clear(columns=True)
    table.add_columns(*columns)
    for row in rows:
        table.add_row(*row)


def run_tui(log_path: str | None = None) -> int:
    DistroRelayApp(log_path).run()
    return 0
