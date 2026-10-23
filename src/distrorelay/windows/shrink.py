"""Shrink C: (M6): siapkan ruang kosong untuk distro baru dengan `Resize-Partition` (sama seperti "Shrink Volume"
di Disk Management).

- Info dibaca read-only: partisi C: (GUID, offset, ukuran), batas dari `Get-PartitionSupportedSize`, dan ruang
  kosong volume.
- Rencana ditolak kalau C: jadi lebih kecil dari batas minimum Windows, atau sisa ruang kosong C: kurang dari
  `MIN_FREE_AFTER`.
- Eksekusi hanya lewat script PowerShell yang persis dibuat dari rencana (AllowListRunner): script mencocokkan ulang
  GUID/offset/ukuran C: sebelum `Resize-Partition`. Dicatat di journal, lalu dicek ulang (post-check).
- Ruang kosong muncul tepat sesudah C:. Ini tidak bisa dipakai untuk "extend" partisi lain; installer Linux bisa
  langsung memakainya.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..format import human_size
from .gpt_types import BRACED_GUID
from .journal import Journal, journal_dir
from .runner import AllowListRunner, Runner, powershell_args
from .stopping import StopSignal

GIB = 1024**3
MIB = 1024**2
MIN_SHRINK = 1 * GIB
# Windows butuh ruang kosong untuk update, file hibernate/pagefile, dan pemulihan.
MIN_FREE_AFTER = 20 * GIB
SHRINK_WORD = "KECILKAN"
# Kode keluar script kalau cek ulang di awalnya membatalkan sebelum Resize-Partition (pasti tidak ada yang berubah).
UNCHANGED_EXIT = 3

QUERY_SCRIPT = r"""# distrorelay:shrink-query
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$p = Get-Partition -DriveLetter C
$s = Get-PartitionSupportedSize -DiskNumber $p.DiskNumber -PartitionNumber $p.PartitionNumber
$v = Get-Volume -DriveLetter C
$d = Get-Disk -Number $p.DiskNumber
ConvertTo-Json -Compress -InputObject @{
  DiskNumber = $p.DiskNumber; PartitionNumber = $p.PartitionNumber; DiskGuid = [string]$d.Guid
  Guid = [string]$p.Guid; Offset = $p.Offset; Size = $p.Size
  SizeMin = $s.SizeMin; SizeMax = $s.SizeMax; Free = $v.SizeRemaining; FileSystem = [string]$v.FileSystem
}
"""

# Post-check hanya butuh identitas + ukuran C:; Get-PartitionSupportedSize (bisa ±1 menit) tidak perlu diulang.
AFTER_SCRIPT = r"""# distrorelay:shrink-after
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$p = Get-Partition -DriveLetter C
ConvertTo-Json -Compress -InputObject @{ DiskNumber = $p.DiskNumber; Guid = [string]$p.Guid; Offset = $p.Offset; Size = $p.Size }
"""

SHRINK_TEMPLATE = r"""# distrorelay:shrink-c
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$p = Get-Partition -DriveLetter C
if ([string]$p.Guid -ne '__GUID__' -or $p.DiskNumber -ne __DISK__ -or $p.PartitionNumber -ne __NUMBER__ -or $p.Offset -ne __OFFSET__ -or $p.Size -ne __SIZE__) {
  [Console]::Error.WriteLine('Partisi C: berubah sejak dicek; dibatalkan tanpa mengubah apa pun')
  exit __UNCHANGED_EXIT__
}
# Ruang kosong dan batas Windows bisa berubah selama menunggu konfirmasi: cek ulang tepat sebelum mengecilkan.
$s = Get-PartitionSupportedSize -DiskNumber __DISK__ -PartitionNumber __NUMBER__
$v = Get-Volume -DriveLetter C
if (__NEW_SIZE__ -lt $s.SizeMin -or ($v.SizeRemaining - __SHRINK_BY__) -lt __MIN_FREE__) {
  [Console]::Error.WriteLine('Ruang kosong C: atau batas Windows berubah sejak dicek; dibatalkan tanpa mengubah apa pun')
  exit __UNCHANGED_EXIT__
}
Resize-Partition -DiskNumber __DISK__ -PartitionNumber __NUMBER__ -Size __NEW_SIZE__
$after = Get-Partition -DiskNumber __DISK__ -PartitionNumber __NUMBER__
ConvertTo-Json -Compress -InputObject @{ Guid = [string]$after.Guid; Offset = $after.Offset; Size = $after.Size }
"""



class ShrinkError(RuntimeError):
    pass


@dataclass(frozen=True)
class CDrive:
    disk_number: int
    partition_number: int
    disk_guid: str
    guid: str
    offset: int
    size: int
    size_min: int
    size_max: int
    free: int
    filesystem: str

    @property
    def max_shrink(self) -> int:
        """Pengecilan terbesar yang diizinkan: batas Windows dan sisa ruang kosong minimum."""
        by_windows = self.size - self.size_min
        by_free = self.free - MIN_FREE_AFTER
        return max(0, min(by_windows, by_free) // MIB * MIB)


def parse_query(text: str) -> CDrive:
    try:
        data = json.loads(text)
        drive = CDrive(
            disk_number=int(data["DiskNumber"]),
            partition_number=int(data["PartitionNumber"]),
            disk_guid=str(data.get("DiskGuid") or ""),
            guid=str(data["Guid"]),
            offset=int(data["Offset"]),
            size=int(data["Size"]),
            size_min=int(data["SizeMin"]),
            size_max=int(data["SizeMax"]),
            free=int(data["Free"]),
            filesystem=str(data.get("FileSystem") or ""),
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ShrinkError(f"Info partisi C: tidak terbaca: {exc}") from exc
    if not BRACED_GUID.fullmatch(drive.guid):
        raise ShrinkError(f"GUID partisi C: tidak dikenali ({drive.guid!r}); disk bukan GPT?")
    return drive


def read_c_drive(runner: Runner) -> CDrive:
    """Read-only (lewat ReadOnlyRunner atau AllowListRunner)."""
    result = runner.run(powershell_args(QUERY_SCRIPT), timeout=600)
    if not result.ok:
        raise ShrinkError(f"Gagal membaca partisi C:: {result.output()}")
    return parse_query(result.stdout)


@dataclass(frozen=True)
class CPartition:
    disk_number: int
    guid: str
    offset: int
    size: int


def read_c_partition(runner: Runner) -> CPartition:
    """Identitas + ukuran C: saja, untuk post-check (read-only, cepat)."""
    result = runner.run(powershell_args(AFTER_SCRIPT))
    if not result.ok:
        raise ShrinkError(f"Gagal membaca partisi C:: {result.output()}")
    try:
        data = json.loads(result.stdout)
        return CPartition(int(data["DiskNumber"]), str(data["Guid"]), int(data["Offset"]), int(data["Size"]))
    except (ValueError, TypeError, KeyError) as exc:
        raise ShrinkError(f"Info partisi C: tidak terbaca: {exc}") from exc


@dataclass
class ShrinkPlan:
    drive: CDrive
    shrink_by: int
    problems: list[str] = field(default_factory=list)

    @property
    def new_size(self) -> int:
        return self.drive.size - self.shrink_by

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def script(self) -> str:
        d = self.drive
        values = {
            "__GUID__": d.guid,
            "__DISK__": str(d.disk_number),
            "__NUMBER__": str(d.partition_number),
            "__OFFSET__": str(d.offset),
            "__NEW_SIZE__": str(self.new_size),
            "__SHRINK_BY__": str(self.shrink_by),
            "__MIN_FREE__": str(MIN_FREE_AFTER),
            "__UNCHANGED_EXIT__": str(UNCHANGED_EXIT),
            "__SIZE__": str(d.size),
        }
        script = SHRINK_TEMPLATE
        for key, value in values.items():
            script = script.replace(key, value)
        return script

    def summary(self) -> list[str]:
        d = self.drive
        return [
            f"C: sekarang {human_size(d.size)} (kosong {human_size(d.free)}), disk {d.disk_number} partisi {d.partition_number}",
            f"Dikecilkan {human_size(self.shrink_by)} → C: jadi {human_size(self.new_size)}, "
            f"kosong tersisa ±{human_size(d.free - self.shrink_by)}",
            f"Ruang kosong {human_size(self.shrink_by)} muncul tepat sesudah C: untuk installer Linux",
        ]


def plan_shrink(drive: CDrive, shrink_by: int) -> ShrinkPlan:
    shrink_by = -(-shrink_by // MIB) * MIB  # bulatkan ke atas ke kelipatan 1 MiB
    plan = ShrinkPlan(drive, shrink_by)
    if drive.filesystem.upper() != "NTFS":
        plan.problems.append(f"C: berformat {drive.filesystem or '?'}, bukan NTFS")
    if shrink_by < MIN_SHRINK:
        plan.problems.append(f"minimal {human_size(MIN_SHRINK)}")
    if plan.new_size < drive.size_min:
        plan.problems.append(
            f"Windows hanya mengizinkan C: dikecilkan sampai {human_size(drive.size_min)} "
            f"(maks. {human_size(drive.size - drive.size_min)}); ada file sistem yang tidak bisa dipindah"
        )
    if drive.free - shrink_by < MIN_FREE_AFTER:
        plan.problems.append(
            f"sisa ruang kosong C: harus tetap ≥ {human_size(MIN_FREE_AFTER)} "
            f"(sekarang kosong {human_size(drive.free)})"
        )
    return plan


@dataclass
class ShrinkResult:
    ok: bool
    message: str
    after: CPartition | None = None
    journal: Path | None = None


def execute(
    plan: ShrinkPlan, inner: Runner, *, journal_root: Path | None = None, stop: StopSignal | None = None
) -> ShrinkResult:
    """Jalankan rencana yang sudah dikonfirmasi, lalu baca ulang C:.

    Hanya satu langkah. Selama `Resize-Partition` berjalan, Ctrl+C / tutup jendela ditahan oleh `console_stop_guard`
    pemanggil (langkahnya diselesaikan); `stop` ditandai selesai setelah journal ditutup.
    """
    if not plan.ok:
        raise ShrinkError("Rencana tidak valid: " + "; ".join(plan.problems))
    stop = stop or StopSignal()
    try:
        if stop.requested():
            return ShrinkResult(False, f"{stop.reason} sebelum mulai; tidak ada yang diubah")
        stop.step_started()
        try:
            return _execute(plan, inner, journal_root)
        finally:
            stop.step_finished()
    finally:
        stop.mark_done()


def _execute(plan: ShrinkPlan, inner: Runner, journal_root: Path | None) -> ShrinkResult:
    runner = AllowListRunner(inner, scripts=frozenset({plan.script}))
    session = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = plan.drive
    journal = Journal.start(
        journal_root or journal_dir(),
        session,
        {"kind": "shrink-c", "guid": d.guid, "size": d.size, "new_size": plan.new_size},
    )
    journal.record(1, "shrink-c", "C:", "started")
    try:
        result = runner.run(powershell_args(plan.script), timeout=1800)
    except subprocess.TimeoutExpired:
        message = ("Resize-Partition tidak selesai dalam 30 menit; STATUS TIDAK DIKETAHUI. Jangan matikan laptop; "
                   "cek ukuran C: di Disk Management, lalu jalankan `shrink-c` (tanpa --size) untuk membaca ulang")
        journal.record(1, "shrink-c", "C:", "failed", message)
        journal.finish(False, message)
        return ShrinkResult(False, message, journal=journal.path)
    if not result.ok:
        journal.record(1, "shrink-c", "C:", "failed", result.output()[:500])
        # Pengecekan di awal script membatalkan sebelum Resize-Partition: pasti tidak ada yang berubah.
        unchanged = result.returncode == UNCHANGED_EXIT
        journal.finish(False, "Dibatalkan tanpa mengubah apa pun" if unchanged else "Resize-Partition gagal",
                       changed=False if unchanged else None)
        return ShrinkResult(False, f"Resize-Partition gagal: {result.output()}", journal=journal.path)
    journal.record(1, "shrink-c", "C:", "done")

    failed = []
    try:
        after = read_c_partition(runner)
    except ShrinkError as exc:
        after = None
        failed.append(str(exc))
    if after is not None:
        if (after.guid, after.offset, after.disk_number) != (d.guid, d.offset, d.disk_number):
            failed.append("identitas partisi C: berubah")
        if after.size != plan.new_size:
            failed.append(f"ukuran C: {after.size}, seharusnya {plan.new_size}")
    journal.post_check(not failed, failed)
    journal.finish(not failed, "; ".join(failed))
    if failed:
        return ShrinkResult(False, "Post-check: " + "; ".join(failed), after, journal.path)
    return ShrinkResult(True, f"C: sekarang {human_size(after.size)}", after, journal.path)
