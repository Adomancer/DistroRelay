"""Dry-run rencana uninstall pada layout laptop asli (fixture bcdedit + NVRAM asli)."""

import dataclasses

from distrorelay.windows.uninstall_plan import build_plan, expand_selection
from helpers import laptop_inventory
from test_laptop_real import FSINFO, analyze_laptop, real_entries

FEDORA_BCD = "{8bd7fdcc-13ca-11f1-84fe-806e6f6e6963}"
ARCH_BCD = "{fb3dba71-dd51-11f0-8498-806e6f6e6963}"


def plan_for(tmp_path, *selectors, entries=None, inventory=None, extend_c=False):
    inventory = inventory or laptop_inventory()
    analysis, _ = analyze_laptop(tmp_path, inventory=inventory)
    entries = entries if entries is not None else real_entries()[0]
    refs, unknown = expand_selection(analysis, list(selectors))
    return build_plan(analysis, entries, refs, inventory=inventory, fsinfo=FSINFO, extend_c=extend_c), unknown


def test_all_three_leftovers_in_safe_order(tmp_path):
    plan, unknown = plan_for(tmp_path, "efi:fedora", "efi:arch", "grubdir:grub")
    assert unknown == []
    assert [(a.kind, a.ref) for a in plan.actions] == [
        ("set-windows-first", None),
        ("restore-fallback", "EFI\\Boot\\bootx64.efi"),
        ("delete-boot-entry", "nv:0000"),
        ("delete-boot-entry", "nv:0004"),
        ("delete-esp-item", "EFI\\fedora"),
        ("delete-esp-item", "EFI\\Arch"),
        ("delete-esp-item", "grub"),
        ("delete-partition", "disk0-p5"),  # paling akhir: paling sulit dibatalkan
    ]
    assert plan.actions[0].commands == ["bcdedit /set {fwbootmgr} displayorder {bootmgr} /addfirst"]
    assert plan.actions[2].commands == [f"bcdedit /delete {FEDORA_BCD}"]
    assert plan.actions[3].commands == [f"bcdedit /delete {ARCH_BCD}"]
    assert any("Get-AuthenticodeSignature" in c for c in plan.actions[1].commands)
    assert plan.blocked == []
    assert plan.warnings == []
    # Laptop asli: C:, lalu Recovery, baru partisi Linux -> tidak bersebelahan, ruangnya dibiarkan unallocated.
    assert plan.notes == [
        "Ruang bekas disk0-p5 tidak bersebelahan dengan C: (ada disk0-p4 Windows Recovery di antaranya), "
        "jadi dibiarkan unallocated"
    ]
    assert not plan.extend_c_possible


def test_arch_alone_does_not_touch_fedoras_fallback(tmp_path):
    plan, _ = plan_for(tmp_path, "efi:arch")
    assert [a.kind for a in plan.actions] == ["set-windows-first", "delete-boot-entry", "delete-esp-item"]
    assert any("bootx64.efi bukan salinan bootmgfw.efi" in w and "folder Linux lain" in w and "fedora" in w
               for w in plan.warnings)


def test_folder_without_its_entry_warns_and_still_restores_fallback(tmp_path):
    plan, _ = plan_for(tmp_path, "EFI\\fedora")
    assert [a.kind for a in plan.actions] == ["set-windows-first", "restore-fallback", "delete-esp-item"]
    assert any("'Fedora' (Boot0000) menjalankan file yang akan dihapus" in w for w in plan.warnings)


def test_protected_and_non_target_items_are_blocked(tmp_path):
    plan, unknown = plan_for(tmp_path, "windows", "EFI\\Microsoft", "other-esp", "nv:0002", "efi:tidak-ada")
    assert plan.actions == []
    assert unknown == ["efi:tidak-ada"]
    blocked = " | ".join(plan.blocked)
    assert "EFI\\Microsoft" in blocked and "EFI\\HP" in blocked and "nv:0002" in blocked and "disk0-p3" in blocked


def test_windows_already_first_skips_reordering(tmp_path):
    entries = [
        dataclasses.replace(e, order=0 if e.is_windows_boot_manager else (e.order or 0) + 1) for e in real_entries()[0]
    ]
    plan, _ = plan_for(tmp_path, "efi:arch", entries=entries)
    assert [a.kind for a in plan.actions] == ["delete-boot-entry", "delete-esp-item"]
