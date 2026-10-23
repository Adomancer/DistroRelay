"""Bandingkan versi terbaru tiap distro dengan ISO yang sudah ada di USB Ventoy dan di folder unduhan.

USB Ventoy dikenali dari label volume "Ventoy" (bawaan Ventoy) atau folder `ventoy\\` di root-nya. ISO dikenali
dari nama file per distro (`LOCAL_PATTERN`); ini hanya untuk tampilan, bukan bukti keaslian.
"""

from __future__ import annotations

import logging
import os
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterable

from . import IsoError, Release, SavedRelease, saved_releases, version_key

log = logging.getLogger(__name__)

_DRIVE_REMOVABLE, _DRIVE_FIXED = 2, 3
_SEM_FAILCRITICALERRORS = 0x0001
# Ventoy mencari ISO di semua folder, tapi jarang dalam; batasi supaya scan USB besar tetap cepat.
MAX_DEPTH = 3


@dataclass(frozen=True)
class Volume:
    letter: str
    label: str
    filesystem: str
    removable: bool
    is_system: bool

    @property
    def root(self) -> Path:
        return Path(f"{self.letter}:\\")


def list_volumes() -> list[Volume]:
    """Volume lokal ber-huruf (flashdisk/HDD), tanpa hak Administrator."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    system = (os.environ.get("SystemDrive") or "C:")[0].upper()
    old_mode = kernel32.SetErrorMode(_SEM_FAILCRITICALERRORS)  # card reader kosong: jangan munculkan dialog
    volumes = []
    try:
        mask = kernel32.GetLogicalDrives()
        for index, letter in enumerate(string.ascii_uppercase):
            if not mask & (1 << index):
                continue
            root = f"{letter}:\\"
            kind = kernel32.GetDriveTypeW(root)
            if kind not in (_DRIVE_REMOVABLE, _DRIVE_FIXED):
                continue
            label = ctypes.create_unicode_buffer(261)
            filesystem = ctypes.create_unicode_buffer(261)
            if not kernel32.GetVolumeInformationW(root, label, 261, None, None, None, filesystem, 261):
                continue
            volumes.append(Volume(letter, label.value, filesystem.value, kind == _DRIVE_REMOVABLE, letter == system))
    finally:
        kernel32.SetErrorMode(old_mode)
    return volumes


def is_ventoy(volume: Volume) -> bool:
    if volume.is_system:
        return False
    return volume.label.strip().lower() == "ventoy" or (volume.root / "ventoy").is_dir()


def ventoy_volumes(volumes: Iterable[Volume]) -> list[Volume]:
    return [v for v in volumes if is_ventoy(v)]


@dataclass(frozen=True)
class LocalIso:
    distro: str
    version: str
    edition: str
    path: Path
    size: int


def _walk_isos(root: Path, depth: int = 0) -> Iterable[Path]:
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and entry.name.lower().endswith(".iso"):
                yield Path(entry.path)
            elif entry.is_dir() and depth < MAX_DEPTH and entry.name.lower() not in ("ventoy", "system volume information"):
                yield from _walk_isos(Path(entry.path), depth + 1)
        except OSError:
            continue


def scan_isos(root: Path, sources: dict[str, ModuleType]) -> list[LocalIso]:
    found = []
    for path in _walk_isos(Path(root)):
        for distro, source in sources.items():
            match = source.LOCAL_PATTERN.match(path.name)
            if match:
                edition = match.groupdict().get("edition") or source.DEFAULT_EDITION
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                found.append(LocalIso(distro, match.group("version"), edition, path, size))
                break
    found.sort(key=lambda iso: (iso.distro, version_key(iso.version)), reverse=True)
    return found


# --- Status per distro ------------------------------------------------------------------------

NEW = "versi baru tersedia"
CURRENT = "sudah terbaru"
MISSING = "belum ada di USB"
UNKNOWN = "tidak bisa dicek"


@dataclass
class CatalogRow:
    distro: str
    name: str
    latest: Release | None
    error: str | None = None
    on_drive: list[LocalIso] = field(default_factory=list)
    downloads: list[SavedRelease] = field(default_factory=list)
    drive_checked: bool = True

    @property
    def newest_on_drive(self) -> LocalIso | None:
        return max(self.on_drive, key=lambda iso: version_key(iso.version), default=None)

    @property
    def status(self) -> str:
        if self.latest is None:
            return UNKNOWN
        if not self.drive_checked:
            return ""
        newest = self.newest_on_drive
        if newest is None:
            return MISSING
        return CURRENT if version_key(newest.version) >= version_key(self.latest.version) else NEW

    @property
    def downloaded_latest(self) -> SavedRelease | None:
        if self.latest is None:
            return None
        return next((d for d in self.downloads if d.release == self.latest), None)


def check(
    sources: dict[str, ModuleType],
    fetch: Callable[[str], bytes],
    *,
    drive: Path | None = None,
    editions: dict[str, str] | None = None,
    downloads_root: Path | None = None,
    progress: Callable[[list[str]], None] | None = None,
) -> list[CatalogRow]:
    """Satu baris per distro, urut seperti `sources`. Distro yang feed-nya gagal dibaca tetap tampil dengan alasannya.

    Feed tiap distro diambil bersamaan: satu server yang lambat tidak membuat distro lain ikut menunggu.
    `progress(nama distro yang masih ditunggu)` dipanggil setiap ada yang selesai.
    """
    on_drive = scan_isos(drive, sources) if drive is not None else []

    def newest(distro: str, source: ModuleType) -> tuple[Release | None, str | None]:
        try:
            latest = source.latest(fetch, (editions or {}).get(distro))
            problems = latest.problems()
            return (None, "; ".join(problems)) if problems else (latest, None)
        except IsoError as exc:
            return None, str(exc)
        except Exception as exc:  # satu feed yang aneh tidak boleh menggagalkan semua distro
            log.exception("Cek versi %s gagal", distro)
            return None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:
        futures = {distro: pool.submit(newest, distro, source) for distro, source in sources.items()}
        owner = {future: distro for distro, future in futures.items()}
        waiting = dict.fromkeys(sources)
        for future in as_completed(owner):
            waiting.pop(owner[future], None)
            if progress is not None and waiting:
                progress([sources[d].NAME for d in waiting])
    rows = []
    for distro, source in sources.items():
        latest, error = futures[distro].result()
        rows.append(
            CatalogRow(
                distro,
                source.NAME,
                latest,
                error,
                [iso for iso in on_drive if iso.distro == distro],
                saved_releases(distro, downloads_root),
                drive_checked=drive is not None,
            )
        )
    return rows
