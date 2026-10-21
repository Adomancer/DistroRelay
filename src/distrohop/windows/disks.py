"""Inventaris disk & partisi lewat Get-Disk/Get-Partition (read-only)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from . import gpt_types
from .runner import Runner, powershell_args

MIB = 1024 * 1024

DISKS_SCRIPT = r"""# distrohop:disks
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$disks = @(Get-Disk | ForEach-Object {
  [pscustomobject]@{
    Number = $_.Number; Guid = $_.Guid; FriendlyName = $_.FriendlyName
    SerialNumber = $_.SerialNumber; Size = $_.Size; PartitionStyle = [string]$_.PartitionStyle
    LogicalSectorSize = $_.LogicalSectorSize; BusType = [string]$_.BusType
    IsBoot = $_.IsBoot; IsSystem = $_.IsSystem
  }
})
$parts = @(Get-Partition | ForEach-Object {
  $vol = $null
  try { $vol = $_ | Get-Volume -ErrorAction Stop } catch { }
  [pscustomobject]@{
    DiskNumber = $_.DiskNumber; PartitionNumber = $_.PartitionNumber; Guid = $_.Guid
    GptType = $_.GptType; Offset = $_.Offset; Size = $_.Size
    DriveLetter = [string]$_.DriveLetter; AccessPaths = @($_.AccessPaths)
    IsBoot = $_.IsBoot; IsSystem = $_.IsSystem; IsHidden = $_.IsHidden; Type = [string]$_.Type
    FileSystem = $(if ($vol) { $vol.FileSystem } else { $null })
    FileSystemLabel = $(if ($vol) { $vol.FileSystemLabel } else { $null })
  }
})
[pscustomobject]@{ Disks = $disks; Partitions = $parts } | ConvertTo-Json -Depth 4 -Compress
"""


class InventoryError(RuntimeError):
    pass


@dataclass
class Disk:
    number: int
    guid: str | None
    model: str
    serial: str
    size: int
    partition_style: str
    sector_size: int
    bus_type: str
    is_boot: bool
    is_system: bool


@dataclass
class Partition:
    disk_number: int
    number: int
    guid: str | None
    gpt_type: str | None
    type_name: str
    category: str
    offset: int
    size: int
    drive_letter: str | None
    access_paths: list[str]
    is_boot: bool
    is_system: bool
    is_hidden: bool
    windows_type: str
    filesystem: str | None
    label: str | None

    @property
    def id(self) -> str:
        return f"disk{self.disk_number}-p{self.number}"

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def is_linux(self) -> bool:
        return self.category == gpt_types.LINUX

    @property
    def is_protected(self) -> bool:
        return self.category in gpt_types.PROTECTED_CATEGORIES or self.is_boot or self.is_system

    @property
    def mount_points(self) -> list[str]:
        """Access path selain path volume internal (\\\\?\\Volume{...}\\), mis. 'C:\\'."""
        return [p for p in self.access_paths if not p.startswith("\\\\?\\")]


@dataclass
class FreeRegion:
    disk_number: int
    offset: int
    size: int


@dataclass
class DiskInventory:
    disks: list[Disk] = field(default_factory=list)
    partitions: list[Partition] = field(default_factory=list)

    def disk(self, number: int) -> Disk | None:
        return next((d for d in self.disks if d.number == number), None)

    def partitions_on(self, disk_number: int) -> list[Partition]:
        return sorted((p for p in self.partitions if p.disk_number == disk_number), key=lambda p: p.offset)

    def by_guid(self, guid: str | None) -> Partition | None:
        wanted = gpt_types.normalize_guid(guid)
        if wanted is None:
            return None
        return next((p for p in self.partitions if p.guid == wanted), None)

    def esp(self) -> Partition | None:
        """ESP yang dipakai boot (IsSystem), atau ESP pertama kalau tidak ada yang bertanda."""
        esps = [p for p in self.partitions if p.category == "esp"]
        return next((p for p in esps if p.is_system), esps[0] if esps else None)

    def free_regions(self, min_size: int = 16 * MIB) -> list[FreeRegion]:
        """Ruang unallocated (perkiraan, mengabaikan header GPT di awal/akhir disk)."""
        regions: list[FreeRegion] = []
        for disk in self.disks:
            if disk.partition_style.upper() != "GPT":
                continue
            cursor = MIB
            usable_end = disk.size - 34 * disk.sector_size
            for part in self.partitions_on(disk.number):
                if part.offset - cursor >= min_size:
                    regions.append(FreeRegion(disk.number, cursor, part.offset - cursor))
                cursor = max(cursor, part.end)
            if usable_end - cursor >= min_size:
                regions.append(FreeRegion(disk.number, cursor, usable_end - cursor))
        return regions

    def fingerprint(self) -> str:
        """Sidik jari layout disk. Berubah kalau ada partisi yang dibuat/dihapus/digeser."""
        items = sorted(
            (d.number, d.guid or "", d.size) for d in self.disks
        ) + sorted(
            (p.disk_number, p.guid or "", p.gpt_type or "", p.offset, p.size) for p in self.partitions
        )
        return hashlib.sha256(json.dumps(items).encode()).hexdigest()


def _as_list(value) -> list:
    """Normalisasi hasil ConvertTo-Json: None, objek tunggal, atau bentuk {value, Count} jadi list."""
    if value is None:
        return []
    if isinstance(value, dict):
        if "value" in value and "Count" in value:
            return _as_list(value["value"])
        return [value]
    return list(value)


def _drive_letter(value) -> str | None:
    text = (value or "").strip("\x00 :")
    return text.upper() if len(text) == 1 and text.isalpha() else None


def parse_inventory(text: str) -> DiskInventory:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InventoryError(f"Output Get-Disk/Get-Partition bukan JSON yang valid: {exc}") from exc

    disks = [
        Disk(
            number=int(d["Number"]),
            guid=gpt_types.normalize_guid(d.get("Guid")),
            model=(d.get("FriendlyName") or "").strip(),
            serial=(d.get("SerialNumber") or "").strip(),
            size=int(d.get("Size") or 0),
            partition_style=d.get("PartitionStyle") or "",
            sector_size=int(d.get("LogicalSectorSize") or 512),
            bus_type=d.get("BusType") or "",
            is_boot=bool(d.get("IsBoot")),
            is_system=bool(d.get("IsSystem")),
        )
        for d in _as_list(data.get("Disks"))
    ]

    partitions = []
    for p in _as_list(data.get("Partitions")):
        gpt = gpt_types.lookup(p.get("GptType"))
        partitions.append(
            Partition(
                disk_number=int(p["DiskNumber"]),
                number=int(p["PartitionNumber"]),
                guid=gpt_types.normalize_guid(p.get("Guid")),
                gpt_type=gpt_types.normalize_guid(p.get("GptType")),
                type_name=gpt.name,
                category=gpt.category,
                offset=int(p.get("Offset") or 0),
                size=int(p.get("Size") or 0),
                drive_letter=_drive_letter(p.get("DriveLetter")),
                access_paths=[a for a in _as_list(p.get("AccessPaths")) if isinstance(a, str) and a],
                is_boot=bool(p.get("IsBoot")),
                is_system=bool(p.get("IsSystem")),
                is_hidden=bool(p.get("IsHidden")),
                windows_type=p.get("Type") or "",
                filesystem=p.get("FileSystem") or None,
                label=p.get("FileSystemLabel") or None,
            )
        )
    partitions.sort(key=lambda p: (p.disk_number, p.offset))
    return DiskInventory(disks, partitions)


def load_inventory(runner: Runner) -> tuple[DiskInventory, str]:
    result = runner.run(powershell_args(DISKS_SCRIPT))
    if not result.ok:
        raise InventoryError(f"Gagal membaca daftar disk: {result.output()}")
    return parse_inventory(result.stdout), result.stdout


_SERIAL = re.compile(r'("SerialNumber"\s*:\s*)"[^"]*"')


def redact_serials(text: str) -> str:
    """Samarkan nomor seri disk di output mentah sebelum disimpan sebagai fixture."""
    return _SERIAL.sub(r'\1"<disamarkan>"', text)
