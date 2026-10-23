"""Menu Pulihkan (M2b): kembalikan file ESP dari backup dan buat ulang entri boot firmware.

Pemulihan tidak pernah menimpa file yang sudah ada dengan isi berbeda, dan tidak menyentuh Windows
(EFI\\Microsoft, entri Windows Boot Manager). Untuk Windows, pakai docs/RECOVERY.md (bcdboot).
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Sequence

from . import gpt_types
from .backup import Verification, sha256_file
from .bootentries import load_firmware
from .disks import DiskInventory, Partition
from .esp import contained_path, norm_path
from .execute import WELL_KNOWN_IDS, _exe
from .runner import AllowListRunner, Runner

_GUID = re.compile(r"\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}")
_EFI_PATH = re.compile(r"^\\efi\\[^\\]+\\.+\.efi$", re.IGNORECASE)


def restore_runner(inner: Runner, scripts: frozenset[str] = frozenset()) -> tuple[AllowListRunner, set[str]]:
    """Runner tahap pemulihan. `bcdedit /delete` hanya untuk GUID yang dibuat sendiri di sesi ini (rollback).
    `scripts` = script PowerShell persis yang boleh jalan (buat ulang partisi tertentu dari backup)."""
    created: set[str] = set()

    def is_guid(value: str) -> bool:
        return bool(_GUID.fullmatch(value)) and value.lower() not in WELL_KNOWN_IDS

    def copy_bootmgr(args):
        return _exe(args) == "bcdedit" and len(args) == 5 and [a.lower() for a in args[1:4]] == ["/copy", "{bootmgr}", "/d"]

    def set_path(args):
        return (
            _exe(args) == "bcdedit" and len(args) == 5 and args[1].lower() == "/set" and is_guid(args[2])
            and args[2].lower() in created and args[3].lower() == "path" and bool(_EFI_PATH.match(args[4]))
        )

    def add_last(args):
        return (
            _exe(args) == "bcdedit" and len(args) == 6 and [a.lower() for a in args[1:4]] == ["/set", "{fwbootmgr}", "displayorder"]
            and args[4].lower() in created and args[5].lower() == "/addlast"
        )

    def rollback(args):
        return _exe(args) == "bcdedit" and len(args) == 3 and args[1].lower() == "/delete" and args[2].lower() in created

    return AllowListRunner(inner, rules=(copy_bootmgr, set_path, add_last, rollback), scripts=scripts), created


# --- Partisi (M3) ------------------------------------------------------------------


def partition_restore_refusal(part: Partition, backup_layout: DiskInventory, current: DiskInventory | None) -> str | None:
    """Kenapa partisi dari backup tidak bisa dibuat ulang sekarang (None = bisa)."""
    if part.category != gpt_types.LINUX:
        return "Hanya partisi Linux yang dipulihkan dari sini"
    if current is None:
        return "Daftar disk sekarang tidak terbaca"
    if current.by_guid(part.guid) is not None:
        return "Masih ada di disk, tidak perlu dipulihkan"
    backup_disk = backup_layout.disk(part.disk_number)
    disk = next((d for d in current.disks if backup_disk and d.guid == backup_disk.guid), None)
    if disk is None:
        return "Disk asalnya tidak ditemukan (GUID disk berbeda)"
    if part.end > disk.size:
        return "Melewati ujung disk"
    for other in current.partitions_on(disk.number):
        if other.offset < part.end and other.end > part.offset:
            return f"Ruangnya sudah dipakai {other.id} ({other.type_name})"
    return None


def partition_restore_script(part: Partition, disk_guid: str) -> str:
    """Buat ulang entri GPT dengan offset, ukuran, dan tipe yang sama persis. Datanya tidak disentuh, jadi isi
    partisi kembali utuh selama ruangnya belum ditimpa. GUID partisi (PARTUUID) yang baru berbeda."""
    disk, gpt_type = _braced(disk_guid), _braced(part.gpt_type)
    start, end = int(part.offset), int(part.offset) + int(part.size)
    return f"""# distrorelay:restore-partition
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$d = @(Get-Disk | Where-Object {{ $_.Guid -eq '{disk}' }})
if ($d.Count -ne 1) {{ throw 'Disk {disk} tidak ditemukan' }}
$d = $d[0]
$used = @(Get-Partition -DiskNumber $d.Number | Where-Object {{ $_.Offset -lt {end} -and ($_.Offset + $_.Size) -gt {start} }})
if ($used.Count) {{ throw 'Ruangnya sudah dipakai partisi lain' }}
$null = New-Partition -DiskNumber $d.Number -Offset {start} -Size {int(part.size)} -GptType '{gpt_type}'
"""


def _braced(value: str | None) -> str:
    text = (value or "").strip("{}").lower()
    if not _GUID.fullmatch("{" + text + "}"):
        raise ValueError(f"GUID tidak valid: {value!r}")
    return "{" + text + "}"


def protected_esp_item(item: str) -> bool:
    """Seluruh ESP, EFI\\, dan milik Windows tidak dipulihkan dari sini."""
    low = norm_path(item).lower()
    return not low or low in ("efi", "efi\\microsoft") or low.startswith("efi\\microsoft\\")


def restore_esp_items(backup_dir: Path, items: Sequence[str], esp_root: Path) -> list[Verification]:
    backup_dir = Path(backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("esp_files", [])
    results: list[Verification] = []
    with zipfile.ZipFile(backup_dir / "esp.zip") as archive:
        for item in items:
            norm = norm_path(item)
            low = norm.lower()
            label = f"Pulihkan {norm or item}"
            if protected_esp_item(item):
                results.append(Verification(label, False, "Dilindungi; pulihkan Windows lewat docs/RECOVERY.md (bcdboot)"))
                continue
            chosen = [e for e in entries if e["path"].lower() == low or e["path"].lower().startswith(low + "\\")]
            if not chosen:
                results.append(Verification(label, False, "Tidak ada di backup"))
                continue
            pending, conflicts, invalid = [], [], []
            for entry in chosen:
                # Path dari manifest backup (file di %LOCALAPPDATA%), jadi dicek seperti input dari luar.
                try:
                    target = contained_path(esp_root, entry["path"])
                except ValueError:
                    invalid.append(entry["path"])
                    continue
                if not target.exists():
                    pending.append((entry, target))
                elif sha256_file(target) != entry["sha256"]:
                    conflicts.append(entry["path"])
            if invalid:
                results.append(Verification(label, False, f"Path di manifest tidak valid (keluar dari ESP), tidak ada yang "
                                                          f"dipulihkan: {invalid[:3]}"))
                continue
            if conflicts:
                results.append(Verification(label, False, f"{len(conflicts)} file sudah ada dengan isi berbeda, tidak ditimpa: {conflicts[:3]}"))
                continue
            for entry, target in pending:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry["path"].replace("\\", "/")) as source, open(target, "wb") as destination:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        destination.write(chunk)
            bad = [e["path"] for e, target in pending if sha256_file(target) != e["sha256"]]
            if bad:
                results.append(Verification(label, False, f"Hash tidak cocok setelah dipulihkan: {bad[:3]}"))
            else:
                skipped = len(chosen) - len(pending)
                note = f" ({skipped} file sudah ada dan sama)" if skipped else ""
                results.append(Verification(label, True, f"{len(pending)} file dipulihkan, hash cocok{note}"))
    return results


def entry_label(record: dict) -> str:
    number = record.get("nvram_number")
    return f"Boot{number:04X}" if number is not None else (record.get("bcd_id") or "?")


def describe_entry(record: dict) -> str:
    return f"'{record.get('description') or '?'}' {entry_label(record)} ({record.get('path') or '-'})"


def select_backup_entries(records: Sequence[dict], wanted: Sequence[str]) -> tuple[list[dict], str | None]:
    """Pilih entri dari firmware-entries.json lewat nama, nomor Boot####, atau GUID bcdedit.

    Nama yang dipakai lebih dari satu entri (mis. Fedora dobel) ditolak dengan daftar pilihannya.
    """
    chosen: list[dict] = []
    for name in wanted:
        key = name.strip().casefold()
        matches = [
            r for r in records
            if key in {
                (r.get("description") or "").strip().casefold(),
                entry_label(r).casefold(),
                (r.get("bcd_id") or "").casefold(),
            }
        ]
        if not matches:
            return [], f"Entri '{name}' tidak ada di backup"
        if len(matches) > 1:
            options = ", ".join(describe_entry(r) for r in matches)
            return [], (
                f"Ada {len(matches)} entri bernama '{name}' di backup: {options}. "
                f"Pilih satu dengan nomornya, mis. --boot-entry {entry_label(matches[0])}"
            )
        chosen.append(matches[0])
    return chosen, None


def unrestorable_reason(record: dict) -> str | None:
    """Kenapa entri ini tidak bisa dibuat ulang otomatis (None = bisa)."""
    path = record.get("path") or ""
    if not _EFI_PATH.match(path):
        return f"Path tidak bisa dipulihkan otomatis: {path or '(kosong)'}"
    if path.lower().startswith("\\efi\\microsoft\\"):
        return "Entri Windows dipulihkan lewat bcdboot (docs/RECOVERY.md)"
    return None


def restore_boot_entry(runner: Runner, created: set[str], record: dict) -> Verification:
    """Buat ulang entri firmware dari catatan backup (firmware-entries.json), ditaruh di urutan terakhir."""
    description = record.get("description") or ""
    path = record.get("path") or ""
    label = f"Pulihkan entri '{description}'"
    reason = unrestorable_reason(record)
    if reason:
        return Verification(label, False, reason)

    config, _ = load_firmware(runner)
    if any(e.description == description and (e.path or "").lower() == path.lower() for e in config.entries):
        return Verification(label, True, "Sudah ada, tidak perlu dibuat ulang")

    copied = runner.run(["bcdedit", "/copy", "{bootmgr}", "/d", description])
    match = _GUID.search(copied.stdout)
    if not copied.ok or match is None:
        return Verification(label, False, f"bcdedit /copy gagal: {copied.output() or copied.returncode}")
    new_id = match.group(0).lower()
    created.add(new_id)
    steps = [
        ["bcdedit", "/set", new_id, "path", path],
        ["bcdedit", "/set", "{fwbootmgr}", "displayorder", new_id, "/addlast"],
    ]
    for args in steps:
        result = runner.run(args)
        if not result.ok:
            runner.run(["bcdedit", "/delete", new_id])
            return Verification(label, False, f"Gagal ({' '.join(args[1:3])}): {result.output()}; entri setengah jadi dihapus lagi")

    config, _ = load_firmware(runner)
    entry = config.find(new_id)
    if entry is None or (entry.path or "").lower() != path.lower() or new_id not in config.display_order:
        return Verification(label, False, "Entri baru tidak terlihat dengan benar setelah dibuat")
    return Verification(label, True, f"Dibuat ulang sebagai {new_id}, urutan terakhir")
