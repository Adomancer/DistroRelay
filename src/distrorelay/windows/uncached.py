"""Baca ulang file langsung dari perangkat, melewati cache Windows (`FILE_FLAG_NO_BUFFERING`).

Setelah menyalin ke flashdisk/HDD eksternal, membaca ulang dengan `open()` biasa hampir selalu dilayani dari cache di
RAM, jadi tidak membuktikan bahwa data di perangkat benar. Baca tanpa buffering memaksa Windows menulis halaman yang
masih kotor ke disk lalu membaca dari perangkat. Butuh buffer yang sejajar dengan ukuran sektor; `VirtualAlloc` memberi
alamat sejajar halaman (4096), dan ukuran baca kelipatan 1 MiB, jadi cocok untuk sektor 512 maupun 4096 byte.

Di Windows, kegagalan membaca langsung dari perangkat (mis. sektor flashdisk rusak) dilempar sebagai OSError, TIDAK
diganti baca biasa: data yang masih ada di cache bisa membuat salinan rusak terlihat cocok. Di luar Windows dibaca biasa.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from ..hashing import Progress, sha256_file

_CHUNK = 1024 * 1024
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x1
_OPEN_EXISTING = 3
_FILE_FLAG_NO_BUFFERING = 0x20000000
_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
_MEM_COMMIT_RESERVE = 0x1000 | 0x2000
_MEM_RELEASE = 0x8000
_PAGE_READWRITE = 0x04


def _sha256_no_buffering(path: Path, progress: Progress | None = None) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.VirtualAlloc.restype = wintypes.LPVOID
    kernel32.VirtualAlloc.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    kernel32.VirtualFree.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]

    handle = kernel32.CreateFileW(
        str(Path(path).resolve()), _GENERIC_READ, _FILE_SHARE_READ, None, _OPEN_EXISTING,
        _FILE_FLAG_NO_BUFFERING | _FILE_FLAG_SEQUENTIAL_SCAN, None,
    )
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), f"{path} tidak bisa dibuka tanpa cache")
    buffer = kernel32.VirtualAlloc(None, _CHUNK, _MEM_COMMIT_RESERVE, _PAGE_READWRITE)
    if not buffer:
        kernel32.CloseHandle(handle)
        raise OSError(ctypes.get_last_error(), "VirtualAlloc gagal")
    digest = hashlib.sha256()
    total, done = Path(path).stat().st_size, 0
    try:
        read = wintypes.DWORD(0)
        while True:
            if not kernel32.ReadFile(handle, buffer, _CHUNK, ctypes.byref(read), None):
                raise OSError(ctypes.get_last_error(), f"Gagal membaca {path} langsung dari perangkat")
            if read.value == 0:
                break
            digest.update(ctypes.string_at(buffer, read.value))
            done += read.value
            if progress is not None:
                progress(done, total)
    finally:
        kernel32.VirtualFree(buffer, 0, _MEM_RELEASE)
        kernel32.CloseHandle(handle)
    return digest.hexdigest()


def sha256_uncached(path: Path, progress: Progress | None = None) -> str:
    """SHA256 isi file sebagaimana tersimpan di perangkat. OSError kalau tidak bisa dibaca langsung dari perangkat."""
    if sys.platform == "win32":
        return _sha256_no_buffering(Path(path), progress)
    return sha256_file(Path(path), progress)
