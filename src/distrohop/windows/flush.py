"""Paksa perubahan boot tertulis ke disk sebelum langkah berikutnya dijalankan.

Windows menunda penulisan hive BCD (file registry `EFI\\Microsoft\\Boot\\BCD` di ESP) dan cache FAT. Di uji VM
skenario 7 (2026-10-22), `bcdedit /delete` yang belum tertulis hilang saat VM dimatikan paksa ±30 detik
kemudian. Saat boot berikutnya Windows melihat objek lama di BCD tanpa entri NVRAM, lalu membuat ulang entri itu
di urutan pertama, di depan Windows Boot Manager.
"""

from __future__ import annotations

import sys
from pathlib import Path

BCD_KEY = "BCD00000000"  # hive BCD sistem, dimuat Windows di HKLM

_GENERIC_READ_WRITE = 0x80000000 | 0x40000000
_FILE_SHARE_READ_WRITE = 0x1 | 0x2
_OPEN_EXISTING = 3


class FlushError(RuntimeError):
    pass


def flush_bcd() -> None:
    """Tulis hive BCD ke disk sekarang (`RegFlushKey`)."""
    if sys.platform != "win32":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, BCD_KEY, 0, winreg.KEY_READ) as key:
            winreg.FlushKey(key)
    except OSError as exc:
        raise FlushError(f"Hive BCD gagal ditulis ke disk: {exc}") from exc


def flush_volume(root: Path) -> None:
    """Tulis cache volume (mis. ESP yang di-mount di S:\\) ke disk sekarang (`FlushFileBuffers`)."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    drive = str(root)[:2]
    if len(drive) != 2 or drive[1] != ":":
        raise FlushError(f"{root} bukan volume ber-huruf drive")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateFileW(
        f"\\\\.\\{drive}", _GENERIC_READ_WRITE, _FILE_SHARE_READ_WRITE, None, _OPEN_EXISTING, 0, None
    )
    if handle in (None, wintypes.HANDLE(-1).value):
        raise FlushError(f"Volume {drive} tidak bisa dibuka untuk ditulis ke disk (WinError {ctypes.get_last_error()})")
    try:
        if not kernel32.FlushFileBuffers(handle):
            raise FlushError(f"Volume {drive} gagal ditulis ke disk (WinError {ctypes.get_last_error()})")
    finally:
        kernel32.CloseHandle(handle)
