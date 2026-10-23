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
    (PyInstaller), selain itu `python -P -m distrorelay <args>`.

    `-P`: tanpa ini, `-m` menaruh folder kerja di depan sys.path, jadi `json.py` atau `rich.py` yang kebetulan ada
    di folder tempat perintah dijalankan (mis. Downloads) ikut di-import oleh proses Administrator.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, subprocess.list2cmdline(list(args))
    return sys.executable, subprocess.list2cmdline(["-P", "-m", "distrorelay", *args])


_SEE_MASK_NOCLOSEPROCESS = 0x00000040  # isi hProcess, supaya prosesnya bisa ditunggu
_SEE_MASK_NOASYNC = 0x00000100
_WAIT_TIMEOUT = 0x00000102


def _shell_execute_info():
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    return SHELLEXECUTEINFOW


def relaunch_as_admin(args: Sequence[str]) -> int | None:
    """Jalankan ulang DistroRelay dengan `args` lewat UAC, lalu tunggu jendela Administrator itu selesai.

    Hasilnya kode keluar proses Administrator (supaya skrip yang memanggil `distrorelay` tahu hasilnya), atau None
    kalau UAC ditolak / jendela tidak bisa dibuka. Ctrl+C di jendela ini hanya berhenti menunggu.
    """
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    info_type = _shell_execute_info()
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(info_type)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    program, params = relaunch_command(args)
    info = info_type()
    info.cbSize = ctypes.sizeof(info_type)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC
    info.lpVerb, info.lpFile, info.lpParameters, info.lpDirectory = "runas", program, params, os.getcwd()
    info.nShow = 1
    if not shell32.ShellExecuteExW(ctypes.byref(info)):  # 1223 = UAC ditolak
        return None
    if not info.hProcess:
        return 0
    try:
        # Ditunggu sepotong-sepotong supaya Ctrl+C (KeyboardInterrupt) tetap bisa diproses Python.
        while kernel32.WaitForSingleObject(info.hProcess, 250) == _WAIT_TIMEOUT:
            pass
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            return 1
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)
