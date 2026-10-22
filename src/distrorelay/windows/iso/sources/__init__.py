"""Satu modul per distro. Setiap modul punya:

- `NAME`, `KEYS` (keyring bawaan + fingerprint yang di-pin), `EDITIONS`/`DEFAULT_EDITION`
- `LOCAL_PATTERN`: regex nama file ISO distro itu (grup `version`, opsional `edition`) untuk mengenali ISO di
  USB Ventoy dan di folder unduhan
- `latest(fetch, edition) -> Release`: versi stabil terbaru dari feed resmi. Kalau formatnya berubah, modul
  melempar `IsoError` dan distro itu ditandai "tidak bisa dicek", bukan ditebak.
"""

from __future__ import annotations

from types import ModuleType

from . import arch, fedora, mint, ubuntu

SOURCES: dict[str, ModuleType] = {"fedora": fedora, "arch": arch, "ubuntu": ubuntu, "mint": mint}


def get(distro: str) -> ModuleType:
    return SOURCES[distro]
