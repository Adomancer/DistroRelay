"""Sisa palsu buatan tests/vm/fake_leftovers.py harus dinilai sama seperti sisa asli di laptop."""

import importlib.util
from pathlib import Path

import pytest

from distrohop.windows.bootrefs import VERDICT_LEFTOVER, VERDICT_NOT_TARGET, analyze, collect_esp_refs
from distrohop.windows.disks import MIB
from distrohop.windows.fsprobe import FsInfo, probe_bytes
from distrohop.windows.runner import CommandResult
from helpers import BOOTMGFW, fake_esp, laptop_inventory
from test_laptop_real import by_key, real_entries

_spec = importlib.util.spec_from_file_location("fake_leftovers", Path(__file__).parent / "vm" / "fake_leftovers.py")
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)


def vm_esp_files():
    files = fl.esp_files()
    files["EFI\\Microsoft\\Boot\\bootmgfw.efi"] = BOOTMGFW
    return files


def analyze_vm(tmp_path, inventory):
    fsinfo = {"disk0-p1": FsInfo("vfat", uuid="9E2E-08D4", label="SYSTEM")}
    if any(p.number == 5 for p in inventory.partitions):
        fsinfo["disk0-p5"] = probe_bytes(fl.ext4_head())
    esp = fake_esp(tmp_path, inventory, vm_esp_files())
    entries, notes = real_entries()
    return analyze(inventory, fsinfo, True, entries, esp, collect_esp_refs(tmp_path, esp), notes)


def without_p5():
    inventory = laptop_inventory()
    inventory.partitions = [p for p in inventory.partitions if p.number != 5]
    return inventory


def test_ext4_head_is_recognised_as_boot_partition():
    info = probe_bytes(fl.ext4_head())
    assert info.fs_type == "ext4" and info.uuid.lower() == fl.BOOT_UUID and info.last_mounted == "/boot"


def test_fake_leftovers_match_real_laptop_verdicts(tmp_path):
    analysis = analyze_vm(tmp_path, laptop_inventory())

    fedora = by_key(analysis, "efi:fedora")
    assert fedora.verdict == VERDICT_LEFTOVER
    assert "disk0-p5" in {c.ref for c in fedora.components}
    assert any("identik dengan EFI\\fedora\\shimx64.efi" in r for r in fedora.reasons)

    arch = by_key(analysis, "efi:arch")
    assert arch.verdict == VERDICT_LEFTOVER
    assert any("partisi nomor 4" in r and "bukan partisi Linux" in r for r in arch.reasons)

    orphan = by_key(analysis, "grubdir:grub")
    assert orphan.verdict == VERDICT_LEFTOVER
    assert any(fl.ARCH_ROOT_UUID in r and "sudah tidak ada" in r for r in orphan.reasons)

    assert by_key(analysis, "other-esp").verdict == VERDICT_NOT_TARGET
    assert [c.ref for c in by_key(analysis, "unattached").components] == ["mach_kernel", "System"]


def test_without_boot_partition_fedora_is_still_leftover(tmp_path):
    analysis = analyze_vm(tmp_path, without_p5())
    fedora = by_key(analysis, "efi:fedora")
    assert fedora.verdict == VERDICT_LEFTOVER
    assert any(fl.BOOT_UUID in r and "sudah tidak ada" in r for r in fedora.reasons)


def test_boot_partition_slot_is_aligned_and_after_existing_partitions():
    inventory = without_p5()
    slot = fl.boot_partition_slot(inventory, 0)
    assert slot % MIB == 0
    assert all(slot >= p.end or slot + fl.BOOT_SIZE <= p.offset for p in inventory.partitions_on(0))


class _Runner:
    def __init__(self, stdout):
        self.stdout = stdout

    def run(self, args, *, timeout=120):
        return CommandResult(tuple(args), 0, self.stdout)


def test_refuses_to_run_outside_a_vm():
    with pytest.raises(fl.SetupError, match="bukan VM"):
        fl.check_is_vm(_Runner('{"Manufacturer":"HP","Model":"HP Laptop 14s"}'))
    assert "VirtualBox" in fl.check_is_vm(_Runner('{"Manufacturer":"innotek GmbH","Model":"VirtualBox"}'))
