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


def relaunch_as_admin(args: Sequence[str]) -> bool:
    """Jalankan ulang `python -m distrohop <args>` lewat UAC. True kalau jendela baru berhasil dibuka."""
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
    params = subprocess.list2cmdline(["-m", "distrohop", *args])
    result = shell_execute(None, "runas", sys.executable, params, os.getcwd(), 1)
    # Nilai > 32 berarti sukses; 5 berarti user menolak UAC.
    return (result or 0) > 32
