"""Aplikasi TUI: dashboard scan read-only (M1) + wizard uninstall, menu Pulihkan, dan journal (M2c fase B).

Semua yang mengubah sistem lewat `windows.workflow` (UninstallFlow / RestoreFlow). TUI tidak pernah memanggil
executor, Runner, flush, atau modul restore secara langsung (dijaga test arsitektur).
"""

from __future__ import annotations

from typing import Callable

from textual.app import App
from textual.screen import Screen

from ..windows.journal import SessionSummary, incomplete_sessions, sessions
from ..windows.scanner import ScanReport
from ..windows.stopping import StopSignal
from ..windows.workflow import FlowDeps, RestoreFlow, UninstallFlow, default_deps
from .dashboard import DashboardScreen
from .recovery import RecoveryScreen
from .sessions import SessionsScreen
from .uninstall import first_screen


class DistroRelayApp(App):
    TITLE = "DistroRelay"
    CSS = """
    #status { height: auto; padding: 0 1; background: $boost; }
    /* Bawaan Textual: TabbedContent/ContentSwitcher/TabPane setinggi isinya (auto), jadi dashboard 1 baris lebih
       tinggi dari layar dan tergulung; baris status di atas ikut hilang di terminal sungguhan (uji VM 2026-10-22). */
    DashboardScreen TabbedContent, DashboardScreen ContentSwitcher, DashboardScreen TabPane { height: 1fr; }
    Tree, DataTable { height: 1fr; }
    #info { padding: 1 2; }
    .step-title { padding: 0 1; background: $primary-background; }
    .help { padding: 0 1 1 1; color: $text-muted; }
    .body { height: 1fr; }
    #plan-pane, #items-pane { width: 1fr; padding: 0 1; }
    #candidates { width: 1fr; }
    #backup-pane { width: 42; padding: 0 1; }
    #backups { height: 1fr; }
    #esp-items, #boot-entries { height: auto; max-height: 12; }
    #existing { height: auto; max-height: 10; }
    #statements, #confirmations { height: auto; }
    #nav { height: auto; padding: 0 1; }
    #nav Button { margin-right: 2; }
    #log { height: auto; max-height: 16; }
    """

    def __init__(
        self,
        log_path: str | None = None,
        scan: Callable[..., ScanReport] | None = None,
        deps: FlowDeps | None = None,
    ) -> None:
        super().__init__()
        self.report: ScanReport | None = None
        self.log_path = log_path
        self._scan = scan
        self._deps = deps
        self.stop_signal: StopSignal | None = None  # diisi layar eksekusi selama uninstall berjalan
        self.quit_blocker: str | None = None  # alasan aplikasi tidak boleh ditutup sekarang

    def get_default_screen(self) -> Screen:
        return DashboardScreen()

    # --- ketergantungan --------------------------------------------------------
    def deps(self) -> FlowDeps:
        if self._deps is None:
            self._deps = default_deps()
        return self._deps

    def run_scan(self, progress=None) -> ScanReport:
        return (self._scan or self.deps().scan)(progress=progress)

    def unfinished_sessions(self) -> list[SessionSummary]:
        return incomplete_sessions(self.deps().journal_dir)

    def sessions(self) -> list[SessionSummary]:
        return sessions(self.deps().journal_dir)

    # --- navigasi -------------------------------------------------------------
    def _dashboard_busy(self) -> bool:
        if self.screen_stack[0].scanning:
            self.notify("Tunggu scan dashboard selesai dulu.", severity="warning")
            return True
        return False

    def action_uninstall(self) -> None:
        if not self._dashboard_busy():
            self.push_screen(first_screen(UninstallFlow(self.deps())))

    def action_recovery(self) -> None:
        if not self._dashboard_busy():
            self.push_screen(RecoveryScreen(RestoreFlow(self.deps())))

    def action_sessions(self) -> None:
        self.push_screen(SessionsScreen())

    def back_to_dashboard(self, rescan: bool = False) -> None:
        while len(self.screen_stack) > 1:
            self.pop_screen()
        if rescan:
            self.screen_stack[0].action_rescan()

    async def action_quit(self) -> None:
        if self.stop_signal is not None:
            # Uninstall sedang berjalan: keluar = berhenti dengan rapi dulu, bukan memotong langkah.
            stopper = getattr(self.screen, "action_request_stop", None)
            if stopper is not None:
                stopper("Ctrl+Q")
            else:
                self.stop_signal.request("Dihentikan pengguna (Ctrl+Q)")
            self.notify("Uninstall berhenti setelah langkah ini selesai. Tekan Ctrl+Q lagi setelah itu untuk keluar.")
            return
        if self.quit_blocker:
            self.notify(self.quit_blocker, severity="warning")
            return
        self.exit()

    def action_help_quit(self) -> None:
        """Ctrl+C di luar eksekusi: Textual tidak keluar dengan Ctrl+C, jadi beri tahu caranya."""
        self.notify("Tekan Ctrl+Q (atau q di dashboard) untuk keluar.")


def run_tui(log_path: str | None = None) -> int:
    DistroRelayApp(log_path).run()
    return 0
