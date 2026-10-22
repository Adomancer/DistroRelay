"""Alur ISO manager: unduh + verifikasi, verifikasi ulang, dan salin ke USB Ventoy.

ISO hanya disalin kalau dua lapis verifikasi lolos tepat sebelum penyalinan. Salinan ditulis dengan nama
sementara (`.distrorelay-part`, tidak terlihat oleh Ventoy), SHA256-nya dihitung saat menyalin, lalu dibaca ulang
dari USB. ISO versi lama di USB tidak pernah dihapus.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import DETACHED_ISO, VERIFIED_FILE, KeySpec, Release, SavedRelease, release_folder, save_release
from . import download as download_mod
from .verify import GpgvRun, Step, Verification, run_gpgv, sha256_file, signed_sha256, verify_release

FAT32_LIMIT = 4 * 1024**3 - 1
FREE_MARGIN = 64 * 1024**2
PART_SUFFIX = ".distrorelay-part"
REJECTED_SUFFIX = ".rejected"

Progress = Callable[[int, int | None], None]


@dataclass
class Tools:
    """Ketergantungan jaringan & gpgv, supaya test bisa memalsukannya."""

    gpgv: str
    fetch: Callable[[str], bytes] = download_mod.fetch
    download: Callable[..., Path] = download_mod.download
    run_gpgv: GpgvRun = run_gpgv
    root: Path | None = None  # folder unduhan; None = %LOCALAPPDATA%\DistroRelay\iso


@dataclass
class PrepareResult:
    release: Release
    folder: Path
    verification: Verification
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verification.ok


def _record(release: Release, folder: Path, result: Verification) -> None:
    """Catat hasil verifikasi. ISO yang SHA256-nya salah disisihkan supaya unduhan berikutnya mulai dari awal."""
    marker = folder / VERIFIED_FILE
    iso = folder / release.iso_name
    if result.ok:
        marker.write_text(
            json.dumps(
                {
                    "iso_name": release.iso_name,
                    "sha256": result.sha256,
                    "size": iso.stat().st_size,
                    "signer": result.signer,
                    "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return
    marker.unlink(missing_ok=True)
    if result.iso_bad and iso.is_file():
        iso.replace(iso.with_name(iso.name + REJECTED_SUFFIX))


def prepare(
    release: Release,
    keys: KeySpec,
    tools: Tools,
    *,
    progress: Progress | None = None,
    hash_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> PrepareResult:
    """Unduh file checksum/tanda tangan, cek tanda tangan dulu (kalau bisa), unduh ISO, lalu verifikasi penuh."""
    folder = release_folder(release, tools.root)
    save_release(release, folder)
    notes = []
    for url, name in ((release.checksum_url, release.checksum_name), (release.signature_url, release.signature_name)):
        if url:
            (folder / name).write_bytes(tools.fetch(url))
    if release.scheme != DETACHED_ISO:
        # Tanda tangan file checksum dicek sebelum ISO besar diunduh.
        early = signed_sha256(release, folder, keys, gpgv=tools.gpgv, run=tools.run_gpgv)
        if not early.ok:
            _record(release, folder, early)
            return PrepareResult(release, folder, early, notes)
    iso = folder / release.iso_name
    if iso.is_file():
        existing = verify_release(release, folder, keys, gpgv=tools.gpgv, run=tools.run_gpgv, progress=hash_progress)
        _record(release, folder, existing)
        if existing.ok:
            notes.append("ISO sudah terunduh sebelumnya; tidak diunduh ulang.")
            return PrepareResult(release, folder, existing, notes)
        if iso.is_file():  # gagal bukan karena isi ISO (mis. gpgv bermasalah): jangan unduh ulang
            return PrepareResult(release, folder, existing, notes)
        notes.append("ISO lama tidak lolos verifikasi, jadi disisihkan (.rejected) dan diunduh ulang.")
    tools.download(release.iso_url, iso, expected_size=release.size, progress=progress, should_stop=should_stop)
    result = verify_release(release, folder, keys, gpgv=tools.gpgv, run=tools.run_gpgv, progress=hash_progress)
    _record(release, folder, result)
    if not result.ok and not iso.is_file():
        notes.append(f"ISO yang gagal verifikasi disimpan sebagai {release.iso_name}{REJECTED_SUFFIX}; jangan dipakai.")
    return PrepareResult(release, folder, result, notes)


def reverify(saved: SavedRelease, keys: KeySpec, tools: Tools, *, hash_progress=None) -> Verification:
    """Verifikasi ulang unduhan yang ada, tanpa internet."""
    result = verify_release(saved.release, saved.folder, keys, gpgv=tools.gpgv, run=tools.run_gpgv, progress=hash_progress)
    _record(saved.release, saved.folder, result)
    return result


# --- Salin ke USB ------------------------------------------------------------------------------


def copy_to_drive(
    saved: SavedRelease,
    keys: KeySpec,
    tools: Tools,
    target: Path,
    *,
    filesystem: str | None = None,
    progress: Progress | None = None,
    hash_progress: Callable[[int, int], None] | None = None,
    disk_usage: Callable[[Path], tuple] = shutil.disk_usage,
) -> list[Step]:
    """Verifikasi ulang tanda tangan tepat sebelum menyalin; SHA256 dicek saat menyalin dan dibaca ulang dari USB."""
    release = saved.release
    iso = saved.iso_path
    if not iso.is_file():
        return [Step("ISO", False, f"{iso} belum terunduh")]
    signed = signed_sha256(release, saved.folder, keys, gpgv=tools.gpgv, run=tools.run_gpgv)
    steps = list(signed.steps)
    if not signed.ok or not signed.sha256:
        if signed.ok:
            steps.append(Step("Checksum resmi", False, "tidak ada nilai SHA256 resmi"))
        return steps
    return steps + copy_verified(iso, Path(target), signed.sha256, filesystem=filesystem, progress=progress,
                                 hash_progress=hash_progress, disk_usage=disk_usage)


def copy_verified(
    source: Path,
    target_dir: Path,
    expected_sha256: str,
    *,
    filesystem: str | None = None,
    progress: Progress | None = None,
    hash_progress: Callable[[int, int], None] | None = None,
    disk_usage: Callable[[Path], tuple] = shutil.disk_usage,
) -> list[Step]:
    size = source.stat().st_size
    dest = target_dir / source.name
    label = f"Salin ke {target_dir}"
    if dest.exists():
        if dest.is_file() and dest.stat().st_size == size and sha256_file(dest, hash_progress) == expected_sha256:
            return [Step(label, True, f"{dest.name} sudah ada dan identik; tidak disalin ulang")]
        return [Step(label, False, f"{dest} sudah ada tapi isinya berbeda; tidak ditimpa (pindahkan/hapus sendiri)")]
    if (filesystem or "").upper() == "FAT32" and size > FAT32_LIMIT:
        return [Step(label, False, "USB berformat FAT32 tidak bisa menyimpan file > 4 GB; pakai exFAT/NTFS (opsi Ventoy)")]
    free = disk_usage(target_dir)[2]
    if free < size + FREE_MARGIN:
        return [Step(label, False, f"ruang kosong {free} byte, butuh {size + FREE_MARGIN}")]

    partial = target_dir / (source.name + PART_SUFFIX)
    digest = hashlib.sha256()
    done = 0
    try:
        with open(source, "rb") as reader, open(partial, "wb") as writer:
            while block := reader.read(4 * 1024 * 1024):
                digest.update(block)
                writer.write(block)
                done += len(block)
                if progress is not None:
                    progress(done, size)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != expected_sha256:
        partial.unlink(missing_ok=True)
        return [Step(label, False, "ISO sumber berubah atau rusak saat dibaca (SHA256 tidak cocok); tidak disalin")]
    os.replace(partial, dest)
    steps = [Step(label, True, f"{dest.name} ({size} byte)")]
    readback = sha256_file(dest, hash_progress)
    if readback != expected_sha256:
        dest.unlink(missing_ok=True)
        steps.append(Step("Baca ulang dari USB", False, "SHA256 salinan berbeda; salinan dihapus (cek USB-nya)"))
    else:
        steps.append(Step("Baca ulang dari USB", True, "SHA256 salinan cocok"))
    return steps
