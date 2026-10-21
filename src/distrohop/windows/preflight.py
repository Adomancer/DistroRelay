"""Preflight M2: syarat sebelum backup (M2a) dan sebelum eksekusi (M2b).

Tidak ada yang diubah di sini. Satu-satunya efek samping adalah mount sementara: ESP (untuk
mengecek tanda tangan bootmgfw.efi) dan ISO Windows di USB Ventoy, yang di-mount read-only lalu
dilepas lagi.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..format import human_size
from .disks import DiskInventory, Partition
from .runner import Runner, powershell_args
from .scanner import ScanReport
from .signature import Signature, check_signature

log = logging.getLogger(__name__)

OK = "ok"
WARNING = "peringatan"
FAIL = "gagal"
MANUAL = "perlu konfirmasi"

# Cmdlet tambahan yang boleh dipakai preflight/backup M2a (lihat AllowListRunner).
M2A_CMDLETS = frozenset({"mount-diskimage", "dismount-diskimage"})

ISO_MOUNT_SCRIPT = r"""# distrohop:iso-mount
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$image = Mount-DiskImage -ImagePath '__PATH__' -Access ReadOnly -StorageType ISO -PassThru
$letter = ($image | Get-Volume).DriveLetter
ConvertTo-Json -InputObject ([string]$letter) -Compress
"""

ISO_DISMOUNT_SCRIPT = r"""# distrohop:iso-dismount
$ErrorActionPreference = 'Stop'
$null = Dismount-DiskImage -ImagePath '__PATH__'
"""

_INSTALLER_FILES = ("sources\\install.wim", "sources\\install.esd", "sources\\install.swm")
# Nama hanya dipakai untuk memilih ISO mana yang dicoba di-mount; buktinya isi ISO itu sendiri.
_WINDOWS_ISO_HINT = re.compile(r"win|w10|w11", re.IGNORECASE)


@dataclass
class Check:
    key: str
    title: str
    status: str
    message: str
    details: list[str] = field(default_factory=list)


@dataclass
class EmergencyMedia:
    drive: str
    kind: str  # "installer" (USB installer biasa) atau "iso" (ISO Windows di Ventoy)
    source: str


@dataclass
class UsbTarget:
    drive: str
    label: str | None
    filesystem: str | None
    free: int
    disk: str = ""  # mis. "WD Elements (USB)"


@dataclass
class PreflightResult:
    checks: list[Check]
    media: list[EmergencyMedia]
    usb_targets: list[UsbTarget]
    signature: Signature | None

    def get(self, key: str) -> Check | None:
        return next((c for c in self.checks if c.key == key), None)

    @property
    def can_backup(self) -> bool:
        return all(c.status != FAIL for c in self.checks if c.key in ("admin", "layout", "backup-usb"))

    @property
    def can_backup_locally(self) -> bool:
        """Backup tanpa USB (mode coba): cukup admin dan layout yang tidak berubah."""
        return all(c.status != FAIL for c in self.checks if c.key in ("admin", "layout"))

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def needs_confirmation(self) -> list[Check]:
        return [c for c in self.checks if c.status == MANUAL]


def _script(template: str, path: Path | str) -> str:
    return template.replace("__PATH__", str(path).replace("'", "''"))


def _root(letter: str) -> Path:
    return Path(f"{letter}:\\")


def system_disks(inventory: DiskInventory) -> set[int]:
    """Disk yang berisi Windows/ESP. Backup di disk ini tidak dihitung sebagai salinan terpisah."""
    disks = {d.number for d in inventory.disks if d.is_boot or d.is_system}
    disks |= {p.disk_number for p in inventory.partitions if p.is_boot or p.is_system or p.category == "esp"}
    return disks


def external_volumes(inventory: DiskInventory) -> list[Partition]:
    """Volume ber-huruf di disk fisik selain disk Windows: flashdisk, HDD/SSD eksternal, apa pun bus-nya."""
    excluded = system_disks(inventory)
    return [p for p in inventory.partitions if p.disk_number not in excluded and p.drive_letter]


def _iso_has_installer(runner: Runner, iso: Path, root_of: Callable[[str], Path]) -> bool:
    mounted = runner.run(powershell_args(_script(ISO_MOUNT_SCRIPT, iso)))
    if not mounted.ok:
        log.warning("Gagal me-mount %s: %s", iso, mounted.output())
        return False
    try:
        letter = (mounted.stdout.strip().strip('"') or "").strip()
        return bool(letter) and any((root_of(letter) / rel).exists() for rel in _INSTALLER_FILES)
    finally:
        unmounted = runner.run(powershell_args(_script(ISO_DISMOUNT_SCRIPT, iso)))
        if not unmounted.ok:
            log.error("Gagal melepas %s: %s", iso, unmounted.output())


def find_emergency_media(
    volumes: list[Partition], runner: Runner, root_of: Callable[[str], Path] = _root
) -> list[EmergencyMedia]:
    media: list[EmergencyMedia] = []
    for volume in volumes:
        root = root_of(volume.drive_letter)
        installer = next((rel for rel in _INSTALLER_FILES if (root / rel).exists()), None)
        if installer:
            media.append(EmergencyMedia(volume.drive_letter, "installer", f"{volume.drive_letter}:\\{installer}"))
            continue
        try:
            isos = sorted([*root.glob("*.iso"), *root.glob("*/*.iso")])
        except OSError:
            continue
        for iso in isos:
            if not _WINDOWS_ISO_HINT.search(iso.name):
                continue
            try:
                found = _iso_has_installer(runner, iso, root_of)
            except Exception as exc:  # satu ISO bermasalah tidak boleh menggagalkan preflight
                log.warning("Gagal memeriksa %s: %s", iso, exc)
                continue
            if found:
                media.append(EmergencyMedia(volume.drive_letter, "iso", str(iso)))
                break
    return media


def run_preflight(
    report: ScanReport,
    runner: Runner,
    *,
    fresh_inventory: DiskInventory | None,
    mount_esp,
    root_of: Callable[[str], Path] = _root,
    disk_usage: Callable[[Path], tuple] = shutil.disk_usage,
    signature_of: Callable[[Runner, Path], Signature] = check_signature,
) -> PreflightResult:
    checks: list[Check] = []
    inventory = report.inventory

    checks.append(
        Check("admin", "Hak Administrator", OK if report.is_admin else FAIL,
              "Berjalan sebagai Administrator" if report.is_admin else "Jalankan ulang sebagai Administrator")
    )

    if inventory is None or fresh_inventory is None:
        checks.append(Check("layout", "Layout disk tidak berubah", FAIL, "Daftar disk tidak terbaca"))
    elif inventory.fingerprint() == fresh_inventory.fingerprint():
        checks.append(Check("layout", "Layout disk tidak berubah", OK, "Sama dengan saat scan"))
    else:
        checks.append(Check("layout", "Layout disk tidak berubah", FAIL, "Layout disk berubah sejak scan; scan ulang dulu"))

    # Windows Boot Manager: entri + file + tanda tangan Microsoft.
    signature = None
    managers = [e for e in report.boot_entries if e.is_windows_boot_manager]
    bootmgfw = report.esp.find("EFI\\Microsoft\\Boot\\bootmgfw.efi") if report.esp else None
    esp_part = inventory.esp() if inventory else None
    details = []
    if not report.boot_entries:
        status, message = FAIL, "Entri boot belum terbaca (butuh Administrator)"
    elif not managers:
        status, message = FAIL, "Entri Windows Boot Manager tidak ditemukan"
    elif bootmgfw is None or esp_part is None:
        status, message = FAIL, "File EFI\\Microsoft\\Boot\\bootmgfw.efi tidak ditemukan di ESP"
    else:
        try:
            with mount_esp(runner, esp_part) as mounted:
                signature = signature_of(runner, mounted.root / "EFI" / "Microsoft" / "Boot" / "bootmgfw.efi")
            if signature.is_microsoft:
                status, message = OK, "Entri ada, file ada, tanda tangan Microsoft valid"
            else:
                status, message = FAIL, f"Tanda tangan bootmgfw.efi tidak valid: {signature.describe()}"
            details.append(f"Tanda tangan: {signature.describe()}")
        except Exception as exc:
            status, message = FAIL, f"Tanda tangan bootmgfw.efi tidak bisa dicek: {exc}"
        order = managers[0].order
        details.append(
            "Sudah di urutan boot pertama" if order == 0 else f"Sekarang urutan ke-{(order or 0) + 1}; akan dijadikan pertama"
        )
    checks.append(Check("windows-boot-manager", "Windows Boot Manager sehat", status, message, details))

    if report.bitlocker is None:
        checks.append(Check("bitlocker", "BitLocker / Device Encryption", MANUAL, "Status tidak terbaca; cek manual"))
    elif any(v.needs_recovery_key for v in report.bitlocker):
        checks.append(
            Check("bitlocker", "BitLocker / Device Encryption", MANUAL,
                  "Proteksi AKTIF: pastikan recovery key tersimpan di akun Microsoft. "
                  "Suspend-BitLocker akan dijalankan sebelum eksekusi (M2b)")
        )
    elif any(v.encrypted for v in report.bitlocker):
        checks.append(Check("bitlocker", "BitLocker / Device Encryption", WARNING, "Terenkripsi, tapi proteksi tidak aktif"))
    else:
        checks.append(Check("bitlocker", "BitLocker / Device Encryption", OK, "Tidak terenkripsi"))

    volumes = external_volumes(inventory) if inventory else []
    media = find_emergency_media(volumes, runner, root_of)
    if media:
        found = [f"{m.drive}: {m.source} ({'USB installer' if m.kind == 'installer' else 'ISO di Ventoy'})" for m in media]
        checks.append(
            Check("emergency-usb", "USB darurat Windows", MANUAL,
                  "Terdeteksi. Centang setelah USB ini dicoba boot sampai Command Prompt (Repair your computer)", found)
        )
    else:
        checks.append(
            Check("emergency-usb", "USB darurat Windows", MANUAL,
                  "Tidak terdeteksi otomatis. Siapkan USB installer Windows (atau Ventoy + ISO Windows), "
                  "lalu coba boot sampai Command Prompt")
        )

    needed = (report.esp.total_size if report.esp else 0) * 3 + 16 * 1024 * 1024
    targets = []
    for volume in volumes:
        try:
            free = disk_usage(root_of(volume.drive_letter))[2]
        except OSError:
            continue
        disk = inventory.disk(volume.disk_number)
        where = f"{disk.model} ({disk.bus_type})" if disk else ""
        targets.append(UsbTarget(volume.drive_letter, volume.label, volume.filesystem, free, where))
    usable = [t for t in targets if t.free >= needed]
    title = "Drive eksternal untuk salinan backup"
    if usable:
        checks.append(
            Check("backup-usb", title, OK, f"Butuh ±{human_size(needed)} di disk fisik selain SSD Windows",
                  [f"{t.drive}: {t.label or '(tanpa label)'} {t.filesystem or ''}, {t.disk}, kosong {human_size(t.free)}"
                   for t in usable])
        )
    else:
        checks.append(
            Check("backup-usb", title, FAIL,
                  f"Colok flashdisk atau HDD eksternal dengan ruang kosong minimal {human_size(needed)}")
        )
    return PreflightResult(checks, media, usable, signature)
