"""Rencana uninstall (dry-run). Menyusun urutan aksi & perintahnya, TIDAK menjalankan apa pun.

Urutan dibuat supaya laptop tetap bisa boot ke Windows di setiap titik kalau proses terhenti:
1. Windows Boot Manager jadi urutan pertama
2. File fallback EFI\\Boot\\bootx64.efi dikembalikan ke milik Windows (selagi folder distro masih ada)
3. Entri boot dihapus
4. Folder/file di ESP dihapus
5. Partisi Linux dihapus (M3), paling akhir karena paling sulit dibatalkan
6. (Opsional, hanya kalau dipilih dan bersebelahan) C: diperluas ke ruang bekasnya
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..format import human_size
from . import gpt_types
from .bootentries import BootEntry
from .bootrefs import DISPOSABLE_ROLES, VERDICT_IN_USE, VERDICT_UNSURE, Analysis, Component, Installation, partition_role
from .disks import DiskInventory, Partition
from .fsprobe import FsInfo

SELECTABLE_KINDS = ("linux", "unattached")
_NEVER_TOUCH = ("efi\\microsoft", "efi\\boot")


@dataclass
class PlannedAction:
    # "set-windows-first" | "restore-fallback" | "delete-boot-entry" | "delete-esp-item" | "delete-partition" | "extend-c"
    kind: str
    title: str
    commands: list[str]
    details: list[str] = field(default_factory=list)
    ref: str | None = None
    data: dict = field(default_factory=dict)  # data terstruktur untuk eksekusi (GUID, path); bukan teks tampilan


@dataclass
class UninstallPlan:
    selected: list[str]
    actions: list[PlannedAction] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    extend_c_possible: bool = False  # partisi tepat sesudah C: ikut dihapus, jadi C: bisa diperluas kalau dipilih


def partition_refusal(part: Partition | None, inventory: DiskInventory) -> str | None:
    """Kenapa partisi ini tidak boleh dihapus (None = boleh). Dicek lagi sesaat sebelum dihapus."""
    if part is None:
        return "partisi tidak ditemukan di inventaris disk"
    if part.category != gpt_types.LINUX:
        return f"tipe {part.type_name} bukan tipe partisi Linux yang diizinkan"
    if part.drive_letter or part.mount_points:
        return "punya drive letter / mount point (sedang dipakai Windows)"
    if part.is_boot or part.is_system:
        return "berflag boot/system"
    disk = inventory.disk(part.disk_number)
    if disk is None or disk.partition_style.upper() != "GPT":
        return "bukan disk GPT"
    if not part.guid or not disk.guid:
        return "GUID partisi/disk tidak terbaca, jadi partisinya tidak bisa dikenali ulang dengan pasti"
    return None


def selectable(analysis: Analysis) -> list[tuple[Installation, Component]]:
    """Komponen yang boleh dipilih: dari instalasi Linux / tak terkait, tidak dikunci."""
    return [
        (inst, comp)
        for inst in analysis.installations
        if inst.kind in SELECTABLE_KINDS
        for comp in inst.components
        if not comp.protected
    ]


def expand_selection(analysis: Analysis, selectors: Sequence[str]) -> tuple[list[str], list[str]]:
    """Selector = key instalasi (semua komponennya) atau ref komponen. -> (refs, selector tak dikenal)."""
    by_installation = {inst.key.lower(): inst for inst in analysis.installations}
    refs: list[str] = []
    unknown: list[str] = []
    all_refs = {comp.ref.lower(): comp.ref for inst in analysis.installations for comp in inst.components}
    for selector in selectors:
        inst = by_installation.get(selector.lower())
        if inst is not None:
            refs.extend(c.ref for c in inst.components)
        elif selector.lower() in all_refs:
            refs.append(all_refs[selector.lower()])
        else:
            unknown.append(selector)
    return list(dict.fromkeys(refs)), unknown


def build_plan(
    analysis: Analysis,
    boot_entries: Sequence[BootEntry],
    refs: Sequence[str],
    *,
    inventory: DiskInventory | None = None,
    fsinfo: Mapping[str, FsInfo] | None = None,
    extend_c: bool = False,
) -> UninstallPlan:
    plan = UninstallPlan(selected=list(refs) + (["extend:c"] if extend_c else []))
    fsinfo = fsinfo or {}
    allowed = {comp.ref.lower(): (inst, comp) for inst, comp in selectable(analysis)}
    owners = {comp.ref.lower(): inst for inst in analysis.installations for comp in inst.components}

    chosen: list[tuple[Installation, Component]] = []
    for ref in refs:
        key = ref.lower()
        if key not in allowed:
            owner = owners.get(key)
            where = f" ({owner.name}, {owner.verdict})" if owner else ""
            plan.blocked.append(f"{ref}{where}: dilindungi / bukan target, tidak bisa dipilih")
            continue
        inst, comp = allowed[key]
        if comp.kind == "partition":
            if inventory is None:
                plan.blocked.append(f"{ref}: inventaris disk tidak tersedia (scan butuh Administrator)")
                continue
            refusal = partition_refusal(next((p for p in inventory.partitions if p.id.lower() == key), None), inventory)
            if refusal:
                plan.blocked.append(f"{ref}: {refusal}")
                continue
        if comp.kind in ("esp-folder", "esp-file") and any(
            key == p or key.startswith(p + "\\") for p in _NEVER_TOUCH
        ):
            plan.blocked.append(f"{ref}: bagian dari Windows/fallback firmware, tidak pernah dihapus langsung")
            continue
        chosen.append((inst, comp))

    for inst in {i.key: i for i, _ in chosen}.values():
        if inst.verdict == VERDICT_IN_USE:
            plan.warnings.append(f"{inst.name} dinilai '{inst.verdict}': instalasi ini kemungkinan masih utuh dan bisa dipakai")
        elif inst.verdict == VERDICT_UNSURE:
            plan.warnings.append(f"{inst.name} dinilai '{inst.verdict}': baca alasannya dulu sebelum lanjut")

    if not chosen:
        if extend_c:
            plan.blocked.append("extend:c: perluas C: hanya bisa bersama penghapusan partisi yang tepat sesudah C:")
        return plan

    managers = [e for e in boot_entries if e.is_windows_boot_manager]
    if not managers:
        plan.blocked.append("Entri Windows Boot Manager tidak ditemukan: rencana tidak bisa dibuat")
        return plan
    if managers[0].order != 0:
        plan.actions.append(
            PlannedAction(
                "set-windows-first",
                "Jadikan Windows Boot Manager urutan boot pertama",
                ["bcdedit /set {fwbootmgr} displayorder {bootmgr} /addfirst"],
                [f"Sekarang urutan ke-{(managers[0].order or 0) + 1}", "Setelah itu urutan dicek ulang"],
            )
        )

    folders = [c.ref.lower() for _, c in chosen if c.kind == "esp-folder"]
    files = [c.ref.lower() for _, c in chosen if c.kind == "esp-file"]
    fallback = analysis.fallback
    if fallback is not None and not fallback.same_as_windows:
        twins = [
            p
            for p in fallback.identical_to
            if p.lower() in files or any(p.lower().startswith(f + "\\") for f in folders)
        ]
        if twins:
            plan.actions.append(
                PlannedAction(
                    "restore-fallback",
                    "Kembalikan file fallback firmware ke milik Windows",
                    [
                        "Get-AuthenticodeSignature <ESP>\\EFI\\Microsoft\\Boot\\bootmgfw.efi  (harus Valid, Microsoft)",
                        "salin <ESP>\\EFI\\Microsoft\\Boot\\bootmgfw.efi -> <ESP>\\EFI\\Boot\\bootx64.efi",
                    ],
                    [
                        f"Isi sekarang: {fallback.owner}",
                        f"Wajib, karena {twins[0]} akan dihapus; tanpa langkah ini fallback menunjuk bootloader yang sudah hilang",
                        "Dijalankan sebelum entri & folder dihapus",
                    ],
                    fallback.path,
                    {"source": "EFI\\Microsoft\\Boot\\bootmgfw.efi", "target": fallback.path},
                )
            )
        else:
            plan.warnings.append(
                f"File fallback {fallback.path} bukan milik Windows ({fallback.owner}), tapi tidak terkait pilihan ini"
            )

    entries = {e.key.lower(): e for e in boot_entries}
    for _, comp in sorted(
        ((i, c) for i, c in chosen if c.kind == "boot-entry"),
        key=lambda item: (entries[item[1].ref.lower()].order is None, entries[item[1].ref.lower()].order or 0),
    ):
        entry = entries[comp.ref.lower()]
        if not entry.bcd_id:
            plan.blocked.append(f"{comp.ref}: entri '{entry.description}' tidak punya GUID bcdedit, tidak bisa dihapus")
            continue
        # `bcdedit /delete` menghapus Boot#### yang dipasangkan Windows dengan objek itu. Kalau pasangan itu
        # tidak terlihat di NVRAM, atau isinya beda, entri yang ikut terhapus bisa milik perangkat lain.
        if "NVRAM" not in entry.sources:
            plan.blocked.append(
                f"{comp.ref}: entri '{entry.description}' hanya terlihat di bcdedit, tidak di NVRAM; "
                "entri firmware mana yang ikut terhapus tidak bisa dipastikan"
            )
            continue
        if entry.conflicts:
            plan.blocked.append(
                f"{comp.ref}: data bcdedit tidak cocok dengan NVRAM ({'; '.join(entry.conflicts)}); "
                "objek BCD-nya kemungkinan basi, tidak dihapus"
            )
            continue
        plan.actions.append(
            PlannedAction(
                "delete-boot-entry",
                f"Hapus entri boot '{entry.description}' ({entry.label})",
                [f"bcdedit /delete {entry.bcd_id}"],
                [
                    f"Menjalankan: {entry.path or '(tanpa file)'}",
                    "Terkonfirmasi di NVRAM + bcdedit; dicek ulang ke keduanya sesaat sebelum dihapus",
                ],
                comp.ref,
                {
                    "bcd_id": entry.bcd_id,
                    "description": entry.description,
                    "path": entry.path,
                    "label": entry.label,
                    "nvram_number": entry.nvram_number,
                },
            )
        )

    for _, comp in chosen:
        if comp.kind not in ("esp-folder", "esp-file"):
            continue
        what = "folder" if comp.kind == "esp-folder" else "file"
        plan.actions.append(
            PlannedAction(
                "delete-esp-item",
                f"Hapus {what} ESP {comp.ref}",
                [f"hapus {what} <ESP>\\{comp.ref}"],
                [human_size(comp.size)] + comp.evidence[:2],
                comp.ref,
                {"path": comp.ref, "kind": what},
            )
        )

    for _, comp in chosen:
        if comp.kind == "partition":
            plan.actions.append(_delete_partition_action(inventory, fsinfo, comp.ref))
    _plan_extend_c(plan, inventory, extend_c)

    deleted_entries = {a.ref.lower() for a in plan.actions if a.kind == "delete-boot-entry"}
    for entry in boot_entries:
        path = (entry.path or "").lower().lstrip("\\")
        if entry.key.lower() in deleted_entries or not path:
            continue
        if any(path == f or path.startswith(f + "\\") for f in folders + files):
            plan.warnings.append(
                f"Entri '{entry.description}' ({entry.label}) menjalankan file yang akan dihapus, tapi entrinya tidak dipilih"
            )
    return plan


def _delete_partition_action(inventory: DiskInventory, fsinfo: Mapping[str, FsInfo], ref: str) -> PlannedAction:
    part = next(p for p in inventory.partitions if p.id.lower() == ref.lower())
    disk = inventory.disk(part.disk_number)
    info = fsinfo.get(part.id)
    role = partition_role(part, fsinfo)
    needs_statement = role not in DISPOSABLE_ROLES
    content = info.describe() if info else "filesystem tidak terbaca / tidak dikenali"
    return PlannedAction(
        "delete-partition",
        f"Hapus partisi {part.id} ({human_size(part.size)}, {part.type_name})",
        [f"Remove-Partition -DiskNumber {part.disk_number} -PartitionNumber {part.number} -Confirm:$false"],
        [
            f"Disk {part.disk_number}: {disk.model} ({disk.bus_type}), GUID disk {disk.guid}",
            f"Offset {part.offset:,} byte · ukuran {part.size:,} byte · tipe GPT {part.gpt_type}",
            f"GUID partisi {part.guid}",
            f"Isi: {content} · peran: {role}",
            "Nomor disk & partisi diambil ulang dari GUID sesaat sebelum dihapus, setelah layout disk dicek ulang",
            "Hanya entri tabel GPT yang dihapus; datanya belum ditimpa, jadi bisa dipulihkan (Pulihkan) "
            "selama ruangnya belum dipakai",
            "Wajib ketik ID partisi" + (" dan pernyataan bahwa datanya sudah kamu backup" if needs_statement else ""),
        ],
        part.id,
        {
            "id": part.id,
            "guid": part.guid,
            "disk_guid": disk.guid,
            "disk_number": part.disk_number,
            "number": part.number,
            "offset": part.offset,
            "size": part.size,
            "gpt_type": part.gpt_type,
            "type_name": part.type_name,
            "role": role,
            "fs_type": info.fs_type if info else None,
            "fs_uuid": info.uuid if info else None,
            "needs_data_statement": needs_statement,
        },
    )


def _plan_extend_c(plan: UninstallPlan, inventory: DiskInventory | None, extend_c: bool) -> None:
    """Perluas C: hanya kalau dipilih sendiri DAN partisi tepat sesudah C: ikut dihapus."""
    deleted = {a.data["guid"]: a.data for a in plan.actions if a.kind == "delete-partition"}
    if not deleted or inventory is None:
        if extend_c:
            plan.blocked.append("extend:c: perluas C: hanya bisa bersama penghapusan partisi yang tepat sesudah C:")
        return
    windows = inventory.windows_partition()
    following = inventory.next_partition(windows) if windows else None
    adjacent = following is not None and following.guid in deleted
    freed = ", ".join(d["id"] for d in deleted.values())
    if not adjacent:
        between = f" (ada {following.id} {following.type_name} di antaranya)" if following is not None else ""
        message = f"Ruang bekas {freed} tidak bersebelahan dengan C:{between}, jadi dibiarkan unallocated"
        if extend_c:
            plan.blocked.append(f"extend:c: {message}")
        else:
            plan.notes.append(message)
        return
    plan.extend_c_possible = True
    if not extend_c:
        plan.notes.append(
            f"Ruang bekas {freed} bersebelahan dengan C:. Default dibiarkan unallocated; C: bisa diperluas kalau "
            "kamu memilih 'Perluas C:' (setelah itu partisinya tidak bisa dipulihkan lagi)"
        )
        return
    disk = inventory.disk(windows.disk_number)
    plan.actions.append(
        PlannedAction(
            "extend-c",
            "Perluas C: ke ruang bekas partisi Linux",
            [
                "Get-PartitionSupportedSize -DriveLetter C  (ukuran maksimum)",
                "Resize-Partition -DriveLetter C -Size <ukuran maksimum>",
            ],
            [
                f"C: = {windows.id}, sekarang {human_size(windows.size)}",
                "GUID C: dicek ulang sebelum diperluas; BitLocker tidak perlu dimatikan untuk memperluas volume",
                "Setelah C: diperluas, partisi yang dihapus TIDAK bisa dipulihkan lagi lewat Pulihkan",
            ],
            windows.id,
            {
                "guid": windows.guid,
                "disk_guid": disk.guid if disk else None,
                "offset": windows.offset,
                "size": windows.size,
                "freed": sorted(deleted),
            },
        )
    )
    plan.warnings.append("C: akan diperluas: partisi yang dihapus tidak bisa dipulihkan lagi sesudahnya")
