"""Orkestrasi scan read-only (M1): inventaris, ESP, entri boot, NVRAM, fsprobe, BitLocker, analisis.

Setiap langkah berdiri sendiri: kalau satu gagal, hasilnya dicatat sebagai `Issue` dan scan lanjut.
Semua perintah lewat `ReadOnlyRunner`, jadi scan secara teknis tidak bisa mengubah sistem
(kecuali mount ESP sementara, yang selalu di-unmount lagi).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import platform
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import __version__
from . import bitlocker as bitlocker_mod
from . import bootentries, bootrefs, disks, esp as esp_mod, fsprobe, gpt_types, nvram
from .bootentries import BootEntry, FirmwareConfig
from .bootrefs import Analysis, EspRefs
from .disks import DiskInventory, Partition
from .elevate import is_admin
from .esp import EspInventory, MountedEsp
from .fsprobe import FsInfo
from .nvram import NvramSnapshot
from .runner import ReadOnlyRunner, Runner, SubprocessRunner

log = logging.getLogger(__name__)

# Partisi yang isinya di-probe: Linux, tipe tak dikenal, dan ESP (untuk mencocokkan UUID vfat-nya).
_PROBE_CATEGORIES = {"linux", "unknown", "other", "esp"}


@dataclass
class Issue:
    step: str
    message: str
    severity: str = "warning"  # "warning" | "error" | "info"


@dataclass
class ScanReport:
    created_at: str
    version: str
    is_admin: bool
    windows: str
    inventory: DiskInventory | None = None
    esp: EspInventory | None = None
    esp_refs: EspRefs | None = None
    firmware: FirmwareConfig | None = None
    nvram: NvramSnapshot | None = None
    fsinfo: dict[str, FsInfo] = field(default_factory=dict)
    fs_probe_complete: bool = False
    gpt_slots: dict[int, dict[int, str]] = field(default_factory=dict)  # disk -> slot GPT -> GUID partisi
    bitlocker: list[bitlocker_mod.BitLockerVolume] | None = None
    boot_entries: list[BootEntry] = field(default_factory=list)
    analysis: Analysis | None = None
    issues: list[Issue] = field(default_factory=list)
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class ScanDeps:
    """Semua akses ke sistem, dipisah supaya test bisa menggantinya."""

    runner: Runner
    is_admin: Callable[[], bool]
    read_nvram: Callable[[], NvramSnapshot]
    read_partition_head: Callable[[Partition, int], bytes]
    mount_esp: Callable[[Runner, Partition], AbstractContextManager[MountedEsp]]
    read_gpt_slots: Callable[[int, int], dict[int, str]] | None = None  # None = tidak dibaca (rujukan gptN = tidak diketahui)


def default_deps() -> ScanDeps:
    return ScanDeps(
        runner=ReadOnlyRunner(SubprocessRunner()),
        is_admin=is_admin,
        read_nvram=nvram.read_nvram,
        read_partition_head=lambda part, sector: fsprobe.read_partition_head(
            part.disk_number, part.offset, part.size, sector
        ),
        mount_esp=esp_mod.mounted_esp,
        read_gpt_slots=fsprobe.read_gpt_slots,
    )


def run_scan(deps: ScanDeps | None = None, progress: Callable[[str], None] | None = None) -> ScanReport:
    deps = deps or default_deps()
    say = progress or (lambda message: None)
    admin = deps.is_admin()
    report = ScanReport(
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        version=__version__,
        is_admin=admin,
        windows=platform.platform(),
    )

    def step(name: str, func: Callable[[], None], needs_admin: bool = False) -> None:
        if needs_admin and not admin:
            report.issues.append(Issue(name, "Dilewati: butuh hak Administrator", "info"))
            return
        try:
            func()
        except Exception as exc:  # satu langkah gagal tidak boleh menggagalkan scan
            log.exception("Langkah scan '%s' gagal", name)
            report.issues.append(Issue(name, f"{type(exc).__name__}: {exc}", "error"))

    def load_disks() -> None:
        say("Membaca daftar disk & partisi…")
        report.inventory, raw = disks.load_inventory(deps.runner)
        report.raw["disks.json"] = disks.redact_serials(raw)

    def probe_partitions() -> None:
        say("Membaca header filesystem partisi Linux…")
        complete = True
        for part in report.inventory.partitions:
            if part.category not in _PROBE_CATEGORIES:
                continue
            disk = report.inventory.disk(part.disk_number)
            try:
                data = deps.read_partition_head(part, disk.sector_size if disk else 512)
            except OSError as exc:
                complete = False
                report.issues.append(Issue("fsprobe", f"Gagal membaca {part.id}: {exc}"))
                continue
            info = fsprobe.probe_bytes(data)
            if info is not None:
                report.fsinfo[part.id] = info
        report.fs_probe_complete = complete
        if deps.read_gpt_slots is not None:
            for disk in report.inventory.disks:
                if (disk.partition_style or "").upper() != "GPT":
                    continue
                try:
                    report.gpt_slots[disk.number] = deps.read_gpt_slots(disk.number, disk.sector_size)
                except (OSError, ValueError) as exc:
                    report.issues.append(Issue("fsprobe", f"Tabel GPT disk {disk.number} tidak terbaca: {exc}"))

    def load_firmware() -> None:
        say("Membaca entri boot (bcdedit)…")
        report.firmware, raw = bootentries.load_firmware(deps.runner)
        report.raw["bcdedit-firmware.txt"] = raw

    def load_nvram() -> None:
        say("Membaca NVRAM UEFI…")
        report.nvram = deps.read_nvram()
        # report.nvram tetap utuh (nanti dipakai untuk backup M2); salinan yang bisa dibagikan disamarkan.
        report.raw["nvram.json"] = json.dumps(nvram.redact_raw(report.nvram.raw), indent=2)

    def load_esp() -> None:
        esp_part = report.inventory.esp()
        if esp_part is None:
            report.issues.append(Issue("esp", "Tidak ada EFI System Partition di disk mana pun"))
            return
        say("Me-mount ESP sementara dan mendata isinya…")
        with deps.mount_esp(deps.runner, esp_part) as mounted:
            report.esp = esp_mod.scan_esp(mounted.root, esp_part, mounted)
            say("Membaca konfigurasi bootloader di ESP…")
            slots = report.gpt_slots.get(esp_part.disk_number) or {}
            esp_slot = next((n for n, guid in slots.items() if gpt_types.normalize_guid(guid) == esp_part.guid), None)
            report.esp_refs = bootrefs.collect_esp_refs(mounted.root, report.esp, esp_slot)

    def load_bitlocker() -> None:
        say("Membaca status BitLocker…")
        report.bitlocker, raw = bitlocker_mod.load_bitlocker(deps.runner)
        report.raw["bitlocker.json"] = raw

    step("disks", load_disks)
    if report.inventory is not None:
        step("fsprobe", probe_partitions, needs_admin=True)
    step("bcdedit", load_firmware, needs_admin=True)
    step("nvram", load_nvram, needs_admin=True)
    if report.inventory is not None:
        step("esp", load_esp, needs_admin=True)
    step("bitlocker", load_bitlocker, needs_admin=True)

    say("Menganalisis rujukan…")
    report.boot_entries, merge_notes = bootentries.merge_entries(report.firmware, report.nvram)
    report.analysis = bootrefs.analyze(
        report.inventory,
        report.fsinfo,
        report.fs_probe_complete,
        report.boot_entries,
        report.esp,
        report.esp_refs,
        extra_notes=merge_notes,
        gpt_slots=report.gpt_slots,
    )
    say("Scan selesai.")
    return report


def _json_default(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Tidak bisa diubah ke JSON: {type(value).__name__}")


def report_to_json(report: ScanReport) -> str:
    """Laporan lengkap sebagai JSON, untuk dibagikan: nomor seri disk dan identitas di NVRAM disamarkan."""
    data = dataclasses.asdict(report)
    for disk in (data.get("inventory") or {}).get("disks", []):
        disk["serial"] = "<disamarkan>"
    if report.nvram is not None:
        data["nvram"] = dataclasses.asdict(nvram.snapshot_from_raw(nvram.redact_raw(report.nvram.raw)))
    return json.dumps(data, indent=2, ensure_ascii=False, default=_json_default)


def save_raw(report: ScanReport, directory: Path) -> list[Path]:
    """Simpan output mentah perintah (untuk fixture test). Serial disk sudah disamarkan."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in report.raw.items():
        path = directory / name
        # Output program Windows sudah memakai \r\n; tulis apa adanya supaya tidak jadi \r\r\n.
        path.write_bytes(content.replace("\r\n", "\n").encode("utf-8"))
        written.append(path)
    return written
