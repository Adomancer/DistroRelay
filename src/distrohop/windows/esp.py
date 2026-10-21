"""Mount ESP sementara dan data isinya (read-only)."""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .disks import Partition
from .runner import Runner

log = logging.getLogger(__name__)

_SKIP_DIRS = {"system volume information"}


class EspError(RuntimeError):
    pass


@dataclass
class EspFile:
    path: str  # relatif terhadap root ESP, pemisah "\", mis. "EFI\fedora\shimx64.efi"
    size: int
    sha256: str

    @property
    def name(self) -> str:
        return self.path.rsplit("\\", 1)[-1]


@dataclass
class EspInventory:
    partition_id: str | None
    partition_guid: str | None
    mounted_at: str | None
    temporary_mount: bool
    files: list[EspFile] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)  # mis. EFI\Microsoft\Boot\BCD yang dikunci Windows

    @property
    def partition_number(self) -> int | None:
        if not self.partition_id or "-p" not in self.partition_id:
            return None
        return int(self.partition_id.rsplit("-p", 1)[1])

    def find(self, path: str | None) -> EspFile | None:
        """Cari file (tidak peka huruf besar/kecil, seperti FAT)."""
        if not path:
            return None
        wanted = norm_path(path).lower()
        return next((f for f in self.files if f.path.lower() == wanted), None)

    def find_dir(self, path: str) -> str | None:
        """Nama folder dengan huruf besar/kecil sesuai di disk, atau None."""
        wanted = norm_path(path).lower()
        return next((d for d in self.dirs if d.lower() == wanted), None)

    def has_dir(self, path: str) -> bool:
        return self.find_dir(path) is not None

    def under(self, directory: str) -> list[EspFile]:
        prefix = norm_path(directory).lower() + "\\"
        return [f for f in self.files if f.path.lower().startswith(prefix)]

    def size_of(self, directory: str) -> int:
        return sum(f.size for f in self.under(directory))

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)


def norm_path(path: str) -> str:
    """'/EFI/fedora/' atau '\\EFI\\fedora' -> 'EFI\\fedora'."""
    return path.replace("/", "\\").strip("\\")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_tree(root: Path) -> tuple[list[EspFile], list[str], list[str]]:
    """Data semua file (ukuran + SHA256), folder, dan file yang tidak bisa dibaca di bawah `root`."""
    files: list[EspFile] = []
    dirs: list[str] = []
    unreadable: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: log.warning("ESP: %s", e)):
        dirnames[:] = sorted((d for d in dirnames if d.lower() not in _SKIP_DIRS), key=str.lower)
        rel_dir = Path(dirpath).relative_to(root)
        for name in dirnames:
            dirs.append("\\".join((rel_dir / name).parts))
        for name in sorted(filenames, key=str.lower):
            full = Path(dirpath) / name
            rel = "\\".join((rel_dir / name).parts)
            try:
                files.append(EspFile(rel, full.stat().st_size, _sha256(full)))
            except OSError as exc:
                log.warning("ESP: gagal membaca %s: %s", full, exc)
                unreadable.append(rel)
    return files, dirs, unreadable


def free_drive_letter() -> str:
    """Huruf drive kosong, dicari dari Z mundur."""
    import ctypes

    used = ctypes.windll.kernel32.GetLogicalDrives()
    for letter in "ZYXWVUTSRQPONMLKJIHGFED":
        if not used & (1 << (ord(letter) - ord("A"))):
            return letter
    raise EspError("Tidak ada huruf drive kosong untuk me-mount ESP")


def volume_guid_of(root: str) -> str | None:
    """GUID volume yang ter-mount di `root` (mis. 'S:\\'), dari \\\\?\\Volume{GUID}\\."""
    if sys.platform != "win32":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buffer, len(buffer)):
        return None
    name = buffer.value
    start, end = name.find("{"), name.find("}")
    return name[start + 1 : end].lower() if start != -1 and end != -1 else None


@dataclass
class MountedEsp:
    root: Path
    letter: str
    temporary: bool


@contextmanager
def mounted_esp(runner: Runner, esp: Partition) -> Iterator[MountedEsp]:
    """Mount ESP ke huruf drive kosong (`mountvol X: /S`), lalu pasti di-unmount (`/D`)."""
    if esp.drive_letter:
        yield MountedEsp(Path(f"{esp.drive_letter}:\\"), esp.drive_letter, temporary=False)
        return

    letter = free_drive_letter()
    result = runner.run(["mountvol", f"{letter}:", "/S"])
    if not result.ok:
        raise EspError(f"mountvol {letter}: /S gagal: {result.output() or result.returncode}")
    log.info("ESP di-mount sementara ke %s:", letter)
    try:
        mounted_guid = volume_guid_of(f"{letter}:\\")
        if mounted_guid and esp.guid and mounted_guid != esp.guid:
            raise EspError(
                f"Volume yang ter-mount ({mounted_guid}) bukan ESP yang diharapkan ({esp.guid}); scan ESP dibatalkan"
            )
        yield MountedEsp(Path(f"{letter}:\\"), letter, temporary=True)
    finally:
        undo = runner.run(["mountvol", f"{letter}:", "/D"])
        if undo.ok:
            log.info("ESP di-unmount dari %s:", letter)
        else:
            log.error("Gagal unmount ESP dari %s: %s", letter, undo.output())


def scan_esp(root: Path, esp: Partition, mounted: MountedEsp | None = None) -> EspInventory:
    files, dirs, unreadable = scan_tree(root)
    return EspInventory(
        partition_id=esp.id,
        partition_guid=esp.guid,
        mounted_at=mounted.letter + ":" if mounted else str(root),
        temporary_mount=bool(mounted and mounted.temporary),
        files=files,
        dirs=dirs,
        unreadable=unreadable,
    )
