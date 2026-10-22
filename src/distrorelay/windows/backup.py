"""Backup per lapisan (M2a), sebelum apa pun diubah.

Lapisan: file ESP (esp.zip), BCD store (bcdedit /export), entri firmware (teks + JSON), NVRAM mentah,
dan metadata GPT. Semua disimpan di %LOCALAPPDATA%\\DistroRelay\\backups\\<id>\\, disalin ke USB
(ditambah folder esp\\ biasa yang bisa di-xcopy dari WinRE), lalu diverifikasi dengan dibaca ulang.

Sengaja TIDAK disamarkan: NVRAM mentah dan daftar file ini adalah backup milik kamu sendiri,
bukan laporan untuk dibagikan.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from .. import __version__
from ..paths import app_data_dir
from .bootentries import parse_firmware
from .disks import Partition, parse_inventory
from .esp import MountedEsp
from .nvram import snapshot_from_raw
from .runner import AllowListRunner, Runner
from .preflight import M2A_CMDLETS
from .scanner import ScanReport
from .uncached import sha256_uncached

log = logging.getLogger(__name__)

_SKIP_DIRS = {"system volume information"}
# ID backup = waktu pembuatan (create_backup). Hanya bentuk ini yang diterima, bukan path: Path(root) / "C:\\..." akan
# membuang root-nya, sehingga "salinan eksternal" bisa diam-diam menunjuk ke salinan lokal yang sama.
BACKUP_ID = re.compile(r"^\d{8}-\d{6}$")
# File ESP yang memang tidak bisa dibaca saat Windows jalan (hive BCD); dicadangkan lewat bcdedit /export.
_LOCKED_ESP_FILE = re.compile(r"^efi\\microsoft\\boot\\bcd(\.log\d*)?$", re.IGNORECASE)


def valid_backup_id(backup_id: str | None) -> bool:
    return bool(backup_id) and BACKUP_ID.fullmatch(backup_id) is not None
_CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    pass


def backups_root() -> Path:
    return app_data_dir() / "backups"


def export_rule(root: Path) -> Callable[[Sequence[str]], bool]:
    """Izinkan `bcdedit /export <file>` hanya ke dalam folder backup."""
    allowed = Path(root).resolve()

    def rule(args: Sequence[str]) -> bool:
        if len(args) != 3 or Path(args[0]).stem.lower() != "bcdedit" or args[1].lower() != "/export":
            return False
        target = Path(args[2]).resolve()
        return allowed == target or allowed in target.parents

    return rule


def backup_runner(inner: Runner, root: Path | None = None) -> AllowListRunner:
    """Runner tahap M2a: read-only + mount ISO read-only + bcdedit /export ke folder backup."""
    return AllowListRunner(inner, extra_cmdlets=M2A_CMDLETS, rules=(export_rule(root or backups_root()),))


@dataclass
class BackupFile:
    name: str
    size: int
    sha256: str


@dataclass
class Verification:
    name: str
    ok: bool
    message: str


@dataclass
class BackupResult:
    backup_id: str
    local_dir: str
    usb_dir: str | None
    local_only: bool = False  # mode coba tanpa USB: belum memenuhi syarat eksekusi M2b
    files: list[BackupFile] = field(default_factory=list)
    esp_files: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    verifications: list[Verification] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.verifications) and all(v.ok for v in self.verifications)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def zip_tree(root: Path, zip_path: Path, opener: Callable = open) -> tuple[list[dict], list[str]]:
    """Zip seluruh isi `root`. Hash dihitung dari byte yang sama dengan yang ditulis ke zip.

    Mengembalikan (daftar file {path, size, sha256}, daftar file yang tidak bisa dibaca).
    """
    entries: list[dict] = []
    skipped: list[str] = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted((d for d in dirnames if d.lower() not in _SKIP_DIRS), key=str.lower)
            rel_dir = Path(dirpath).relative_to(root)
            if rel_dir.parts and not filenames and not dirnames:
                archive.writestr("/".join(rel_dir.parts) + "/", b"")
            for name in sorted(filenames, key=str.lower):
                full = Path(dirpath) / name
                rel_parts = (rel_dir / name).parts
                try:
                    source = opener(full, "rb")
                except OSError as exc:
                    log.warning("Backup: %s dilewati: %s", full, exc)
                    skipped.append("\\".join(rel_parts))
                    continue
                # strict_timestamps=False: tanggal sebelum 1980 (tidak didukung ZIP) dibulatkan, bukan error.
                info = zipfile.ZipInfo.from_file(full, "/".join(rel_parts), strict_timestamps=False)
                info.compress_type = zipfile.ZIP_DEFLATED
                digest, size = hashlib.sha256(), 0
                with source, archive.open(info, "w") as target:
                    for chunk in iter(lambda: source.read(_CHUNK), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        target.write(chunk)
                entries.append({"path": "\\".join(rel_parts), "size": size, "sha256": digest.hexdigest()})
    return entries, skipped


def verify_zip(zip_path: Path, expected: list[dict]) -> Verification:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            broken = archive.testzip()
            if broken is not None:
                return Verification("Isi esp.zip", False, f"File rusak di dalam zip: {broken}")
            mismatched = []
            for entry in expected:
                with archive.open(entry["path"].replace("\\", "/")) as handle:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: handle.read(_CHUNK), b""):
                        digest.update(chunk)
                if digest.hexdigest() != entry["sha256"]:
                    mismatched.append(entry["path"])
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        return Verification("Isi esp.zip", False, f"Zip tidak bisa dibaca: {exc}")
    if mismatched:
        return Verification("Isi esp.zip", False, f"{len(mismatched)} file tidak cocok, mis. {mismatched[0]}")
    return Verification("Isi esp.zip", True, f"{len(expected)} file dibaca ulang dari zip, semua hash cocok")


def verify_files(directory: Path, files: list[BackupFile], label: str, hasher: Callable = sha256_file) -> Verification:
    mismatched = []
    for f in files:
        path = directory / f.name
        try:
            if not path.exists() or hasher(path) != f.sha256:
                mismatched.append(f.name)
        except OSError as exc:  # mis. sektor rusak saat dibaca langsung dari perangkat
            mismatched.append(f"{f.name} (tidak terbaca: {exc.strerror or exc})")
    if mismatched:
        return Verification(label, False, f"Tidak cocok/hilang: {', '.join(mismatched)}")
    return Verification(label, True, f"{len(files)} file dibaca ulang, semua hash cocok")


def verify_tree(directory: Path, expected: list[dict], label: str, hasher: Callable = sha256_file) -> Verification:
    mismatched = []
    for entry in expected:
        path = directory.joinpath(*entry["path"].split("\\"))
        try:
            if not path.exists() or hasher(path) != entry["sha256"]:
                mismatched.append(entry["path"])
        except OSError as exc:
            mismatched.append(f"{entry['path']} (tidak terbaca: {exc.strerror or exc})")
    if mismatched:
        return Verification(label, False, f"{len(mismatched)} file tidak cocok/hilang, mis. {mismatched[0]}")
    return Verification(label, True, f"{len(expected)} file dibaca ulang, semua hash cocok")


def verify_backup_dir(directory: Path, hasher: Callable = sha256_file) -> list[Verification]:
    """Verifikasi ulang folder backup (lokal atau USB) terhadap manifest.json-nya.

    Untuk salinan di drive eksternal, pakai `hasher=sha256_uncached` supaya yang dibaca isi perangkatnya, bukan cache.
    """
    directory = Path(directory)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [Verification("manifest.json", False, f"Tidak bisa dibaca: {exc}")]
    files = [BackupFile(**f) for f in manifest.get("files", [])]
    checks = [verify_files(directory, files, "Hash file backup", hasher)]
    if (directory / "esp.zip").exists():
        checks.append(verify_zip(directory / "esp.zip", manifest.get("esp_files", [])))
    if (directory / "esp").is_dir():
        checks.append(verify_tree(directory / "esp", manifest.get("esp_files", []), "Folder esp\\", hasher))
    return checks


def create_backup(
    report: ScanReport,
    runner: Runner,
    mount_esp: Callable[[Runner, Partition], AbstractContextManager[MountedEsp]],
    *,
    local_root: Path | None = None,
    usb_root: Path | None = None,
    local_only: bool = False,
    opener: Callable = open,
    progress: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> BackupResult:
    say = progress or (lambda message: None)
    if local_only and usb_root is not None:
        raise BackupError("Pilih salah satu: salinan ke USB, atau mode lokal saja")
    if report.inventory is None or report.nvram is None or report.firmware is None or report.esp is None:
        raise BackupError("Scan belum lengkap (butuh Administrator): disk, NVRAM, entri boot, dan ESP harus terbaca")
    esp_part = report.inventory.esp()
    if esp_part is None:
        raise BackupError("ESP tidak ditemukan")

    backup_id = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    local = (local_root or backups_root()) / backup_id
    local.mkdir(parents=True, exist_ok=False)
    result = BackupResult(backup_id, str(local), None, local_only=local_only)

    say("Menyalin seluruh isi ESP ke esp.zip…")
    with mount_esp(runner, esp_part) as mounted:
        result.esp_files, result.skipped = zip_tree(mounted.root, local / "esp.zip", opener)

    say("Mengekspor BCD store (bcdedit /export)…")
    exported = runner.run(["bcdedit", "/export", str(local / "bcd-store.bak")])
    if not exported.ok:
        raise BackupError(f"bcdedit /export gagal: {exported.output() or exported.returncode}")

    say("Menyimpan entri firmware, NVRAM mentah, dan metadata partisi…")
    (local / "firmware-entries.txt").write_bytes(report.raw["bcdedit-firmware.txt"].replace("\r\n", "\n").encode("utf-8"))
    entries = [dataclasses.asdict(e) for e in report.boot_entries]
    (local / "firmware-entries.json").write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    (local / "nvram-raw.json").write_text(json.dumps(report.nvram.raw, indent=2), encoding="utf-8")
    (local / "partitions.json").write_text(report.raw["disks.json"], encoding="utf-8")
    # UUID filesystem per partisi (dari superblock): setelah partisi dipulihkan, isinya dicocokkan dengan ini.
    fsinfo = {pid: dataclasses.asdict(info) for pid, info in sorted(report.fsinfo.items())}
    (local / "fsinfo.json").write_text(json.dumps(fsinfo, indent=2, ensure_ascii=False), encoding="utf-8")

    # bcdedit /export juga membuat log transaksi hive (bcd-store.bak.LOG*, tersembunyi); ikut dicadangkan.
    bcd_files = sorted(p.name for p in local.glob("bcd-store.bak*"))
    names = ("esp.zip", *bcd_files, "firmware-entries.txt", "firmware-entries.json", "nvram-raw.json",
             "partitions.json", "fsinfo.json")
    result.files = [BackupFile(name, (local / name).stat().st_size, sha256_file(local / name)) for name in names]
    manifest = {
        "distrorelay": __version__,
        "backup_id": backup_id,
        "created_at": (now or datetime.now()).astimezone().isoformat(timespec="seconds"),
        "layout_fingerprint": report.inventory.fingerprint(),
        # M2b hanya menerima backup yang punya salinan USB terverifikasi.
        "usb_copy": usb_root is not None,
        "esp_partition": {"id": esp_part.id, "guid": esp_part.guid, "offset": esp_part.offset, "size": esp_part.size},
        "files": [dataclasses.asdict(f) for f in result.files],
        "esp_files": result.esp_files,
        "skipped": [{"path": p, "reason": "tidak bisa dibaca (dikunci Windows); BCD dicadangkan lewat bcd-store.bak"} for p in result.skipped],
    }
    (local / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    say("Memverifikasi backup lokal…")
    checks = result.verifications
    checks.append(verify_zip(local / "esp.zip", result.esp_files))
    scanned = {f.path.lower(): f.sha256 for f in report.esp.files}
    changed = [e["path"] for e in result.esp_files if scanned.get(e["path"].lower()) not in (None, e["sha256"])]
    added = [e["path"] for e in result.esp_files if e["path"].lower() not in scanned]
    backed = {e["path"].lower() for e in result.esp_files} | {p.lower() for p in result.skipped}
    removed = [p for p in scanned if p not in backed]
    unreadable = [p for p in result.skipped if not _LOCKED_ESP_FILE.match(p)]
    if changed or added or removed:
        checks.append(Verification("ESP sama dengan saat scan", False,
                                   f"Berubah sejak scan: {(changed + added + removed)[:3]}; scan ulang"))
    else:
        checks.append(Verification("ESP sama dengan saat scan", True, "Semua file sama dengan hasil scan"))
    if unreadable:
        checks.append(Verification("Semua file ESP tercadang", False,
                                   f"Tidak bisa dibaca saat backup: {unreadable[:3]}; tutup program yang memakainya lalu ulangi"))

    # bcdedit /store me-load file sebagai registry hive dan memperbarui header-nya, jadi yang dibaca
    # adalah salinan sementara. File backup itu sendiri tidak boleh berubah setelah hash-nya dicatat.
    with tempfile.TemporaryDirectory(prefix="distrorelay-bcd-", ignore_cleanup_errors=True) as scratch:
        for name in bcd_files:
            shutil.copy2(local / name, Path(scratch) / name)
        store = runner.run(["bcdedit", "/store", str(Path(scratch) / "bcd-store.bak"), "/enum", "all"])
    checks.append(
        Verification("BCD store bisa dibaca", store.ok and bool(store.stdout.strip()),
                     "bcdedit /store … /enum all berhasil (pada salinan sementara)" if store.ok else f"Gagal: {store.output()}")
    )
    firmware = parse_firmware((local / "firmware-entries.txt").read_text(encoding="utf-8"))
    nvram = snapshot_from_raw(json.loads((local / "nvram-raw.json").read_text(encoding="utf-8")))
    same_entries = len(firmware.entries) == len(report.firmware.entries) and set(nvram.options) == set(report.nvram.options)
    checks.append(
        Verification("Entri firmware & NVRAM bisa dibaca ulang", same_entries,
                     f"{len(firmware.entries)} entri bcdedit, {len(nvram.options)} entri NVRAM")
    )
    layout_ok = parse_inventory((local / "partitions.json").read_text(encoding="utf-8")).fingerprint() == report.inventory.fingerprint()
    checks.append(Verification("Metadata partisi bisa dibaca ulang", layout_ok, "Sidik jari layout cocok" if layout_ok else "Tidak cocok"))
    checks.append(verify_files(local, result.files, "Hash file backup lokal"))

    if usb_root is not None:
        say(f"Menyalin backup ke drive eksternal {usb_root}…")
        usb = Path(usb_root) / "DistroRelay" / "backups" / backup_id
        try:
            usb.mkdir(parents=True, exist_ok=False)
            for name in [f.name for f in result.files] + ["manifest.json"]:
                shutil.copy2(local / name, usb / name)
            with zipfile.ZipFile(local / "esp.zip") as archive:
                archive.extractall(usb / "esp")
        except OSError as exc:
            # Drive eksternal bermasalah tidak boleh menggagalkan seluruh backup: salinan lokal tetap utuh.
            log.error("Gagal menulis ke %s: %s", usb, exc)
            shutil.rmtree(usb, ignore_errors=True)
            checks.append(
                Verification(
                    "Salinan USB",
                    False,
                    f"Gagal menulis ke {usb_root}: {exc}. Salinan yang setengah jadi sudah dihapus; "
                    "backup lokal tetap utuh. Cek kabel, port, atau drive-nya",
                )
            )
        else:
            result.usb_dir = str(usb)
            say("Memverifikasi salinan di drive eksternal (dibaca langsung dari perangkat)…")
            checks.append(verify_files(usb, result.files + [BackupFile("manifest.json", 0, sha256_file(local / "manifest.json"))],
                                       "Hash salinan USB", sha256_uncached))
            checks.append(verify_tree(usb / "esp", result.esp_files, "Folder esp\\ di USB (untuk xcopy dari WinRE)",
                                      sha256_uncached))
    elif not local_only:
        checks.append(Verification("Salinan USB", False, "Tidak ada USB tujuan; backup hanya ada di C:"))

    (local / "verification.json").write_text(
        json.dumps([dataclasses.asdict(v) for v in checks], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result
