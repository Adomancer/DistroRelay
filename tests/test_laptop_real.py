"""Skenario berdasarkan scan admin laptop asli (2026-10-21).

Fixture asli: daftar disk, output bcdedit, dan NVRAM (nomor seri disamarkan).
Layout ESP meniru hasil scan, tapi isi filenya sintetis.
"""

import json

from distrohop.windows import nvram
from distrohop.windows.bootentries import merge_entries, parse_firmware
from distrohop.windows.bootrefs import (
    VERDICT_LEFTOVER,
    VERDICT_NOT_TARGET,
    VERDICT_UNSURE,
    analyze,
    collect_esp_refs,
    grub_embedded,
    split_grub_prefix,
)
from distrohop.windows import gpt_types
from distrohop.windows.fsprobe import FsInfo
from helpers import (
    LAPTOP_BOOT_UUID,
    add_partition,
    fake_esp,
    fixture_text,
    grub_core_image,
    laptop_inventory,
    laptop_real_esp_files,
)

FSINFO = {
    "disk0-p1": FsInfo("vfat", uuid="9E2E-08D4", label="SYSTEM"),
    "disk0-p5": FsInfo("ext4", uuid=LAPTOP_BOOT_UUID, last_mounted="/boot", write_time="2026-06-08T03:10:53+00:00"),
}


def real_entries():
    config = parse_firmware(fixture_text("bcdedit_firmware_laptop.txt"))
    snapshot = nvram.snapshot_from_raw(json.loads(fixture_text("nvram_laptop.json")))
    return merge_entries(config, snapshot)


def analyze_laptop(tmp_path, files=None, inventory=None):
    inventory = inventory or laptop_inventory()
    esp = fake_esp(tmp_path, inventory, files or laptop_real_esp_files())
    entries, notes = real_entries()
    return analyze(inventory, FSINFO, True, entries, esp, collect_esp_refs(tmp_path, esp), notes), esp


def by_key(analysis, key):
    return next(i for i in analysis.installations if i.key == key)


def test_real_bcdedit_and_nvram_agree():
    entries, notes = real_entries()
    assert notes == []
    assert [e.description for e in entries] == [
        "Fedora",
        "USB Drive (UEFI)",
        "Arch",
        "Windows Boot Manager",
        "Solid State Disk",
    ]
    assert all(e.sources == ["NVRAM", "bcdedit"] for e in entries)
    assert [e.label for e in entries] == ["Boot0000", "Boot9999", "Boot0004", "Boot0001", "Boot0002"]
    assert entries[3].is_windows_boot_manager and entries[3].order == 3


def test_real_nvram_fixture_is_redacted_but_still_parses():
    snapshot = nvram.snapshot_from_raw(json.loads(fixture_text("nvram_laptop.json")))
    ssd = snapshot.options[2]
    vendor = next(n for n in ssd.nodes if (n.type, n.subtype) == (0x01, 0x04))
    nvme = next(n for n in ssd.nodes if (n.type, n.subtype) == (0x03, 0x17))
    assert set(vendor.data[16:]) == {0} and set(nvme.data[4:12]) == {0}
    assert ssd.device_summary.endswith("| VenHw")
    assert ssd.partition_guid == "55aefd10-5fa7-4724-b83b-0fe2e6911ae0"


def test_grub_embedded_prefix():
    assert grub_embedded(grub_core_image("(,gpt1)/grub", "set root=x")) == ("(,gpt1)/grub", "set root=x")
    assert grub_embedded(b"MZ tanpa modul") == (None, None)
    assert split_grub_prefix("(,gpt1)/grub") == ("grub", 1)
    assert split_grub_prefix("/boot/grub") == ("boot\\grub", None)
    assert split_grub_prefix("(hd0,gpt7)/boot/grub") == ("boot\\grub", 7)


