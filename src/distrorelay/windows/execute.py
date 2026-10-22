"""Eksekusi rencana uninstall (M2b). INI satu-satunya modul DistroRelay yang menghapus sesuatu.

Pengaman berlapis:
1. Gerbang (`check_gate`): preflight lolos, konfirmasi manual sudah diberikan, dan backup lokal + salinan di
   drive eksternal diverifikasi ulang serta masih mencerminkan kondisi ESP & disk saat ini.
2. Runner khusus tahap ini: selain read-only, hanya `bcdedit /set {fwbootmgr} displayorder {bootmgr} /addfirst`,
   `bcdedit /delete` untuk GUID yang ada di rencana, dan script PowerShell yang persis dibuat dari data rencana
   (hapus partisi tertentu, perluas C:). Cmdlet pengubah partisi tidak diizinkan di script lain.
3. Penjaga path ESP: EFI\\Microsoft tidak pernah disentuh; di EFI\\Boot hanya bootx64.efi yang boleh diganti,
   dan hanya lewat langkah pengembalian fallback.
4. Setiap langkah dicek ulang sesaat sebelum dijalankan (entri masih sama, tanda tangan masih valid, isi folder
   masih sama dengan backup) dan hasilnya dicek sesudahnya.
5. Gagal = berhenti. Tidak ada langkah yang dicoba ulang dengan cara yang lebih "kuat".
6. Journal mencatat setiap langkah sebelum & sesudah dijalankan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..format import human_size
from . import flush
from .backup import sha256_file, valid_backup_id, verify_backup_dir
from .bootentries import BOOTMGR_ID, FWBOOTMGR_ID, load_firmware, merge_entries
from .disks import DiskInventory, parse_inventory
from .esp import norm_path
from .journal import Journal
from .nvram import NvramSnapshot
from .preflight import FAIL, PreflightResult
from .runner import AllowListRunner, Runner, powershell_args
from .scanner import ScanReport
from .signature import Signature, check_signature
from .stopping import StopSignal
from .uncached import sha256_uncached
from .uninstall_plan import VENDOR_DIRS, PlannedAction, UninstallPlan, partition_refusal

log = logging.getLogger(__name__)

# Simulasi crash untuk skenario VM 7. Hanya aktif kalau DISTRORELAY_DEV=1 juga di-set.
DEV_FLAG_ENV = "DISTRORELAY_DEV"
FAIL_AFTER_ENV = "DISTRORELAY_FAIL_AFTER"

SET_WINDOWS_FIRST = ["bcdedit", "/set", "{fwbootmgr}", "displayorder", "{bootmgr}", "/addfirst"]
BOOTMGFW = "EFI\\Microsoft\\Boot\\bootmgfw.efi"
FALLBACK = "EFI\\Boot\\bootx64.efi"
WELL_KNOWN_IDS = frozenset(
    {
        BOOTMGR_ID,
        FWBOOTMGR_ID,
        "{bootmgr}",
        "{fwbootmgr}",
        "{current}",
        "{default}",
        "{memdiag}",
        "{ntldr}",
        "{globalsettings}",
        "{badmemory}",
        "{dbgsettings}",
        "{emssettings}",
        "{bootloadersettings}",
        "{resumeloadersettings}",
        "{hypervisorsettings}",
        "{ramdiskoptions}",
        "{7ea2e1ac-2e61-4728-aaa3-896d9d0a9f0e}",  # {globalsettings}
    }
)

BITLOCKER_SUSPEND_SCRIPT = r"""# distrorelay:suspend-bitlocker
$ErrorActionPreference = 'Stop'
$null = Suspend-BitLocker -MountPoint 'C:' -RebootCount 1
"""

_GUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ExecutionError(RuntimeError):
    pass


def _guid(value) -> str:
    """GUID dari data rencana, divalidasi sebelum ditempel ke script PowerShell."""
    text = str(value or "").strip("{}").lower()
    if not _GUID_TEXT.match(text):
        raise ExecutionError(f"GUID tidak valid di rencana: {value!r}")
    return "{" + text + "}"


def partition_delete_script(data: dict) -> str:
    """Script hapus SATU partisi. Partisi dicari lewat GUID, lalu disk, offset, ukuran, tipe, dan status pakainya
    dicocokkan ulang di proses PowerShell yang sama, tepat sebelum Remove-Partition."""
    guid, disk_guid, gpt_type = _guid(data["guid"]), _guid(data["disk_guid"]), _guid(data["gpt_type"])
    offset, size = int(data["offset"]), int(data["size"])
    return f"""# distrorelay:delete-partition
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$p = @(Get-Partition | Where-Object {{ $_.Guid -eq '{guid}' }})
if ($p.Count -ne 1) {{ throw 'Partisi {guid} tidak ditemukan' }}
$p = $p[0]
$d = Get-Disk -Number $p.DiskNumber
if ($d.Guid -ne '{disk_guid}') {{ throw 'Partisi ada di disk lain dari yang direncanakan' }}
if ($p.Offset -ne {offset} -or $p.Size -ne {size} -or $p.GptType -ne '{gpt_type}') {{ throw 'Offset/ukuran/tipe partisi berubah' }}
$letter = ([string]$p.DriveLetter).Trim([char]0)
$paths = @($p.AccessPaths | Where-Object {{ $_ -and -not $_.StartsWith('\\\\?\\') }})
if ($p.IsBoot -or $p.IsSystem -or $letter -or $paths.Count) {{ throw 'Partisi sedang dipakai Windows (boot/system/drive letter)' }}
Remove-Partition -DiskNumber $p.DiskNumber -PartitionNumber $p.PartitionNumber -Confirm:$false
"""


def extend_c_script(data: dict) -> str:
    """Script perluas C: ke ukuran maksimum yang didukung (ruang kosong tepat sesudahnya)."""
    guid, offset, size = _guid(data["guid"]), int(data["offset"]), int(data["size"])
    return f"""# distrorelay:extend-c
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$p = Get-Partition -DriveLetter C
if ($p.Guid -ne '{guid}') {{ throw 'C: bukan partisi yang direncanakan' }}
if ($p.Offset -ne {offset} -or $p.Size -ne {size}) {{ throw 'Offset/ukuran C: berubah sejak rencana dibuat' }}
$max = (Get-PartitionSupportedSize -DriveLetter C).SizeMax
if ($max -le $p.Size) {{ throw 'Tidak ada ruang kosong tepat sesudah C:' }}
Resize-Partition -DriveLetter C -Size $max
"""


def planned_scripts(plan: UninstallPlan) -> frozenset[str]:
    scripts = set()
    for action in plan.actions:
        if action.kind == "delete-partition":
            scripts.add(partition_delete_script(action.data))
        elif action.kind == "extend-c":
            scripts.add(extend_c_script(action.data))
    return frozenset(scripts)


class SimulatedCrash(RuntimeError):
    """Crash buatan untuk test VM: langkah sudah dijalankan tapi belum dicatat selesai di journal."""


def _exe(args: Sequence[str]) -> str:
    return Path(args[0]).stem.lower() if args else ""


def execution_runner(inner: Runner, plan: UninstallPlan) -> AllowListRunner:
    """Runner tahap eksekusi: read-only + perintah pengubah yang persis ada di rencana."""
    allowed_ids = {
        a.data["bcd_id"].lower() for a in plan.actions if a.kind == "delete-boot-entry" and a.data.get("bcd_id")
    } - WELL_KNOWN_IDS

    def set_windows_first(args: Sequence[str]) -> bool:
        return _exe(args) == "bcdedit" and [a.lower() for a in args[1:]] == SET_WINDOWS_FIRST[1:]

    def delete_planned_entry(args: Sequence[str]) -> bool:
        return _exe(args) == "bcdedit" and len(args) == 3 and args[1].lower() == "/delete" and args[2].lower() in allowed_ids

    return AllowListRunner(
        inner,
        extra_cmdlets=frozenset({"suspend-bitlocker"}),
        rules=(set_windows_first, delete_planned_entry),
        scripts=planned_scripts(plan),
    )


def safe_esp_path(root: Path, rel: str, *, fallback_target: bool = False) -> Path:
    """Path absolut di dalam ESP yang boleh diubah, atau ExecutionError."""
    norm = norm_path(rel)
    low = norm.lower()
    if not norm or low in ("efi", "efi\\boot", "efi\\microsoft") or low.startswith("efi\\microsoft\\"):
        raise ExecutionError(f"{rel}: dilindungi, tidak boleh diubah")
    if any(low == d or low.startswith(d + "\\") for d in VENDOR_DIRS):
        raise ExecutionError(f"{rel}: folder alat pabrikan, dilindungi")
    if low.startswith("efi\\boot\\") and not (fallback_target and low == FALLBACK.lower()):
        raise ExecutionError(f"{rel}: file fallback firmware hanya boleh diganti lewat langkah pengembalian fallback")
    base = root.resolve()
    target = (base / Path(*norm.split("\\"))).resolve()
    if target == base or base not in target.parents:
        raise ExecutionError(f"{rel}: berada di luar ESP")
    return target


def _rel(root: Path, path: Path) -> str:
    return "\\".join(path.relative_to(root.resolve()).parts)


def _files_under(target: Path) -> list[Path]:
    return [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())


def _check_covered_by_backup(root: Path, target: Path, hashes: dict[str, str]) -> int:
    """Pastikan setiap file yang akan dihapus ada di backup dengan isi yang sama persis."""
    files = _files_under(target)
    for path in files:
        rel = _rel(root, path)
        expected = hashes.get(rel.lower())
        if expected is None:
            raise ExecutionError(f"{rel} tidak ada di backup; buat backup baru dulu")
        if sha256_file(path) != expected:
            raise ExecutionError(f"{rel} berubah sejak backup dibuat; buat backup baru dulu")
    return len(files)


def _make_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _remove(target: Path) -> None:
    """Hapus file/folder, termasuk file ber-atribut read-only (umum di FAT)."""
    if target.is_dir():
        for path in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            _make_writable(path)
            path.rmdir() if path.is_dir() else path.unlink()
        _make_writable(target)
        target.rmdir()
    else:
        _make_writable(target)
        target.unlink()


@dataclass
class StepResult:
    step: int
    kind: str
    title: str
    ok: bool
    message: str


@dataclass
class ExecutionResult:
    session: str
    journal_path: str
    steps: list[StepResult] = field(default_factory=list)
    completed: bool = False
    stopped: str | None = None
    stopped_by_user: bool = False


@dataclass
class _Context:
    runner: Runner
    esp_root: Path
    hashes: dict[str, str]
    signature_of: Callable[[Runner, Path], Signature]
    read_nvram: Callable[[], NvramSnapshot] | None = None
    flush_bcd: Callable[[], None] = flush.flush_bcd
    flush_volume: Callable[[Path], None] = flush.flush_volume
    load_inventory: Callable[[Runner], DiskInventory] | None = None
    # Layout yang diharapkan (DiskInventory.layout()): hasil scan, dikurangi/diubah oleh langkah partisi sesi ini.
    expected_layout: set | None = None

    def inventory(self) -> DiskInventory:
        if self.load_inventory is None or self.expected_layout is None:
            raise ExecutionError("Inventaris disk tidak bisa dibaca ulang, jadi partisi tidak bisa dipastikan")
        return self.load_inventory(self.runner)

    def check_layout(self, inventory: DiskInventory, expected: set, when: str) -> None:
        """Layout harus sama persis (GUID, tipe, offset, ukuran). Beda sedikit = berhenti."""
        current = inventory.layout()
        if current == expected:
            return
        added = sorted(current - expected)
        gone = sorted(expected - current)
        detail = "; ".join(
            [f"baru/berubah: {p[1]} offset {p[3]:,} ukuran {p[4]:,}" for p in added[:2]]
            + [f"hilang/berubah: {p[1]} offset {p[3]:,} ukuran {p[4]:,}" for p in gone[:2]]
        )
        raise ExecutionError(
            f"Layout disk {when} tidak sama dengan yang diharapkan ({detail}). Langkah partisi dibatalkan. "
            "Kalau drive eksternal dicabut/dicolok selama proses, colok/cabut kembali lalu scan ulang"
        )

    def durable_bcd(self) -> None:
        try:
            self.flush_bcd()
        except flush.FlushError as exc:
            raise ExecutionError(str(exc)) from exc

    def durable_esp(self) -> None:
        try:
            self.flush_volume(self.esp_root)
        except flush.FlushError as exc:
            raise ExecutionError(str(exc)) from exc


def _suspend_bitlocker(action: PlannedAction, ctx: _Context) -> str:
    result = ctx.runner.run(powershell_args(BITLOCKER_SUSPEND_SCRIPT))
    if not result.ok:
        raise ExecutionError(f"Suspend-BitLocker gagal: {result.output() or result.returncode}")
    return "BitLocker ditangguhkan untuk 1 kali restart"


def _set_windows_first(action: PlannedAction, ctx: _Context) -> str:
    result = ctx.runner.run(SET_WINDOWS_FIRST)
    if not result.ok:
        raise ExecutionError(f"bcdedit gagal: {result.output() or result.returncode}")
    config, _ = load_firmware(ctx.runner)
    if not config.display_order or config.display_order[0] != BOOTMGR_ID:
        raise ExecutionError("Setelah perintah dijalankan, Windows Boot Manager belum juga di urutan pertama")
    ctx.durable_bcd()
    return "Windows Boot Manager sekarang urutan pertama (dicek ulang, BCD ditulis ke disk)"


def _restore_fallback(action: PlannedAction, ctx: _Context) -> str:
    source = ctx.esp_root / Path(*BOOTMGFW.split("\\"))
    target = safe_esp_path(ctx.esp_root, action.data.get("target", FALLBACK), fallback_target=True)
    if not source.is_file():
        raise ExecutionError(f"{BOOTMGFW} tidak ditemukan")
    signature = ctx.signature_of(ctx.runner, source)
    if not signature.is_microsoft:
        raise ExecutionError(f"Tanda tangan {BOOTMGFW} tidak valid: {signature.describe()}")
    if target.exists():
        rel = _rel(ctx.esp_root, target)
        if ctx.hashes.get(rel.lower()) != sha256_file(target):
            raise ExecutionError(f"{rel} berubah sejak backup dibuat; buat backup baru dulu")
    source_hash = sha256_file(source)
    temporary = target.with_name(target.name + ".distrorelay-baru")
    shutil.copyfile(source, temporary)
    if sha256_file(temporary) != source_hash:
        temporary.unlink()
        raise ExecutionError("Salinan sementara bootmgfw.efi tidak sama dengan aslinya")
    if target.exists():
        _make_writable(target)
    os.replace(temporary, target)
    if sha256_file(target) != source_hash:
        raise ExecutionError(f"{FALLBACK} tidak sama dengan bootmgfw.efi setelah disalin")
    ctx.durable_esp()
    return f"{FALLBACK} sekarang identik dengan bootmgfw.efi (sha256 {source_hash[:16]}…, ditulis ke disk)"


def _current_entries(ctx: _Context):
    if ctx.read_nvram is None:
        raise ExecutionError("NVRAM tidak bisa dibaca ulang, jadi entri tidak bisa dipastikan sebelum dihapus")
    config, _ = load_firmware(ctx.runner)
    entries, _ = merge_entries(config, ctx.read_nvram())
    return config, entries


def _delete_boot_entry(action: PlannedAction, ctx: _Context) -> str:
    bcd_id = (action.data.get("bcd_id") or "").lower()
    if not bcd_id or bcd_id in WELL_KNOWN_IDS:
        raise ExecutionError(f"GUID entri tidak boleh dihapus: {bcd_id or '(kosong)'}")
    number = action.data.get("nvram_number")
    if number is None:
        raise ExecutionError("Rencana tidak mencatat nomor Boot#### entri ini; susun ulang rencananya")
    config, entries = _current_entries(ctx)
    if config.find(bcd_id) is None:
        raise ExecutionError("Entri tidak ditemukan lagi; kondisi berubah sejak rencana dibuat")
    entry = next((e for e in entries if (e.bcd_id or "").lower() == bcd_id), None)
    label = f"Boot{number:04X}"
    if entry is None or entry.nvram_number != number:
        now = entry.label if entry else "tidak ada"
        raise ExecutionError(f"Objek {bcd_id} sekarang dipasangkan dengan {now}, bukan {label}; kondisi berubah")
    if entry.is_windows_boot_manager:
        raise ExecutionError("Entri ini adalah Windows Boot Manager")
    if not entry.confirmed:
        why = "; ".join(entry.conflicts) or f"hanya terlihat di {' + '.join(entry.sources)}"
        raise ExecutionError(f"Entri tidak terkonfirmasi di NVRAM + bcdedit: {why}")
    if not _same_text(entry.description, action.data.get("description")) or not _same_text(
        entry.path, action.data.get("path")
    ):
        raise ExecutionError(f"Entri berubah sejak rencana dibuat: sekarang '{entry.description}' → {entry.path}")
    others = {e.nvram_number for e in entries if e.nvram_number is not None and e.nvram_number != number}

    result = ctx.runner.run(["bcdedit", "/delete", bcd_id])
    if not result.ok:
        raise ExecutionError(f"bcdedit /delete gagal: {result.output() or result.returncode}")
    config, entries = _current_entries(ctx)
    if config.find(bcd_id) is not None:
        raise ExecutionError("Entri masih ada setelah dihapus")
    remaining = {e.nvram_number for e in entries if e.nvram_number is not None}
    if number in remaining:
        raise ExecutionError(f"Objek BCD terhapus, tapi {label} masih ada di NVRAM")
    gone = sorted(others - remaining)
    if gone:
        raise ExecutionError("Entri lain ikut hilang dari NVRAM: " + ", ".join(f"Boot{n:04X}" for n in gone))
    # Tanpa ini, objek yang sudah dihapus bisa kembali dari BCD lama kalau listrik putus, lalu Windows
    # membuat ulang entri NVRAM-nya di urutan pertama.
    ctx.durable_bcd()
    return f"Entri '{entry.description}' ({label}, {bcd_id}) dihapus (dicek ulang di NVRAM + bcdedit, BCD ditulis ke disk)"


def _same_text(a: str | None, b: str | None) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def _delete_esp_item(action: PlannedAction, ctx: _Context) -> str:
    rel = action.data.get("path") or ""
    target = safe_esp_path(ctx.esp_root, rel)
    if not target.exists():
        raise ExecutionError(f"{rel} tidak ditemukan")
    count = _check_covered_by_backup(ctx.esp_root, target, ctx.hashes)
    _remove(target)
    if target.exists():
        raise ExecutionError(f"{rel} masih ada setelah dihapus")
    ctx.durable_esp()
    return f"{rel} dihapus ({count} file, semuanya ada di backup; ditulis ke disk)"


def _delete_partition(action: PlannedAction, ctx: _Context) -> str:
    data = action.data
    fresh = ctx.inventory()
    ctx.check_layout(fresh, ctx.expected_layout, "sebelum partisi dihapus")
    part = fresh.by_guid(data.get("guid"))
    refusal = partition_refusal(part, fresh)
    if refusal:
        raise ExecutionError(f"{data.get('id')}: {refusal}")
    disk = fresh.disk(part.disk_number)
    planned = (data.get("disk_guid"), data.get("offset"), data.get("size"), data.get("gpt_type"))
    if (disk.guid, part.offset, part.size, part.gpt_type) != planned:
        raise ExecutionError(f"{data.get('id')}: disk/offset/ukuran/tipe berbeda dari rencana")

    result = ctx.runner.run(powershell_args(partition_delete_script(data)))
    if not result.ok:
        raise ExecutionError(f"Remove-Partition gagal: {result.output() or result.returncode}")
    after = ctx.inventory()
    if after.by_guid(data["guid"]) is not None:
        raise ExecutionError("Partisi masih ada setelah dihapus")
    expected = {item for item in ctx.expected_layout if item[1] != part.guid}
    ctx.check_layout(after, expected, "sesudah partisi dihapus")
    ctx.expected_layout = expected
    return (
        f"Partisi {part.id} ({human_size(part.size)}, {part.type_name}) dihapus dari tabel GPT sebagai "
        f"partisi nomor {part.number} di disk {part.disk_number}; layout lainnya tidak berubah. Datanya belum "
        "ditimpa, jadi bisa dipulihkan selama ruangnya belum dipakai"
    )


def _extend_c(action: PlannedAction, ctx: _Context) -> str:
    data = action.data
    fresh = ctx.inventory()
    ctx.check_layout(fresh, ctx.expected_layout, "sebelum C: diperluas")
    c = fresh.by_guid(data.get("guid"))
    if c is None or c.drive_letter != "C" or (c.offset, c.size) != (data.get("offset"), data.get("size")):
        raise ExecutionError("C: berbeda dari rencana (GUID/offset/ukuran)")
    following = fresh.next_partition(c)
    if following is not None and following.offset - c.end < 1024 * 1024:
        raise ExecutionError(f"Tidak ada ruang kosong tepat sesudah C: (langsung disusul {following.id})")

    result = ctx.runner.run(powershell_args(extend_c_script(data)))
    if not result.ok:
        raise ExecutionError(f"Resize-Partition gagal: {result.output() or result.returncode}")
    after = ctx.inventory()
    grown = after.by_guid(c.guid)
    if grown is None or grown.size <= c.size or grown.offset != c.offset:
        raise ExecutionError("C: tidak bertambah besar setelah Resize-Partition")
    expected = {item for item in ctx.expected_layout if item[1] != c.guid}
    expected |= {item for item in after.layout() if item[1] == c.guid}
    ctx.check_layout(after, expected, "sesudah C: diperluas")
    ctx.expected_layout = expected
    return f"C: diperluas dari {human_size(c.size)} ke {human_size(grown.size)}; partisi lain tidak berubah"


_HANDLERS: dict[str, Callable[[PlannedAction, _Context], str]] = {
    "suspend-bitlocker": _suspend_bitlocker,
    "set-windows-first": _set_windows_first,
    "restore-fallback": _restore_fallback,
    "delete-boot-entry": _delete_boot_entry,
    "delete-esp-item": _delete_esp_item,
    "delete-partition": _delete_partition,
    "extend-c": _extend_c,
}


def manifest_hashes(manifest: dict) -> dict[str, str]:
    return {e["path"].lower(): e["sha256"] for e in manifest.get("esp_files", [])}


def execute_plan(
    plan: UninstallPlan,
    *,
    runner: Runner,
    esp_root: Path,
    manifest: dict,
    journal: Journal,
    suspend_bitlocker: bool = False,
    signature_of: Callable[[Runner, Path], Signature] = check_signature,
    read_nvram: Callable[[], NvramSnapshot] | None = None,
    flush_bcd: Callable[[], None] = flush.flush_bcd,
    flush_volume: Callable[[Path], None] = flush.flush_volume,
    progress: Callable[[str], None] | None = None,
    fail_after: str | None = None,
    stop: StopSignal | None = None,
    inventory: DiskInventory | None = None,
    load_inventory: Callable[[Runner], DiskInventory] | None = None,
) -> ExecutionResult:
    """`inventory` = layout saat scan (acuan langkah partisi); `load_inventory` membaca ulang layout saat itu."""
    stop = stop or StopSignal()
    try:
        return _execute_steps(
            plan, runner=runner, esp_root=esp_root, manifest=manifest, journal=journal,
            suspend_bitlocker=suspend_bitlocker, signature_of=signature_of, read_nvram=read_nvram,
            flush_bcd=flush_bcd, flush_volume=flush_volume, progress=progress, fail_after=fail_after, stop=stop,
            inventory=inventory, load_inventory=load_inventory,
        )
    finally:
        stop.mark_done()  # penangan "jendela ditutup" menunggu titik ini


def _execute_steps(
    plan: UninstallPlan,
    *,
    runner: Runner,
    esp_root: Path,
    manifest: dict,
    journal: Journal,
    suspend_bitlocker: bool,
    signature_of: Callable[[Runner, Path], Signature],
    read_nvram: Callable[[], NvramSnapshot] | None,
    flush_bcd: Callable[[], None],
    flush_volume: Callable[[Path], None],
    progress: Callable[[str], None] | None,
    fail_after: str | None,
    stop: StopSignal,
    inventory: DiskInventory | None,
    load_inventory: Callable[[Runner], DiskInventory] | None,
) -> ExecutionResult:
    say = progress or (lambda message: None)
    actions = list(plan.actions)
    if suspend_bitlocker:
        actions.insert(
            0,
            PlannedAction(
                "suspend-bitlocker",
                "Tangguhkan BitLocker untuk 1 kali restart",
                ["Suspend-BitLocker -MountPoint C: -RebootCount 1"],
            ),
        )
    ctx = _Context(
        runner, Path(esp_root), manifest_hashes(manifest), signature_of, read_nvram, flush_bcd, flush_volume,
        load_inventory=load_inventory, expected_layout=inventory.layout() if inventory is not None else None,
    )
    result = ExecutionResult(session=journal.path.stem, journal_path=str(journal.path))

    for number, action in enumerate(actions, 1):
        # Permintaan berhenti (Ctrl+C, jendela ditutup) hanya berlaku di antara langkah. Urutan aksinya dirancang
        # supaya setiap titik di antara langkah tetap bisa boot ke Windows.
        if stop.requested():
            done = number - 1
            result.stopped = (
                f"{stop.reason} setelah langkah {done} dari {len(actions)}; langkah {number} dan sesudahnya belum dijalankan"
                if done else f"{stop.reason} sebelum langkah pertama; tidak ada yang diubah"
            )
            result.stopped_by_user = True
            journal.finish(False, result.stopped, changed=False if not done else None)
            return result
        say(f"[{number}/{len(actions)}] {action.title}…")
        handler = _HANDLERS.get(action.kind)
        stop.step_started()
        try:
            journal.record(number, action.kind, action.ref, "started")
            try:
                if handler is None:
                    raise ExecutionError(f"Jenis aksi tidak dikenal: {action.kind}")
                message = handler(action, ctx)
            except Exception as exc:  # gagal = berhenti, apa pun penyebabnya
                log.exception("Langkah %s (%s) gagal", number, action.kind)
                text = f"{type(exc).__name__}: {exc}" if not isinstance(exc, ExecutionError) else str(exc)
                if isinstance(exc, subprocess.TimeoutExpired):
                    text = (f"perintah tidak selesai dalam {exc.timeout:.0f} detik; STATUS TIDAK DIKETAHUI (mungkin "
                            "sudah jalan sebagian). Scan ulang dan periksa sebelum melanjutkan")
                journal.record(number, action.kind, action.ref, "failed", text)
                result.steps.append(StepResult(number, action.kind, action.title, False, text))
                result.stopped = f"Berhenti di langkah {number} ({action.title}): {text}"
                journal.finish(False, result.stopped)
                return result
            if fail_after and fail_after in (str(number), action.kind):
                raise SimulatedCrash(f"Simulasi crash setelah langkah {number} ({action.kind}), sebelum dicatat selesai")
            journal.record(number, action.kind, action.ref, "done", message)
            result.steps.append(StepResult(number, action.kind, action.title, True, message))
        finally:
            stop.step_finished()

    result.completed = True
    journal.finish(True, "Semua langkah selesai")
    return result


# --- Gerbang sebelum eksekusi -----------------------------------------------


@dataclass
class GateResult:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    local_dir: Path | None = None
    external_dir: Path | None = None
    manifest: dict | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def check_gate(
    plan: UninstallPlan,
    report: ScanReport,
    preflight: PreflightResult,
    *,
    backup_id: str,
    local_root: Path,
    external_roots: Iterable[Path],
    confirmed: set[str],
) -> GateResult:
    gate = GateResult()
    problems = gate.problems

    if not plan.actions:
        problems.append("Rencana tidak berisi aksi apa pun")
    for item in plan.blocked:
        problems.append(f"Pilihan berisi item yang diblokir: {item}")

    for key in ("admin", "layout", "windows-boot-manager"):
        check = preflight.get(key)
        if check is None or check.status == FAIL:
            problems.append(f"Preflight gagal: {check.title if check else key}: {check.message if check else '-'}")
    for check in preflight.needs_confirmation:
        if check.key not in confirmed:
            problems.append(f"Belum dikonfirmasi: {check.title}")

    if not valid_backup_id(backup_id):
        problems.append(f"ID backup '{backup_id}' tidak valid: pakai ID seperti 20261021-193000, bukan path folder")
        return gate
    local = Path(local_root) / backup_id
    gate.local_dir = local
    if not (local / "manifest.json").exists():
        problems.append(f"Backup lokal {local} tidak ditemukan")
        return gate
    failed = [v for v in verify_backup_dir(local) if not v.ok]
    if failed:
        problems.append(f"Backup lokal tidak lolos verifikasi: {failed[0].name}: {failed[0].message}")
    manifest = json.loads((local / "manifest.json").read_text(encoding="utf-8"))
    gate.manifest = manifest

    if not manifest.get("usb_copy"):
        problems.append("Backup ini dibuat dengan --local-only. Eksekusi butuh backup yang punya salinan di drive eksternal")
    else:
        candidates = [Path(root) / "DistroRelay" / "backups" / backup_id for root in external_roots]
        candidates = [c for c in candidates if c.resolve() != local.resolve()]
        external = next((c for c in candidates if (c / "manifest.json").exists()), None)
        if external is None:
            problems.append("Salinan backup di drive eksternal tidak ditemukan; colok drive tempat backup ini disalin")
        else:
            gate.external_dir = external
            # Dibaca langsung dari perangkat (bukan cache), dan harus salinan dari backup yang sama persis.
            failed = [v for v in verify_backup_dir(external, sha256_uncached) if not v.ok]
            if failed:
                problems.append(f"Salinan eksternal tidak lolos verifikasi: {failed[0].name}: {failed[0].message}")
            elif sha256_file(external / "manifest.json") != sha256_file(local / "manifest.json"):
                problems.append("Salinan eksternal bukan salinan backup lokal ini (manifest berbeda)")

    if report.inventory is None or manifest.get("layout_fingerprint") != report.inventory.fingerprint():
        problems.append("Layout disk berubah sejak backup dibuat; buat backup baru")
    if report.esp is None:
        problems.append("ESP tidak terbaca")
    else:
        backed_up = manifest_hashes(manifest)
        current = {f.path.lower(): f.sha256 for f in report.esp.files}
        if backed_up != current:
            changed = sorted(set(backed_up.items()) ^ set(current.items()))
            problems.append(f"Isi ESP berubah sejak backup dibuat (mis. {changed[0][0]}); buat backup baru")

    entries_file = local / "firmware-entries.json"
    backed_entries = json.loads(entries_file.read_text(encoding="utf-8")) if entries_file.exists() else []
    backed_ids = {(e.get("bcd_id") or "").lower() for e in backed_entries}
    for action in plan.actions:
        if action.kind == "delete-boot-entry" and action.data.get("bcd_id", "").lower() not in backed_ids:
            problems.append(f"Entri {action.data.get('bcd_id')} tidak tercatat di backup; buat backup baru")

    # Hapus partisi: metadata GPT-nya harus tercatat identik di backup (syarat minimum aturan backup bertingkat;
    # pernyataan backup data untuk partisi berisi data diminta saat konfirmasi akhir).
    partition_actions = [a for a in plan.actions if a.kind == "delete-partition"]
    if partition_actions:
        try:
            backed_layout = parse_inventory((local / "partitions.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, RuntimeError):
            backed_layout = None
        for action in partition_actions:
            data = action.data
            backed = backed_layout.by_guid(data.get("guid")) if backed_layout else None
            disk = backed_layout.disk(backed.disk_number) if backed else None
            same = backed is not None and disk is not None and (
                disk.guid, backed.offset, backed.size, backed.gpt_type
            ) == (data.get("disk_guid"), data.get("offset"), data.get("size"), data.get("gpt_type"))
            if not same:
                problems.append(f"Metadata partisi {data.get('id')} tidak tercatat identik di backup; buat backup baru")
    return gate
