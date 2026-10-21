"""Berhenti dengan rapi di tengah eksekusi: selesaikan langkah yang sedang jalan, lalu berhenti.

Ctrl+C / Ctrl+Break tidak memotong langkah di tengah (misalnya di antara `bcdedit /delete` dan flush BCD).
Permintaan berhenti dicatat, langkah yang sedang jalan diselesaikan, lalu eksekusi berhenti sebelum langkah
berikutnya dan journal mencatat "dihentikan pengguna".

Menutup jendela konsol, logoff, atau shutdown: Windows hanya memberi waktu ±5 detik sebelum proses dipaksa
mati. Penangan di sini menunggu langkah yang sedang jalan selesai selama waktu itu (biasanya cukup, satu
langkah 1–2 detik). Kalau tidak sempat, journal menunjukkan sesi itu TERPUTUS dan menu Pulihkan/sesi
terputus yang mengambil alih.
"""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from typing import Iterator

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

# Waktu tunggu maksimum saat jendela ditutup; Windows memaksa mati setelah ±5 detik.
CLOSE_GRACE_SECONDS = 4.5


class StopSignal:
    def __init__(self) -> None:
        self._requested = threading.Event()
        self._idle = threading.Event()  # tidak ada langkah yang sedang jalan
        self._idle.set()
        self.reason: str | None = None

    def request(self, reason: str = "Dihentikan pengguna") -> None:
        if self.reason is None:
            self.reason = reason
        self._requested.set()

    def requested(self) -> bool:
        return self._requested.is_set()

    def step_started(self) -> None:
        self._idle.clear()

    def step_finished(self) -> None:
        self._idle.set()

    def wait_idle(self, timeout: float) -> bool:
        return self._idle.wait(timeout)


def handle_console_event(stop: StopSignal, event: int, notify=None, grace: float = CLOSE_GRACE_SECONDS) -> bool:
    """True = sinyal ditangani di sini (proses tidak dipotong Ctrl+C / KeyboardInterrupt)."""
    if event in (CTRL_C_EVENT, CTRL_BREAK_EVENT):
        stop.request("Dihentikan pengguna (Ctrl+C)")
        if notify:
            notify("Berhenti setelah langkah ini selesai…")
        return True
    if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
        stop.request("Jendela ditutup / Windows logoff atau shutdown")
        stop.wait_idle(grace)  # Windows memaksa mati begitu penangan ini kembali
        return True
    return False


@contextmanager
def console_stop_guard(stop: StopSignal, *, notify=None) -> Iterator[StopSignal]:
    """Selama blok ini, sinyal konsol meminta berhenti alih-alih memotong proses."""
    if sys.platform != "win32":
        yield stop
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    handler = handler_type(lambda event: handle_console_event(stop, event, notify))  # simpan referensinya
    kernel32.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
    installed = bool(kernel32.SetConsoleCtrlHandler(handler, True))
    try:
        yield stop
    finally:
        if installed:
            kernel32.SetConsoleCtrlHandler(handler, False)