def test_laptop_fedora_arch_and_orphan_grub_are_leftovers(tmp_path):
    analysis, _ = analyze_laptop(tmp_path)

    fedora = by_key(analysis, "efi:fedora")
    assert fedora.verdict == VERDICT_LEFTOVER
    assert any("urutan boot PERTAMA" in r for r in fedora.reasons)
    assert any("identik dengan EFI\\fedora\\shimx64.efi" in r for r in fedora.reasons)
    assert "disk0-p5" in {c.ref for c in fedora.components}

    arch = by_key(analysis, "efi:arch")
    assert arch.name == "Arch Linux" and arch.verdict == VERDICT_LEFTOVER
    assert {c.ref for c in arch.components} == {"nv:0004", "EFI\\Arch"}
    assert any(
        "partisi nomor 4" in r and "Windows Recovery (NTFS)" in r and "bukan partisi Linux" in r for r in arch.reasons
    )

    orphan = by_key(analysis, "grubdir:grub")
    assert orphan.verdict == VERDICT_LEFTOVER
    assert [c.ref for c in orphan.components] == ["grub"]
    assert any("tidak ada bootloader di EFI\\ yang menunjuk folder ini" in e for e in orphan.components[0].evidence)
    assert any("77777777-0000-4000-8000-000000000007" in r and "sudah tidak ada" in r for r in orphan.reasons)

    hp = by_key(analysis, "other-esp")
    assert hp.verdict == VERDICT_NOT_TARGET and [c.ref for c in hp.components] == ["EFI\\HP"]

    unattached = by_key(analysis, "unattached")
    assert [c.ref for c in unattached.components] == ["mach_kernel", "System"]  # satu komponen per folder

    firmware = by_key(analysis, "firmware")
    ssd = next(c for c in firmware.components if "Solid State Disk" in c.label)
    assert any("fallback EFI\\Boot\\bootx64.efi" in e and "shim" in e for e in ssd.evidence)

    windows = by_key(analysis, "windows")
    assert any("urutan boot ke-4" in r for r in windows.reasons)


def test_grub_folder_belongs_to_arch_when_its_prefix_points_there(tmp_path):
    files = laptop_real_esp_files()
    files["EFI\\Arch\\grubx64.efi"] = grub_core_image("(,gpt1)/grub")
    analysis, _ = analyze_laptop(tmp_path, files)
    arch = by_key(analysis, "efi:arch")
    assert arch.verdict == VERDICT_LEFTOVER
    assert {c.ref for c in arch.components} == {"nv:0004", "EFI\\Arch", "grub"}
    grub_folder = next(c for c in arch.components if c.ref == "grub")
    assert any("prefix GRUB yang tertanam di EFI\\Arch" in e for e in grub_folder.evidence)
    assert not any(i.key == "grubdir:grub" for i in analysis.installations)


def test_arch_without_readable_prefix_still_leftover_when_no_root_candidate(tmp_path):
    files = laptop_real_esp_files()
    files["EFI\\Arch\\grubx64.efi"] = b"MZ GNU GRUB tanpa modul"
    analysis, _ = analyze_laptop(tmp_path, files)
    arch = by_key(analysis, "efi:arch")
    assert arch.verdict == VERDICT_LEFTOVER
    assert any("sudah terhubung ke instalasi lain" in r for r in arch.reasons)
    assert "grub" in {c.ref for c in by_key(analysis, "grubdir:grub").components}


def test_arch_is_unsure_when_an_unlinked_linux_partition_exists(tmp_path):
    files = laptop_real_esp_files()
    files["EFI\\Arch\\grubx64.efi"] = b"MZ GNU GRUB tanpa modul"
    inventory = laptop_inventory()
    add_partition(inventory, 6, gpt_types.LINUX_FILESYSTEM, "99999999-0000-4000-8000-000000000006")
    esp = fake_esp(tmp_path, inventory, files)
    entries, notes = real_entries()
    fsinfo = dict(FSINFO, **{"disk0-p6": FsInfo("ext4", uuid="66666666-0000-4000-8000-000000000006", last_mounted="/")})
    analysis = analyze(inventory, fsinfo, True, entries, esp, collect_esp_refs(tmp_path, esp), notes)
    assert by_key(analysis, "efi:arch").verdict == VERDICT_UNSURE


def test_locked_esp_files_are_reported(tmp_path):
    inventory = laptop_inventory()
    esp = fake_esp(tmp_path, inventory, laptop_real_esp_files())
    esp.unreadable = ["EFI\\Microsoft\\Boot\\BCD", "EFI\\Microsoft\\Boot\\BCD.LOG"]
    entries, notes = real_entries()
    analysis = analyze(inventory, FSINFO, True, entries, esp, collect_esp_refs(tmp_path, esp), notes)
    assert any("2 file di ESP tidak bisa dibaca" in n and "bcdedit /export" in n for n in analysis.notes)
