"""Eksekusi uninstall di laptop simulasi (FakeBcd + ESP di folder temp). Tidak menyentuh firmware/disk asli."""

import json
from pathlib import Path

import pytest

from distrohop.windows.backup import backup_runner, create_backup
from distrohop.windows.bootentries import BOOTMGR_ID
from distrohop.windows.execute import (
    ExecutionError,
    SimulatedCrash,
    check_gate,
    execute_plan,
    execution_runner,
    safe_esp_path,
)
from distrohop.windows.journal import Journal, incomplete_sessions, read_journal, summarize
from distrohop.windows.postcheck import post_check
from distrohop.windows.preflight import MANUAL, OK, Check, PreflightResult
from distrohop.windows.restore import restore_boot_entry, restore_esp_items, restore_runner
from distrohop.windows.runner import ReadOnlyViolation
from distrohop.windows.scanner import run_scan
from distrohop.windows.uninstall_plan import build_plan, expand_selection
from helpers import BOOTMGFW, SHIM, fake_mount, laptop_world

SELECT = ["efi:fedora", "efi:arch", "grubdir:grub"]
FEDORA = "{8bd7fdcc-13ca-11f1-84fe-806e6f6e6963}"
ARCH = "{fb3dba71-dd51-11f0-8498-806e6f6e6963}"


def preflight_ok():
    return PreflightResult(
        checks=[
            Check("admin", "Hak Administrator", OK, ""),
            Check("layout", "Layout disk tidak berubah", OK, ""),
            Check("windows-boot-manager", "Windows Boot Manager sehat", OK, ""),
            Check("emergency-usb", "USB darurat Windows", MANUAL, ""),
        ],
        media=[],
        usb_targets=[],
        signature=None,
    )


def world(tmp_path, select=SELECT, local_only=False):
    bcd, esp_root, deps = laptop_world(tmp_path)
    report = run_scan(deps())
    refs, unknown = expand_selection(report.analysis, select)
    assert unknown == []
    plan = build_plan(report.analysis, report.boot_entries, refs)
    backup = create_backup(
        report,
        backup_runner(bcd, root=tmp_path / "local"),
        fake_mount(esp_root),
        local_root=tmp_path / "local",
        usb_root=None if local_only else tmp_path / "ext",
        local_only=local_only,
    )
    assert backup.ok, [v for v in backup.verifications if not v.ok]
    return bcd, esp_root, deps, report, plan, backup


def gate_for(tmp_path, plan, report, backup, confirmed=("emergency-usb",), external=True):
    return check_gate(
        plan,
        report,
        preflight_ok(),
        backup_id=backup.backup_id,
        local_root=tmp_path / "local",
        external_roots=[tmp_path / "ext"] if external else [],
        confirmed=set(confirmed),
    )


def run(tmp_path, bcd, esp_root, plan, manifest, **kwargs):
    journal = Journal.start(tmp_path / "journal", "sesi-test", {})
    result = execute_plan(plan, runner=execution_runner(bcd, plan), esp_root=esp_root, manifest=manifest, journal=journal, **kwargs)
    return result, journal


