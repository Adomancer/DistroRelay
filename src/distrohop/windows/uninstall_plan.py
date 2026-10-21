"""Rencana uninstall (dry-run). Menyusun urutan aksi & perintahnya, TIDAK menjalankan apa pun.

Urutan dibuat supaya laptop tetap bisa boot ke Windows di setiap titik kalau proses terhenti:
1. Windows Boot Manager jadi urutan pertama
2. File fallback EFI\\Boot\\bootx64.efi dikembalikan ke milik Windows (selagi folder distro masih ada)
3. Entri boot dihapus
4. Folder/file di ESP dihapus
Hapus partisi baru tersedia di M3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..format import human_size
from .bootentries import BootEntry
from .bootrefs import VERDICT_IN_USE, VERDICT_UNSURE, Analysis, Component, Installation

SELECTABLE_KINDS = ("linux", "unattached")
_NEVER_TOUCH = ("efi\\microsoft", "efi\\boot")


@dataclass
class PlannedAction:
    kind: str  # "set-windows-first" | "restore-fallback" | "delete-boot-entry" | "delete-esp-item"
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


def build_plan(analysis: Analysis, boot_entries: Sequence[BootEntry], refs: Sequence[str]) -> UninstallPlan:
    plan = UninstallPlan(selected=list(refs))
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
            plan.blocked.append(f"{ref}: hapus partisi baru tersedia di M3")
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
        plan.actions.append(
            PlannedAction(
                "delete-boot-entry",
                f"Hapus entri boot '{entry.description}' ({entry.label})",
                [f"bcdedit /delete {entry.bcd_id}"],
                [f"Menjalankan: {entry.path or '(tanpa file)'}", "GUID & path dicek ulang sesaat sebelum dihapus"],
                comp.ref,
                {"bcd_id": entry.bcd_id, "description": entry.description, "path": entry.path, "label": entry.label},
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
