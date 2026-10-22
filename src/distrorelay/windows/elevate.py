"""Cek dan minta hak Administrator (UAC)."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Sequence


def is_admin() -> bool:
    if sys.platform != "win32":
        return hasattr(os, "geteuid") and os.geteuid() == 0
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_command(args: Sequence[str]) -> tuple[str, str]:
    """(program, parameter) untuk menjalankan ulang DistroRelay: `distrorelay.exe <args>` kalau dari .exe
    (PyInstaller), selain itu `python -m distrorelay <args>`."""
    if getattr(sys, "frozen", False):
        return sys.executable, subprocess.list2cmdline(list(args))
    return sys.executable, subprocess.list2cmdline(["-m", "distrorelay", *args])


def relaunch_as_admin(args: Sequence[str]) -> bool:
    """Jalankan ulang DistroRelay dengan `args` lewat UAC. True kalau jendela baru berhasil dibuka."""
    import ctypes
    from ctypes import wintypes

    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    shell_execute.restype = ctypes.c_void_p
    program, params = relaunch_command(args)
    result = shell_execute(None, "runas", program, params, os.getcwd(), 1)
    # Nilai > 32 berarti sukses; 5 berarti user menolak UAC.
    return (result or 0) > 32