def test_full_uninstall_then_post_check(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    gate = gate_for(tmp_path, plan, report, backup)
    assert gate.ok, gate.problems
    assert gate.notes == ["Dilewati (M3): disk0-p5: hapus partisi baru tersedia di M3"]

    result, journal = run(tmp_path, bcd, esp_root, plan, gate.manifest)
    assert result.completed, result.stopped
    assert [s.kind for s in result.steps] == [
        "set-windows-first",
        "restore-fallback",
        "delete-boot-entry",
        "delete-boot-entry",
        "delete-esp-item",
        "delete-esp-item",
        "delete-esp-item",
    ]
    for gone in ("EFI/fedora", "EFI/Arch", "grub"):
        assert not (esp_root / gone).exists()
    assert (esp_root / "EFI/Boot/bootx64.efi").read_bytes() == BOOTMGFW
    assert (esp_root / "EFI/Microsoft/Boot/bootmgfw.efi").read_bytes() == BOOTMGFW
    assert (esp_root / "EFI/HP/BIOS/Current/08A4F.bin").exists()
    assert (esp_root / "mach_kernel").exists()  # tidak dipilih, tidak disentuh
    assert bcd.order[0] == BOOTMGR_ID and FEDORA not in bcd.entries and ARCH not in bcd.entries

    summary = summarize(journal.path)
    assert summary.finished and summary.ok and summary.unfinished_step is None
    assert [e.status for e in read_journal(journal.path)].count("done") == 7

    after = run_scan(deps())
    checks = post_check(plan, gate.manifest, report, after)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_gate_refuses_when_conditions_are_not_met(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    assert any("Belum dikonfirmasi: USB darurat" in p for p in gate_for(tmp_path, plan, report, backup, confirmed=()).problems)
    assert any("drive eksternal tidak ditemukan" in p for p in gate_for(tmp_path, plan, report, backup, external=False).problems)

    (esp_root / "EFI/fedora/grub.cfg").write_text("diubah setelah backup", encoding="utf-8")
    changed = run_scan(deps())
    assert any("Isi ESP berubah sejak backup" in p for p in gate_for(tmp_path, plan, changed, backup).problems)

    plan.blocked.append("EFI\\Microsoft: dilindungi / bukan target, tidak bisa dipilih")
    assert any("diblokir" in p for p in gate_for(tmp_path, plan, report, backup).problems)


def test_gate_refuses_local_only_backup(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path, local_only=True)
    problems = gate_for(tmp_path, plan, report, backup).problems
    assert any("--local-only" in p for p in problems)


def test_changed_entry_stops_before_deleting(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    gate = gate_for(tmp_path, plan, report, backup)
    bcd.entries[FEDORA]["path"] = "\\EFI\\fedora\\grubx64.efi"  # berubah setelah rencana dibuat
    result, journal = run(tmp_path, bcd, esp_root, plan, gate.manifest)
    assert not result.completed
    assert [s.ok for s in result.steps] == [True, True, False]
    assert "Entri berubah" in result.steps[-1].message
    assert FEDORA in bcd.entries and ARCH in bcd.entries
    assert (esp_root / "EFI/fedora").exists() and (esp_root / "grub").exists()
    summary = summarize(journal.path)
    assert summary.finished and summary.ok is False


def test_invalid_signature_stops_before_anything_is_deleted(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    gate = gate_for(tmp_path, plan, report, backup)
    bcd.signature = '{"Status":"HashMismatch","Message":"Hash tidak cocok","Subject":"CN=Microsoft Windows, O=Microsoft Corporation"}'
    result, _ = run(tmp_path, bcd, esp_root, plan, gate.manifest)
    assert [s.ok for s in result.steps] == [True, False]
    assert "Tanda tangan" in result.steps[-1].message
    assert (esp_root / "EFI/Boot/bootx64.efi").read_bytes() == SHIM
    assert FEDORA in bcd.entries and (esp_root / "EFI/fedora").exists()


def test_file_changed_after_gate_blocks_that_folder(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    gate = gate_for(tmp_path, plan, report, backup)
    (esp_root / "grub/grub.cfg").write_text("berubah di tengah jalan", encoding="utf-8")
    result, _ = run(tmp_path, bcd, esp_root, plan, gate.manifest)
    assert result.steps[-1].kind == "delete-esp-item" and not result.steps[-1].ok
    assert "berubah sejak backup" in result.steps[-1].message
    assert (esp_root / "grub/grub.cfg").exists()


def test_bitlocker_is_suspended_first_when_needed(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    gate = gate_for(tmp_path, plan, report, backup)
    result, _ = run(tmp_path, bcd, esp_root, plan, gate.manifest, suspend_bitlocker=True)
    assert result.completed and result.steps[0].kind == "suspend-bitlocker"


def test_simulated_crash_leaves_an_incomplete_journal(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    gate = gate_for(tmp_path, plan, report, backup)
    with pytest.raises(SimulatedCrash):
        run(tmp_path, bcd, esp_root, plan, gate.manifest, fail_after="3")
    [unfinished] = incomplete_sessions(tmp_path / "journal")
    assert unfinished.unfinished_step.step == 3 and unfinished.unfinished_step.kind == "delete-boot-entry"
    assert bcd.order[0] == BOOTMGR_ID  # langkah 1-2 sudah jalan: laptop tetap boot ke Windows


def test_execution_runner_only_allows_planned_changes(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    runner = execution_runner(bcd, plan)
    for args in (
        ["bcdedit", "/delete", BOOTMGR_ID],
        ["bcdedit", "/delete", "{bootmgr}"],
        ["bcdedit", "/delete", "{12345678-1234-4234-8234-123456789abc}"],
        ["bcdedit", "/set", "{fwbootmgr}", "displayorder", FEDORA, "/addfirst"],
        ["bcdedit", "/set", "{bootmgr}", "path", "\\EFI\\fedora\\shimx64.efi"],
        ["bcdedit", "/import", "C:\\x.bak"],
    ):
        with pytest.raises(ReadOnlyViolation):
            runner.run(args)
    assert runner.run(["bcdedit", "/delete", FEDORA]).ok


@pytest.mark.parametrize(
    "path",
    ["", "EFI", "EFI\\Microsoft", "EFI\\Microsoft\\Boot", "efi\\microsoft\\boot\\bootmgfw.efi", "EFI\\Boot",
     "EFI\\Boot\\fbx64.efi", "EFI\\Boot\\bootx64.efi", "..\\..\\Windows", "EFI\\..\\..\\x"],
)
def test_protected_paths_are_refused(tmp_path, path):
    with pytest.raises(ExecutionError):
        safe_esp_path(tmp_path, path)


def test_only_bootx64_can_be_a_fallback_target(tmp_path):
    assert safe_esp_path(tmp_path, "EFI\\Boot\\bootx64.efi", fallback_target=True).name == "bootx64.efi"
    assert safe_esp_path(tmp_path, "EFI\\fedora").name == "fedora"
    with pytest.raises(ExecutionError):
        safe_esp_path(tmp_path, "EFI\\Microsoft\\Boot\\bootmgfw.efi", fallback_target=True)


def test_restore_folders_and_entries_after_uninstall(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    gate = gate_for(tmp_path, plan, report, backup)
    run(tmp_path, bcd, esp_root, plan, gate.manifest)
    local = tmp_path / "local" / backup.backup_id

    results = restore_esp_items(local, ["EFI\\fedora", "grub", "EFI\\Microsoft", "EFI\\Boot\\bootx64.efi"], esp_root)
    assert [r.ok for r in results] == [True, True, False, False]
    assert "Dilindungi" in results[2].message and "tidak ditimpa" in results[3].message
    assert (esp_root / "EFI/fedora/shimx64.efi").read_bytes() == SHIM

    runner, created = restore_runner(bcd)
    records = json.loads((local / "firmware-entries.json").read_text(encoding="utf-8"))
    fedora = next(r for r in records if r["description"] == "Fedora")
    restored = restore_boot_entry(runner, created, fedora)
    assert restored.ok, restored.message
    new = next(i for i, e in bcd.entries.items() if e["description"] == "Fedora")
    assert bcd.entries[new]["path"] == "\\EFI\\fedora\\shimx64.efi" and bcd.order[-1] == new and bcd.order[0] == BOOTMGR_ID
    assert restore_boot_entry(runner, created, fedora).message.startswith("Sudah ada")

    windows = next(r for r in records if r["description"] == "Windows Boot Manager")
    assert not restore_boot_entry(runner, created, windows).ok
    with pytest.raises(ReadOnlyViolation):
        runner.run(["bcdedit", "/delete", BOOTMGR_ID])
